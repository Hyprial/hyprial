"""Suite-wide file-descriptor hygiene and leak pinning (card #77).

Why this file exists
--------------------
The suite's daemon-shaped objects (``DaemonApplication``, ``InboxService``,
``AgentRegistry``) own real file descriptors: WAL-mode SQLite databases hold
three fds each (main/-wal/-shm).  Three mechanisms used to keep those fds
open long after the owning test finished, pushing the pytest process toward
macOS's ``FD_SETSIZE`` cliff (the condition that made #76 detonate in CI):

1. Bound-method reference cycles defeat refcounting.  ``InboxService`` hands
   ``self._deliver_system_notice`` / ``self._claim_alarm`` to its
   ``AlarmEmitter``; ``DaemonApplication`` hands bound methods and a
   ``lambda: self._clock()`` closure to ``AgentLiveness``.  The objects are
   therefore collectable only by the *cyclic* GC, and pytest never forces a
   collection between tests — garbage (and its fds) accumulates.
2. ``set_default_registry()`` is a deliberate module-global pin: every
   ``DaemonApplication`` construction installs its registry as the process
   default, and nothing ever cleared it between tests.
3. One-time imports (notably ``lark_oapi.ws.client``, which creates an
   asyncio self-pipe socketpair at import time) open fds once per process.
   Those are not leaks, so they are warmed up below *before* measurement
   starts.

The autouse fixture below clears the global pin and collects young cycles
after every test. If new descriptors remain, it forces a full collection too,
then fails the test if any descriptor it opened is still held. A test that
legitimately keeps an fd must say so explicitly; anything else is a leak and goes red here, in milliseconds, instead of at fd 1024.

Decision (0.5.5.3, runs 33456/33461 shard 2, KP-007): a candidate that closes
between the post-gc snapshot and the report is a transient descriptor
finishing its own teardown, not an accumulator — the same class _fd_set
already drops at snapshot time. Only descriptors still open at report time
are counted as leaks; the report names any additionally vanished candidates.
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import asyncio
import gc
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from hyprial.identity.impl.agents import set_default_registry

# Warm every known one-time fd owner before any per-test measurement, so a
# first import mid-suite is not mistaken for a leak.  Importing the daemon
# package pulls in lark_oapi.ws.client, whose module-level
# ``asyncio.new_event_loop()`` owns a permanent socketpair.
import hyprial.daemon.impl.application  # noqa: F401  (import side effect: one-time fds)


@pytest.fixture(autouse=True)
def _isolated_hyprial_home(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep every unit test away from the operator's ambient hyprial state.

    Product commands may only create a home through ``hyprial init``.  Unit tests
    that do not exercise init receive an explicitly-created fixture home;
    tests may still override HYPRIAL_HOME to verify missing-home behavior.

    ⭐ The fixture also supplies a **user identity**, because
    ``resolve_node_owner`` no longer falls back to the host login: without one
    every code path that mints an agent URI fails.  Both spellings are set on
    purpose, and they cover different populations:

    - ``settings.json`` ``owner`` — the shape a real home has after the
      identity migration, so the fixture home looks like a live one.
    - ``HYPRIAL_OWNER`` — also covers tests that build their **own** home (e.g.
      the ``adapter remove`` CLI tests seed one) and tests that shell out to
      ``python -m hyprial.cli``, where a settings file in the *fixture* home is
      not the file the subprocess reads.

    ⚠️ Neither alone is enough; the second was found by measurement, not by
    reasoning — the settings-only version still left the self-seeding CLI
    tests failing on a subprocess exit code.

    ⛔ Tests that assert the *absence* of an identity must build an explicit
    empty home and pass it in, never rely on this fixture not setting one.
    """

    with tempfile.TemporaryDirectory(prefix="hyprial-unit-", dir=hyprial_test_tmp_root()) as raw:
        from tests.support.runtime.environment import isolated_environment

        root = Path(raw)
        environment = isolated_environment(root)
        for key, value in environment.items():
            if key == "HOME" or key.startswith("XDG_"):
                monkeypatch.setenv(key, value)
        for key in tuple(os.environ):
            if key.startswith("DBUS_"):
                monkeypatch.delenv(key)
        home = root / "home"
        state = root / "state"
        home.mkdir(mode=0o700)
        state.mkdir(mode=0o700)
        (home / "settings.json").write_text(
            json.dumps({"version": 1, "owner": "hyprial-unit-owner"}),
            encoding="utf-8",
        )
        monkeypatch.setenv("HYPRIAL_OWNER", "hyprial-unit-owner")
        monkeypatch.setenv("HYPRIAL_HOME", str(home))
        # Every daemon a test spawns inherits this: loopback-only, no discovery,
        # forwarding, gossip or usage fetch (2026-09-16 ban on "isolated"
        # daemons that could still reach the tailnet).
        monkeypatch.setenv("HYPRIAL_NETWORK_ISOLATED", "1")
        monkeypatch.setenv("HARNESS_STATE_DIR", str(state))
        monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
        monkeypatch.delenv("HARNESS_CONFIG", raising=False)
        monkeypatch.setenv("HYPRIAL_ZENOH_LISTEN", "")
        monkeypatch.setenv("HYPRIAL_ZENOH_CONNECT", "")
        # A daemon-managed worker (this very test runner, when pytest runs
        # inside one) exports the session binding the fenced PAC write
        # methods key on.  Without this, ``pac`` CLI tests would silently
        # ride the daemon path in worker contexts and the local human path
        # everywhere else -- the same test, two behaviors, decided by where
        # it runs.  Tests of the worker path set the pair explicitly.
        monkeypatch.delenv("HYPRIAL_WORKER_ACTOR", raising=False)
        monkeypatch.delenv("HYPRIAL_WORKER_SESSION_REF", raising=False)
        monkeypatch.delenv("HYPRIAL_MANAGED_WORKER", raising=False)
        yield


