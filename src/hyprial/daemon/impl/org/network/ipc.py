"""The ``org.*`` IPC family: create/invite/execute/network (§4.3).

The handlers are module-level functions over the application object
(``app``), mirroring how ``orgfs_bridge`` reaches the OrgFS facade
(``app._require_orgfs_runtime().facade``).  The forwarding seams W-C owns
— ``map_peer(peer, address)``, redial, the sidecar status — are injected
here as callbacks, never imported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.ipc.params import JsonObject, _required_string
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.org.network.directory import OrgFsDirectoryStore
from hyprial.daemon.impl.org.network.service import (
    OrgNetworkError,
    OrgNetworkService,
)
from hyprial.identity import address_path
from hyprial.identity import InviteError


def _build_service(app: Any) -> OrgNetworkService:
    fs = app._require_orgfs_runtime().facade

    def map_peer(peer: str, address: str) -> int:
        # v3 seam (tailnet cutover §4.2): the supervisor's map_peer takes
        # the peer's Tailcat address, which only the org directory holds.
        app._start_forwarding_supervisor()
        supervisor = getattr(app, "_forwarding_supervisor", None)
        if supervisor is None:
            raise OrgNetworkError(
                "FORWARDING_UNAVAILABLE", "forwarding sidecar is not configured"
            )
        return supervisor.map_peer(peer, address)

    def unmap_peer(peer: str) -> None:
        # Unlike map_peer this never *starts* the sidecar: a daemon whose
        # forwarding is down has nothing mapped to undo.
        supervisor = getattr(app, "_forwarding_supervisor", None)
        if supervisor is None:
            return
        supervisor.unmap_peer(peer)

    def read_own_address() -> str | None:
        try:
            return address_path(Path(app.hyprial_home)).read_text("utf-8").strip() or None
        except OSError:
            return None

    return OrgNetworkService(
        fs=fs,
        home=Path(app.hyprial_home),
        node_id=app.node_id,
        owner=str(app.owner),
        store=OrgFsDirectoryStore(
            fs,
            logger=lambda level, domain, event, **fields: app._log(
                level, domain, event, **fields
            ),
        ),
        map_peer=map_peer,
        unmap_peer=unmap_peer,
        request_redial=lambda: app._reconcile_forwarding_endpoints(),
        read_own_address=read_own_address,
        forwarding_status=lambda: app._forwarding_status_json(),
        # LAX(tailnet-cutover): Casdoor group creation is not wired yet;
        # create() treats the group as best effort and only warns.
        casdoor_create_group=None,
        logger=lambda level, domain, event, **fields: app._log(
            level, domain, event, **fields
        ),
    )


def _translate(error: BaseException) -> DaemonRequestError:
    if isinstance(error, OrgFsError):
        return DaemonRequestError(error.code, str(error), error.details)
    if isinstance(error, InviteError):
        return DaemonRequestError(error.code, str(error))
    if isinstance(error, OrgNetworkError):
        return DaemonRequestError(error.code, str(error))
    if isinstance(error, ValueError):
        return DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(error))
    return DaemonRequestError("ORG_NETWORK_ERROR", str(error))


def ipc_org_create(app: Any, params: JsonObject) -> dict:
    org = _required_string(params.get("org"), "org")
    try:
        return _build_service(app).create(org)
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_invite(app: Any, params: JsonObject) -> dict:
    org = _required_string(params.get("org"), "org")
    user = _required_string(params.get("user"), "user")
    try:
        return _build_service(app).invite(org, user)
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def _publish_identity_binding_after_join(app: Any) -> None:
    """The binding proof is an add-on: it never fails a completed join."""

    try:
        app._publish_identity_binding()
    except Exception as error:  # noqa: BLE001 - the join already succeeded
        app._log(
            "warn",
            "identity",
            "identity.org-binding.publish-after-join-failed",
            reason=type(error).__name__,
        )


def ipc_org_execute(app: Any, params: JsonObject) -> dict:
    link = _required_string(params.get("link"), "link")
    try:
        result = _build_service(app).execute(link)
        _publish_identity_binding_after_join(app)
        return result
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_join(app: Any, params: JsonObject) -> dict:
    """``org.join``: the §5.1 alias of execute, all three link forms."""

    link = _required_string(params.get("link"), "link")
    try:
        result = _build_service(app).join(link)
        _publish_identity_binding_after_join(app)
        return result
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_leave(app: Any, params: JsonObject) -> dict:
    org = _required_string(params.get("org"), "org")
    try:
        return _build_service(app).leave(org)
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_remove(app: Any, params: JsonObject) -> dict:
    org = _required_string(params.get("org"), "org")
    user = _required_string(params.get("user"), "user")
    try:
        return _build_service(app).remove(org, user)
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_delete(app: Any, params: JsonObject) -> dict:
    org = _required_string(params.get("org"), "org")
    try:
        return _build_service(app).delete(org)
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_list(app: Any, params: JsonObject) -> dict:
    try:
        return _build_service(app).list()
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_network(app: Any, params: JsonObject) -> dict:
    org = params.get("org")
    if org is not None and not isinstance(org, str):
        raise DaemonRequestError(
            ipc_errors.INVALID_ARGUMENT, "org must be a string when present"
        )
    try:
        return _build_service(app).network(org)
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def publish_self_for_app(app: Any) -> dict:
    """Best-effort ``publish_self`` for startup/reconcile wiring (§4.3).

    Never raises: a daemon start must not depend on the org directory, and
    the refresh path simply retries on the next tick.
    """

    try:
        return _build_service(app).publish_self()
    except Exception as error:  # noqa: BLE001 - startup must not depend on it
        try:
            app._log("warn", "org", "org.publish_self.failed", detail=str(error))
        except Exception:  # noqa: BLE001 - logging must never mask the tick
            pass
        return {"published": [], "skipped": []}


def process_leave_requests_for_app(app: Any) -> dict:
    """Best-effort owner/admin reconcile of pending leave requests (§4.3).

    Never raises; a node that is not owner/admin of an org simply skips it.
    """

    try:
        return _build_service(app).process_leave_requests()
    except Exception as error:  # noqa: BLE001 - the reconcile tick must survive
        try:
            app._log("warn", "org", "org.leave_requests.failed", detail=str(error))
        except Exception:  # noqa: BLE001 - logging must never mask the tick
            pass
        return {"processed": [], "skipped": []}


def directory_peers_for_app(app: Any) -> dict[str, str]:
    """The directory peers callback for endpoint discovery (§4.2 seam).

    Failures answer an empty mapping: discovery is additive and a directory
    that cannot answer must not stop a daemon from starting.
    """

    try:
        return _build_service(app).directory_peers()
    except Exception:  # noqa: BLE001 - discovery must never raise
        return {}


__all__ = [
    "directory_peers_for_app",
    "ipc_org_create",
    "ipc_org_delete",
    "ipc_org_execute",
    "ipc_org_invite",
    "ipc_org_join",
    "ipc_org_leave",
    "ipc_org_list",
    "ipc_org_network",
    "ipc_org_remove",
    "publish_self_for_app",
]
