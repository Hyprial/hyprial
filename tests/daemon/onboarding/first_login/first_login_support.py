"""Controlled ports and clients for the daemon-driven first login tests.

Decision: daemon-first-z5-first-login-tests-20261118.  Every home is the
isolated ownerless home of ``wire_support``; the identity worker, device
provisioner and user-key step are controlled in-process ports, so no test
contacts an issuer, a sidecar, a key store or an ambient home.  Poll counters
record the NAME of the thread that polled, which is how the tests prove that the
driver is the only poller of a login it drives.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from hyprial.identity import (
    DeviceRecord,
    IdentityPublicState,
    write_device_record,
)
from hyprial.kernel import DaemonRequestError, RequestPortError, daemon_request_once
from tests.daemon.onboarding.wire.wire_support import (
    OWNER,
    ControlledIdentity,
    ServedWire,
    gateway_options,
)

DRIVER_THREAD = "hyprial-first-login"
WIRE_THREAD = "hyprial-bootstrap-wire-client"
PROJECTION_THREADS = (DRIVER_THREAD, WIRE_THREAD)


class PendingIdentity(ControlledIdentity):
    """Stays pending until ``release``; then commits like ``ControlledIdentity``."""

    def __init__(self) -> None:
        self.polls: list[str] = []
        self.cancels = 0
        self.released = threading.Event()

    def release(self) -> None:
        self.released.set()

    def poll_public(self, operation_id):
        self.polls.append(threading.current_thread().name)
        if not self.released.is_set():
            return IdentityPublicState(operation_id, "pending")
        return super().poll_public(operation_id)

    def cancel(self, operation_id):
        self.cancels += 1
        return IdentityPublicState(operation_id, "cancelled")


class BrokenIdentity(PendingIdentity):
    """The worker cannot be polled at all."""

    def poll_public(self, operation_id):
        self.polls.append(threading.current_thread().name)
        raise RuntimeError("controlled worker failure")


class CommitOnCancelIdentity(PendingIdentity):
    """The worker had already committed when the cancel reached it."""

    def cancel(self, operation_id):
        self.cancels += 1
        return ControlledIdentity.poll_public(self, operation_id)


class ExpiredIdentity(PendingIdentity):
    def poll_public(self, operation_id):
        self.polls.append(threading.current_thread().name)
        return IdentityPublicState(
            operation_id, "failed", error_code="DEVICE_CODE_EXPIRED"
        )


class RecordingDevice:
    """Writes a real public device record, like the sidecar capability does."""

    def __init__(self) -> None:
        self.calls = 0

    def ensure(self, *, home, owner):
        self.calls += 1
        device = DeviceRecord(
            "controlled-device",
            owner,
            "nodekey:test-server",
            "nodekey:test-client",
            key_generation=2,
        )
        write_device_record(home, device)
        return device


class UserKeyRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path, str, object]] = []

    def __call__(self, home, state_dir, owner, device):
        self.calls.append((Path(home), Path(state_dir), owner, device))
        return {"state": "unbound", "code": "USER_KEY_UNBOUND"}


def driven_options(provisioner=None, user_key=None):
    options = gateway_options()
    options["provisioner"] = provisioner or RecordingDevice()
    options["user_key"] = user_key or UserKeyRecorder()
    return options


def ask(running: ServedWire, method, params=None, timeout=10.0):
    return daemon_request_once(
        method, params, socket_path=running.host.socket_path, timeout=timeout
    )


def begin_serving(running: ServedWire, key: str = "opkey-serving-0001"):
    return ask(
        running,
        "auth.login.begin",
        {"intent": "login", "operationKey": key, "until": "serving"},
    )


class Parked:
    """One request made on its own thread, so a test can act while it is parked."""

    def __init__(self, running: ServedWire, method, params, timeout=30.0) -> None:
        self.started = time.monotonic()
        self.finished: float | None = None
        self.result = None
        self.error: BaseException | None = None
        self._thread = threading.Thread(
            target=self._run,
            args=(running.host.socket_path, method, params, timeout),
            name="first-login-test-client",
            daemon=True,
        )
        self._thread.start()

    def _run(self, path, method, params, timeout) -> None:
        try:
            self.result = daemon_request_once(
                method, params, socket_path=path, timeout=timeout
            )
        except (DaemonRequestError, RequestPortError, OSError) as error:
            self.error = error
        finally:
            self.finished = time.monotonic()

    @property
    def parked(self) -> bool:
        return self._thread.is_alive()

    def wait(self, timeout: float = 15.0):
        self._thread.join(timeout)
        assert not self._thread.is_alive(), "the parked request never answered"
        if self.error is not None:
            raise self.error
        return self.result

    @property
    def seconds(self) -> float:
        assert self.finished is not None
        return self.finished - self.started


def wait_for(predicate, timeout: float = 10.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def leftovers() -> dict[str, list[str] | int]:
    """Threads of this feature still alive and the open descriptor count."""

    names = [
        thread.name
        for thread in threading.enumerate()
        if thread.name in PROJECTION_THREADS and thread.is_alive()
    ]
    return {"threads": names, "fds": len(os.listdir("/proc/self/fd"))}


NO_PROVISIONER = SimpleNamespace(ensure=lambda **_: None)
OWNER_ID = OWNER


def wait_parked(running: ServedWire, count: int = 1, timeout: float = 10.0) -> None:
    """Block until ``count`` waits are parked in the host (a bounded poll of its parked set)."""

    assert wait_for(lambda: len(running.host._parked) >= count, timeout), (
        "the waits never parked"
    )
