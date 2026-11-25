# Decision record (contract freeze r3 §3.3, §4.3, §5.2, §7.3, §8.3, §9.5; impl spec B1)
# - One grant-state record per (agent, grantId) slot; the new API slot is the
#   provider. allow requires an active meta whose envelope binds and this device as
#   a recipient, signs an E1 grant (expiresAt = issuedAt + GRANT_TTL_MS) and CASes
#   the slot to active. Equivalent content (same keyId, names, legacyRef, current
#   dekVersion and this device as recipient) does not raise seq. Another key or an
#   awaiting-key slot needs replace (PROVIDER_CREDENTIAL_EXISTS). The registry must
#   still hold the resolved incarnation right before the write (§8.3).
# - revoke: CAS to revoked (grant null, keyId kept); already revoked succeeds with
#   no write; a slot without any record is PROVIDER_GRANT_MISSING. restart reports
#   not-running only when liveness observed the agent not running, else
#   not-supported (no controlled restart this phase).
# - status is read-only and does no freshness check: it verifies the records with
#   check_access at the grant's own issuedAt and unwraps this device's DEK without
#   decrypting the payload; every failure is a per-slot state and code.
# - launch_secrets fails closed: any grant file of the agent that cannot be
#   verified, an awaiting-key slot or any active-slot refusal raises. Revoked slots
#   are held (they shadow the legacy grant of the same id) but not injected and do
#   not fail the launch. A refused launch appends only the refused slot's receipt;
#   injected and revoked-slot receipts are appended only after every slot passed,
#   so a refused launch never leaves an injected row or a lastUsedAt. A receipt
#   failure is logged without values and never changes the result.
# - Renewal (§9.5, F-5): on this owner device a grant that is expired or past its
#   half-life is re-signed (seq+1, issuedAt = launch time) before use. When signing
#   is unavailable (U1 refusal, or a signing key the anchor does not verify) an
#   expired grant refuses with PROVIDER_KEY_RECIPIENT_PENDING (wait for the owner
#   device), while an unexpired one is used as is. An anchor that is unavailable is
#   PROVIDER_STORE_UNAVAILABLE (§9.5.3), never a waiting code. Half-life renewal is
#   opportunistic: a renewal that loses every compare-and-set uses the still-valid
#   grant, and for an expired grant it is RECIPIENT_PENDING, because
#   PROVIDER_STORE_CONFLICT never appears in a launch refusal (§7.1). Renewal keeps
#   the grant's dekVersion: a grant left behind by an unfinished rotate stays refused.
# - Every object is verified with the anchor-verified owner key its own sealedBy names
#   (§8.4: records stay valid across an owner signing-key rotation): the envelope with
#   the key that sealed it, the grant with the key that signed it. A renewal re-signs
#   only the grant, so after a U1 key change the two keys legitimately differ.
# - A refusal over plaintext (undecodable bytes) is raised after its except block
#   ends, so no exception holding the value is chained as __context__.
"""Grant slots: allow, awaiting-key, revoke, status, renewal and launch unsealing."""
from __future__ import annotations

import logging
from typing import Any, Mapping, Sequence

from hyprial.identity import ProviderKeys, UserKey
from hyprial.kernel import DaemonRequestError, ipc_errors
from ..api import GRANT_TTL_MS, AgentRef, LaunchSecret
from ..storage import InvalidReceipt, append_receipt, grant_id_from_name, grant_path
from .records import (
    GRANT_STATE_TYPE, INVALID, UNAVAILABLE, OwnerContext, Signer, commit, grant_states, invalid_argument,
    key_ids, key_lock, owner_key, read_envelope, read_meta, read_record, refuse, slot_files, slot_lock,
)

_LOG = logging.getLogger(__name__)
_STATUS_STATE = {
    ipc_errors.PROVIDER_GRANT_MISSING: "missing",
    ipc_errors.PROVIDER_GRANT_REVOKED: "revoked",
    ipc_errors.PROVIDER_KEY_NOT_FOUND: "key-not-found",
    ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING: "recipient-pending",
    ipc_errors.PROVIDER_KEY_UNSEAL_FAILED: "unseal-failed",
    INVALID: "signature-invalid",
    UNAVAILABLE: "unavailable",
}


def _local_recipient_id(ctx: OwnerContext) -> str | None:
    try:
        return ctx.sealed.recipient().key_id
    except UserKey.Error:
        return None


