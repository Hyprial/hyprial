"""Pure ownership and state rules for the M2 onboarding declarations.

Decision: gui-m2-onboarding-contracts-20261105.  This module is deliberately
not a BootstrapRuntime: it never authenticates, never touches a home, never
contacts an external identity service and never performs promotion.  It exists
so the ownership, idempotency, unknown-outcome and no-replay rules can be tested
before the runtime is assembled.  Direct calls from an old transport generation
are rejected; a runtime may only query an old receipt after the control channel
has verified a new recovery proof.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from hyprial.error.codes import AUTH_OPERATION_CONFLICT, AUTH_OPERATION_NOT_FOUND
from hyprial.kernel import MethodSpec, ipc_errors

from .first_run import FirstRunSnapshot
from .projection import onboarding_status, status_snapshot
from .schemas import LOGOUT_REMOVED_LABELS, NEXT_ACTIONS, OperationError

AUTH_INTENTS = frozenset({"login", "register"})
PHASES = frozenset({"bootstrap", "serving"})
CALLERS = frozenset({"local-bootstrap", "local-gui", "local-operator"})
INITIALIZE_STATES = frozenset(
    {"accepted", "promoting", "cancelled", "completed", "failed", "unknown"}
)
DEVICE_STATES = frozenset({"pending", "ready", "failed", "unknown"})
# Account logout has no ``cancelled`` and no ``promoting`` terminal: acceptance is
# durable before any side effect, and the only successful end state is
# ``completed`` with a truthful ``removed`` projection.
LOGOUT_STATES = frozenset({"accepted", "completed", "failed", "unknown"})
LOGOUT_KIND = "logout"


class OnboardingStateError(Exception):
    """One refused pure-state transition or ownership check."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class Surface:
    home_binding: str
    host_control_session_id: str
    view_session_id: str
    transport_generation: str

    def __post_init__(self) -> None:
        for name in (
            "home_binding",
            "host_control_session_id",
            "view_session_id",
            "transport_generation",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT, f"{name} must be non-empty"
                )


@dataclass(frozen=True, slots=True)
class CallContext:
    surface: Surface
    phase: str
    caller: str
    owner: str | None = None

    def __post_init__(self) -> None:
        if self.phase not in PHASES:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "unknown onboarding phase"
            )
        if self.caller not in CALLERS:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "unknown onboarding caller"
            )
        if self.owner is not None and (
            not isinstance(self.owner, str) or not self.owner
        ):
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "owner must be non-empty or null"
            )


@dataclass(frozen=True, slots=True)
class OperationRecord:
    operation_id: str
    operation_key: str
    kind: str
    surface: Surface
    state: str
    receipt_id: str | None = None
    account_owner: str | None = None
    cancelable: bool = True
    # Logout-only projection.  Labels are bounded by the public schema; the
    # record stores them so a completed receipt reports what was removed rather
    # than a boolean success.
    removed: tuple[str, ...] = ()
    error: OperationError | None = None


@dataclass(frozen=True, slots=True)
class StoreSnapshot:
    records: tuple[OperationRecord, ...]
    key_index: tuple[tuple[str, str, str, str, str, str], ...]


CREDENTIAL_STATES = frozenset({"unverified", "pending", "committed", "failed"})


