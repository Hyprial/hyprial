"""The real ``hyprial daemon run --json`` CLI path with a controlled identity port.

Decision: daemon-first-z2-controlled-entry-20261116.  Real OIDC is unavailable
in a test environment, so this entry replaces exactly three ports before it
runs the unmodified CLI: the identity worker (a login that commits an owner
into ``settings.json`` when polled), the issuer profile resolver and the device
provisioner, for both the bootstrap and the serving gateway.  Everything else
(the CLI command, the lifecycle loop, the wire host, the native engine and the
serving ``DaemonApplication``) is the shipped code.

Test seams are environment-selected and default to doing nothing:

* ``Z2_RECORD`` names a file that receives one JSON line per observation (the
  keyword arguments the CLI passed to the lifecycle loop, every wire-host open).
* ``Z2_SIGTERM_BEFORE_OPEN=N`` sends this process SIGTERM right before the Nth
  wire-host open, which is the window between a logout stop and the next
  bootstrap wire.
* ``Z2_LINGER_THREAD=1`` leaves a non-daemon thread parked forever, the shape of
  the dependency threads that keep a finished interpreter alive.
* ``Z2_FAIL_AFTER_ACCEPT=encode|registry`` makes the wire host fail after it
  durably accepted an initialize, once.
* ``Z6_OWNERS=a,b`` makes the Nth login of this process commit the Nth owner
  (the last one repeats); unset, every login commits ``OWNER``.
* ``Z3_SERVING_DEVICE=ok|fail`` selects the provisioner the serving-start
  recovery uses (default ``ok``, so no test process ever reaches a real
  Tailcat sidecar); ``fail`` raises a definite provisioning failure.
* ``Z5_BOOTSTRAP_DEVICE=ok|fail`` selects the provisioner the BOOTSTRAP gateway
  uses for a daemon-driven first login (default ``ok``); ``fail`` raises a
  definite provisioning failure, so the login ends serving with the device not
  ready.
* ``Z5_BOOTSTRAP_HOLD=1`` makes the controlled identity worker never commit, so
  a test can cancel a login that is still waiting for authorisation.
* every failing device attempt records ``device-ensure-failed``, so a test can
  count the attempts (the bootstrap one plus one per serving start).
* every serving-start recovery pass records ``recovery-finished`` (with its
  outcome label) when it ends, which is the positive signal tests wait on.
"""

from __future__ import annotations

import json
import os
import runpy
import queue
import signal
import sys
import threading

ROOT = os.environ["Z2_REPO_ROOT"]
sys.path.insert(0, ROOT)

from hyprial.daemon.impl.application.onboarding.bootstrap import wire  # noqa: E402
from hyprial.daemon.impl.application.onboarding.lifecycle import recovery  # noqa: E402
from hyprial.daemon.impl.application.onboarding.provisioning import (  # noqa: E402
    DeviceProvisioningError,
)
from hyprial.identity import (  # noqa: E402
    AuthProfile,
    DeviceRecord,
    IdentityBegin,
    IdentityPublicState,
    write_device_record,
)
from hyprial.shell.impl.daemon_wiring.onboarding import launch  # noqa: E402

OWNER = "z2-owner@example.test"
ISSUER = "https://issuer.example.test"
RECORD = os.environ.get("Z2_RECORD")


def record(**fields: object) -> None:
    if RECORD:
        with open(RECORD, "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"pid": os.getpid(), **fields}) + "\n")


_LOGINS = [0]


def _next_owner() -> str:
    owners = [item for item in os.environ.get("Z6_OWNERS", "").split(",") if item]
    index = _LOGINS[0]
    _LOGINS[0] += 1
    return owners[min(index, len(owners) - 1)] if owners else OWNER


class CommittingIdentity:
    def begin(self, *, profile, home, permit, lock_fd=None, transport=None):
        del profile, lock_fd, transport
        self.home = home
        self.owner = _next_owner()
        return IdentityBegin(permit.operation_id, ISSUER + "/device", "Z2-CODE", 600, 1)

    def poll_public(self, operation_id):
        if os.environ.get("Z5_BOOTSTRAP_HOLD") == "1":
            return IdentityPublicState(operation_id, "pending")
        (self.home / "settings.json").write_text(
            json.dumps({"owner": self.owner, "identityMode": "casdoor", "identityIssuer": ISSUER})
        )
        return IdentityPublicState(
            operation_id, "committed", self.owner, ISSUER,
            {"credentialCommitted": True, "settingsCommitted": True},
        )

    def cancel(self, operation_id):
        return IdentityPublicState(operation_id, "cancelled")

    def close(self):
        pass


class ControlledDevice:
    def ensure(self, *, home, owner):
        device = DeviceRecord("z2-node-device", owner, "nodekey:test-server", "nodekey:test-client")
        write_device_record(home, device)
        return device


class FailingDevice:
    def ensure(self, *, home, owner):
        record(event="device-ensure-failed")
        raise DeviceProvisioningError("controlled provisioner failure")


recovery._default_provisioner = (
    FailingDevice if os.environ.get("Z3_SERVING_DEVICE") == "fail" else ControlledDevice
)

_run_recovery = recovery.ServingRecovery.run


def _recorded_recovery_run(self):
    outcome = _run_recovery(self)
    record(event="recovery-finished", outcome=outcome)
    return outcome


recovery.ServingRecovery.run = _recorded_recovery_run

_opens = [0]
_open = wire.BootstrapWireHost.open.__func__


def _controlled_open(cls, home, **options):
    _opens[0] += 1
    if os.environ.get("Z2_SIGTERM_BEFORE_OPEN") == str(_opens[0]):
        os.kill(os.getpid(), signal.SIGTERM)
    options.setdefault("gateway_options", {
        "profile_resolver": lambda _: AuthProfile(ISSUER, "test-client"),
        "provisioner": (
            FailingDevice()
            if os.environ.get("Z5_BOOTSTRAP_DEVICE") == "fail"
            else ControlledDevice()
        ),
    })
    host = _open(cls, home, **options)
    host.dispatcher.runtime._identity_factory = CommittingIdentity
    failure = os.environ.get("Z2_FAIL_AFTER_ACCEPT")
    if failure == "encode":
        original = host._encode

        def encode(document):
            if document.get("result", {}).get("state") == "promoting":
                raise ValueError("injected encode failure")
            return original(document)

        host._encode = encode
    elif failure == "registry":
        original_call = host.dispatcher.call

        def call(method, params=None):
            result = original_call(method, params)
            if method == "onboarding.initialize":
                raise RuntimeError("injected registry failure")
            return result

        host.dispatcher.call = call
    record(event="open", count=_opens[0])
    return host


wire.BootstrapWireHost.open = classmethod(_controlled_open)

_lifecycle = launch.launch_daemon_lifecycle


def _recording_lifecycle(**options):
    record(
        event="lifecycle",
        owns_process_exit=options.get("owns_process_exit"),
        exit_unless_logout=options.get("exit_unless_logout"),
        has_bind_epoch=options.get("bind_epoch") is not None,
        has_launch_id=options.get("launch_id") is not None,
    )
    return _lifecycle(**options)


launch.launch_daemon_lifecycle = _recording_lifecycle

if os.environ.get("Z2_LINGER_THREAD"):
    threading.Thread(target=queue.Queue().get, name="z2-linger").start()

sys.argv = ["hyprial", "daemon", "run", "--json"]
runpy.run_module("hyprial.cli", run_name="__main__")
