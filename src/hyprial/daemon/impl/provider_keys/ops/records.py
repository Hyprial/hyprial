# Decision record (contract freeze r3 §3.1-§3.3, §4.3-§4.5, §9.5; impl spec slice B1)
# - Every owner record is read through read_record: parse, look up the signing key
#   named by sealedBy.keyId through the U2 anchor (verified only; absent is
#   SIGNATURE_INVALID, unavailable is PROVIDER_STORE_UNAVAILABLE and never absent),
#   verify the signature before any field is trusted, then require the path subject
#   to equal the record subject (keyId, agent, grantId).
# - Local strong consistency (§9.5 layer 1): per-owner rollback marks (ops.marks)
#   remember the highest (seq, digest) accepted per path; a lower seq, or the same
#   seq with other content, is a rollback and refuses as SIGNATURE_INVALID. The marks
#   are durable under the local state directory, so a record replayed while the
#   daemon was down is still refused, and so never renewed, after a restart.
# - A read is refused only when the mark is still above it after a fresh re-read
#   (READ_ATTEMPTS): scans read without the slot lock, so another thread's commit can
#   raise the mark between our read and our observe; that stale read is not a rollback.
# - Owner records are never deleted (§4.4: grants are revoked, keys tombstoned). A path
#   that carries a mark but is absent from the store was deleted by a writer to the
#   space, and refuses as SIGNATURE_INVALID instead of reading as "never existed". Slot
#   listings (_agent_records, slot_names, status) include the marked names of the agent,
#   so a deleted record still holds its slot and still shadows the legacy grant.
# - Re-grant recovery (§8.4 "record fails verification"): an explicit allow or
#   awaiting-key write (commit regrant=True) over a SIGNATURE_INVALID record, a deleted
#   record included, writes a fresh record above the mark (seq = mark + 1) with
#   compare-and-set on the stored version. The exact snapshot supplying that version
#   is verified again: a recovered valid record keeps its normal write preconditions,
#   and an unavailable anchor still refuses. No other write overwrites such a record:
#   revoke, renewal, reissue and cascades refuse or skip it.
# - Every write: read and verify the previous record, build the next one from it,
#   check_transition, sign, then write with expect_version (create_only when absent).
#   A lost compare-and-set re-reads and re-validates preconditions; after
#   CAS_ATTEMPTS it is PROVIDER_STORE_CONFLICT. A readback verifies the stored record;
#   a different record is accepted only as a valid newer transition of ours.
# - Signing is lazy: the gated owner key is fetched only when a record must change,
#   so an idempotent no-op never needs the signer, and a refused signer writes nothing.
#   Before first use the key must be the anchor-verified owner key of this sub (its
#   ed25519 key id is the U1 rule): nothing is written that this device could not
#   verify itself.
# - In-process locks per key and per (agent, grantId) slot; the order is always key
#   lock before slot lock.
# - Owner-wide scans (grant_states: logout cascade, rotate reissue, list rows and
#   awaiting-key completion) skip, with a value-free warning, a grant-state record
#   that is SIGNATURE_INVALID or whose sealing key the anchor reports unavailable: it
#   can be neither revoked nor proven to name the key, and the tombstone or the newer
#   dekVersion already makes it fail closed at launch. One planted record therefore
#   never blocks a logout. Reads of a named record (the key's own meta, the target
#   agent's slots at launch) still raise.
"""Owner context, verified record reads and compare-and-set record writes."""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hyprial.identity import ProviderKeys
from hyprial.kernel import DaemonRequestError, ipc_errors
from ..api import CAS_ATTEMPTS, AgentRef
from ..storage import (
    GRANTS_ROOT, KEYS_ROOT, InvalidSegment, StaleWrite, grant_id_from_name, grant_path, grants_dir,
    key_envelope_path, key_meta_path,
)
from .marks import HighWater

KEY_TYPE = "provider-key"
GRANT_STATE_TYPE = "provider-grant-state"
INVALID = ipc_errors.PROVIDER_KEY_SIGNATURE_INVALID
UNAVAILABLE = ipc_errors.PROVIDER_STORE_UNAVAILABLE

_LOG = logging.getLogger(__name__)
READ_ATTEMPTS = 3
_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[tuple[str, ...], threading.Lock] = {}


def refuse(code: str) -> ProviderKeys.Error:
    """A value-free provider-key refusal."""
    return ProviderKeys.Error(code)


