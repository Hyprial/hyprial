# Decision record (provider-key contract freeze r3 §8, docs/design/provider-key-contract-freeze-2026-11-03.md)
# - Single entry point for the four legacy agent.secret.* names; the router/registry
#   handler calls dispatch() first and runs the legacy code unchanged on NOT_HANDLED.
# - Gate (§8.7): NOT_HANDLED is decided by the gate value alone, before any param is
#   read: only an absent file/section/key or the exact value "legacy" stays legacy. A
#   damaged gate file, and every owner-context failure behind an open gate (store,
#   anchor or signer unavailable included), refuses through the per-method mapping, so
#   an open gate never falls back to the plaintext files or agent_secret_grants. The
#   gate is read only through the single reader storage.gate.authority, shared with the
#   launch path and the seven methods. An injected test service counts as open.
# - Routing: grant only for source=user-provider; revoke only when the slot has a
#   new-authority record; provider-write and list always (§8.1, §8.6).
# - Single authority (§8.2): nothing is written to H/secrets/providers or to the
#   agent_secret_grants table. A legacy row whose (agent, grantId) slot has any
#   new-authority record is ignored in list (all sources, matching launch shadowing).
#   "Any record" is launch.slot_ids: verified or not (§8.4 "record fails verification":
#   no projection, no fallback to the old table). Revoke of such a slot goes to the
#   authority, which refuses an unverifiable record; a re-grant overwrites it.
# - A registry agent that is not owner-local (received by
#   transfer for another owner) can hold no slot of this owner: an actor-scoped list
#   returns its legacy rows, checking expectedAgent against its registry snapshot,
#   instead of reporting AGENT_NOT_FOUND for an agent that exists (§8.1).
# - Replaying an identical awaiting-key grant re-checks for the key: when one now
#   exists (an earlier completion failed), the replay completes the slot (§4.3).
# - Incarnation (§8.3): expectedAgent shape rules copied from #2328; every read and
#   write targets the AgentRef snapshot; grant re-resolves before writing and refuses
#   with AGENT_INCARNATION_MISMATCH when the entity_token changed.
# - Projection (§8.4) is computed from signed grant-state records only (legacyRef):
#   no local index exists, so a lost state directory reproduces identical rows.
#   Replay of equivalent content is idempotent; other content must raise the revision
#   (legacy monotonic rule, AGENT_ERROR) unless the slot is revoked.
# - Concurrency: IPC requests run on their own threads, so every check-then-act runs
#   under an in-process lock: (entryId, fieldName) around find + login/rotate (legacyRef
#   stays unique per owner), and (agent, grantId) around the revision/replay check and
#   the slot write or revoke. grant takes the slot lock before the entry lock; nothing
#   takes them in the other order. One daemon owns a home, so in-process is enough.
# - Callers: owner-signed writes (provider-write, user-provider grant, revoke of a held
#   slot) refuse an IPC peer matched to an interactive session, as §5 preauthorize does
#   for the new methods; reads and legacy-routed calls keep the legacy caller rule.
# - Errors (§8.5): new-authority failures become AGENT_SECRET_RESOLUTION_FAILED
#   (provider-write, grant, revoke) or AGENT_ERROR (list) with fixed messages.
#   INVALID_ARGUMENT, AGENT_NOT_FOUND, AGENT_INCARNATION_MISMATCH and AGENT_ERROR pass
#   through; any other exception (raw OS/store errors included) gets the fixed message.
"""Legacy agent.secret.* adapter onto the owner-held provider-key authority (contract §8)."""
from __future__ import annotations

import logging
import re
import threading
from contextlib import contextmanager
from typing import Any, Iterator, Mapping

from hyprial.daemon.impl.ipc.params import (
    _optional_positive_integer,
    _optional_string_param,
    _required_string,
)
from hyprial.identity import AgentError, secret_environment_name_is_allowed
from hyprial.kernel import DaemonRequestError, ipc_errors

from .api import AgentRef, JsonObject
from .launch import slot_ids
from .storage import gate

LEGACY_SECRET_METHODS = frozenset({
    "agent.secret.provider-write", "agent.secret.grant", "agent.secret.list", "agent.secret.revoke",
})

USER_PROVIDER = "user-provider"
AGENT_ERROR = AgentError.code
MAX_ENVIRONMENT_NAMES = 8
MAX_LABEL = 128
REFUSED = "provider key authority refused the request"
LIST_UNAVAILABLE = "provider key authority unavailable"

