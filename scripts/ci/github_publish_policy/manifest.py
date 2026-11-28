"""Snapshot-manifest parsing and the publish purity gate.

The command module supplies its exception type so its public constants
and monkeypatch seams remain authoritative (same split as ``intranet``).
"""

from __future__ import annotations

from collections.abc import Iterable
import fnmatch
from pathlib import Path


def excluded_entries(
    paths: Iterable[str],
    excludes: Iterable[str],
    globs: Iterable[str],
) -> tuple[str, ...]:
    """The ``paths`` an exclusion set forbids, sorted; the fail-closed guard.

    Pure and separately tested: the publish path must be able to answer "did
    anything forbidden land on disk" without running git at all, because the
    whole point is to not trust the archive step.  The glob set uses the same
    fnmatch wildcard semantics as the archive pathspec, so the guard and the
    mechanism agree (2026-11-03 structural-limit move from the command
    module, where the denylist and exclusion globs stay).
    """
    return tuple(
        sorted(
            rel
            for rel in paths
            if any(
                rel == entry.rstrip("/") or rel.startswith(entry.rstrip("/") + "/")
                for entry in excludes
            )
            or any(
                fnmatch.fnmatchcase(rel, pattern)
                for pattern in globs
            )
        )
    )


def parse_snapshot_manifest(text: str, error_type) -> tuple[str, ...]:
    """The scanned snapshot's path list, from the teed snapshot output.

    :func:`print_manifest` writes one ``snapshot-file\t<path>\t<size>`` line
    per file; the workflow tees that output to a file and hands it to the
    publish step, which treats it as the record of what the scans actually
    saw.  Pure and separately tested; fail-closed on any other shape.
    """
    paths: list[str] = []
    for line in text.splitlines():
        if not line.startswith("snapshot-file\t"):
            continue
        parts = line.split("\t")
        if len(parts) != 3 or not parts[1]:
            raise error_type(f"malformed snapshot manifest line: {line!r}")
        paths.append(parts[1])
    if not paths:
        raise error_type("snapshot manifest has no snapshot-file lines")
    duplicates = sorted({p for p in paths if paths.count(p) > 1})
    if duplicates:
        raise error_type(
            "snapshot manifest lists duplicate paths: " + ", ".join(duplicates)
        )
    return tuple(paths)


def read_scanned_manifest(manifest: Path, error_type) -> tuple[str, ...]:
    """Read and parse the scanned manifest file; unreadable is a refusal."""
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise error_type(
            f"scanned snapshot manifest unreadable ({manifest}): "
            f"{exc.__class__.__name__}"
        ) from exc
    return parse_snapshot_manifest(text, error_type)


def summarise_paths(paths: list[str]) -> str:
    """A bounded, path-naming summary for gate failures."""
    head = ", ".join(paths[:10])
    return head + (f" … and {len(paths) - 10} more" if len(paths) > 10 else "")


def staged_purity_error(
    staged_paths: set[str], scanned_paths: Iterable[str]
) -> str | None:
    """The purity gate as a pure function: the refusal message, or ``None``.

    ``None`` means the staged path set equals the scanned snapshot manifest
    exactly.  Otherwise the message names BOTH divergences (extra paths no
    gate ever scanned, missing paths the scans saw but the staged tree lost),
    each bounded by :func:`summarise_paths` -- byte-identical to the inline
    check this replaced (2026-10-08 structural-limit split, pure move).
    """
    extra = sorted(staged_paths - set(scanned_paths))
    missing = sorted(set(scanned_paths) - staged_paths)
    if not extra and not missing:
        return None
    return (
        "purity gate: staged tree diverges from the scanned snapshot "
        "manifest -- "
        + "; ".join(
            detail
            for detail in (
                f"extra (never scanned): {summarise_paths(extra)}"
                if extra else "",
                f"missing (scanned, absent from the staged tree): "
                f"{summarise_paths(missing)}"
                if missing else "",
            )
            if detail
        )
        + "; refusing to publish, nothing was pushed"
    )
