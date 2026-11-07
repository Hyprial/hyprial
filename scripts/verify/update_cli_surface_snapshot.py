#!/usr/bin/env python3
"""Regenerate ``contract/cli/surface.json`` from the real CLI application.

Run from the repository root with:

    uv run --python 3.12 python scripts/verify/update_cli_surface_snapshot.py

A PR that changes the registered CLI surface must regenerate the snapshot in
that same PR and explain the change in its body.  Removing or renaming any
command introduced after CLI refresh #1830 requires an explicit reason.

The application import happens only after this process is pointed at fresh,
private HOME/XDG/Hyprial directories.  Extraction walks Typer/Click metadata;
it does not invoke a command, start a daemon, or connect to one.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT = ROOT / "contract/cli/surface.json"


def _private_environment(root: Path) -> None:
    directories = {
        "HOME": root / "home",
        "XDG_CONFIG_HOME": root / "xdg",
        "HYPRIAL_HOME": root / "hyprial",
        "HARNESS_STATE_DIR": root / "state",
    }
    for name, path in directories.items():
        path.mkdir(mode=0o700)
        os.environ[name] = str(path)
    os.environ["HYPRIAL_NETWORK_ISOLATED"] = "1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="snapshot destination (default: contract/cli/surface.json)",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory(prefix="hyprial-cli-surface-") as raw:
        _private_environment(Path(raw))

        # Import only after isolation. This is the same app object composed by
        # hyprial.shell.main and therefore by the installed console entry point.
        from hyprial.shell.impl.cli.runtime import entry as cli
        from hyprial.shell.impl.cli.surface import extract_cli_surface, render_cli_surface

        document = extract_cli_surface(cli.app)
        rendered = render_cli_surface(document)

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    print(
        f"wrote {output} ({len(document['commands'])} command paths, "
        f"{len(rendered.encode('utf-8'))} bytes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
