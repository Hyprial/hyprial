# Decision record (W1E-2, 2026-11-21):
# - Onboarding operation journals live inside the guarded daemon state root and
#   run under apply_private_umask(0o077), so redundant inner os.chmod / 0o077
#   platform-branched mode checks are removed while symlink/reparse/non-regular
#   file rejections remain and directory fsync delegates to manysys.fsync_directory.
"""Private durable journal for M2 onboarding operation receipts.

Decision: gui-m2-operation-receipts-20261105.  The journal stores only public
operation identity, state, ownership surface, receipt ids and the process
identities needed for crash reconciliation.  It never accepts credential
material, verification instructions, bearer values or authorization codes.
Loading is strict: malformed, truncated, duplicate-key, unknown-field,
out-of-bound or index-inconsistent files fail closed and remain untouched.
Writes use a same-directory temporary file, file fsync, atomic replace and
directory fsync so a reported commit survives a process or machine restart.  A
failure before replace leaves the previous journal untouched and is a write
failure; a failure after replace is an unconfirmed commit because the new bytes
are visible while their durability cannot be proven.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from hyprial.daemon.impl.request_catalog.onboarding import (
    OperationError,
    OperationRecord,
    StoreSnapshot,
    Surface,
)
from hyprial.kernel import fsync_directory, path_is_reparse
from hyprial.daemon.impl.request_catalog.onboarding.schemas import AUTH_STATES
from hyprial.daemon.impl.request_catalog.onboarding.state import (
    AUTH_INTENTS,
    DEVICE_STATES,
    INITIALIZE_STATES,
    LOGOUT_STATES,
    OnboardingStateError,
    check_removed_labels,
)

JOURNAL_FILENAME = "operations.json"
JOURNAL_VERSION = 1
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 10_000
MAX_SURFACE_TEXT = 512
MAX_OWNER = 320
MAX_BIRTH = 512
MAX_PID = 2**32 - 1

OPERATION_STORE_COMMIT_UNCONFIRMED = "OPERATION_STORE_COMMIT_UNCONFIRMED"
OPERATION_STORE_CORRUPT = "OPERATION_STORE_CORRUPT"
OPERATION_STORE_INSECURE = "OPERATION_STORE_INSECURE"
OPERATION_STORE_UNVERIFIED = "OPERATION_STORE_UNVERIFIED"
OPERATION_STORE_WRITE_FAILED = "OPERATION_STORE_WRITE_FAILED"

_OPERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_OPAQUE_ID = _OPERATION_ID
_OPERATION_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_RECORD_FIELDS = frozenset(
    {
        "operationId",
        "operationKey",
        "kind",
        "state",
        "surface",
        "receiptId",
        "accountOwner",
        "cancelable",
        "writerPid",
        "writerBirth",
        "controllerPid",
        "controllerBirth",
    }
)
# A logout receipt records the login-owned artifacts it really removed.  It is
# the only record kind that may carry the field, and an older journal without it
# is still valid (an absent list means "nothing was recorded", never "removed
# everything").
_RECORD_FIELDS_WITH_REMOVED = _RECORD_FIELDS | {"removed"}
_RECORD_FIELDS_WITH_ERROR = _RECORD_FIELDS | {"error"}
_SURFACE_FIELDS = frozenset(
    {
        "homeBinding",
        "hostControlSessionId",
        "viewSessionId",
        "transportGeneration",
    }
)
_DOCUMENT_FIELDS = frozenset({"version", "records", "keys"})
_DOCUMENT_FIELDS_WITH_HANDOFF = _DOCUMENT_FIELDS | {"handoff"}
_HANDOFF_FIELDS = frozenset(
    {
        "operationId",
        "receiptId",
        "surface",
        "oldPid",
        "oldBirth",
        "oldEpoch",
        "custodianPid",
        "custodianBirth",
        "proof",
        "takenAt",
        "expiresAt",
    }
)
#: The only exit proof a handoff may name.  A coordinator that cannot observe
#: both the pid and its birth identity must not take custody at all.
HANDOFF_PROOF_PID_BIRTH_EXIT = "pid-birth-exit"
_HANDOFF_PROOFS = frozenset({HANDOFF_PROOF_PID_BIRTH_EXIT})
_HANDOFF_TIMESTAMP = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"
)
_KEY_FIELDS = frozenset(
    {
        "kind",
        "homeBinding",
        "hostControlSessionId",
        "viewSessionId",
        "operationKey",
        "operationId",
    }
)


class OperationReceiptError(RuntimeError):
    """One fail-closed operation receipt storage or transition failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    """One explicit, fenced lifecycle handoff in the private journal.

    The section exists only after a coordinator *proved* the old writer exited,
    and it names both sides of the transition so a restarted coordinator can
    tell "I already took custody" from "I could take custody now".  General
    recovery is unchanged: this is the single exception path, not a rule.
    """

    operation_id: str
    receipt_id: str
    surface: Surface
    old_pid: int
    old_birth: str
    old_epoch: str
    custodian_pid: int
    custodian_birth: str
    proof: str
    taken_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class JournalState:
    snapshot: StoreSnapshot
    writer_identities: tuple[tuple[str, int, str], ...]
    controller_identities: tuple[tuple[str, int, str], ...]
    handoff: HandoffRecord | None = None