def invalid_argument(message: str) -> DaemonRequestError:
    return DaemonRequestError(ipc_errors.INVALID_ARGUMENT, message)


def _unknown(ref: AgentRef) -> bool | None:
    return None


def _nothing() -> Mapping[str, str]:
    return {}


def _current(ref: AgentRef) -> bool:
    return True


@dataclass(frozen=True)
class OwnerContext:
    """Everything one owner-device operation needs; built per call by context.service_for."""

    owner: str
    device_uid: str
    store: Any
    anchor: Any
    signing_key: Callable[[str], Any]
    sealed: Any
    clock: Callable[[], int]
    state_dir: Path
    high_water: HighWater = field(default_factory=HighWater)
    running: Callable[[AgentRef], bool | None] = _unknown
    local_agents: Callable[[], Mapping[str, str]] = _nothing
    current: Callable[[AgentRef], bool] = _current

    def __repr__(self) -> str:
        return f"OwnerContext(device_uid={self.device_uid!r})"

    def now(self, previous: Mapping[str, Any] | None = None) -> int:
        """Current ms, never before the previous record's updatedAt."""
        now = self.clock()
        return now if previous is None else max(now, previous["updatedAt"])


class Signer:
    """Lazily fetched gated owner key for one operation."""

    def __init__(self, ctx: OwnerContext, operation: str) -> None:
        self._ctx = ctx
        self._operation = operation
        self._key: Any = None

    def __repr__(self) -> str:
        return f"Signer(operation={self._operation!r})"

    def __call__(self) -> Any:
        if self._key is None:
            key = self._ctx.signing_key(self._operation)
            public = Ed25519PrivateKey.from_private_bytes(key.private_key).public_key().public_bytes_raw()
            anchored = owner_key(self._ctx, {"keyId": "ed25519:" + hashlib.sha256(public).hexdigest()[:32]})
            if anchored.public_key != public or key.owner != self._ctx.owner:
                raise refuse(INVALID)
            self._key = key
        return self._key


@contextmanager
def locked(*key: str) -> Iterator[None]:
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    with lock:
        yield


def key_lock(ctx: OwnerContext, key_id: str):
    return locked("key", ctx.owner, key_id)


def slot_lock(ctx: OwnerContext, agent: str, grant_id: str):
    return locked("slot", ctx.owner, agent, grant_id)


def parse(text: str) -> dict:
    try:
        value = json.loads(text)
    except (ValueError, TypeError):
        raise refuse(INVALID) from None
    if type(value) is not dict:
        raise refuse(INVALID)
    return value


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def owner_key(ctx: OwnerContext, sealed_by: Any) -> Any:
    """The verified owner key named by an untrusted sealedBy, or a value-free refusal."""
    key_id = sealed_by.get("keyId") if type(sealed_by) is dict else None
    if type(key_id) is not str or not key_id:
        raise refuse(INVALID)
    result = ctx.anchor.lookup(ctx.owner, key_id)
    state = getattr(result, "state", None)
    key = getattr(result, "key", None)
    if state == "verified" and isinstance(key, ProviderKeys.OwnerKey) and key.owner == ctx.owner \
            and key.key_id == key_id:
        return key
    raise refuse(INVALID if state == "absent" else UNAVAILABLE)


def _subject_path(payload: Mapping[str, Any]) -> str:
    if payload["type"] == KEY_TYPE:
        return key_meta_path(payload["keyId"])
    return grant_path(payload["agent"], payload["grantId"])


def _verified(ctx: OwnerContext, path: str, text: str, record_type: str) -> dict:
    record = parse(text)
    payload = ProviderKeys.verify_record(record, owner_key=owner_key(ctx, record.get("sealedBy")),
                                         record_type=record_type)
    try:
        subject = _subject_path(payload)
    except InvalidSegment:
        raise refuse(INVALID) from None
    if subject != path:
        raise refuse(INVALID)
    return payload


def _below_mark(ctx: OwnerContext, path: str, payload: dict | None) -> bool:
    """Accept what this read saw, or report that the mark is above it."""
    if payload is None:
        return ctx.high_water.mark(path) is not None
    try:
        ctx.high_water.observe(path, payload)
    except ProviderKeys.Error as error:
        if error.code != INVALID:
            raise
        return True
    return False


