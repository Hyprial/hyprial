"""The ``org.*`` IPC family: create/invite/execute/network (§4.3).

The handlers are module-level functions over the application object
(``app``), mirroring how ``orgfs_bridge`` reaches the OrgFS facade
(``app._require_orgfs_runtime().facade``).  The forwarding seams W-C owns
— ``map_peer(peer, address)``, redial, the sidecar status — are injected
here as callbacks, never imported.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.kernel import canonical_user_uri, parse_agent_uri
from hyprial.daemon.impl.configuration.identity import normalize_agent_recipient
from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _optional_boolean,
    _required_string,
)
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.org.network.access import PeerAccessActor
from hyprial.daemon.impl.org.network.access.snapshot import with_bootstrap
from hyprial.daemon.impl.org.network.casdoor import casdoor_groups_for_home
from hyprial.daemon.impl.org.network.casdoor.client import CasdoorGroupError
from hyprial.daemon.impl.org.network.casdoor.membership import (
    CasdoorConfigError,
    casdoor_credential_file,
)
from hyprial.daemon.impl.org.network.casdoor.sync import (
    CasdoorMembershipSync,
    ConfirmedMembers,
)
from hyprial.daemon.impl.org.network.casdoor.webhook import (
    CasdoorWebhookReceiver,
    Trigger,
    webhook_settings,
)
from hyprial.daemon.impl.org.network.dialing import DialState, dial_peers
from hyprial.daemon.impl.org.network.directory import (
    InvalidDeviceQuarantine,
    OrgFsDirectoryStore,
)
from hyprial.daemon.impl.org.network.service import (
    OrgNetworkError,
    OrgNetworkService,
)
from hyprial.daemon.impl.org.network.state import (
    add_org_acl_grant,
    read_org_acl_grants,
    remove_org_acl_grant,
)
from hyprial.identity import address_path
from hyprial.identity import InviteError


def _invalid_device_quarantine(app: Any) -> InvalidDeviceQuarantine:
    quarantine = getattr(app, "_org_directory_quarantine", None)
    if quarantine is None:
        quarantine = InvalidDeviceQuarantine()
        app._org_directory_quarantine = quarantine
    return quarantine


def _build_service(app: Any) -> OrgNetworkService:
    fs = app._require_orgfs_runtime().facade

    def map_peer(peer: str, address: str) -> int | None:
        # The invite/join bootstrap connects a peer by assigning it a
        # fixed slot (the only dialing shape; design §5): start the
        # sidecar, then bind the slot.  The address is the peer's Tailcat
        # address, which only the org directory holds.
        from hyprial.daemon.impl.forwarding.slots import bind_peer_slot

        app._start_forwarding_supervisor()
        supervisor = getattr(app, "_forwarding_supervisor", None)
        if supervisor is None:
            raise OrgNetworkError(
                "FORWARDING_UNAVAILABLE", "forwarding sidecar is not configured"
            )
        return bind_peer_slot(app, peer, address)

    def unmap_peer(peer: str) -> None:
        # Unlike map_peer this never *starts* the sidecar: a daemon whose
        # forwarding is down has nothing mapped to undo.  Releasing the
        # slot (unmap + free) keeps the pool reusable.
        from hyprial.daemon.impl.forwarding.slots import unbind_peer_slot

        supervisor = getattr(app, "_forwarding_supervisor", None)
        if supervisor is None:
            return
        unbind_peer_slot(app, peer)

    def read_own_address() -> str | None:
        try:
            return (
                address_path(Path(app.hyprial_home)).read_text("utf-8").strip() or None
            )
        except OSError:
            return None

    def derive_client_key(server_public: str) -> str:
        app._start_forwarding_supervisor()
        supervisor = getattr(app, "_forwarding_supervisor", None)
        if supervisor is None:
            raise OrgNetworkError(
                "CLIENT_KEY_DERIVATION_UNAVAILABLE",
                "forwarding sidecar is not configured",
            )
        return supervisor.derive_client_key(server_public)

    def local_server_public() -> str | None:
        supervisor = getattr(app, "_forwarding_supervisor", None)
        backend = supervisor.endpoints() if supervisor is not None else None
        value = (
            getattr(backend.controller, "server_public", None)
            if backend is not None
            else None
        )
        return value if isinstance(value, str) and value else None

    return OrgNetworkService(
        fs=fs,
        home=Path(app.hyprial_home),
        node_id=app.node_id,
        owner=str(app.owner),
        store=OrgFsDirectoryStore(
            fs,
            invalid_devices=_invalid_device_quarantine(app),
            logger=lambda level, domain, event, **fields: app._log(
                level, domain, event, **fields
            ),
        ),
        map_peer=map_peer,
        unmap_peer=unmap_peer,
        derive_client_key=derive_client_key,
        local_server_public=local_server_public,
        request_redial=lambda: app._reconcile_forwarding_endpoints(),
        read_own_address=read_own_address,
        forwarding_status=lambda: app._forwarding_status_json(),
        # Casdoor group membership is the authority (design §2): owner-side
        # commands write it first; absent config = not this node's job.
        casdoor=casdoor_groups_for_home(Path(app.hyprial_home)),
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
        return _access_changed(app, "org-create", _build_service(app).create(org))
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_repair(app: Any, params: JsonObject) -> dict:
    org_value = params.get("org")
    org = None if org_value is None else _required_string(org_value, "org")
    apply = _optional_boolean(params.get("apply"), "apply") or False
    try:
        return _build_service(app).repair(org, apply=apply)
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_invite(app: Any, params: JsonObject) -> dict:
    org = _required_string(params.get("org"), "org")
    user = _required_string(params.get("user"), "user")
    try:
        result = _build_service(app).invite(org, user)
        _membership_changed(app, org, user)
        return _access_changed(app, "org-invite", result)
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
        result = _access_changed(app, "org-execute", _build_service(app).execute(link))
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
        result = _access_changed(app, "org-join", _build_service(app).join(link))
        _publish_identity_binding_after_join(app)
        return result
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_leave(app: Any, params: JsonObject) -> dict:
    org = _required_string(params.get("org"), "org")
    try:
        return _access_changed(app, "org-leave", _build_service(app).leave(org))
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_remove(app: Any, params: JsonObject) -> dict:
    org = _required_string(params.get("org"), "org")
    user = _required_string(params.get("user"), "user")
    try:
        result = _build_service(app).remove(org, user)
        _membership_changed(app, org, user)
        return _access_changed(app, "org-remove", result)
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error


def ipc_org_delete(app: Any, params: JsonObject) -> dict:
    org = _required_string(params.get("org"), "org")
    try:
        return _access_changed(app, "org-delete", _build_service(app).delete(org))
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
        result = _build_service(app).network(org)
    except OrgFsError as error:
        raise _translate(error) from error
    except (InviteError, OrgNetworkError, ValueError) as error:
        raise _translate(error) from error
    sync = getattr(app, "_casdoor_membership_sync", None)
    # Owner side only (design §3): casdoorOnly, last sync time, last error.
    result["casdoorSync"] = (
        {name: sync.status(name) for name in result.get("orgs", [])}
        if sync is not None
        else None
    )
    return result


# -- org.acl.*: owner-node entry authorizations (design §8 ruling 3) ---------
#
# The grants feed the ACL-space entry gate in ``orgfs_bridge``.  They live in
# the owner node's local state only — never in orgfs, never synchronized —
# because the gate itself exists only where the writes can be authored.


def _org_acl_entry_or_forbidden(
    app: Any, params: JsonObject, *, method: str, trusted_origin: str | None
) -> None:
    """Only the owner's terminal or the owner's user-proxy manages grants."""

    entry = app._org_acl_entry_caller(
        params, method=method, trusted_origin=trusted_origin
    )
    if entry is not None and entry[0] in ("terminal", "user-proxy"):
        return
    raise DaemonRequestError(
        ipc_errors.ORG_ACL_GRANT_FORBIDDEN,
        f"{method} is available only to the owner's terminal or the owner's user-proxy",
        {"method": method} | ({"caller": entry[1]} if entry is not None else {}),
    )


def _org_acl_owner_space(app: Any, org: str) -> None:
    """The org must exist here and this daemon's owner must own it."""

    try:
        space = _build_service(app)._require_space(org)
    except (OrgFsError, ValueError) as error:
        raise _translate(error) from error
    author = (
        app.owner
        if str(app.owner).startswith("user:")
        else canonical_user_uri(app.owner)
    )
    if space.owner != author:
        raise DaemonRequestError(
            ipc_errors.ORG_ACL_GRANT_FORBIDDEN,
            f"ACL entry grants live on the owner node; this node does not own {org}",
            {"org": org, "reason": "not-owner-node"},
        )


