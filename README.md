# HLS WebVTT S3 Processor

A production-oriented Python 3 application for AWS CloudShell that adds generated, intentionally empty WebVTT subtitle tracks to HLS Ads stored in Amazon S3. It uses `boto3` directly—never the AWS CLI—and defaults to a read-only dry run.

## What it does

For every immediate Ad prefix under `s3://dlar-prod/ads/VODv3/H264/HLS/` (or a configured location), the application:

1. Uses paginated `ListObjectsV2` requests with `Delimiter="/"` to discover immediate child prefixes only.
2. Checks for `h264_manifest-vtt-hls-h264-subtitle.m3u8`. If it exists, the entire Ad is `SKIPPED` before any source download.
3. Reads and validates these exact, case-sensitive objects:
   - `h264_manifest-1080-hls-h264-24p-video.m3u8` (timing authority)
   - `h264_manifest.m3u8` (authoritative audio language and primary master)
  - `Manifest.m3u8` (secondary master)
4. Generates one `vtt-hls-h264-{language}-{index}.vtt` segment per source `#EXTINF`.
5. Generates `h264_manifest-vtt-hls-h264-subtitle.m3u8`.
6. Adds one `TYPE=SUBTITLES` declaration to both master manifests and adds `SUBTITLES="vtt"` to every stream variant.
7. In `--apply` mode only, stages every output, verifies exact staged content, then promotes in dependency order.

One malformed or incomplete Ad becomes `FAILED`; processing continues for the remaining Ads. Existing subtitle configuration is a conflict and is never merged or replaced.

## Timing and language rules

All HLS durations are parsed with `Decimal`; binary floating-point arithmetic is not used. Inputs with sub-millisecond precision are rounded to the nearest millisecond using `ROUND_HALF_UP`. Cue calculations then use integer milliseconds.

The final source segment receives exactly 15 ms extra in two places only:

- the final WebVTT cue end timestamp;
- the final subtitle-playlist `#EXTINF`.

For source durations `6.000`, `6.000`, and `3.042`, the cues end at `00:00:06.000`, `00:00:12.000`, and `00:00:15.057`; the final subtitle duration is `3.057`. Earlier segments and all original media are unchanged.

The first AUDIO declaration in `h264_manifest.m3u8` is authoritative. Its exact `LANGUAGE` value is used in the subtitle declarations, VTT filenames, and playlist references. A conservative token of letters, digits, and hyphens is required. Differing later audio languages produce a warning and the first is selected. Missing/invalid language on the first primary AUDIO declaration or a mismatch with the first AUDIO language in `Manifest.m3u8` fails that Ad. A secondary manifest with no AUDIO declaration is allowed; it receives the selected primary language.

## Repository layout

- `src/hls_vtt_s3/manifest_parser.py` — pure playlist parsing, language detection, conflict checks, and minimally invasive master-manifest edits.
- `src/hls_vtt_s3/vtt_generator.py` — exact cue, segment, playlist, and validation logic.
- `src/hls_vtt_s3/s3_repository.py` — injectable boto3 S3 boundary and error classification.
- `src/hls_vtt_s3/workflow.py` — dry-run, staging, promotion, cleanup, and batch isolation.
- `src/hls_vtt_s3/cli.py` — arguments, retry configuration, logging, summaries, and reports.
- `tests/` — pure unit tests and a purpose-built boto3-compatible fake S3 workflow suite; no AWS calls.

## AWS CloudShell setup

AWS CloudShell already provides a configured AWS identity and Python. Do not put credentials in this repository.

```bash
cd ~/HLS-VTT-empty-add
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e .
```

An editable install provides the `hls-vtt-s3` command. Alternatively, after installing dependencies, use `PYTHONPATH=src python -m hls_vtt_s3` in place of `hls-vtt-s3`.

## Test safely (no AWS required)

The tests use an in-memory fake S3 client and do not require credentials or make network calls.

```bash
source .venv/bin/activate
python -m pytest
```

Optional coverage report:

```bash
python -m pytest --cov=hls_vtt_s3 --cov-report=term-missing
```

A “local dry run” still reads the configured S3 objects, so it requires AWS connectivity and read permissions; “local” means running the application from your workstation rather than CloudShell. For entirely local validation with zero AWS access, run the test suite.

