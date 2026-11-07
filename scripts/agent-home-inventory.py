#!/usr/bin/env python3
"""Read-only inventory for the proposed agent-home structure.

The command deliberately accepts explicit home paths.  It does not consult or
mutate the daemon registry, create missing directories, or read file contents
other than ``.git`` pointers used to identify worktrees.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Iterable


EXPECTED = {
    "config": "config",
    "state": "state",
    "secrets": "secrets",
    "workspace": "workspace",
    "persona": "persona",
    "sandbox": "sandbox",
    "notes": "workspace/notes",
    "skills": "persona/skills",
}


def _exists(path: Path) -> bool:
    """Return existence without following a broken final symlink."""

    return path.exists() or path.is_symlink()


def _iter_entries(root: Path) -> Iterable[Path]:
    """Walk *root* without following symlinked directories."""

    if not root.is_dir():
        return
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            path = current_path / name
            if path.is_symlink():
                yield path
            else:
                yield path
        for name in files:
            yield current_path / name


def _count_external_refs(home: Path) -> dict[str, int]:
    """Count worktree pointers, Python environments, and symlinks.

    A worktree is identified by a regular ``.git`` file containing a
    ``gitdir:`` pointer.  A virtual environment is identified by
    ``pyvenv.cfg`` or by a conventional ``venv``/``.venv``/``env`` directory
    containing that marker.  The scan never follows symlinked directories.
    """

    worktrees = 0
    venvs: set[Path] = set()
    symlinks = 0
    secret_root = home / "secrets"
    for path in _iter_entries(home):
        try:
            if path.is_symlink():
                symlinks += 1
                continue
            if path == secret_root or secret_root in path.parents:
                continue
            if path.name == ".git" and path.is_file():
                with path.open("rb") as stream:
                    content = stream.read(4096)
                if content.lstrip().startswith(b"gitdir:"):
                    worktrees += 1
            if path.name == "pyvenv.cfg" and path.is_file():
                venvs.add(path.parent)
        except OSError:
            # A read-only inventory must still report the other observable
            # fields when one entry disappears or is not readable mid-scan.
            continue

    # Keep the count tied to actual Python-environment markers; the directory
    # name alone is too ambiguous in an agent workspace.
    del home
    return {"worktrees": worktrees, "venv": len(venvs), "symlinks": symlinks}


def inventory(home: Path, agent: str | None = None) -> dict[str, object]:
    """Return the JSON-compatible inventory for one agent home."""

    resolved = home.expanduser().resolve(strict=False)
    name = agent or resolved.name
    paths = {key: resolved / relative for key, relative in EXPECTED.items()}
    present = {key: _exists(path) for key, path in paths.items()}
    absent = {key: not value for key, value in present.items()}
    return {
        "agent": name,
        "home": str(resolved),
        "sandbox": str(paths["sandbox"]),
        "workspace": str(paths["workspace"]),
        "notes_dir": str(paths["notes"]),
        "persona_dir": str(paths["persona"]),
        "skills_dir": str(paths["skills"]),
        "present": present,
        "absent": absent,
        "legacy": {
            "records": _exists(resolved / "records"),
            "e2e_at_top_level": _exists(resolved / "e2e"),
        },
        "external_refs": _count_external_refs(resolved),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only JSON inventory of one or more agent homes."
    )
    parser.add_argument(
        "homes",
        nargs="+",
        type=Path,
        metavar="AGENT_HOME",
        help="Explicit agent-home path(s); no path is inferred or created.",
    )
    parser.add_argument(
        "--agent",
        help="Agent name for a single home (defaults to the final path component).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.agent is not None and len(args.homes) != 1:
        raise SystemExit("--agent is valid only with one AGENT_HOME")
    result = [inventory(path, args.agent) for path in args.homes]
    payload: object = result[0] if len(result) == 1 else {"homes": result}
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