def read_record(ctx: OwnerContext, path: str, record_type: str) -> tuple[dict | None, str | None]:
    """(verified payload, version), or (None, None) when absent and never accepted here."""
    for _ in range(READ_ATTEMPTS):
        found = ctx.store.read(path)
        payload = None if found is None else _verified(ctx, path, found[0], record_type)
        if not _below_mark(ctx, path, payload):
            return (None, None) if found is None else (payload, found[1])
    raise refuse(INVALID)


def read_meta(ctx: OwnerContext, key_id: str) -> dict | None:
    return read_record(ctx, key_meta_path(key_id), KEY_TYPE)[0]


def read_envelope(ctx: OwnerContext, meta: Mapping[str, Any]) -> tuple[dict, Any]:
    """The verified current envelope of an active meta and the owner key that sealed it."""
    found = ctx.store.read(key_envelope_path(meta["keyId"], meta["dekVersion"]))
    if found is None:
        raise refuse(INVALID)
    envelope = parse(found[0])
    key = owner_key(ctx, envelope.get("sealedBy"))
    value = ProviderKeys.verify_envelope(envelope, owner_key=key)
    ProviderKeys.check_envelope_matches(dict(meta), value)
    return value, key


def _regrant_snapshot(ctx: OwnerContext, path: str,
                      record_type: str) -> tuple[dict | None, str | None, int]:
    """Verify the same snapshot used for CAS; only its INVALID state permits repair."""
    found = ctx.store.read(path)
    version = None if found is None else found[1]
    try:
        payload = None if found is None else _verified(ctx, path, found[0], record_type)
        if _below_mark(ctx, path, payload):
            raise refuse(INVALID)
    except ProviderKeys.Error as error:
        if error.code != INVALID:
            raise
        mark = ctx.high_water.mark(path)
        _LOG.warning("re-grant overwrites one grant-state record that cannot be verified")
        return None, version, 0 if mark is None else mark[0]
    return payload, version, 0


def commit(ctx: OwnerContext, path: str, record_type: str,
           build: Callable[[dict | None], dict | None], signer: Signer, *,
           regrant: bool = False) -> tuple[dict | None, bool]:
    """Compare-and-set one record; build returns the next payload or None for no change."""
    for _ in range(CAS_ATTEMPTS):
        floor = 0
        try:
            previous, version = read_record(ctx, path, record_type)
        except ProviderKeys.Error as error:
            if not regrant or error.code != INVALID:
                raise
            previous, version, floor = _regrant_snapshot(ctx, path, record_type)
        current = build(previous)
        if current is None:
            return previous, False
        if current["seq"] <= floor:
            current = {**current, "seq": floor + 1}
        ProviderKeys.check_transition(previous, current)
        signed = ProviderKeys.sign_record(current, signer=signer())
        try:
            ctx.store.write(path, dumps(signed), expect_version=version, create_only=version is None)
        except StaleWrite:
            continue
        stored, _ = read_record(ctx, path, record_type)
        if stored is None:
            raise refuse(INVALID)
        if stored != signed["payload"]:
            ProviderKeys.check_transition(signed["payload"], stored)
        return stored, True
    raise refuse(ipc_errors.PROVIDER_STORE_CONFLICT)


def key_ids(ctx: OwnerContext) -> tuple[str, ...]:
    return ctx.store.listdir(KEYS_ROOT)


def agent_dirs(ctx: OwnerContext) -> tuple[str, ...]:
    return ctx.store.listdir(GRANTS_ROOT)


def slot_files(ctx: OwnerContext, agent: str) -> tuple[str, ...]:
    """Every grant file name of the agent in the store, plus the names this device has marked."""
    directory = grants_dir(agent)
    return tuple(sorted(set(ctx.store.listdir(directory)) | set(ctx.high_water.marked_names(directory))))


def grant_states(ctx: OwnerContext) -> Iterator[dict]:
    """Every verifiable grant-state record of this owner; unverifiable or unavailable ones are skipped."""
    for agent in agent_dirs(ctx):
        try:
            names = slot_files(ctx, agent)
        except InvalidSegment:
            continue
        for name in names:
            grant_id = grant_id_from_name(name)
            if grant_id is None:
                continue
            try:
                payload, _ = read_record(ctx, grant_path(agent, grant_id), GRANT_STATE_TYPE)
            except ProviderKeys.Error as error:
                if error.code not in (INVALID, UNAVAILABLE):
                    raise
                _LOG.warning("skipped one grant-state record that cannot be verified (%s)", error.code)
                continue
            if payload is not None:
                yield payload