_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PASSTHROUGH = frozenset({
    ipc_errors.INVALID_ARGUMENT, ipc_errors.AGENT_NOT_FOUND,
    ipc_errors.AGENT_INCARNATION_MISMATCH, AGENT_ERROR,
})
_LOG = logging.getLogger(__name__)
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[str, ...], threading.Lock] = {}


class _NotHandled:
    def __repr__(self) -> str:
        return "NOT_HANDLED"


NOT_HANDLED: Any = _NotHandled()


class _Refusing:
    """Service stand-in for an open or damaged gate whose owner context failed: every call re-raises."""

    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __getattr__(self, name: str) -> Any:
        def refuse(*args: Any, **kwargs: Any) -> Any:
            raise self._error
        return refuse


def dispatch(app: Any, method: str, params: Mapping[str, Any]) -> Any:
    """Answer a legacy agent.secret.* call from the new authority, or return NOT_HANDLED."""
    if method not in LEGACY_SECRET_METHODS:
        return NOT_HANDLED
    service = _open_service(app)
    if service is None:
        return NOT_HANDLED
    if method == "agent.secret.provider-write":
        return _write(app, service, params)
    if method == "agent.secret.grant":
        return _grant(app, service, params)
    if method == "agent.secret.list":
        return _list(app, service, params)
    return _revoke(app, service, params)


def _gate_closed(app: Any) -> bool:
    """True only for the "legacy" gate; a damaged ``settings.json`` raises instead."""
    if getattr(app, "_provider_key_service", None) is not None:
        return False
    return gate.authority(getattr(app, "hyprial_home", None)) == gate.LEGACY


def _open_service(app: Any) -> Any:
    from .context import service_for

    try:
        if _gate_closed(app):
            return None
        return service_for(app)
    except Exception as error:
        return _Refusing(error)


@contextmanager
def _serialized(*key: str) -> Iterator[None]:
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


def _refuse_interactive(app: Any) -> None:
    peer_pid = getattr(getattr(app, "_ipc_peer", None), "pid", None)
    if peer_pid is None:
        return
    matcher = getattr(app, "_interactive_identity_binding_peer", None)
    if matcher is None or matcher(peer_pid):
        raise DaemonRequestError(ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, REFUSED)


def _resolve(app: Any, actor: str, expected: Mapping[str, Any] | None) -> AgentRef:
    from .context import resolve_agent

    return resolve_agent(app, actor, expected)


@contextmanager
def _mapped(code: str, message: str) -> Iterator[None]:
    try:
        yield
    except DaemonRequestError as error:
        if error.code in _PASSTHROUGH:
            raise
        raise DaemonRequestError(code, message) from error
    except AgentError:
        raise
    except Exception as error:
        raise DaemonRequestError(code, message) from error


def _invalid(message: str) -> DaemonRequestError:
    return DaemonRequestError(ipc_errors.INVALID_ARGUMENT, message)


def _segment(value: str, message: str) -> str:
    if not _SEGMENT.fullmatch(value):
        raise _invalid(message)
    return value


def _expected_agent(value: object) -> dict[str, Any]:
    if (not isinstance(value, dict) or set(value) != {"uri", "createdAtMs"}
            or not isinstance(value["uri"], str) or not value["uri"]
            or type(value["createdAtMs"]) is not int or value["createdAtMs"] < 0):
        raise _invalid("invalid expectedAgent")
    return {"uri": value["uri"], "createdAtMs": value["createdAtMs"]}


def _environment_names(raw: object) -> list[str]:
    if not isinstance(raw, list) or not raw or any(not isinstance(item, str) or not item for item in raw):
        raise _invalid("environmentNames must be a non-empty string array")
    if len(raw) > MAX_ENVIRONMENT_NAMES:
        raise _invalid("secret environment names are limited to 8")
    if any(not secret_environment_name_is_allowed(name) for name in raw):
        raise _invalid("secret environment names must be valid unreserved env names")
    if len(set(raw)) != len(raw):
        raise _invalid("secret environment names must be unique")
    return list(raw)


def _slot(records: list[JsonObject], ref: AgentRef, grant_id: str) -> JsonObject | None:
    return next((record for record in records
                 if record.get("agent") == ref.agent and record.get("grantId") == grant_id), None)


