# Decision record (Z-3, owner decision 2026-11-16: auto-complete on serving start):
# - Scope.  A daemon that starts serving on a home whose owner is committed but
#   whose device registration or initialize never finished completes that work
#   itself.  Nothing is exposed to outside callers: ``device.ensure`` and
#   ``initialize`` stay bootstrap-only in the request catalog, and this module
#   is reached only from ``ServingOnboardingGateway.on_daemon_ready``.
# - Readiness is never held hostage.  Device provisioning is a local ``genkey``
#   subprocess of the Tailcat sidecar, bounded at 15 s; it does not reach the
#   network.  A damaged journal can still fail at any read.  The whole recovery
#   therefore runs on one daemon thread started from the ready hook; the hook
#   returns at once, so the ready signal, the pid file and the accept loop never
#   wait on it, and no join happens on the start path.  The budget the start
#   path pays is the cost of creating a thread.
# - Shutdown.  The daemon shutdown path does not call ``close``; only
#   ``ServingOnboardingGateway.close`` does, and the tests are its callers.  At
#   process exit the thread is a daemon thread that is simply left behind.  That
#   is safe: every journal write is temp file + fsync + replace, so a write is
#   all or nothing, and a device receipt left ``pending`` is reconciled to
#   ``unknown`` by the next start.  ``close`` waits a bounded 2 s and abandons
#   the thread otherwise.
# - Fault isolation.  Every exception, whether from the identity read, the
#   journal read, the provisioner or a receipt transition, is logged and leaves
#   state exactly as it was; a half-done sequence is safe because each step is
#   a separate durable receipt transition and the next start re-evaluates from
#   the durable facts.  An unreadable journal is never repaired here.
# - Bounded growth (F3).  A failed attempt used to append a device receipt per
#   serving start, and the journal is capped (4 MiB, 10,000 records), so a
#   service-manager restart loop could fill it and then fail every journal write,
#   logout and login included.  Recovery now writes a ``pending`` receipt, and
#   its ``failed``/``unknown`` outcome, only while the owner has fewer than
#   ``_MAX_FAILURE_RECEIPTS`` failed/unknown device receipts on the recovery
#   surface.  Beyond that the provisioner is still attempted on every start, so
#   a later healthy start recovers, but a failure is logged only and writes
#   nothing; a success begins and marks the ``ready`` receipt after the fact.
#   The store cannot mark a receipt that was never begun, so this order (attempt,
#   then begin+ready) is the one safe form of "attempt without a receipt".  The
#   cost is that a crash inside an over-budget attempt leaves no pending receipt;
#   the public device record, not the receipt, decides ``deviceReady``, and the
#   provisioner adopts or rewrites that record idempotently, so nothing is
#   misreported.  The journal caps are untouched and no record is deleted.
# - Truthfulness.  A failed attempt writes ``failed``/``unknown`` on the device
#   receipt it opened and nothing else, so ``onboarding.status`` keeps
#   reporting ``initialized: false`` / ``deviceReady: false`` and the existing
#   ``nextActions``.  The initialize receipt is marked completed only after the
#   public device record has been read back for the same owner.  While the
#   provisioning is running ``nextActions`` still lists ``device.ensure``, which
#   a serving caller is refused; this is transient and known (KP-094).
# - Retry.  One attempt per serving start.  It is not retried inside the same
#   process: a provisioning failure (missing sidecar, unreachable network) is
#   not transient on the scale of a loop, retrying would hammer the sidecar and
#   append a device receipt per attempt, and the operator's remedy is the same
#   as for any serving fault, a daemon restart.  Each attempt uses a fresh
#   operation key so a terminal ``failed``/``unknown`` device receipt from an
#   earlier start never blocks the next one.
# - Account identities only.  Recovery acts only when the snapshot is a
#   login-committed account identity (mode ``casdoor``, the same set the serving
#   reauthentication accepts); a legacy or local owner is not an account the
#   login flow committed, so nothing is provisioned or fabricated for it.
# - Owner binding.  Only receipts whose ``account_owner`` equals the owner read
#   from the public identity snapshot are touched.  ``auth.logout`` never removes
#   ``device.json``, so after an account switch the record may belong to the
#   previous owner.  Bootstrap ``device.ensure`` does not look at that record's
#   owner either: it calls the provisioner for the current owner, which adopts
#   the old device id and rewrites the record.  Recovery does exactly that
#   through the same ``TailcatDeviceProvisioner`` (F1), instead of skipping.  The
#   owner is read again after provisioning, so a logout or account change during
#   the wait aborts before the initialize receipt moves.  New receipts live
#   on a dedicated per-owner recovery surface, so one owner's receipts can never
#   collide with another owner's on the same home.
# - Identity used.  The only identity is the owner the committed local identity
#   already proves (it was committed by the login the user completed); the
#   daemon invents none.  The catalog declares ``begin_device`` and
#   ``accept_initialize`` for the bootstrap/local-bootstrap pair, and this
#   recovery finishes the very bootstrap that an earlier daemon generation
#   started, so it builds that pair internally, never from request input, which
#   keeps the request catalog and its OpenAPI untouched.  Transitions on
#   receipts that already exist use the surface stored on the receipt itself.
# - Why serving initialize uses a no-op promotion and completes at once.  The
#   bootstrap promotion quiesces the bootstrap wire host and hands the home to a
#   serving generation.  Here the daemon is already the serving generation, so
#   there is nothing to quiesce or hand over; the promotion callback is a no-op
#   and the receipt is moved straight from ``promoting`` to ``completed``,
#   which is what the initialize receipt attests: owner committed plus device
#   registered, both verified immediately before.  An ``accepted`` receipt is
#   skipped, because the store allows no ``accepted`` to ``completed`` move and
#   journal reconciliation has already turned any ``accepted`` or ``promoting``
#   receipt of a dead writer into ``unknown`` before this runs.  A live writer's
#   ``accepted`` cannot exist on a fresh serving start: opening the journal
#   refuses while an earlier writer is alive, and this process has written none.
# - Failed initialize (F2).  ``BootstrapRuntime.run_daemon`` marks the receipt
#   ``failed`` when the application factory raises, with owner and device both
#   committed.  A fresh initialize with a new operation key is started whenever
#   the owner has no completed and no resumable (``promoting``/``unknown``)
#   receipt and no ``accepted`` one, that is, none or only terminal ``failed``,
#   ``cancelled`` ones (the store has no ``expired`` initialize state).  The old
#   failed receipt is never modified.
# Decision record (Z-5, owner decision 2026-11-18: first login is one daemon operation):
# - A start that promoted a bootstrap runs this pass too.  A first login the
#   daemon drove may reach serving with the device not registered (the login
#   succeeds, the device is marked not ready) and the initialize receipt left
#   ``promoting``; this pass is how that device is registered and that receipt
#   completed, under the same one-attempt-per-serving-start rule as above.  When
#   the device was ready the pass is a no-op (``already-complete``).
# - The user key follows the device.  When this pass registers the device it also
#   ensures the owner's local user key (no network, no keychain, no terminal),
#   through ``ensure_login_user_key``.  It takes the identity transaction lock
#   for a bounded wait (the daemon itself still holds it until it is ready, a
#   concurrent login holds it for longer) and skips on busy: a skipped key is
#   made by the next login or recovery, never by a loop here.  The owner is read
#   again under the lock, so a change of account aborts the step.  The state
#   directory is the parent of the journal root the pass already owns.
# Decision record (service grant device encryption key, 2026-11-26: the device encryption key):
# - This pass is where serving start ensures the device's encryption key, the
#   one other member devices seal a service credential to.  It runs on every
#   serving start and on every relogin, ahead of the early ``already-complete``
#   return: a device registered before the key existed has nothing else to
#   recover and would otherwise never get one.  The publish path alone is not
#   enough, because it needs an org to publish into and a sidecar address.
# - Unlike the signing key the step runs for a device that is already
#   registered, not only for one this pass provisions: the signing key is made
#   by login, the encryption key has no earlier caller at all.
# - The key is a ``user`` kind key, bound to the committed account and this
#   device record; ``device_key.encryption_key_reader`` holds what counts as
#   a complete account.  A relogin reads them again, so the key belongs to the
#   account that is committed now and never to the one recovery started with.
# - The step runs under the home identity transaction lock.  The key store's
#   own file lock does not cover the identity files: without the transaction
#   lock a concurrent login could swap ``settings.json`` and ``device.json``
#   between the two reads.  The step reads the committed account and the device
#   uid in that one locked read and never re-reads ``device.json``.
# - The lock is not re-entrant inside one process, and at serving start this
#   process already holds it: ``daemon run`` takes it before reading identity
#   and releases it only just before the accept loop, while the ready hook has
#   already started this thread.  Taking it again there could only time out and
#   skip the key, which then existed only after a cache-miss publish.  So when
#   ``is_held_in_process`` is true the step runs under the lock already held;
#   otherwise it takes the lock with the same bounded wait as the signing-key
#   step and skips on busy.  The owner is read again under the lock, so a
#   change of account aborts the step.  It never changes the outcome of the
#   pass: a device that cannot have a key, a busy lock, or a step that fails
#   is still a recovered device.  A skipped key is made by the next serving
#   start, relogin or cache-miss publish, never by a loop.
"""Complete unfinished device registration and initialize when serving starts."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from contextlib import nullcontext
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.identity import (
    IdentityTransactionBusy,
    IdentityTransactionLock,
    read_device_record,
    read_identity_snapshot,
)

from hyprial.daemon.impl.onboarding.operations import journal
from hyprial.daemon.impl.onboarding.operations.service import OperationService
from hyprial.daemon.impl.org.network.device_key import ensure_encryption_key
from hyprial.daemon.impl.request_catalog.onboarding.specs import DEVICE_ENSURE_FAILED
from hyprial.daemon.impl.request_catalog.onboarding.state import (
    CallContext,
    OperationRecord,
    Surface,
)
from hyprial.daemon.impl.user_key.login_key import ensure_login_user_key
from hyprial.kernel import ipc_errors

_RETRY_SERVING_START = "serving-start"

_LOG = logging.getLogger("hyprial.daemon.onboarding.recovery")
_CLOSE_JOIN_SECONDS = 2.0
_KEY_LOCK_SECONDS = 5.0
_ACCOUNT_MODE = "casdoor"
_RESUMABLE = frozenset({"promoting", "unknown"})
_TERMINAL_FAILURES = frozenset({"failed", "cancelled"})
_MAX_FAILURE_RECEIPTS = 8

__all__ = ["ServingRecovery"]


def _default_provisioner() -> Any:
    from ..provisioning import TailcatDeviceProvisioner

    return TailcatDeviceProvisioner()


class ServingRecovery:
    """One fault-isolated, off-thread recovery attempt per serving start."""

    def __init__(
        self,
        *,
        operations: OperationService,
        home: Path,
        identity_reader: Callable[[Path], Any] = read_identity_snapshot,
        device_reader: Callable[[Path], Any] = read_device_record,
        journal_reader: Callable[[Path], Any] = journal.read_journal,
        provisioner: Any | None = None,
        user_key: Callable[[Path, Path, str, Any], Any] = ensure_login_user_key,
        encryption_key: Callable[[Path, Path], Any] | None = ensure_encryption_key,
    ) -> None:
        self._operations = operations
        self._home = Path(home)
        self._user_key = user_key
        self._encryption_key = encryption_key
        self._identity_reader = identity_reader
        self._device_reader = device_reader
        self._journal_reader = journal_reader
        self._provisioner = provisioner
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False

    def start(self) -> None:
        """Begin the single attempt of this start; never raises, never waits."""

        with self._lock:
            if self._thread is not None or self._closed:
                return
            thread = threading.Thread(
                target=self.run,
                name="onboarding-serving-recovery",
                daemon=True,
            )
            self._thread = thread
        try:
            thread.start()
        except Exception:
            _LOG.warning("serving recovery thread did not start", exc_info=True)

    def join(self, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds; True when no attempt is running."""

        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self.join(_CLOSE_JOIN_SECONDS)

    def run(self) -> str:
        """Run one attempt synchronously and return its outcome label."""

        with self._run_lock:
            try:
                return self._recover()
            except Exception:
                _LOG.warning(
                    "serving onboarding recovery failed; state left untouched",
                    exc_info=True,
                )
                return "failed"

    def recover_relogin(self, expected_owner: str) -> dict[str, Any]:
        """Complete device, user key and initialize for a relogin without revoking identity."""

        from ..first_login.completion import authored_error

        with self._run_lock:
            owner = self._owner()
            if owner is None or owner != expected_owner:
                return _relogin_device_block(
                    ready=False,
                    error=authored_error(ipc_errors.IDENTITY_UNBOUND),
                )
            device = self._device_reader(self._home)
            if device is not None and getattr(device, "owner", None) != owner:
                _LOG.info("device record belongs to another owner; re-provisioning")
                device = None
            state = self._journal_reader(self._operations.root)
            records = () if state is None else tuple(state.snapshot.records)
            mine = [
                record
                for record in records
                if record.kind == "initialize" and record.account_owner == owner
            ]
            if device is None:
                device, error_code = self._provision_with_error(owner, records)
                if device is None:
                    return _relogin_device_block(
                        ready=False,
                        error=authored_error(error_code or DEVICE_ENSURE_FAILED),
                    )
                if self._owner() != owner:
                    _LOG.info("owner changed during device provisioning; stopping")
                    return _relogin_device_block(
                        ready=False,
                        error=authored_error(ipc_errors.IDENTITY_UNBOUND),
                    )
            self._ensure_encryption_key(owner, device)
            key_state = self._ensure_user_key(owner, device)
            resumed = [record for record in mine if record.state in _RESUMABLE]
            for record in resumed:
                self._complete(record, owner)
            if not resumed and not any(record.state == "completed" for record in mine):
                self._initialize_fresh(owner)
            return _relogin_device_block(ready=True, record=device, user_key=key_state)

    def _owner(self) -> str | None:
        identity = self._identity_reader(self._home)
        owner = identity.owner
        if identity.mode != _ACCOUNT_MODE or not isinstance(owner, str) or not owner:
            return None
        return owner

    def _context(self, surface: Surface, owner: str) -> CallContext:
        return CallContext(
            surface=surface,
            phase="bootstrap",
            caller="local-bootstrap",
            owner=owner,
        )

    def _recovery_surface(self, owner: str) -> Surface:
        return Surface(
            home_binding=str(self._home),
            host_control_session_id="daemon-serving-recovery",
            view_session_id="serving-recovery-"
            + sha256(owner.encode("utf-8")).hexdigest()[:16],
            transport_generation="serving-recovery",
        )

    def _recover(self) -> str:
        owner = self._owner()
        if owner is None:
            return "no-owner"
        device = self._device_reader(self._home)
        if device is not None and getattr(device, "owner", None) != owner:
            _LOG.info("device record belongs to another owner; re-provisioning")
            device = None
        self._ensure_encryption_key(owner, device)
        state = self._journal_reader(self._operations.root)
        records = () if state is None else tuple(state.snapshot.records)
        mine = [
            record
            for record in records
            if record.kind == "initialize" and record.account_owner == owner
        ]
        if device is not None and any(r.state == "completed" for r in mine):
            return "already-complete"
        if device is None:
            device = self._provision(owner, records)
            if device is None:
                return "device-failed"
            if self._owner() != owner:
                _LOG.info("owner changed during device provisioning; stopping")
                return "owner-changed"
            self._ensure_encryption_key(owner, device)
            self._ensure_user_key(owner, device)
        resumed = [record for record in mine if record.state in _RESUMABLE]
        for record in resumed:
            self._complete(record, owner)
        if all(record.state in _TERMINAL_FAILURES for record in mine):
            self._initialize_fresh(owner)
            return "initialized"
        return "resumed" if resumed else "nothing-to-resume"

    def _ensure_encryption_key(self, owner: str, device: Any) -> None:
        """Ensure this device's encryption key; never raises and never blocks the pass.

        The step runs under the home's identity transaction lock, so a
        concurrent login or relogin cannot swap ``settings.json`` and
        ``device.json`` mid-read.  When this process already holds the lock,
        as ``daemon run`` does until it is ready to serve, the step runs under
        that lock at once: the lock is not re-entrant inside one process, so
        taking it again could only time out.  Otherwise it is taken with the
        same bounded wait as the signing-key step and skipped on busy.
        """

        if self._encryption_key is None or device is None:
            return
        try:
            held_here = IdentityTransactionLock.is_held_in_process(self._home)
            transaction = (
                nullcontext()
                if held_here
                else IdentityTransactionLock.acquire(self._home, timeout=_KEY_LOCK_SECONDS)
            )
        except IdentityTransactionBusy:
            _LOG.info("encryption key skipped: the identity transaction is busy")
            return
        except Exception as error:
            _LOG.warning("encryption key was not ensured: %s", type(error).__name__)
            return
        with transaction:
            try:
                if self._owner() != owner:
                    _LOG.info("owner changed before the encryption key; skipping")
                    return
                self._encryption_key(self._home, Path(self._operations.root).parent)
            except Exception as error:
                _LOG.warning("encryption key was not ensured: %s", type(error).__name__)

    def _ensure_user_key(self, owner: str, device: Any) -> dict[str, Any] | None:
        try:
            transaction = IdentityTransactionLock.acquire(
                self._home, timeout=_KEY_LOCK_SECONDS
            )
        except IdentityTransactionBusy:
            _LOG.info("user key skipped: the identity transaction is busy")
            return None
        except Exception as error:
            _LOG.warning("user key was not ensured: %s", type(error).__name__)
            return None
        try:
            if self._owner() != owner:
                _LOG.info("owner changed before the user key; skipping")
                return None
            state_dir = Path(self._operations.root).parent
            result = self._user_key(self._home, state_dir, owner, device)
            _LOG.info("user key state: %s", result.get("state"))
            if isinstance(result, dict) and isinstance(result.get("state"), str):
                return {"state": result["state"], "code": result.get("code")}
            return None
        except Exception as error:
            _LOG.warning("user key was not ensured: %s", type(error).__name__)
            return None
        finally:
            transaction.close()

    def _complete(self, record: OperationRecord, owner: str) -> None:
        try:
            self._operations.mark_initialize(
                self._context(record.surface, owner),
                record.operation_id,
                "completed",
            )
        except Exception:
            _LOG.warning(
                "initialize receipt %s was not completed", record.operation_id,
                exc_info=True,
            )

    def _initialize_fresh(self, owner: str) -> None:
        context = self._context(self._recovery_surface(owner), owner)
        record = self._operations.accept_initialize(
            context,
            "serving-recovery-initialize",
            uuid4().hex,
            uuid4().hex,
            promotion=lambda _record: None,
        )
        if record.state in _RESUMABLE:
            self._operations.mark_initialize(context, record.operation_id, "completed")
        else:
            _LOG.warning("fresh initialize receipt stayed %s", record.state)

    def _provision(self, owner: str, records: tuple[OperationRecord, ...]) -> Any | None:
        device, _code = self._provision_with_error(owner, records)
        return device

    def _provision_with_error(
        self, owner: str, records: tuple[OperationRecord, ...]
    ) -> tuple[Any | None, str | None]:
        surface = self._recovery_surface(owner)
        context = self._context(surface, owner)
        failures = sum(
            1
            for record in records
            if record.kind == "device"
            and record.account_owner == owner
            and record.surface.view_session_id == surface.view_session_id
            and record.surface.transport_generation == surface.transport_generation
            and record.state in ("failed", "unknown")
        )
        pending = None
        if failures < _MAX_FAILURE_RECEIPTS:
            pending = self._begin_device(context)
        provisioner = self._provisioner or _default_provisioner()
        try:
            device = provisioner.ensure(home=self._home, owner=owner)
            if getattr(device, "owner", None) != owner:
                raise ValueError("provisioned device belongs to another owner")
        except Exception as error:
            terminal = "unknown" if getattr(error, "outcome_unknown", False) else "failed"
            _LOG.warning("device provisioning did not complete: %s", error)
            if pending is not None:
                self._mark_device(context, pending.operation_id, terminal)
            code = getattr(error, "code", None)
            if not isinstance(code, str) or not code:
                code = (
                    ipc_errors.SUBMIT_OUTCOME_UNKNOWN
                    if terminal == "unknown"
                    else DEVICE_ENSURE_FAILED
                )
            return None, code
        if pending is None:
            pending = self._begin_device(context)
        self._operations.mark_device(context, pending.operation_id, "ready")
        return device, None

    def _begin_device(self, context: CallContext) -> OperationRecord:
        return self._operations.begin_device(
            context,
            "serving-recovery-device-" + uuid4().hex,
            uuid4().hex,
            uuid4().hex,
        )

    def _mark_device(self, context: CallContext, operation_id: str, state: str) -> None:
        try:
            self._operations.mark_device(context, operation_id, state)
        except Exception:
            _LOG.warning("device receipt %s was not marked %s", operation_id, state,
                         exc_info=True)


def _relogin_device_block(
    *,
    ready: bool,
    record: Any = None,
    user_key: dict[str, Any] | None = None,
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    if ready:
        code = None if user_key is None else user_key.get("code")
        return {
            "ready": True,
            "deviceId": record.device_id,
            "keyGeneration": record.key_generation,
            "userKey": (
                {"state": "unavailable", "code": ipc_errors.USER_KEY_ABSENT}
                if user_key is None
                else {"state": str(user_key.get("state")), "code": code if isinstance(code, str) else None}
            ),
            "error": None,
            "retry": None,
        }
    from ..first_login.completion import authored_error

    return {
        "ready": False,
        "deviceId": None,
        "keyGeneration": None,
        "userKey": {"state": "unavailable", "code": ipc_errors.USER_KEY_ABSENT},
        "error": dict(error) if error is not None else authored_error(DEVICE_ENSURE_FAILED),
        "retry": _RETRY_SERVING_START,
    }
