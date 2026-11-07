'Layer-1 client-side contract: the Channel owner fence vs marker environments.\n\nSibling of tests/daemon/sessions/test_session_carrier_protocol_contract.py: same layer (no\nreal TUI, no PTY, no terminal), but pinned on the *client* side of the carrier\n(``hyprial.mcp.channel``) instead of the daemon-side verdicts.\n\nThe defect this file pinned (measured on Darwin, E2E-008/E2E-010 family): the\nmanaged-launch owner fence compared a process-birth marker captured by the\nlauncher (``hyprial agent start`` / the probe) against markers re-read by the stdio\nchild. On macOS the marker came from ``ps -o lstart=``, whose output is\n**local-time text** -- it changes with the reader\'s TZ/LC environment:\n\n    $ ps -o lstart= -p $$                      -> Tue Aug 18 11:56:43 2026\n    $ TZ=UTC ps -o lstart= -p $$               -> Tue Aug 18 03:56:43 2026\n    $ TZ=America/New_York ps -o lstart= -p $$  -> Mon Aug 17 23:56:43 2026\n\nThe launcher and the Claude-Code-spawned child do not share one environment\n(GUI-launched apps carry no shell TZ; Claude Code sanitizes what it passes to\nMCP children), so the same live owner read as two different "identities".\nThe fence then classified IDENTITY_MISMATCH, and after the grace window it\nunregistered and force-exited a child whose owner was still alive -- the\n"Channel client exited before current-generation confirmation" /\n"CC TUI exited before registration" symptom.\n\nThe fix this file now guards:\n\n* markers are **multi-component** (``scheme:value`` joined by ``;``) and every\n  component source is environment-invariant (native ``proc_pidinfo`` start\n  time on Darwin, procfs ticks on Linux, and a TZ/LC-pinned ps fallback), so\n  the marker is a function of the process birth alone;\n* comparison is **component-wise**: shared precise Darwin births must agree; otherwise compatible\n  shared components preserve legacy matching, and no shared component -- including "could not read" -- is UNKNOWN (fail-safe),\n  never mismatch evidence. "Unreadable" and "different" travel different\n  paths: the first must not reap a live owner, the second must.\n\nThe session-level test drives the exact production wiring\n(``run_channel_session`` over in-memory MCP streams, a fake daemon on a real\nUnix socket -- the harness from tests/daemon/channels/mcp/lifecycle/test_mcp_channel_restart.py) with a\ngenuinely alive owner; the unit-level tests pin marker stability across\nTZ/LC environments and the unreadable-vs-mismatch verdict split.\n'

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import os
import shutil
import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from hyprial.daemon.impl.mcp import (
    StatelessDaemonProxy,
    UnixDaemonConnectionFactory,
    run_channel_session,
)
from hyprial.daemon.impl.mcp.channel import ownership as channel_module
from hyprial.daemon.impl.mcp.channel import loops as channel_loops
from hyprial.daemon.impl.mcp.channel.ownership import (
    _identity_components,
    _owner_process_status,
    _OwnerProcessStatus,
    _read_process_identity,
)