def _sign_grant(ctx: OwnerContext, signer: Signer, *, meta: Mapping[str, Any], agent: str,
                recipient_key_id: str, dek_version: int, issued_at: int) -> dict:
    payload = {"v": 1, "provider": meta["provider"], "keyId": meta["keyId"], "owner": ctx.owner,
               "agent": agent, "dekVersion": dek_version, "recipientKeyId": recipient_key_id,
               "issuedAt": issued_at, "expiresAt": issued_at + GRANT_TTL_MS}
    return ProviderKeys.sign_grant(payload, signer=signer())


def _active_state(ctx: OwnerContext, previous: Mapping[str, Any] | None, *, agent: str, grant_id: str,
                  meta: Mapping[str, Any], grant: dict, names: Sequence[str],
                  legacy_ref: Mapping[str, Any] | None) -> dict:
    return {"type": GRANT_STATE_TYPE, "v": 1, "owner": ctx.owner, "agent": agent, "grantId": grant_id,
            "provider": meta["provider"], "state": "active",
            "seq": 1 if previous is None else previous["seq"] + 1, "keyId": meta["keyId"], "grant": grant,
            "environmentNames": list(names), "legacyRef": None if legacy_ref is None else dict(legacy_ref),
            "updatedAt": ctx.now(previous)}


def _legacy_slot_ref(value: Mapping[str, Any] | None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"entryId", "fieldName", "revision"}:
        raise invalid_argument("legacyRef must be {entryId, fieldName, revision}")
    return {"entryId": value["entryId"], "fieldName": value["fieldName"], "revision": value["revision"]}


def grant_view(ref: AgentRef, state: Mapping[str, Any]) -> dict:
    payload = state["grant"]["payload"] if state["grant"] is not None else None
    return {
        "agent": state["agent"], "agentId": ref.agent_id, "actor": ref.actor, "grantId": state["grantId"],
        "provider": state["provider"], "owner": state["owner"], "keyId": state["keyId"],
        "state": state["state"], "seq": state["seq"],
        "dekVersion": None if payload is None else payload["dekVersion"],
        "issuedAt": None if payload is None else payload["issuedAt"],
        "expiresAt": None if payload is None else payload["expiresAt"],
        "environmentNames": list(state["environmentNames"]),
        "recipientKeyId": None if payload is None else payload["recipientKeyId"],
        "legacyRef": None if state["legacyRef"] is None else dict(state["legacyRef"]),
    }


def _single_active_key(ctx: OwnerContext, provider: str) -> str:
    found = []
    for key_id in key_ids(ctx):
        try:
            meta = read_meta(ctx, key_id)
        except ProviderKeys.Error as error:
            if error.code in (INVALID, ipc_errors.INVALID_ARGUMENT):
                continue
            raise
        if meta is not None and meta["state"] == "active" and meta["provider"] == provider:
            found.append(key_id)
    if not found:
        raise refuse(ipc_errors.PROVIDER_KEY_NOT_FOUND)
    if len(found) > 1:
        raise invalid_argument("several active keys for this provider; give keyId")
    return found[0]


def _equivalent(previous: Mapping[str, Any], meta: Mapping[str, Any], names: Sequence[str],
                legacy_ref: Mapping[str, Any] | None, recipient_key_id: str) -> bool:
    payload = previous["grant"]["payload"]
    return (previous["keyId"] == meta["keyId"] and previous["provider"] == meta["provider"]
            and list(previous["environmentNames"]) == list(names) and previous["legacyRef"] == legacy_ref
            and payload["dekVersion"] == meta["dekVersion"] and payload["recipientKeyId"] == recipient_key_id)


def _require_current(ctx: OwnerContext, ref: AgentRef) -> None:
    if not ctx.current(ref):
        raise DaemonRequestError(ipc_errors.AGENT_INCARNATION_MISMATCH,
                                 "target agent incarnation changed before the grant write")


