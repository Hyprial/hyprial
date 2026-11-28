"""The publish-side GUI provenance stamp for checkout-free wheel builds.

Decisions (2026-11-03, infra-ops/release-0.5.5.3.2, brief ``publish-gui``):

* The public snapshot is a ``git archive`` extraction and has no ``.git``, so
  ``gui_build_hook`` cannot read tracked-input, clean-source, or HEAD facts
  from git (run 44202, v0.5.5.3.1: the installability gate died on the
  missing ``gui/package.json``; with the tree whitelisted the next failure
  would have been the git checks).  Mirroring the retired ``src/gui``
  ``stamp_snapshot`` mechanism, the snapshot step writes
  ``gui/gui-source.json``: schema ``hyprial.gui-snapshot/v1``, the pinned
  40-hex internal commit, the publish target remote, and a sha256 for every
  file shipped under ``gui/``.
* The stamp is written into the snapshot BEFORE the intranet and gitleaks
  gates read it, so it is scanned content listed in the manifest, and the
  publish step's pristine re-extraction reproduces it byte-for-byte (a pure
  function of the pinned revision) -- keeping the 2026-10-08 purity gate
  exact.
* The remote is the publish target remote (``DEFAULT_TARGET_REMOTE`` in
  ``scripts/ci/github_publish.py``), never the builder's
  ``remote.origin.url``: that origin is the builder's intranet Git host,
  which would trip the intranet-identifier
  gate in the snapshot and leak infrastructure naming into the wheel's
  ``release.json``, which no publish gate scans.
* The retired ``src/gui`` stamp (``hyprial.gui-source/v1`` at
  ``src/hyprial/gui-source.json``) also recorded the commit timestamp and
  executable bits; no current consumer reads either, so this schema keeps
  only what ``gui_build_hook`` validates and uses.
* The wheel build re-verifies every stamped hash against the on-disk tree
  and refuses files the stamp does not cover, so GUI inputs cannot drift
  between the scanned snapshot and the wheel build.

This module is stdlib-only and pure (no subprocess): the caller in
``github_publish.py`` resolves the commit id, and CI may run this under the
container's system python3 before any uv environment exists.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

STAMP_SCHEMA = "hyprial.gui-snapshot/v1"
STAMP_RELATIVE_PATH = "gui/gui-source.json"
STAMP_FILE_NAME = "gui-source.json"
GUI_DIR_NAME = "gui"
_GENERATED_GUI_DIRS = frozenset({"node_modules", "dist"})
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def write_gui_stamp(
    snapshot_root: Path,
    *,
    commit: str,
    remote: str,
    error: type[Exception],
) -> Path:
    """Stamp ``snapshot_root/gui`` with its provenance; return the stamp path.

    Fail-closed on every invalid input: a snapshot whose GUI tree is absent,
    empty, or symlink-bearing, or whose provenance fields are malformed, is
    refused here rather than allowed to produce a wheel of unknown origin.
    The stamp lists only the first-party inputs the wheel build reads:
    generated ``node_modules``/``dist`` trees are not snapshot content.
    """
    if not _COMMIT_RE.fullmatch(commit):
        raise error(
            f"GUI stamp commit must be a full 40-hex object id: {commit!r}"
        )
    if not remote.strip():
        raise error("GUI stamp remote must be a non-empty string")
    gui = snapshot_root / GUI_DIR_NAME
    if not gui.is_dir():
        raise error(
            f"the snapshot has no {GUI_DIR_NAME}/ tree to stamp: {gui}"
        )
    files: list[dict[str, str]] = []
    for path in sorted(gui.rglob("*")):
        relative = path.relative_to(gui).as_posix()
        if path.is_symlink():
            raise error(
                f"GUI snapshots must contain only regular inputs: {relative}"
            )
        if not path.is_file():
            continue
        if relative == STAMP_FILE_NAME:
            continue
        if relative.split("/")[0] in _GENERATED_GUI_DIRS:
            continue
        files.append(
            {
                "path": relative,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        )
    if not files:
        raise error(
            f"the snapshot {GUI_DIR_NAME}/ tree is empty; refusing to stamp"
        )
    payload = {
        "schema": STAMP_SCHEMA,
        "commit": commit,
        "remote": remote,
        "files": files,
    }
    stamp_path = snapshot_root / STAMP_RELATIVE_PATH
    stamp_path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return stamp_path
