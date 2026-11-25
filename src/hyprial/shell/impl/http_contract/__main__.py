"""Offline H-02 exporter: daemon catalog JSON -> canonical HTTP OpenAPI.

Usage:
    uv run python -m hyprial.shell.impl.http_contract \
        --catalog catalog.json --out http-openapi.json --digest

``catalog.json`` is the payload of ``GET /daemon/request`` (or any object with
a ``methods`` list of admitted entries).  The output is deterministic: equal
input bytes produce equal output bytes on every host, with no daemon, clock,
home directory or environment in the document.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .projection import (
    ProjectionError,
    canonical_json,
    document_digest,
    owners_from_document,
    project_catalog,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hyprial-http-contract",
        description="Project an admitted daemon HTTP catalog as canonical OpenAPI.",
    )
    parser.add_argument(
        "--catalog",
        default="-",
        help="catalog JSON path, or - for stdin (default: -)",
    )
    parser.add_argument(
        "--out",
        default="-",
        help="output path, or - for stdout (default: -)",
    )
    parser.add_argument(
        "--owners",
        default=None,
        help=(
            "optional owner adapter: a daemon.catalog JSON or a plain "
            "{\"method.name\": \"owner\"} map"
        ),
    )
    parser.add_argument(
        "--digest",
        action="store_true",
        help="write the canonical sha256 digest to stderr",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        raw = (
            sys.stdin.read()
            if args.catalog == "-"
            else Path(args.catalog).read_text(encoding="utf-8")
        )
        owners = None
        if args.owners is not None:
            owners = owners_from_document(
                json.loads(Path(args.owners).read_text(encoding="utf-8"))
            )
        document = project_catalog(json.loads(raw), owners=owners)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProjectionError) as error:
        print(f"http-contract: {error}", file=sys.stderr)
        return 2
    text = canonical_json(document) + "\n"
    if args.out == "-":
        sys.stdout.write(text)
        sys.stdout.flush()
    else:
        try:
            Path(args.out).write_text(text, encoding="utf-8")
        except OSError as error:
            print(f"http-contract: {error}", file=sys.stderr)
            return 2
    if args.digest:
        print(document_digest(document), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
