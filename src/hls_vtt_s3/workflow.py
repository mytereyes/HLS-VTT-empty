"""Per-ad and batch orchestration, separated from CLI and boto3 creation."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone

from .errors import HlsVttError, PromotionError, S3RepositoryError
from .manifest_parser import (
    decode_utf8,
    detect_audio_language,
    modify_master_manifest,
    parse_source_playlist,
    validate_secondary_language,
)
from .models import (
    AdResult,
    AdStatus,
    BatchReport,
    GeneratedObject,
    H264_MANIFEST_NAME,
    HLS_CONTENT_TYPE,
    PreparedAd,
    SECONDARY_MANIFEST_NAME,
    VIDEO_PLAYLIST_NAME,
    VTT_PLAYLIST_NAME,
)
from .s3_repository import S3Repository, join_key, normalize_prefix
from .vtt_generator import generate_vtt_playlist, generate_vtt_segments

LOGGER = logging.getLogger(__name__)


def _staging_prefix(ad_prefix: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{normalize_prefix(ad_prefix)}_vtt_staging/{timestamp}-{uuid.uuid4().hex}/"


def prepare_ad(repository: S3Repository, ad_prefix: str) -> PreparedAd:
    """Read, parse, transform, and validate one ad without any S3 writes."""
    source_names = (VIDEO_PLAYLIST_NAME, H264_MANIFEST_NAME, SECONDARY_MANIFEST_NAME)
    source_keys = {name: join_key(ad_prefix, name) for name in source_names}

    # Complete existence/snapshot validation happens before the first source download.
    snapshots_by_name = {
        name: repository.snapshot_object(
            source_keys[name],
            preserve_metadata=name in {H264_MANIFEST_NAME, SECONDARY_MANIFEST_NAME},
        )
        for name in source_names
    }
    raw = {
        name: repository.get_object_bytes(
            source_keys[name],
            version_id=snapshots_by_name[name].version_id,
            expected_etag=snapshots_by_name[name].etag,
        )
        for name in source_names
    }
    texts = {name: decode_utf8(raw[name], name) for name in source_names}

    source_playlist = parse_source_playlist(texts[VIDEO_PLAYLIST_NAME])
    language_selection = detect_audio_language(texts[H264_MANIFEST_NAME])
    language = language_selection.language
    validate_secondary_language(texts[SECONDARY_MANIFEST_NAME], language)

    segments, _cues = generate_vtt_segments(source_playlist, language)
    playlist = generate_vtt_playlist(source_playlist, language)
    modified_h264 = modify_master_manifest(
        texts[H264_MANIFEST_NAME], language, H264_MANIFEST_NAME
    )
    modified_secondary = modify_master_manifest(
        texts[SECONDARY_MANIFEST_NAME], language, SECONDARY_MANIFEST_NAME
    )
    objects = segments + (
        playlist,
        GeneratedObject(
            H264_MANIFEST_NAME,
            modified_h264.encode("utf-8"),
            HLS_CONTENT_TYPE,
            snapshots_by_name[H264_MANIFEST_NAME].preserved_args,
        ),
        GeneratedObject(
            SECONDARY_MANIFEST_NAME,
            modified_secondary.encode("utf-8"),
            HLS_CONTENT_TYPE,
            snapshots_by_name[SECONDARY_MANIFEST_NAME].preserved_args,
        ),
    )
    relative_keys = [item.relative_key for item in objects]
    if len(relative_keys) != len(set(relative_keys)):
        raise S3RepositoryError("Generated output contains duplicate object keys.")
    if any(not item.body for item in objects):
        raise S3RepositoryError("Generated output unexpectedly contains an empty object.")
    return PreparedAd(
        language=language,
        source_playlist=source_playlist,
        objects=objects,
        segment_keys=tuple(item.relative_key for item in segments),
        source_snapshots=tuple(snapshots_by_name[name] for name in source_names),
    )


def _base_result(ad_prefix: str, dry_run: bool, prepared: PreparedAd) -> AdResult:
    return AdResult(
        ad_prefix=ad_prefix,
        status=AdStatus.PROCESSED,
        dry_run=dry_run,
        language=prepared.language,
        source_segment_count=len(prepared.source_playlist.durations_ms),
        generated_vtt_count=prepared.generated_vtt_count,
        total_original_duration_ms=prepared.source_playlist.total_duration_ms,
        total_vtt_duration_ms=prepared.source_playlist.total_duration_ms + 15,
        planned_objects=[join_key(ad_prefix, item.relative_key) for item in prepared.objects],
    )


def process_ad(
    repository: S3Repository,
    ad_prefix: str,
    *,
    apply: bool,
    keep_staging_on_success: bool = False,
) -> AdResult:
    """Process one ad; all expected failures become a FAILED result."""
    ad_prefix = normalize_prefix(ad_prefix)
    dry_run = not apply
    final_playlist_key = join_key(ad_prefix, VTT_PLAYLIST_NAME)
    prepared: PreparedAd | None = None
    staging_prefix: str | None = None
    promoted: list[str] = []

    try:
        if repository.object_exists(final_playlist_key):
            return AdResult(
                ad_prefix=ad_prefix,
                status=AdStatus.SKIPPED,
                dry_run=dry_run,
                message="VTT subtitle manifest already exists.",
            )

        prepared = prepare_ad(repository, ad_prefix)
        result = _base_result(ad_prefix, dry_run, prepared)
        for snapshot in prepared.source_snapshots:
            repository.assert_unchanged(snapshot)
        if dry_run:
            result.message = "Validated successfully; no S3 writes performed."
            LOGGER.info(
                "%s DRY-RUN would write: %s",
                ad_prefix,
                ", ".join(result.planned_objects),
            )
            return result

        for segment_key in prepared.segment_keys:
            final_segment_key = join_key(ad_prefix, segment_key)
            if repository.object_exists(final_segment_key):
                raise PromotionError(
                    "Concurrency conflict: generated VTT segment destination already exists: "
                    f"{final_segment_key!r}; no writes were made."
                )

        # A second check immediately before the first write narrows the race window.
        if repository.object_exists(final_playlist_key):
            raise PromotionError(
                "Final VTT subtitle manifest appeared before staging; no writes were made."
            )

        staging_prefix = _staging_prefix(ad_prefix)
        staged_keys: list[str] = []
        for item in prepared.objects:
            staged_key = join_key(staging_prefix, item.relative_key)
            repository.put_object(staged_key, item)
            staged_keys.append(staged_key)

        # Full byte/hash comparison proves staged content equals prevalidated content.
        for staged_key, item in zip(staged_keys, prepared.objects, strict=True):
            repository.verify_object(staged_key, item)

        for snapshot in prepared.source_snapshots:
            repository.assert_unchanged(snapshot)

        # Required optimistic-concurrency guard immediately before final promotion.
        if repository.object_exists(final_playlist_key):
            raise PromotionError(
                "Final VTT subtitle manifest appeared after staging; promotion was not started.",
                staging_prefix=staging_prefix,
            )

        # prepared.objects is deliberately ordered: segments, playlist, h264, secondary.
        snapshots_by_key = {snapshot.key: snapshot for snapshot in prepared.source_snapshots}
        for staged_key, item in zip(staged_keys, prepared.objects, strict=True):
            final_key = join_key(ad_prefix, item.relative_key)
            original = snapshots_by_key.get(final_key)
            try:
                repository.copy_object(
                    staged_key,
                    final_key,
                    item,
                    destination_etag=original.etag if original is not None else None,
                )
                promoted.append(final_key)
                repository.verify_object(final_key, item)
            except HlsVttError as exc:
                raise PromotionError(
                    f"Promotion failed for {final_key!r}: {exc}",
                    promoted_objects=tuple(promoted),
                    staging_prefix=staging_prefix,
                ) from exc

        if not keep_staging_on_success:
            try:
                repository.delete_objects(staged_keys)
            except HlsVttError as exc:
                raise PromotionError(
                    f"Final promotion succeeded, but staging cleanup failed: {exc}",
                    promoted_objects=tuple(promoted),
                    staging_prefix=staging_prefix,
                ) from exc

        result.staging_prefix = staging_prefix
        result.promoted_objects = promoted
        result.message = (
            "Promoted and retained staging objects."
            if keep_staging_on_success
            else "Promoted successfully and removed staging objects."
        )
        return result
    except Exception as exc:  # Per-ad isolation is an explicit batch requirement.
        if isinstance(exc, PromotionError):
            promoted = list(exc.promoted_objects) or promoted
            staging_prefix = exc.staging_prefix or staging_prefix
        result = AdResult(
            ad_prefix=ad_prefix,
            status=AdStatus.FAILED,
            dry_run=dry_run,
            staging_prefix=staging_prefix,
            error_type=type(exc).__name__,
            error_message=str(exc),
            promoted_objects=promoted,
            message="Ad processing failed; final changes were not claimed as rolled back.",
        )
        if prepared is not None:
            result.language = prepared.language
            result.source_segment_count = len(prepared.source_playlist.durations_ms)
            result.generated_vtt_count = prepared.generated_vtt_count
            result.total_original_duration_ms = prepared.source_playlist.total_duration_ms
            result.total_vtt_duration_ms = prepared.source_playlist.total_duration_ms + 15
            result.planned_objects = [
                join_key(ad_prefix, item.relative_key) for item in prepared.objects
            ]
        if isinstance(exc, HlsVttError):
            LOGGER.error("%s FAILED: %s: %s", ad_prefix, type(exc).__name__, exc)
        else:
            LOGGER.exception("%s FAILED with an unexpected internal error", ad_prefix)
        return result


def process_batch(
    repository: S3Repository,
    base_prefix: str,
    *,
    apply: bool,
    ad_id: str | None = None,
    keep_staging_on_success: bool = False,
) -> BatchReport:
    started = time.monotonic()
    normalized_base = normalize_prefix(base_prefix)
    ad_prefixes = (
        [repository.ad_prefix_for_id(normalized_base, ad_id)]
        if ad_id is not None
        else repository.discover_ad_prefixes(normalized_base)
    )
    results: list[AdResult] = []
    for ad_prefix in ad_prefixes:
        LOGGER.info("Processing ad prefix %s", ad_prefix)
        result = process_ad(
            repository,
            ad_prefix,
            apply=apply,
            keep_staging_on_success=keep_staging_on_success,
        )
        LOGGER.info("%s %s: %s", ad_prefix, result.status.value, result.message)
        results.append(result)
    return BatchReport(
        bucket=repository.bucket,
        base_prefix=normalized_base,
        dry_run=not apply,
        elapsed_seconds=time.monotonic() - started,
        results=tuple(results),
    )
