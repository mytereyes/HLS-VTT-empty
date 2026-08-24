"""Command-line entry point for AWS CloudShell and local execution."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Sequence

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .errors import HlsVttError
from .models import BatchReport
from .s3_repository import S3Repository, normalize_prefix
from .workflow import process_batch

LOGGER = logging.getLogger(__name__)
DEFAULT_BUCKET = "dlar-prod"
DEFAULT_PREFIX = "ads/VODv3/H264/HLS/"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely generate and add empty WebVTT tracks to HLS ads in S3."
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET, help="S3 bucket name")
    parser.add_argument("--prefix", default=DEFAULT_PREFIX, help="Base S3 key prefix")
    parser.add_argument("--region", help="AWS region override")
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
    parser.add_argument("--report-file", type=Path, help="Write a JSON batch report")
    parser.add_argument(
        "--keep-staging-on-success",
        action="store_true",
        help="Retain staging objects even after complete successful promotion",
    )
    return parser


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


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
    _configure_logging(args.log_level)
    if not args.bucket.strip():
        parser.error("--bucket cannot be empty")
    try:
        base_prefix = normalize_prefix(args.prefix)
        client = boto3.client(
            "s3",
            region_name=args.region,
            config=Config(
                retries={"mode": "adaptive", "max_attempts": 10},
                connect_timeout=10,
                read_timeout=60,
            ),
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
    except (HlsVttError, BotoCoreError, ClientError, ValueError, OSError) as exc:
        LOGGER.error("Batch failed before completion: %s: %s", type(exc).__name__, exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
