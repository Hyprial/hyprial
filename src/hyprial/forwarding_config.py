"""Build non-secret daemon environment for the candidate forwarding sidecar."""

from __future__ import annotations

import ipaddress
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from hyprial.contracts.forwarding import (
    DEFAULT_PEER_PORT,
    FORWARDING_COMMAND_ENV,
    FORWARDING_UP_ENV,
)
from hyprial.network_profile import TSNET_STATE_DIRNAME, resolve_profile

FORWARDING_SIDECAR_ENV = "HYPRIAL_FORWARDING_SIDECAR"
FORWARDING_TARGET_ENV = "HYPRIAL_FORWARDING_INBOUND_TARGET"
# The operator policy switch.  Deliberately NOT ``HYPRIAL_FORWARDING_*``:
# the isolated daemon refuses any variable with that prefix, and a policy
# of ``off`` must be settable on an isolated daemon without tripping it.
FORWARDING_MODE_ENV = "HYPRIAL_FORWARDING"
FORWARDING_MODES = ("off", "auto", "on")
# What an absent switch means: automatic forwarding for a sidecar-joined
# home (Allen, 2026-09-24: the sidecar is the main and future only
# channel).  An unjoined home is unaffected -- nothing joins or downloads.
FORWARDING_DEFAULT_MODE = "auto"
# The durable operator setting in ``$HYPRIAL_HOME/settings.json``:
# ``{"forwarding": {"mode": "off"|"auto"|"on"}}``.  The environment wins.
FORWARDING_SETTINGS_KEY = "forwarding"

class ForwardingConfigurationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _loopback_target(raw: str) -> str:
    parsed = urlsplit(f"//{raw}")
    try:
        host = parsed.hostname
        port = parsed.port
        address = ipaddress.ip_address(host or "")
    except ValueError as error:
        raise ForwardingConfigurationError(
            "FORWARDING_TARGET_INVALID",
            f"forwarding inbound target must be an explicit loopback host:port; got {raw!r}",
        ) from error
    if not address.is_loopback or port is None or port <= 0:
        raise ForwardingConfigurationError(
            "FORWARDING_TARGET_INVALID",
            f"forwarding inbound target must be an explicit loopback host:port; got {raw!r}",
        )
    return raw


def daemon_forwarding_environment(
    hyprial_home: Path,
    environ: Mapping[str, str],
    *,
    node_id: str,
) -> dict[str, str]:
    """Return v2 sidecar launch variables, or none when not configured.

    The candidate path is explicit because publishing binaries and updating
    hyprial's 0.1.3 pin are a separate reviewed unit. No released binary is used
    as an implicit fallback.
    """

    raw_binary = environ.get(FORWARDING_SIDECAR_ENV, "").strip()
    raw_target = environ.get(FORWARDING_TARGET_ENV, "").strip()
    if not raw_binary and not raw_target:
        return {}
    if not raw_binary:
        raise ForwardingConfigurationError(
            "FORWARDING_SIDECAR_MISSING",
            f"{FORWARDING_SIDECAR_ENV} is required when forwarding is enabled",
        )
    binary = Path(raw_binary).expanduser()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ForwardingConfigurationError(
            "FORWARDING_SIDECAR_MISSING",
            f"candidate forwarding sidecar is not an executable file: {binary}",
        )
    if not raw_target:
        raise ForwardingConfigurationError(
            "FORWARDING_LISTEN_MISSING",
            f"{FORWARDING_TARGET_ENV} must name the fixture's explicit listener",
        )
    target = _loopback_target(raw_target)
    expected_listen = f"tcp/{target}"
    configured_listen = {
        item.strip()
        for item in environ.get("HYPRIAL_ZENOH_LISTEN", "").split(",")
        if item.strip()
    }
    if expected_listen not in configured_listen:
        raise ForwardingConfigurationError(
            "FORWARDING_LISTEN_MISSING",
            f"forwarding inbound target {target} has no matching explicit "
            "HYPRIAL_ZENOH_LISTEN endpoint",
        )

    home = Path(hyprial_home)
    state_dir = home / TSNET_STATE_DIRNAME
    if not (state_dir / "node").is_dir():
        raise ForwardingConfigurationError(
            "FORWARDING_STATE_MISSING",
            f"candidate forwarding sidecar has no joined node state under {state_dir}",
        )
    try:
        profile, _source = resolve_profile(environ, hyprial_home=home)
    except ValueError as error:
        raise ForwardingConfigurationError(
            "FORWARDING_PROFILE_INVALID", str(error)
        ) from error

    up = {
        "v": 2,
        "op": "up",
        "controlUrl": (
            ""
            if profile.control_plane_kind == "tailscale"
            else profile.control_plane_url
        ),
        "hostname": node_id.split(".", 1)[0],
        "dir": str(state_dir),
        "ephemeral": False,
        "join": profile.join,
        "authKey": None,
        "resume": True,
        "inboundTarget": target,
        "peerPort": DEFAULT_PEER_PORT,
    }
    return {
        FORWARDING_COMMAND_ENV: json.dumps(
            [str(binary), "forward"], separators=(",", ":")
        ),
        FORWARDING_UP_ENV: json.dumps(up, separators=(",", ":")),
    }