def allow(ctx: OwnerContext, ref: AgentRef, *, provider: str, key_id: str | None,
          environment_names: Sequence[str] | None, replace: bool, grant_id: str | None,
          legacy_ref: Mapping[str, Any] | None) -> tuple[dict, bool]:
    slot = grant_id or provider
    reference = _legacy_slot_ref(legacy_ref)
    signer = Signer(ctx, "provider-key/allow")
    key_id = key_id or _single_active_key(ctx, provider)
    with key_lock(ctx, key_id):
        meta = read_meta(ctx, key_id)
        if meta is None or meta["state"] != "active" or meta["provider"] != provider:
            raise refuse(ipc_errors.PROVIDER_KEY_NOT_FOUND)
        read_envelope(ctx, meta)
        recipient = _local_recipient_id(ctx)
        if recipient is None or recipient not in {row["recipientKeyId"] for row in meta["recipients"]}:
            raise refuse(ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING)
        names = list(environment_names) if environment_names else list(meta["environmentNames"])

        def build(previous: dict | None) -> dict | None:
            if previous is not None and previous["state"] != "revoked":
                if previous["keyId"] != key_id and not replace:
                    raise refuse(ipc_errors.PROVIDER_CREDENTIAL_EXISTS)
                if previous["state"] == "active" and _equivalent(previous, meta, names, reference, recipient):
                    return None
            _require_current(ctx, ref)
            now = ctx.now(previous)
            grant = _sign_grant(ctx, signer, meta=meta, agent=ref.agent, recipient_key_id=recipient,
                                dek_version=meta["dekVersion"], issued_at=now)
            return _active_state(ctx, previous, agent=ref.agent, grant_id=slot, meta=meta, grant=grant,
                                 names=names, legacy_ref=reference)

        with slot_lock(ctx, ref.agent, slot):
            state, changed = commit(ctx, grant_path(ref.agent, slot), GRANT_STATE_TYPE, build, signer,
                                    regrant=True)
    return grant_view(ref, state), changed


def put_awaiting(ctx: OwnerContext, ref: AgentRef, *, grant_id: str, provider: str,
                 environment_names: Sequence[str], legacy_ref: Mapping[str, Any]) -> tuple[dict, bool]:
    reference = _legacy_slot_ref(legacy_ref)
    if reference is None:
        raise invalid_argument("an awaiting-key grant needs legacyRef")
    names = list(environment_names)
    signer = Signer(ctx, "provider-key/awaiting")

    def build(previous: dict | None) -> dict | None:
        if (previous is not None and previous["state"] == "awaiting-key" and previous["provider"] == provider
                and previous["legacyRef"] == reference and list(previous["environmentNames"]) == names):
            return None
        _require_current(ctx, ref)
        return {"type": GRANT_STATE_TYPE, "v": 1, "owner": ctx.owner, "agent": ref.agent, "grantId": grant_id,
                "provider": provider, "state": "awaiting-key",
                "seq": 1 if previous is None else previous["seq"] + 1, "keyId": None, "grant": None,
                "environmentNames": names, "legacyRef": reference, "updatedAt": ctx.now(previous)}

    with slot_lock(ctx, ref.agent, grant_id):
        state, changed = commit(ctx, grant_path(ref.agent, grant_id), GRANT_STATE_TYPE, build, signer,
                                regrant=True)
    return grant_view(ref, state), changed


def _revoked(ctx: OwnerContext, previous: Mapping[str, Any]) -> dict:
    return {**previous, "state": "revoked", "seq": previous["seq"] + 1, "grant": None,
            "updatedAt": ctx.now(previous)}


def revoke(ctx: OwnerContext, ref: AgentRef, *, grant_id: str) -> tuple[dict, bool, str]:
    signer = Signer(ctx, "provider-key/revoke")

    def build(previous: dict | None) -> dict | None:
        if previous is None:
            raise refuse(ipc_errors.PROVIDER_GRANT_MISSING)
        return None if previous["state"] == "revoked" else _revoked(ctx, previous)

    with slot_lock(ctx, ref.agent, grant_id):
        state, changed = commit(ctx, grant_path(ref.agent, grant_id), GRANT_STATE_TYPE, build, signer)
    return grant_view(ref, state), changed, "not-running" if ctx.running(ref) is False else "not-supported"


