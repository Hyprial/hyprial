"""Typed durable operation receipt service for the M2 bootstrap runtime.

Decision: gui-m2-operation-receipts-20261105.  This service composes the pure
onboarding state model and adds a private journal, crash reconciliation and an
explicit recovery boundary.  It is not wired into production handlers here.
Operation ids and operation keys are selectors, never credentials.  The service
never authenticates, never promotes, never contacts an external identity
service and never stores authorization-guide material.  A promotion callback runs only
after the accepted record and non-cancelable promotion claim are both durably
committed. A new process does
not replay accepted work.  A post-replace commit whose durability cannot be
confirmed freezes the service; callers must reopen and reconcile before any
further operation.  Recovery authority is a typed object that the operator
control layer may construct only after its own control-proof and OS peer
verification; constructing it here does not prove recovery authorization.
"""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from hyprial.kernel import ipc_errors, probe_process, process_birth_identity

from hyprial.daemon.impl.request_catalog.onboarding import (
    AUTH_LOGOUT_SPEC,
    AUTH_OPERATION_CANCEL_SPEC,
    AUTH_OPERATION_GET_SPEC,
    AUTH_OPERATION_NOT_FOUND,
    DEVICE_ENSURE_SPEC,
    INITIALIZE_SPEC,
    LOGIN_BEGIN_SPEC,
    STATUS_SPEC,
    CallContext,
    CancelOutcome,
    FirstRunSnapshot,
    OnboardingStateError,
    OperationError,
    OperationRecord,
    OperationStore,
    StoreSnapshot,
    authorize_call,
    onboarding_status,
)

from . import journal
from .journal import (
    OPERATION_STORE_COMMIT_UNCONFIRMED,
    OPERATION_STORE_CORRUPT,
    OPERATION_STORE_UNVERIFIED,
    OperationReceiptError,
)

RECOVERY_PROOF_REQUIRED = "RECOVERY_PROOF_REQUIRED"
RECOVERY_SCOPE_MISMATCH = "RECOVERY_SCOPE_MISMATCH"
RECOVERY_CONTROLLER_GONE = "RECOVERY_CONTROLLER_GONE"
RECOVERY_CONTROLLER_UNVERIFIED = "RECOVERY_CONTROLLER_UNVERIFIED"
OPERATION_STORE_IN_USE = "OPERATION_STORE_IN_USE"

_NONTERMINAL = frozenset({"accepted", "promoting", "pending"})
_MAX_PID = 2**32 - 1


@dataclass(frozen=True, slots=True)
class ControllerIdentity:
    """The live control process and its sampled birth identity."""

    pid: int
    birth_identity: str

    def __post_init__(self) -> None:
        if type(self.pid) is not int or self.pid <= 0 or self.pid > _MAX_PID:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "controller PID must be a positive integer",
            )
        _require_birth(self.birth_identity)

    @classmethod
    def current(cls) -> ControllerIdentity:
        pid = os.getpid()
        birth = process_birth_identity(pid)
        if birth is None:
            raise OperationReceiptError(
                OPERATION_STORE_UNVERIFIED,
                "current process birth identity is unavailable",
            )
        return cls(pid, birth)


@dataclass(frozen=True, slots=True)
class RecoveryAuthority:
    """Trust-boundary value supplied by the operator control layer.

    The control layer is responsible for generating and verifying its private
    control proof and for verifying the OS peer.  This service only checks the
    same scope and that the original live controller PID/birth still matches.
    An arbitrary string or a caller-created object is not recovery proof.
    """

    home_binding: str
    host_control_session_id: str
    view_session_id: str
    owner: str | None
    controller_pid: int
    controller_birth: str

    def __post_init__(self) -> None:
        for name in (
            "home_binding",
            "host_control_session_id",
            "view_session_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise OperationReceiptError(
                    RECOVERY_PROOF_REQUIRED,
                    "recovery authority scope must be non-empty",
                )
        if self.owner is not None and (
            not isinstance(self.owner, str) or not self.owner.strip()
        ):
            raise OperationReceiptError(
                RECOVERY_PROOF_REQUIRED,
                "recovery authority owner must be non-empty or null",
            )
        if (
            type(self.controller_pid) is not int
            or self.controller_pid <= 0
            or self.controller_pid > _MAX_PID
        ):
            raise OperationReceiptError(
                RECOVERY_PROOF_REQUIRED,
                "recovery authority controller PID must be positive",
            )
        _require_birth(self.controller_birth, RECOVERY_PROOF_REQUIRED)


