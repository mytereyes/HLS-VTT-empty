"""Application-specific exception hierarchy."""

from __future__ import annotations


class HlsVttError(Exception):
    """Base class for expected per-ad failures."""


class ParseError(HlsVttError):
    """An input could not be decoded or parsed."""


class ValidationError(HlsVttError):
    """Parsed or generated content failed validation."""


class ManifestConflictError(ValidationError):
    """Existing subtitle configuration makes an update unsafe."""


class S3RepositoryError(HlsVttError):
    """An S3 operation failed or returned an unsafe result."""


class ObjectMissingError(S3RepositoryError):
    """A required S3 object does not exist."""


class PermissionDeniedError(S3RepositoryError):
    """S3 denied an operation; this is not treated as a missing key."""


class PromotionError(S3RepositoryError):
    """Final-object promotion failed after zero or more successful copies."""

    def __init__(
        self,
        message: str,
        *,
        promoted_objects: tuple[str, ...] = (),
        staging_prefix: str | None = None,
    ) -> None:
        super().__init__(message)
        self.promoted_objects = promoted_objects
        self.staging_prefix = staging_prefix