def journal_path(root: Path | str) -> Path:
    return Path(root) / JOURNAL_FILENAME


def prepare_root(root: Path | str) -> Path:
    path = Path(root)
    try:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            path.mkdir(parents=True, mode=0o700)
        else:
            if stat.S_ISLNK(info.st_mode) or path_is_reparse(info):
                raise OperationReceiptError(
                    OPERATION_STORE_INSECURE, "operation store root must not be a symlink"
                )
            if not stat.S_ISDIR(info.st_mode):
                raise OperationReceiptError(
                    OPERATION_STORE_INSECURE, "operation store root must be a directory"
                )
    except OSError as error:
        raise OperationReceiptError(
            OPERATION_STORE_INSECURE, "operation store root cannot be prepared safely"
        ) from error
    return path


def _precheck_journal(path: Path) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise
    except OSError as error:
        raise OperationReceiptError(
            OPERATION_STORE_INSECURE, "operation journal cannot be inspected safely"
        ) from error
    if stat.S_ISLNK(info.st_mode) or path_is_reparse(info) or not stat.S_ISREG(info.st_mode):
        raise OperationReceiptError(
            OPERATION_STORE_INSECURE, "operation journal must be a regular file"
        )


def read_journal(root: Path | str) -> JournalState | None:
    path = journal_path(root)
    try:
        _precheck_journal(path)
    except FileNotFoundError:
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise OperationReceiptError(
            OPERATION_STORE_INSECURE, "operation journal cannot be opened safely"
        ) from error
    try:
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or path_is_reparse(info):
                raise OperationReceiptError(
                    OPERATION_STORE_INSECURE, "operation journal must be a regular file"
                )
            if info.st_size > MAX_JOURNAL_BYTES:
                raise OperationReceiptError(
                    OPERATION_STORE_CORRUPT, "operation journal exceeds its size limit"
                )
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = -1
                data = handle.read(MAX_JOURNAL_BYTES + 1)
        except OSError as error:
            raise OperationReceiptError(
                OPERATION_STORE_INSECURE,
                "operation journal cannot be read safely",
            ) from error
    finally:
        if descriptor != -1:
            os.close(descriptor)
    if len(data) > MAX_JOURNAL_BYTES:
        raise OperationReceiptError(
            OPERATION_STORE_CORRUPT, "operation journal exceeds its size limit"
        )
    if not data:
        raise OperationReceiptError(
            OPERATION_STORE_CORRUPT, "operation journal is empty"
        )
    return _decode_journal(data)


def write_journal(
    root: Path | str,
    snapshot: StoreSnapshot,
    writer_identities: dict[str, tuple[int, str]],
    controller_identities: dict[str, tuple[int, str]],
    handoff: HandoffRecord | None = None,
) -> None:
    payload = _encode_journal(
        snapshot, writer_identities, controller_identities, handoff
    )
    directory = prepare_root(root)
    descriptor = -1
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=".operations-", suffix=".tmp", dir=directory
        )
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, journal_path(directory))
        temporary = None
    except OSError as error:
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED,
            "operation journal could not be committed durably",
        ) from error
    finally:
        if descriptor != -1:
            os.close(descriptor)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
    try:
        _fsync_directory(directory)
    except OSError as error:
        raise OperationReceiptError(
            OPERATION_STORE_COMMIT_UNCONFIRMED,
            "operation journal was replaced but its durability is unconfirmed",
        ) from error


