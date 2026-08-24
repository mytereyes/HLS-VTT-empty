from __future__ import annotations

import pytest

from hls_vtt_s3.manifest_parser import parse_source_playlist
from hls_vtt_s3.models import SourcePlaylist
from hls_vtt_s3.vtt_generator import (
    format_timestamp,
    generate_vtt_playlist,
    generate_vtt_segments,
)


@pytest.mark.parametrize(
    ("milliseconds", "expected"),
    [
        (0, "00:00:00.000"),
        (6000, "00:00:06.000"),
        (12000, "00:00:12.000"),
        (15057, "00:00:15.057"),
        (3_600_000, "01:00:00.000"),
        (7_384_005, "02:03:04.005"),
        (100 * 3_600_000, "100:00:00.000"),
    ],
)
def test_timestamp_formatting(milliseconds: int, expected: str) -> None:
    assert format_timestamp(milliseconds) == expected


def test_exact_vtt_segments(video_text: str) -> None:
    source = parse_source_playlist(video_text)
    objects, cues = generate_vtt_segments(source, "eng")
    assert [item.relative_key for item in objects] == [
        "vtt-hls-h264-eng-0.vtt",
        "vtt-hls-h264-eng-1.vtt",
        "vtt-hls-h264-eng-2.vtt",
    ]
    ranges = [
        "00:00:00.000 --> 00:00:06.000",
        "00:00:06.000 --> 00:00:12.000",
        "00:00:12.000 --> 00:00:15.057",
    ]
    for generated, expected_range in zip(objects, ranges, strict=True):
        assert generated.body.decode("utf-8") == (
            "WEBVTT\n"
            "X-TIMESTAMP-MAP=MPEGTS:180000,LOCAL:00:00:00.000\n"
            "\n"
            f"{expected_range}\n"
        )
    assert [(cue.start_ms, cue.end_ms) for cue in cues] == [
        (0, 6000),
        (6000, 12000),
        (12000, 15057),
    ]
    assert [cue.end_ms - cue.start_ms for cue in cues] == [6000, 6000, 3057]


def test_exact_english_playlist(video_text: str) -> None:
    source = parse_source_playlist(video_text)
    generated = generate_vtt_playlist(source, "eng")
    assert generated.body.decode("utf-8") == (
        "#EXTM3U\n"
        "#EXT-X-VERSION:3\n"
        "#EXT-X-TARGETDURATION:6\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n"
        "#EXT-X-PLAYLIST-TYPE:VOD\n"
        "#EXTINF:6.000\n"
        "vtt-hls-h264-eng-0.vtt\n"
        "#EXTINF:6.000\n"
        "vtt-hls-h264-eng-1.vtt\n"
        "#EXTINF:3.057\n"
        "vtt-hls-h264-eng-2.vtt\n"
        "#EXT-X-ENDLIST\n"
    )


def test_french_playlist_names_and_media_sequence(video_text: str) -> None:
    source = parse_source_playlist(
        video_text.replace("#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-MEDIA-SEQUENCE:27")
    )
    text = generate_vtt_playlist(source, "fre").body.decode("utf-8")
    assert "#EXT-X-MEDIA-SEQUENCE:27" in text
    assert [line for line in text.splitlines() if line.endswith(".vtt")] == [
        "vtt-hls-h264-fre-0.vtt",
        "vtt-hls-h264-fre-1.vtt",
        "vtt-hls-h264-fre-2.vtt",
    ]


def test_target_duration_accounts_for_adjusted_final_duration() -> None:
    source = SourcePlaylist((1000, 6999), target_duration_seconds=6, media_sequence=0)
    text = generate_vtt_playlist(source, "eng").body.decode("utf-8")
    assert "#EXT-X-TARGETDURATION:8" in text
    assert "#EXTINF:7.014" in text
    extinf = [line for line in text.splitlines() if line.startswith("#EXTINF:")]
    assert all(len(line.rsplit(".", 1)[1]) == 3 for line in extinf)