class OperationService:
    """Durable, typed operation receipts over one private journal directory."""

    def __init__(
        self,
        root: Path,
        store: OperationStore,
        writer_identities: dict[str, tuple[int, str]],
        controller_identities: dict[str, tuple[int, str]],
        writer: ControllerIdentity,
        controller: ControllerIdentity,
        handoff: "journal.HandoffRecord | None" = None,
    ) -> None:
        self._root = root
        self._store = store
        self._writer_identities = writer_identities
        self._controller_identities = controller_identities
        self._writer = (writer.pid, writer.birth_identity)
        self._controller = (controller.pid, controller.birth_identity)
        self._handoff = handoff
        self._lock = threading.RLock()
        self._unconfirmed: OperationReceiptError | None = None

    @property
    def root(self) -> Path:
        return self._root

    def _ensure_usable(self) -> None:
        if self._unconfirmed is not None:
            raise OperationReceiptError(
                self._unconfirmed.code,
                "operation store is frozen after an unconfirmed commit; "
                "reopen and reconcile before further operations",
            )

    @classmethod
    def open(
        cls,
        root: Path | str,
        *,
        controller: ControllerIdentity | None = None,
        writer: ControllerIdentity | None = None,
    ) -> OperationService:
        """Open one private journal; the caller's bootstrap locks serialize instances.

        ``writer`` exists for the one custody path: a coordinator that *proved*
        the previous writer exited may reopen the journal as that writer, so the
        receipt is not reconciled to ``unknown`` under its feet and an
        ``accepted`` logout can still become ``completed`` or ``failed``
        truthfully.  Ordinary callers must not pass it.
        """

        directory = journal.prepare_root(root)
        writer = ControllerIdentity.current() if writer is None else writer
        if not isinstance(writer, ControllerIdentity):
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "the journal writer identity must be a ControllerIdentity",
            )
        chosen = controller if controller is not None else writer
        if not isinstance(chosen, ControllerIdentity):
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "controller identity must be a ControllerIdentity",
            )
        state = journal.read_journal(directory)
        if state is None:
            store = OperationStore()
            writers: dict[str, tuple[int, str]] = {}
            controllers: dict[str, tuple[int, str]] = {}
        else:
            try:
                store = OperationStore.from_snapshot(state.snapshot)
            except OnboardingStateError as error:
                raise OperationReceiptError(
                    OPERATION_STORE_CORRUPT,
                    "operation journal index is inconsistent",
                ) from error
            writers = {
                operation_id: (pid, birth)
                for operation_id, pid, birth in state.writer_identities
            }
            controllers = {
                operation_id: (pid, birth)
                for operation_id, pid, birth in state.controller_identities
            }
        handoff = None if state is None else state.handoff
        service = cls(directory, store, writers, controllers, writer, chosen, handoff)
        service._reconcile()
        return service

    def _reconcile(self) -> None:
        with self._lock:
            snapshot = self._store.snapshot()
            changes: dict[str, OperationRecord] = {}
            for record in snapshot.records:
                if record.state not in _NONTERMINAL:
                    continue
                writer = self._writer_identities.get(record.operation_id)
                if writer is None:
                    raise OperationReceiptError(
                        OPERATION_STORE_CORRUPT,
                        "operation journal writer identity is missing",
                    )
                pid, birth = writer
                if pid == self._writer[0] and birth == self._writer[1]:
                    continue
                try:
                    probe_process(pid)
                except ProcessLookupError:
                    pass
                except OSError as error:
                    raise OperationReceiptError(
                        OPERATION_STORE_UNVERIFIED,
                        "operation journal writer exit is unverified",
                    ) from error
                else:
                    observed = process_birth_identity(pid)
                    if observed is None:
                        raise OperationReceiptError(
                            OPERATION_STORE_UNVERIFIED,
                            "operation journal writer birth identity is unavailable",
                        )
                    if observed == birth:
                        raise OperationReceiptError(
                            OPERATION_STORE_IN_USE,
                            "an earlier operation writer is still alive",
                        )
                changes[record.operation_id] = replace(
                    record, state="unknown", cancelable=False
                )
            if not changes:
                return
            updated = tuple(
                changes.get(record.operation_id, record) for record in snapshot.records
            )
            working = OperationStore.from_snapshot(
                StoreSnapshot(updated, snapshot.key_index)
            )
            writers = dict(self._writer_identities)
            controllers = dict(self._controller_identities)
            for operation_id, record in changes.items():
                if record.kind == "logout":
                    # A reconciled logout keeps its original writer identity: it
                    # is the generation a coordinator's handoff must name, and
                    # the receipt is terminal from here on, so the identity is
                    # only provenance, never a lock.
                    continue
                writers[operation_id] = self._writer
            self._commit(working, writers, controllers)

    def _clone(self) -> OperationStore:
        return OperationStore.from_snapshot(self._store.snapshot())

    def _commit(
        self,
        working: OperationStore,
        writers: dict[str, tuple[int, str]],
        controllers: dict[str, tuple[int, str]],
    ) -> None:
        # A journal that already carries a handoff this instance does not know
        # about means another writer took custody: refuse instead of silently
        # dropping that section.  ``take_handoff``/``release_handoff`` write
        # directly, so only the ordinary commit path is guarded.
        on_disk = journal.read_journal(self._root)
        recorded = None if on_disk is None else on_disk.handoff
        if recorded is not None and (
            self._handoff is None or recorded.operation_id != self._handoff.operation_id
        ):
            raise OperationReceiptError(
                OPERATION_STORE_IN_USE,
                "another writer holds lifecycle custody of this journal",
            )
        try:
            journal.write_journal(
                self._root,
                working.snapshot(),
                writers,
                controllers,
                self._handoff,
            )
        except OperationReceiptError as error:
            if error.code == OPERATION_STORE_COMMIT_UNCONFIRMED:
                self._unconfirmed = error
            raise
        self._store = working
        self._writer_identities = writers
        self._controller_identities = controllers

    def _mutate(self, action: Callable[[OperationStore], object]) -> object:
        with self._lock:
            self._ensure_usable()
            working = self._clone()
            result = action(working)
            if working.snapshot() == self._store.snapshot():
                return result
            operation_id = result.operation_id
            writers = dict(self._writer_identities)
            controllers = dict(self._controller_identities)
            writers[operation_id] = self._writer
            controllers.setdefault(operation_id, self._controller)
            for record in working.snapshot().records:
                writers.setdefault(record.operation_id, self._writer)
                controllers.setdefault(record.operation_id, self._controller)
            self._commit(working, writers, controllers)
            return result

    def begin_auth(
        self,
        context: CallContext,
        intent: str,
        operation_key: str,
        operation_id: str,
    ) -> OperationRecord:
        authorize_call(LOGIN_BEGIN_SPEC, context)
        return self._mutate(
            lambda store: store.begin_auth(context, intent, operation_key, operation_id)
        )

    def mark_authenticated(
        self,
        context: CallContext,
        operation_id: str,
        owner: str,
    ) -> OperationRecord:
        return self._mutate(
            lambda store: store.mark_authenticated(context, operation_id, owner)
        )

    def mark_auth_unknown(
        self, context: CallContext, operation_id: str
    ) -> OperationRecord:
        return self._mutate(
            lambda store: store.mark_auth_unknown(context, operation_id)
        )

    def mark_auth_ended(
        self,
        context: CallContext,
        operation_id: str,
        state: str,
        *,
        error: OperationError | None = None,
    ) -> OperationRecord:
        return self._mutate(
            lambda store: store.mark_auth_ended(
                context, operation_id, state, error=error
            )
        )

    def get(self, context: CallContext, operation_id: str) -> OperationRecord:
        authorize_call(AUTH_OPERATION_GET_SPEC, context)
        with self._lock:
            self._ensure_usable()
            return self._store.get(context, operation_id)

    def cancel(self, context: CallContext, operation_id: str) -> CancelOutcome:
        authorize_call(AUTH_OPERATION_CANCEL_SPEC, context)
        return self._mutate(lambda store: store.cancel(context, operation_id))

    def accept_initialize(
        self,
        context: CallContext,
        operation_key: str,
        operation_id: str,
        receipt_id: str,
        *,
        promotion: Callable[[OperationRecord], None],
    ) -> OperationRecord:
        if not callable(promotion):
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "initialize promotion callback must be callable",
            )
        authorize_call(INITIALIZE_SPEC, context)
        with self._lock:
            self._ensure_usable()
            working = self._clone()
            record = working.begin_initialize(
                context, operation_key, operation_id, receipt_id
            )
            changed = working.snapshot() != self._store.snapshot()
            if changed:
                writers = dict(self._writer_identities)
                controllers = dict(self._controller_identities)
                writers[record.operation_id] = self._writer
                controllers.setdefault(record.operation_id, self._controller)
                for item in working.snapshot().records:
                    writers.setdefault(item.operation_id, self._writer)
                    controllers.setdefault(item.operation_id, self._controller)
                self._commit(working, writers, controllers)
            if changed:
                claim = self._mutate(
                    lambda store: store.claim_promotion(context, record.operation_id)
                )
                if claim.started:
                    promotion(claim.record)
                return self._store.get(context, record.operation_id)
            return record

    def mark_initialize(
        self,
        context: CallContext,
        operation_id: str,
        state: str,
    ) -> OperationRecord:
        return self._mutate(
            lambda store: store.mark_initialize(context, operation_id, state)
        )

    def begin_device(
        self,
        context: CallContext,
        operation_key: str,
        operation_id: str,
        receipt_id: str,
    ) -> OperationRecord:
        authorize_call(DEVICE_ENSURE_SPEC, context)
        return self._mutate(
            lambda store: store.begin_device(
                context, operation_key, operation_id, receipt_id
            )
        )

    def mark_device(
        self,
        context: CallContext,
        operation_id: str,
        state: str,
    ) -> OperationRecord:
        return self._mutate(
            lambda store: store.mark_device(context, operation_id, state)
        )

    def begin_logout(
        self,
        context: CallContext,
        operation_key: str,
        operation_id: str,
        receipt_id: str,
    ) -> OperationRecord:
        """Persist the durable acceptance a logout coordinator will act on.

        The write happens here, before any caller is allowed to stop a daemon or
        remove an artifact, and it goes through the one ``_mutate`` path so the
        journal keeps a single writer.
        """

        authorize_call(AUTH_LOGOUT_SPEC, context)
        return self._mutate(
            lambda store: store.begin_logout(
                context, operation_key, operation_id, receipt_id
            )
        )

    def mark_logout(
        self,
        context: CallContext,
        operation_id: str,
        state: str,
        removed: tuple[str, ...] = (),
    ) -> OperationRecord:
        """Move one accepted logout to a truthful terminal state."""

        authorize_call(AUTH_LOGOUT_SPEC, context)
        return self._mutate(
            lambda store: store.mark_logout(context, operation_id, state, removed)
        )

    @property
    def handoff(self) -> journal.HandoffRecord | None:
        with self._lock:
            return self._handoff

    def take_handoff(self, handoff: journal.HandoffRecord) -> journal.HandoffRecord:
        """Persist one fenced lifecycle handoff; the only custody transfer.

        The caller is responsible for having *proved* the old writer's exit; this
        method refuses a handoff whose receipt is not a logout of the same
        surface, so a malformed transfer cannot become an escape hatch around the
        general recovery rules.
        """

        if not isinstance(handoff, journal.HandoffRecord):
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "a lifecycle handoff record is required",
            )
        with self._lock:
            self._ensure_usable()
            snapshot = self._store.snapshot()
            record = next(
                (
                    item
                    for item in snapshot.records
                    if item.operation_id == handoff.operation_id
                ),
                None,
            )
            if (
                record is None
                or record.kind != "logout"
                or record.receipt_id != handoff.receipt_id
                or record.surface != handoff.surface
            ):
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "a lifecycle handoff must name one existing logout receipt",
                )
            # Fence 1: the handoff must name the *recorded* writer, so custody
            # cannot be taken from a live or a different generation.  The
            # reconciled ``unknown`` path keeps ``writer_identities`` intact, so
            # this still holds after a crash.
            writer = self._writer_identities.get(handoff.operation_id)
            if writer is None:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "the logout receipt has no recorded writer identity",
                )
            if (handoff.old_pid, handoff.old_birth) != writer:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "a lifecycle handoff must name the receipt's recorded writer",
                )
            # Fence 1b: only a receipt that has not reached a terminal state may
            # be taken.  A completed logout needs no custody, and a failed one
            # needs a fresh operator action rather than a handoff bypass.
            if record.state not in {"accepted", "unknown"}:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"a {record.state} logout receipt cannot be taken",
                )
            # Fence 2: the epoch is the receipt's own transport generation.
            if handoff.old_epoch != record.surface.transport_generation:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "a lifecycle handoff epoch must be the receipt's generation",
                )
            # Fence 3: an expired handoff cannot take custody, and no caller may
            # present a window that has already closed.
            if handoff.expires_at <= _handoff_now():
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "the lifecycle handoff window has already closed",
                )
            previous = self._handoff
            self._handoff = handoff
            try:
                journal.write_journal(
                    self._root,
                    snapshot,
                    self._writer_identities,
                    self._controller_identities,
                    handoff,
                )
            except BaseException:
                self._handoff = previous
                raise
            return handoff

    def release_handoff(self) -> None:
        """Drop the custody section after completion, keeping the receipts."""

        with self._lock:
            if self._handoff is None:
                return
            self._ensure_usable()
            self._handoff = None
            journal.write_journal(
                self._root,
                self._store.snapshot(),
                self._writer_identities,
                self._controller_identities,
                None,
            )

    def visible(self, context: CallContext) -> tuple[OperationRecord, ...]:
        with self._lock:
            self._ensure_usable()
            return self._store.visible(context)

    def status(
        self,
        context: CallContext,
        *,
        first_run: FirstRunSnapshot | None = None,
        username: str | None = None,
    ) -> dict:
        authorize_call(STATUS_SPEC, context)
        with self._lock:
            self._ensure_usable()
            return onboarding_status(
                self._store, context, first_run=first_run, username=username
            )

    def migrate_generation(
        self,
        context: CallContext,
        operation_id: str,
        authority: RecoveryAuthority | None,
    ) -> OperationRecord:
        with self._lock:
            self._ensure_usable()
            if not isinstance(authority, RecoveryAuthority):
                raise OperationReceiptError(
                    RECOVERY_PROOF_REQUIRED,
                    "typed recovery authority is required",
                )
            snapshot = self._store.snapshot()
            record = next(
                (
                    item
                    for item in snapshot.records
                    if item.operation_id == operation_id
                ),
                None,
            )
            if record is None:
                raise OnboardingStateError(
                    AUTH_OPERATION_NOT_FOUND, "unknown operation in this context"
                )
            if (
                record.surface.home_binding != context.surface.home_binding
                or record.surface.host_control_session_id
                != context.surface.host_control_session_id
                or record.surface.view_session_id != context.surface.view_session_id
            ):
                raise OnboardingStateError(
                    AUTH_OPERATION_NOT_FOUND,
                    "the operation is not visible in this home, control session or view",
                )
            if context.owner != record.account_owner:
                raise OnboardingStateError(
                    ipc_errors.IDENTITY_CONFLICT,
                    "the operation belongs to a different owner",
                )
            persisted = self._controller_identities.get(operation_id)
            if persisted is None:
                raise OperationReceiptError(
                    OPERATION_STORE_CORRUPT,
                    "operation journal controller identity is missing",
                )
            if (
                authority.home_binding != record.surface.home_binding
                or authority.host_control_session_id
                != record.surface.host_control_session_id
                or authority.view_session_id != record.surface.view_session_id
                or authority.owner != record.account_owner
                or (authority.controller_pid, authority.controller_birth) != persisted
            ):
                raise OperationReceiptError(
                    RECOVERY_SCOPE_MISMATCH,
                    "recovery authority does not match the operation scope",
                )
            _require_live_controller(
                authority.controller_pid, authority.controller_birth
            )
            if (
                record.surface.transport_generation
                == context.surface.transport_generation
            ):
                return record
            updated = replace(
                record,
                surface=replace(
                    record.surface,
                    transport_generation=context.surface.transport_generation,
                ),
            )
            updated_records = tuple(
                updated if item.operation_id == operation_id else item
                for item in snapshot.records
            )
            working = OperationStore.from_snapshot(
                StoreSnapshot(updated_records, snapshot.key_index)
            )
            writers = dict(self._writer_identities)
            writers[operation_id] = self._writer
            self._commit(
                working,
                writers,
                dict(self._controller_identities),
            )
            return working.get(context, operation_id)


