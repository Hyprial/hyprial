#!/usr/bin/env python3
# Decision record (KP-134 Windows runner probe, 2026-11-25):
# - Measures two things on the machine it runs on and prints one JSON object:
#   where the Pi SDK package root is found for the ``pi`` command on PATH, and
#   whether Node can produce random bytes under the environment a managed
#   worker inherits.
# - The package-root part calls the product function and, next to it, repeats
#   the ancestor-only walk the function used before the shim branch existed,
#   so one run shows whether the installed layout needs the shim branch.
# - The environment part builds the inherited base with the product's
#   ``whitelist_replacement_environment`` and never edits the frozen
#   vocabulary. It then adds the Windows system names one at a time, and all
#   together, to show which of them Node needs. This is the ambient filter
#   only; the per-agent home and generated names a real launch adds are not
#   reproduced here.
# - Exit code 1 when the package-root part does not complete (``pi`` missing,
#   the command file unreadable, or the product function finding no root).
#   The environment part is a measurement and never fails the run.
"""Probe the Pi SDK package root and the worker environment for KP-134.

Usage::

    python scripts/verify/windows_followup_probe.py

Requires ``pi`` and ``node`` on PATH and an importable ``hyprial``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any

SDK_PACKAGE = "@earendil-works/pi-coding-agent"
SYSTEM_NAMES = ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
NODE_RANDOM = (
    "const c=require('node:crypto');"
    "const b=new Uint8Array(8);"
    "globalThis.crypto.getRandomValues(b);"
    "c.randomFillSync(b);"
    "process.stdout.write(c.randomUUID());"
)
SHIM_EXCERPT_BYTES = 1500
OUTPUT_LIMIT = 1200
TIMEOUT_SECONDS = 120.0


def _manifest_names_sdk(directory: Path) -> bool:
    manifest = directory / "package.json"
    try:
        return json.loads(manifest.read_text(encoding="utf-8")).get("name") == SDK_PACKAGE
    except (OSError, ValueError, AttributeError):
        return False


def _guarded(probe: Any) -> dict[str, Any]:
    try:
        return probe()
    except Exception as error:
        return {
            "ok": False,
            "error": f"{type(error).__name__}: {error}",
            "traceback": traceback.format_exc()[-OUTPUT_LIMIT:],
        }


def probe_package_root() -> dict[str, Any]:
    from hyprial.daemon.impl.harnesses.pi.loader import find_pi_package_root

    located = shutil.which("pi")
    if located is None:
        return {"ok": False, "error": "pi is not on PATH"}
    resolved = Path(located).resolve(strict=True)
    ancestors = [
        str(parent)
        for parent in (resolved, *resolved.parents)
        if _manifest_names_sdk(parent)
    ]
    with resolved.open("rb") as handle:
        excerpt = handle.read(SHIM_EXCERPT_BYTES).decode("utf-8", errors="replace")
    root = find_pi_package_root(("pi",), dict(os.environ))
    return {
        "ok": True,
        "which": located,
        "resolved": str(resolved),
        "resolved_differs_from_which": str(resolved) != os.path.abspath(located),
        "ancestor_walk_alone_finds": ancestors,
        "product_function_finds": str(root),
        "siblings": sorted(entry.name for entry in resolved.parent.iterdir())[:40],
        "command_file_excerpt": excerpt,
    }


def _run(arguments: list[str], environment: dict[str, str]) -> dict[str, Any]:
    try:
        finished = subprocess.run(
            arguments,
            env=environment,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"started": False, "error": f"{type(error).__name__}: {error}"}
    return {
        "started": True,
        "returncode": finished.returncode,
        "stdout": finished.stdout.decode("utf-8", errors="replace")[-OUTPUT_LIMIT:],
        "stderr": finished.stderr.decode("utf-8", errors="replace")[-OUTPUT_LIMIT:],
    }


def _pi_cli(root: Path) -> Path | None:
    try:
        declared = json.loads((root / "package.json").read_text(encoding="utf-8")).get("bin")
    except (OSError, ValueError):
        return None
    if isinstance(declared, dict):
        declared = declared.get("pi") or next(iter(declared.values()), None)
    return root / declared if isinstance(declared, str) else None


def probe_environment(package_root: str | None) -> dict[str, Any]:
    from hyprial.identity.impl.agents.home.environment import (
        whitelist_replacement_environment,
    )

    node = shutil.which("node")
    if node is None:
        return {"ok": False, "error": "node is not on PATH"}
    ambient = dict(os.environ)
    filtered = whitelist_replacement_environment(ambient)
    present = [name for name in SYSTEM_NAMES if name in os.environ]
    variants: dict[str, dict[str, str]] = {"ambient": ambient, "filtered": filtered}
    for name in present:
        variants[f"filtered+{name}"] = {**filtered, name: os.environ[name]}
    variants["filtered+all"] = {**filtered, **{name: os.environ[name] for name in present}}
    cli = _pi_cli(Path(package_root)) if package_root else None
    results: dict[str, Any] = {}
    for label, environment in variants.items():
        entry = {"node_random": _run([node, "-e", NODE_RANDOM], environment)}
        if cli is not None:
            entry["pi_version"] = _run([node, str(cli), "--version"], environment)
        results[label] = entry
    return {
        "ok": True,
        "node": node,
        "pi_cli": None if cli is None else str(cli),
        "filtered_names": sorted(filtered),
        "system_names_present": present,
        "variants": results,
    }


def main() -> int:
    package_root = _guarded(probe_package_root)
    environment = _guarded(lambda: probe_environment(package_root.get("product_function_finds")))
    report = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "package_root": package_root,
        "environment": environment,
    }
    sys.stdout.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return 0 if package_root.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