@dataclass(frozen=True, slots=True)
class AccountStatus:
    """The account-level projection of one home, not of one receipt.

    A local identity (``owner``), the online credential and the local device
    are three different facts.  ``authenticated`` follows only the committed
    public identity, so a refused or failed reauthentication never erases a
    working local login.  ``ready`` additionally requires a provisioned device
    and a completed initialize receipt.  ``next_actions`` is the only part the
    frozen wire schema can carry; ``recovery_action`` names what an operator
    must do for states that enum cannot express.
    """

    owner: str | None = None
    mode: str | None = None
    issuer: str | None = None
    credential: str = "unverified"
    device_ready: bool = False
    device_id: str | None = None
    initialize_completed: bool = False
    username: str | None = None

    def __post_init__(self) -> None:
        if self.owner is not None and (
            not isinstance(self.owner, str) or not self.owner
        ):
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "account owner must be non-empty or null"
            )
        for name in ("mode", "issuer", "device_id", "username"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value):
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"account {name} must be non-empty or null",
                )
        if self.credential not in CREDENTIAL_STATES:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "unknown credential state"
            )
        for name in ("device_ready", "initialize_completed"):
            if type(getattr(self, name)) is not bool:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT, f"account {name} must be boolean"
                )
        if self.device_ready and self.device_id is None:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "a ready device must name its device id",
            )

    @property
    def authenticated(self) -> bool:
        return self.owner is not None

    @property
    def initialized(self) -> bool:
        return self.authenticated and self.device_ready and self.initialize_completed

    @property
    def ready(self) -> bool:
        return self.initialized and self.credential != "failed"

    @property
    def next_actions(self) -> tuple[str, ...]:
        if not self.authenticated:
            return ("login",)
        if not self.device_ready:
            return ("device.ensure",)
        if not self.initialize_completed:
            return ("initialize",)
        return ()

    @property
    def recovery_action(self) -> str | None:
        if self.credential == "failed":
            return "relogin"
        actions = self.next_actions
        return actions[0] if actions else None

    def as_status(self) -> dict:
        actions = self.next_actions
        if not all(action in NEXT_ACTIONS for action in actions):
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "account recovery action is outside the declared status enum",
            )
        return {
            "authenticated": self.authenticated,
            "owner": self.owner,
            "username": self.username,
            "deviceReady": self.device_ready,
            "initialized": self.initialized,
            "nextActions": list(actions),
        }


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    operation_id: str
    state: str
    cancelled: bool
    reason: str


@dataclass(frozen=True, slots=True)
class PromotionClaim:
    record: OperationRecord
    started: bool

    @property
    def operation_id(self) -> str:
        return self.record.operation_id


def check_removed_labels(removed: tuple[str, ...]) -> None:
    """Refuse an artifact label the wire contract cannot truthfully report.

    Account logout may only name login-owned artifacts, in the canonical order
    the identity-owned removal primitive produces them.  A duplicate, an
    out-of-order subset or an unrelated path is refused before any projection.
    """

    if not all(isinstance(label, str) and label for label in removed):
        raise OnboardingStateError(
            ipc_errors.INVALID_ARGUMENT, "removed labels must be non-empty strings"
        )
    position = -1
    for label in removed:
        try:
            index = LOGOUT_REMOVED_LABELS.index(label)
        except ValueError:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "removed label is not a login-owned artifact label",
            ) from None
        if index <= position:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "removed labels must follow the canonical artifact order",
            )
        position = index


def authorize_call(spec: MethodSpec, context: CallContext) -> None:
    """Enforce the declared exact phase/caller constraint before any handler runs."""

    constraint = spec.phase_caller
    if constraint is None:
        return
    if not constraint.allows(context.phase, context.caller):
        raise OnboardingStateError(
            ipc_errors.CALLER_NOT_AUTHORIZED,
            f"{spec.name} is not callable by {context.caller} in {context.phase}",
        )


def _check_surface(record: Surface, current: Surface) -> None:
    if (
        record.home_binding != current.home_binding
        or record.host_control_session_id != current.host_control_session_id
        or record.view_session_id != current.view_session_id
    ):
        raise OnboardingStateError(
            AUTH_OPERATION_NOT_FOUND,
            "the operation is not visible in this home, control session or view",
        )
    if record.transport_generation != current.transport_generation:
        raise OnboardingStateError(
            ipc_errors.STALE_DAEMON_GENERATION,
            "the transport generation is stale for this operation",
        )


def _same_view(left: Surface, right: Surface) -> bool:
    return (
        left.home_binding == right.home_binding
        and left.host_control_session_id == right.host_control_session_id
        and left.view_session_id == right.view_session_id
    )


