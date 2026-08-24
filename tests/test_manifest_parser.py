from __future__ import annotations

import logging

import pytest

from hls_vtt_s3.errors import ParseError, ValidationError
from hls_vtt_s3.manifest_parser import (
    decode_utf8,
    detect_audio_language,
    parse_source_playlist,
    validate_secondary_language,
)


def playlist(extinf_lines: list[str], *, endlist: bool = True) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:7"]
    for index, extinf in enumerate(extinf_lines):
        lines.extend((extinf, f"segment-{index}.ts"))
    if endlist:
        lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    ("line", "expected_ms"),
    [
        ("#EXTINF:6.000", 6000),
        ("#EXTINF:3.042", 3042),
        ("#EXTINF:6", 6000),
        ("#EXTINF:6.000,", 6000),
        ("#EXTINF:6.000,A title", 6000),
        ("#EXTINF:1.2344,round down", 1234),
        ("#EXTINF:1.2345,round half up", 1235),
        ("#EXTINF:1.2349,round up", 1235),
    ],
)
def test_duration_parsing_and_half_up_rounding(line: str, expected_ms: int) -> None:
    parsed = parse_source_playlist(playlist([line]))
    assert parsed.durations_ms == (expected_ms,)


@pytest.mark.parametrize("line", ["#EXTINF:not-a-decimal", "#EXTINF:0", "#EXTINF:-1"])
def test_invalid_or_nonpositive_duration_fails(line: str) -> None:
    with pytest.raises(ParseError):
        parse_source_playlist(playlist([line]))


def test_positive_submillisecond_value_that_rounds_to_zero_fails() -> None:
    with pytest.raises(ParseError, match="rounds to zero"):
        parse_source_playlist(playlist(["#EXTINF:0.0004"]))


def test_missing_extinf_fails() -> None:
    with pytest.raises(ParseError, match="no #EXTINF"):
        parse_source_playlist("#EXTM3U\n#EXT-X-ENDLIST\n")


def test_missing_endlist_fails() -> None:
    with pytest.raises(ParseError, match="ENDLIST"):
        parse_source_playlist(playlist(["#EXTINF:6"], endlist=False))


def test_target_duration_is_required_positive_and_unique() -> None:
    with pytest.raises(ParseError, match="missing #EXT-X-TARGETDURATION"):
        parse_source_playlist("#EXTM3U\n#EXTINF:1\nsegment.ts\n#EXT-X-ENDLIST\n")
    with pytest.raises(ParseError, match="must be positive"):
        parse_source_playlist(
            "#EXTM3U\n#EXT-X-TARGETDURATION:0\n#EXTINF:1\nsegment.ts\n#EXT-X-ENDLIST\n"
        )
    with pytest.raises(ParseError, match="duplicate #EXT-X-TARGETDURATION"):
        parse_source_playlist(
            "#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-TARGETDURATION:2\n"
            "#EXTINF:1\nsegment.ts\n#EXT-X-ENDLIST\n"
        )


@pytest.mark.parametrize(
    "text",
    [
        "#EXTM3U\n#EXTINF:1\n#EXT-X-ENDLIST\n",
        "#EXTM3U\n#EXTINF:1\n#EXTINF:2\nsegment.ts\n#EXT-X-ENDLIST\n",
        "#EXTM3U\nsegment.ts\n#EXT-X-ENDLIST\n",
        "#EXTM3U\n#EXTINF:1\nsegment.ts\n#EXT-X-ENDLIST\n#EXT-X-ENDLIST\n",
        "#EXTM3U\n#EXTINF:1\nsegment.ts\n#EXT-X-ENDLIST\nlate.ts\n",
    ],
)
def test_malformed_segment_structure_fails(text: str) -> None:
    with pytest.raises(ParseError):
        parse_source_playlist(text)


def test_first_nonempty_line_must_be_header() -> None:
    with pytest.raises(ParseError, match="start"):
        parse_source_playlist("\n#comment\n#EXTM3U\n#EXTINF:1\n#EXT-X-ENDLIST\n")


def test_media_sequence_defaults_to_zero_and_is_preserved(video_text: str) -> None:
    assert parse_source_playlist(video_text).media_sequence == 0
    changed = video_text.replace("#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-MEDIA-SEQUENCE:42")
    assert parse_source_playlist(changed).media_sequence == 42


def test_invalid_utf8_and_empty_object_fail() -> None:
    with pytest.raises(ParseError, match="valid UTF-8"):
        decode_utf8(b"\xff\xfe", "bad.m3u8")
    with pytest.raises(ParseError, match="empty"):
        decode_utf8(b"", "empty.m3u8")


@pytest.mark.parametrize("language", ["eng", "fre", "spa"])
def test_detects_supported_conservative_language_tokens(language: str) -> None:
    text = f'#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="{language}",GROUP-ID="a"\n'
    assert detect_audio_language(text).language == language


def test_multiple_identical_languages_do_not_warn(caplog: pytest.LogCaptureFixture) -> None:
    text = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="fre"\n'
        '#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="fre"\n'
    )
    with caplog.at_level(logging.WARNING):
        selected = detect_audio_language(text)
    assert selected.distinct_languages == ("fre",)
    assert not caplog.records


def test_multiple_languages_select_first_and_warn(caplog: pytest.LogCaptureFixture) -> None:
    text = (
        '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="fre"\n'
        '#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="eng"\n'
    )
    with caplog.at_level(logging.WARNING):
        selected = detect_audio_language(text)
    assert selected.language == "fre"
    assert "selected first language fre" in caplog.text


def test_missing_or_invalid_language_fails() -> None:
    with pytest.raises(ParseError, match="no valid LANGUAGE"):
        detect_audio_language("#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID=\"a\"\n")
    with pytest.raises(ValidationError, match="Invalid language token"):
        detect_audio_language(
            '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="../eng"\n'
        )


def test_secondary_language_mismatch_fails() -> None:
    text = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,LANGUAGE="spa"\n'
    with pytest.raises(ValidationError, match="Language mismatch"):
        validate_secondary_language(text, "eng")


def test_secondary_without_audio_is_allowed() -> None:
    validate_secondary_language("#EXTM3U\n#EXT-X-VERSION:3\n", "eng")
