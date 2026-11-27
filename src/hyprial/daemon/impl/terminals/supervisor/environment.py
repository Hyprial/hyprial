"""Private hc environment and filesystem preparation."""

from __future__ import annotations

import os
from pathlib import Path
from collections.abc import Mapping
from urllib.parse import urlsplit

from hyprial.kernel import hc_core_root
from hyprial.kernel import hc_core_socket_path
from hyprial.kernel import hc_session_name
from hyprial.kernel import local_ipc_path_limit
from hyprial.kernel import native_profile_environment
from hyprial.kernel import posix_runtime_dir_environment
from hyprial.kernel import secure_private_directory
from hyprial.kernel import write_private_file_atomic


class HcSocketPathTooLongError(ValueError):
    """Raised when hc or its holder socket cannot fit the local IPC limit."""


_BASE_KEYS = frozenset({"HOME", "PATH", "USER", "LOGNAME", "TMPDIR", "TEMP", "TMP"})
_SAFE_PASSTHROUGH = frozenset(
    {"MAGPIE_URL", "MAGPIE_BASE_URL", "MAGPIE_GATEWAY_URL", "NO_PROXY", "no_proxy"}
)


def build_private_hc_environment(
    home: Path,
    source: Mapping[str, str] | None = None,
    *,
    passthrough: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the secret-free environment used by the private hc core."""
    resolved_home = Path(home).resolve()
    host = dict(os.environ if source is None else source)
    core_root = hc_core_root(resolved_home)
    private: dict[str, str] = {
        key: value
        for key, value in host.items()
        if key in _BASE_KEYS or key == "LANG" or key.startswith("LC_")
    }
    private.update(posix_runtime_dir_environment(host))
    native, path_parts = native_profile_environment(resolved_home, host)
    private.update(native)
    if path_parts and "PATH" not in host:
        private["PATH"] = os.pathsep.join(path_parts)
    if passthrough is not None:
        for key, value in passthrough.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("hc passthrough values must be strings")
            if key not in _SAFE_PASSTHROUGH or not _safe_passthrough_value(key, value):
                raise ValueError(f"unsafe hc passthrough key or value: {key}")
            private[key] = value
    private.update(
        {
            "HC_SESSION": hc_session_name(resolved_home),
            "HC_SOCKET": str(hc_core_socket_path(resolved_home)),
            "XDG_STATE_HOME": str(core_root / "state"),
            "XDG_CONFIG_HOME": str(core_root / "config"),
            "XDG_DATA_HOME": str(core_root / "data"),
        }
    )
    validate_hc_socket_paths(resolved_home)
    return private


def _safe_passthrough_value(key: str, value: str) -> bool:
    if any(character in value for character in "\r\n"):
        return False
    if key in {"NO_PROXY", "no_proxy"}:
        return "@" not in value and "://" not in value and "?" not in value and "#" not in value
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname is not None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def validate_hc_socket_paths(home: Path, path_limit: int | None = None) -> None:
    """Validate the core socket and the longer holder socket before launch."""
    limit = local_ipc_path_limit() if path_limit is None else path_limit
    if limit is None:
        return
    socket = hc_core_socket_path(Path(home).resolve())
    holder = socket.with_name("hc.holder.deadbeef.sock")
    if len(os.fsencode(holder)) > limit:
        raise HcSocketPathTooLongError(
            f"hc holder socket path is {len(os.fsencode(holder))} bytes, limit is {limit}"
        )


def write_private_hc_config(home: Path) -> Path:
    """Write the private core settings and return the config path."""
    config_dir = secure_private_directory(
        hc_core_root(Path(home).resolve()) / "config" / "herdr-core",
        create=True,
        mode=0o700,
    )
    path = config_dir / "config.toml"
    write_private_file_atomic(
        path,
        b"[notes]\nenabled = false\n[notifications]\nenabled = false\n",
    )
    return path


__all__ = [
    "HcSocketPathTooLongError",
    "build_private_hc_environment",
    "validate_hc_socket_paths",
    "write_private_hc_config",
]
