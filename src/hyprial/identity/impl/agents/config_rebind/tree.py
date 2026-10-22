"""Private configuration-tree validation and exact snapshot helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
from pathlib import Path

from hyprial.identity.impl.agents.home.config import (
    AgentConfig,
    build_native_projection,
)
from hyprial.kernel import ipc_errors


class ConfigRebindError(RuntimeError):
    """Stable operator-facing failure raised by the config rebind saga."""

    def __init__(
        self, code: str, message: str, *, data: dict[str, object] | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def validate_private_tree(source: Path) -> dict[str, object]:
    """Reject ambiguous, shared, linked, or secret-bearing staging trees."""

    source = source.expanduser()
    if not source.is_absolute():
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_INVALID_SOURCE,
            "staging source must be an absolute path",
        )
    try:
        root_stat = source.lstat()
    except FileNotFoundError as error:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_INVALID_SOURCE,
            f"staging directory does not exist: {source}",
        ) from error
    if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_INVALID_SOURCE,
            "staging source must be a real directory, not a symlink",
        )
    _validate_owner_mode(source, root_stat, directory=True)
    for root, dirs, files in os.walk(source, topdown=True, followlinks=False):
        root_path = Path(root)
        for name in sorted(dirs):
            path = root_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ConfigRebindError(
                    ipc_errors.AGENT_CONFIG_REBIND_SYMLINK,
                    f"configuration tree contains a linked directory: {path.relative_to(source)}",
                )
            _validate_owner_mode(path, info, directory=True)
        for name in sorted(files):
            path = root_path / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise ConfigRebindError(
                    ipc_errors.AGENT_CONFIG_REBIND_SYMLINK,
                    f"configuration tree contains a linked or special file: {path.relative_to(source)}",
                )
            _validate_owner_mode(path, info, directory=False)
            _reject_secret_like_content(path, source)
    # The existing authority owns harness mapping/root-file rules. Building all
    # native projections verifies the complete frozen map without duplicating it.
    try:
        manifest = AgentConfig(source=str(source)).freeze_manifest()
        projections = {
            harness: build_native_projection(manifest, harness)
            for harness in ("claude", "codex", "pi")
        }
    except Exception as error:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_INVALID_MAPPING,
            f"staging configuration mapping is invalid: {error}",
        ) from error
    return {
        "harnesses": sorted(
            harness for harness, projection in projections.items() if projection.items
        ),
        "manifestDigest": manifest.digest,
        "tree": tree_snapshot(source),
    }


def tree_snapshot(root: Path) -> dict[str, object]:
    root_info = root.lstat()
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_SYMLINK,
            "configuration tree root must be a real directory",
        )
    root_mode = stat.S_IMODE(root_info.st_mode)
    entries: list[dict[str, object]] = []
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        info = path.lstat()
        relative = path.relative_to(root).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": relative, "kind": "directory", "mode": mode})
        elif stat.S_ISREG(info.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": mode,
                    "size": info.st_size,
                    "sha256": digest,
                }
            )
        else:
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_SYMLINK,
                f"configuration tree contains a linked or special entry: {relative}",
            )
    canonical = json.dumps(
        {"rootMode": root_mode, "entries": entries},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return {
        "digest": hashlib.sha256(b"hyprial-config-tree-v1\0" + canonical).hexdigest(),
        "rootMode": root_mode,
        "entries": entries,
    }


def copy_tree_exact(source: Path, target: Path) -> None:
    if target.exists():
        raise FileExistsError(target)
    target.mkdir(mode=stat.S_IMODE(source.lstat().st_mode), parents=False)
    for path in sorted(
        source.rglob("*"), key=lambda item: item.relative_to(source).as_posix()
    ):
        relative = path.relative_to(source)
        info = path.lstat()
        destination = target / relative
        if stat.S_ISDIR(info.st_mode):
            destination.mkdir(mode=stat.S_IMODE(info.st_mode))
        elif stat.S_ISREG(info.st_mode):
            shutil.copyfile(path, destination, follow_symlinks=False)
            os.chmod(destination, stat.S_IMODE(info.st_mode), follow_symlinks=False)
        else:
            raise ConfigRebindError(
                ipc_errors.AGENT_CONFIG_REBIND_SYMLINK,
                f"configuration tree contains a linked or special entry: {relative}",
            )
    os.chmod(target, stat.S_IMODE(source.lstat().st_mode), follow_symlinks=False)
    _fsync_tree(target)


def same_tree(root: Path, expected: object) -> bool:
    if not root.is_dir() or not isinstance(expected, dict):
        return False
    try:
        return tree_snapshot(root) == expected
    except (OSError, ConfigRebindError):
        return False


def remove_tree(root: Path) -> None:
    if root.exists():
        shutil.rmtree(root)


def _validate_owner_mode(path: Path, info: os.stat_result, *, directory: bool) -> None:
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_OWNERSHIP,
            f"configuration entry is not owned by the daemon user: {path}",
        )
    expected = 0o700 if directory else 0o600
    actual = stat.S_IMODE(info.st_mode)
    if actual != expected:
        kind = "directory" if directory else "file"
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_MODE,
            f"private staging {kind} must have mode {expected:o}, got {actual:o}: {path}",
        )


def _reject_secret_like_content(path: Path, root: Path) -> None:
    name = path.name.casefold()
    suspicious_name = name in {
        ".env",
        ".netrc",
        "credentials",
        "credentials.json",
        "secrets.json",
    } or any(
        part in name
        for part in (
            "api_key",
            "apikey",
            "credential",
            "password",
            "private_key",
            "secret",
            "token",
        )
    )
    data = path.read_bytes()
    # Runtime config is textual. NUL-bearing payloads are not a supported
    # harness config and must not bypass the textual secret scanner.
    if b"\x00" in data:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_INVALID_SOURCE,
            f"configuration file is not text: {path.relative_to(root)}",
        )
    try:
        text = data.decode("utf-8").casefold()
    except UnicodeDecodeError as error:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_INVALID_SOURCE,
            f"configuration file is not UTF-8 text: {path.relative_to(root)}",
        ) from error
    assignment = re.compile(
        r"(?:^|[^a-z0-9])(?:api[_-]?key|(?:access[_-]?)?token|secret|password|"
        r"private[ _-]?key)[\"']?\s*(?:=|:)",
        re.MULTILINE,
    )
    private_key_block = re.search(
        r"-----begin (?:[a-z0-9]+ )*private key(?: block)?-----", text
    )
    if suspicious_name or assignment.search(text) is not None or private_key_block:
        raise ConfigRebindError(
            ipc_errors.AGENT_CONFIG_REBIND_SECRET_CONTENT,
            f"secret-like content is not allowed in staged configuration: {path.relative_to(root)}",
        )


def _fsync_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file():
            with path.open("rb") as handle:
                os.fsync(handle.fileno())
    for path in [
        *sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True),
        root,
    ]:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
