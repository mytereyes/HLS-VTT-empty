"""Pure HLS parsing and minimally invasive master-manifest updates."""

from __future__ import annotations

import logging
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from .errors import ManifestConflictError, ParseError, ValidationError
from .models import LanguageSelection, SourcePlaylist, VTT_PLAYLIST_NAME

LOGGER = logging.getLogger(__name__)
LANGUAGE_TOKEN_RE = re.compile(r"^[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*$")
ATTRIBUTE_RE_TEMPLATE = r'(?:^|,)\s*{name}\s*=\s*(?:"([^"]*)"|([^,]*))'
SUBTITLE_DECLARATION_TEMPLATE = (
    '#EXT-X-MEDIA:TYPE=SUBTITLES,URI="{uri}",GROUP-ID="vtt",'
    'LANGUAGE="{language}",NAME="Subtitle0",DEFAULT=NO,AUTOSELECT=YES'
)


def decode_utf8(data: bytes, object_name: str) -> str:
    """Decode a nonempty UTF-8 S3 object without silently replacing bytes."""
    if not data:
        raise ParseError(f"{object_name} is empty.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ParseError(f"{object_name} is not valid UTF-8: {exc}") from exc


def _first_nonempty_line(text: str) -> str | None:
    return next((line.strip() for line in text.splitlines() if line.strip()), None)


def _attribute(line: str, name: str) -> str | None:
    if ":" not in line:
        return None
    attributes = line.split(":", 1)[1]
    match = re.search(ATTRIBUTE_RE_TEMPLATE.format(name=re.escape(name)), attributes)
    if not match:
        return None
    return match.group(1) if match.group(1) is not None else match.group(2).strip()


def _is_media_type(line: str, media_type: str) -> bool:
    return line.startswith("#EXT-X-MEDIA:") and _attribute(line, "TYPE") == media_type


def parse_source_playlist(text: str) -> SourcePlaylist:
    """Parse VOD timing, rounding each Decimal duration half-up to milliseconds."""
    if _first_nonempty_line(text) != "#EXTM3U":
        raise ParseError("Source playlist must start with #EXTM3U.")

    durations_ms: list[int] = []
    target_duration: int | None = None
    media_sequence = 0
    has_endlist = False
    awaiting_segment_uri = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXTINF:"):
            if has_endlist:
                raise ParseError("Source playlist contains #EXTINF after #EXT-X-ENDLIST.")
            if awaiting_segment_uri:
                raise ParseError("Source playlist has an #EXTINF without a following media URI.")
            token = line[len("#EXTINF:") :].split(",", 1)[0].strip()
            try:
                duration = Decimal(token)
            except InvalidOperation as exc:
                raise ParseError(f"Invalid #EXTINF duration {token!r}.") from exc
            if not duration.is_finite() or duration <= 0:
                raise ParseError(f"#EXTINF duration must be positive: {token!r}.")
            milliseconds = int(
                (duration * Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            )
            if milliseconds <= 0:
                raise ParseError(
                    f"#EXTINF duration {token!r} rounds to zero milliseconds."
                )
            durations_ms.append(milliseconds)
            awaiting_segment_uri = True
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            if target_duration is not None:
                raise ParseError("Source playlist contains duplicate #EXT-X-TARGETDURATION tags.")
            token = line.split(":", 1)[1].strip()
            try:
                target_duration = int(token)
            except ValueError as exc:
                raise ParseError(f"Invalid #EXT-X-TARGETDURATION {token!r}.") from exc
            if target_duration <= 0:
                raise ParseError("#EXT-X-TARGETDURATION must be positive.")
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            token = line.split(":", 1)[1].strip()
            try:
                media_sequence = int(token)
            except ValueError as exc:
                raise ParseError(f"Invalid #EXT-X-MEDIA-SEQUENCE {token!r}.") from exc
            if media_sequence < 0:
                raise ParseError("#EXT-X-MEDIA-SEQUENCE cannot be negative.")
        elif line == "#EXT-X-ENDLIST":
            if awaiting_segment_uri:
                raise ParseError("Source playlist has an #EXTINF without a following media URI.")
            if has_endlist:
                raise ParseError("Source playlist contains duplicate #EXT-X-ENDLIST tags.")
            has_endlist = True
        elif line and not line.startswith("#"):
            if has_endlist:
                raise ParseError("Source playlist contains a media URI after #EXT-X-ENDLIST.")
            if not awaiting_segment_uri:
                raise ParseError(f"Media URI {line!r} has no preceding #EXTINF.")
            awaiting_segment_uri = False

    if not durations_ms:
        raise ParseError("Source playlist contains no #EXTINF entries.")
    if not has_endlist:
        raise ParseError("Source VOD playlist is missing #EXT-X-ENDLIST.")
    if target_duration is None:
        raise ParseError("Source playlist is missing #EXT-X-TARGETDURATION.")
    return SourcePlaylist(tuple(durations_ms), target_duration, media_sequence)


def validate_language_token(language: str) -> str:
    if not LANGUAGE_TOKEN_RE.fullmatch(language):
        raise ValidationError(
            f"Invalid language token {language!r}; only letters, digits, and hyphens are allowed."
        )
    return language


def detect_audio_language(text: str) -> LanguageSelection:
    """Select the first AUDIO line's language and report distinct later values."""
    audio_lines = [line for line in text.splitlines() if _is_media_type(line, "AUDIO")]
    if not audio_lines:
        raise ParseError("h264_manifest.m3u8 contains no AUDIO #EXT-X-MEDIA declaration.")

    first = _attribute(audio_lines[0], "LANGUAGE")
    if first is None or first == "":
        raise ParseError(
            "The first AUDIO #EXT-X-MEDIA declaration has no valid LANGUAGE attribute."
        )
    validate_language_token(first)

    languages: list[str] = []
    for line in audio_lines:
        value = _attribute(line, "LANGUAGE")
        if value is not None and value not in languages:
            validate_language_token(value)
            languages.append(value)
    selection = LanguageSelection(first, tuple(languages))
    if selection.has_multiple_languages:
        LOGGER.warning(
            "Multiple audio languages detected (%s); selected first language %s.",
            ", ".join(selection.distinct_languages),
            selection.language,
        )
    return selection


def validate_secondary_language(text: str, expected_language: str) -> None:
    audio_lines = [line for line in text.splitlines() if _is_media_type(line, "AUDIO")]
    if not audio_lines:
        return
    language = _attribute(audio_lines[0], "LANGUAGE")
    if language is None or language == "":
        raise ParseError(
            "The first AUDIO declaration in Manifest.m3u8 has no valid LANGUAGE attribute."
        )
    validate_language_token(language)
    if language != expected_language:
        raise ValidationError(
            "Language mismatch: h264_manifest.m3u8 selected "
            f"{expected_language!r}, but Manifest.m3u8 uses {language!r}."
        )


def validate_reconcilable_subtitle_config(
    text: str, language: str, object_name: str
) -> bool:
    """Validate that existing subtitle tags identify only the managed vtt track."""
    validate_language_token(language)
    declarations = [
        line for line in text.splitlines() if _is_media_type(line, "SUBTITLES")
    ]
    if len(declarations) > 1:
        raise ManifestConflictError(
            f"{object_name} contains multiple SUBTITLES media declarations."
        )
    if declarations:
        declaration = declarations[0]
        uri = _attribute(declaration, "URI")
        group_id = _attribute(declaration, "GROUP-ID")
        if uri != VTT_PLAYLIST_NAME or group_id != "vtt":
            raise ManifestConflictError(
                f"{object_name} contains a SUBTITLES media declaration for URI "
                f"{uri!r} and GROUP-ID {group_id!r}; expected URI "
                f"{VTT_PLAYLIST_NAME!r} and GROUP-ID 'vtt'."
            )

    for line in text.splitlines():
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        subtitle_attribute_count = len(
            re.findall(r'(?:^|,)\s*SUBTITLES\s*=', line.split(":", 1)[1])
        )
        if subtitle_attribute_count > 1:
            raise ManifestConflictError(
                f"{object_name} contains multiple SUBTITLES attributes on one stream."
            )
        subtitle_group = _attribute(line, "SUBTITLES")
        exact_vtt_attribute = re.search(
            r'(?:^|,)\s*SUBTITLES\s*=\s*"vtt"\s*(?=,|$)',
            line.split(":", 1)[1],
        )
        if subtitle_group is not None and exact_vtt_attribute is None:
            raise ManifestConflictError(
                f"{object_name} stream SUBTITLES configuration does not exactly "
                'match SUBTITLES="vtt".'
            )
    return bool(declarations)


def _newline_style(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _audio_lines(text: str) -> tuple[str, ...]:
    return tuple(line for line in text.splitlines() if _is_media_type(line, "AUDIO"))


def _variant_uris(lines: list[str]) -> tuple[str, ...]:
    uris: list[str] = []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            following_index = index + 1
            while following_index < len(lines) and not lines[following_index]:
                following_index += 1
            if following_index >= len(lines) or lines[following_index].startswith("#"):
                raise ParseError("#EXT-X-STREAM-INF has no following variant URI.")
            uris.append(lines[following_index])
    return tuple(uris)


def modify_master_manifest(text: str, language: str, object_name: str) -> str:
    """Reconcile the exact vtt declaration and annotate missing stream references."""
    validate_language_token(language)
    if _first_nonempty_line(text) != "#EXTM3U":
        raise ParseError(f"{object_name} must start with #EXTM3U.")
    validate_reconcilable_subtitle_config(text, language, object_name)

    newline = _newline_style(text)
    original_lines = text.splitlines()
    original_audio = _audio_lines(text)
    original_uris = _variant_uris(original_lines)
    original_stream_count = sum(
        line.startswith("#EXT-X-STREAM-INF:") for line in original_lines
    )

    updated_lines = []
    for line in original_lines:
        if _is_media_type(line, "SUBTITLES"):
            continue
        if line.startswith("#EXT-X-STREAM-INF:") and _attribute(line, "SUBTITLES") is None:
            updated_lines.append(f'{line},SUBTITLES="vtt"')
        else:
            updated_lines.append(line)

    subtitle_line = SUBTITLE_DECLARATION_TEMPLATE.format(
        uri=VTT_PLAYLIST_NAME, language=language
    )
    audio_indexes = [
        index
        for index, line in enumerate(updated_lines)
        if _is_media_type(line, "AUDIO")
    ]
    if audio_indexes:
        insertion_index = audio_indexes[-1] + 1
    else:
        header_index = next(
            (
                index
                for index, line in enumerate(updated_lines)
                if line.strip() == "#EXTM3U"
            ),
            0,
        )
        insertion_index = header_index + 1
    updated_lines.insert(insertion_index, subtitle_line)
    updated = newline.join(updated_lines) + newline

    if _audio_lines(updated) != original_audio:
        raise ValidationError(f"{object_name} audio declarations changed unexpectedly.")
    if _variant_uris(updated.splitlines()) != original_uris:
        raise ValidationError(f"{object_name} variant URI lines changed unexpectedly.")
    if sum(line.startswith("#EXT-X-STREAM-INF:") for line in updated.splitlines()) != original_stream_count:
        raise ValidationError(f"{object_name} stream variant count changed unexpectedly.")
    validate_modified_manifest(updated, language, object_name, original_stream_count)
    return updated


def validate_modified_manifest(
    text: str, language: str, object_name: str, expected_stream_count: int
) -> None:
    expected_line = SUBTITLE_DECLARATION_TEMPLATE.format(
        uri=VTT_PLAYLIST_NAME, language=language
    )
    if text.splitlines().count(expected_line) != 1:
        raise ValidationError(
            f"{object_name} must contain exactly one generated subtitle declaration."
        )
    stream_lines = [
        line for line in text.splitlines() if line.startswith("#EXT-X-STREAM-INF:")
    ]
    if len(stream_lines) != expected_stream_count:
        raise ValidationError(f"{object_name} stream count validation failed.")
    for line in stream_lines:
        if line.count('SUBTITLES="vtt"') != 1:
            raise ValidationError(
                f"Every stream in {object_name} must contain exactly one SUBTITLES=\"vtt\"."
            )
