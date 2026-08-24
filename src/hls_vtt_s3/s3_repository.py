"""Boto3-backed S3 access with explicit error classification and verification."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from .errors import ObjectMissingError, PermissionDeniedError, S3RepositoryError
from .models import GeneratedObject, ObjectSnapshot

LOGGER = logging.getLogger(__name__)
MISSING_CODES = {"404", "NoSuchKey", "NotFound"}
DENIED_CODES = {"403", "AccessDenied", "Forbidden"}
CONFLICT_CODES = {"409", "412", "ConditionalRequestConflict", "PreconditionFailed"}
MAX_MANIFEST_BYTES = 10 * 1024 * 1024
PRESERVED_HEAD_ARGS = {
    "CacheControl": "CacheControl",
    "ContentDisposition": "ContentDisposition",
    "ContentEncoding": "ContentEncoding",
    "ContentLanguage": "ContentLanguage",
    "Expires": "Expires",
    "Metadata": "Metadata",
    "ServerSideEncryption": "ServerSideEncryption",
    "SSEKMSKeyId": "SSEKMSKeyId",
    "BucketKeyEnabled": "BucketKeyEnabled",
    "StorageClass": "StorageClass",
    "WebsiteRedirectLocation": "WebsiteRedirectLocation",
}


def normalize_prefix(prefix: str) -> str:
    """Return an S3 key prefix with no leading slash and exactly one trailing slash."""
    normalized = prefix.strip().strip("/")
    return f"{normalized}/" if normalized else ""


def validate_ad_id(ad_id: str) -> str:
    normalized = ad_id.strip().strip("/")
    if (
        not normalized
        or "/" in normalized
        or "\\" in normalized
        or normalized in {".", ".."}
        or ".." in normalized
        or normalized.startswith("_vtt_staging")
    ):
        raise ValueError(
            "Ad ID must name one immediate child prefix and cannot contain traversal or staging syntax."
        )
    return normalized


def join_key(prefix: str, relative_key: str) -> str:
    return f"{normalize_prefix(prefix)}{relative_key.lstrip('/')}"


class S3Repository:
    """Small injectable wrapper around the boto3 S3 client."""

    def __init__(self, client: Any, bucket: str) -> None:
        self.client = client
        self.bucket = bucket

    @staticmethod
    def _classify_client_error(exc: ClientError, operation: str, key: str | None = None) -> Exception:
        error = exc.response.get("Error", {})
        code = str(error.get("Code", "Unknown"))
        message = str(error.get("Message", "no service message"))
        subject = f" for key {key!r}" if key else ""
        detail = f"S3 {operation}{subject} failed ({code}): {message}"
        if code in MISSING_CODES:
            return ObjectMissingError(detail)
        if code in DENIED_CODES:
            return PermissionDeniedError(detail)
        if code in CONFLICT_CODES:
            return S3RepositoryError(f"Concurrency conflict: {detail}")
        return S3RepositoryError(detail)

    def discover_ad_prefixes(self, base_prefix: str) -> list[str]:
        base = normalize_prefix(base_prefix)
        prefixes: list[str] = []
        continuation_token: str | None = None
        while True:
            request: dict[str, Any] = {
                "Bucket": self.bucket,
                "Prefix": base,
                "Delimiter": "/",
            }
            if continuation_token:
                request["ContinuationToken"] = continuation_token
            try:
                response = self.client.list_objects_v2(**request)
            except ClientError as exc:
                raise self._classify_client_error(exc, "ListObjectsV2") from exc
            except BotoCoreError as exc:
                raise S3RepositoryError(f"S3 ListObjectsV2 failed: {exc}") from exc

            for item in response.get("CommonPrefixes", []):
                prefix = item.get("Prefix")
                if not isinstance(prefix, str) or not prefix.startswith(base):
                    continue
                relative = prefix[len(base) :].strip("/")
                if not relative or "/" in relative or relative.startswith("_vtt_staging"):
                    continue
                prefixes.append(normalize_prefix(prefix))

            if not response.get("IsTruncated"):
                break
            continuation_token = response.get("NextContinuationToken")
            if not continuation_token:
                raise S3RepositoryError(
                    "ListObjectsV2 returned IsTruncated without NextContinuationToken."
                )
        return sorted(set(prefixes))

    def ad_prefix_for_id(self, base_prefix: str, ad_id: str) -> str:
        return join_key(base_prefix, f"{validate_ad_id(ad_id)}/")

    def object_exists(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            classified = self._classify_client_error(exc, "HeadObject", key)
            if isinstance(classified, ObjectMissingError):
                return False
            raise classified from exc
        except BotoCoreError as exc:
            raise S3RepositoryError(f"S3 HeadObject for {key!r} failed: {exc}") from exc

    def require_object(self, key: str) -> None:
        if not self.object_exists(key):
            raise ObjectMissingError(f"Required object is missing: s3://{self.bucket}/{key}")

    def snapshot_object(self, key: str, *, preserve_metadata: bool = False) -> ObjectSnapshot:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            raise self._classify_client_error(exc, "HeadObject snapshot", key) from exc
        except BotoCoreError as exc:
            raise S3RepositoryError(f"S3 HeadObject snapshot for {key!r} failed: {exc}") from exc
        etag = response.get("ETag")
        size = response.get("ContentLength")
        if not isinstance(etag, str) or not etag or not isinstance(size, int) or size <= 0:
            raise S3RepositoryError(f"Object {key!r} has invalid ETag or content length.")
        preserved: dict[str, Any] = {}
        if preserve_metadata:
            for response_name, request_name in PRESERVED_HEAD_ARGS.items():
                if response_name in response and response[response_name] is not None:
                    preserved[request_name] = response[response_name]
        return ObjectSnapshot(
            key=key,
            etag=etag,
            version_id=response.get("VersionId"),
            content_length=size,
            content_type=response.get("ContentType"),
            preserved_args=tuple(preserved.items()),
        )

    def assert_unchanged(self, snapshot: ObjectSnapshot) -> None:
        current = self.snapshot_object(snapshot.key)
        if current.etag != snapshot.etag or (
            snapshot.version_id is not None and current.version_id != snapshot.version_id
        ):
            raise S3RepositoryError(
                f"Concurrency conflict: source object changed since download: {snapshot.key!r}."
            )

    def get_object_bytes(
        self,
        key: str,
        *,
        version_id: str | None = None,
        expected_etag: str | None = None,
    ) -> bytes:
        request: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if version_id is not None:
            request["VersionId"] = version_id
        elif expected_etag is not None:
            request["IfMatch"] = expected_etag
        try:
            response = self.client.get_object(**request)
            body = response["Body"]
            data = body.read(MAX_MANIFEST_BYTES + 1)
            close = getattr(body, "close", None)
            if close:
                close()
        except ClientError as exc:
            raise self._classify_client_error(exc, "GetObject", key) from exc
        except (BotoCoreError, OSError, KeyError, TypeError) as exc:
            raise S3RepositoryError(f"S3 GetObject for {key!r} failed: {exc}") from exc
        if not isinstance(data, bytes):
            raise S3RepositoryError(f"S3 GetObject for {key!r} did not return bytes.")
        if len(data) > MAX_MANIFEST_BYTES:
            raise S3RepositoryError(
                f"Object {key!r} exceeds the {MAX_MANIFEST_BYTES}-byte manifest safety limit."
            )
        return data

    def put_object(self, key: str, generated: GeneratedObject) -> None:
        if not generated.body:
            raise S3RepositoryError(f"Refusing to upload empty object {key!r}.")
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=generated.body,
                ContentType=generated.content_type,
                **dict(generated.extra_args),
            )
        except ClientError as exc:
            raise self._classify_client_error(exc, "PutObject", key) from exc
        except BotoCoreError as exc:
            raise S3RepositoryError(f"S3 PutObject for {key!r} failed: {exc}") from exc

    def verify_object(self, key: str, expected: GeneratedObject) -> None:
        try:
            metadata = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            raise self._classify_client_error(exc, "HeadObject verification", key) from exc
        except BotoCoreError as exc:
            raise S3RepositoryError(f"S3 verification for {key!r} failed: {exc}") from exc
        size = metadata.get("ContentLength")
        if not isinstance(size, int) or size <= 0:
            raise S3RepositoryError(f"Verified object {key!r} is empty or has no valid size.")
        if size != len(expected.body):
            raise S3RepositoryError(
                f"Size mismatch for {key!r}: expected {len(expected.body)}, got {size}."
            )
        content_type = metadata.get("ContentType")
        if content_type != expected.content_type:
            raise S3RepositoryError(
                f"Content-Type mismatch for {key!r}: expected {expected.content_type!r}, "
                f"got {content_type!r}."
            )
        for property_name, expected_value in expected.extra_args:
            if metadata.get(property_name) != expected_value:
                raise S3RepositoryError(
                    f"Property mismatch for {key!r}: {property_name} expected "
                    f"{expected_value!r}, got {metadata.get(property_name)!r}."
                )
        downloaded = self.get_object_bytes(key)
        if hashlib.sha256(downloaded).digest() != hashlib.sha256(expected.body).digest():
            raise S3RepositoryError(f"Content hash mismatch for {key!r}.")

    def copy_object(
        self,
        source_key: str,
        destination_key: str,
        expected: GeneratedObject,
        *,
        destination_etag: str | None = None,
    ) -> None:
        request: dict[str, Any] = {
            "Bucket": self.bucket,
            "Key": destination_key,
            "CopySource": {"Bucket": self.bucket, "Key": source_key},
            "MetadataDirective": "REPLACE",
            "ContentType": expected.content_type,
            **dict(expected.extra_args),
        }
        staged = self.snapshot_object(source_key)
        request["CopySourceIfMatch"] = staged.etag
        if destination_etag is None:
            request["IfNoneMatch"] = "*"
        else:
            request["IfMatch"] = destination_etag
        try:
            self.client.copy_object(**request)
        except ClientError as exc:
            raise self._classify_client_error(exc, "CopyObject", destination_key) from exc
        except BotoCoreError as exc:
            raise S3RepositoryError(
                f"S3 CopyObject to {destination_key!r} failed: {exc}"
            ) from exc

    def delete_objects(self, keys: Iterable[str]) -> None:
        key_list = list(keys)
        for offset in range(0, len(key_list), 1000):
            chunk = key_list[offset : offset + 1000]
            try:
                response = self.client.delete_objects(
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": key} for key in chunk], "Quiet": True},
                )
            except ClientError as exc:
                raise self._classify_client_error(exc, "DeleteObjects") from exc
            except BotoCoreError as exc:
                raise S3RepositoryError(f"S3 DeleteObjects failed: {exc}") from exc
            errors = response.get("Errors", [])
            if errors:
                raise S3RepositoryError(f"S3 DeleteObjects returned errors: {errors!r}")
