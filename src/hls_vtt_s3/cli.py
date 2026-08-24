"""Command-line entry point for AWS CloudShell and local execution."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Sequence

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .auth import AuthenticationError, create_s3_client
from .errors import HlsVttError
from .models import BatchReport
from .s3_repository import S3Repository, normalize_prefix
from .workflow import process_batch

LOGGER = logging.getLogger(__name__)
DEFAULT_BUCKET = "dlar-prod"
DEFAULT_PREFIX = "ads/VODv3/H264/HLS/"
AUTH_EXIT_CODE = 3
LOG_MAX_BYTES = 10 * 1024 * 1024
LOG_BACKUP_COUNT = 5
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely generate and add empty WebVTT tracks to HLS ads in S3."
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="S3 bucket name")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="Base S3 key prefix")
    parser.add_argument("--region", help="AWS region override")
    auth_group = parser.add_mutually_exclusive_group()
    auth_group.add_argument(
        "--profile",
        help="Use a named AWS profile through boto3.Session",
    )
    auth_group.add_argument(
        "--prompt-auth",
        action="store_true",
        help="Interactively select the AWS authentication method",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform staged S3 writes; without this flag the run is read-only",
    )
    parser.add_argument(
        "--ad-id",
        "--ad-prefix",
        dest="ad_id",
        help="Process one immediate child Ad ID (a trailing slash is accepted)",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="Also write rotating UTF-8 logs to this local file",
    )
    parser.add_argument("--report-file", type=Path, help="Write a JSON batch report")
    parser.add_argument(
        "--keep-staging-on-success",
        action="store_true",
        help="Retain staging objects even after complete successful promotion",
    )
    return parser


def _configure_logging(level: str, log_file: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    for handler in root.handlers[:]:
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            log_file,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)


def _print_summary(report: BatchReport) -> None:
    mode = "DRY-RUN" if report.dry_run else "APPLY"
    print(f"Mode: {mode}")
    print(f"Total Ads discovered: {report.total_ads_discovered}")
    print(f"Processed: {report.processed}")
    print(f"Skipped: {report.skipped}")
    print(f"Failed: {report.failed}")
    print(f"Total VTT segments generated: {report.total_vtt_segments_generated}")
    print(f"Elapsed time: {report.elapsed_seconds:.3f} seconds")
    for item in report.results:
        detail = item.message
        if item.error_message:
            detail = f"{item.error_type}: {item.error_message}"
        print(f"{item.status.value} {item.ad_prefix} - {detail}")
        if item.staging_prefix:
            print(f"  Staging prefix: {item.staging_prefix}")
        if item.promoted_objects:
            print("  Promoted objects:")
            for key in item.promoted_objects:
                print(f"    - {key}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _configure_logging(args.log_level, args.log_file)
    except OSError as exc:
        print(f"Unable to configure logging: {exc}", file=sys.stderr)
        return 1
    if not args.bucket.strip():
        parser.error("--bucket cannot be empty")
    try:
        base_prefix = normalize_prefix(args.prefix)
        client = create_s3_client(
            boto3,
            region=args.region,
            config=Config(
                retries={"mode": "adaptive", "max_attempts": 10},
                connect_timeout=10,
                read_timeout=60,
            ),
            profile=args.profile,
            prompt_auth=args.prompt_auth,
        )
        repository = S3Repository(client, args.bucket.strip())
        mode = "APPLY" if args.apply else "DRY-RUN"
        LOGGER.warning("Operating mode: %s", mode)
        report = process_batch(
            repository,
            base_prefix,
            apply=args.apply,
            ad_id=args.ad_id,
            keep_staging_on_success=args.keep_staging_on_success,
        )
        _print_summary(report)
        if args.report_file:
            args.report_file.write_text(
                json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            LOGGER.info("Wrote JSON report to %s", args.report_file)
        return 1 if report.failed else 0
    except AuthenticationError as exc:
        LOGGER.error("Authentication failed or was cancelled: %s", exc)
        return AUTH_EXIT_CODE
    except (HlsVttError, BotoCoreError, ClientError, ValueError, OSError) as exc:
        LOGGER.error("Batch failed before completion: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
