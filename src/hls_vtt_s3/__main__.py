"""Allow execution with ``python -m hls_vtt_s3``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