def _org_acl_owned_agent(app: Any, value: str) -> str:
    """Resolve a grantee to an agent this daemon runs for its own owner.

    The daemon's registration is the authority: a short name must resolve in
    the local alias table, and a canonical URI must name this owner, this
    node, and a registered local agent — anything else is not "an agent of
    the owner's own" and cannot be granted.
    """

    not_owned = DaemonRequestError(
        ipc_errors.ORG_ACL_GRANT_NOT_OWNED,
        f"{value!r} is not an agent registered to this node's owner",
        {"agent": value},
    )
    normalized = normalize_agent_recipient(value)
    if ":" not in normalized:
        resolved = app._resolve_agent_alias(normalized)
        if resolved == normalized:
            raise not_owned
        normalized = resolved
    parsed = parse_agent_uri(normalized)
    if parsed is None:
        raise not_owned
    owner, machine, short = parsed
    if owner != app.owner or machine != app.node_id:
        raise not_owned
    if app._resolve_agent_alias(short) != normalized:
        raise not_owned
    return normalized


def ipc_org_acl_grant(
    app: Any, params: JsonObject, trusted_origin: str | None = None
) -> dict:
    org = _required_string(params.get("org"), "org")
    _org_acl_entry_or_forbidden(
        app, params, method="org.acl.grant", trusted_origin=trusted_origin
    )
    _org_acl_owner_space(app, org)
    agent = _org_acl_owned_agent(app, _required_string(params.get("agent"), "agent"))
    now_ms = int(time.time() * 1000)
    until_ms = params.get("untilMs")
    if until_ms is not None:
        if isinstance(until_ms, bool) or not isinstance(until_ms, int):
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "untilMs must be an integer"
            )
        if until_ms <= now_ms:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT,
                "untilMs must be in the future",
                {"untilMs": until_ms, "nowMs": now_ms},
            )
    entry = add_org_acl_grant(
        Path(app.hyprial_home),
        org,
        agent,
        granted_at_ms=now_ms,
        until_ms=until_ms,
    )
    app._log("info", "org", "org.acl.grant", org=org, agent=agent, untilMs=until_ms)
    return {"org": org, "agent": agent, **entry}