class OperationStore:
    """In-memory pure-state model; durable storage belongs to the runtime."""

    def __init__(self) -> None:
        self._records: dict[str, OperationRecord] = {}
        self._keys: dict[tuple[str, str, str, str, str], str] = {}

    def snapshot(self) -> StoreSnapshot:
        """Return an immutable copy suitable for durable storage and replay."""

        records = tuple(
            sorted(self._records.values(), key=lambda record: record.operation_id)
        )
        key_index = tuple(
            sorted(
                (namespace, home, host, view, operation_key, operation_id)
                for (
                    namespace,
                    home,
                    host,
                    view,
                    operation_key,
                ), operation_id in self._keys.items()
            )
        )
        return StoreSnapshot(records=records, key_index=key_index)

    @classmethod
    def from_snapshot(cls, snapshot: StoreSnapshot) -> OperationStore:
        """Rebuild the store only from a structurally consistent snapshot."""

        if not isinstance(snapshot, StoreSnapshot):
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "operation snapshot is required"
            )
        store = cls()
        for record in snapshot.records:
            if not isinstance(record, OperationRecord):
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT, "operation snapshot has a bad record"
                )
            store._reserve(record)
        seen: set[str] = set()
        for row in snapshot.key_index:
            if not isinstance(row, tuple) or len(row) != 6:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT, "operation snapshot has a bad key row"
                )
            namespace, home, host, view, operation_key, operation_id = row
            if not all(
                isinstance(item, str) and item
                for item in (namespace, home, host, view, operation_key, operation_id)
            ):
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "operation snapshot key row must be non-empty text",
                )
            record = store._records.get(operation_id)
            if record is None:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "operation snapshot key row points at no record",
                )
            if operation_id in seen:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "operation snapshot has duplicate key rows",
                )
            expected_namespace = (
                "auth" if record.kind in AUTH_INTENTS else record.kind
            )
            if (
                namespace != expected_namespace
                or operation_key != record.operation_key
                or home != record.surface.home_binding
                or host != record.surface.host_control_session_id
                or view != record.surface.view_session_id
            ):
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "operation snapshot key row does not match its record",
                )
            key = (namespace, home, host, view, operation_key)
            if key in store._keys:
                raise OnboardingStateError(
                    ipc_errors.INVALID_ARGUMENT,
                    "operation snapshot has duplicate keys",
                )
            store._keys[key] = operation_id
            seen.add(operation_id)
        if set(store._records) != seen:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT,
                "operation snapshot record has no key row",
            )
        return store

    def _record(
        self,
        context: CallContext,
        operation_id: str,
        *,
        owner_required: bool = False,
    ) -> OperationRecord:
        record = self._records.get(operation_id)
        if record is None:
            raise OnboardingStateError(
                AUTH_OPERATION_NOT_FOUND, "unknown operation in this context"
            )
        _check_surface(record.surface, context.surface)
        if owner_required and context.owner is None:
            raise OnboardingStateError(
                ipc_errors.IDENTITY_UNBOUND, "a trusted owner is required"
            )
        if record.account_owner != context.owner:
            raise OnboardingStateError(
                ipc_errors.IDENTITY_CONFLICT,
                "the operation belongs to a different owner",
            )
        return record

    def _key(
        self, surface: Surface, kind: str, operation_key: str
    ) -> tuple[str, str, str, str, str]:
        return (
            kind,
            surface.home_binding,
            surface.host_control_session_id,
            surface.view_session_id,
            operation_key,
        )

    def _by_key(
        self, surface: Surface, kind: str, operation_key: str
    ) -> OperationRecord | None:
        operation_id = self._keys.get(self._key(surface, kind, operation_key))
        return self._records.get(operation_id) if operation_id else None

    def _pending_auth(self, surface: Surface) -> OperationRecord | None:
        for record in self._records.values():
            if (
                record.kind in AUTH_INTENTS
                and record.state == "pending"
                and _same_view(record.surface, surface)
            ):
                return record
        return None

    def _reserve(self, record: OperationRecord) -> None:
        if record.operation_id in self._records:
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT,
                "the operation id already belongs to another operation",
            )
        self._records[record.operation_id] = record

    def begin_auth(
        self,
        context: CallContext,
        intent: str,
        operation_key: str,
        operation_id: str,
    ) -> OperationRecord:
        if intent not in AUTH_INTENTS:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "intent must be login or register"
            )
        existing = self._by_key(context.surface, "auth", operation_key)
        if existing is not None:
            resumed = self._record(
                context, existing.operation_id
            )
            if resumed.kind != intent:
                raise OnboardingStateError(
                    AUTH_OPERATION_CONFLICT,
                    "the operation key already belongs to another intent",
                )
            return resumed
        pending = self._pending_auth(context.surface)
        if pending is not None:
            raise OnboardingStateError(
                ipc_errors.OPERATION_IN_PROGRESS,
                "another login or registration operation is pending",
            )
        record = OperationRecord(
            operation_id=operation_id,
            operation_key=operation_key,
            kind=intent,
            surface=context.surface,
            state="pending",
            account_owner=context.owner,
            cancelable=True,
        )
        self._reserve(record)
        self._keys[self._key(context.surface, "auth", operation_key)] = operation_id
        return record

    def mark_authenticated(
        self,
        context: CallContext,
        operation_id: str,
        owner: str,
    ) -> OperationRecord:
        if not owner:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "authenticated owner must be non-empty"
            )
        record = self._record(
            context, operation_id
        )
        if record.kind not in AUTH_INTENTS:
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT, "only a login or registration can authenticate"
            )
        if context.owner is not None and context.owner != owner:
            raise OnboardingStateError(
                ipc_errors.IDENTITY_CONFLICT,
                "the authenticated owner differs from the control owner",
            )
        if record.account_owner is not None and record.account_owner != owner:
            raise OnboardingStateError(
                ipc_errors.IDENTITY_CONFLICT,
                "the operation is already bound to another owner",
            )
        if record.state == "authenticated" and record.account_owner == owner:
            return record
        if record.state != "pending":
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT,
                "a terminal auth operation cannot authenticate again",
            )
        updated = replace(
            record,
            state="authenticated",
            account_owner=owner,
            cancelable=False,
        )
        self._records[operation_id] = updated
        return updated

    def mark_auth_unknown(
        self, context: CallContext, operation_id: str
    ) -> OperationRecord:
        return self.mark_auth_ended(context, operation_id, "unknown")

    def mark_auth_ended(
        self,
        context: CallContext,
        operation_id: str,
        state: str,
        *,
        error: OperationError | None = None,
    ) -> OperationRecord:
        if state not in {"unknown", "expired", "denied", "failed"}:
            raise OnboardingStateError(AUTH_OPERATION_CONFLICT, "invalid auth outcome")
        if error is not None and state != "failed":
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT, "only a failed auth operation may carry an error"
            )
        record = self._record(context, operation_id)
        if record.kind not in AUTH_INTENTS:
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT, "only a login or registration can end"
            )
        if record.state == state:
            if state == "failed" and error is not None and record.error is None:
                updated = replace(record, cancelable=False, error=error)
                self._records[operation_id] = updated
                return updated
            return record
        if record.state != "pending":
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT,
                "only a pending auth operation can end",
            )
        updated = replace(record, state=state, cancelable=False, error=error)
        self._records[operation_id] = updated
        return updated

    def get(self, context: CallContext, operation_id: str) -> OperationRecord:
        return self._record(context, operation_id)

    def cancel(self, context: CallContext, operation_id: str) -> CancelOutcome:
        record = self._record(
            context, operation_id
        )
        if record.kind == "initialize":
            return self._cancel_initialize(record)
        if record.kind == LOGOUT_KIND:
            # Accepted logout is never cancellable: the acceptance is durable
            # before any side effect, so cancelling it would be a lie.
            return CancelOutcome(
                operation_id, record.state, False, "not-cancellable"
            )
        if record.kind not in AUTH_INTENTS:
            raise OnboardingStateError(
                AUTH_OPERATION_NOT_FOUND,
                "operation cancel is limited to login, registration and initialize",
            )
        if record.state == "cancelled":
            return CancelOutcome(
                operation_id, "cancelled", True, "already-cancelled"
            )
        if record.state == "unknown":
            return CancelOutcome(
                operation_id, "unknown", False, "outcome-unknown"
            )
        if record.state == "authenticated":
            return CancelOutcome(
                operation_id, "authenticated", False, "identity-committed"
            )
        if record.state in {"denied", "expired", "failed"}:
            return CancelOutcome(
                operation_id, record.state, False, "not-cancellable"
            )
        updated = replace(record, state="cancelled", cancelable=False)
        self._records[operation_id] = updated
        return CancelOutcome(operation_id, "cancelled", True, "cancelled")

    def _cancel_initialize(self, record: OperationRecord) -> CancelOutcome:
        state = record.state
        if state == "accepted" and record.cancelable:
            updated = replace(record, state="cancelled", cancelable=False)
            self._records[record.operation_id] = updated
            return CancelOutcome(
                record.operation_id, "cancelled", True, "cancelled"
            )
        if state == "cancelled":
            return CancelOutcome(
                record.operation_id, "cancelled", True, "already-cancelled"
            )
        if state == "promoting":
            return CancelOutcome(
                record.operation_id, "promoting", False, "not-cancellable"
            )
        if state == "completed":
            return CancelOutcome(
                record.operation_id, "completed", False, "identity-committed"
            )
        if state == "failed":
            return CancelOutcome(
                record.operation_id, "failed", False, "not-cancellable"
            )
        return CancelOutcome(
            record.operation_id, "unknown", False, "outcome-unknown"
        )

    def claim_promotion(
        self, context: CallContext, operation_id: str
    ) -> PromotionClaim:
        record = self._record(
            context,
            operation_id,
            owner_required=True,
        )
        if record.kind != "initialize":
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT, "not an initialize receipt"
            )
        if record.state != "accepted" or not record.cancelable:
            return PromotionClaim(record, False)
        updated = replace(record, state="promoting", cancelable=False)
        self._records[operation_id] = updated
        return PromotionClaim(updated, True)

    def begin_initialize(
        self,
        context: CallContext,
        operation_key: str,
        operation_id: str,
        receipt_id: str,
    ) -> OperationRecord:
        if context.owner is None:
            raise OnboardingStateError(
                ipc_errors.IDENTITY_UNBOUND,
                "initialize requires a completed identity and trusted owner",
            )
        existing_key = self._by_key(context.surface, "initialize", operation_key)
        if existing_key is not None:
            return self._record(
                context, existing_key.operation_id
            )
        for record in self._records.values():
            if (
                record.kind == "initialize"
                and _same_view(record.surface, context.surface)
                and (
                    record.surface.transport_generation
                    == context.surface.transport_generation
                    or record.state in {"accepted", "promoting", "unknown"}
                )
            ):
                return self._record(
                    context, record.operation_id
                )
        pending = self._pending_auth(context.surface)
        if pending is not None:
            raise OnboardingStateError(
                ipc_errors.OPERATION_IN_PROGRESS,
                "a pending auth operation must finish before initialize",
            )
        record = OperationRecord(
            operation_id=operation_id,
            operation_key=operation_key,
            kind="initialize",
            surface=context.surface,
            state="accepted",
            receipt_id=receipt_id,
            account_owner=context.owner,
            cancelable=True,
        )
        self._reserve(record)
        self._keys[self._key(context.surface, "initialize", operation_key)] = operation_id
        return record

    def mark_initialize(
        self,
        context: CallContext,
        operation_id: str,
        state: str,
    ) -> OperationRecord:
        if state not in INITIALIZE_STATES:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "unknown initialize state"
            )
        record = self._record(
            context,
            operation_id,
            owner_required=True,
        )
        if record.kind != "initialize":
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT, "not an initialize receipt"
            )
        allowed = {
            "accepted": {"accepted"},
            "promoting": {"promoting", "completed", "failed", "unknown"},
            "cancelled": {"cancelled"},
            "failed": {"failed"},
            "unknown": {"unknown", "completed"},
            "completed": {"completed"},
        }
        if state not in allowed[record.state]:
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT,
                f"initialize cannot move from {record.state} to {state}",
            )
        updated = replace(
            record,
            state=state,
            cancelable=state == "accepted",
        )
        self._records[operation_id] = updated
        return updated

    def begin_device(
        self,
        context: CallContext,
        operation_key: str,
        operation_id: str,
        receipt_id: str,
    ) -> OperationRecord:
        if context.owner is None:
            raise OnboardingStateError(
                ipc_errors.IDENTITY_UNBOUND,
                "device.ensure requires a completed identity and trusted owner",
            )
        existing_key = self._by_key(context.surface, "device", operation_key)
        if existing_key is not None:
            return self._record(
                context, existing_key.operation_id
            )
        for record in self._records.values():
            if (
                record.kind == "device"
                and _same_view(record.surface, context.surface)
                and record.surface.transport_generation
                == context.surface.transport_generation
                and record.state == "ready"
            ):
                return self._record(
                    context, record.operation_id
                )
        record = OperationRecord(
            operation_id=operation_id,
            operation_key=operation_key,
            kind="device",
            surface=context.surface,
            state="pending",
            receipt_id=receipt_id,
            account_owner=context.owner,
            cancelable=False,
        )
        self._reserve(record)
        self._keys[self._key(context.surface, "device", operation_key)] = operation_id
        return record

    def mark_device(
        self,
        context: CallContext,
        operation_id: str,
        state: str,
    ) -> OperationRecord:
        if state not in DEVICE_STATES or state == "pending":
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "device state must be ready, failed or unknown"
            )
        record = self._record(
            context,
            operation_id,
            owner_required=True,
        )
        if record.kind != "device":
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT, "not a device receipt"
            )
        if record.state == state:
            return record
        if record.state != "pending":
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT,
                "a terminal device receipt cannot change state",
            )
        updated = replace(record, state=state)
        self._records[operation_id] = updated
        return updated

    def begin_logout(
        self,
        context: CallContext,
        operation_key: str,
        operation_id: str,
        receipt_id: str,
    ) -> OperationRecord:
        """Accept at most one logout per home view; a replay returns it.

        Nothing here stops a daemon or removes an artifact.  This records the
        durable acceptance the lifecycle coordinator will act on, and it is the
        only place a second concurrent logout can be refused.
        """

        existing_key = self._by_key(context.surface, LOGOUT_KIND, operation_key)
        if existing_key is not None:
            return self._record(context, existing_key.operation_id)
        for record in self._records.values():
            if (
                record.kind == LOGOUT_KIND
                and _same_view(record.surface, context.surface)
                and record.surface.transport_generation
                == context.surface.transport_generation
            ):
                return self._record(context, record.operation_id)
        pending = self._pending_auth(context.surface)
        if pending is not None:
            raise OnboardingStateError(
                ipc_errors.OPERATION_IN_PROGRESS,
                "a pending auth operation must finish before logout",
            )
        record = OperationRecord(
            operation_id=operation_id,
            operation_key=operation_key,
            kind=LOGOUT_KIND,
            surface=context.surface,
            state="accepted",
            receipt_id=receipt_id,
            account_owner=context.owner,
            cancelable=False,
        )
        self._reserve(record)
        self._keys[self._key(context.surface, LOGOUT_KIND, operation_key)] = (
            operation_id
        )
        return record

    def mark_logout(
        self,
        context: CallContext,
        operation_id: str,
        state: str,
        removed: tuple[str, ...] = (),
    ) -> OperationRecord:
        """Move one accepted logout to a truthful terminal state."""

        if state not in LOGOUT_STATES:
            raise OnboardingStateError(
                ipc_errors.INVALID_ARGUMENT, "unknown logout state"
            )
        check_removed_labels(removed)
        record = self._record(context, operation_id)
        if record.kind != LOGOUT_KIND:
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT, "not a logout receipt"
            )
        allowed = {
            "accepted": {"completed", "failed", "unknown"},
            "failed": {"failed"},
            "unknown": {"unknown", "completed"},
            "completed": {"completed"},
        }
        if state not in allowed[record.state]:
            raise OnboardingStateError(
                AUTH_OPERATION_CONFLICT,
                f"logout cannot move from {record.state} to {state}",
            )
        updated = replace(
            record,
            state=state,
            # A ``failed`` receipt must keep the labels it really removed: the
            # contract allows them on every terminal state except ``accepted``
            # (and never invents them for ``unknown``, which is neither a
            # rollback nor a success).
            removed=tuple(removed) if state in {"completed", "failed"} else record.removed,
            cancelable=False,
        )
        self._records[operation_id] = updated
        return updated

    def visible(self, context: CallContext) -> tuple[OperationRecord, ...]:
        records: list[OperationRecord] = []
        for record in self._records.values():
            if not _same_view(record.surface, context.surface):
                continue
            if record.surface.transport_generation != context.surface.transport_generation:
                continue
            if record.account_owner != context.owner:
                continue
            records.append(record)
        return tuple(sorted(records, key=lambda record: record.operation_id))


__all__ = [
    "CREDENTIAL_STATES",
    "AccountStatus",
    "CancelOutcome",
    "CallContext",
    "FirstRunSnapshot",
    "LOGOUT_KIND",
    "LOGOUT_STATES",
    "OnboardingStateError",
    "OperationError",
    "OperationRecord",
    "OperationStore",
    "PromotionClaim",
    "StoreSnapshot",
    "Surface",
    "authorize_call",
    "check_removed_labels",
    "onboarding_status",
    "status_snapshot",
]
