"""Build the bounded three-pane projection returned by ``org network``."""

# hyprial-unbounded-wait-allow: result - BoundedProjection enforces an absolute deadline

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from hyprial.daemon.impl.org.network.binding import (
    resolve_org_acl_space,
    resolve_org_directory_space,
)
from hyprial.daemon.impl.org.network.directory import (
    OrgFsDirectoryStore,
    network_directory_projection,
)
from hyprial.daemon.impl.org.network.status import start_bounded_projection
from hyprial.daemon.impl.orgfs.api import OrgFs, OrgFsError
from hyprial.identity import (
    ACTION_READ,
    RESOURCE_DIRECTORY_ALL,
    DirectoryDevice,
    org_space_name,
)

NETWORK_PANE_TIMEOUT_SECONDS = 1.0


def device_view(device: DirectoryDevice) -> dict[str, Any]:
    """Render a directory row without exposing its address secret."""

    view = dict(device.as_record())
    view["addressPresent"] = bool(view.pop("address"))
    view["presence"] = "unknown"
    return view



def _org_state_details(
    fs: OrgFs, store: OrgFsDirectoryStore, org: str, logger: Callable[..., None]
) -> tuple[str, dict[str, object] | None]:
    details = getattr(store, "org_state_details", None)
    state_reader = details or getattr(store, "org_state", None)
    if state_reader is None:
        acl_space, _reason = resolve_org_acl_space(fs, org)
        return ("legacy-active" if acl_space is not None else "partial"), None
    try:
        result = state_reader(org)
        return result if details is not None else (result, None)
    except (OrgFsError, ValueError) as error:
        logger("warn", "org", "org.state.unreadable", org=org, detail=str(error))
        return "partial", None

def network_view(
    *,
    fs: OrgFs,
    store: OrgFsDirectoryStore,
    node_id: str,
    owner: str,
    orgs: list[str],
    org: str | None,
    require_allows: Callable[..., None],
    authorization_error: type[Exception],
    forwarding_status: Callable[[], Mapping[str, Any] | None] | None,
    own_address: Callable[[], str | None],
    server_public: str,
    logger: Callable[..., None],
) -> dict[str, Any]:
    """Return directory, mapped-peer, and sidecar panes with source status."""

    states = {
        name: _org_state_details(fs, store, name, logger) for name in orgs
    }
    if org is not None:
        directory_space, reason = resolve_org_directory_space(fs, org)
        if reason == "missing-directory-space":
            raise OrgFsError(
                "unknown-space", {"org": org, "space": org_space_name(org)}
            )
        if directory_space is None:
            raise OrgFsError("ambiguous-space", {"org": org})
        state, problem = states[org]
        if state in {"creating", "partial"}:
            value = problem.get("value") if problem is not None else None
            reported = f"; marker state={value!r}" if value is not None else ""
            raise authorization_error(
                "ORG_ACTION_NOT_AUTHORIZED", f"org {org!r} is {state}{reported}"
            )
        require_allows(
            org=org, action=ACTION_READ, resource=RESOURCE_DIRECTORY_ALL
        )
    unavailable: list[dict[str, object]] = []
    for name in orgs:
        if name == org:
            continue
        state, _problem = states[name]
        unavailable_reason = state if state in {"creating", "partial"} else None
        if unavailable_reason is None:
            try:
                require_allows(
                    org=name, action=ACTION_READ, resource=RESOURCE_DIRECTORY_ALL
                )
            except authorization_error:
                unavailable_reason = "policy-denied"
        if unavailable_reason is not None:
            unavailable.append(
                {
                    "org": name,
                    "available": False,
                    "reason": unavailable_reason,
                }
            )
    unavailable_names = {str(item["org"]) for item in unavailable}
    directory_read = start_bounded_projection(
        lambda: network_directory_projection(
            store,
            orgs,
            unavailable_names,
            device_view=device_view,
            logger=logger,
        ),
        lane="directory",
        timeout_seconds=NETWORK_PANE_TIMEOUT_SECONDS,
    )
    forwarding_read = start_bounded_projection(
        forwarding_status,
        lane="sidecar",
        timeout_seconds=NETWORK_PANE_TIMEOUT_SECONDS,
    )
    directory_result, directory_status = directory_read.result()
    forwarding, forwarding_pane_status = forwarding_read.result()
    if directory_result is None:
        directory = {name: [] for name in orgs}
        directory_pending: dict[str, Any] = dict.fromkeys(orgs)
    else:
        directory, directory_pending = directory_result
    address = own_address()
    mapped_peers = forwarding.get("peers") if isinstance(forwarding, Mapping) else None
    directory_problems = [
        problem
        for status in directory_pending.values()
        if isinstance(status, Mapping)
        for problem in status.get("dataProblems", [])
        if isinstance(problem, Mapping)
    ]
    sidecar = (
        {
            key: forwarding.get(key)
            for key in (
                "policy",
                "endpoints",
                "dialed",
                "restartRequired",
                "lastPoll",
                "slots",
                "connectionTelemetry",
                "derpMapURL",
                "derpMapSource",
            )
        }
        if isinstance(forwarding, Mapping)
        else None
    )
    return {
        "orgs": orgs,
        "unavailableOrgs": unavailable,
        "dataProblems": [
            problem for _state, problem in states.values() if problem is not None
        ] + directory_problems,
        "directory": directory,
        "directoryPending": directory_pending,
        "self": {
            "deviceId": node_id,
            "owner": owner,
            "serverPublic": server_public,
            "addressPresent": address is not None,
        },
        "mappedPeers": mapped_peers,
        "sidecar": sidecar,
        "paneStatus": {
            "directory": directory_status,
            "mappedPeers": dict(forwarding_pane_status),
            "sidecar": dict(forwarding_pane_status),
        },
        "sources": {
            "directory": "orgfs",
            "mappedPeers": "forwarding-sidecar",
            "sidecar": "forwarding-sidecar",
        },
    }