@pytest.fixture
def own_daemon_application(request):  # type: ignore[no-untyped-def]
    """Give one test explicit ownership of a real daemon composition root."""

    def own(
        app: hyprial.daemon.impl.application.DaemonApplication,
    ) -> hyprial.daemon.impl.application.DaemonApplication:
        def close() -> None:
            app._close()
            app._close_home_lease()

        request.addfinalizer(close)
        return app

    return own


def _warm_zenoh_runtime() -> None:
    """Pay zenoh-py's one-time descriptor costs up front (card #77).

    eclipse-zenoh 1.9.0 keeps the tokio runtime's descriptors (kqueue et al.)
    for the process lifetime: 9 fds on the first ``zenoh.open`` of any kind
    and 6 more on the first actually-connected session pair -- verified to be
    one-time (subsequent sessions and pairs add nothing).  Warming a real
    connected pair with a queryable round trip here moves that constant off
    every zenoh test's per-test budget.
    """

    try:
        from hyprial.daemon.impl.transport.zenoh.config import ZenohConfig
        from hyprial.daemon.impl.transport.zenoh.transport import ZenohTransport
    except Exception:
        return
    # The connection tier only opens when a session genuinely connects, and
    # zenoh will not listen on port 0, so hand it a real free port.
    import socket as _socket

    with _socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    address = f"tcp/127.0.0.1:{port}"
    holder: ZenohTransport | None = None
    guest: ZenohTransport | None = None
    try:
        holder = ZenohTransport(ZenohConfig(listen=(address,)))
        guest = ZenohTransport(ZenohConfig(connect=(address,)))
        registration = holder.declare_queryable("hyprial-warmup/**", lambda _q: b"{}")
        guest.get("hyprial-warmup/probe", timeout=1.0)
        registration.close()
    except Exception:
        pass  # warmup is best-effort; the hygiene check names what remains
    finally:
        for transport in (guest, holder):
            if transport is not None:
                transport.close()


