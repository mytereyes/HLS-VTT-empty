from __future__ import annotations

import logging

import pytest

from hls_vtt_s3.auth import AuthenticationError, create_s3_client


class FakeSession:
    def __init__(self, calls: list[tuple[str, object]], **kwargs: object) -> None:
        calls.append(("session", kwargs))
        self.calls = calls

    def client(self, service: str, **kwargs: object) -> object:
        self.calls.append(("session_client", (service, kwargs)))
        return object()


class FakeBoto3:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def client(self, service: str, **kwargs: object) -> object:
        self.calls.append(("client", (service, kwargs)))
        return object()

    def Session(self, **kwargs: object) -> FakeSession:  # noqa: N802 - boto3 API
        return FakeSession(self.calls, **kwargs)


def test_default_auth_uses_standard_boto3_credential_chain() -> None:
    boto3_module = FakeBoto3()
    create_s3_client(boto3_module, region="us-east-1", config=object())
    assert boto3_module.calls[0][0] == "client"
    assert not any(name == "session" for name, _value in boto3_module.calls)


def test_noninteractive_profile_creates_named_session() -> None:
    boto3_module = FakeBoto3()
    create_s3_client(
        boto3_module, region=None, config=object(), profile="production-readonly"
    )
    assert boto3_module.calls[0] == (
        "session",
        {"profile_name": "production-readonly"},
    )
    assert boto3_module.calls[1][0] == "session_client"


def test_named_profile_initialization_failure_is_an_authentication_error() -> None:
    class FailingBoto3:
        def Session(self, **_kwargs: object) -> object:  # noqa: N802 - boto3 API
            raise RuntimeError("profile lookup failed")

    with pytest.raises(AuthenticationError, match="Unable to initialize AWS profile"):
        create_s3_client(
            FailingBoto3(), region=None, config=object(), profile="missing"
        )


def test_prompt_defaults_to_standard_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    boto3_module = FakeBoto3()
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    create_s3_client(
        boto3_module, region=None, config=object(), prompt_auth=True
    )
    assert boto3_module.calls[0][0] == "client"


def test_prompt_named_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(("2", "production"))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    boto3_module = FakeBoto3()
    create_s3_client(
        boto3_module, region=None, config=object(), prompt_auth=True
    )
    assert boto3_module.calls[0] == ("session", {"profile_name": "production"})


def test_temporary_credentials_are_only_passed_to_client_and_not_represented(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    secrets = iter(("AKIA_TEST_SECRET", "very-secret-value", "session-secret"))
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")
    monkeypatch.setattr("hls_vtt_s3.auth.getpass", lambda _prompt: next(secrets))
    boto3_module = FakeBoto3()
    with caplog.at_level(logging.DEBUG):
        client = create_s3_client(
            boto3_module, region="us-west-2", config=object(), prompt_auth=True
        )
    call = boto3_module.calls[0]
    assert call[0] == "client"
    kwargs = call[1][1]  # type: ignore[index]
    assert kwargs["aws_access_key_id"] == "AKIA_TEST_SECRET"
    assert kwargs["aws_secret_access_key"] == "very-secret-value"
    assert kwargs["aws_session_token"] == "session-secret"
    exposed = capsys.readouterr().out + capsys.readouterr().err + caplog.text + repr(client)
    assert "AKIA_TEST_SECRET" not in exposed
    assert "very-secret-value" not in exposed
    assert "session-secret" not in exposed


def test_optional_session_token_is_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    secrets = iter(("temporary-access", "temporary-secret", ""))
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")
    monkeypatch.setattr("hls_vtt_s3.auth.getpass", lambda _prompt: next(secrets))
    boto3_module = FakeBoto3()
    create_s3_client(
        boto3_module, region=None, config=object(), prompt_auth=True
    )
    kwargs = boto3_module.calls[0][1][1]  # type: ignore[index]
    assert "aws_session_token" not in kwargs


def test_temporary_credential_client_exception_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access_key = "submitted-access-secret"
    secret_key = "submitted-secret-key"
    token = "submitted-session-token"
    secrets = iter((access_key, secret_key, token))
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")
    monkeypatch.setattr("hls_vtt_s3.auth.getpass", lambda _prompt: next(secrets))

    class LeakyBoto3:
        def client(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError(f"provider echoed {access_key} {secret_key} {token}")

    with pytest.raises(AuthenticationError) as exc_info:
        create_s3_client(
            LeakyBoto3(), region=None, config=object(), prompt_auth=True
        )
    rendered = str(exc_info.value) + repr(exc_info.value)
    assert access_key not in rendered
    assert secret_key not in rendered
    assert token not in rendered


@pytest.mark.parametrize("choice", ["0", "4", "unknown"])
def test_invalid_menu_choice_fails_safely(
    monkeypatch: pytest.MonkeyPatch, choice: str
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: choice)
    with pytest.raises(AuthenticationError, match="Invalid authentication choice"):
        create_s3_client(FakeBoto3(), region=None, config=object(), prompt_auth=True)


@pytest.mark.parametrize("values", [("",), ("access", "")])
def test_empty_required_temporary_values_fail(
    monkeypatch: pytest.MonkeyPatch, values: tuple[str, ...]
) -> None:
    answers = iter(values)
    monkeypatch.setattr("builtins.input", lambda _prompt: "3")
    monkeypatch.setattr("hls_vtt_s3.auth.getpass", lambda _prompt: next(answers))
    with pytest.raises(AuthenticationError, match="cannot be empty"):
        create_s3_client(FakeBoto3(), region=None, config=object(), prompt_auth=True)


@pytest.mark.parametrize("failure", [EOFError(), KeyboardInterrupt()])
def test_eof_and_ctrl_c_are_clean_authentication_cancellations(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    def fail(_prompt: str) -> str:
        raise failure

    monkeypatch.setattr("builtins.input", fail)
    with pytest.raises(AuthenticationError, match="cancelled"):
        create_s3_client(FakeBoto3(), region=None, config=object(), prompt_auth=True)