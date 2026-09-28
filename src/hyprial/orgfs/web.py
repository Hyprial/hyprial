"""Per-tailnet orgfs web links — derivation, never persistence (design §7a).

The ``orgfs:<owner>:<spaceId>:<nodeId>`` URI is the only host-free identity
ever stored or passed between components.  Anything that shows a link to a
human derives ``https://<fs-host>/<owner>/<spaceId>/<nodeId>`` at render
time for the viewer's own node, through the single grammar-side helper
:func:`hyprial.uri.orgfs_web_url`.  This module is the environment side: it
resolves the :class:`~hyprial.uri.OrgfsWebNetwork` view from the node's own
records, in the design's precedence order:

1. the explicit ``orgfs.web_host`` override in ``settings.json`` — a bare
   host name, for orgs whose fs service is not at ``fs.<tailnet domain>``;
2. the node's own tailnet FQDN recorded on either join path
   (``state/tsnet/node.json`` ``hostname`` on the sidecar path), or a live
   read-only ``tailscale status --json`` probe on the host-tailnet path
   (``CurrentTailnet.MagicDNSSuffix`` preferred over ``Self.DNSName``);
3. nothing — the web URL is absent and a host is never guessed.

No host is ever hard-coded here; the guard in
``tests/test_uri_construction_guard.py`` keeps it that way.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
from typing import Any, Callable, Mapping

from hyprial.network_profile import TSNET_STATE_DIRNAME
from hyprial.uri import (
    OrgfsWebNetwork,
    orgfs_web_url,
    parse_orgfs_uri,
)

#: The ``settings.json`` section/field spelling the design's
#: ``orgfs.web_host`` config key.
ORGFS_WEB_SETTINGS_KEY = "orgfs"
ORGFS_WEB_HOST_FIELD = "web_host"

#: Read-only probe budget — mirrors tailnet_identity.TAILNET_PROBE_TIMEOUT_SECONDS.
_WEB_PROBE_TIMEOUT_SECONDS = 5.0

#: ``Runner`` returns ``(returncode, stdout, stderr)`` for one argv — the
#: same test seam ``hyprial.tailnet_identity`` uses for its probes.
Runner = Callable[[list[str]], "tuple[int, str, str]"]


def read_orgfs_web_host(hyprial_home: Path) -> str | None:
    """``orgfs.web_host`` from settings.json, or None when unset.

    Same contract as the other settings readers (``forwarding._stored_mode``,
    ``read_org_fetch_source``): a settings file that exists but does not
    parse is an error, never a silent "unset".
    """

    path = Path(hyprial_home) / "settings.json"
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot parse {path}: {error}") from error
    if not isinstance(record, dict):
        raise ValueError(f"cannot parse {path}: top level is not an object")
    section = record.get(ORGFS_WEB_SETTINGS_KEY)
    if section is None:
        return None
    value = section.get(ORGFS_WEB_HOST_FIELD) if isinstance(section, dict) else None
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: orgfs.web_host must be a non-empty host name")
    return value.strip()


def resolve_orgfs_web_network(
    hyprial_home: Path, *, runner: Runner | None = None
) -> OrgfsWebNetwork:
    """The local node's web-network view, in the design's precedence order.

    The override wins when configured.  Otherwise the sidecar's recorded
    FQDN (cheap, and it is the org tailnet this home actually joined) beats
    a live host-tailscale probe; the probe supplies the host-tailnet join
    path and prefers the authoritative MagicDNS suffix.  A node with no
    tailnet evidence gets an empty view — the web URL is then absent.
    """

    override = read_orgfs_web_host(hyprial_home)
    if override is not None:
        return OrgfsWebNetwork(web_host=override)
    fqdn = _sidecar_fqdn(hyprial_home)
    if fqdn is not None:
        return OrgfsWebNetwork(fqdn=fqdn)
    probed = _probe_host_tailnet(runner)
    if probed is not None:
        return probed
    return OrgfsWebNetwork()


def with_human_web_urls(
    value: Any, hyprial_home: Path, *, runner: Runner | None = None
) -> Any:
    """``value`` with a derived ``webUrl`` next to every node ``uri``.

    Render-time derivation for human-facing output ONLY: the wire form
    (``--json``) and agent-facing surfaces carry the host-free URI alone
    (design §7a rule 1).  The network view is resolved lazily — a result
    with no node URI never touches settings, node.json, or tailscaled.
    When no host is derivable the URI stands alone and nothing is added.
    """

    if not _contains_node_uri(value):
        return value
    network = resolve_orgfs_web_network(hyprial_home, runner=runner)
    return _attach(value, network)


def _contains_node_uri(value: Any) -> bool:
    if isinstance(value, Mapping):
        uri = value.get("uri")
        if isinstance(uri, str) and parse_orgfs_uri(uri) is not None:
            return True
        return any(_contains_node_uri(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_node_uri(item) for item in value)
    return False


def _attach(value: Any, network: OrgfsWebNetwork) -> Any:
    if isinstance(value, Mapping):
        uri = value.get("uri")
        if isinstance(uri, str) and parse_orgfs_uri(uri) is not None:
            web_url = orgfs_web_url(uri, network=network)
            if web_url is not None:
                attached: dict[str, Any] = {}
                for key, item in value.items():
                    attached[str(key)] = _attach(item, network)
                    if key == "uri":
                        attached["webUrl"] = web_url
                return attached
        return {str(key): _attach(item, network) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_attach(item, network) for item in value]
    return value


def _sidecar_fqdn(hyprial_home: Path) -> str | None:
    """The sidecar-recorded ``status.Self.DNSName``, or None when absent."""

    record = Path(hyprial_home) / TSNET_STATE_DIRNAME / "node.json"
    try:
        parsed = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    hostname = parsed.get("hostname") if isinstance(parsed, dict) else None
    if isinstance(hostname, str) and hostname.strip():
        return hostname.strip()
    return None


def _probe_host_tailnet(runner: Runner | None = None) -> OrgfsWebNetwork | None:
    """Read-only ``tailscale status --json`` probe; None when unavailable."""

    if runner is None:
        executable = shutil.which("tailscale")
        if executable is None:
            return None
        run = _subprocess_runner
    else:
        executable = "tailscale"
        run = runner
    try:
        returncode, stdout, _stderr = run([executable, "status", "--json"])
    except (OSError, subprocess.TimeoutExpired):
        return None
    if returncode != 0:
        return None
    try:
        status = json.loads(stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(status, dict):
        return None
    tailnet = status.get("CurrentTailnet")
    suffix = tailnet.get("MagicDNSSuffix") if isinstance(tailnet, dict) else None
    self_node = status.get("Self")
    dns_name = self_node.get("DNSName") if isinstance(self_node, dict) else None
    return OrgfsWebNetwork(
        magic_dns_suffix=(
            suffix.strip() if isinstance(suffix, str) and suffix.strip() else None
        ),
        fqdn=(
            dns_name.strip() if isinstance(dns_name, str) and dns_name.strip() else None
        ),
    )


def _subprocess_runner(argv: list[str]) -> tuple[int, str, str]:
    completed = subprocess.run(
        argv,
        capture_output=True,
        timeout=_WEB_PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", "replace"),
        completed.stderr.decode("utf-8", "replace"),
    )
