'Two profiles, two homes, two isolated daemons on one machine (login U1, T11).\n\nDesign §9.8 / §10-U1 验收②: one machine may hold several profiles, each its\nown ``HYPRIAL_HOME`` with its own daemon state — selection is ``HYPRIAL_HOME`` and\nnothing else. The daemons here are started exactly the way\n``tests/daemon/home/test_home_guard.py`` starts its isolated daemons (bare ``HYPRIAL_HOME`` +\n``HARNESS_STATE_DIR`` + explicit owner, zenoh listen/connect emptied), plus\nthe U1 isolation trio the spec pins: ``HYPRIAL_PEER_DISCOVERY=0`` and a\nper-daemon ``UV_TOOL_DIR`` alongside ``HYPRIAL_HOME``.\n\nThe mutation judgement: if the two homes ever share one\n``HARNESS_STATE_DIR`` (or any other state root), the sockets collide, both\n``ps`` IPC reads hit one daemon, and this file goes red.\n'

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.configuration.network_profile import NetworkProfile, resolve_profile, write_profile

_OWNER = "u1-two-homes-owner"
_ISSUER_A = "https://auth.alpha.example"
_ISSUER_B = "https://auth.beta.example"


def _short_temp_root() -> str:
    """A writable short root for AF_UNIX sockets (Darwin's 104-byte sun_path
    limit rules out pytest's tmp_path; same shape as test_home_guard.py)."""

    configured = os.environ.get("HYPRIAL_TEST_TMP_ROOT")
    if configured:
        # CI points this at the /ci-tmpfs RAM disk: daemons started on the
        # shared btrfs stall on its commits during startup (infra-ops,
        # 2026-09-28).  Locally it is unset and the lookup below is unchanged.
        return os.path.realpath(configured)
    for candidate in ("/private/tmp", "/tmp"):
        if os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    return tempfile.gettempdir()