def ipc_org_acl_revoke(
    app: Any, params: JsonObject, trusted_origin: str | None = None
) -> dict:
    org = _required_string(params.get("org"), "org")
    agent = _required_string(params.get("agent"), "agent")
    _org_acl_entry_or_forbidden(
        app, params, method="org.acl.revoke", trusted_origin=trusted_origin
    )
    _org_acl_owner_space(app, org)
    # Revoke takes the grant's own spelling: resolve when it is a live local
    # agent, else fall back to the literal so a grant for a since-removed
    # agent can still be revoked.
    try:
        agent = _org_acl_owned_agent(app, agent)
    except DaemonRequestError:
        pass
    revoked = remove_org_acl_grant(Path(app.hyprial_home), org, agent)
    app._log("info", "org", "org.acl.revoke", org=org, agent=agent, revoked=revoked)
    return {"org": org, "agent": agent, "revoked": revoked}


def ipc_org_acl_ls(
    app: Any, params: JsonObject, trusted_origin: str | None = None
) -> dict:
    org = _required_string(params.get("org"), "org")
    _org_acl_entry_or_forbidden(
        app, params, method="org.acl.ls", trusted_origin=trusted_origin
    )
    _org_acl_owner_space(app, org)
    now_ms = int(time.time() * 1000)
    grants = read_org_acl_grants(Path(app.hyprial_home), now_ms=now_ms).get(org, {})
    return {
        "org": org,
        "grants": [
            {"agent": agent, **entry} for agent, entry in sorted(grants.items())
        ],
    }


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
        result = _build_service(app).process_leave_requests()
        for item in result.get("processed", []):
            _membership_changed(app, str(item.get("org")), str(item.get("user")))
        return result
    except Exception as error:  # noqa: BLE001 - the reconcile tick must survive
        try:
            app._log("warn", "org", "org.leave_requests.failed", detail=str(error))
        except Exception:  # noqa: BLE001 - logging must never mask the tick
            pass
        return {"processed": [], "skipped": []}