def _warm_threaded_http_server() -> None:
    """Pay the one-time descriptor cost of a served-in-a-thread HTTP server.

    Starting ``serve_forever`` on a background thread creates one AF_UNIX
    descriptor the first time in a process and never again: measured across
    four start/stop cycles, the first adds one and the rest add none.  So it
    is one-time state, not a per-test leak, and warming it here keeps the
    first test using a local HTTP fake from being charged for it (card #77's
    rule: warm one-time owners before measurement begins).

    Warming raises the starting point, not the sensitivity: each test is still
    measured as a before/after difference, so a real leak still lands in the
    delta.  Verified rather than assumed -- a test holding an unclosed socket
    past its end is still reported after this runs.

    ⚠️ That is also the condition under which warming would stop being safe:
    if the guard were ever changed to assert an absolute descriptor count
    rather than a per-test difference, every warmed descriptor would become
    headroom a leak could hide in.
    """

    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class _Silent(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), _Silent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def _warm_lark_websocket_module() -> None:
    """Pay ``lark_oapi.ws``'s import-time event loop before measurement.

    That module runs ``asyncio.get_event_loop()`` at import, which builds a
    kqueue plus a self-pipe: three descriptors, once per process.  Measured
    rather than assumed -- importing costs 3 and five successive
    ``LarkEventStream`` constructions afterwards cost 0 each -- so this is
    one-time state and not a per-test leak (card #77's rule: warm one-time
    owners before measurement begins).

    The import is deferred until the first ws client is *constructed*, because
    ``lark.ws.Client`` is only resolved inside ``_default_factory``.  Whichever
    test constructs one first is charged for the whole module, which is a
    property of test ordering rather than of that test -- and until the
    endpoint-override tests existed, nothing in this suite constructed one at
    all.

    ⚠️ Same boundary as the HTTP-server warmup above: this raises the starting
    point, not the sensitivity, because each test is still measured as a
    before/after difference.  It would stop being safe if the guard were
    changed to assert an absolute count.
    """

    try:
        import lark_oapi.ws  # noqa: F401
    except Exception:
        # Best-effort, exactly like the other warmups: an unavailable optional
        # dependency must not stop the suite from running.
        pass


_warm_zenoh_runtime()
_warm_threaded_http_server()
_warm_lark_websocket_module()


@pytest.fixture
def lark_ws_default_loop():
    """Point this thread's default loop at the one the Lark SDK actually runs.

    ``lark_oapi.ws.Client`` reads the *thread's* default event loop when it
    constructs its expiring cache, but every ``start()`` call runs on the
    module-level loop the SDK owns.  When an earlier test has run
    ``asyncio.run()`` -- which sets a loop and then closes it -- construction
    finds no usable default and builds a fresh one that nobody closes: a
    kqueue and two unix descriptors, per construction, in the full suite only.

    Aligning the two costs nothing (measured at +0 descriptors in a
    deliberately contaminated process) and puts the cache's cron task on the
    loop that will actually run it, which owning a private loop here would
    not.
    """

    import lark_oapi.ws.client as ws_client

    try:
        previous = asyncio.get_event_loop_policy().get_event_loop()
    except Exception:  # noqa: BLE001 - best-effort restore only
        previous = None
    asyncio.set_event_loop(ws_client.loop)
    try:
        yield ws_client.loop
    finally:
        asyncio.set_event_loop(previous)


def _mp_noop() -> None:
    """Spawn-warmup target; must live at module level to be picklable."""


def _warm_multiprocessing_tracker() -> None:
    """Pay the spawn context's one-time descriptors (resource tracker pipe
    et al.) in this process before measurement; verified one-time: a second
    spawn round trip adds nothing (card #77)."""

    import multiprocessing

    try:
        context = multiprocessing.get_context("spawn")
        process = context.Process(target=_mp_noop)
        process.start()
        process.join(timeout=10)
    except Exception:
        pass


_warm_multiprocessing_tracker()


def _fd_set() -> set[int]:
    """Currently open descriptor numbers, without self-observation artifacts.

    ``os.listdir('/dev/fd')`` briefly opens a directory descriptor that
    appears in its own listing, and ``lsof`` reports its own stdout pipe;
    both have produced ghost "leaks" in earlier tooling.  Every candidate is
    therefore confirmed with ``fstat`` afterwards — the transient artifact is
    already closed by then and is dropped.
    """

    candidates = {int(fd) for fd in os.listdir("/dev/fd")}
    confirmed = set()
    for fd in candidates:
        try:
            mode = os.fstat(fd).st_mode
        except OSError:
            continue  # transient listdir artifact
        if stat.S_ISDIR(mode):
            continue
        confirmed.add(fd)
    return confirmed


