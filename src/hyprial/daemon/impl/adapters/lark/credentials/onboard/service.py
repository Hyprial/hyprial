"""Own one non-replayable SDK creation and its local registration receipt.

Remote creation and local registration are distinct effects. A created App
whose credential cannot be saved remains unknown and needs explicit inspection.
Cancellation stops local waiting; it does not delete anything on the platform.
"""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
import threading
import time
from uuid import uuid4

from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.daemon.impl.bootstrap.adapter_registration import ensure_lark_gateway_addable
from ..authorization.process import AuthorizationEffect
from ..authorization.service import _prompt
from .journal import ACTIVE, OnboardingJournal, OnboardingReceiptError


class LarkOnboardingService:
    def __init__(self, home: Path, state_dir: Path, *, register, effects=None, timeout=900.0):
        self._home, self._state_dir = Path(home), Path(state_dir)
        self._register_adapter_configuration = register
        self._journal = OnboardingJournal(self._state_dir / "adapter-onboard" / "receipts.json")
        self._operations = self._journal.load()
        self._lock = threading.RLock()
        self._effects = effects or ThreadPoolExecutor(max_workers=4, thread_name_prefix="lark-onboard")
        self._owns_effects = effects is None
        self._handles = {}
        self._reserved = set()
        self._closed = False
        self._timeout = timeout

    def _save(self):
        try:
            self._journal.save(self._operations)
        except OnboardingReceiptError:
            for row in self._operations.values():
                if row["status"] in ACTIVE:
                    row.update(status="unknown", remoteOutcomeUnknown=True, errorCode="APP_RECEIPT_UNCONFIRMED", nextAction="inspect")
                    row.pop("verificationUrl", None)
                    row.pop("expiresAt", None)
            raise

    def start(self, name, operation_key):
        with self._lock:
            if self._closed or self._journal.failed:
                raise OnboardingReceiptError("onboarding service unavailable")
            if not isinstance(operation_key, str) or not 1 <= len(operation_key) <= 128:
                raise ValueError("invalid operation key")
            for row in self._operations.values():
                if row["operationKey"] == operation_key:
                    if row["adapter"] != name:
                        raise ValueError("operation key belongs to another adapter")
                    return deepcopy(row)
            for row in self._operations.values():
                if row["adapter"] == name and row["status"] in ACTIVE | {"unknown"}:
                    return deepcopy(row)
            ensure_lark_gateway_addable(hyprial_home=self._home, state_dir=self._state_dir, name=name)
            if len(self._operations) >= 128 or len(self._reserved) >= 4:
                raise RuntimeError("onboarding capacity reached")
            identity = uuid4().hex
            row = {"operationId": identity, "operationKey": operation_key, "adapter": name,
                   "provider": "lark", "kind": "add", "status": "starting", "configured": False,
                   "reloaded": False, "remoteOutcomeUnknown": False, "nextAction": "wait"}
            self._operations[identity] = row
            self._save()
            self._reserved.add(identity)
            try:
                self._effects.submit(self._monitor, identity)
            except Exception:
                self._reserved.discard(identity)
                row.update(status="failed", errorCode="APP_CREATION_UNAVAILABLE", nextAction="none")
                self._save()
            return deepcopy(row)

    def operation(self, identity):
        with self._lock:
            return deepcopy(self._operations[identity])

    def current_operation(self, name):
        with self._lock:
            return next((deepcopy(row) for row in reversed(self._operations.values()) if row["adapter"] == name), None)

    def _cleanup(self, identity):
        with self._lock:
            handle = self._handles.get(identity)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    self._operations[identity].update(status="unknown", remoteOutcomeUnknown=True,
                                                     errorCode="APP_WORKER_CLEANUP_UNCONFIRMED", nextAction="inspect")
                    self._operations[identity].pop("verificationUrl", None)
                    self._operations[identity].pop("expiresAt", None)
                    self._save()
                    return False
                self._handles.pop(identity, None)
            self._reserved.discard(identity)
            return True

    def _finish(self, identity, status, *, error=None):
        row = self._operations[identity]
        row.update(status=status, remoteOutcomeUnknown=status == "unknown", nextAction="inspect" if status == "unknown" else "none")
        if error:
            row["errorCode"] = error
        row.pop("verificationUrl", None)
        row.pop("expiresAt", None)
        try:
            self._save()
        except OnboardingReceiptError:
            row.update(status="unknown", remoteOutcomeUnknown=True, errorCode="APP_RECEIPT_UNCONFIRMED", nextAction="inspect")
            raise

    def _commit(self, identity, credential):
        row = self._operations[identity]
        if (not isinstance(credential, dict) or not all(isinstance(credential.get(k), str) and credential[k] for k in ("appId", "appSecret"))):
            self._finish(identity, "unknown", error="APP_CREDENTIAL_UNCONFIRMED")
            return
        row.update(status="checking", appId=credential["appId"], remoteOutcomeUnknown=True)
        self._save()
        try:
            result = self._register_adapter_configuration(row["adapter"], credential["appId"], credential["appSecret"])
        except Exception as error:
            rejected = isinstance(error, DaemonRequestError) and error.code in {"ADAPTER_EXISTS", "ADAPTER_CONFIG_CONFLICT", ipc_errors.INVALID_ARGUMENT}
            self._finish(identity, "unknown", error="APP_CREATED_NOT_SAVED" if rejected else "APP_CONFIGURATION_UNCONFIRMED")
            return
        row.update(configured=True, reloaded=result["reloaded"])
        if not self._cleanup(identity):
            return
        self._finish(identity, "succeeded")
        row["nextAction"] = result["nextAction"]
        self._save()

    def _monitor(self, identity):
        try:
            with self._lock:
                row = self._operations[identity]
                if self._closed or row["status"] not in ACTIVE:
                    return
                handle = AuthorizationEffect({"action": "onboard", "adapter": row["adapter"],
                                               "home": str(self._home), "stateDir": str(self._state_dir)})
                self._handles[identity] = handle
            deadline = time.monotonic() + self._timeout
            prompt_seen = False
            while time.monotonic() < deadline:
                with self._lock:
                    if self._closed or row["status"] not in ACTIVE:
                        return
                    value = handle.snapshot()
                    if value:
                        state = value.get("status")
                        if state == "completed":
                            self._commit(identity, value.get("credential"))
                            return
                        if state in {"denied", "expired", "unknown", "failed"}:
                            self._finish(identity, state, error="APP_CREATION_" + state.upper())
                            return
                        if state == "awaiting_user" and not prompt_seen:
                            prompt = _prompt(value)
                            if prompt is None:
                                self._finish(identity, "unknown", error="INVALID_VERIFICATION_PROMPT")
                                return
                            url, ttl = prompt
                            deadline = min(deadline, time.monotonic() + ttl)
                            row.update(status="awaiting_user", verificationUrl=url, expiresAt=time.time() + ttl, remoteOutcomeUnknown=True)
                            prompt_seen = True
                time.sleep(.02)
            with self._lock:
                if row["status"] in ACTIVE:
                    self._finish(identity, "unknown", error="APP_CREATION_TIMEOUT")
        except Exception:
            with self._lock:
                row = self._operations[identity]
                if row["status"] in ACTIVE:
                    try:
                        self._finish(identity, "unknown", error="APP_CREATION_UNCONFIRMED")
                    except OnboardingReceiptError:
                        pass
        finally:
            try:
                self._cleanup(identity)
            except OnboardingReceiptError:
                pass

    def cancel(self, identity):
        with self._lock:
            row = self._operations[identity]
            if row["status"] not in ACTIVE | {"unknown"}:
                return deepcopy(row)
            row.update(status="unknown", remoteOutcomeUnknown=True)
            if self._cleanup(identity):
                row.update(status="cancelled", nextAction="inspect")
                row.pop("verificationUrl", None)
                row.pop("expiresAt", None)
                try:
                    self._save()
                except OnboardingReceiptError:
                    row.update(status="unknown", errorCode="APP_RECEIPT_UNCONFIRMED")
                    raise
            return deepcopy(row)

    def close(self):
        with self._lock:
            self._closed = True
            for row in self._operations.values():
                if row["status"] in ACTIVE:
                    row.update(status="unknown", remoteOutcomeUnknown=True, nextAction="inspect")
                    row.pop("verificationUrl", None)
                    row.pop("expiresAt", None)
            try:
                self._save()
            except OnboardingReceiptError:
                pass
            for identity in list(self._reserved):
                try:
                    self._cleanup(identity)
                except OnboardingReceiptError:
                    pass
        if self._owns_effects:
            self._effects.shutdown(wait=True, cancel_futures=True)
        if self._handles:
            raise RuntimeError("onboarding worker cleanup unconfirmed")
