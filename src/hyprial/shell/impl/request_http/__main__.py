"""``python -m hyprial.shell.impl.request_http serve --config <absolute path>``."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from . import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hyprial-request-http")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("serve", help="run the local HTTP request entry")
    run.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command != "serve":
        parser.error("unknown command")
    return serve(args.config)


if __name__ == "__main__":
    sys.exit(main())