def complete_awaiting(ctx: OwnerContext, meta: Mapping[str, Any], recipient: Any, signer: Signer) -> int:
    """Turn every awaiting-key slot waiting for this key's legacyRef active; never raises."""
    wanted = meta["legacyRef"]
    completed = 0
    try:
        waiting = [state for state in grant_states(ctx) if state["state"] == "awaiting-key"
                   and {"entryId": state["legacyRef"]["entryId"],
                        "fieldName": state["legacyRef"]["fieldName"]} == wanted]
    except Exception:
        _LOG.warning("awaiting-key completion could not scan grant states")
        return 0
    for found in waiting:
        def build(previous: dict | None, found: dict = found) -> dict | None:
            if previous is None or previous["state"] != "awaiting-key" or previous["legacyRef"] != found["legacyRef"]:
                return None
            grant = _sign_grant(ctx, signer, meta=meta, agent=previous["agent"], recipient_key_id=recipient.key_id,
                                dek_version=meta["dekVersion"], issued_at=ctx.now(previous))
            return _active_state(ctx, previous, agent=previous["agent"], grant_id=previous["grantId"], meta=meta,
                                 grant=grant, names=previous["environmentNames"], legacy_ref=previous["legacyRef"])

        try:
            with slot_lock(ctx, found["agent"], found["grantId"]):
                _, changed = commit(ctx, grant_path(found["agent"], found["grantId"]), GRANT_STATE_TYPE,
                                    build, signer)
            completed += int(changed)
        except Exception:
            _LOG.warning("one awaiting-key grant could not be completed")
    return completed


def reissue(ctx: OwnerContext, meta: Mapping[str, Any], recipient: Any, signer: Signer) -> int:
    """Re-sign every active grant of this key on the meta's current dekVersion."""
    reissued = 0
    for found in list(grant_states(ctx)):
        if found["state"] != "active" or found["keyId"] != meta["keyId"]:
            continue

        def build(previous: dict | None) -> dict | None:
            if (previous is None or previous["state"] != "active" or previous["keyId"] != meta["keyId"]
                    or previous["grant"]["payload"]["dekVersion"] >= meta["dekVersion"]):
                return None
            grant = _sign_grant(ctx, signer, meta=meta, agent=previous["agent"], recipient_key_id=recipient.key_id,
                                dek_version=meta["dekVersion"], issued_at=ctx.now(previous))
            return {**previous, "seq": previous["seq"] + 1, "grant": grant, "updatedAt": ctx.now(previous)}

        with slot_lock(ctx, found["agent"], found["grantId"]):
            _, changed = commit(ctx, grant_path(found["agent"], found["grantId"]), GRANT_STATE_TYPE, build, signer)
        reissued += int(changed)
    return reissued


def revoke_key(ctx: OwnerContext, key_id: str, signer: Signer) -> list[dict]:
    """Revoke every active grant of one key (logout cascade); return the §5.2 rows."""
    agents = ctx.local_agents()
    rows = []
    for found in list(grant_states(ctx)):
        if found["state"] != "active" or found["keyId"] != key_id:
            continue

        def build(previous: dict | None) -> dict | None:
            if previous is None or previous["state"] != "active" or previous["keyId"] != key_id:
                return None
            return _revoked(ctx, previous)

        with slot_lock(ctx, found["agent"], found["grantId"]):
            _, changed = commit(ctx, grant_path(found["agent"], found["grantId"]), GRANT_STATE_TYPE, build, signer)
        if changed:
            rows.append({"agent": found["agent"], "agentId": agents.get(found["agent"]),
                         "grantId": found["grantId"], "provider": found["provider"]})
    rows.sort(key=lambda row: (row["agent"], row["grantId"]))
    return rows


def _agent_records(ctx: OwnerContext, ref: AgentRef, *, strict: bool) -> list[tuple[str, dict | None]]:
    """(grantId, verified state) for every slot file of this incarnation; strict raises on any bad file."""
    found = []
    for name in slot_files(ctx, ref.agent):
        grant_id = grant_id_from_name(name)
        if grant_id is None:
            if strict:
                raise refuse(INVALID)
            continue
        try:
            state, _ = read_record(ctx, grant_path(ref.agent, grant_id), GRANT_STATE_TYPE)
        except ProviderKeys.Error as error:
            if strict or error.code != INVALID:
                raise
            continue
        if state is not None:
            found.append((grant_id, state))
    return found


def grants_for(ctx: OwnerContext, ref: AgentRef) -> list[dict]:
    return [state for _, state in _agent_records(ctx, ref, strict=False)]


def slot_names(ctx: OwnerContext, ref: AgentRef) -> frozenset[str]:
    names = set()
    for name in slot_files(ctx, ref.agent):
        names.add(grant_id_from_name(name) or name)
    return frozenset(names)