def _fsync_directory(directory: Path) -> None:
    fsync_directory(directory)


def _decode_journal(data: bytes) -> JournalState:
    try:
        text = data.decode("utf-8")
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as error:
        raise OperationReceiptError(
            OPERATION_STORE_CORRUPT, "operation journal is malformed"
        ) from error
    if not isinstance(value, dict) or set(value) not in (
        _DOCUMENT_FIELDS,
        _DOCUMENT_FIELDS_WITH_HANDOFF,
    ):
        raise _corrupt("operation journal fields do not match the local format")
    version = value["version"]
    if type(version) is not int or version != JOURNAL_VERSION:
        raise _corrupt("operation journal version is not supported")
    raw_records = value["records"]
    raw_keys = value["keys"]
    if type(raw_records) is not list or type(raw_keys) is not list:
        raise _corrupt("operation journal records and keys must be lists")
    if len(raw_records) > MAX_RECORDS or len(raw_keys) > MAX_RECORDS:
        raise _corrupt("operation journal exceeds its record limit")
    records: list[OperationRecord] = []
    writers: list[tuple[str, int, str]] = []
    controllers: list[tuple[str, int, str]] = []
    for raw in raw_records:
        record, writer, controller = _decode_record(raw)
        records.append(record)
        writers.append(writer)
        controllers.append(controller)
    records.sort(key=lambda record: record.operation_id)
    by_id = {record.operation_id: record for record in records}
    if len(by_id) != len(records):
        raise _corrupt("operation journal has duplicate operation ids")
    rows: list[tuple[str, str, str, str, str, str]] = []
    seen: set[str] = set()
    for raw in raw_keys:
        row = _decode_key(raw)
        namespace, home, host, view, operation_key, operation_id = row
        record = by_id.get(operation_id)
        if record is None:
            raise _corrupt("operation journal key row points at no record")
        if operation_id in seen:
            raise _corrupt("operation journal has duplicate key rows")
        expected_namespace = "auth" if record.kind in AUTH_INTENTS else record.kind
        if (
            namespace != expected_namespace
            or operation_key != record.operation_key
            or home != record.surface.home_binding
            or host != record.surface.host_control_session_id
            or view != record.surface.view_session_id
        ):
            raise _corrupt("operation journal key row does not match its record")
        rows.append(row)
        seen.add(operation_id)
    if set(by_id) != seen:
        raise _corrupt("operation journal record has no key row")
    rows.sort()
    row_keys = {row[:5] for row in rows}
    if len(row_keys) != len(rows) or len(set(rows)) != len(rows):
        raise _corrupt("operation journal has duplicate keys")
    snapshot = StoreSnapshot(records=tuple(records), key_index=tuple(rows))
    handoff = None
    if "handoff" in value:
        handoff = _decode_handoff(value["handoff"], snapshot)
    return JournalState(
        snapshot=snapshot,
        writer_identities=tuple(sorted(writers)),
        controller_identities=tuple(sorted(controllers)),
        handoff=handoff,
    )


