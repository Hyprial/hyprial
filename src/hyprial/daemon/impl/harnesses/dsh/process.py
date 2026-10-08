"""DshHarnessProcess: streaming process driver over the DSH worker."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING


from hyprial.kernel import HarnessLaunchSpec
from hyprial.kernel import Logger

from hyprial.kernel import OwnedProcessGroup
from hyprial.daemon.impl.harnesses.streaming.process  import (
    StreamingTurnProcess,
    )
from hyprial.daemon.impl.harnesses.streaming.protocol  import (
    TurnClientFactory,
    TurnCompletedObserver,
)
from hyprial.daemon.impl.harnesses.worker_channel  import WorkerChannel
from hyprial.daemon.impl.harnesses.model_provider  import validate_model_selection

if TYPE_CHECKING:
    from hyprial.identity import ChildEnvironmentLaunch
from hyprial.daemon.impl.harnesses.dsh.api import (
    DSH_REQUEST_TIMEOUT_SECONDS,
    DshApiError,
    DshHttpApi,
    _close_stream,
    _close_stream_async,
    _iter_pipe_lines,
)
from hyprial.daemon.impl.harnesses.dsh.client import (
    DshApiClient,
    _DshSession,
    _unsupported_dsh_message,
)
from hyprial.daemon.impl.harnesses.dsh.worker_home import (
    _environment_secrets,
    _validate_client_arguments,
    _option_value,
    dsh_worker_home,
    prepare_worker_home,
)

# Declared start budget for the launcher's readiness wait.  ``wait_ready``
# resolves only after ``DshApiClient.__aenter__`` returns, whose readiness path
# is a sequence of request-bounded steps: the banner read, ``host.describe``,
# ``agentPreset.copy`` (worker channel), ``session.create``/``session.history``,
# ``session.models`` and ``session.selectModel``.  Six sequential
# ``DSH_REQUEST_TIMEOUT_SECONDS`` waits plus a margin is therefore the real
# composition; ``actor_runtime`` exposes stop/drain/shutdown timeouts and
# restart budgets but no request-level deadline primitive (the same reason
# codex's ``APP_SERVER_STARTUP_TIMEOUT_SECONDS_DEFAULT`` is declared here).
# Registered in ``tests/supervision_exemptions.json``.
DSH_STARTUP_TIMEOUT_SECONDS_DEFAULT = DSH_REQUEST_TIMEOUT_SECONDS * 6 + 15.0

# Granularity of the banner wait: a poll slice, not a budget.
DSH_BANNER_POLL_SECONDS = 0.1

# Bounded wait for a closed or rejected generation's process group to be
# reaped.  Part of the start/stop handshake, so it is registered in
# ``tests/supervision_exemptions.json`` alongside the declared start budget.
DSH_STARTUP_REAP_SECONDS = 2.0

_DSH_WEB_BANNER = re.compile(r"^dsh web: http://127\.0\.0\.1:(\d+)\s*$")

# dsh >= 0.1.5 prints ``dsh web: http://127.0.0.1:<port>/?token=<launch token>``:
# its web API moved to a token-to-cookie login, ``/api/<service>/<method>``
# routes and a new envelope, which this client does not speak yet.  Seeing
# that banner fails the worker at once with a readable reason instead of
# leaving it "never ready" (which read as a hang).
_DSH_TOKEN_BANNER = re.compile(r"^dsh web: http://127\.0\.0\.1:\d+/\?token=")

_DSH_LAUNCH_TOKEN = re.compile(rb"([?&]token=)[^\s&]+")

_DSH_IO_TAIL_BYTES = 64 * 1024

#: An unterminated line cannot buffer without bound; ``readline(size)`` caps it.
_DSH_MAX_LINE_BYTES = 64 * 1024

@dataclass(frozen=True, slots=True)
class _DshGeneration:
    """One spawned ``dsh`` process and the transport bound to its port."""

    process: subprocess.Popen[bytes]
    group: OwnedProcessGroup
    api: DshHttpApi
    endpoint: str
    argv: tuple[str, ...]
    #: This generation's own output buffers: a late line from a replaced child
    #: must never land in the next generation's tail or ``exit_error``.
    stdout_tail: bytearray
    stderr_tail: bytearray

class DshHarnessProcess(StreamingTurnProcess):
    """Daemon-managed DSH session driven through the shared turn pump.

    Each (re)connect spawns one private ``dsh --profile web --host 127.0.0.1
    --port 0`` in its own session (process group), reads the OS-assigned port
    out of the child's banner, and hands that endpoint to a fresh
    ``DshHttpApi``.  Ownership mirrors the codex app-server: an
    ``OwnedProcessGroup`` fenced by PID and birth identity, so stop/``hyprial
    down`` signals only a generation this daemon actually started.
    """

    def __init__(
        self,
        spec: HarnessLaunchSpec,
        *,
        client_factory: TurnClientFactory | None = None,
        worker_channel: WorkerChannel | None = None,
        env: Mapping[str, str] | None = None,
        complete_launch: "ChildEnvironmentLaunch | None" = None,
        state_dir: Path | None = None,
        dsh_home: Path | None = None,
        on_turn_completed: TurnCompletedObserver | None = None,
    ) -> None:
        if spec.harness != "dsh" or not spec.headless:
            raise ValueError("DSH API process requires a headless dsh spec")
        if complete_launch is not None and env is not None:
            raise ValueError(
                "complete child environment cannot be combined with a "
                "partial env mapping"
            )
        if (
            complete_launch is not None
            and worker_channel is not None
            and complete_launch.actor != worker_channel.actor
        ):
            raise ValueError("DSH worker channel and complete environment actors differ")
        self.spec = spec
        self._complete_launch = complete_launch
        self._session = _DshSession(_option_value(spec.args, "--session-id"))
        self.worker_channel = worker_channel
        if dsh_home is not None:
            self.dsh_home: Path | None = Path(dsh_home)
        elif worker_channel is not None:
            self.dsh_home = dsh_worker_home(worker_channel.state_dir, spec.name)
        elif state_dir is not None:
            self.dsh_home = dsh_worker_home(Path(state_dir), spec.name)
        else:
            # Only reachable for injected client factories (tests) and direct
            # embedding; a real spawn fails loudly below instead of guessing a
            # shared home.
            self.dsh_home = None
        # Deterministic configuration errors fail once, before the pump (and
        # therefore before any child) exists; they must not be rediscovered on
        # every retry.  An injected client factory owns its own arguments.
        if client_factory is None:
            if self.dsh_home is None:
                raise ValueError(
                    "managed DSH requires a worker channel or a state directory"
                )
            _validate_client_arguments(spec)
            # The same deterministic checks ``DshApiClient.__aenter__`` makes;
            # re-running them there is fine, but they must not be discovered
            # only after the first child already exists (that would respawn
            # one process per retry in the reconnect window).
            validate_model_selection(
                spec.harness, spec.model_provider, spec.model
            )
            if worker_channel is not None and self._session.session_id is not None:
                raise DshApiError(
                    "a fresh worker MCP identity cannot resume an existing DSH "
                    "session; omit --session-id"
                )
            complete_environment = (
                complete_launch.environment.for_exec()
                if complete_launch is not None
                else None
            )
            if complete_environment is not None:
                missing = [
                    name
                    for name in ("PATH", "HOME")
                    if not complete_environment.get(name)
                ]
                if missing:
                    raise DshApiError(
                        "complete child environment for DSH is missing required "
                        + ", ".join(missing)
                    )
            binary = (
                shutil.which("dsh", path=complete_environment["PATH"])
                if complete_environment is not None
                else shutil.which("dsh")
            )
            if binary is None:
                raise DshApiError("dsh not on PATH")
        self._base_env: dict[str, str] | None = (
            dict(env) if env is not None else None
        )
        logger = (
            Logger.worker(worker_channel.state_dir, runtime="dsh", name=spec.name)
            if worker_channel is not None
            else None
        )
        self._dsh_lock = threading.Lock()
        self._io_log_lock = threading.Lock()
        self._generation: _DshGeneration | None = None
        self._exit_error: str | None = None
        self._secret_values: tuple[str, ...] = ()
        self._stdout_tail = bytearray()
        self._stderr_tail = bytearray()
        self._io_log_path = (
            self.dsh_home.parent / "io.log" if self.dsh_home is not None else None
        )
        factory = client_factory or self._connect_generation
        super().__init__(
            harness="dsh",
            label="DSH API",
            client_factory=factory,
            thread_name=f"hyprial-dsh-{spec.name}",
            reconnect_delay_max_seconds=1.0,
            logger=logger,
            force_stop=self._force_stop_client,
            force_stopped=self._force_stopped_client,
            on_turn_completed=on_turn_completed,
        )

    @property
    def session_ref(self) -> str | None:
        return self._session.session_id

    @property
    def endpoint(self) -> str | None:
        """The current generation's real endpoint, or ``None`` before spawn."""

        generation = self._generation
        return generation.endpoint if generation is not None else None

    @property
    def pid(self) -> int | None:
        """The live DSH child PID of the current generation, if any."""

        generation = self._generation
        if generation is None:
            return None
        process = generation.process
        return process.pid if process.poll() is None else None

    @property
    def argv(self) -> tuple[str, ...] | None:
        generation = self._generation
        return generation.argv if generation is not None else None

    @property
    def exit_error(self) -> str | None:
        """The current or most recent child's exit, with its status code."""

        return self._exit_error

    def io_log(self) -> bytes:
        """The bounded tail of the child's drained stdout/stderr."""

        if self._io_log_path is None:
            return b""
        try:
            return self._io_log_path.read_bytes()
        except OSError:
            return b""

    def stderr_tail(self) -> bytes:
        return bytes(self._stderr_tail)

    def _connect_generation(self) -> DshApiClient:
        self._terminate_generation()
        if self.dsh_home is None:
            raise DshApiError(
                "managed DSH requires a worker channel or a state directory"
            )
        prepare_worker_home(self.dsh_home)
        if self._complete_launch is not None:
            # The launch is the complete child mapping resolved for this actor.
            # DSH_HOME is the sole harness-owned addition: it selects the
            # private session/preset root this managed process prepared and
            # must not be inherited from either the daemon or the agent.
            environment = self._complete_launch.environment.for_exec()
            binary = shutil.which("dsh", path=environment["PATH"])
        else:
            # Legacy direct callers retain the historical partial-overlay
            # behaviour until they opt into a complete launch.
            environment = {**os.environ, **(self._base_env or {})}
            binary = shutil.which("dsh")
        if binary is None:
            raise DshApiError("dsh not on PATH")
        environment["DSH_HOME"] = str(self.dsh_home)
        # Redaction is derived from the exact mapping handed to Popen, after
        # the one harness-owned addition, so granted values cannot reach any
        # stdout/stderr tail, transport error, or io.log entry.
        self._secret_values = _environment_secrets(environment)
        argv = (
            binary,
            "--profile",
            "web",
            "--host",
            "127.0.0.1",
            "--port",
            "0",
        )
        process = subprocess.Popen(
            argv,
            cwd=str(self.dsh_home),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        group = OwnedProcessGroup(label="DSH")
        # Every failure after spawn -- identity registration, the banner and its
        # drain setup, the client's own argument parsing -- must reap this
        # fresh process group.  Otherwise the pump's next retry leaves the
        # previous child running and every generation leaks one DSH.
        try:
            group.register(process.pid)
            if self._complete_launch is not None and self._logger is not None:
                self._logger.info(
                    "worker.environment.receipt",
                    actor=self._complete_launch.actor,
                    grants=[
                        {"grantId": grant_id, "revision": revision}
                        for grant_id, revision in self._complete_launch.grants
                    ],
                )
            endpoint, stdout_tail, stderr_tail = self._read_banner(process, group)
            api = DshHttpApi(
                endpoint,
                timeout_seconds=DSH_REQUEST_TIMEOUT_SECONDS,
                redact=self._redact_text,
            )
            generation = _DshGeneration(
                process, group, api, endpoint, argv, stdout_tail, stderr_tail
            )
            client = DshApiClient(
                self.spec,
                api=api,
                session=self._session,
                worker_channel=self.worker_channel,
                dsh_home=self.dsh_home,
                redact_secrets=self._redact_text,
            )
            with self._dsh_lock:
                self._exit_error = None
                self._generation = generation
            threading.Thread(
                target=self._watch_generation,
                args=(generation, client),
                name=f"hyprial-dsh-exit-{self.spec.name}",
                daemon=True,
            ).start()
            return client
        except BaseException:
            group.force_close()
            self._reap(process)
            raise

    def _watch_generation(
        self, generation: _DshGeneration, client: DshApiClient
    ) -> None:
        """Publish the child's real exit status to the client and this process."""

        returncode = generation.process.wait()
        if self._generation is not generation:
            return
        detail = bytes(generation.stderr_tail).decode(
            "utf-8", errors="replace"
        ).strip()
        message = f"DSH process exited with status {returncode}"
        if detail:
            message = f"{message}: {detail[-500:]}"
        self._exit_error = message
        client.exit_error = message
        client.running = False

    def _read_banner(
        self, process: subprocess.Popen[bytes], group: OwnedProcessGroup
    ) -> tuple[str, bytearray, bytearray]:
        """Read the first ``dsh web:`` banner, then keep draining both pipes.

        Returns the endpoint plus this generation's own output buffers.
        """

        stdout = process.stdout
        stderr = process.stderr
        assert stdout is not None and stderr is not None
        stdout_tail = bytearray()
        stderr_tail = bytearray()
        with self._dsh_lock:
            self._stdout_tail = stdout_tail
            self._stderr_tail = stderr_tail
        self._io_log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self._io_log_path.write_bytes(b"")
            os.chmod(self._io_log_path, 0o600)
        except OSError:
            pass
        banner_event = threading.Event()
        endpoint: list[str] = []
        unsupported: list[str] = []

        def drain_stdout() -> None:
            try:
                for raw in _iter_pipe_lines(stdout):
                    # The launch token is a live credential for the web API:
                    # mask it before anything is buffered or logged.
                    redacted = self._redact_output(
                        _DSH_LAUNCH_TOKEN.sub(rb"\1<redacted>", raw)
                    )
                    with self._dsh_lock:
                        self._extend_tail(stdout_tail, redacted)
                    self._append_io_log(b"STDOUT " + redacted)
                    if not endpoint:
                        line = redacted.decode("utf-8", errors="replace").strip()
                        match = _DSH_WEB_BANNER.match(line)
                        if match:
                            endpoint.append(f"http://127.0.0.1:{match.group(1)}")
                        elif _DSH_TOKEN_BANNER.match(line):
                            unsupported.append(line)
                            banner_event.set()
            except (OSError, ValueError):
                pass
            finally:
                banner_event.set()
                _close_stream(stdout)

        def drain_stderr() -> None:
            try:
                for raw in _iter_pipe_lines(stderr):
                    redacted = self._redact_output(
                        _DSH_LAUNCH_TOKEN.sub(rb"\1<redacted>", raw)
                    )
                    with self._dsh_lock:
                        self._extend_tail(stderr_tail, redacted)
                    self._append_io_log(b"STDERR " + redacted)
            except (OSError, ValueError):
                pass
            finally:
                _close_stream(stderr)

        threading.Thread(
            target=drain_stdout, name=f"hyprial-dsh-stdout-{self.spec.name}", daemon=True
        ).start()
        threading.Thread(
            target=drain_stderr, name=f"hyprial-dsh-stderr-{self.spec.name}", daemon=True
        ).start()

        deadline = time.monotonic() + DSH_REQUEST_TIMEOUT_SECONDS
        while not endpoint:
            if self._stopping.is_set() or unsupported:
                break
            # stdout reached EOF without a banner: no later line can carry one,
            # so do not spin on a set event until the deadline.
            if banner_event.is_set():
                break
            if process.poll() is not None:
                banner_event.wait(DSH_BANNER_POLL_SECONDS)
                break
            if time.monotonic() >= deadline:
                break
            banner_event.wait(
                min(DSH_BANNER_POLL_SECONDS, max(0.0, deadline - time.monotonic()))
            )
        if endpoint:
            return endpoint[0], stdout_tail, stderr_tail

        group.force_close()
        self._reap(process)
        if unsupported:
            raise DshApiError(_unsupported_dsh_message(process))
        detail = bytes(stderr_tail).decode("utf-8", errors="replace").strip()
        message = (
            "DSH web banner was not printed within "
            f"{DSH_REQUEST_TIMEOUT_SECONDS:g}s (child exit status "
            f"{process.returncode})"
        )
        if detail:
            message = f"{message}: {detail[-2000:]}"
        raise DshApiError(message)

    @staticmethod
    def _extend_tail(buffer: bytearray, chunk: bytes) -> None:
        buffer.extend(chunk)
        if len(buffer) > _DSH_IO_TAIL_BYTES:
            del buffer[: len(buffer) - _DSH_IO_TAIL_BYTES]

    def _redact_text(self, text: str) -> str:
        for secret in self._secret_values:
            text = text.replace(secret, "[REDACTED]")
        return text

    def _redact_output(self, chunk: bytes) -> bytes:
        """Scrub the child's env secret values before anything keeps them.

        stderr tails become ``exit_error`` -> status ``error`` and the worker
        turn event, and the drain log is on disk; a child that echoes its own
        environment must not leak it into any of them.
        """

        if not self._secret_values:
            return chunk
        return self._redact_text(chunk.decode("utf-8", errors="replace")).encode(
            "utf-8"
        )

    def _append_io_log(self, chunk: bytes) -> None:
        path = self._io_log_path
        if path is None:
            return
        with self._io_log_lock:
            try:
                with open(path, "ab") as handle:
                    handle.write(chunk)
                    handle.flush()
                if path.stat().st_size > _DSH_IO_TAIL_BYTES:
                    path.write_bytes(path.read_bytes()[-_DSH_IO_TAIL_BYTES:])
            except OSError:
                pass

    def _terminate_generation(self) -> None:
        generation = self._generation
        if generation is None:
            return
        if self._stopping.is_set():
            generation.api.close()
        else:
            generation.api.cancel_active()
        generation.group.force_close()
        self._reap(generation.process)

    @staticmethod
    def _reap(process: subprocess.Popen[bytes]) -> None:
        try:
            process.wait(timeout=DSH_STARTUP_REAP_SECONDS)
        except (subprocess.TimeoutExpired, OSError, ValueError):
            pass
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                _close_stream_async(stream)

    def _force_stop_client(self) -> None:
        self._terminate_generation()
        with self._lock:
            # During __aenter__ the pump has published only _connecting_client;
            # reaching it is what unblocks a pre-socket / pre-connection call.
            client = self._client or self._connecting_client
        force_stop = getattr(client, "force_stop", None)
        if callable(force_stop):
            force_stop()

    def _force_stopped_client(self) -> bool:
        generation = self._generation
        if generation is not None:
            if not generation.api.stopped():
                return False
            return generation.group.stopped()
        with self._lock:
            client = self._client or self._connecting_client
        force_stopped = getattr(client, "force_stopped", None)
        return bool(force_stopped()) if callable(force_stopped) else True