def _checked(ctx: OwnerContext, ref: AgentRef, state: Mapping[str, Any], *, now: int,
             recipient: str | None) -> tuple[dict, dict, Any]:
    """(meta, envelope, owner key) of an active slot after check_access at `now`."""
    meta = read_meta(ctx, state["keyId"])
    if meta is None or meta["state"] != "active":
        raise refuse(ipc_errors.PROVIDER_KEY_NOT_FOUND)
    envelope, key = read_envelope(ctx, meta)
    if recipient is None:
        raise refuse(ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING)
    ProviderKeys.check_access(
        envelope=envelope, grant=state["grant"], owner_key=key, agent=ref.agent, recipient_key_id=recipient,
        now=now, current_grant_digest=ProviderKeys.record_digest(state["grant"]),
        current_dek_version=meta["dekVersion"], revoked=False,
        grant_owner_key=owner_key(ctx, state["grant"].get("sealedBy")))
    return meta, envelope, key


def _status_row(grant_id: str, provider: str, code: str | None, state: Mapping[str, Any] | None,
                payload: Mapping[str, Any] | None, *, label: str | None = None, unsealable: bool = False) -> dict:
    return {"grantId": grant_id, "provider": provider, "state": label or _STATUS_STATE[code], "code": code,
            "owner": None if state is None else state["owner"], "keyId": None if state is None else state["keyId"],
            "seq": None if state is None else state["seq"],
            "dekVersion": None if payload is None else payload["dekVersion"],
            "expiresAt": None if payload is None else payload["expiresAt"], "unsealable": unsealable}


def _slot_status(ctx: OwnerContext, ref: AgentRef, grant_id: str, state: Mapping[str, Any],
                 recipient: str | None) -> dict:
    payload = state["grant"]["payload"] if state["grant"] is not None else None
    if state["state"] == "revoked":
        return _status_row(grant_id, state["provider"], ipc_errors.PROVIDER_GRANT_REVOKED, state, payload)
    if state["state"] == "awaiting-key":
        return _status_row(grant_id, state["provider"], ipc_errors.PROVIDER_KEY_NOT_FOUND, state, payload,
                           label="awaiting-key")
    try:
        _, envelope, key = _checked(ctx, ref, state, now=payload["issuedAt"], recipient=recipient)
        ctx.sealed.probe_sealed(envelope, owner_key=key)
    except ProviderKeys.Error as error:
        code = error.code if error.code in _STATUS_STATE else INVALID
        return _status_row(grant_id, state["provider"], code, state, payload)
    except UserKey.Error:
        return _status_row(grant_id, state["provider"], ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING, state, payload)
    return _status_row(grant_id, state["provider"], None, state, payload, label="ok", unsealable=True)


def status(ctx: OwnerContext, ref: AgentRef, *, provider: str | None) -> list[dict]:
    recipient = _local_recipient_id(ctx)
    rows = []
    for name in slot_files(ctx, ref.agent):
        grant_id = grant_id_from_name(name)
        if grant_id is None:
            continue
        try:
            state, _ = read_record(ctx, grant_path(ref.agent, grant_id), GRANT_STATE_TYPE)
        except ProviderKeys.Error as error:
            if provider is None or provider == grant_id:
                code = error.code if error.code in (INVALID, UNAVAILABLE) else INVALID
                rows.append(_status_row(grant_id, grant_id, code, None, None))
            continue
        if state is None or (provider is not None and state["provider"] != provider):
            continue
        rows.append(_slot_status(ctx, ref, grant_id, state, recipient))
    if provider is not None and not rows:
        rows.append(_status_row(provider, provider, ipc_errors.PROVIDER_GRANT_MISSING, None, None))
    rows.sort(key=lambda row: row["grantId"])
    return rows


def _receipt(ctx: OwnerContext, ref: AgentRef, state: Mapping[str, Any], at: int, outcome: str) -> None:
    grant = state["grant"]
    row = {"at": at, "owner": state["owner"], "keyId": state["keyId"], "provider": state["provider"],
           "agent": ref.agent, "agentId": ref.agent_id, "deviceUid": ctx.device_uid,
           "dekVersion": None if grant is None else grant["payload"]["dekVersion"],
           "grantDigest": None if grant is None else ProviderKeys.record_digest(grant), "outcome": outcome}
    try:
        append_receipt(ctx.state_dir, row)
    except (OSError, InvalidReceipt, ProviderKeys.Error):
        _LOG.warning("provider key usage receipt could not be appended (%s)", outcome)


def _unrenewed(state: dict, expired: bool) -> dict:
    """The grant as is when it is still valid; an expired one waits for the owner device."""
    if expired:
        raise refuse(ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING)
    return state