def _row(actor: str, record: Mapping[str, Any]) -> JsonObject:
    legacy_ref = record["legacyRef"]
    return {
        "actor": actor, "grantId": record["grantId"], "source": USER_PROVIDER,
        "entryId": legacy_ref["entryId"], "fieldName": legacy_ref["fieldName"],
        "environmentNames": list(record["environmentNames"]), "revision": legacy_ref["revision"],
        "ownerActor": None, "ownerEntityToken": None,
    }


def _legacy_row(grant: Any) -> JsonObject:
    return {
        "actor": grant.actor, "grantId": grant.grant_id, "source": grant.source.value,
        "entryId": grant.entry_id, "fieldName": grant.field_name,
        "environmentNames": list(grant.environment_names), "revision": grant.revision,
        "ownerActor": grant.owner_actor, "ownerEntityToken": grant.owner_entity_token,
    }


def _write(app: Any, service: Any, params: Mapping[str, Any]) -> JsonObject:
    entry_id = _required_string(params.get("entryId"), "entryId")
    field_name = _required_string(params.get("fieldName"), "fieldName")
    value = params.get("value")
    if not isinstance(value, str) or not value:
        raise _invalid("secret model-vendor value must be non-empty")
    if "\n" in value or "\r" in value:
        raise _invalid("secret model-vendor value must contain one line")
    _segment(entry_id, "secret entry id is not a safe segment")
    _segment(field_name, "secret field name is not a safe key")
    _refuse_interactive(app)
    with _mapped(ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, REFUSED), _serialized("entry", entry_id, field_name):
        key = service.find_key_by_legacy_ref(entry_id=entry_id, field_name=field_name)
        if key is None:
            service.login(provider=entry_id, kind="api-key", secret=value,
                          label=f"legacy:{entry_id}/{field_name}"[:MAX_LABEL],
                          legacy_ref={"entryId": entry_id, "fieldName": field_name})
        else:
            service.rotate(key_id=key["keyId"], replace_secret=value)
    return {"ok": True, "entryId": entry_id, "fieldNames": [field_name]}


def _grant(app: Any, service: Any, params: Mapping[str, Any]) -> Any:
    if params.get("source") != USER_PROVIDER:
        return NOT_HANDLED
    actor = _required_string(params.get("actor"), "actor")
    grant_id = _required_string(params.get("grantId"), "grantId")
    entry_id = _required_string(params.get("entryId"), "entryId")
    field_name = _required_string(params.get("fieldName"), "fieldName")
    names = _environment_names(params.get("environmentNames"))
    revision = _optional_positive_integer(params.get("revision"), "revision")
    if revision is None:
        raise _invalid("revision is required")
    expected = _expected_agent(params["expectedAgent"]) if "expectedAgent" in params else None
    _segment(grant_id, "secret grant id is not a safe segment")
    _segment(entry_id, "secret entry id is not a safe segment")
    _segment(field_name, "secret field name is not a safe key")
    if _optional_string_param(params.get("ownerActor"), "ownerActor") is not None:
        raise _invalid("only shared secret grants can name an owner")
    legacy_ref = {"entryId": entry_id, "fieldName": field_name, "revision": revision}
    _refuse_interactive(app)
    with _mapped(ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, REFUSED):
        ref = _resolve(app, actor, expected)
        with _serialized("slot", ref.agent, grant_id):
            current = _slot(service.grants_for(ref), ref, grant_id)
            replay = False
            if current is not None and current.get("state") != "revoked" and current.get("legacyRef"):
                if current["legacyRef"] == legacy_ref and list(current["environmentNames"]) == names:
                    if current.get("state") != "awaiting-key":
                        return {"ok": True, "grant": _row(ref.actor, current)}
                    replay = True
                elif revision <= current["legacyRef"]["revision"]:
                    raise DaemonRequestError(AGENT_ERROR, "secret grant revision must increase")
            with _serialized("entry", entry_id, field_name):
                key = service.find_key_by_legacy_ref(entry_id=entry_id, field_name=field_name)
                if replay and key is None:
                    return {"ok": True, "grant": _row(ref.actor, current)}
                if _resolve(app, actor, expected).entity_token != ref.entity_token:
                    raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH,
                                             "target agent incarnation changed before secret grant")
                if key is None:
                    written = service.put_awaiting(agent=ref, grant_id=grant_id, provider=entry_id,
                                                   environment_names=names, legacy_ref=legacy_ref)
                else:
                    written = service.allow(agent=ref, provider=key["provider"], key_id=key["keyId"],
                                            environment_names=names, replace=True, grant_id=grant_id,
                                            legacy_ref=legacy_ref)
            return {"ok": True, "grant": _row(ref.actor, written["grant"])}