def _handoff_now() -> str:
    """The current UTC instant in the journal's ISO-8601 form."""

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _require_birth(value: str, code: str = OPERATION_STORE_UNVERIFIED) -> None:
    if not isinstance(value, str) or not value.strip():
        raise OperationReceiptError(
            code,
            "process birth identity must be non-empty",
        )


def _require_live_controller(pid: int, birth: str) -> None:
    if type(pid) is not int or pid <= 0 or pid > _MAX_PID:
        raise OperationReceiptError(
            RECOVERY_CONTROLLER_UNVERIFIED,
            "control process PID is out of bounds",
        )
    try:
        probe_process(pid)
    except ProcessLookupError as error:
        raise OperationReceiptError(
            RECOVERY_CONTROLLER_GONE,
            "the original control process is no longer alive",
        ) from error
    except OSError as error:
        raise OperationReceiptError(
            RECOVERY_CONTROLLER_UNVERIFIED,
            "the original control process liveness is unverified",
        ) from error
    observed = process_birth_identity(pid)
    if observed is None:
        raise OperationReceiptError(
            RECOVERY_CONTROLLER_UNVERIFIED,
            "the original control process birth identity is unavailable",
        )
    if observed != birth:
        raise OperationReceiptError(
            RECOVERY_CONTROLLER_GONE,
            "the original control process PID has been reused",
        )


__all__ = [
    "ControllerIdentity",
    "OPERATION_STORE_IN_USE",
    "OperationService",
    "RECOVERY_CONTROLLER_GONE",
    "RECOVERY_CONTROLLER_UNVERIFIED",
    "RECOVERY_PROOF_REQUIRED",
    "RECOVERY_SCOPE_MISMATCH",
    "RecoveryAuthority",
]