def _renewed(ctx: OwnerContext, ref: AgentRef, state: dict, now: int) -> dict:
    payload = state["grant"]["payload"]
    issued, expires = payload["issuedAt"], payload["expiresAt"]
    expired = now >= expires
    if not expired and now < issued + (expires - issued) // 2:
        return state
    signer = Signer(ctx, "provider-key/renew")
    try:
        signer()
    except UserKey.Error:
        return _unrenewed(state, expired)
    except ProviderKeys.Error as error:
        if error.code == UNAVAILABLE:
            raise
        return _unrenewed(state, expired)
    meta = read_meta(ctx, state["keyId"])
    if meta is None or meta["state"] != "active":
        raise refuse(ipc_errors.PROVIDER_KEY_NOT_FOUND)
    digest = ProviderKeys.record_digest(state["grant"])

    def build(previous: dict | None) -> dict | None:
        if (previous is None or previous["state"] != "active" or previous["grant"] is None
                or ProviderKeys.record_digest(previous["grant"]) != digest):
            return None
        grant = _sign_grant(ctx, signer, meta=meta, agent=previous["agent"],
                            recipient_key_id=payload["recipientKeyId"], dek_version=payload["dekVersion"],
                            issued_at=now)
        return {**previous, "seq": previous["seq"] + 1, "grant": grant, "updatedAt": max(now, previous["updatedAt"])}

    try:
        renewed, _ = commit(ctx, grant_path(ref.agent, state["grantId"]), GRANT_STATE_TYPE, build, signer)
    except ProviderKeys.Error as error:
        if error.code != ipc_errors.PROVIDER_STORE_CONFLICT:
            raise
        _LOG.warning("grant renewal lost every compare-and-set before launch")
        return _unrenewed(state, expired)
    if renewed is None:
        raise refuse(ipc_errors.PROVIDER_GRANT_MISSING)
    return renewed


def _unsealed_text(ctx: OwnerContext, envelope: Mapping[str, Any], key: Any) -> str:
    """The plaintext as text; refusals are raised outside the except blocks so no value is chained."""
    try:
        plain = ctx.sealed.open_sealed(envelope, owner_key=key)
    except UserKey.Error:
        plain = None
    if plain is None:
        raise refuse(ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING)
    try:
        value = plain.decode("utf-8")
    except UnicodeDecodeError:
        value = None
    if value is None:
        raise refuse(ipc_errors.PROVIDER_KEY_UNSEAL_FAILED)
    return value


def _launch_slot(ctx: OwnerContext, ref: AgentRef, grant_id: str, now: int, recipient: str | None,
                 outcomes: list[tuple[dict, str]]) -> LaunchSecret | None:
    with slot_lock(ctx, ref.agent, grant_id):
        state, _ = read_record(ctx, grant_path(ref.agent, grant_id), GRANT_STATE_TYPE)
        if state is None:
            raise refuse(ipc_errors.PROVIDER_GRANT_MISSING)
        try:
            if state["state"] == "active":
                state = _renewed(ctx, ref, state, now)
            if state["state"] == "revoked":
                raise refuse(ipc_errors.PROVIDER_GRANT_REVOKED)
            if state["state"] != "active":
                raise refuse(ipc_errors.PROVIDER_KEY_NOT_FOUND)
            _, envelope, key = _checked(ctx, ref, state, now=now, recipient=recipient)
            value = _unsealed_text(ctx, envelope, key)
        except DaemonRequestError as error:
            _receipt(ctx, ref, state, now, "refused:" + error.code)
            raise
    outcomes.append((state, "injected"))
    return LaunchSecret(grant_id=grant_id, provider=state["provider"], seq=state["seq"],
                        environment_names=tuple(state["environmentNames"]), value=value)


def launch_secrets(ctx: OwnerContext, ref: AgentRef, *, now_ms: int) -> tuple[frozenset[str], tuple[LaunchSecret, ...]]:
    records = _agent_records(ctx, ref, strict=True)
    recipient = _local_recipient_id(ctx)
    secrets, outcomes = [], []
    for grant_id, _ in records:
        secret = _launch_slot(ctx, ref, grant_id, now_ms, recipient, outcomes)
        if secret is not None:
            secrets.append(secret)
    for state, outcome in outcomes:
        _receipt(ctx, ref, state, now_ms, outcome)
    return frozenset(grant_id for grant_id, _ in records), tuple(secrets)