def _fd_open(fd: int) -> bool:
    """Whether the descriptor is open right now (no snapshot artifacts)."""

    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _still_open_after_settle(fds: set[int]) -> set[int]:
    """Candidates still open after a bounded settle for async teardown closes."""

    settled = set(fds)
    for _ in range(3):
        if not settled:
            break
        time.sleep(0.1)
        settled = {fd for fd in settled if _fd_open(fd)}
    return settled


def _describe_fds(fds: set[int]) -> str:
    if not fds:
        return ""
    try:
        out = subprocess.run(
            ["lsof", "-p", str(os.getpid()), "-F", "ftn"],
            capture_output=True,
            text=True,
        ).stdout
    except OSError:
        # Minimal image without lsof: on Linux /proc answers directly.
        parts = []
        for fd in sorted(fds):
            try:
                parts.append(f"fd={fd} target={os.readlink(f'/proc/self/fd/{fd}')}")
            except OSError:
                parts.append(f"fd={fd} (unresolvable)")
        return "\n  ".join(parts)
    info: dict[str, dict[str, str]] = {}
    cur_fd = None
    for line in out.splitlines():
        if line.startswith("f"):
            cur_fd = line[1:]
        elif line.startswith("t") and cur_fd is not None:
            info.setdefault(cur_fd, {})["type"] = line[1:]
        elif line.startswith("n") and cur_fd is not None:
            info.setdefault(cur_fd, {})["name"] = line[1:]
    parts = []
    for fd in sorted(fds):
        entry = info.get(str(fd), {})
        parts.append(f"fd={fd} type={entry.get('type')} name={entry.get('name')}")
    return "\n  ".join(parts)


@pytest.fixture(autouse=True)
def _fd_hygiene(request):
    before = _fd_set()
    yield
    # pytest keeps resolved fixture values on ``item.funcargs`` through the
    # whole teardown phase (it drops the attribute only afterwards), so a
    # rig-style fixture that built a DaemonApplication would survive the
    # collection below and take its sqlite fds with it.  All real fixture
    # finalizers have already run at this point (autouse fixtures tear down
    # last); clearing the dict only releases pytest's own pin.
    funcargs = getattr(request.node, "funcargs", None)
    if funcargs:
        funcargs.clear()
    # Release the deliberate global pin first. Most tests have no remaining
    # descriptors: collect young cycles without rescanning the entire imported
    # application/test graph. Any outstanding descriptor still requires a full
    # collection, including cycles promoted into an older generation.
    set_default_registry(None)
    gc.collect(0)
    leaked = _fd_set() - before
    if leaked:
        gc.collect()
        leaked = _fd_set() - before
    if leaked:
        confirmed = _still_open_after_settle(leaked)
        vanished = len(leaked) - len(confirmed)
        leaked = confirmed
    if leaked:
        suffix = f" (+{vanished} already closed at report time)" if vanished else ""
        raise AssertionError(
            f"test leaked {len(leaked)} file descriptor(s){suffix}:\n  {_describe_fds(leaked)}\n"
            "Close what you open (or, for suite-level one-time state, warm it "
            "in tests/conftest.py). See card #77: per-test fd accumulation is "
            "what pushes long runs over macOS FD_SETSIZE (#76's cliff)."
        )


