"""Credential selection for local and AWS-hosted execution."""

from __future__ import annotations

from getpass import getpass
from pathlib import Path
import re
from typing import Any, Callable


class AuthenticationError(Exception):
    """Authentication selection failed or interactive input was cancelled."""


def _read_input(prompt: str, reader: Callable[[str], str]) -> str:
    try:
        return reader(prompt).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise AuthenticationError("Authentication input was cancelled.") from exc


def _read_secret(prompt: str) -> str:
    try:
        return getpass(prompt).strip()
    except (EOFError, KeyboardInterrupt) as exc:
        raise AuthenticationError("Authentication input was cancelled.") from exc


def _client_kwargs(region: str | None, config: Any) -> dict[str, Any]:
    return {"region_name": region, "config": config}


def _has_default_credentials_profile(credentials_path: Path) -> bool:
    """Scan section headers without parsing credential values into application state."""
    try:
        with credentials_path.expanduser().open(encoding="utf-8") as stream:
            for line in stream:
                match = re.fullmatch(r"\s*\[([^]]+)]\s*(?:[#;].*)?", line.rstrip("\r\n"))
                if match and match.group(1).strip() == "default":
                    return True
    except (OSError, UnicodeError):
        return False
    return False


def _profile_client(
    boto3_module: Any,
    profile_name: str,
    *,
    region: str | None,
    config: Any,
) -> Any:
    try:
        session = boto3_module.Session(profile_name=profile_name)
        return session.client("s3", **_client_kwargs(region, config))
    except Exception:
        raise AuthenticationError(
            f"Unable to initialize AWS profile {profile_name!r}."
        ) from None


def create_s3_client(
    boto3_module: Any,
    *,
    region: str | None,
    config: Any,
    profile: str | None = None,
    prompt_auth: bool = False,
    input_reader: Callable[[str], str] | None = None,
    credentials_path: Path | None = None,
) -> Any:
    """Create an S3 client without retaining or exposing credential values."""
    reader = input if input_reader is None else input_reader
    if profile is not None:
        profile_name = profile.strip()
        if not profile_name:
            raise AuthenticationError("AWS profile name cannot be empty.")
        return _profile_client(
            boto3_module, profile_name, region=region, config=config
        )

    if not prompt_auth:
        shared_credentials = credentials_path or Path("~/.aws/credentials")
        if _has_default_credentials_profile(shared_credentials):
            return _profile_client(
                boto3_module, "default", region=region, config=config
            )
        prompt_auth = True

    print("Select AWS authentication method:")
    print("  1. Use the standard boto3 credential chain (default)")
    print("  2. Use a named AWS profile")
    print("  3. Enter temporary AWS credentials for this run")
    choice = _read_input("Choice [1]: ", reader) or "1"

    if choice == "1":
        return boto3_module.client("s3", **_client_kwargs(region, config))
    if choice == "2":
        profile_name = _read_input("AWS profile name: ", reader)
        if not profile_name:
            raise AuthenticationError("AWS profile name cannot be empty.")
        return _profile_client(
            boto3_module, profile_name, region=region, config=config
        )
    if choice == "3":
        access_key = _read_secret("AWS access key ID: ")
        if not access_key:
            raise AuthenticationError("AWS access key ID cannot be empty.")
        secret_key = _read_secret("AWS secret access key: ")
        if not secret_key:
            raise AuthenticationError("AWS secret access key cannot be empty.")
        session_token = _read_secret("AWS session token (optional): ")
        kwargs = _client_kwargs(region, config)
        kwargs.update(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        if session_token:
            kwargs["aws_session_token"] = session_token
        try:
            return boto3_module.client("s3", **kwargs)
        except Exception as exc:
            raise AuthenticationError(
                "Unable to initialize AWS temporary credentials."
            ) from None

    raise AuthenticationError(
        f"Invalid authentication choice {choice!r}; select 1, 2, or 3."
    )
