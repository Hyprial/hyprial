# Decision record (contract freeze r3 §3.2, §4.3, §4.4, §5.2, §7.2; impl spec slice B1)
# - login: allocate keyId, seal to this device's X1 owner-device key (the only
#   recipient this phase), envelope-1 create_only, meta create_only (commit point,
#   seq 1), then read back: meta signature, envelope binding and a local DEK unwrap.
#   Matching awaiting-key grants complete after the commit; their failure never
#   fails the login (list shows them as they are).
# - Default environment name when none is given: the provider upper-cased with every
#   run of other characters as "_", prefixed "API_" when it does not start with a
#   letter, plus "_API_KEY" (anthropic -> ANTHROPIC_API_KEY).
# - rotate: next envelope create_only at max(current, any stored envelope) + 1, meta
#   CAS (seq+1, new digest), reissue every active grant of the key, then remove the
#   older envelopes. A crash leaves older grants refused for dekVersion mismatch
#   (fail closed) until a rerun. Without a replacement the payload is resealed by
#   the X1 store (never leaves it as plaintext); a gap in versions reseals through
#   open + seal in this process.
# - logout: revoke every active grant of the key, tombstone the meta (permanent,
#   keyId never reused), then remove every envelope. A rerun on a tombstone only
#   removes leftover envelopes and reports alreadyDeleted.
# - list: keys from verified metas; unverifiable metas (or an active meta whose
#   envelope does not bind) are listed only in invalid; grants come from scanning
#   grant states; lastUsedAt from local receipts, null when they cannot be read.
# - Nothing after a commit point turns a committed login or rotate into an error:
#   the result's grant rows are best effort (empty when the scan fails), because a
#   retried login is never deduplicated and would create a second key.
# - A refusal over a write-only value (not UTF-8 encodable) is raised after its
#   except block ends, so no exception holding the value is chained as __context__.
"""Key lifecycle: login, list, rotate and logout over the owner store."""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping, Sequence

from hyprial.identity import ProviderKeys, UserKey
from hyprial.kernel import DaemonRequestError, ipc_errors
from ..storage import (
    InvalidSegment, StaleWrite, envelope_dek_version, key_dir, key_envelope_path, key_meta_path, last_used,
)
from . import grants
from .records import (
    INVALID, KEY_TYPE, OwnerContext, Signer, commit, dumps, grant_states, invalid_argument, key_ids, key_lock,
    read_envelope, read_meta, refuse,
)

_LOG = logging.getLogger(__name__)
_KEY_ID = re.compile(r"pk\.[a-z2-7]{26}")
_HEADER = {"v": 1, "alg": "A256GCM", "wrap": "X25519-HKDF-SHA256-A256GCM"}
KIND = "api-key"


def default_environment_names(provider: str) -> list[str]:
    stem = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")
    if not stem or not stem[0].isalpha():
        stem = "API_" + stem
    return [stem + "_API_KEY"]


def _secret_bytes(secret: Any) -> bytes:
    if not isinstance(secret, str) or not secret or "\n" in secret or "\r" in secret:
        raise invalid_argument("write-only value must be one non-empty line")
    try:
        data = secret.encode("utf-8")
    except UnicodeEncodeError:
        data = None
    if data is None:
        raise invalid_argument("write-only value must be valid UTF-8")
    return data


