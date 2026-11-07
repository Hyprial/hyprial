#!/usr/bin/env python3
"""Retire one explicit agent home into its local ``destroyed`` graveyard.

The default mode is a read-only plan. Applying a plan writes its audit receipt
before any move, never copies secret material into the graveyard, and writes a
completion marker only after every destructive operation has succeeded.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


class DestroyError(RuntimeError):
    """An agent home cannot be safely planned or retired."""


def _kind(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _utc_iso(now: datetime | None) -> str:
    value = datetime.now(timezone.utc) if now is None else now
    if value.tzinfo is None or value.utcoffset() is None:
        raise DestroyError("destruction time must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _validate_text(value: str, field: str) -> None:
    if not value or "\n" in value or "\r" in value:
        raise DestroyError(f"{field} must be a non-empty single line")


def _iter_entries(root: Path) -> Iterable[Path]:
    if _kind(root) != "directory":
        return
    for current, directories, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in directories:
            yield current_path / name
        for name in files:
            yield current_path / name


def _reject_secret_hardlinks(home: Path) -> None:
    """Refuse aliases anywhere, including outside the authorized home.

    Never truncate a shared inode: that would modify its other names. Symlinks
    are unlinked, not followed. An unreadable subtree is not proof of safety.
    The caller must keep this offline home quiescent through apply.
    """
    root = home / "secrets"

    def inspect(path: Path) -> None:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            if info.st_nlink > 1:
                raise DestroyError(f"hardlinked secret is unsupported: {path}")

    def unreadable(error: OSError) -> None:
        raise error

    try:
        if _kind(root) == "absent":
            return
        inspect(root)
        if _kind(root) == "directory":
            for current, directories, files in os.walk(root, followlinks=False, onerror=unreadable):
                for name in directories + files:
                    inspect(Path(current) / name)
    except OSError as exc:
        raise DestroyError(f"cannot inspect secret tree: {root} ({type(exc).__name__})") from exc


def _named_tree(path: Path, home: Path, *, include_root: bool = False) -> list[str]:
    kind = _kind(path)
    if kind == "absent":
        return []
    names = [path.relative_to(home).as_posix()] if include_root else []
    if kind == "directory":
        names.extend(entry.relative_to(home).as_posix() for entry in _iter_entries(path))
    elif not include_root:
        names.append(path.relative_to(home).as_posix())
    return sorted(set(names))


def _external_refs(home: Path) -> dict[str, int]:
    worktrees = 0
    virtual_environments: set[Path] = set()
    symlinks = 0
    secret_root = home / "secrets"
    for path in _iter_entries(home):
        try:
            if path.is_symlink():
                symlinks += 1
                continue
            # Secret names belong in the destruction receipt, but their bytes
            # must never be interpreted by inventory heuristics. In
            # particular, a credential file named .git is not a worktree.
            if path == secret_root or secret_root in path.parents:
                continue
            if path.name == ".git" and path.is_file():
                with path.open("rb") as stream:
                    content = stream.read(4096)
                if content.lstrip().startswith(b"gitdir:"):
                    worktrees += 1
            if path.name == "pyvenv.cfg" and path.is_file():
                virtual_environments.add(path.parent)
        except OSError:
            continue
    result = {
        "worktrees": worktrees,
        "venv": len(virtual_environments),
        "symlinks": symlinks,
    }
    result["total"] = sum(result.values())
    return result


def _state_plan(home: Path) -> tuple[list[str], list[str]]:
    state = home / "state"
    kind = _kind(state)
    if kind == "absent":
        return [], []
    if kind != "directory":
        return [], ["state"]

    files: list[str] = []
    discarded: list[str] = []
    for entry in sorted(state.iterdir(), key=lambda item: item.name):
        if _kind(entry) == "file":
            files.append(entry.relative_to(home).as_posix())
        else:
            discarded.extend(_named_tree(entry, home, include_root=True))
    return files, sorted(set(discarded))


def _plan(
    home: Path, *, who: str, why: str, now: datetime | None
) -> dict[str, object]:
    _validate_text(who, "who")
    _validate_text(why, "why")
    home = home.expanduser().absolute()
    if _kind(home) != "directory":
        raise DestroyError(f"home must be an existing real directory: {home}")
    if home.name in {"", ".", "..", "destroyed"}:
        raise DestroyError(f"invalid agent-home name: {home.name!r}")

    _reject_secret_hardlinks(home)
    when = _utc_iso(now)
    destroyed_root = home.parent / "destroyed"
    root_kind = _kind(destroyed_root)
    if root_kind not in {"absent", "directory"}:
        raise DestroyError(f"destroyed root must be a real directory: {destroyed_root}")
    target = destroyed_root / f"{home.name}@{when}"
    if _kind(target) != "absent":
        raise DestroyError(f"target already exists: {target}")

    entries = sorted(home.iterdir(), key=lambda item: item.name)
    reserved = {"COMPLETE", "DESTROYED.json"}
    collision = sorted(entry.name for entry in entries if entry.name in reserved)
    if collision:
        raise DestroyError("source contains reserved destruction artifacts: " + ", ".join(collision))

    subdirs = [
        entry.name
        for entry in entries
        if entry.name not in {"secrets", "state"} and _kind(entry) == "directory"
    ]
    files = [
        entry.name
        for entry in entries
        if entry.name not in {"secrets", "state"} and _kind(entry) != "directory"
    ]
    state_files, state_discarded = _state_plan(home)
    secrets = _named_tree(home / "secrets", home)
    external_refs = _external_refs(home)
    what = {
        "source": str(home),
        "target": str(target),
        "subdirs_moved": subdirs,
        "files_moved": files,
        "state_files_moved": state_files,
        "state_discarded": state_discarded,
        "secrets_deleted": secrets,
    }
    receipt = {
        "who": who,
        "when": when,
        "why": why,
        "what": what,
        "external_refs": external_refs,
    }
    return {
        "home": str(home),
        "target": str(target),
        "subdirs_to_move": subdirs,
        "files_to_move": files,
        "state_files_to_move": state_files,
        "state_discarded": state_discarded,
        "secrets_to_delete": secrets,
        "receipt": receipt,
    }


def _write_receipt(target: Path, receipt: dict[str, object]) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if _kind(target.parent) != "directory":
        raise DestroyError(f"destroyed root is not a directory: {target.parent}")
    target.mkdir(mode=0o700)
    path = target / "DESTROYED.json"
    with path.open("x", encoding="utf-8") as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    path.chmod(0o600)


def _move_path(source: Path, destination: Path) -> None:
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.rename(source, destination)


def _remove_path(path: Path) -> None:
    kind = _kind(path)
    if kind == "absent":
        return
    if kind == "directory":
        shutil.rmtree(path)
    else:
        path.unlink()


def _delete_secrets(path: Path) -> None:
    """Delete the secret root without following a symlink at that path."""

    _remove_path(path)


def _write_completion_marker(target: Path, when: str) -> None:
    marker = target / "COMPLETE"
    with marker.open("x", encoding="utf-8") as stream:
        stream.write(f"{when}\n")
    marker.chmod(0o600)


def _apply(plan: dict[str, object]) -> None:
    home = Path(str(plan["home"]))
    target = Path(str(plan["target"]))
    receipt = plan["receipt"]
    assert isinstance(receipt, dict)
    try:
        _reject_secret_hardlinks(home)
        # The receipt is the durable intent record. Its missing COMPLETE
        # companion distinguishes any interrupted application.
        _write_receipt(target, receipt)

        for relative in plan["subdirs_to_move"]:
            source = home / str(relative)
            _move_path(source, target / str(relative))
        for relative in plan["files_to_move"]:
            source = home / str(relative)
            _move_path(source, target / str(relative))
        for relative in plan["state_files_to_move"]:
            source = home / str(relative)
            _move_path(source, target / str(relative))

        # State directories are reproducible caches. Only direct, unique state
        # files survive in the graveyard; every discarded path is in receipt.
        _remove_path(home / "state")

        # Secret bytes never enter the target. This deliberately follows all
        # non-secret moves so an early move failure leaves credentials intact.
        _delete_secrets(home / "secrets")
        home.rmdir()
        _write_completion_marker(target, str(receipt["when"]))
    except Exception as exc:
        if isinstance(exc, DestroyError):
            raise
        raise DestroyError(f"destruction incomplete: {exc}") from exc


def destroy(
    home: Path,
    *,
    who: str,
    why: str,
    yes: bool = False,
    now: datetime | None = None,
) -> dict[str, object]:
    """Return a JSON-compatible dry-run or apply result for one home."""

    plan = _plan(home, who=who, why=why, now=now)
    result = {**plan, "dry_run": not yes}
    if yes:
        _apply(plan)
        result["dry_run"] = False
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Retire one explicit agent home into agents/destroyed."
    )
    parser.add_argument("home", type=Path, help="Explicit agent-home path")
    parser.add_argument("--who", required=True, help="Principal authorizing destruction")
    parser.add_argument("--why", required=True, help="Single-line destruction reason")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply destruction; without this flag the command is a dry run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = destroy(args.home, who=args.who, why=args.why, yes=args.yes)
    except DestroyError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
