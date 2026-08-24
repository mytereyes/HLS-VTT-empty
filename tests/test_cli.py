from __future__ import annotations

import json
from botocore.exceptions import NoCredentialsError

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