def _list(app: Any, service: Any, params: Mapping[str, Any]) -> JsonObject:
    actor = params.get("actor")
    if actor is not None and (not isinstance(actor, str) or not actor):
        raise _invalid("actor must be a non-empty string")
    raw_expected = params.get("expectedAgent")
    expected = None
    if raw_expected is not None:
        if actor is None:
            raise _invalid("invalid expectedAgent")
        expected = _expected_agent(raw_expected)
    held: dict[str, tuple[AgentRef, list[JsonObject], frozenset[str]]] = {}
    with _mapped(AGENT_ERROR, LIST_UNAVAILABLE):
        for name in [actor] if actor is not None else [agent.actor for agent in app.agents.list()]:
            ref = _list_ref(app, name, expected, scoped=actor is not None)
            if ref is None:
                continue
            records = [record for record in service.grants_for(ref) if record.get("agent") == ref.agent]
            held[ref.actor] = (ref, records, slot_ids(service, ref, records))
    rows: list[JsonObject] = []
    inventory = app._agent_session_domains.agent
    grants = (inventory.read_secret_inventory(
        actor, expected_uri=expected["uri"], expected_created_at_ms=expected["createdAtMs"],
    ) if expected is not None else inventory.read_secret_inventory(actor))
    for grant in grants:
        ref, records, ids = held.get(grant.actor, (None, [], frozenset()))
        if expected is not None and ref is not None and grant.entity_token != ref.entity_token:
            raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH,
                                     "secret inventory belongs to another incarnation")
        if ref is not None and grant.grant_id in ids:
            _LOG.warning("legacy secret grant shadowed by the owner authority: actor=%s grantId=%s",
                         grant.actor, grant.grant_id)
            continue
        rows.append(_legacy_row(grant))
    for ref, records, _ in held.values():
        rows.extend(_row(ref.actor, record) for record in records
                    if record.get("legacyRef") and record.get("state") != "revoked")
    rows.sort(key=lambda row: (row["actor"], row["grantId"]))
    return {"ok": True, "grants": rows}


def _list_ref(app: Any, name: str, expected: Mapping[str, Any] | None, *, scoped: bool) -> AgentRef | None:
    """The owner-local snapshot to list; None for an agent that keeps only its legacy rows."""
    from .context import non_local_agent

    try:
        return _resolve(app, name, expected)
    except DaemonRequestError as error:
        if error.code not in (ipc_errors.AGENT_NOT_FOUND, ipc_errors.AGENT_INCARNATION_MISMATCH):
            raise
        foreign = non_local_agent(app, name)
        if foreign is None:
            if not scoped and error.code == ipc_errors.AGENT_NOT_FOUND:
                return None
            raise
    if expected is not None and (foreign.uri != expected["uri"]
                                 or foreign.created_at_ms != expected["createdAtMs"]):
        raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH,
                                 "target agent incarnation does not match expectedAgent")
    return None


def _revoke(app: Any, service: Any, params: Mapping[str, Any]) -> Any:
    actor, grant_id = params.get("actor"), params.get("grantId")
    if not (isinstance(actor, str) and actor and isinstance(grant_id, str) and grant_id):
        return NOT_HANDLED
    with _mapped(ipc_errors.AGENT_SECRET_RESOLUTION_FAILED, REFUSED):
        try:
            ref = _resolve(app, actor, None)
        except DaemonRequestError as error:
            if error.code == ipc_errors.AGENT_NOT_FOUND:
                return NOT_HANDLED
            raise
        with _serialized("slot", ref.agent, grant_id):
            records = [record for record in service.grants_for(ref) if record.get("agent") == ref.agent]
            if grant_id not in slot_ids(service, ref, records):
                return NOT_HANDLED
            _refuse_interactive(app)
            result = service.revoke(agent=ref, grant_id=grant_id)
    return {"ok": True, "revoked": bool(result["changed"])}