def _decode_record(
    raw: object,
) -> tuple[OperationRecord, tuple[str, int, str], tuple[str, int, str]]:
    if not isinstance(raw, dict) or set(raw) not in (
        _RECORD_FIELDS,
        _RECORD_FIELDS_WITH_REMOVED,
        _RECORD_FIELDS_WITH_ERROR,
    ):
        raise _corrupt("operation journal record fields do not match the local format")
    operation_id = _bounded_id(raw["operationId"], "operation id")
    operation_key = _bounded_key(raw["operationKey"])
    kind = raw["kind"]
    if not isinstance(kind, str) or kind not in {
        "login",
        "register",
        "device",
        "initialize",
        "logout",
    }:
        raise _corrupt("operation journal record has an invalid kind")
    state = raw["state"]
    surface = _decode_surface(raw["surface"])
    receipt_id = _optional_opaque(raw["receiptId"])
    account_owner = _optional_owner(raw["accountOwner"])
    cancelable = raw["cancelable"]
    if type(cancelable) is not bool:
        raise _corrupt("operation journal cancelable flag must be boolean")
    writer = (_pid(raw["writerPid"]), _birth(raw["writerBirth"]))
    controller = (_pid(raw["controllerPid"]), _birth(raw["controllerBirth"]))
    if kind in AUTH_INTENTS:
        if state not in AUTH_STATES:
            raise _corrupt("operation journal auth state is invalid")
        if receipt_id is not None:
            raise _corrupt("operation journal auth record must not carry a receipt")
        if state == "authenticated":
            if account_owner is None:
                raise _corrupt("authenticated auth record must carry an owner")
        if cancelable != (state == "pending"):
            raise _corrupt("operation journal auth cancelable flag is inconsistent")
    elif kind == "logout":
        if state not in LOGOUT_STATES:
            raise _corrupt("operation journal logout state is invalid")
        if receipt_id is None:
            raise _corrupt("logout record must carry a receipt")
        if cancelable:
            raise _corrupt("logout record must not be cancelable")
    elif kind == "device":
        if state not in DEVICE_STATES:
            raise _corrupt("operation journal device state is invalid")
        if receipt_id is None or account_owner is None:
            raise _corrupt("device record must carry a receipt and owner")
        if cancelable:
            raise _corrupt("device record must not be cancelable")
    else:
        if state not in INITIALIZE_STATES:
            raise _corrupt("operation journal initialize state is invalid")
        if receipt_id is None or account_owner is None:
            raise _corrupt("initialize record must carry a receipt and owner")
        if cancelable != (state == "accepted"):
            raise _corrupt(
                "operation journal initialize cancelable flag is inconsistent"
            )
    removed = _decode_removed(raw.get("removed"), kind)
    error = _decode_error(raw.get("error"), kind, state)
    record = OperationRecord(
        operation_id=operation_id,
        operation_key=operation_key,
        kind=kind,
        surface=surface,
        state=state,
        receipt_id=receipt_id,
        account_owner=account_owner,
        cancelable=cancelable,
        removed=removed,
        error=error,
    )
    return record, (operation_id, *writer), (operation_id, *controller)


def _decode_error(value: object, kind: str, state: str) -> OperationError | None:
    """Decode the bounded error payload persisted for a failed auth record."""

    if value is None:
        return None
    if kind not in AUTH_INTENTS or state != "failed":
        raise _corrupt("only a failed auth record may carry an operation error")
    if not isinstance(value, dict) or set(value) not in (
        {"code", "message"},
        {"code", "message", "data"},
    ):
        raise _corrupt("operation journal error fields do not match the local format")
    code = value.get("code")
    message = value.get("message")
    if (
        not isinstance(code, str)
        or not code
        or len(code) > 64
        or not isinstance(message, str)
        or not message
        or len(message) > 1000
    ):
        raise _corrupt("operation journal error must carry non-empty code and message")
    raw_data = value.get("data")
    data: dict[str, str] | None = None
    if raw_data is not None:
        if not isinstance(raw_data, dict):
            raise _corrupt("operation journal error data must be a mapping")
        data = {}
        for key, item in raw_data.items():
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(item, str)
                or not item
                or len(item) > MAX_SURFACE_TEXT
            ):
                raise _corrupt("operation journal error data entries must be strings")
            data[key] = item
    return OperationError(code=code, message=message, data=data)


def _decode_removed(value: object, kind: str) -> tuple[str, ...]:
    """Decode the bounded, canonically ordered removed-artifact labels."""

    if value is None:
        return ()
    if kind != "logout":
        raise _corrupt("only a logout record may carry removed artifact labels")
    if not isinstance(value, list) or not all(
        isinstance(label, str) for label in value
    ):
        raise _corrupt("removed artifact labels must be a list of strings")
    labels = tuple(value)
    try:
        check_removed_labels(labels)
    except OnboardingStateError as error:
        raise _corrupt(f"removed artifact labels are invalid: {error.code}") from error
    return labels


def _decode_surface(raw: object) -> Surface:
    if not isinstance(raw, dict) or set(raw) != _SURFACE_FIELDS:
        raise _corrupt("operation journal surface fields do not match the local format")
    return Surface(
        home_binding=_surface_text(raw["homeBinding"]),
        host_control_session_id=_surface_text(raw["hostControlSessionId"]),
        view_session_id=_surface_text(raw["viewSessionId"]),
        transport_generation=_surface_text(raw["transportGeneration"]),
    )


