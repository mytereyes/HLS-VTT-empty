from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from botocore.exceptions import NoCredentialsError

import pytest

from hls_vtt_s3 import cli
from hls_vtt_s3.models import AdResult, AdStatus, BatchReport


class DummyBoto3:
    def client(self, *_args: object, **_kwargs: object) -> object:
        return object()


def report(*, failed: bool = False) -> BatchReport:
    status = AdStatus.FAILED if failed else AdStatus.SKIPPED
    return BatchReport(
        bucket="bucket",
        base_prefix="base/",
        dry_run=True,
        elapsed_seconds=0.125,
        results=(
            AdResult(
                ad_prefix="base/AD/",
                status=status,
                dry_run=True,
                message="test",
                error_type="ValidationError" if failed else None,
                error_message="bad input" if failed else None,
            ),
        ),
    )


def test_cli_defaults_to_dry_run_and_writes_json_report(
    monkeypatch: object, tmp_path: object, capsys: object
) -> None:
    target = tmp_path / "report.json"  # type: ignore[operator]
    captured: dict[str, object] = {}

    def fake_process(_repository: object, prefix: str, **kwargs: object) -> BatchReport:
        captured.update(prefix=prefix, **kwargs)
        return report()

    monkeypatch.setattr(cli, "boto3", DummyBoto3())  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "process_batch", fake_process)  # type: ignore[attr-defined]
    assert cli.main(["--prefix", "/base///", "--report-file", str(target)]) == 0
    assert captured["prefix"] == "base/"
    assert captured["apply"] is False
    assert captured["keep_staging_on_success"] is False
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["mode"] == "DRY-RUN"
    assert payload["summary"]["skipped"] == 1
    assert "Mode: DRY-RUN" in capsys.readouterr().out  # type: ignore[attr-defined]


def test_cli_apply_and_failed_report_return_nonzero(monkeypatch: object) -> None:
    captured: dict[str, object] = {}

    def fake_process(_repository: object, prefix: str, **kwargs: object) -> BatchReport:
        captured.update(prefix=prefix, **kwargs)
        return report(failed=True)

    monkeypatch.setattr(cli, "boto3", DummyBoto3())  # type: ignore[attr-defined]
    monkeypatch.setattr(cli, "process_batch", fake_process)  # type: ignore[attr-defined]
    assert cli.main(["--apply", "--ad-prefix", "AD/"]) == 1
    assert captured["apply"] is True
    assert captured["ad_id"] == "AD/"


def test_cli_handles_boto3_initialization_failure(monkeypatch: object) -> None:
    class FailingBoto3:
        def client(self, *_args: object, **_kwargs: object) -> object:
            raise NoCredentialsError()

    monkeypatch.setattr(cli, "boto3", FailingBoto3())  # type: ignore[attr-defined]
    assert cli.main([]) == 1


def test_summary_prints_staging_and_partial_promotion(capsys: object) -> None:
    partial = report(failed=True)
    partial.results[0].staging_prefix = "base/AD/_vtt_staging/run/"
    partial.results[0].promoted_objects = ["base/AD/segment.vtt"]
    cli._print_summary(partial)
    output = capsys.readouterr().out  # type: ignore[attr-defined]
    assert "Staging prefix: base/AD/_vtt_staging/run/" in output
    assert "base/AD/segment.vtt" in output


def test_profile_and_prompt_auth_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.build_parser().parse_args(["--profile", "prod", "--prompt-auth"])
    assert exc_info.value.code == 2


def test_profile_does_not_open_interactive_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_create(
        _boto3: object, **kwargs: object
    ) -> object:
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(cli, "create_s3_client", fake_create)
    monkeypatch.setattr(cli, "process_batch", lambda *_args, **_kwargs: report())
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("profile mode opened an interactive prompt"),
    )
    assert cli.main(["--profile", "production"]) == 0
    assert captured["profile"] == "production"
    assert captured["prompt_auth"] is False


def test_authentication_cancellation_returns_documented_code_without_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError()))
    assert cli.main(["--prompt-auth"]) == cli.AUTH_EXIT_CODE
    captured = capsys.readouterr()
    assert "Traceback" not in captured.out + captured.err


def test_file_logging_creates_parent_rotates_and_retains_console(
    tmp_path: object, capsys: pytest.CaptureFixture[str]
) -> None:
    log_file = tmp_path / "nested" / "run.log"  # type: ignore[operator]
    cli._configure_logging("INFO", log_file)
    logging.getLogger("test.local").info("local logging works")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert log_file.read_text(encoding="utf-8").endswith("local logging works\n")
    assert "local logging works" in capsys.readouterr().err
    rotating = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, RotatingFileHandler)
    ]
    assert len(rotating) == 1
    assert rotating[0].maxBytes == 10 * 1024 * 1024
    assert rotating[0].backupCount == 5
