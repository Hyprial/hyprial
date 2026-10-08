"""Orgfs web-host resolution — derivation, never persistence (design §7a).

The ``orgfs:<owner>:<spaceId>:<nodeId>`` URI is the only host-free identity
ever stored or passed between components.  Anything that shows a link to a
human derives ``https://<fs-host>/<owner>/<spaceId>/<nodeId>`` at render
time for the viewer's own node, through the single grammar-side helper
:func:`hyprial.uri.orgfs_web_url`.  This module is the environment side: it
resolves the :class:`~hyprial.uri.OrgfsWebNetwork` view from the node's own
records.

Precedence after the tailnet cutover (2026-10-03):

1. the explicit ``orgfs.web_host`` override in ``settings.json`` — a bare
   host name, for orgs whose fs service is not reachable under a
   derivable name;
2. nothing — the web URL is absent and a host is never guessed.

The former tailnet FQDN sources (``state/tsnet/node.json`` and a live
``tailscale status --json`` probe) are gone with the self-host tailnet:
under the Tailcat cutover the transport has no DNS name for this node, so
no derivation is possible and the operator configures ``orgfs.web_host``
explicitly.

No host is ever hard-coded here; the guard in
``tests/test_uri_construction_guard.py`` keeps it that way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from hyprial.kernel import (
    OrgfsWebNetwork,
    orgfs_web_url,
    parse_orgfs_uri)

#: The ``settings.json`` section/field spelling the design's
#: ``orgfs.web_host`` config key.
ORGFS_WEB_SETTINGS_KEY = "orgfs"
ORGFS_WEB_HOST_FIELD = "web_host"


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


def resolve_orgfs_web_network(hyprial_home: Path) -> OrgfsWebNetwork:
    """The local node's web-network view: the override, or an empty view.

    When ``orgfs.web_host`` is unset the view is empty and the web URL is
    absent: the Tailcat transport carries no DNS name to derive a host
    from, and a host is never guessed.
    """

    override = read_orgfs_web_host(hyprial_home)
    if override is not None:
        return OrgfsWebNetwork(web_host=override)
    return OrgfsWebNetwork()


def with_human_web_urls(value: Any, hyprial_home: Path) -> Any:
    """``value`` with a derived ``webUrl`` next to every node ``uri``.

    Render-time derivation for human-facing output ONLY: the wire form
    (``--json``) and agent-facing surfaces carry the host-free URI alone
    (design §7a rule 1).  The network view is resolved lazily — a result
    with no node URI never touches settings.  When no host is configured
    the URI stands alone and nothing is added.
    """

    if not _contains_node_uri(value):
        return value
    return _attach(value, resolve_orgfs_web_network(hyprial_home))


def _contains_node_uri(value: Any) -> bool:
    if isinstance(value, dict):
        uri = value.get("uri")
        if isinstance(uri, str) and parse_orgfs_uri(uri) is not None:
            return True
        return any(_contains_node_uri(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_node_uri(item) for item in value)
    return False


def _attach(value: Any, network: OrgfsWebNetwork) -> Any:
    if isinstance(value, dict):
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
