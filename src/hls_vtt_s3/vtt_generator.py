"""Pure WebVTT segment and subtitle-playlist generation."""

from __future__ import annotations

from .errors import ValidationError
from .manifest_parser import validate_language_token
from .models import (
    Cue,
    GeneratedObject,
    HLS_CONTENT_TYPE,
    SourcePlaylist,
    VTT_CONTENT_TYPE,
    VTT_PLAYLIST_NAME,
)

FINAL_EXTENSION_MS = 15
TIMESTAMP_MAP = "X-TIMESTAMP-MAP=MPEGTS:180000,LOCAL:00:00:00.000"


def format_timestamp(milliseconds: int) -> str:
    if milliseconds < 0:
        raise ValidationError("WebVTT timestamps cannot be negative.")
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def build_cues(durations_ms: tuple[int, ...]) -> tuple[Cue, ...]:
    if not durations_ms:
        raise ValidationError("At least one duration is required to generate WebVTT cues.")
    cues: list[Cue] = []
    start_ms = 0
    for index, duration_ms in enumerate(durations_ms):
        if duration_ms <= 0:
            raise ValidationError("WebVTT durations must be positive.")
        adjusted = duration_ms + (FINAL_EXTENSION_MS if index == len(durations_ms) - 1 else 0)
        end_ms = start_ms + adjusted
        cues.append(Cue(start_ms, end_ms))
        start_ms = end_ms
    validate_cues(tuple(cues), durations_ms)
    return tuple(cues)


def _segment_name(language: str, index: int) -> str:
    return f"vtt-hls-h264-{language}-{index}.vtt"


def _segment_body(cue: Cue) -> bytes:
    text = (
        "WEBVTT\n"
        f"{TIMESTAMP_MAP}\n"
        "\n"
        f"{format_timestamp(cue.start_ms)} --> {format_timestamp(cue.end_ms)}\n"
    )
    return text.encode("utf-8")


def generate_vtt_segments(
    source: SourcePlaylist, language: str
) -> tuple[tuple[GeneratedObject, ...], tuple[Cue, ...]]:
    validate_language_token(language)
    cues = build_cues(source.durations_ms)
    objects = tuple(
        GeneratedObject(_segment_name(language, index), _segment_body(cue), VTT_CONTENT_TYPE)
        for index, cue in enumerate(cues)
    )
    validate_vtt_segments(objects, cues, source, language)
    return objects, cues


def generate_vtt_playlist(source: SourcePlaylist, language: str) -> GeneratedObject:
    validate_language_token(language)
    adjusted_durations = list(source.durations_ms)
    adjusted_durations[-1] += FINAL_EXTENSION_MS
    target_duration = max(
        source.target_duration_seconds,
        (max(adjusted_durations) + 999) // 1000,
    )

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        f"#EXT-X-MEDIA-SEQUENCE:{source.media_sequence}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for index, duration_ms in enumerate(adjusted_durations):
        lines.extend(
            [
                f"#EXTINF:{duration_ms // 1000}.{duration_ms % 1000:03d}",
                _segment_name(language, index),
            ]
        )
    lines.append("#EXT-X-ENDLIST")
    generated = GeneratedObject(
        VTT_PLAYLIST_NAME,
        ("\n".join(lines) + "\n").encode("utf-8"),
        HLS_CONTENT_TYPE,
    )
    validate_vtt_playlist(generated, source, language, adjusted_durations, target_duration)
    return generated


def validate_cues(cues: tuple[Cue, ...], durations_ms: tuple[int, ...]) -> None:
    if len(cues) != len(durations_ms):
        raise ValidationError("Cue count does not match source duration count.")
    if cues[0].start_ms != 0:
        raise ValidationError("The first WebVTT cue must start at zero.")
    for index, cue in enumerate(cues):
        if cue.end_ms < cue.start_ms:
            raise ValidationError(f"Cue {index} ends before it starts.")
        if index and cue.start_ms != cues[index - 1].end_ms:
            raise ValidationError(f"Cue {index} is not contiguous with the preceding cue.")
        expected_duration = durations_ms[index] + (
            FINAL_EXTENSION_MS if index == len(cues) - 1 else 0
        )
        if cue.end_ms - cue.start_ms != expected_duration:
            raise ValidationError(f"Cue {index} has an incorrect duration.")


def validate_vtt_segments(
    objects: tuple[GeneratedObject, ...],
    cues: tuple[Cue, ...],
    source: SourcePlaylist,
    language: str,
) -> None:
    if len(objects) != len(source.durations_ms):
        raise ValidationError("Generated VTT segment count does not match source #EXTINF count.")
    validate_cues(cues, source.durations_ms)
    for index, (generated, cue) in enumerate(zip(objects, cues, strict=True)):
        if generated.relative_key != _segment_name(language, index):
            raise ValidationError(f"Generated VTT filename at index {index} is invalid.")
        expected = _segment_body(cue)
        if generated.body != expected:
            raise ValidationError(f"Generated VTT content at index {index} is invalid.")
        if not generated.body.endswith(b"\n"):
            raise ValidationError(f"Generated VTT segment {generated.relative_key} lacks a newline.")


def validate_vtt_playlist(
    generated: GeneratedObject,
    source: SourcePlaylist,
    language: str,
    adjusted_durations: list[int] | None = None,
    target_duration: int | None = None,
) -> None:
    text = generated.body.decode("utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != "#EXTM3U":
        raise ValidationError("Generated VTT playlist must start with #EXTM3U.")
    if "#EXT-X-ENDLIST" not in lines or "#EXT-X-PLAYLIST-TYPE:VOD" not in lines:
        raise ValidationError("Generated VTT playlist is not a complete VOD playlist.")
    extinf = [line for line in lines if line.startswith("#EXTINF:")]
    uris = [line for line in lines if line and not line.startswith("#")]
    if len(extinf) != len(source.durations_ms) or len(uris) != len(extinf):
        raise ValidationError("Generated VTT playlist entry counts are inconsistent.")
    expected_durations = list(source.durations_ms)
    expected_durations[-1] += FINAL_EXTENSION_MS
    if adjusted_durations is not None and adjusted_durations != expected_durations:
        raise ValidationError("Adjusted playlist durations are invalid.")
    expected_uris = [_segment_name(language, index) for index in range(len(extinf))]
    if uris != expected_uris:
        raise ValidationError("Generated VTT playlist references unexpected segment names.")
    expected_extinf = [
        f"#EXTINF:{value // 1000}.{value % 1000:03d}" for value in expected_durations
    ]
    if extinf != expected_extinf:
        raise ValidationError("Generated VTT playlist durations are invalid.")
    target_line = next(
        (line for line in lines if line.startswith("#EXT-X-TARGETDURATION:")), None
    )
    if target_line is None:
        raise ValidationError("Generated VTT playlist lacks a target duration.")
    parsed_target = int(target_line.split(":", 1)[1])
    minimum_target = max(
        source.target_duration_seconds,
        (max(expected_durations) + 999) // 1000,
    )
    if parsed_target < minimum_target or (target_duration is not None and parsed_target != target_duration):
        raise ValidationError("Generated VTT playlist target duration is too small.")
