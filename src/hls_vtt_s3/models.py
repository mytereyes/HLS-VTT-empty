"""Typed domain models for parsing, generation, and reporting."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


VIDEO_PLAYLIST_NAME = "h264_manifest-1080-hls-h264-24p-video.m3u8"
H264_MANIFEST_NAME = "h264_manifest.m3u8"
SECONDARY_MANIFEST_NAME = "manifest.m3u8"
VTT_PLAYLIST_NAME = "h264_manifest-vtt-hls-h264-subtitle.m3u8"
VTT_CONTENT_TYPE = "text/vtt; charset=utf-8"
HLS_CONTENT_TYPE = "application/vnd.apple.mpegurl"


class AdStatus(str, Enum):
    PROCESSED = "PROCESSED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"


@dataclass(frozen=True)
class SourcePlaylist:
    durations_ms: tuple[int, ...]
    target_duration_seconds: int
    media_sequence: int

    @property
    def total_duration_ms(self) -> int:
        return sum(self.durations_ms)


@dataclass(frozen=True)
class LanguageSelection:
    language: str
    distinct_languages: tuple[str, ...]

    @property
    def has_multiple_languages(self) -> bool:
        return len(self.distinct_languages) > 1


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class GeneratedObject:
    relative_key: str
    body: bytes
    content_type: str
    extra_args: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class ObjectSnapshot:
    key: str
    etag: str
    version_id: str | None
    content_length: int
    content_type: str | None
    preserved_args: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class PreparedAd:
    language: str
    source_playlist: SourcePlaylist
    objects: tuple[GeneratedObject, ...]
    segment_keys: tuple[str, ...]
    source_snapshots: tuple[ObjectSnapshot, ...]

    @property
    def generated_vtt_count(self) -> int:
        return len(self.segment_keys)


@dataclass
class AdResult:
    ad_prefix: str
    status: AdStatus
    dry_run: bool
    message: str = ""
    language: str | None = None
    source_segment_count: int = 0
    generated_vtt_count: int = 0
    total_original_duration_ms: int = 0
    total_vtt_duration_ms: int = 0
    staging_prefix: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    promoted_objects: list[str] = field(default_factory=list)
    planned_objects: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["status"] = self.status.value
        return result


@dataclass(frozen=True)
class BatchReport:
    bucket: str
    base_prefix: str
    dry_run: bool
    elapsed_seconds: float
    results: tuple[AdResult, ...]

    @property
    def total_ads_discovered(self) -> int:
        return len(self.results)

    @property
    def processed(self) -> int:
        return sum(item.status is AdStatus.PROCESSED for item in self.results)

    @property
    def skipped(self) -> int:
        return sum(item.status is AdStatus.SKIPPED for item in self.results)

    @property
    def failed(self) -> int:
        return sum(item.status is AdStatus.FAILED for item in self.results)

    @property
    def total_vtt_segments_generated(self) -> int:
        return sum(item.generated_vtt_count for item in self.results)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "base_prefix": self.base_prefix,
            "mode": "DRY-RUN" if self.dry_run else "APPLY",
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "summary": {
                "total_ads_discovered": self.total_ads_discovered,
                "processed": self.processed,
                "skipped": self.skipped,
                "failed": self.failed,
                "total_vtt_segments_generated": self.total_vtt_segments_generated,
            },
            "results": [item.to_dict() for item in self.results],
        }