def directory_peers_for_app(app: Any) -> dict[str, str]:
    """Return the snapshot-derived dial set without a per-tick OrgFS read."""

    actor = getattr(app, "_peer_access_actor", None)
    snapshot = actor.snapshot() if actor is not None else None
    if snapshot is not None:
        # Join writes its invite-authorized bootstrap peer after a snapshot
        # can already exist. Merge that small local file on every tick.
        snapshot = with_bootstrap(snapshot, Path(app.hyprial_home))
    state = getattr(app, "_org_dial_state", None)
    if state is None:
        state = DialState()
        app._org_dial_state = state
    service: Any | None = None
    try:
        if snapshot is None:
            service = _build_service(app)
    except Exception:  # noqa: BLE001 - discovery must never raise
        return {}
    try:
        return dial_peers(
            service,
            snapshot=snapshot,
            state=state,
            log=getattr(app, "_log", None),
            is_live=_host_liveliness(app),
        )
    except Exception as error:  # noqa: BLE001 - degrade to mutual, never to none
        log = getattr(app, "_log", None)
        if log is not None:
            log(
                "warn",
                "org",
                "org.dial_direction.failed",
                errorType=type(error).__name__,
                detail=str(error)[:200],
            )
        if snapshot is not None:
            return dict(snapshot.peers)
        try:
            return service.directory_peers() if service is not None else {}
        except Exception:  # noqa: BLE001 - discovery must never raise
            return {}


def _membership_changed(app: Any, org: str, user: str) -> None:
    """A local membership command: let the owner's actor re-read that user."""

    sync = getattr(app, "_casdoor_membership_sync", None)
    if sync is not None:
        sync.user_changed(user, org)


def start_casdoor_membership_sync(app: Any) -> None:
    """Start the owner-side membership actor and its webhook (design §3).

    Nothing starts on a node whose settings name no Casdoor credential.  A
    webhook that cannot bind is logged; the start-up pass and the backstop
    still run.  Never raises: daemon start must not depend on Casdoor.
    """

    def log(level: str, domain: str, event: str, **fields: Any) -> None:
        app._log(level, domain, event, **fields)

    try:
        credential_file = casdoor_credential_file(Path(app.hyprial_home))
    except CasdoorConfigError as config:
        log("warn", "org", "org.sync.unavailable", code=config.error.code)
        return
    if credential_file is None:
        return
    sync = CasdoorMembershipSync(
        service_factory=lambda: _build_service(app),
        confirmed=ConfirmedMembers(
            Path(app.hyprial_home) / "org-sync" / "confirmed-members.json"
        ),
        log=log,
    )
    app._casdoor_membership_sync = sync
    sync.start()
    try:
        settings = webhook_settings(credential_file)
    except CasdoorGroupError as error:
        log("warn", "org", "org.sync.webhook-unavailable", code=error.code)
        return
    if settings is None:
        log("info", "org", "org.sync.webhook-unconfigured")
        return

    def on_trigger(trigger: Trigger) -> None:
        if trigger.kind == "user" and trigger.name:
            sync.user_changed(trigger.name)
        else:
            sync.reconcile(trigger.name if trigger.kind == "org" else None)

    try:
        receiver = CasdoorWebhookReceiver(settings, on_trigger, log=log)
    except OSError as error:
        log("warn", "org", "org.sync.webhook-unavailable", code=type(error).__name__)
        return
    receiver.start()
    app._casdoor_webhook = receiver