class FakeDaemon:
    'One daemon generation on a real Unix socket (same shape as\n    tests/daemon/channels/mcp/lifecycle/test_mcp_channel_restart.py, trimmed to what this contract needs).'

    def __init__(self, socket_path: Path, *, pending: list[dict[str, str]]) -> None:
        self.socket_path = Path(socket_path)
        self.pending = [dict(item) for item in pending]
        self.methods: list[str] = []
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.socket_path.unlink(missing_ok=True)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.socket_path))
        self._sock.listen(64)
        self._sock.settimeout(0.05)
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
        self.socket_path.unlink(missing_ok=True)

    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (TimeoutError, OSError):
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(2.0)
            buffer = bytearray()
            while b"\n" not in buffer:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer.extend(chunk)
            request = json.loads(buffer.partition(b"\n")[0])
            result = self._dispatch(str(request.get("method")), request.get("params") or {})
            with self._lock:
                self.methods.append(str(request.get("method")))
            conn.sendall(
                json.dumps({"version": 1, "id": request.get("id"), "result": result}).encode()
                + b"\n"
            )
        except OSError:
            pass
        finally:
            conn.close()

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "session.register":
            return {"ok": True, "registered": True, "actor": params.get("actor")}
        if method == "session.refresh":
            return {"ok": True, "refreshed": True, "actor": params.get("actor")}
        if method == "session.heartbeat":
            return {"ok": True, "alive": True, "actor": params.get("actor")}
        if method == "session.unregister":
            return {"ok": True, "unregistered": True, "actor": params.get("actor")}
        if method == "message.pending.list":
            return {"ok": True, "messages": [dict(item) for item in self.pending]}
        if method == "message.pending.batch":
            items = [dict(item) for item in self.pending]
            return {
                "ok": True,
                "schemaVersion": 1,
                "format": "inbox-batch-v1",
                "cursor": "gen1-d1",
                "daemonEpoch": "epoch-1",
                "actor": params.get("actor"),
                "itemCount": len(items),
                "totalBodyBytes": 0,
                "inlineLimitBytes": 16384,
                "items": items,
                "delivery": {
                    "mode": "inline",
                    "text": "\n".join(str(item["messageId"]) for item in items),
                    "spillPath": None,
                    "spillBytes": None,
                    "spillSha256": None,
                },
            }
        return {"ok": True}


@pytest.fixture
def sock_path() -> Iterator[Path]:
    # AF_UNIX paths are length-capped (~104 bytes on macOS); pytest's tmp_path
    # is too long, so bind under a short private temp dir instead.
    directory = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprialown"))
    try:
        yield directory / "d.sock"
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def _read_identity_with_tz(
    reader: Callable[[int], str | None], pid: int, tz: str
) -> str | None:
    """Read the real production marker with the subprocess environment a
    differently-launched process would carry (``ps`` inherits TZ at exec)."""

    previous = os.environ.get("TZ")
    os.environ["TZ"] = tz
    try:
        return reader(pid)
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous


async def _until(
    predicate: Any, *, timeout: float = 5.0, interval: float = 0.02
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await anyio.sleep(interval)
    raise AssertionError("condition was not met within the timeout")


@pytest.mark.anyio
async def test_live_owner_is_not_reaped_when_identity_reads_cross_environments(
    sock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live owner must survive an identity-marker environment skew.

    RED today: the ps-lstart marker is local-time text, so a child reading it
    under a different TZ than the launcher sees a persistent IDENTITY_MISMATCH
    and the fence unregisters + force-exits the child while its owner is alive.
    """

    # Exercise the portable ps fallback on every platform (Linux included), so
    # this contract pins the environment-sensitive marker path that Darwin
    # production launches actually take.
    monkeypatch.setattr(channel_module, "_PROC_ROOT", Path("/nonexistent-no-procfs"))
    # Shrink the production 10s mismatch grace so the verdict arrives in-test.
    monkeypatch.setattr(
        channel_loops, "_OWNER_IDENTITY_MISMATCH_GRACE_SECONDS", 0.3
    )

    owner_pid = os.getpid()  # a genuinely alive owner for the whole test
    real_reader = channel_module._read_process_identity

    # The launcher captures the marker under ITS environment ...
    owner_identity = _read_identity_with_tz(real_reader, owner_pid, "UTC")
    assert owner_identity is not None, "launcher could not read its own marker"

    # ... and the spawned child re-reads it under a DIFFERENT environment.
    def child_reader(pid: int) -> str | None:
        return _read_identity_with_tz(real_reader, pid, "Asia/Shanghai")

    # No precondition on the skew producing a different string: when the marker
    # is environment-invariant (the fix), the fence simply never misfires and
    # the assertions below pass on their own -- this test is the acceptance
    # anchor, RED while the marker is environment-sensitive, GREEN once fixed.
    monkeypatch.setattr(channel_loops, "_read_process_identity", child_reader)

    daemon = FakeDaemon(
        sock_path, pending=[{"messageId": "m1", "deliveryId": "gen1-d1"}]
    )
    daemon.start()
    proxy = StatelessDaemonProxy(UnixDaemonConnectionFactory(sock_path))
    parent_death = anyio.Event()
    try:
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            client_read, client_write = client_streams
            server_read, server_write = server_streams
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    lambda: run_channel_session(
                        server_read,
                        server_write,
                        proxy,
                        actor="coordinator",
                        session_ref="session-1",
                        cwd="/work/coordinator",
                        command=("claude",),
                        poll_interval=0.02,
                        on_error=lambda _error, _n: None,
                        owner_pid=owner_pid,
                        owner_identity=owner_identity,
                        parent_poll_interval=0.02,
                        # Record the reaper instead of os._exit()ing the test run.
                        on_parent_death=parent_death.set,
                        # Compat path unused (owner fence is supplied), but never
                        # let a container's PID 1 drift tear the session down.
                        getppid=lambda: 4242,
                    )
                )
                async with ClientSession(client_read, client_write) as client:
                    await client.initialize()
                    await _until(lambda: "session.register" in daemon.methods)

                    # Several mismatch-grace windows with a provably live owner.
                    await anyio.sleep(1.0)

                    assert not parent_death.is_set(), (
                        "owner fence reaped a child whose owner is still alive: "
                        "the identity marker changed across process environments"
                    )
                    assert "session.unregister" not in daemon.methods, (
                        "reaper unregistered a live-owner session; the daemon now "
                        "routes a dead route and the next start sees a phantom "
                        "'already has a live connector'"
                    )

                    # The held-open session must still serve daemon truth (the
                    # generation confirmation the E2E waits for).
                    response = await client.call_tool("harness_read", {})
                    assert response.is_error is not True
                    items = response.structured_content.get("items") or []
                    assert items and items[0]["messageId"] == "m1"

                tasks.cancel_scope.cancel()
    finally:
        daemon.stop()


# --------------------------------------------------------------------------- #
# Marker environment-stability regressions (unit level, no session wiring)
# --------------------------------------------------------------------------- #
def _force_ps_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every platform exercise the portable ps path (procfs hidden)."""

    monkeypatch.setattr(channel_module, "_PROC_ROOT", Path("/nonexistent-no-procfs"))


def test_identity_marker_is_stable_across_timezones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same live PID must read identically under any reader TZ."""

    _force_ps_fallback(monkeypatch)
    pid = os.getpid()
    markers = {
        tz: _read_identity_with_tz(_read_process_identity, pid, tz)
        for tz in ("UTC", "Asia/Shanghai", "America/New_York")
    }
    assert all(marker is not None for marker in markers.values())
    assert len(set(markers.values())) == 1, (
        f"process-birth marker changed with the reader's timezone: {markers}"
    )


def test_identity_marker_is_stable_across_locales(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same live PID must read identically under any reader LC_*."""

    _force_ps_fallback(monkeypatch)
    pid = os.getpid()
    markers: list[str | None] = []
    for lc_all in ("C", "en_US.UTF-8", "zh_CN.UTF-8"):
        previous = os.environ.get("LC_ALL")
        os.environ["LC_ALL"] = lc_all
        try:
            markers.append(_read_process_identity(pid))
        finally:
            if previous is None:
                os.environ.pop("LC_ALL", None)
            else:
                os.environ["LC_ALL"] = previous
    assert all(marker is not None for marker in markers)
    assert len(set(markers)) == 1, (
        f"process-birth marker changed with the reader's locale: {markers}"
    )


# --------------------------------------------------------------------------- #
# Cross-format comparison and the unreadable-vs-mismatch verdict split
# --------------------------------------------------------------------------- #
def _status(expected: str, observed: str | None) -> _OwnerProcessStatus:
    return _owner_process_status(
        1234,
        expected,
        signal_process=lambda _pid, _signal: None,
        read_identity=lambda _pid: observed,
    )


def test_legacy_single_scheme_marker_matches_multi_component_marker() -> None:
    """An old-format expected marker matches a new multi-component read."""

    observed = "darwin-starttime:1755490000:123;ps-lstart:Tue Aug 18 03:56:43 2026"
    assert (
        _status("ps-lstart:Tue Aug 18 03:56:43 2026", observed)
        is _OwnerProcessStatus.ALIVE
    )
    assert (
        _status("darwin-starttime:1755490000:123", observed)
        is _OwnerProcessStatus.ALIVE
    )


def test_shared_component_with_all_values_differing_is_mismatch() -> None:
    """Comparable evidence that disagrees IS the PID-reuse signal."""

    assert (
        _status(
            "ps-lstart:Tue Aug 18 03:56:43 2026",
            "darwin-starttime:1755490000:123;ps-lstart:Wed Aug 19 03:56:43 2026",
        )
        is _OwnerProcessStatus.IDENTITY_MISMATCH
    )
    assert (
        _status("original-start", "reused-start")
        is _OwnerProcessStatus.IDENTITY_MISMATCH
    )


def test_no_shared_component_is_unknown_never_mismatch() -> None:
    """'Cannot compare' must not become 'does not match'.

    The two failure directions this splits: reading nothing (or nothing
    comparable) must retain a possibly-live owner (UNKNOWN, fail-safe), while
    only positive disagreement may advance toward reaping.
    """

    # Unreadable observation: UNKNOWN, and _owner_process_alive retains.
    assert _status("ps-lstart:Tue Aug 18 03:56:43 2026", None) is (
        _OwnerProcessStatus.UNKNOWN
    )
    # Readable but incomparable (disjoint schemes): still UNKNOWN.
    assert (
        _status("ps-lstart:Tue Aug 18 03:56:43 2026", "proc-starttime:987654")
        is _OwnerProcessStatus.UNKNOWN
    )
    # And neither may log/return as a mismatch verdict.
    assert _OwnerProcessStatus.UNKNOWN is not _OwnerProcessStatus.IDENTITY_MISMATCH


def test_ps_unavailable_still_matches_on_native_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A sandboxed child (ps spawn denied) matches via the native component.

    procfs is hidden too, so ps is the ONLY fallback source on Linux -- the
    point is the verdict split when one source dies, not procfs itself.
    """

    _force_ps_fallback(monkeypatch)
    pid = os.getpid()
    full_marker = _read_process_identity(pid)
    assert full_marker is not None

    def denied_popen(*args: object, **kwargs: object) -> object:
        raise OSError("sandbox denies ps")

    monkeypatch.setattr(channel_module, "_PROCESS_POPEN", denied_popen)
    degraded = _read_process_identity(pid)
    if channel_module.sys.platform == "darwin":
        # Native proc_pidinfo survives the ps denial; the shared component
        # still proves the same process.
        assert degraded is not None
        assert (
            _owner_process_status(
                pid,
                full_marker,
                read_identity=lambda _pid: degraded,
            )
            is _OwnerProcessStatus.ALIVE
        )
    else:
        # Without procfs and without ps there is nothing to compare: UNKNOWN
        # (fail-safe retain), never a mismatch.
        assert degraded is None
        assert (
            _owner_process_status(
                pid,
                full_marker,
                read_identity=lambda _pid: degraded,
            )
            is _OwnerProcessStatus.UNKNOWN
        )


def test_identity_components_parses_legacy_and_opaque_markers() -> None:
    assert _identity_components("proc-starttime:987654") == {
        "proc-starttime": "987654"
    }
    assert _identity_components(
        "darwin-starttime:1:2;ps-lstart:Tue Aug 18 03:56:43 2026"
    ) == {
        "darwin-starttime": "1:2",
        "ps-lstart": "Tue Aug 18 03:56:43 2026",
    }
    # Unknown grammar opaques the whole marker instead of inventing a scheme.
    assert _identity_components("original-start") == {"raw": "original-start"}
