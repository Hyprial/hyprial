"""Step 1: rewrite persisted launcher argv from the retired mcp command."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

from . import UpgradeContext, UpgradeStep


LEGACY_PREFIX = ["-m", "hyprial.cli", "mcp"]
NEW_PREFIX = ["-m", "hyprial.daemon.impl.mcp.entry"]
CONFIG_GLOB = "claude-channel-*.json"


def _rewrite_args(value: Any) -> tuple[Any, int]:
    if not isinstance(value, list):
        return value, 0
    if len(value) >= len(LEGACY_PREFIX) and value[: len(LEGACY_PREFIX)] == LEGACY_PREFIX:
        return [*NEW_PREFIX, *value[len(LEGACY_PREFIX) :]], 1
    return value, 0


def _rewrite(value: Any) -> tuple[Any, int]:
    """Rewrite only the launcher's known argv locations, never arbitrary arrays."""

    if not isinstance(value, dict):
        return value, 0
    changed = 0
    servers = value.get("mcpServers")
    if isinstance(servers, dict):
        rewritten_servers: dict[str, Any] = {}
        for name, server in servers.items():
            if isinstance(server, dict) and "args" in server:
                args, count = _rewrite_args(server.get("args"))
                server = {**server, "args": args}
                changed += count
            rewritten_servers[str(name)] = server
        value = {**value, "mcpServers": rewritten_servers}
    hooks = value.get("hooks")
    if isinstance(hooks, dict):
        rewritten_hooks: dict[str, Any] = {}
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                rewritten_hooks[str(event)] = groups
                continue
            new_groups = []
            for group in groups:
                if not isinstance(group, dict):
                    new_groups.append(group)
                    continue
                hook_rows = group.get("hooks")
                if not isinstance(hook_rows, list):
                    new_groups.append(group)
                    continue
                new_rows = []
                for hook in hook_rows:
                    if isinstance(hook, dict) and "args" in hook:
                        args, count = _rewrite_args(hook.get("args"))
                        hook = {**hook, "args": args}
                        changed += count
                    new_rows.append(hook)
                new_groups.append({**group, "hooks": new_rows})
            rewritten_hooks[str(event)] = new_groups
        value = {**value, "hooks": rewritten_hooks}
    return value, changed


def _atomic_write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    try:
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def scan_legacy_files(state_dir: Path) -> list[str]:
    """Read-only list of files still holding the exact old argv."""

    found: list[str] = []
    for path in sorted(Path(state_dir).glob(CONFIG_GLOB)):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            _rewritten, count = _rewrite(value)
        except (OSError, ValueError, TypeError):
            continue
        if count:
            found.append(str(path))
    return found


def migrate_persisted_argv(state_dir: Path) -> dict[str, Any]:
    """Rewrite only the exact legacy prefix in launcher JSON files."""

    rewritten: list[str] = []
    failed: list[dict[str, str]] = []
    for path in sorted(Path(state_dir).glob(CONFIG_GLOB)):
        if path.is_symlink() or not path.is_file():
            failed.append({"path": str(path), "error": "not a regular file"})
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            rewritten_value, count = _rewrite(value)
            if not count:
                continue
            payload = (
                json.dumps(rewritten_value, ensure_ascii=False, indent=2) + "\n"
            ).encode("utf-8")
            _atomic_write(path, payload)
            rewritten.append(str(path))
        except (OSError, ValueError, TypeError) as error:
            failed.append({"path": str(path), "error": f"{type(error).__name__}: {error}"})
    return {"rewritten": rewritten, "failed": failed}


def _parse_process(line: str) -> dict[str, Any] | None:
    parts = line.strip().split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    pid = int(parts[0])
    try:
        tokens = shlex.split(parts[1])
    except ValueError:
        return None
    for index in range(len(tokens) - 2):
        if tokens[index] == "hyprial.cli" and tokens[index + 1] == "mcp":
            entry = tokens[index + 2]
            row: dict[str, Any] = {"pid": pid, "entry": entry}
            for flag, key in (("--actor", "actor"), ("--session-ref", "sessionRef")):
                try:
                    row[key] = tokens[tokens.index(flag) + 1]
                except (ValueError, IndexError):
                    pass
            return row
    return None


def legacy_processes(
    *, process_runner: Callable[..., Any] | None = None
) -> list[dict[str, Any]]:
    """Read-only scan; never signals a process and never prints its full argv."""

    runner = process_runner or subprocess.run
    try:
        result = runner(
            ("ps", "-axo", "pid=,command="),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    rows: list[dict[str, Any]] = []
    for line in result.stdout.splitlines():
        row = _parse_process(line)
        if row is not None:
            rows.append(row)
    return rows


def _run(context: UpgradeContext) -> dict[str, Any]:
    return migrate_persisted_argv(context.state_dir)


STEP = UpgradeStep("mcp-entry-v1", _run, repeatable=True)

__all__ = [
    "CONFIG_GLOB",
    "LEGACY_PREFIX",
    "NEW_PREFIX",
    "STEP",
    "legacy_processes",
    "migrate_persisted_argv",
    "scan_legacy_files",
]