class _CiTestProfile:
    """Aggregate measurements; GC time overlaps phases and must not be added."""

    def __init__(self, config):
        import time

        self.clock = time.perf_counter
        self.started = self.clock()
        self.phases = {"setup": 0.0, "call": 0.0, "teardown": 0.0}
        self.collections = {0: 0, 1: 0, 2: 0}
        self.gc_seconds = 0.0
        self.gc_started = None
        self.files = {}
        self.completed = 0
        self.collection_seconds = 0.0
        self.result = None
        self.is_controller = (
            not hasattr(config, "workerinput")
            and bool(config.getoption("numprocesses", default=0))
        )
        self.worker_profiles = {}
        self.collection_count = 0
        self.collection_digest = None

    def _observe_gc(self, phase, info):
        if phase == "start":
            self.gc_started = self.clock()
        elif self.gc_started is not None:
            self.gc_seconds += self.clock() - self.gc_started
            generation = info["generation"]
            self.collections[generation] = self.collections.get(generation, 0) + 1
            self.gc_started = None

    def pytest_sessionstart(self, session):
        self.started = self.clock()
        gc.callbacks.append(self._observe_gc)

    def pytest_collection_finish(self, session):
        self.collection_seconds = self.clock() - self.started
        self._record_collection([item.nodeid for item in session.items])

    def _record_collection(self, ids):
        self.collection_count = len(ids)
        self.collection_digest = hashlib.sha256(
            "\n".join(sorted(ids)).encode()
        ).hexdigest()

    @pytest.hookimpl(optionalhook=True)
    def pytest_xdist_node_collection_finished(self, node, ids):
        # xdist itself rejects workers with different collections. Retain the
        # fingerprint so a serial/parallel comparison can also prove coverage.
        self._record_collection(ids)
        self.collection_seconds = self.clock() - self.started

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        profile = getattr(node, "workeroutput", {}).get("hyprial_ci_profile")
        if profile is not None:
            self.worker_profiles[node.gateway.id] = profile

    def pytest_runtest_logreport(self, report):
        # xdist synthesizes a crash report with when="???". Preserve it
        # without turning the original worker failure into a profiler error.
        self.phases[report.when] = self.phases.get(report.when, 0.0) + report.duration
        path = report.nodeid.split("::", 1)[0]
        self.files[path] = self.files.get(path, 0.0) + report.duration
        if report.when == "teardown":
            self.completed += 1
            if self.completed % 500 == 0:
                print("CI_TEST_PROGRESS " + json.dumps(self._snapshot()), flush=True)

    def _snapshot(self):
        return {
            "completed": self.completed,
            "collected": self.collection_count,
            "collection_digest": self.collection_digest,
            "parallel_controller": self.is_controller,
            "wall_seconds": round(self.clock() - self.started, 3),
            "collection_seconds": round(self.collection_seconds, 3),
            "phases_seconds": {k: round(v, 3) for k, v in self.phases.items()},
            "gc_seconds": round(self.gc_seconds, 3),
            "gc_collections": self.collections,
        }

    def pytest_sessionfinish(self, session, exitstatus):
        self.pytest_unconfigure(None)
        self.result = {
            **self._snapshot(),
            "exit_status": int(exitstatus),
            "slowest_files_seconds": {
                k: round(v, 3)
                for k, v in sorted(
                    self.files.items(), key=lambda item: item[1], reverse=True
                )[:15]
            },
        }
        if hasattr(session.config, "workeroutput"):
            session.config.workeroutput["hyprial_ci_profile"] = self.result
        elif self.is_controller:
            # Phase durations above are summed work across workers, not wall
            # time. The controller GC counters do not describe worker cleanup.
            self.result["workers"] = self.worker_profiles
            self.result["worker_gc_seconds"] = round(
                sum(p["gc_seconds"] for p in self.worker_profiles.values()), 3
            )

    def pytest_terminal_summary(self, terminalreporter):
        terminalreporter.write_line("CI_TEST_PROFILE " + json.dumps(self.result))

    def pytest_unconfigure(self, config):
        if self._observe_gc in gc.callbacks:
            gc.callbacks.remove(self._observe_gc)


def pytest_configure(config):
    from tests.support.runtime.environment import ServiceUnitTripwire, real_account_home

    config.pluginmanager.register(ServiceUnitTripwire(real_account_home()), "service-unit-tripwire")
    if os.environ.get("HYPRIAL_CI_PROFILE") == "1":
        config.pluginmanager.register(_CiTestProfile(config), "hyprial-ci-profile")