def _legacy_key_ref(value: Mapping[str, Any] | None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != {"entryId", "fieldName"}:
        raise invalid_argument("legacyRef must be {entryId, fieldName}")
    return {"entryId": value["entryId"], "fieldName": value["fieldName"]}


def local_recipient(ctx: OwnerContext, code: str = ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING) -> Any:
    """This device's X1 recipient, or `code` when it has no usable key."""
    try:
        return ctx.sealed.recipient()
    except UserKey.Error:
        raise refuse(code) from None


def _recipient_rows(ctx: OwnerContext, recipient: Any) -> list[dict]:
    return [{"recipientKeyId": recipient.key_id, "role": "owner-device", "deviceUid": ctx.device_uid}]


def _readback(ctx: OwnerContext, meta: Mapping[str, Any]) -> None:
    envelope, key = read_envelope(ctx, meta)
    ctx.sealed.probe_sealed(envelope, owner_key=key)


def login(ctx: OwnerContext, *, provider: str, kind: str, secret: str, label: str | None,
          environment_names: Sequence[str] | None, legacy_ref: Mapping[str, Any] | None) -> dict:
    if kind != KIND:
        raise invalid_argument("kind must be api-key")
    plaintext = _secret_bytes(secret)
    reference = _legacy_key_ref(legacy_ref)
    names = list(environment_names) if environment_names else default_environment_names(provider)
    signer = Signer(ctx, "provider-key/login")
    signing_key = signer()
    ctx.sealed.ensure()
    recipient = local_recipient(ctx)
    key_id = ProviderKeys.new_key_id()
    now = ctx.now()
    header = {**_HEADER, "provider": provider, "keyId": key_id, "owner": ctx.owner, "dekVersion": 1,
              "createdAt": now}
    envelope = ProviderKeys.seal(plaintext, header=header, recipients=[recipient], signer=signing_key)
    meta = {"type": KEY_TYPE, "v": 1, "owner": ctx.owner, "keyId": key_id, "provider": provider, "kind": KIND,
            "label": label or "", "state": "active", "seq": 1, "createdAt": now, "updatedAt": now,
            "dekVersion": 1, "envelopeDigest": ProviderKeys.record_digest(envelope),
            "recipients": _recipient_rows(ctx, recipient), "environmentNames": names, "legacyRef": reference}
    with key_lock(ctx, key_id):
        try:
            ctx.store.write(key_envelope_path(key_id, 1), dumps(envelope), expect_version=None, create_only=True)
        except StaleWrite:
            raise refuse(ipc_errors.PROVIDER_STORE_CONFLICT) from None
        stored, _ = commit(ctx, key_meta_path(key_id), KEY_TYPE, lambda previous: _created(previous, meta), signer)
        _readback(ctx, stored)
    if reference is not None:
        grants.complete_awaiting(ctx, stored, recipient, signer)
    return stored


def _created(previous: dict | None, meta: dict) -> dict:
    if previous is not None:
        raise refuse(ipc_errors.PROVIDER_STORE_CONFLICT)
    return meta


def _active_meta(ctx: OwnerContext, key_id: str) -> dict:
    meta = read_meta(ctx, key_id)
    if meta is None or meta["state"] != "active":
        raise refuse(ipc_errors.PROVIDER_KEY_NOT_FOUND)
    return meta


def _stored_versions(ctx: OwnerContext, key_id: str) -> list[int]:
    return [version for version in (envelope_dek_version(name) for name in ctx.store.listdir(key_dir(key_id)))
            if version is not None]


def rotate(ctx: OwnerContext, *, key_id: str, replace_secret: str | None) -> tuple[dict, int]:
    plaintext = None if replace_secret is None else _secret_bytes(replace_secret)
    signer = Signer(ctx, "provider-key/rotate")
    with key_lock(ctx, key_id):
        meta = _active_meta(ctx, key_id)
        envelope, owner_key = read_envelope(ctx, meta)
        signing_key = signer()
        recipient = local_recipient(ctx, ipc_errors.PROVIDER_KEY_UNSEAL_FAILED)
        current = meta["dekVersion"]
        version = max([current, *_stored_versions(ctx, key_id)]) + 1
        now = max(ctx.now(meta), envelope["header"]["createdAt"])
        try:
            if plaintext is None and version == current + 1:
                sealed = ctx.sealed.reseal_sealed(envelope, owner_key=owner_key, signer=signing_key,
                                                  recipients=[recipient], created_at=now)
            else:
                body = plaintext if plaintext is not None else ctx.sealed.open_sealed(envelope, owner_key=owner_key)
                header = {**envelope["header"], "dekVersion": version, "createdAt": now}
                sealed = ProviderKeys.seal(body, header=header, recipients=[recipient], signer=signing_key)
        except ProviderKeys.Error as error:
            if error.code != ipc_errors.PROVIDER_KEY_RECIPIENT_PENDING:
                raise
            raise refuse(ipc_errors.PROVIDER_KEY_UNSEAL_FAILED) from None
        try:
            ctx.store.write(key_envelope_path(key_id, version), dumps(sealed), expect_version=None, create_only=True)
        except StaleWrite:
            raise refuse(ipc_errors.PROVIDER_STORE_CONFLICT) from None

        def build(previous: dict | None) -> dict:
            if previous != meta:
                raise refuse(ipc_errors.PROVIDER_STORE_CONFLICT)
            return {**meta, "seq": meta["seq"] + 1, "updatedAt": ctx.now(meta), "dekVersion": version,
                    "envelopeDigest": ProviderKeys.record_digest(sealed),
                    "recipients": _recipient_rows(ctx, recipient)}

        stored, _ = commit(ctx, key_meta_path(key_id), KEY_TYPE, build, signer)
        _readback(ctx, stored)
        reissued = grants.reissue(ctx, stored, recipient, signer)
        for old in _stored_versions(ctx, key_id):
            if old < version:
                ctx.store.remove(key_envelope_path(key_id, old))
    return stored, reissued


def logout(ctx: OwnerContext, *, key_id: str) -> tuple[list[dict], bool]:
    signer = Signer(ctx, "provider-key/logout")
    with key_lock(ctx, key_id):
        meta = read_meta(ctx, key_id)
        if meta is None:
            raise refuse(ipc_errors.PROVIDER_KEY_NOT_FOUND)
        already = meta["state"] == "deleted"
        revoked = [] if already else grants.revoke_key(ctx, key_id, signer)
        if not already:
            def build(previous: dict | None) -> dict | None:
                if previous is None:
                    raise refuse(INVALID)
                if previous["state"] == "deleted":
                    return None
                return {**previous, "seq": previous["seq"] + 1, "updatedAt": ctx.now(previous),
                        "state": "deleted", "dekVersion": None, "envelopeDigest": None, "recipients": []}

            commit(ctx, key_meta_path(key_id), KEY_TYPE, build, signer)
        for version in _stored_versions(ctx, key_id):
            ctx.store.remove(key_envelope_path(key_id, version))
    return revoked, already


def _last_used(ctx: OwnerContext) -> dict[str, int]:
    try:
        return last_used(ctx.state_dir)
    except (OSError, ValueError, ProviderKeys.Error):
        _LOG.warning("provider key usage receipts are unreadable; lastUsedAt is null")
        return {}


def key_view(meta: Mapping[str, Any], grant_rows: Sequence[dict], used: Mapping[str, int]) -> dict:
    return {
        "keyId": meta["keyId"], "provider": meta["provider"], "kind": meta["kind"], "label": meta["label"],
        "owner": meta["owner"], "state": meta["state"], "createdAt": meta["createdAt"],
        "updatedAt": meta["updatedAt"], "dekVersion": meta["dekVersion"],
        "environmentNames": list(meta["environmentNames"]),
        "recipients": [dict(row) for row in meta["recipients"]],
        "lastUsedAt": used.get(meta["keyId"]), "grants": list(grant_rows),
    }


def _grant_rows(ctx: OwnerContext) -> dict[str, list[dict]]:
    agents = ctx.local_agents()
    rows: dict[str, list[dict]] = {}
    for state in grant_states(ctx):
        if state["keyId"] is not None:
            rows.setdefault(state["keyId"], []).append({
                "agent": state["agent"], "agentId": agents.get(state["agent"]), "grantId": state["grantId"],
                "provider": state["provider"], "state": state["state"]})
    for value in rows.values():
        value.sort(key=lambda row: (row["agent"], row["grantId"]))
    return rows


def _checked_meta(ctx: OwnerContext, key_id: str) -> dict | None:
    """A verified meta whose envelope binds when active; None when only an orphan dir exists."""
    if _KEY_ID.fullmatch(key_id) is None:
        raise refuse(INVALID)
    try:
        meta = read_meta(ctx, key_id)
    except InvalidSegment:
        raise refuse(INVALID) from None
    if meta is not None and meta["state"] == "active":
        read_envelope(ctx, meta)
    return meta


def list_keys(ctx: OwnerContext, *, provider: str | None, include_deleted: bool) -> dict:
    metas, invalid = [], []
    for key_id in key_ids(ctx):
        try:
            meta = _checked_meta(ctx, key_id)
        except ProviderKeys.Error as error:
            if error.code != INVALID:
                raise
            invalid.append({"keyId": key_id, "code": INVALID})
            continue
        if meta is None or (provider is not None and meta["provider"] != provider):
            continue
        if meta["state"] == "active" or include_deleted:
            metas.append(meta)
    rows = _grant_rows(ctx) if metas else {}
    used = _last_used(ctx) if metas else {}
    metas.sort(key=lambda meta: (meta["createdAt"], meta["keyId"]))
    return {"keys": [key_view(meta, rows.get(meta["keyId"], []), used) for meta in metas], "invalid": invalid}


def view(ctx: OwnerContext, meta: Mapping[str, Any]) -> dict:
    """The ProviderKeyView of a committed meta; its grant rows are best effort and never fail the call."""
    try:
        rows = _grant_rows(ctx).get(meta["keyId"], [])
    except DaemonRequestError as error:
        _LOG.warning("grant rows of a committed provider key could not be read (%s)", error.code)
        rows = []
    return key_view(meta, rows, _last_used(ctx))


def find_by_legacy_ref(ctx: OwnerContext, *, entry_id: str, field_name: str) -> dict | None:
    wanted = {"entryId": entry_id, "fieldName": field_name}
    for row in list_keys(ctx, provider=None, include_deleted=False)["keys"]:
        meta = read_meta(ctx, row["keyId"])
        if meta is not None and meta["state"] == "active" and meta["legacyRef"] == wanted:
            return row
    return None
