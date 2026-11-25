#!/usr/bin/env python3
# Decisions:
# - What packaging/clients/sign_bundle.py does with `<out>.unfinished` after a
#   failure, moved here (2026-11-19, PR #2980 review) to keep sign_bundle.py
#   under the 1000-line limit; the functions take the running Run and the
#   sign_bundle module, as signing/modes.py does.
# - An attached dmg under `work/mnt` is detached with -force first. When it
#   is still attached the directory is left in place (removing a tree under a
#   mount point is not something to do blindly), `cleanup: incomplete` is
#   recorded, and the signed copies are STILL marked: every .app and .zip is
#   renamed `-UNFINISHED` and FAILED.txt written, whatever --keep-unfinished
#   says, and the walk never enters the mount (it is read-only and is not
#   ours to rename). Otherwise the directory is removed and its absence
#   confirmed, or with --keep-unfinished marked the same way.
# - FAILED.txt states per app and dmg: notarized yes / no / unknown (a
#   submission exists that was not seen Accepted), stapled yes / "yes
#   (the staple's validation failed)" / no, from what the run recorded, so a ticket
#   that was stapled is never reported as missing.
"""Cleanup and marking of a failed Developer ID signing run's unfinished output."""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path
from typing import Any


def fail_cleanup(run: Any, sb: Any, reason: str) -> None:
    """Remove the unfinished directory and confirm it is gone, or mark it (always when a dmg stays attached)."""

    if not run.created or not run.unfinished.exists():
        return
    mount = run.unfinished / "work" / "mnt"
    if os.path.ismount(mount):
        with contextlib.suppress(sb.SignError):
            run.tools.run("hdiutil", "detach", "-force", str(mount), timeout=sb.DETACH_TIMEOUT)
    if os.path.ismount(mount):
        run.evidence["problems"].append(f"the dmg is still attached at {mount}; {run.unfinished} left in place, its signed copies marked")
        _mark_or_record(run, sb, reason, mount)
        run.evidence["cleanup"] = "incomplete"
    elif run.args.keep_unfinished:
        _mark_or_record(run, sb, reason, None)
    else:
        shutil.rmtree(run.unfinished, ignore_errors=True)
        run.evidence["cleanup"] = "incomplete" if os.path.lexists(run.unfinished) else "removed"
        if os.path.lexists(run.unfinished):
            run.evidence["problems"].append(f"{run.unfinished} left behind by the cleanup")


def _mark_or_record(run: Any, sb: Any, reason: str, mount: Path | None) -> None:
    try:
        mark_unfinished(run, sb, reason, mount)
    except OSError as error:
        run.evidence["problems"].append(f"marking {run.unfinished} failed: {error}")


def _stapled(run: Any, label: str) -> str:
    state = run.evidence.get("stapled", {}).get(label)
    return {"validated": "yes", "stapled": "yes (the staple's validation failed)"}.get(state, "no")


def mark_unfinished(run: Any, sb: Any, reason: str, mount: Path | None = None) -> None:
    """Delete the dmg, rename every signed copy -UNFINISHED (never inside ``mount``) and write FAILED.txt."""

    mark = sb.UNFINISHED_MARK
    for path in run.unfinished.glob("*.dmg"):
        path.unlink()
    for directory, names, files in os.walk(run.unfinished):
        names[:] = [name for name in names if mount is None or Path(directory, name) != mount]
        for name in [*names, *files]:
            if Path(name).suffix in {".app", ".zip"}:
                Path(directory, name).rename(Path(directory, f"{Path(name).stem}{mark}{Path(name).suffix}"))
        names[:] = [name for name in names if Path(name).suffix != ".app"]
    passed = {entry["step"] for entry in run.evidence["steps"] if entry["status"] == "PASS"}
    done = {step: "yes" if step in passed else "no" for step in ("notarize-app", "notarize-dmg")}
    for item in run.evidence["notary"]:
        if done[f"notarize-{item['label']}"] == "no" and item["id"] and item["status"] not in {"Invalid", "Rejected"}:
            done[f"notarize-{item['label']}"] = f"unknown (submission {item['id']}, status {item['status']}, may still be accepted)"
    submissions = ", ".join(f"{item['label']} {item['id'] or '-'} {item['status']}" for item in run.evidence["notary"]) or "none"
    text = (
        f"FAILED: {reason}\n"
        f"app notarized: {done['notarize-app']}\napp stapled: {_stapled(run, 'app')}\n"
        f"dmg notarized: {done['notarize-dmg']}\ndmg stapled: {_stapled(run, 'dmg')}\n"
        f"notary submissions: {submissions}\n"
        f"Every signed copy here is renamed *{mark} and the dmg is deleted. "
        "This directory is not a release; do not distribute anything from it.\n"
    )
    (run.unfinished / "FAILED.txt").write_text(sb.scrub(text), encoding="utf-8")
    run.evidence["cleanup"] = "kept"
