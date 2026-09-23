"""Host tailnet identity for ``hyprial init``'s self-host branch (U6).

Allen 2026-09-18: a user who already runs their own tailscale (official or
self-hosted headscale) does not go through the Hyprial login/join flow.
When they pick the self-host branch at ``hyprial init``, the owner is
whatever the host's own tailnet control plane asserts — ``tailscale whoami``
— and nothing else: no manual entry, no Hyprial OIDC, no sidecar, no join.

Detection mirrors the daemon's own host-tailscale reads
(``daemon/discovery.py``: ``shutil.which("tailscale")`` plus read-only JSON
subcommands), so this module is the same trick, not a new invention.  Every
command here is read-only; this module never runs ``tailscale up`` or
``tailscale login`` and never changes network or daemon state.

Failure is loud and specific (Allen: "如果使用self-host，但没有安装
tailscale，则直接失败"): no tailscale on PATH, a tailscale that is not
connected to a tailnet, and a whoami that asserts no login name are three
different operator situations and carry three different codes.  There is no
fallback to the Hyprial service — that choice belongs to the human at
``hyprial init``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

#: Read-only probes against the local tailscaled socket; the same order of
#: budget ``daemon/discovery.py`` gives its status read.
TAILNET_PROBE_TIMEOUT_SECONDS = 5.0

#: ``Runner`` returns ``(returncode, stdout, stderr)`` for one argv — the
#: test seam for the two read-only subcommands this module issues.
Runner = Callable[[list[str]], "tuple[int, str, str]"]


class TailnetIdentityError(RuntimeError):
    """The host tailnet cannot assert an identity for this home.

    ``code`` names the precise gap (not installed / not connected / no
    identity) and ``data`` is JSON-serializable diagnostic detail, so the
    CLI can fail with the same shape it reports every other named failure.
    """

    def __init__(self, code: str, message: str, data: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(frozen=True)
class HostTailnetIdentity:
    """What the host's own control plane asserted about this node.

    ``login_name`` is ``tailscale whoami --json``'s
    ``UserProfile.LoginName`` **verbatim** (whitespace-stripped).  On a
    tagged unattended node that value is the node's DNS name rather than a
    human user — Allen 2026-09-18: adopting it is the user's choice, and the
    caller must print what was adopted so the user can see exactly that.
    """

    login_name: str
    dns_name: str | None
    ip4: str | None
    tags: tuple[str, ...]
    tailnet_name: str | None


def resolve_host_tailnet_identity(
    *,
    executable: str | None = None,
    runner: Runner | None = None,
) -> HostTailnetIdentity:
    """Assert this node's tailnet identity, or raise :class:`TailnetIdentityError`.

    Three named failures, in probing order: ``TAILSCALE_NOT_INSTALLED``
    (no CLI on PATH), ``TAILNET_NOT_CONNECTED`` (installed but ``status``
    does not show a joined, running node), ``TAILNET_IDENTITY_UNAVAILABLE``
    (connected, but ``whoami`` asserts no login name).
    """

    run = runner if runner is not None else _subprocess_runner
    exe = executable if executable is not None else shutil.which("tailscale")
    if exe is None:
        raise TailnetIdentityError(
            "TAILSCALE_NOT_INSTALLED",
            "the self-hosted identity source requires the tailscale CLI, but no "
            "`tailscale` executable is on PATH; install tailscale (or your "
            "headscale client), log it into your tailnet, then rerun "
            "`hyprial init`",
            data={"reason": "no tailscale executable found on PATH"},
        )

    status = _run_json(
        run,
        [exe, "status", "--json"],
        code="TAILNET_STATUS_FAILED",
        what="tailscale status --json",
    )
    backend_state = status.get("BackendState")
    self_node = status.get("Self")
    addresses = (
        self_node.get("TailscaleIPs")
        if isinstance(self_node, dict)
        else None
    ) or []
    if backend_state != "Running" or not addresses:
        raise TailnetIdentityError(
            "TAILNET_NOT_CONNECTED",
            "tailscale is installed but this node is not connected to a "
            f"tailnet (BackendState={backend_state!r}, "
            f"tailscaleIPs={list(addresses)}); connect it to your tailnet "
            "(with your own tailscale/headscale account, not the Hyprial "
            "service), then rerun `hyprial init`",
            data={"backendState": backend_state, "tailscaleIPs": list(addresses)},
        )

    whoami = _run_json(
        run,
        [exe, "whoami", "--json"],
        code="TAILNET_WHOAMI_FAILED",
        what="tailscale whoami --json",
    )
    profile = whoami.get("UserProfile")
    login_name = (
        profile.get("LoginName") if isinstance(profile, dict) else None
    )
    if not isinstance(login_name, str) or not login_name.strip():
        raise TailnetIdentityError(
            "TAILNET_IDENTITY_UNAVAILABLE",
            "tailscale whoami returned no UserProfile.LoginName, so the host "
            "tailnet asserts no identity this home could adopt; log this node "
            "into your tailnet, then rerun `hyprial init`",
            data={"profileKeys": sorted(profile) if isinstance(profile, dict) else []},
        )

    dns_name = self_node.get("DNSName") if isinstance(self_node, dict) else None
    tags = self_node.get("Tags") if isinstance(self_node, dict) else None
    tailnet = status.get("CurrentTailnet")
    return HostTailnetIdentity(
        login_name=login_name.strip(),
        # Display normalization only: status reports the FQDN with a trailing
        # dot.  The *owner* is whoami's LoginName verbatim and is never
        # touched here.
        dns_name=(
            dns_name.rstrip(".") if isinstance(dns_name, str) and dns_name else None
        ),
        ip4=_first_ipv4(addresses),
        tags=tuple(
            tag
            for tag in (tags or [])
            if isinstance(tag, str) and tag
        ),
        tailnet_name=(
            tailnet.get("Name")
            if isinstance(tailnet, dict)
            else None
        ),
    )


def _subprocess_runner(argv: list[str]) -> tuple[int, str, str]:
    """Run one read-only subcommand under the module's probe budget."""

    completed = subprocess.run(
        argv,
        capture_output=True,
        timeout=TAILNET_PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )


def _run_json(
    run: Runner,
    argv: list[str],
    *,
    code: str,
    what: str,
) -> dict:
    try:
        returncode, stdout, stderr = run(list(argv))
    except subprocess.TimeoutExpired as error:
        raise TailnetIdentityError(
            code,
            f"{what} timed out after {TAILNET_PROBE_TIMEOUT_SECONDS}s; is "
            "the local tailscaled socket responsive?",
            data={"reason": "timeout"},
        ) from error
    except OSError as error:
        raise TailnetIdentityError(
            code,
            f"{what} could not run: {type(error).__name__}: {error}",
            data={"reason": type(error).__name__},
        ) from error
    if returncode != 0:
        raise TailnetIdentityError(
            code,
            f"{what} failed with exit code {returncode}"
            + (f": {stderr.strip()}" if stderr.strip() else ""),
            data={"exitCode": returncode, "stderr": stderr.strip()},
        )
    try:
        parsed = json.loads(stdout)
    except (ValueError, TypeError) as error:
        raise TailnetIdentityError(
            code,
            f"{what} did not print JSON ({type(error).__name__}); an "
            "unusual tailscale build cannot back the self-hosted identity",
            data={"reason": "unparseable stdout"},
        ) from error
    if not isinstance(parsed, dict):
        raise TailnetIdentityError(
            code,
            f"{what} printed {type(parsed).__name__}, not a JSON object",
            data={"reason": "not an object"},
        )
    return parsed


def _first_ipv4(addresses: list) -> str | None:
    for address in addresses:
        if isinstance(address, str) and "." in address:
            return address
    return None
