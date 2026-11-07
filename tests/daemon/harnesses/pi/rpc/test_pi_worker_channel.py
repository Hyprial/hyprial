"""Pi headless workers receive their own WorkerChannel through the RPC carrier.

Claude workers get the harness-bridge as an MCP server; pi has no MCP support
by design, so the pi carrier is the bundled ``pi_harness_bridge.ts`` extension
plus the identity environment the launcher injects.  These tests fence:

1. ``PiRpcClient`` adds ``--extension pi_harness_bridge.ts`` and the
   HYPRIAL_WORKER_* / daemon-pinning env only when a worker channel is present;
   the no-channel path stays byte-identical to the pre-fix behaviour.
2. ``PiRpcProcess`` keeps one stable worker identity across client reconnects
   (same contract as ``ClaudeAgentSdkProcess``).
3. ``HarnessLauncher`` feeds the daemon-minted channel to the pi factory --
   the mutation guard: deleting that wiring turns these tests red.
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

import hyprial.daemon.impl.harnesses.runtime.launcher as launcher_module
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.runtime.launcher import HarnessLauncher
from hyprial.daemon.impl.harnesses.pi.rpc_client import PI_HARNESS_BRIDGE_EXTENSION, PiRpcClient
from hyprial.daemon.impl.harnesses.pi.rpc_process import PiRpcProcess
from hyprial.daemon.impl.harnesses.worker_channel import WorkerChannel, build_worker_channel

CANONICAL = "agent:allen:node-1:pi-worker"

# The streaming pump connects its client eagerly on construction, so every
# constructed PiRpcProcess MUST use a fake command and be stopped -- a default
# command would launch the real `pi` binary from a unit test and the leaked
# pump state hangs the interpreter at exit (see test_pi_rpc_process.py, the
# convention this file follows).
FAKE_PI = Path(__file__).resolve().parents[5] / 'tests/fixtures/harnesses/fake_pi_rpc.py'
FAKE_PI_COMMAND = (sys.executable, str(FAKE_PI))


def _channel() -> WorkerChannel:
    return build_worker_channel(
        actor=CANONICAL,
        session_ref="sess-pi-1",
        hyprial_home=Path("/h/home"),
        state_dir=Path("/h/state"),
    )


def _spec(**kwargs: object) -> HarnessLaunchSpec:
    return HarnessLaunchSpec(harness="pi", name="pi-worker", headless=True, **kwargs)  # type: ignore[arg-type]


def _arg(args: tuple[str, ...], flag: str) -> str:
    return args[args.index(flag) + 1]


# --- The pi carrier: extension flag + identity env --------------------------


def test_pi_environment_pins_worker_identity_and_this_daemon() -> None:
    env = _channel().pi_environment()

    assert env == {
        "HYPRIAL_WORKER_ACTOR": CANONICAL,
        "HYPRIAL_WORKER_SESSION_REF": "sess-pi-1",
        "HYPRIAL_MANAGED_WORKER": "1",
        "HYPRIAL_HOME": "/h/home",
        "HARNESS_STATE_DIR": "/h/state",
    }


def test_extension_file_is_a_shipped_asset() -> None:
    assert PI_HARNESS_BRIDGE_EXTENSION.name == "pi_harness_bridge.ts"
    assert PI_HARNESS_BRIDGE_EXTENSION.is_file()


def test_client_injects_extension_and_identity_env() -> None:
    channel = _channel()
    client = PiRpcClient(
        _spec(args=("--model", "k3")), "stable-session", worker_channel=channel
    )

    assert _arg(client.command, "--extension") == str(PI_HARNESS_BRIDGE_EXTENSION)
    assert _arg(client.command, "--session-id") == "stable-session"
    # Passthrough args survive; the extension flag is appended, not mangled.
    assert _arg(client.command, "--model") == "k3"
    assert client._env is not None
    assert client._env["HYPRIAL_WORKER_ACTOR"] == CANONICAL
    assert client._env["HYPRIAL_WORKER_SESSION_REF"] == "sess-pi-1"
    # The extension reaches THIS daemon, not an ambient production one.
    assert client._env["HYPRIAL_HOME"] == "/h/home"
    assert client._env["HARNESS_STATE_DIR"] == "/h/state"


def test_client_env_merges_caller_env_under_channel_env() -> None:
    client = PiRpcClient(
        _spec(),
        "stable-session",
        env={"EXTRA": "1", "HYPRIAL_HOME": "/ambient/home"},
        worker_channel=_channel(),
    )

    assert client._env is not None
    assert client._env["EXTRA"] == "1"
    # The channel's daemon pinning always wins over an inherited override.
    assert client._env["HYPRIAL_HOME"] == "/h/home"


def test_no_worker_channel_leaves_command_and_env_untouched() -> None:
    """The pre-fix path is byte-identical: no channel means no injection."""

    client = PiRpcClient(_spec(args=("--model", "k3")), "stable-session")

    assert client.command == (
        "pi",
        "--model",
        "k3",
        "--mode",
        "rpc",
        "--session-id",
        "stable-session",
    )
    assert client._env is None

    with_env = PiRpcClient(_spec(), "stable-session", env={"EXTRA": "1"})
    assert with_env._env == {"EXTRA": "1"}
    assert "--extension" not in with_env.command


# --- pi --session-id charset boundary ----------------------------------------
#
# Pi >=0.76 rejects a --session-id containing any character outside
# [A-Za-z0-9._-] at launch (colons included).  The session ref is OUR
# identity key -- daemon state, fencing and desired-state key on the raw
# value -- so the boundary translation happens only when the ref becomes a pi
# argument; the internal ref must never be rewritten.

_ALLOWED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"


def test_client_sanitizes_colon_session_ref_for_pi_only() -> None:
    client = PiRpcClient(_spec(), "squire:alw:usrb")

    session_id = _arg(client.command, "--session-id")
    assert session_id == "squire.alw.usrb"
    assert all(char in _ALLOWED for char in session_id)
    # The internal identity key stays byte-for-byte raw.
    assert client.session_ref == "squire:alw:usrb"


def test_client_passes_valid_session_ref_through_unchanged() -> None:
    ref = uuid.uuid4().hex
    client = PiRpcClient(_spec(), ref)

    assert _arg(client.command, "--session-id") == ref
    assert client.session_ref == ref


# --- Process-level identity stability ----------------------------------------


def test_process_keeps_one_stable_worker_identity() -> None:
    channel = _channel()
    process = PiRpcProcess(_spec(), command=FAKE_PI_COMMAND, worker_channel=channel)
    try:
        assert process.worker_channel is channel
        # The default client factory rebuilds clients on reconnect; each
        # rebuilt client must re-inject the same canonical identity and
        # extension.
        client = process._client_factory()
        assert isinstance(client, PiRpcClient)
        assert _arg(client.command, "--extension") == str(PI_HARNESS_BRIDGE_EXTENSION)
        assert client._env is not None
        assert client._env["HYPRIAL_WORKER_ACTOR"] == CANONICAL
    finally:
        process.stop()


def test_process_without_channel_builds_plain_client() -> None:
    process = PiRpcProcess(_spec(), command=FAKE_PI_COMMAND)
    try:
        assert process.worker_channel is None
        client = process._client_factory()
        assert isinstance(client, PiRpcClient)
        assert "--extension" not in client.command
        assert client._env is None
    finally:
        process.stop()


# --- Launcher wiring (mutation guard) ----------------------------------------
#
# The launcher's default pi factory has no command seam, so a real PiRpcProcess
# here would launch the real `pi` binary.  Substituting a recorder for the
# PiRpcProcess *name the launcher's default factory resolves* keeps the guard
# honest: the wiring under test -- ``worker_channel=self._worker_channel_for``
# inside launcher.py -- still executes, and deleting it turns these red.


class RecordingPiRpcProcess:
    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        env: object = None,
        worker_channel: WorkerChannel | None = None,
        complete_launch: object = None,
        command: tuple[str, ...] = ("pi",),
        on_turn_failure_for_spec: object = None,
        on_turn_completed: object = None,
        runtime_launch_custody: object = None,
    ) -> None:
        self.spec = spec
        self.on_turn_completed = on_turn_completed
        self.env = env
        self.worker_channel = worker_channel
        self.complete_launch = complete_launch
        self.command = command
        self.on_turn_failure_for_spec = on_turn_failure_for_spec
        self.runtime_launch_custody = runtime_launch_custody
        self.last_error: str | None = None

    def wait_ready(self, *, timeout: float) -> bool:
        assert timeout > 0
        return True

    def stop(self) -> None:
        self.last_error = None


def test_launcher_feeds_the_worker_channel_to_the_pi_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher_module, "PiRpcProcess", RecordingPiRpcProcess)
    minted: list[str] = []

    def factory(spec: HarnessLaunchSpec) -> WorkerChannel:
        minted.append(spec.name)
        return _channel()

    process = HarnessLauncher(worker_channel_factory=factory).start(_spec())

    assert isinstance(process, RecordingPiRpcProcess)
    assert process.worker_channel is not None
    assert process.worker_channel.actor == CANONICAL
    assert minted == ["pi-worker"]


def test_launcher_without_factory_leaves_pi_worker_unmanaged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(launcher_module, "PiRpcProcess", RecordingPiRpcProcess)

    process = HarnessLauncher().start(_spec())

    assert isinstance(process, RecordingPiRpcProcess)
    assert process.worker_channel is None
