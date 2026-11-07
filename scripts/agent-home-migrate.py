#!/usr/bin/env python3
"""Migrate one legacy agent home into the workspace notes/e2e layout.

The command is intentionally explicit about its home argument.  It performs
no writes unless ``--yes`` is supplied and uses a small rename transaction for
the apply path so a failed migration is rolled back before the error is
reported.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile
from pathlib import Path


class MigrationError(RuntimeError):
    """A migration cannot be safely planned or completed."""


class Move:
    __slots__ = ("source", "destination")

    def __init__(self, source: Path, destination: Path) -> None:
        self.source = source
        self.destination = destination


#: ``state/home`` is deliberately not moved here.  The harness HOME becomes a
#: per-organelle ``sandbox/<organelle>/`` directory with the cell mechanism and is
#: migrated once, there (Allen 2026-10-26).
MOVES = (
    (Path("records"), Path("workspace/notes")),
    (Path("workspace/records"), Path("workspace/notes")),
    (Path("e2e"), Path("workspace/e2e")),
)

#: Two legacy sources that share one destination (spec v3.1).  They are an
#: either/or layout: a home holding both is ambiguous, because picking one would
#: silently leave the other behind.
EXCLUSIVE_SOURCES = (
    (Path("records"), Path("workspace/records")),
)


def _error_category(exc: Exception) -> str:
    """Do not serialize exception payloads, which may contain file bytes."""
    category = type(exc).__name__
    if isinstance(exc, OSError) and exc.errno is not None:
        category += f" ({os.strerror(exc.errno)})"
    return category


def _kind(path: Path) -> str:
    """Return a non-following-filesystem kind for *path*."""

    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        # "cannot inspect" is a refusal, not a crash: an unreadable path must not
        # reach the CLI as a traceback, and it must not be read as absent either.
        raise MigrationError(f"cannot inspect {path}: {_error_category(exc)}") from exc
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    return "file" if stat.S_ISREG(mode) else "other"


def _require_directory(path: Path, *, allow_absent: bool = True) -> None:
    kind = _kind(path)
    if kind == "absent" and allow_absent:
        return
    if kind != "directory":
        raise MigrationError(f"expected directory or absent path: {path} ({kind})")


def _resolved_home(home: Path) -> Path:
    home = home.absolute()
    if _kind(home) != "directory":
        raise MigrationError(f"home must be an existing real directory: {home}")
    # Accept platform aliases above the explicit root (e.g. /tmp on macOS).
    try:
        return home.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MigrationError(f"cannot resolve home: {home} ({_error_category(exc)})") from exc


def _validate_parent_chain(home: Path, path: Path) -> None:
    """Check every component from the resolved root outward, never through a link.

    This is an offline migration: the caller must keep the home quiescent.
    No component is resolved before its ancestors have passed lstat checks.
    """
    if not path.is_relative_to(home) or ".." in path.relative_to(home).parts:
        raise MigrationError(f"migration path is outside home: {path}")
    current = home
    for part in path.relative_to(home).parts:
        current = current / part
        kind = _kind(current)
        if kind == "absent":
            return  # Remaining descendants cannot exist without this parent.
        if kind != "directory":
            raise MigrationError(f"migration path must be a real directory: {current} ({kind})")


def _reject_ambiguous_legacy(home: Path) -> None:
    """Refuse a home that holds two sources for the same destination."""

    for first, second in EXCLUSIVE_SOURCES:
        if _kind(home / first) == "directory" and _kind(home / second) == "directory":
            raise MigrationError(
                "ambiguous legacy layout: both "
                f"{home / first} and {home / second} exist; "
                "migrate one of them explicitly first"
            )


def _reject_git_pointer(path: Path) -> None:
    """Refuse a git *pointer* (registered worktree or submodule) inside a move.

    A plain repository keeps ``.git`` as a directory and moves as one tree.  A
    pointer is a file holding an absolute path into another repository; moving
    the tree would leave the pointer aiming at a directory that still exists and
    no longer matches the moved content.
    """

    pointer = path / ".git"
    pointer_kind = _kind(pointer)
    if pointer_kind in {"absent", "directory"}:
        return
    kind = "git pointer"
    if pointer_kind == "file":
        try:
            # Classification only. Never decode or echo arbitrary file bytes.
            with pointer.open("rb") as stream:
                prefix = stream.read(4096).lstrip()
            if prefix.startswith(b"gitdir:") and b"/worktrees/" in prefix:
                kind = "registered worktree"
        except OSError:
            pass  # Unreadable pointers still get a content-free refusal.
    raise MigrationError(
        f"{kind} inside migration source: {path}; "
        "unregister or detach it first, the pointer cannot be moved"
    )


def _device(path: Path) -> int | None:
    """Return the device id of the nearest existing ancestor, or ``None``.

    ``None`` means "could not be determined" and is deliberately not a sentinel
    number: a sentinel can enter the comparison and turn every run into a
    refusal (false red), or a caller can skip it with ``is not None`` and let
    the check silently pass (false green, the more expensive of the two).
    """

    current = path
    while True:
        try:
            return os.stat(current).st_dev
        except FileNotFoundError:
            parent = current.parent
            if parent == current:
                return None
            current = parent
        except OSError:
            # Unreadable is not absent: os.path.exists() would swallow this into
            # False and hand back an ancestor's device as if it were this one.
            return None


def _reject_cross_device(home: Path, source: Path) -> None:
    """Refuse a move whose rename would cross filesystems.

    ``os.rename`` never copies: across devices it raises ``EXDEV``.  The staging
    directory lives in ``home.parent``, so a home that is itself a mount point
    makes every rename cross-device and the whole apply fails at the first move.
    """

    staging_device = _device(home.parent)
    source_device = _device(source)
    if source_device is None or staging_device is None:
        unknown = source if source_device is None else home.parent
        raise MigrationError(
            f"cannot determine the device of {unknown}; refusing to migrate "
            "(an unreadable device is not evidence that both sides match)"
        )
    if source_device != staging_device:
        raise MigrationError(
            f"cross-device migration is unsupported: {source} (device {source_device}) "
            f"stages under {home.parent} (device {staging_device}); "
            "os.rename cannot copy across filesystems"
        )


def _plan(home: Path) -> tuple[list[Move], list[Path]]:
    home = _resolved_home(home)
    # Preflight every source and destination, even when no move is needed.
    # In particular, check state/workspace before inspecting their children.
    for source, destination in MOVES:
        _validate_parent_chain(home, home / source)
        _validate_parent_chain(home, home / destination)
    _reject_ambiguous_legacy(home)

    moves: list[Move] = []
    for source_relative, destination_relative in MOVES:
        source = home / source_relative
        destination = home / destination_relative
        source_kind = _kind(source)
        destination_kind = _kind(destination)

        if source_kind == "symlink" or destination_kind == "symlink":
            raise MigrationError(f"symlink migration paths are unsupported: {source} or {destination}")
        if source_kind == "file" or destination_kind == "file":
            raise MigrationError(f"migration paths must be directories: {source} or {destination}")
        if source_kind == "directory" and destination_kind == "directory":
            raise MigrationError(f"source and destination both exist: {source} -> {destination}")
        if source_kind == "directory":
            _reject_git_pointer(source)
            _reject_cross_device(home, source)
            _validate_parent_chain(home, destination.parent)
            moves.append(Move(source, destination))

    creates: list[Path] = []
    persona = home / "persona"
    skills = persona / "skills"
    for path in (persona, skills):
        kind = _kind(path)
        if kind not in {"absent", "directory"}:
            raise MigrationError(f"persona path must be a real directory: {path} ({kind})")
        if kind == "absent":
            _validate_parent_chain(home, path.parent)
            creates.append(path)

    return moves, creates


def _mkdir_recorded(path: Path, created: list[Path]) -> None:
    """Create a directory and record only directories created by this run."""

    if _kind(path) == "directory":
        return
    if _kind(path) != "absent":
        raise MigrationError(f"cannot create directory: {path}")
    _mkdir_recorded(path.parent, created)
    path.mkdir()
    created.append(path)


def _rollback(
    staged: list[tuple[Move, Path]],
    delivered: list[tuple[Move, Path]],
    created: list[Path],
    stage: Path,
) -> list[str]:
    errors: list[str] = []
    for move, staged_path in reversed(delivered):
        try:
            os.rename(move.destination, staged_path)
        except OSError as exc:
            errors.append(f"restore destination {move.destination}: {_error_category(exc)}")
    for move, staged_path in reversed(staged):
        if _kind(staged_path) == "absent":
            continue
        try:
            os.rename(staged_path, move.source)
        except OSError as exc:
            errors.append(f"restore source {move.source}: {_error_category(exc)}")
    for path in reversed(created):
        try:
            path.rmdir()
        except OSError as exc:
            errors.append(f"remove created directory {path}: {_error_category(exc)}")
    try:
        stage.rmdir()
    except OSError as exc:
        errors.append(f"remove staging directory {stage}: {_error_category(exc)}")
    return errors


def apply(home: Path, moves: list[Move], creates: list[Path]) -> None:
    """Apply the plan, rolling back all observable changes on failure."""

    if not moves and not creates:
        return

    _plan(home)  # Recheck before the first write if a caller held the plan.
    stage = Path(tempfile.mkdtemp(prefix=".agent-home-migrate-", dir=home.parent))
    staged: list[tuple[Move, Path]] = []
    delivered: list[tuple[Move, Path]] = []
    created: list[Path] = []
    try:
        for index, move in enumerate(moves):
            staged_path = stage / str(index)
            os.rename(move.source, staged_path)
            staged.append((move, staged_path))

        for move, staged_path in staged:
            _mkdir_recorded(move.destination.parent, created)
            os.rename(staged_path, move.destination)
            delivered.append((move, staged_path))

        for path in creates:
            _mkdir_recorded(path, created)
    except Exception as exc:
        rollback_errors = _rollback(staged, delivered, created, stage)
        detail = f"migration rolled back: {_error_category(exc)}"
        if rollback_errors:
            detail += "; rollback errors: " + "; ".join(rollback_errors)
        raise MigrationError(detail) from exc
    else:
        stage.rmdir()


def migrate(home: Path, *, yes: bool = False) -> dict[str, object]:
    """Return a JSON-compatible dry-run or apply result."""

    home = _resolved_home(home)
    moves, creates = _plan(home)
    result: dict[str, object] = {
        "home": str(home),
        "dry_run": not yes,
        "changed": bool(moves or creates),
        # None means undetermined; reported as such instead of collapsing into
        # True (unknown != same).
        "same_device": (
            all(_device(home.parent) == _device(move.source) for move in moves)
            if moves
            else True
        )
        if all(_device(home.parent) is not None for move in moves)
        else None,
        "moves": [
            {"from": str(move.source), "to": str(move.destination)} for move in moves
        ],
        "creates": [str(path) for path in creates],
    }
    if yes:
        apply(home, moves, creates)
        result["dry_run"] = False
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Migrate one explicit agent home.")
    parser.add_argument("home", type=Path, help="Throwaway or explicitly authorized agent-home path")
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Apply the migration; without this flag the command is a dry run",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        print(json.dumps(migrate(args.home, yes=args.yes), sort_keys=True))
    except MigrationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