def _decode_key(raw: object) -> tuple[str, str, str, str, str, str]:
    if not isinstance(raw, dict) or set(raw) != _KEY_FIELDS:
        raise _corrupt("operation journal key fields do not match the local format")
    namespace = raw["kind"]
    if not isinstance(namespace, str) or namespace not in {
        "auth",
        "device",
        "initialize",
        "logout",
    }:
        raise _corrupt("operation journal key namespace is invalid")
    return (
        namespace,
        _surface_text(raw["homeBinding"]),
        _surface_text(raw["hostControlSessionId"]),
        _surface_text(raw["viewSessionId"]),
        _bounded_key(raw["operationKey"]),
        _bounded_id(raw["operationId"], "operation id"),
    )


def _encode_handoff(handoff: HandoffRecord) -> dict[str, object]:
    return {
        "operationId": handoff.operation_id,
        "receiptId": handoff.receipt_id,
        "surface": _encode_surface(handoff.surface),
        "oldPid": handoff.old_pid,
        "oldBirth": handoff.old_birth,
        "oldEpoch": handoff.old_epoch,
        "custodianPid": handoff.custodian_pid,
        "custodianBirth": handoff.custodian_birth,
        "proof": handoff.proof,
        "takenAt": handoff.taken_at,
        "expiresAt": handoff.expires_at,
    }


def _decode_handoff(raw: object, snapshot: StoreSnapshot) -> HandoffRecord:
    """Validate the fenced handoff section against the receipts it names."""

    if not isinstance(raw, dict) or set(raw) != _HANDOFF_FIELDS:
        raise _corrupt("operation journal handoff fields do not match the local format")
    operation_id = _bounded_id(raw["operationId"], "handoff operation id")
    record = next(
        (item for item in snapshot.records if item.operation_id == operation_id),
        None,
    )
    if record is None or record.kind != "logout":
        raise _corrupt("operation journal handoff names no logout receipt")
    receipt_id = _bounded_id(raw["receiptId"], "handoff receipt id")
    if receipt_id != record.receipt_id:
        raise _corrupt("operation journal handoff receipt does not match its record")
    surface = _decode_surface(raw["surface"])
    if surface != record.surface:
        raise _corrupt("operation journal handoff surface does not match its record")
    old_pid = _pid(raw["oldPid"])
    custodian_pid = _pid(raw["custodianPid"])
    if old_pid == custodian_pid:
        raise _corrupt("operation journal handoff custodian is the old writer")
    proof = raw["proof"]
    if not isinstance(proof, str) or proof not in _HANDOFF_PROOFS:
        raise _corrupt("operation journal handoff proof is not an exit proof")
    taken_at = _handoff_timestamp(raw["takenAt"])
    expires_at = _handoff_timestamp(raw["expiresAt"])
    if expires_at <= taken_at:
        raise _corrupt("operation journal handoff expiry does not follow its start")
    return HandoffRecord(
        operation_id=operation_id,
        receipt_id=receipt_id,
        surface=surface,
        old_pid=old_pid,
        old_birth=_birth(raw["oldBirth"]),
        old_epoch=_surface_text(raw["oldEpoch"]),
        custodian_pid=custodian_pid,
        custodian_birth=_birth(raw["custodianBirth"]),
        proof=proof,
        taken_at=taken_at,
        expires_at=expires_at,
    )


def _handoff_timestamp(value: object) -> str:
    if not isinstance(value, str) or not _HANDOFF_TIMESTAMP.match(value):
        raise _corrupt("operation journal handoff timestamp is not ISO-8601 UTC")
    return value


