from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from conftest import BASE, BUCKET, FakeS3Client, seed_ad
from hls_vtt_s3.errors import PermissionDeniedError
from hls_vtt_s3.models import (
    AdStatus,
    H264_MANIFEST_NAME,
    SECONDARY_MANIFEST_NAME,
    VIDEO_PLAYLIST_NAME,
    VTT_PLAYLIST_NAME,
)
from hls_vtt_s3.s3_repository import S3Repository
from hls_vtt_s3.workflow import process_ad, process_batch

WRITE_OPERATIONS = ("put_object", "copy_object", "delete_objects")


def repository(fake: FakeS3Client) -> S3Repository:
    return S3Repository(fake, BUCKET)


def test_discovery_handles_pagination_and_filters_non_immediate_prefixes(
    fake_s3: FakeS3Client,
) -> None:
    fake_s3.list_pages = [
        {
            "IsTruncated": True,
            "NextContinuationToken": "page-1",
            "CommonPrefixes": [
                {"Prefix": BASE + "AD2/"},
                {"Prefix": BASE + "AD1/nested/"},
                {"Prefix": BASE + "_vtt_staging/"},
            ],
        },
        {
            "IsTruncated": False,
            "CommonPrefixes": [
                {"Prefix": BASE + "AD1/"},
                {"Prefix": BASE + "AD2/"},
            ],
        },
    ]
    discovered = repository(fake_s3).discover_ad_prefixes(BASE.rstrip("/"))
    assert discovered == [BASE + "AD1/", BASE + "AD2/"]
    list_calls = fake_s3.operations("list_objects_v2")
    assert len(list_calls) == 2
    assert list_calls[0]["Delimiter"] == "/"
    assert list_calls[1]["ContinuationToken"] == "page-1"


def test_empty_base_prefix_has_zero_ads(fake_s3: FakeS3Client) -> None:
    report = process_batch(repository(fake_s3), BASE, apply=False)
    assert report.total_ads_discovered == 0
    assert report.failed == 0