def stop_casdoor_membership_sync(app: Any) -> None:
    receiver = getattr(app, "_casdoor_webhook", None)
    app._casdoor_webhook = None
    sync = getattr(app, "_casdoor_membership_sync", None)
    app._casdoor_membership_sync = None
    failures: list[str] = []
    if receiver is not None and not receiver.close():
        failures.append("webhook listener")
    if sync is not None and not sync.stop():
        failures.append("membership actor")
    if failures:
        raise RuntimeError(f"org Casdoor sync did not stop: {', '.join(failures)}")


def _host_liveliness(app: Any):  # type: ignore[no-untyped-def]
    """``device id -> currently live?`` from OrgFS host liveliness, if running."""

    runtime = getattr(app, "_orgfs_runtime", None)
    if runtime is None:
        return lambda _device: False
    return lambda device: device in getattr(runtime, "_lively_peers", ())


def _access_changed(app: Any, reason: str, result: Any) -> Any:
    """Ask the peer-access actor to rebuild after a local org mutation."""

    actor = getattr(app, "_peer_access_actor", None)
    if actor is not None:
        actor.request(reason)
    return result


def _reissue_conflicting_identity(app: Any, reason: str, token: str) -> None:
    from hyprial.daemon.impl.network.tailcat import reissue_device_key
    from hyprial.identity import consume_rotation_event

    record = reissue_device_key(app.hyprial_home, reason=reason, conflict_token=token)
    if record is None:
        return
    event = consume_rotation_event(app.hyprial_home)
    app._log(
        "error",
        "org",
        "org.access.identity-reissued",
        **(event or {"deviceId": record.device_id, "reason": reason}),
    )
    supervisor = getattr(app, "_forwarding_supervisor", None)
    if supervisor is not None:
        supervisor.restart(reason=reason)


def start_peer_access_for_app(app: Any) -> PeerAccessActor:
    """Start this daemon's directory-driven allow/dial/leave actor."""

    actor = PeerAccessActor(
        service_factory=lambda: _build_service(app),
        facade=app._require_orgfs_runtime().facade,
        supervisor=lambda: getattr(app, "_forwarding_supervisor", None),
        log=lambda level, domain, event, **fields: app._log(
            level, domain, event, **fields
        ),
        request_redial=lambda: app._reconcile_forwarding_endpoints(),
        process_leaves=lambda: _build_service(app).process_leave_requests(),
        reissue_identity=lambda reason, token: _reissue_conflicting_identity(
            app, reason, token
        ),
    )
    app._peer_access_actor = actor
    actor.start()
    return actor


__all__ = [
    "directory_peers_for_app",
    "ipc_org_acl_grant",
    "ipc_org_acl_ls",
    "ipc_org_acl_revoke",
    "ipc_org_create",
    "ipc_org_delete",
    "ipc_org_execute",
    "ipc_org_invite",
    "ipc_org_join",
    "ipc_org_leave",
    "ipc_org_list",
    "ipc_org_network",
    "ipc_org_remove",
    "ipc_org_repair",
    "publish_self_for_app",
    "start_casdoor_membership_sync",
    "start_peer_access_for_app",
    "stop_casdoor_membership_sync",
]