def _encode_journal(
    snapshot: StoreSnapshot,
    writer_identities: dict[str, tuple[int, str]],
    controller_identities: dict[str, tuple[int, str]],
    handoff: HandoffRecord | None = None,
) -> bytes:
    if not isinstance(snapshot, StoreSnapshot):
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED, "operation journal state is inconsistent"
        )
    records = sorted(snapshot.records, key=lambda record: record.operation_id)
    if len(records) > MAX_RECORDS or len(snapshot.key_index) > MAX_RECORDS:
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED, "operation journal exceeds its record limit"
        )
    ids = {record.operation_id for record in records}
    if set(writer_identities) != ids or set(controller_identities) != ids:
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED,
            "operation journal process identities are incomplete",
        )
    rows = sorted(snapshot.key_index)
    if len(set(rows)) != len(rows):
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED, "operation journal keys are duplicated"
        )
    document = {
        "version": JOURNAL_VERSION,
        "records": [
            _encode_record(
                record,
                writer_identities[record.operation_id],
                controller_identities[record.operation_id],
            )
            for record in records
        ],
        "keys": [
            {
                "kind": namespace,
                "homeBinding": home,
                "hostControlSessionId": host,
                "viewSessionId": view,
                "operationKey": operation_key,
                "operationId": operation_id,
            }
            for namespace, home, host, view, operation_key, operation_id in rows
        ],
    }
    if handoff is not None:
        document["handoff"] = _encode_handoff(handoff)
    try:
        payload = (
            json.dumps(
                document,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED, "operation journal state cannot be encoded"
        ) from error
    if len(payload) > MAX_JOURNAL_BYTES:
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED, "operation journal exceeds its size limit"
        )
    try:
        _decode_journal(payload)
    except OperationReceiptError as error:
        raise OperationReceiptError(
            OPERATION_STORE_WRITE_FAILED,
            "operation journal state cannot pass strict reload validation",
        ) from error
    return payload


def _encode_surface(surface: Surface) -> dict[str, object]:
    return {
        "homeBinding": surface.home_binding,
        "hostControlSessionId": surface.host_control_session_id,
        "viewSessionId": surface.view_session_id,
        "transportGeneration": surface.transport_generation,
    }


def _encode_record(
    record: OperationRecord,
    writer: tuple[int, str],
    controller: tuple[int, str],
) -> dict[str, object]:
    return {
        "operationId": record.operation_id,
        "operationKey": record.operation_key,
        "kind": record.kind,
        "state": record.state,
        "surface": _encode_surface(record.surface),
        "receiptId": record.receipt_id,
        "accountOwner": record.account_owner,
        "cancelable": record.cancelable,
        "writerPid": writer[0],
        "writerBirth": writer[1],
        "controllerPid": controller[0],
        "controllerBirth": controller[1],
        **({"removed": list(record.removed)} if record.kind == "logout" else {}),
        **({"error": record.error.as_dict()} if record.error is not None else {}),
    }


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate json key")
        result[key] = value
    return result


def _reject_constant(value: str) -> object:
    raise ValueError("non-finite json value")


def _corrupt(message: str) -> OperationReceiptError:
    return OperationReceiptError(OPERATION_STORE_CORRUPT, message)


def _surface_text(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_SURFACE_TEXT
        or not value.strip()
    ):
        raise _corrupt("operation journal surface text is invalid")
    return value


def _bounded_id(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 8 <= len(value) <= 200
        or _OPAQUE_ID.fullmatch(value) is None
    ):
        raise _corrupt(f"operation journal {label} is invalid")
    return value


def _bounded_key(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 16 <= len(value) <= 128
        or _OPERATION_KEY.fullmatch(value) is None
    ):
        raise _corrupt("operation journal operation key is invalid")
    return value


def _optional_opaque(value: object) -> str | None:
    if value is None:
        return None
    return _bounded_id(value, "receipt id")


def _optional_owner(value: object) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not 3 <= len(value) <= MAX_OWNER
        or not value.strip()
    ):
        raise _corrupt("operation journal owner is invalid")
    return value


def _pid(value: object) -> int:
    if type(value) is not int or value <= 0 or value > MAX_PID:
        raise _corrupt("operation journal process id is invalid")
    return value


def _birth(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_BIRTH:
        raise _corrupt("operation journal process birth identity is invalid")
    return value


__all__ = [
    "JOURNAL_FILENAME",
    "JOURNAL_VERSION",
    "JournalState",
    "OPERATION_STORE_COMMIT_UNCONFIRMED",
    "OPERATION_STORE_CORRUPT",
    "OPERATION_STORE_INSECURE",
    "OPERATION_STORE_WRITE_FAILED",
    "OperationReceiptError",
    "OPERATION_STORE_UNVERIFIED",
    "journal_path",
    "prepare_root",
    "read_journal",
    "HANDOFF_PROOF_PID_BIRTH_EXIT",
    "HandoffRecord",
    "write_journal",
]
