from __future__ import annotations

import pytest

from hls_vtt_s3.errors import ManifestConflictError, ParseError
from hls_vtt_s3.manifest_parser import modify_master_manifest

SUBTITLE_FRE = (
    '#EXT-X-MEDIA:TYPE=SUBTITLES,URI="h264_manifest-vtt-hls-h264-subtitle.m3u8",'
    'GROUP-ID="vtt",LANGUAGE="fre",NAME="Subtitle0",DEFAULT=NO,AUTOSELECT=YES'
)


def test_h264_manifest_is_modified_without_reformatting(h264_fre_text: str) -> None:
    updated = modify_master_manifest(h264_fre_text, "fre", "h264_manifest.m3u8")
    before = h264_fre_text.splitlines()
    after = updated.splitlines()
    audio_before = [line for line in before if "TYPE=AUDIO" in line]
    audio_after = [line for line in after if "TYPE=AUDIO" in line]
    assert audio_after == audio_before
    assert after.index(SUBTITLE_FRE) == max(after.index(line) for line in audio_after) + 1
    assert updated.count(SUBTITLE_FRE) == 1

    old_streams = [line for line in before if line.startswith("#EXT-X-STREAM-INF:")]
    new_streams = [line for line in after if line.startswith("#EXT-X-STREAM-INF:")]
    assert new_streams == [f'{line},SUBTITLES="vtt"' for line in old_streams]
    assert all(line.count('SUBTITLES="vtt"') == 1 for line in new_streams)
    assert 'CODECS="mp4a.40.2,avc1.640028"' in new_streams[0]
    assert 'CODECS="ac-3,avc1.64001f"' in new_streams[1]
    assert [line for line in after if line.endswith(".m3u8") and not line.startswith("#")] == [
        "video-1080.m3u8",
        "video-720.m3u8",
    ]
    assert updated.endswith("\n")


def test_crlf_newline_style_is_preserved(h264_fre_text: str) -> None:
    crlf = h264_fre_text.replace("\n", "\r\n")
    updated = modify_master_manifest(crlf, "fre", "h264_manifest.m3u8")
    assert "\r\n" in updated
    assert "\n" not in updated.replace("\r\n", "")


@pytest.mark.parametrize(
    "existing",
    [
        '#EXT-X-MEDIA:TYPE=SUBTITLES,URI="old.m3u8",GROUP-ID="old"',
        '#EXT-X-STREAM-INF:BANDWIDTH=1,SUBTITLES="old"\nvideo.m3u8',
    ],
)
def test_any_existing_subtitle_configuration_is_a_conflict(existing: str) -> None:
    text = f'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="fre"\n{existing}\n'
    with pytest.raises(ManifestConflictError):
        modify_master_manifest(text, "fre", "h264_manifest.m3u8")


def test_second_transformation_is_idempotent(h264_fre_text: str) -> None:
    once = modify_master_manifest(h264_fre_text, "fre", "h264_manifest.m3u8")
    twice = modify_master_manifest(once, "fre", "h264_manifest.m3u8")
    assert twice == once
    assert twice.count(SUBTITLE_FRE) == 1


def test_exact_declaration_is_preserved_and_missing_stream_references_are_added(
    h264_fre_text: str,
) -> None:
    source = h264_fre_text.replace(
        "#EXT-X-STREAM-INF:", f"{SUBTITLE_FRE}\n#EXT-X-STREAM-INF:", 1
    )
    first_stream = next(
        line for line in source.splitlines() if line.startswith("#EXT-X-STREAM-INF:")
    )
    source = source.replace(first_stream, f'{first_stream},SUBTITLES="vtt"', 1)

    updated = modify_master_manifest(source, "fre", "h264_manifest.m3u8")

    assert updated.count(SUBTITLE_FRE) == 1
    stream_lines = [
        line for line in updated.splitlines() if line.startswith("#EXT-X-STREAM-INF:")
    ]
    assert all(line.count('SUBTITLES="vtt"') == 1 for line in stream_lines)


@pytest.mark.parametrize(
    "declaration",
    [
        SUBTITLE_FRE.replace(
            "h264_manifest-vtt-hls-h264-subtitle.m3u8", "other.m3u8"
        ),
        SUBTITLE_FRE.replace('GROUP-ID="vtt"', 'GROUP-ID="other"'),
        SUBTITLE_FRE.replace('LANGUAGE="fre"', 'LANGUAGE="eng"'),
    ],
)
def test_conflicting_subtitle_declaration_still_fails(declaration: str) -> None:
    source = f'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="fre"\n{declaration}\n'
    with pytest.raises(ManifestConflictError):
        modify_master_manifest(source, "fre", "h264_manifest.m3u8")


def test_duplicate_exact_subtitle_declarations_fail() -> None:
    source = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="fre"\n'
        f"{SUBTITLE_FRE}\n{SUBTITLE_FRE}\n"
    )
    with pytest.raises(ManifestConflictError, match="multiple SUBTITLES"):
        modify_master_manifest(source, "fre", "h264_manifest.m3u8")


def test_conflicting_stream_subtitle_group_still_fails() -> None:
    source = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="fre"\n'
        f"{SUBTITLE_FRE}\n"
        '#EXT-X-STREAM-INF:BANDWIDTH=100,SUBTITLES="other"\nvideo.m3u8\n'
    )
    with pytest.raises(ManifestConflictError, match="does not exactly match"):
        modify_master_manifest(source, "fre", "h264_manifest.m3u8")


def test_unquoted_stream_subtitle_group_is_not_an_exact_match() -> None:
    source = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="fre"\n'
        f"{SUBTITLE_FRE}\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=100,SUBTITLES=vtt\nvideo.m3u8\n"
    )
    with pytest.raises(ManifestConflictError, match="does not exactly match"):
        modify_master_manifest(source, "fre", "h264_manifest.m3u8")


def test_audio_only_secondary_gets_one_declaration(secondary_fre_text: str) -> None:
    updated = modify_master_manifest(secondary_fre_text, "fre", "Manifest.m3u8")
    assert updated.count(SUBTITLE_FRE) == 1
    assert secondary_fre_text.splitlines()[2] in updated.splitlines()
    assert "SUBTITLES=\"vtt\"" not in "\n".join(
        line for line in updated.splitlines() if line.startswith("#EXT-X-STREAM-INF:")
    )


def test_secondary_with_variants_gets_stream_attributes(secondary_fre_text: str) -> None:
    source = secondary_fre_text + (
        '#EXT-X-STREAM-INF:BANDWIDTH=100,CODECS="avc1,mp4a"\nvideo.m3u8\n'
    )
    updated = modify_master_manifest(source, "fre", "Manifest.m3u8")
    assert '#EXT-X-STREAM-INF:BANDWIDTH=100,CODECS="avc1,mp4a",SUBTITLES="vtt"' in updated
    assert "video.m3u8" in updated.splitlines()


def test_stream_without_immediate_variant_uri_fails(secondary_fre_text: str) -> None:
    source = secondary_fre_text + "#EXT-X-STREAM-INF:BANDWIDTH=100\n#EXT-X-VERSION:3\n"
    with pytest.raises(ParseError, match="variant URI"):
        modify_master_manifest(source, "fre", "Manifest.m3u8")
