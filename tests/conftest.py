from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError

from hls_vtt_s3.models import (
    H264_MANIFEST_NAME,
    SECONDARY_MANIFEST_NAME,
    VIDEO_PLAYLIST_NAME,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "ads/VODv3/H264/HLS/"
BUCKET = "test-bucket"


@dataclass
class StoredObject:
    body: bytes
    content_type: str = "application/octet-stream"
    properties: dict[str, Any] | None = None


class FakeS3Client:
    """Purpose-built boto3-compatible S3 fake; it never opens a network connection."""

    def __init__(self) -> None:
        self.objects: dict[str, StoredObject] = {}
        self.calls: list[dict[str, Any]] = []
        self.list_pages: list[dict[str, Any]] | None = None
        self.head_counts: dict[str, int] = {}
        self.appear_on_head_call: dict[str, int] = {}
        self.fail_copy_destination: str | None = None
        self.fail_put_key: str | None = None
        self.fail_delete = False
        self.corrupt_staged_get_suffix: str | None = None
        self.mutate_on_head_call: dict[str, tuple[int, bytes]] = {}

    @staticmethod
    def _error(code: str, operation: str, message: str = "simulated") -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": message}}, operation)

    def seed(
        self,
        key: str,
        body: bytes | str,
        content_type: str = "application/octet-stream",
        **properties: Any,
    ) -> None:
        encoded = body.encode("utf-8") if isinstance(body, str) else body
        self.objects[key] = StoredObject(encoded, content_type, properties)

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"operation": "list_objects_v2", **kwargs})
        if self.list_pages is not None:
            token = kwargs.get("ContinuationToken")
            index = int(str(token).split("-")[-1]) if token else 0
            return self.list_pages[index]

        prefix = kwargs.get("Prefix", "")
        delimiter = kwargs.get("Delimiter", "/")
        children: set[str] = set()
        for key in self.objects:
            if not key.startswith(prefix):
                continue
            remainder = key[len(prefix) :]
            if delimiter in remainder:
                children.add(prefix + remainder.split(delimiter, 1)[0] + delimiter)
        return {
            "IsTruncated": False,
            "CommonPrefixes": [{"Prefix": value} for value in sorted(children)],
        }

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        self.calls.append({"operation": "head_object", **kwargs})
        count = self.head_counts.get(key, 0) + 1
        self.head_counts[key] = count
        if self.appear_on_head_call.get(key) == count:
            self.seed(key, b"#EXTM3U\n#EXT-X-ENDLIST\n", "application/vnd.apple.mpegurl")
        mutation = self.mutate_on_head_call.get(key)
        if mutation and mutation[0] == count:
            self.seed(key, mutation[1], self.objects[key].content_type)
        if key not in self.objects:
            raise self._error("404", "HeadObject", "Not Found")
        stored = self.objects[key]
        return {
            "ContentLength": len(stored.body),
            "ContentType": stored.content_type,
            "ETag": f'"{hashlib.md5(stored.body, usedforsecurity=False).hexdigest()}"',
            **(stored.properties or {}),
        }

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        self.calls.append({"operation": "get_object", **kwargs})
        if key not in self.objects:
            raise self._error("NoSuchKey", "GetObject", "Not Found")
        if "IfMatch" in kwargs:
            current_etag = (
                f'"{hashlib.md5(self.objects[key].body, usedforsecurity=False).hexdigest()}"'
            )
            if kwargs["IfMatch"] != current_etag:
                raise self._error("PreconditionFailed", "GetObject", "source changed")
        if (
            self.corrupt_staged_get_suffix
            and "_vtt_staging/" in key
            and key.endswith(self.corrupt_staged_get_suffix)
        ):
            return {"Body": BytesIO(b"corrupted")}
        return {"Body": BytesIO(self.objects[key].body)}

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        key = kwargs["Key"]
        self.calls.append({"operation": "put_object", **kwargs})
        if key == self.fail_put_key:
            raise self._error("SlowDown", "PutObject", "injected put failure")
        body = kwargs["Body"]
        properties = {
            name: kwargs[name]
            for name in (
                "CacheControl",
                "ContentDisposition",
                "ContentEncoding",
                "ContentLanguage",
                "Expires",
                "Metadata",
                "WebsiteRedirectLocation",
            )
            if name in kwargs
        }
        self.seed(
            key,
            bytes(body),
            kwargs.get("ContentType", "application/octet-stream"),
            **properties,
        )
        return {"ETag": '"fake"'}

    def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        destination = kwargs["Key"]
        self.calls.append({"operation": "copy_object", **kwargs})
        if destination == self.fail_copy_destination:
            raise self._error("InternalError", "CopyObject", "injected copy failure")
        source = kwargs["CopySource"]["Key"]
        if source not in self.objects:
            raise self._error("NoSuchKey", "CopyObject", "source missing")
        if kwargs.get("IfNoneMatch") == "*" and destination in self.objects:
            raise self._error("PreconditionFailed", "CopyObject", "destination exists")
        if "IfMatch" in kwargs:
            if destination not in self.objects:
                raise self._error("PreconditionFailed", "CopyObject", "destination missing")
            destination_etag = (
                f'"{hashlib.md5(self.objects[destination].body, usedforsecurity=False).hexdigest()}"'
            )
            if kwargs["IfMatch"] != destination_etag:
                raise self._error("PreconditionFailed", "CopyObject", "destination changed")
        properties = {
            name: kwargs[name]
            for name in (
                "CacheControl",
                "ContentDisposition",
                "ContentEncoding",
                "ContentLanguage",
                "Expires",
                "Metadata",
                "WebsiteRedirectLocation",
            )
            if name in kwargs
        }
        self.seed(destination, self.objects[source].body, kwargs["ContentType"], **properties)
        return {"CopyObjectResult": {"ETag": '"fake"'}}

    def delete_objects(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"operation": "delete_objects", **kwargs})
        if self.fail_delete:
            raise self._error("InternalError", "DeleteObjects", "injected delete failure")
        for item in kwargs["Delete"]["Objects"]:
            self.objects.pop(item["Key"], None)
        return {"Deleted": kwargs["Delete"]["Objects"]}

    def operations(self, *names: str) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["operation"] in names]


@pytest.fixture
def video_text() -> str:
    return (FIXTURES / "video_playlist_three_segments.m3u8").read_text(encoding="utf-8")


@pytest.fixture
def h264_fre_text() -> str:
    return (FIXTURES / "h264_manifest_fre.m3u8").read_text(encoding="utf-8")


@pytest.fixture
def secondary_fre_text() -> str:
    return (FIXTURES / "manifest_fre.m3u8").read_text(encoding="utf-8")


@pytest.fixture
def fake_s3() -> FakeS3Client:
    return FakeS3Client()


def seed_ad(
    fake: FakeS3Client,
    ad_id: str,
    video: str,
    h264: str,
    secondary: str,
) -> str:
    prefix = f"{BASE}{ad_id}/"
    fake.seed(prefix + VIDEO_PLAYLIST_NAME, video)
    fake.seed(prefix + H264_MANIFEST_NAME, h264)
    fake.seed(prefix + SECONDARY_MANIFEST_NAME, secondary)
    return prefix