@dataclass(frozen=True, slots=True)
class ForwardingPolicy:
    """The effective forwarding switch and where it came from."""

    mode: str
    source: str

    def to_json(self) -> dict[str, str]:
        return {"mode": self.mode, "source": self.source}


def forwarding_policy(
    environ: Mapping[str, str], hyprial_home: Path | None = None
) -> ForwardingPolicy:
    """``HYPRIAL_FORWARDING=off|auto|on``, else ``settings.json``, else the default.

    ``off`` suppresses every forwarding source, including explicit and
    generated variables, so it is a durable rollback. ``auto`` enables
    forwarding for a sidecar-joined home with a verified sidecar and stays
    off (with a visible reason) otherwise; ``on`` is the same but refuses to
    start when the home is not eligible. An unknown value is an error.
    """

    raw = environ.get(FORWARDING_MODE_ENV, "").strip().lower()
    if raw:
        return ForwardingPolicy(_checked_mode(raw, FORWARDING_MODE_ENV), "env")
    if hyprial_home is not None:
        stored = _stored_mode(Path(hyprial_home) / "settings.json")
        if stored is not None:
            return ForwardingPolicy(stored, "settings")
    return ForwardingPolicy(FORWARDING_DEFAULT_MODE, "default")


def _checked_mode(raw: str, where: str) -> str:
    if raw not in FORWARDING_MODES:
        raise ForwardingConfigurationError(
            "FORWARDING_MODE_INVALID",
            f"{where} must be one of {', '.join(FORWARDING_MODES)}; got {raw!r}",
        )
    return raw


def _stored_mode(path: Path) -> str | None:
    """``forwarding.mode`` from settings.json, or None when unset.

    A settings file that exists but does not parse is an error rather than
    "unset": silently falling back to ``auto`` would undo an operator's
    ``off`` precisely when their file is damaged.
    """

    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ForwardingConfigurationError(
            "FORWARDING_SETTINGS_INVALID", f"cannot parse {path}: {error}"
        ) from error
    section = record.get(FORWARDING_SETTINGS_KEY) if isinstance(record, dict) else None
    if section is None:
        return None
    mode = section.get("mode") if isinstance(section, dict) else None
    if not isinstance(mode, str):
        raise ForwardingConfigurationError(
            "FORWARDING_SETTINGS_INVALID",
            f"{path}: {FORWARDING_SETTINGS_KEY}.mode must be one of "
            f"{', '.join(FORWARDING_MODES)}",
        )
    return _checked_mode(mode.strip().lower(), f"{path}: {FORWARDING_SETTINGS_KEY}.mode")


def write_forwarding_mode(mode: str, hyprial_home: Path) -> Path:
    """Persist ``forwarding.mode`` and return the file written.

    Read-modify-write like ``updates.write_auto_upgrade``: every unrelated
    key survives, an unparseable file is refused rather than clobbered, and
    the file is replaced atomically. Takes effect on the next daemon start.
    """

    checked = _checked_mode(mode.strip().lower(), f"{FORWARDING_SETTINGS_KEY}.mode")
    path = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        record = {}
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    section = record.get(FORWARDING_SETTINGS_KEY)
    section = dict(section) if isinstance(section, dict) else {}
    section["mode"] = checked
    record[FORWARDING_SETTINGS_KEY] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staging.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    os.replace(staging, path)
    return path


@dataclass(frozen=True, slots=True)
class AutomaticForwarding:
    """An eligible home: everything but the inbound port, which is only
    certain once this daemon's own listener has bound it."""

    hyprial_home: Path
    binary: Path
    node_id: str

    def environment(self, target: str) -> dict[str, str]:
        return daemon_forwarding_environment(
            self.hyprial_home,
            {
                FORWARDING_SIDECAR_ENV: str(self.binary),
                FORWARDING_TARGET_ENV: target,
                "HYPRIAL_ZENOH_LISTEN": f"tcp/{target}",
            },
            node_id=self.node_id,
        )


def automatic_forwarding(
    hyprial_home: Path, environ: Mapping[str, str], *, node_id: str
) -> tuple[AutomaticForwarding | None, str | None]:
    """``(plan, None)`` for a sidecar-joined home, else ``(None, reason)``.

    Eligible means: the sidecar has joined (``state/tsnet/node/`` plus its
    ``node.json`` record), the network profile resolves, and the installed
    or bundled sidecar matches its pin. Either control-plane kind qualifies
    (Allen, 2026-09-24: the sidecar is the main and future only channel).
    Nothing here joins, downloads or starts anything.
    """

    home = Path(hyprial_home)
    state_dir = home / TSNET_STATE_DIRNAME
    record = state_dir / "node.json"
    if not (state_dir / "node").is_dir() or not record.is_file():
        return None, "NOT_JOINED"
    try:
        json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "NOT_JOINED"
    try:
        resolve_profile(environ, hyprial_home=home)
    except ValueError:
        return None, "PROFILE_INVALID"
    # Imported here: the CLI imports this module at startup and the sidecar
    # module is not part of that import boundary.
    from hyprial.tsnet_sidecar import verify_installed_sidecar

    binary, reason = verify_installed_sidecar(home)
    if binary is None:
        return None, reason or "SIDECAR_MISSING"
    return AutomaticForwarding(home, binary, node_id), None
