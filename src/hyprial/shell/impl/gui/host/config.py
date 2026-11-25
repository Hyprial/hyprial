# Decision record (GUI session host, PR 2b):
# - The host reads exactly the HYPRIAL_GUI_* variables the Node host read
#   (gui/server/daemon-request/config.ts, deleted in PR 3). Names, ranges and error
#   wording are kept so the launcher, the conformance suite and the redaction
#   set need no change.
# - A missing driver is an error here: the Node host stayed unmounted without
#   it, a Python child that was started on purpose must refuse instead.
# - An absent HYPRIAL_GUI_PRINCIPAL is the anonymous bootstrap; a present but
#   blank or malformed one is an error and is never downgraded to bootstrap.
# - Decision host-serves-daemon-in-process-20261119 (2822, B3): the daemon driver
#   of a host started with --home/--state-dir/--socket/--node has no upstream
#   endpoint or bearer in its environment (read_local_config refuses them).
#   Its authority is a callable, the in-process holder of the embedded server's
#   published bearer and principal: each read returns one immutable HostConfig,
#   a new object invalidates the business sessions before it is returned, and a
#   callable that raises fails closed with AuthorityUnavailable.
# - Decision drop-file-authority-20261120 (owner-approved): the file-based
#   authority (a private frontend-authority.json named by an environment
#   variable) is gone with its reader.  No launcher wrote or set it after the
#   fold; the endpoint-and-token configuration (contract-mock, the TypeScript
#   conformance host) has one authority for the life of the host.
# - Secrets and the launch grant come only from the environment. Nothing here
#   returns them to a browser, embeds them in a URL or writes them to a log.
"""Environment to frozen host configuration."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

DRIVERS = ("daemon", "contract-mock")
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_LAUNCH_TTL_MS = 60_000
DEFAULT_SESSION_TTL_MS = 12 * 60 * 60_000
DEFAULT_MAX_SESSIONS = 32
MIN_DURATION_MS = 1_000
MAX_DURATION_MS = 86_400_000
UPSTREAM_REQUEST_SECONDS = 15.0
IN_PROCESS_ENDPOINT = "http://127.0.0.1:1"

_PRINCIPAL = re.compile(r"^user:([^:\s]+)$")
_NODE = re.compile(r"^[^:\s]+$")


class ConfigError(ValueError):
    """The launcher environment does not describe a trusted host."""


@dataclass(frozen=True)
class Principal:
    """A verified serving account; anonymous bootstrap is ``None`` instead."""

    actor: str
    owner: str
    node: str


@dataclass(frozen=True)
class HostConfig:
    """Everything the host may know about its upstream and its launch."""

    driver: str
    principal: Principal | None
    endpoint: str
    upstream_token: str
    launch_grant: str
    handoff_path: str | None
    publication_path: str | None
    launch_ttl_ms: int
    session_ttl_ms: int
    max_sessions: int
    upstream_request_seconds: float = UPSTREAM_REQUEST_SECONDS


def is_loopback_name(hostname: str) -> bool:
    """True for the exact loopback names the session contract allows."""
    return hostname.strip("[]").lower() in LOOPBACK_NAMES


def parse_endpoint(raw: str | None) -> str:
    """Return the upstream origin; the browser never chooses it."""
    if raw is None or not raw.strip():
        raise ConfigError("HYPRIAL_BACKEND_ENDPOINT is required for the GUI backend")
    message = "HYPRIAL_BACKEND_ENDPOINT must be http://<loopback-host>:<port>"
    try:
        value = urlsplit(raw.strip())
        port = value.port
        hostname = value.hostname or ""
    except ValueError as error:
        raise ConfigError(message) from error
    if (
        value.scheme != "http"
        or not is_loopback_name(hostname)
        or not port
        or value.username is not None
        or value.password is not None
        or value.path not in ("", "/")
        or value.query
        or value.fragment
        or "?" in raw
        or "#" in raw
    ):
        raise ConfigError(message)
    shown = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{shown}:{port}"


def parse_principal(raw: str | None, raw_node: str | None) -> Principal | None:
    """Absent means anonymous bootstrap; anything else must be canonical."""
    if raw is None:
        return None
    actor = raw.strip()
    match = _PRINCIPAL.match(actor)
    if match is None:
        raise ConfigError("HYPRIAL_GUI_PRINCIPAL must be the canonical user:<owner> URI")
    node = (raw_node or "").strip()
    if _NODE.match(node) is None:
        raise ConfigError("HYPRIAL_NODE_ID must be the verified daemon node")
    return Principal(actor=actor, owner=match.group(1), node=node)


def _integer(raw: str | None, default: int, low: int, high: int, name: str) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        value = low - 1
    if not low <= value <= high:
        raise ConfigError(f"{name} must be an integer between {low} and {high}")
    return value


def _launch_path(raw: str | None, name: str, file: str) -> str | None:
    if raw is None or not raw.strip():
        return None
    if not os.path.isabs(raw) or os.path.basename(raw) != file:
        raise ConfigError(f"{name} must be an absolute {file} path")
    return os.path.abspath(raw)


def _launch_fields(env: Mapping[str, str], grant: str) -> dict[str, Any]:
    return {
        "launch_grant": grant,
        "handoff_path": _launch_path(env.get("HYPRIAL_GUI_HANDOFF_PATH"), "HYPRIAL_GUI_HANDOFF_PATH", "launch.url"),
        "publication_path": _launch_path(
            env.get("HYPRIAL_GUI_PUBLICATION_PATH"), "HYPRIAL_GUI_PUBLICATION_PATH", "launch.ready"
        ),
        "launch_ttl_ms": _integer(
            env.get("HYPRIAL_GUI_LAUNCH_TTL_MS"), DEFAULT_LAUNCH_TTL_MS, MIN_DURATION_MS, MAX_DURATION_MS,
            "HYPRIAL_GUI_LAUNCH_TTL_MS",
        ),
        "session_ttl_ms": _integer(
            env.get("HYPRIAL_GUI_SESSION_TTL_MS"), DEFAULT_SESSION_TTL_MS, MIN_DURATION_MS, MAX_DURATION_MS,
            "HYPRIAL_GUI_SESSION_TTL_MS",
        ),
        "max_sessions": _integer(
            env.get("HYPRIAL_GUI_MAX_SESSIONS"), DEFAULT_MAX_SESSIONS, 1, 1_024, "HYPRIAL_GUI_MAX_SESSIONS"
        ),
    }


def _launch_grant(env: Mapping[str, str]) -> str:
    grant = (env.get("HYPRIAL_GUI_LAUNCH_GRANT") or "").strip()
    if len(grant) < 32:
        raise ConfigError("HYPRIAL_GUI_LAUNCH_GRANT must be a launcher-generated one-time secret")
    return grant


def read_config(env: Mapping[str, str]) -> HostConfig:
    """Parse and validate the launcher environment once."""
    driver = (env.get("HYPRIAL_GUI_BACKEND_DRIVER") or "").strip()
    if driver not in DRIVERS:
        raise ConfigError("HYPRIAL_GUI_BACKEND_DRIVER must be daemon or contract-mock")
    token = (env.get("HYPRIAL_GUI_UPSTREAM_TOKEN") or "").strip()
    if not token:
        raise ConfigError("HYPRIAL_GUI_UPSTREAM_TOKEN is required for the GUI backend")
    grant = _launch_grant(env)
    principal = parse_principal(env.get("HYPRIAL_GUI_PRINCIPAL"), env.get("HYPRIAL_NODE_ID"))
    if driver == "contract-mock" and principal is None:
        raise ConfigError("the contract-mock driver requires HYPRIAL_GUI_PRINCIPAL and HYPRIAL_NODE_ID")
    return HostConfig(
        driver=driver,
        principal=principal,
        endpoint=parse_endpoint(env.get("HYPRIAL_BACKEND_ENDPOINT")),
        upstream_token=token,
        **_launch_fields(env, grant),
    )


def read_local_config(env: Mapping[str, str]) -> HostConfig:
    """The configuration of a host that serves the daemon driver in its own process.

    There is no upstream to name and no secret to receive: the endpoint is the
    in-process placeholder, the bearer and the principal are filled in by the
    caller from the embedded server, and any upstream variable
    is refused so a stale launcher cannot be mistaken for the in-process one.
    """
    if (env.get("HYPRIAL_GUI_BACKEND_DRIVER") or "").strip() != "daemon":
        raise ConfigError("the in-process host serves the daemon driver only")
    for name in ("HYPRIAL_BACKEND_ENDPOINT", "HYPRIAL_GUI_UPSTREAM_TOKEN"):
        if (env.get(name) or "").strip():
            raise ConfigError(f"{name} must not be set for the in-process daemon driver")
    return HostConfig(
        driver="daemon",
        principal=None,
        endpoint=IN_PROCESS_ENDPOINT,
        upstream_token="",
        **_launch_fields(env, _launch_grant(env)),
    )


class AuthorityUnavailable(Exception):
    """The in-process authority source raised, or the host no longer serves."""


class AuthorityView:
    """The host's current upstream authority, read before every route.

    Without a source the initial config is the authority for the life of the
    host. With the in-process source of the daemon driver, a new object calls
    ``invalidate`` first (the view must refresh and never replay) and a source
    that raises fails closed.
    """

    def __init__(
        self,
        initial: HostConfig,
        invalidate: Callable[[], None],
        source: Callable[[], HostConfig] | None = None,
    ) -> None:
        self._invalidate = invalidate
        self._source = source
        self._current = initial

    def read(self) -> HostConfig:
        if self._source is None:
            return self._current
        try:
            current = self._source()
        except Exception:
            self._invalidate()
            raise AuthorityUnavailable("the in-process authority is unavailable") from None
        if current is not self._current:
            self._invalidate()
            self._current = current
        return current
