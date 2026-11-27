"""Private hc core lifecycle supervision."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
import os
from pathlib import Path
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from hyprial.kernel import cleanup_private_hc_runtime_dirs
from hyprial.kernel import hc_bin_root
from hyprial.kernel import hc_core_socket_path
from hyprial.kernel import hc_session_name

from ..acquisition import HcLock
from ..acquisition import ensure_pinned_hc_installed
from ..acquisition import remove_hc_version_dir
from ..client import HcClient
from ..client import HcEventSubscription
from ..client import HcHeartbeatTimeout
from ..client import HcStreamClosed
from ..client import HcTransportError
from ..client import TerminalRecord
from ..client import probe_socket_alive
from .environment import build_private_hc_environment
from .environment import write_private_hc_config
from .naming import member_terminal_labels
from .naming import member_terminal_name


class HcGenerationIncompatibleError(RuntimeError):
    """Raised when a private core belongs to a different hc generation."""


@dataclass(frozen=True, slots=True)
class SupervisorSnapshot:
    """Public state of the private core supervisor."""

    running: bool
    core_version: str | None
    pinned_version: str
    version_lagging: bool
    generation_incompatible: bool
    pending_old_versions: tuple[str, ...]
    runtime_cleanup_warnings: tuple[str, ...]
    tracked_terminals: tuple[str, ...]


class PrivateCoreSupervisor:
    """Own one private hc core and its daemon-owned terminal records."""

    def __init__(
        self,
        home: Path,
        *,
        lock: HcLock,
        source_env: Mapping[str, str] | None = None,
        passthrough_env: Mapping[str, str] | None = None,
        client_factory: Callable[[Path], HcClient] | None = None,
        acquirer: Callable[[], Path] | None = None,
        launcher: Callable[[Sequence[str], Mapping[str, str]], Any] | None = None,
        restart_runner: Callable[[Sequence[str], Mapping[str, str]], Any] | None = None,
        cleanup: Callable[[str], Sequence[Path]] | None = None,
        version_remover: Callable[[str, Path], bool] | None = None,
        subscription_timeout: float = 0.2,
        startup_timeout: float = 5.0,
    ) -> None:
        self.home = Path(home).resolve()
        self.lock = lock
        self._source_env = dict(os.environ if source_env is None else source_env)
        self._passthrough_env = dict(passthrough_env or {})
        self._environment = build_private_hc_environment(
            self.home, self._source_env, passthrough=self._passthrough_env
        )
        write_private_hc_config(self.home)
        self._socket = hc_core_socket_path(self.home)
        self._client_factory = client_factory or (lambda path: HcClient(path))
        self._client: HcClient | None = None
        self._acquirer = acquirer or (
            lambda: ensure_pinned_hc_installed(self.home, lock=self.lock)
        )
        self._launcher = launcher or self._default_launcher
        self._restart_runner = restart_runner or self._default_restart_runner
        self._cleanup = cleanup or cleanup_private_hc_runtime_dirs
        self._version_remover = version_remover or (
            lambda version, home: remove_hc_version_dir(version, home)
        )
        self._subscription_timeout = subscription_timeout
        self._startup_timeout = startup_timeout
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hc-acquirer")
        self._acquire_future: Future[Path] | None = None
        self._subscription: HcEventSubscription | None = None
        self._subscription_thread: threading.Thread | None = None
        self._state_lock = threading.RLock()
        self._tracked: dict[str, TerminalRecord] = {}
        self._running = False
        self._core_version: str | None = None
        self._version_lagging = False
        self._generation_incompatible = False
        self._pending_old_versions: set[str] = set()
        self._runtime_cleanup_warnings: tuple[str, ...] = ()
        self._closed = False

    def snapshot(self) -> SupervisorSnapshot:
        with self._state_lock:
            return SupervisorSnapshot(
                running=self._running,
                core_version=self._core_version,
                pinned_version=self.lock.version,
                version_lagging=self._version_lagging,
                generation_incompatible=self._generation_incompatible,
                pending_old_versions=tuple(sorted(self._pending_old_versions)),
                runtime_cleanup_warnings=self._runtime_cleanup_warnings,
                tracked_terminals=tuple(sorted(self._tracked)),
            )

    def probe_on_restore(self) -> SupervisorSnapshot:
        """Adopt a live socket without starting hc when no work is requested."""
        if not probe_socket_alive(self._socket):
            return self.snapshot()
        self.ensure_running()
        return self.snapshot()

    def ensure_running(self) -> HcClient:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("private hc supervisor is closed")
            client = self._client_factory(self._socket)
            if probe_socket_alive(self._socket):
                self._client = client
                try:
                    hello = client.hello()
                except HcTransportError:
                    self._client = None
                else:
                    if hello.version != self.lock.version:
                        self._connect_old_core(client, hello)
                    else:
                        self._adopt_connected(client, hello)
                    if self._generation_incompatible:
                        raise HcGenerationIncompatibleError(
                            "private hc core generation is incompatible with the pinned lock"
                        )
                    return client
            executable = self._acquire().result()
            self._launcher((str(executable), "server", "start"), self._environment)
            client = self._wait_for_client()
            hello = client.hello()
            if hello.version != self.lock.version or hello.generation != self.lock.generation:
                raise RuntimeError("started private hc core does not match the pinned lock")
            self._adopt_connected(client, hello)
            return client

    def ensure_running_and_run_terminal(
        self,
        argv: Sequence[str],
        *,
        agent_id: str,
        member: str,
        role: str,
        spec_digest: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        keep: bool = False,
        size: Mapping[str, int] | None = None,
    ) -> TerminalRecord:
        client = self.ensure_running()
        with self._state_lock:
            if self._generation_incompatible:
                raise HcGenerationIncompatibleError(
                    "new terminals are refused while hc generation is incompatible"
                )
        terminal = client.terminal_run(
            argv,
            cwd=cwd,
            env=env,
            labels=member_terminal_labels(agent_id, member, role, spec_digest),
            name=member_terminal_name(agent_id, member, role),
            keep=keep,
            no_parent=True,
            no_notify=True,
        )
        with self._state_lock:
            self._tracked[terminal.address] = terminal
        return terminal

    def remove_terminal(self, terminal: str) -> Mapping[str, Any]:
        client = self._require_client()
        result = client.terminal_rm(terminal)
        with self._state_lock:
            self._tracked.pop(terminal, None)
        self._maybe_stop_when_idle()
        return result

    def reconcile_versions(self) -> None:
        client = self._require_client()
        status = client.server_status()
        if not all(holder.current for holder in status.holders):
            return
        with self._state_lock:
            pending = tuple(self._pending_old_versions)
            self._pending_old_versions.clear()
        for version in pending:
            self._version_remover(version, self.home)
        if not pending:
            bin_root = hc_bin_root(self.home)
            if bin_root.is_dir():
                for candidate in bin_root.iterdir():
                    if candidate.is_dir() and candidate.name != self.lock.version and not candidate.name.startswith("."):
                        self._version_remover(candidate.name, self.home)

    def close_for_daemon_shutdown(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            subscription = self._subscription
            self._subscription = None
        if subscription is not None:
            subscription.close()
        thread = self._subscription_thread
        if thread is not None:
            thread.join(timeout=2)
        self._executor.shutdown(wait=True, cancel_futures=True)

    def _connect_old_core(self, client: HcClient, hello: Any) -> None:
        try:
            executable = self._acquire().result()
        except BaseException:
            with self._state_lock:
                self._core_version = hello.version
                self._running = True
                if hello.generation == self.lock.generation:
                    self._version_lagging = True
                    self._generation_incompatible = False
                    self._start_subscription(client)
                    return
                self._generation_incompatible = True
                self._start_subscription(client)
                return
        self._restart_runner(
            (str(executable), "server", "restart"),
            self._environment,
        )
        refreshed = self._wait_for_client()
        refreshed_hello = refreshed.hello()
        if refreshed_hello.version != self.lock.version:
            raise RuntimeError("private hc core did not switch to the pinned version")
        if refreshed_hello.generation != self.lock.generation:
            with self._state_lock:
                self._generation_incompatible = True
            raise HcGenerationIncompatibleError(
                "private hc core generation is incompatible after version switch"
            )
        with self._state_lock:
            self._pending_old_versions.add(hello.version)
            self._version_lagging = False
            self._generation_incompatible = False
        self._adopt_connected(refreshed, refreshed_hello)

    def _adopt_connected(self, client: HcClient, hello: Any) -> None:
        with self._state_lock:
            self._client = client
            self._core_version = hello.version
            self._running = True
            self._version_lagging = hello.version != self.lock.version
            self._generation_incompatible = hello.generation != self.lock.generation
        self._start_subscription(client)
        if not self._generation_incompatible:
            self.reconcile_versions()

    def _start_subscription(self, client: HcClient) -> None:
        with self._state_lock:
            old = self._subscription
            self._subscription = None
        if old is not None:
            old.close()
        subscription = HcEventSubscription(
            client, read_timeout=self._subscription_timeout
        )
        with self._state_lock:
            self._subscription = subscription
            self._tracked = {item.address: item for item in subscription.snapshot.terminals}
        thread = threading.Thread(
            target=self._follow_events,
            args=(client,),
            name="hc-events-follower",
            daemon=True,
        )
        self._subscription_thread = thread
        thread.start()

    def _follow_events(self, client: HcClient) -> None:
        delay = 0.05
        while True:
            with self._state_lock:
                subscription = self._subscription
                closed = self._closed
            if closed or subscription is None:
                return
            try:
                event = subscription.next_event()
            except HcHeartbeatTimeout:
                continue
            except HcStreamClosed:
                if closed:
                    return
                try:
                    fresh = HcEventSubscription(
                        client, read_timeout=self._subscription_timeout
                    )
                except HcTransportError:
                    time.sleep(delay)
                    delay = min(delay * 2, 0.5)
                    continue
                with self._state_lock:
                    if self._closed:
                        fresh.close()
                        return
                    self._subscription = fresh
                    self._tracked = {item.address: item for item in fresh.snapshot.terminals}
                delay = 0.05
                continue
            except HcTransportError:
                return
            delay = 0.05
            self._apply_event(event.type, event.terminal, event.data)

    def _apply_event(self, event_type: str, terminal: str | None, data: Mapping[str, Any]) -> None:
        if terminal is None:
            return
        with self._state_lock:
            current = self._tracked.get(terminal)
            if event_type == "removed":
                self._tracked.pop(terminal, None)
            elif current is not None and event_type in {"finished", "lost"}:
                self._tracked[terminal] = replace(current, state=event_type)
        if event_type == "removed":
            self._maybe_stop_when_idle()

    def _maybe_stop_when_idle(self) -> None:
        with self._state_lock:
            if not self._running or any(item.state == "live" for item in self._tracked.values()):
                return
            client = self._client
        if client is None:
            return
        client.server_stop()
        deadline = time.monotonic() + min(self._startup_timeout, 1.0)
        while time.monotonic() < deadline and probe_socket_alive(self._socket):
            time.sleep(0.01)
        failures = tuple(self._cleanup(hc_session_name(self.home)))
        with self._state_lock:
            self._running = False
            self._subscription = None
            self._runtime_cleanup_warnings = tuple(str(item) for item in failures)

    def _acquire(self) -> Future[Path]:
        with self._state_lock:
            if self._acquire_future is None or self._acquire_future.done():
                self._acquire_future = self._executor.submit(self._acquirer)
            return self._acquire_future

    def _wait_for_client(self) -> HcClient:
        deadline = time.monotonic() + self._startup_timeout
        while time.monotonic() < deadline:
            if probe_socket_alive(self._socket):
                return self._client_factory(self._socket)
            time.sleep(0.01)
        raise RuntimeError("private hc core did not open its socket")

    def _require_client(self) -> HcClient:
        with self._state_lock:
            client = self._client
        if client is None:
            raise RuntimeError("private hc core is not running")
        return client

    @staticmethod
    def _default_launcher(argv: Sequence[str], env: Mapping[str, str]) -> Any:
        return subprocess.Popen(list(argv), env=dict(env), close_fds=True)

    @staticmethod
    def _default_restart_runner(argv: Sequence[str], env: Mapping[str, str]) -> Any:
        return subprocess.run(list(argv), env=dict(env), check=True, timeout=30)


__all__ = [
    "HcGenerationIncompatibleError",
    "PrivateCoreSupervisor",
    "SupervisorSnapshot",
]