## Run modes

Every invocation clearly logs and prints `DRY-RUN` or `APPLY`. Prefixes are normalized to no leading slash and exactly one trailing slash. `--ad-id XXX1` and `--ad-prefix XXX1/` are aliases and process only that immediate child folder.

Dry-run one Ad (recommended first production check):

```bash
hls-vtt-s3 --ad-id XXX1 --report-file report.json
```

Dry-run one Ad with explicit location and region:

```bash
hls-vtt-s3 --bucket dlar-prod --prefix ads/VODv3/H264/HLS/ --region us-east-1 --ad-prefix XXX1/
```

Dry-run all discovered Ads:

```bash
hls-vtt-s3 --bucket dlar-prod --prefix ads/VODv3/H264/HLS/ --report-file report.json
```

Apply one Ad only after reviewing its dry-run output:

```bash
hls-vtt-s3 --apply --ad-id XXX1 --report-file apply-XXX1.json
```

Apply all Ads:

```bash
hls-vtt-s3 --apply --bucket dlar-prod --prefix ads/VODv3/H264/HLS/ --report-file apply-all.json
```

Increase diagnostics without exposing credentials:

```bash
hls-vtt-s3 --log-level DEBUG --ad-id XXX1
```

Dry-run performs downloads, parsing, generation, and all in-memory validations, and reports every planned final key. It performs **no** `PutObject`, `CopyObject`, or `DeleteObjects` operation and creates no staging prefix. S3 changes require the explicit `--apply` flag.

## IAM permissions

The execution identity normally needs:

- `s3:ListBucket` on the bucket;
- `s3:GetObject`, `s3:PutObject`, and `s3:DeleteObject` on the configured object prefix.

