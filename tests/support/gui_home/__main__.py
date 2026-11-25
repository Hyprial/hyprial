"""CLI entrypoint for preparing an isolated, device-ready test home.

Decision record (S2, 2026-11-21):
- Exposes ``python -m tests.support.gui_home prepare --home <dir> --owner <name> --json``.
- ``--home`` is strictly required with no default and never consults
  ``HYPRIAL_HOME``.
- Lives under ``tests/support/`` rather than ``src/hyprial/`` so the helper is
  never packaged into wheels or exposed as a product CLI command.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .prepare import (
    DEFAULT_DEVICE_NAME,
    DEFAULT_ISSUER,
    ReadyHomeError,
    prepare_ready_home,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m tests.support.gui_home",
        description="Prepare an isolated, device-ready hyprial home for acceptance tests.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare_parser = subparsers.add_parser(
        "prepare",
        help="Prepare a new isolated device-ready hyprial home.",
    )
    prepare_parser.add_argument(
        "--home",
        type=Path,
        required=True,
        default=argparse.SUPPRESS,
        help="Explicit isolated home directory (must be outside any real HOME).",
    )
    prepare_parser.add_argument(
        "--owner",
        required=True,
        help="Account owner name for the prepared home.",
    )
    prepare_parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Optional explicit state directory (defaults to <home>/state).",
    )
    prepare_parser.add_argument(
        "--issuer",
        default=DEFAULT_ISSUER,
        help=f"Synthetic issuer URL (default: {DEFAULT_ISSUER}).",
    )
    prepare_parser.add_argument(
        "--device-name",
        default=DEFAULT_DEVICE_NAME,
        help=f"Synthetic device name (default: {DEFAULT_DEVICE_NAME}).",
    )
    prepare_parser.add_argument(
        "--json",
        action="store_true",
        dest="emit_json",
        help="Emit a single JSON summary object on stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "prepare":
        parser.error(f"unsupported command: {args.command}")
    try:
        ready = prepare_ready_home(
            args.home,
            owner=args.owner,
            state_dir=args.state_dir,
            issuer=args.issuer,
            device_name=args.device_name,
        )
    except ReadyHomeError as error:
        if args.emit_json:
            print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        print(f"gui_home: {error}", file=sys.stderr)
        return 1

    if args.emit_json:
        print(json.dumps(ready.as_dict(), ensure_ascii=False))
        return 0

    print(f"home: {ready.home}")
    print(f"owner: {ready.owner}")
    print(f"nodeId: {ready.node_id}")
    print(f"marker: {ready.marker_path}")
    for path in ready.written_files:
        print(f"wrote: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