def test_single_ad_filter_does_not_list_other_ads(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    seed_ad(fake_s3, "ONLY", video_text, h264_fre_text, secondary_fre_text)
    seed_ad(fake_s3, "OTHER", video_text, h264_fre_text, secondary_fre_text)
    report = process_batch(repository(fake_s3), BASE, apply=False, ad_id="ONLY/")
    assert [item.ad_prefix for item in report.results] == [BASE + "ONLY/"]
    assert not fake_s3.operations("list_objects_v2")


def test_existing_vtt_playlist_skips_without_source_download_or_writes(
    fake_s3: FakeS3Client,
) -> None:
    prefix = BASE + "DONE/"
    fake_s3.seed(prefix + VTT_PLAYLIST_NAME, "already exists")
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.SKIPPED
    assert result.message == "VTT subtitle manifest already exists."
    assert not fake_s3.operations("get_object", *WRITE_OPERATIONS)


@pytest.mark.parametrize(
    "missing_name", [VIDEO_PLAYLIST_NAME, H264_MANIFEST_NAME, SECONDARY_MANIFEST_NAME]
)
def test_each_missing_source_fails_without_writes(
    fake_s3: FakeS3Client,
    video_text: str,
    h264_fre_text: str,
    secondary_fre_text: str,
    missing_name: str,
) -> None:
    prefix = seed_ad(fake_s3, "MISSING", video_text, h264_fre_text, secondary_fre_text)
    fake_s3.objects.pop(prefix + missing_name)
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.FAILED
    assert missing_name in (result.error_message or "")
    assert not fake_s3.operations(*WRITE_OPERATIONS)


def test_dry_run_parses_and_reports_but_never_writes(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "DRY", video_text, h264_fre_text, secondary_fre_text)
    result = process_ad(repository(fake_s3), prefix, apply=False)
    assert result.status is AdStatus.PROCESSED
    assert result.language == "fre"
    assert result.source_segment_count == result.generated_vtt_count == 3
    assert result.total_original_duration_ms == 15042
    assert result.total_vtt_duration_ms == 15057
    assert len(result.planned_objects) == 6
    assert len(fake_s3.operations("get_object")) == 3
    assert all("IfMatch" in call for call in fake_s3.operations("get_object")[:3])
    assert not fake_s3.operations(*WRITE_OPERATIONS)


def test_apply_reconciles_exact_existing_vtt_tags_when_playlist_is_missing(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    subtitle = (
        '#EXT-X-MEDIA:TYPE=SUBTITLES,URI="h264_manifest-vtt-hls-h264-subtitle.m3u8",'
        'GROUP-ID="vtt",LANGUAGE="fre",NAME="Subtitle0",DEFAULT=NO,AUTOSELECT=YES'
    )
    h264_with_partial_vtt = h264_fre_text.replace(
        "#EXT-X-STREAM-INF:", f"{subtitle}\n#EXT-X-STREAM-INF:", 1
    )
    secondary_with_declaration = secondary_fre_text + f"{subtitle}\n"
    prefix = seed_ad(
        fake_s3,
        "RECONCILE",
        video_text,
        h264_with_partial_vtt,
        secondary_with_declaration,
    )

    result = process_ad(repository(fake_s3), prefix, apply=True)

    assert result.status is AdStatus.PROCESSED
    updated_h264 = fake_s3.objects[prefix + H264_MANIFEST_NAME].body.decode("utf-8")
    updated_secondary = fake_s3.objects[prefix + SECONDARY_MANIFEST_NAME].body.decode(
        "utf-8"
    )
    assert updated_h264.count(subtitle) == 1
    assert updated_secondary.count(subtitle) == 1
    assert all(
        line.count('SUBTITLES="vtt"') == 1
        for line in updated_h264.splitlines()
        if line.startswith("#EXT-X-STREAM-INF:")
    )
    assert prefix + VTT_PLAYLIST_NAME in fake_s3.objects


def test_apply_normalizes_stale_subtitle_language_to_authoritative_audio_language(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    stale = (
        '#EXT-X-MEDIA:TYPE=SUBTITLES,URI="h264_manifest-vtt-hls-h264-subtitle.m3u8",'
        'GROUP-ID="vtt",LANGUAGE="eng",NAME="Subtitle0",DEFAULT=NO,AUTOSELECT=YES'
    )
    expected = stale.replace('LANGUAGE="eng"', 'LANGUAGE="fre"')
    prefix = seed_ad(
        fake_s3,
        "NORMALIZE",
        video_text,
        h264_fre_text + stale + "\n",
        secondary_fre_text,
    )

    result = process_ad(repository(fake_s3), prefix, apply=True)

    assert result.status is AdStatus.PROCESSED
    assert result.language == "fre"
    updated_h264 = fake_s3.objects[prefix + H264_MANIFEST_NAME].body.decode("utf-8")
    lines = updated_h264.splitlines()
    audio_indexes = [index for index, line in enumerate(lines) if "TYPE=AUDIO" in line]
    assert stale not in lines
    assert lines.count(expected) == 1
    assert lines.index(expected) == max(audio_indexes) + 1
    assert prefix + "vtt-hls-h264-fre-0.vtt" in fake_s3.objects
    assert prefix + VTT_PLAYLIST_NAME in fake_s3.objects


def test_conflicting_existing_subtitle_declaration_fails_without_writes(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    conflict = (
        '#EXT-X-MEDIA:TYPE=SUBTITLES,URI="other.m3u8",GROUP-ID="other",'
        'LANGUAGE="fre",NAME="Other",DEFAULT=NO,AUTOSELECT=YES'
    )
    prefix = seed_ad(
        fake_s3,
        "CONFLICT",
        video_text,
        h264_fre_text.replace(
            "#EXT-X-STREAM-INF:", f"{conflict}\n#EXT-X-STREAM-INF:", 1
        ),
        secondary_fre_text,
    )

    result = process_ad(repository(fake_s3), prefix, apply=True)

    assert result.status is AdStatus.FAILED
    assert result.error_type == "ManifestConflictError"
    assert "expected URI" in (result.error_message or "")
    assert not fake_s3.operations(*WRITE_OPERATIONS)


def test_apply_stages_verifies_promotes_in_dependency_order_and_cleans_up(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "APPLY", video_text, h264_fre_text, secondary_fre_text)
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.PROCESSED
    put_calls = fake_s3.operations("put_object")
    copy_calls = fake_s3.operations("copy_object")
    delete_calls = fake_s3.operations("delete_objects")
    assert len(put_calls) == len(copy_calls) == 6
    assert all("_vtt_staging/" in call["Key"] for call in put_calls)
    assert [call["Key"] for call in copy_calls] == [
        prefix + "vtt-hls-h264-fre-0.vtt",
        prefix + "vtt-hls-h264-fre-1.vtt",
        prefix + "vtt-hls-h264-fre-2.vtt",
        prefix + VTT_PLAYLIST_NAME,
        prefix + H264_MANIFEST_NAME,
        prefix + SECONDARY_MANIFEST_NAME,
    ]
    assert len(delete_calls) == 1
    assert not any("_vtt_staging/" in key for key in fake_s3.objects)
    assert fake_s3.objects[prefix + "vtt-hls-h264-fre-0.vtt"].content_type == (
        "text/vtt; charset=utf-8"
    )
    assert fake_s3.objects[prefix + VTT_PLAYLIST_NAME].content_type == (
        "application/vnd.apple.mpegurl"
    )


def test_master_cache_headers_and_custom_metadata_are_preserved(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "META", video_text, h264_fre_text, secondary_fre_text)
    fake_s3.seed(
        prefix + H264_MANIFEST_NAME,
        h264_fre_text,
        "application/old-type",
        CacheControl="max-age=60",
        ContentLanguage="fr",
        Metadata={"owner": "ads"},
    )
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.PROCESSED
    stored = fake_s3.objects[prefix + H264_MANIFEST_NAME]
    assert stored.content_type == "application/vnd.apple.mpegurl"
    assert stored.properties == {
        "CacheControl": "max-age=60",
        "ContentLanguage": "fr",
        "Metadata": {"owner": "ads"},
    }


def test_existing_generated_segment_collision_fails_without_overwrite(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "COLLIDE", video_text, h264_fre_text, secondary_fre_text)
    collision = prefix + "vtt-hls-h264-fre-0.vtt"
    fake_s3.seed(collision, b"unrelated")
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.FAILED
    assert "Concurrency conflict" in (result.error_message or "")
    assert fake_s3.objects[collision].body == b"unrelated"
    assert result.promoted_objects == []


def test_source_change_after_staging_fails_before_promotion(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "CHANGED", video_text, h264_fre_text, secondary_fre_text)
    master_key = prefix + H264_MANIFEST_NAME
    fake_s3.mutate_on_head_call[master_key] = (2, b"newer concurrent content")
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.FAILED
    assert "source object changed" in (result.error_message or "")
    assert not fake_s3.operations("copy_object", "delete_objects")
    assert fake_s3.objects[master_key].body == b"newer concurrent content"


def test_staging_verification_failure_never_touches_final_keys_and_retains_staging(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "BADSTAGE", video_text, h264_fre_text, secondary_fre_text)
    fake_s3.corrupt_staged_get_suffix = VTT_PLAYLIST_NAME
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.FAILED
    assert "Content hash mismatch" in (result.error_message or "")
    assert fake_s3.operations("put_object")
    assert not fake_s3.operations("copy_object", "delete_objects")
    assert any("_vtt_staging/" in key for key in fake_s3.objects)
    assert prefix + VTT_PLAYLIST_NAME not in fake_s3.objects


def test_partial_promotion_is_reported_and_staging_is_retained(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "PARTIAL", video_text, h264_fre_text, secondary_fre_text)
    fake_s3.fail_copy_destination = prefix + H264_MANIFEST_NAME
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.FAILED
    assert result.promoted_objects == [
        prefix + "vtt-hls-h264-fre-0.vtt",
        prefix + "vtt-hls-h264-fre-1.vtt",
        prefix + "vtt-hls-h264-fre-2.vtt",
        prefix + VTT_PLAYLIST_NAME,
    ]
    assert result.staging_prefix and "_vtt_staging/" in result.staging_prefix
    assert len(result.planned_objects) == 6
    assert any(key.startswith(result.staging_prefix) for key in fake_s3.objects)
    assert not fake_s3.operations("delete_objects")


def test_final_playlist_race_after_staging_causes_safe_failure(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "RACE", video_text, h264_fre_text, secondary_fre_text)
    final_key = prefix + VTT_PLAYLIST_NAME
    fake_s3.appear_on_head_call[final_key] = 3
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.FAILED
    assert "appeared after staging" in (result.error_message or "")
    assert fake_s3.operations("put_object")
    assert not fake_s3.operations("copy_object", "delete_objects")
    assert any("_vtt_staging/" in key for key in fake_s3.objects)


def test_keep_staging_on_success_retains_staged_objects(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "KEEP", video_text, h264_fre_text, secondary_fre_text)
    result = process_ad(
        repository(fake_s3), prefix, apply=True, keep_staging_on_success=True
    )
    assert result.status is AdStatus.PROCESSED
    assert not fake_s3.operations("delete_objects")
    assert result.staging_prefix
    assert any(key.startswith(result.staging_prefix) for key in fake_s3.objects)


def test_staging_upload_failure_is_reported_and_no_promotion_occurs(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "PUTFAIL", video_text, h264_fre_text, secondary_fre_text)
    # Discover the dynamic staging key by failing every matching call through a tiny override.
    original_put = fake_s3.put_object

    def fail_second_put(**kwargs: Any) -> dict[str, Any]:
        if len(fake_s3.operations("put_object")) == 1:
            fake_s3.fail_put_key = kwargs["Key"]
        return original_put(**kwargs)

    fake_s3.put_object = fail_second_put  # type: ignore[method-assign]
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.FAILED
    assert "injected put failure" in (result.error_message or "")
    assert not fake_s3.operations("copy_object", "delete_objects")


def test_cleanup_failure_reports_all_promoted_objects_and_retains_staging(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    prefix = seed_ad(fake_s3, "DELETEFAIL", video_text, h264_fre_text, secondary_fre_text)
    fake_s3.fail_delete = True
    result = process_ad(repository(fake_s3), prefix, apply=True)
    assert result.status is AdStatus.FAILED
    assert len(result.promoted_objects) == 6
    assert len(result.planned_objects) == 6
    assert result.staging_prefix
    assert any(key.startswith(result.staging_prefix) for key in fake_s3.objects)


def test_batch_continues_across_skipped_processed_and_failed_ads(
    fake_s3: FakeS3Client, video_text: str, h264_fre_text: str, secondary_fre_text: str
) -> None:
    complete = BASE + "A-COMPLETE/"
    fake_s3.seed(complete + VTT_PLAYLIST_NAME, "existing")
    seed_ad(fake_s3, "B-VALID", video_text, h264_fre_text, secondary_fre_text)
    failed = seed_ad(fake_s3, "C-FAILED", video_text, h264_fre_text, secondary_fre_text)
    fake_s3.objects.pop(failed + SECONDARY_MANIFEST_NAME)

    report = process_batch(repository(fake_s3), BASE, apply=False)
    assert [item.status for item in report.results] == [
        AdStatus.SKIPPED,
        AdStatus.PROCESSED,
        AdStatus.FAILED,
    ]
    assert (report.processed, report.skipped, report.failed) == (1, 1, 1)
    assert report.total_vtt_segments_generated == 3


def test_access_denied_is_not_misclassified_as_missing() -> None:
    class DeniedClient:
        def head_object(self, **_kwargs: Any) -> dict[str, Any]:
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "HeadObject"
            )

    with pytest.raises(PermissionDeniedError):
        S3Repository(DeniedClient(), BUCKET).object_exists("secret")