Copying staged objects uses `CopyObject`, which S3 authorizes through `s3:GetObject` on the source and `s3:PutObject` on the destination. A least-privilege starting policy for the default location is:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListOnlyHlsAds",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::dlar-prod",
      "Condition": {
        "StringLike": {
          "s3:prefix": [
            "ads/VODv3/H264/HLS/",
            "ads/VODv3/H264/HLS/*"
          ]
        }
      }
    },
    {
      "Sid": "ReadWriteOnlyHlsAds",
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject"
      ],
      "Resource": "arn:aws:s3:::dlar-prod/ads/VODv3/H264/HLS/*"
    }
  ]
}
```

Do not replace these resources with unrestricted `*` unless a documented operational requirement outweighs the increased blast radius. Organization SCPs, bucket policies, VPC endpoint policies, object ownership, and explicit denies may further restrict access.

Versioned buckets may additionally require `s3:GetObjectVersion`; checking versioning operationally may require `s3:GetBucketVersioning`. For SSE-KMS objects, the key policy and IAM identity may require `kms:Decrypt`, `kms:Encrypt`, and `kms:GenerateDataKey`, scoped to the specific KMS key. The application relies on bucket default encryption for new objects; confirm that policy before applying.

## Staging, promotion, and recovery

Each apply creates a unique internal prefix:

`{ad_prefix}_vtt_staging/{UTC timestamp}-{UUID}/`

The application uploads all VTT segments, the subtitle playlist, and both modified masters there. It verifies every staged object is nonempty, has the expected size, and is byte-for-byte identical by SHA-256 comparison to the already validated output. Because identical staged bytes were validated before upload, this also revalidates staged manifest content.

Immediately before promotion, it checks again that the final subtitle playlist does not exist. Promotion uses server-side copies in this exact order:

1. VTT segments;
2. VTT subtitle playlist;
3. `h264_manifest.m3u8`;
4. `Manifest.m3u8`.

Each final object is downloaded and hash-verified after copy. New final segment/playlist keys use conditional `If-None-Match: *` copies, while each master replacement uses `If-Match` with the ETag captured before download. All source ETags (and version IDs when available) are rechecked after staging. These safeguards refuse known generated-key collisions and stale master replacements. Staging is deleted only after complete success. `--keep-staging-on-success` retains it intentionally.

For modified master objects, the application explicitly preserves cache/content headers, custom metadata, website redirect, storage class, and supported server-side-encryption/KMS settings returned by `HeadObject`, while setting the required HLS content type. S3 object tags and legacy ACL grants are not copied by this application; use bucket-owner-enforced object ownership and bucket policy, and confirm that tag-based operations do not apply to these manifests before rollout.

Amazon S3 has no atomic transaction spanning multiple objects. Dependency ordering and per-object preconditions reduce, but cannot eliminate, partial-promotion risk. If a copy, verification, or cleanup fails, the Ad is `FAILED`, the JSON/text report lists promoted objects, and remaining staging objects are retained. The application does **not** claim or attempt an unsafe automatic rollback. Per the primary idempotency contract, a rerun skips an Ad once the final VTT playlist exists—even if a prior run failed while promoting a later master—so partial-promotion reports must be investigated rather than relying on an automatic rerun repair.

### Investigating a failed apply

1. Preserve the JSON report and logs; note `staging_prefix`, `error_type`, `error_message`, and `promoted_objects`.
2. Compare the retained staging keys with the reported final keys using the S3 console or approved operational tooling.
3. Determine whether final objects were created and whether consumers observed them.
4. Correct permissions, encryption, throttling, malformed input, or other root cause.
5. If the final VTT playlist now exists, a rerun will `SKIP` under the required idempotency rule. Verify both masters and every promoted key; make any recovery decision through your change-management process rather than deleting the playlist blindly.
6. Remove retained staging objects only after investigation and recovery are complete.

Enable S3 bucket versioning before production rollout. Versioning is strongly recommended because the two master manifests are overwritten during promotion and this application does not create separate backups. Versioning provides a recoverable history, subject to lifecycle and permissions.

## Logging, reports, and exit codes

Python logging includes each Ad prefix and final `PROCESSED`, `SKIPPED`, or `FAILED` status. It never intentionally logs credentials, tokens, signed URLs, or environment-variable contents.

`--report-file PATH` writes JSON containing mode, elapsed time, batch totals, language, source/generated counts, original/adjusted durations, planned keys, staging prefix, exact error details, and partially promoted keys. Protect reports according to your normal operational data policy.

Exit codes:

- `0`: completed with no `FAILED` Ads; `SKIPPED` is successful.
- `1`: one or more Ads failed, or an AWS/filesystem error prevented batch completion.
- `2`: invalid command-line syntax or argument configuration reported by `argparse`.

## Post-deployment validation

After deployment, validate a controlled Ad before scaling out:

1. Confirm all expected VTT segments and the VTT playlist exist and have the documented content types.
2. Inspect the subtitle playlist: one URI per source `#EXTINF`, sequential indexes, final duration +15 ms, valid target duration, and `#EXT-X-ENDLIST`.
3. Inspect both masters: exactly one generated `TYPE=SUBTITLES` line and exactly one `SUBTITLES="vtt"` per stream variant.
4. Verify every playlist URI resolves with the playback identity/CDN path, not only with an administrator identity.
5. Run your approved HLS validator and playback tests against representative players/CDNs. Confirm the intentionally empty subtitle track is selectable/handled as expected.
6. Check CloudTrail/S3 access logs, application output, the JSON report, and playback monitoring for errors.
7. Retain deployment reports and use a small canary batch before an all-Ad apply.

## Operational notes and limitations

- Object names and language tokens are case-sensitive.
- Ads are immediate children only; nested folders and `_vtt_staging` prefixes are not interpreted as Ads.
- Boto3 uses adaptive retry mode with a configured `max_attempts` value of 10 plus bounded connection/read timeouts. AccessDenied is reported distinctly from missing objects.
- Conditional per-object copies prevent expected destination collisions and stale master ETags, but S3 offers no conditional multi-object transaction for the workflow as a whole.
- Source and generated manifest downloads are bounded to 10 MiB per object to protect the CloudShell process from unexpectedly large inputs.
- Staging and final verification download generated objects, increasing request count and data transfer slightly in exchange for stronger safety.
- Empty base prefixes are valid and return a successful zero-Ad report.
- No real-AWS integration test is included or run automatically. Production S3 writes must be exercised intentionally in an approved sandbox/canary process.