def _wait_for(predicate: Any, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


def _daemon_environment(
    home: Path, state: Path, uv_tools: Path, node: str
) -> dict[str, str]:
    from tests.support.runtime.environment import isolated_environment

    environment = isolated_environment(home.parent)
    for name in tuple(environment):
        if name.startswith("HYPRIAL_") or name.startswith("HARNESS_"):
            environment.pop(name)
    environment.update(
        {
            "HYPRIAL_HOME": str(home),
            "HARNESS_STATE_DIR": str(state),
            "HYPRIAL_NODE_ID": node,
            # Same reason as test_home_guard.py: this helper strips every
            # ambient HYPRIAL_* variable, so the identity the daemon needs is
            # supplied explicitly instead of leaking in from the runner.
            "HYPRIAL_OWNER": _OWNER,
            "HYPRIAL_ZENOH_LISTEN": "",
            "HYPRIAL_ZENOH_CONNECT": "",
            # The U1 isolation trio's remaining two: no peer discovery (no
            # tailnet reachability, no well-known-port probing) and a
            # per-daemon uv tool dir, so neither daemon can see the other's
            # (or the machine's real) tool installations.
            "HYPRIAL_PEER_DISCOVERY": "0",
            # And the switch that closes the rest (forwarding, zenoh's default
            # listener, gossip, usage fetch): each daemon listens on loopback
            # only and reports itself isolated.
            "HYPRIAL_NETWORK_ISOLATED": "1",
            "UV_TOOL_DIR": str(uv_tools),
            "HYPRIAL_DAEMON_KEEPALIVE_DURATION": "0.1",
        }
    )
    return environment


def _start_daemon(environment: dict[str, str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "hyprial.cli", "daemon", "run", "--json"],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _stop_daemon(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        os.kill(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.kill(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def _ps(environment: dict[str, str]) -> dict[str, Any]:
    """Read the daemon ``ps`` payload over the public IPC seam.

    R7 deleted the top-level ``ps`` CLI command; this test needs the top-level
    ``zenoh`` block, which ``daemon status`` does not carry, so it talks IPC.
    """

    from hyprial.kernel import daemon_request

    state = Path(environment["HARNESS_STATE_DIR"])
    # Socket creation precedes restore completion. The public client waits
    # only on the explicit pre-dispatch refusal; other IPC failures still fail.
    result = daemon_request(
        "ps", {}, socket_path=state / "daemon.sock", timeout=30, restore_wait=30
    )
    assert isinstance(result, dict), result
    return result


@pytest.mark.parametrize("code", ["DAEMON_RESTORING", "ACCESS_DENIED"])
def test_profile_ps_waits_only_for_restore(monkeypatch, tmp_path, code) -> None:
    """A listening socket can still refuse requests during daemon restore."""
    from hyprial.kernel import RequestPortError

    replies = iter([
        {"error": {"code": code, "message": "request refused"}},
        {"result": {"daemon": {"nodeId": "profile-ready"}}},
    ])
    calls = []

    class Socket:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def settimeout(self, _timeout):
            pass

        def connect(self, path):
            assert path == str(tmp_path / "daemon.sock")

        def sendall(self, frame):
            request = json.loads(frame)
            assert request["method"] == "ps"
            calls.append(request)
            self.response = {"version": 1, "id": request["id"], **next(replies)}

        def recv(self, _size):
            return json.dumps(self.response).encode() + b"\n"

        def close(self):
            pass

    monkeypatch.setattr(socket, "socket", lambda *_args: Socket())
    if code == "ACCESS_DENIED":
        with pytest.raises(RequestPortError, match="request refused"):
            _ps({"HARNESS_STATE_DIR": str(tmp_path)})
        assert len(calls) == 1
        return
    assert _ps({"HARNESS_STATE_DIR": str(tmp_path)}) == {
        "daemon": {"nodeId": "profile-ready"}
    }
    assert len(calls) == 2


def test_t11_two_homes_two_isolated_daemons() -> None:
    with tempfile.TemporaryDirectory(
        prefix="hyprial-u1two-", dir=_short_temp_root()
    ) as raw:
        root = Path(raw)
        home_a, home_b = root / "home-a", root / "home-b"
        state_a, state_b = root / "state-a", root / "state-b"
        uv_a, uv_b = root / "uv-a", root / "uv-b"
        for directory in (home_a, home_b, state_a, state_b, uv_a, uv_b):
            directory.mkdir(mode=0o700)
        write_profile(
            NetworkProfile(
                org="alpha",
                issuer=_ISSUER_A,
                client_id="alpha-client-id",
                invite_base_url="https://invite.alpha.example",
            ),
            hyprial_home=home_a,
        )
        write_profile(
            NetworkProfile(
                org="beta",
                issuer=_ISSUER_B,
                client_id="beta-client-id",
                invite_base_url="https://invite.beta.example",
            ),
            hyprial_home=home_b,
        )

        # Each home resolves to its own profile — the record is a property of
        # the home, before any daemon exists.
        profile_a, source_a = resolve_profile(hyprial_home=home_a)
        profile_b, source_b = resolve_profile(hyprial_home=home_b)
        assert (profile_a.issuer, source_a) == (_ISSUER_A, "profile.json")
        assert (profile_b.issuer, source_b) == (_ISSUER_B, "profile.json")
        assert profile_a.invite_base_url == "https://invite.alpha.example"
        assert profile_b.invite_base_url == "https://invite.beta.example"

        env_a = _daemon_environment(home_a, state_a, uv_a, "u1-profile-a")
        env_b = _daemon_environment(home_b, state_b, uv_b, "u1-profile-b")
        daemon_a = _start_daemon(env_a)
        daemon_b = _start_daemon(env_b)
        try:
            marker_a, marker_b = home_a / ".active_daemon", home_b / ".active_daemon"
            socket_a, socket_b = state_a / "daemon.sock", state_b / "daemon.sock"
            _wait_for(marker_a.exists)
            _wait_for(marker_b.exists)
            _wait_for(socket_a.exists)
            _wait_for(socket_b.exists)

            # One home = one daemon = one marker, each naming its own PID.
            assert json.loads(marker_a.read_text(encoding="utf-8"))["pid"] == daemon_a.pid
            assert json.loads(marker_b.read_text(encoding="utf-8"))["pid"] == daemon_b.pid

            # Nothing shared: sockets, state roots and markers are pairwise
            # distinct paths — sharing any of them is the red mutation.
            assert socket_a != socket_b
            assert state_a != state_b
            assert marker_a != marker_b

            ps_a, ps_b = _ps(env_a), _ps(env_b)
            for payload, daemon, socket, node in (
                (ps_a, daemon_a, socket_a, "u1-profile-a"),
                (ps_b, daemon_b, socket_b, "u1-profile-b"),
            ):
                daemon_view = payload["daemon"]
                assert daemon_view["nodeId"] == node
                assert daemon_view["pid"] == daemon.pid
                assert daemon_view["socket"] == str(socket)
            # Each ps sees *only itself*: cross-check both directions.
            assert ps_a["daemon"]["pid"] != ps_b["daemon"]["pid"]
            assert ps_a["daemon"]["socket"] != ps_b["daemon"]["socket"]
            assert ps_a["daemon"]["nodeId"] != ps_b["daemon"]["nodeId"]
            # zenoh is offline in isolation: a loopback-only listener (explicit,
            # so zenoh never adds its all-interface default), no connect, and
            # the daemon itself reports the isolation as effective.
            for status in (ps_a, ps_b):
                assert status["zenoh"]["listen"] == ["tcp/127.0.0.1:0"]
                assert status["zenoh"]["connect"] == []
                assert status["zenoh"]["isolated"] is True, status["zenoh"]["isolation"]

            # Still each other's profile after the daemons ran: a daemon in
            # home A neither reads nor rewrites home B's record.
            assert resolve_profile(hyprial_home=home_a)[0].issuer == _ISSUER_A
            assert resolve_profile(hyprial_home=home_b)[0].issuer == _ISSUER_B
        finally:
            _stop_daemon(daemon_b)
            _stop_daemon(daemon_a)
            _wait_for(lambda: not (home_a / ".active_daemon").exists())
            _wait_for(lambda: not (home_b / ".active_daemon").exists())
