"""Codex managed process-group authority, launch constants and connector."""
from __future__ import annotations


import asyncio
import json
import os
import platform
import shutil
import signal
import sys as sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from hyprial.identity import (
    ChildEnvironmentLaunch,
    whitelist_replacement_environment,
)
from hyprial.kernel import HarnessLaunchSpec

from hyprial.daemon.impl.harnesses.protocol.common  import (
    ConnectorOptions,
    HarnessStartError,
    PtyHarnessProcess,
    summarize_stderr,
)
from hyprial.daemon.impl.harnesses.streaming.protocol  import (
    ProgressObservation,
)
from hyprial.daemon.impl.harnesses.model_provider  import codex_provider_configuration
from hyprial.kernel import create_owned_process_group, is_windows_owned_process_group
from hyprial.kernel import (
    PROCESS_FORCE_KILL_SECONDS,
    OwnedProcessGroup,
    darwin_group_has_live_members,
    linux_group_has_live_members,
    parse_linux_process_stat,
    process_birth_identity,
)

STREAM_LIMIT_BYTES = 8 * 1024 * 1024

STDERR_TAIL_BYTES = 16 * 1024

MAX_PENDING_REQUESTS = 1024

PROCESS_EXIT_GRACE_SECONDS = 0.5

PROCESS_GROUP_TERM_SECONDS = 0.5

PROCESS_GROUP_KILL_SECONDS = 0.5

PROCESS_IO_DRAIN_SECONDS = 0.25

PROCESS_STOP_TIMEOUT_SECONDS = 3.0

PROCESS_FORCE_JOIN_SECONDS = 1.0


class CodexExecutableResolutionError(ValueError):
    """The executable selected for a Codex launch cannot be resolved."""


def resolve_codex_executable(
    environment: Mapping[str, str], executable: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve the exact executable a Codex launch must use."""

    selected = environment.get("HARNESS_CODEX_BIN") or os.fspath(
        executable if executable is not None else "codex"
    )
    found = shutil.which(selected, path=environment.get("PATH"))
    if found is None:
        raise CodexExecutableResolutionError(
            f"Codex executable {selected!r} cannot be resolved; arg0 aliases are refused"
        )
    try:
        return Path(found).resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise CodexExecutableResolutionError(
            f"Codex executable {selected!r} cannot be resolved; arg0 aliases are refused"
        ) from error


# The published ``@openai/codex`` npm package ships a ``bin/codex.js`` shim
# that spawns a vendored native binary. Its own ``process.platform`` / ``arch``
# switch selects the installed platform package and the vendor target triple;
# mirror that table so the daemon accepts the binary the shim actually runs.
_CODEX_NPM_TARGETS: dict[tuple[str, str], tuple[str, str]] = {
    ("darwin", "arm64"): ("@openai/codex-darwin-arm64", "aarch64-apple-darwin"),
    ("darwin", "x64"): ("@openai/codex-darwin-x64", "x86_64-apple-darwin"),
    ("linux", "arm64"): ("@openai/codex-linux-arm64", "aarch64-unknown-linux-musl"),
    ("linux", "x64"): ("@openai/codex-linux-x64", "x86_64-unknown-linux-musl"),
    ("win32", "arm64"): ("@openai/codex-win32-arm64", "aarch64-pc-windows-msvc"),
    ("win32", "x64"): ("@openai/codex-win32-x64", "x86_64-pc-windows-msvc"),
}


def _npm_platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "win32"
    return sys.platform


def _npm_arch() -> str:
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def _node_module_lookup_paths(start: Path) -> list[Path]:
    """Mirror Node's ``node_modules`` lookup order for a module at ``start``."""

    paths: list[Path] = []
    for directory in (start, *start.parents):
        paths.append(directory if directory.name == "node_modules" else directory / "node_modules")
    return paths


def _resolve_node_package(start: Path, package_name: str) -> Path | None:
    """Locate the package directory Node's ``node_modules`` lookup reads first.

    Mirrors the lookup order only; whether Node can actually resolve a
    subpath inside the directory is decided by the caller, because the
    package ``exports`` gate can still refuse it.
    """

    for lookup in _node_module_lookup_paths(start):
        candidate = lookup.joinpath(*package_name.split("/"))
        if (candidate / "package.json").is_file():
            return candidate
    return None


def _node_package_json_export_state(manifest_path: Path) -> bool | None:
    """Mirror whether Node resolves ``<platform package>/package.json``.

    The npm shim picks its vendor root once, through
    ``require.resolve(`${platformPackage}/package.json`)``: a resolvable
    platform package uses that package's vendor directory, otherwise the main
    package's. ``require.resolve`` obeys the ``exports`` gate, so a
    ``package.json`` file on disk is not proof of resolution.

    Returns ``True`` when Node would resolve the subpath, ``False`` when it
    definitively cannot, and ``None`` when this mirror cannot decide. Callers
    must treat ``None`` as "no vendor target at all".
    """

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        # Node rejects the unreadable package config and the shim falls back.
        return False
    if not isinstance(manifest, dict):
        return None
    if "exports" not in manifest:
        # Legacy resolution exposes every package subpath.
        return True
    exports = manifest["exports"]
    if not isinstance(exports, dict) or not exports:
        # String, list, empty and condition-only forms export the root only.
        return False
    keys = [key for key in exports if isinstance(key, str)]
    if len(keys) != len(exports) or not all(key.startswith(".") for key in keys):
        # Condition keys and mixed keys cannot export ``./package.json``;
        # Node reports the subpath as not exported.
        return False
    if "./package.json" in exports:
        value = exports["./package.json"]
        if value is None:
            return False
        return True if value == "./package.json" else None
    if any("*" in key for key in keys):
        # A pattern may remap the subpath; do not mirror it by guesswork.
        return None
    return False


def _npm_shim_vendor_executable(shim: Path) -> Path | None:
    """Resolve the native binary an ``@openai/codex`` npm shim spawns.

    Returns ``None`` for anything that is not the published shim laid out as
    Node resolves it, or when the vendored binary cannot be found. Callers
    must treat ``None`` as "no extra target": the alias stays refused.
    """

    package_root = shim.parent.parent
    try:
        manifest = json.loads(
            (package_root / "package.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict) or manifest.get("name") != "@openai/codex":
        return None
    declared = manifest.get("bin")
    if not isinstance(declared, dict):
        return None
    declared_entry = declared.get("codex")
    if not isinstance(declared_entry, str):
        return None
    try:
        shim_relative = shim.relative_to(package_root).as_posix()
    except ValueError:
        return None
    if declared_entry.removeprefix("./") != shim_relative:
        return None
    target = _CODEX_NPM_TARGETS.get((_npm_platform(), _npm_arch()))
    if target is None:
        return None
    package_name, triple = target
    optional = manifest.get("optionalDependencies")
    if not isinstance(optional, dict) or package_name not in optional:
        return None
    executable_name = "codex.exe" if sys.platform == "win32" else "codex"
    # The shim resolves the platform package's ``package.json`` once and uses
    # that vendor root; only when the resolution fails does it use the main
    # package's vendor. A resolved platform package with a missing binary is
    # an error, not a fallback, so a single vendor root is inspected.
    vendor_root = package_root / "vendor"
    platform_package = _resolve_node_package(shim.parent, package_name)
    if platform_package is not None:
        resolution = _node_package_json_export_state(
            platform_package / "package.json"
        )
        if resolution is None:
            # Node's resolution cannot be mirrored: accept only ``launched()``.
            return None
        if resolution:
            vendor_root = platform_package / "vendor"
    candidate = vendor_root / triple / "bin" / executable_name
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_file() else None


def codex_launch_executables(launched: Path) -> frozenset[Path]:
    """Return every executable a Codex launch may legitimately execute.

    A native ``launched`` binary stands alone. When ``launched`` is the
    ``bin/codex.js`` shim of an npm ``@openai/codex`` install, add the vendored
    native binary the shim spawns. An unresolvable shim adds nothing, so an
    arg0 alias must still point at ``launched`` exactly.
    """

    targets = {launched}
    vendor = _npm_shim_vendor_executable(launched)
    if vendor is not None:
        targets.add(vendor)
    return frozenset(targets)

class CodexAppServerRpcError(RuntimeError):
    """An error response returned by Codex app-server."""

    def __init__(
        self, message: str, *, code: int | None = None, data: object = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.data = data

@dataclass(slots=True)
class _CodexSession:
    thread_id: str | None = None
    server_request_methods: list[str] | None = None

@dataclass(frozen=True, slots=True)
class _CodexTurnOutcome:
    result: str
    is_error: bool = False

def _bounded(text: object, *, fallback: str) -> str:
    value = str(text).strip() if text is not None else ""
    if not value:
        value = fallback
    return value if len(value) <= 200 else f"{value[:197]}..."

def _notification_turn_id(params: dict[str, object]) -> str | None:
    value = params.get("turnId")
    if isinstance(value, str):
        return value
    turn = params.get("turn")
    if isinstance(turn, dict) and isinstance(turn.get("id"), str):
        return turn["id"]
    return None

def _codex_item_summary(item: dict[str, object], *, started: bool) -> tuple[str, str | None, dict[str, object]]:
    item_type = _bounded(item.get("type"), fallback="item")
    tool_name: str | None = None
    if item_type == "mcpToolCall":
        server = item.get("server")
        tool = item.get("tool")
        tool_name = ".".join(
            part
            for part in (
                server if isinstance(server, str) else "",
                tool if isinstance(tool, str) else "",
            )
            if part
        ) or item_type
    elif item_type in {"dynamicToolCall", "collabAgentToolCall"}:
        tool = item.get("tool")
        tool_name = tool if isinstance(tool, str) and tool else item_type
    elif item_type in {"commandExecution", "webSearch"}:
        tool_name = item_type

    if started:
        if tool_name is not None:
            summary = f"calling {tool_name}"
        elif item_type == "reasoning":
            summary = "reasoning started"
        elif item_type == "contextCompaction":
            summary = "context compaction started"
        else:
            summary = f"{item_type} started"
    else:
        status = item.get("status")
        suffix = f" ({status})" if isinstance(status, str) and status else ""
        if tool_name is not None:
            summary = f"{tool_name} finished{suffix}"
        elif item_type == "reasoning":
            summary = "reasoning finished"
        elif item_type == "contextCompaction":
            summary = "context compaction finished"
        else:
            summary = f"{item_type} finished{suffix}"

    detail: dict[str, object] = {"itemType": item_type}
    for key in ("status", "durationMs", "exitCode"):
        value = item.get(key)
        if isinstance(value, str | int):
            detail[key] = value
    return _bounded(summary, fallback=item_type), tool_name, detail

def _codex_progress_observation(
    message: dict[str, object], *, thread_id: str, turn_id: str
) -> ProgressObservation | None:
    """Map Codex lifecycle notifications; every delta stream is out of scope."""

    method = message.get("method")
    params = message.get("params")
    if not isinstance(method, str) or not isinstance(params, dict):
        return None
    if params.get("threadId") != thread_id:
        return None
    if _notification_turn_id(params) != turn_id:
        return None

    if method == "turn/started":
        return ProgressObservation(
            phase="turn-start", summary="Codex turn started"
        )
    if method == "turn/completed":
        turn = params.get("turn")
        status = turn.get("status") if isinstance(turn, dict) else None
        return ProgressObservation(
            phase="turn-end",
            summary=(
                "Codex turn completed"
                if status == "completed"
                else f"Codex turn ended ({_bounded(status, fallback='unknown')})"
            ),
            terminal=True,
        )
    if method == "turn/plan/updated":
        plan = params.get("plan")
        steps = [item for item in plan if isinstance(item, dict)] if isinstance(plan, list) else []
        completed = sum(1 for item in steps if item.get("status") == "completed")
        current = next(
            (
                item.get("step")
                for item in steps
                if item.get("status") == "inProgress"
                and isinstance(item.get("step"), str)
            ),
            None,
        )
        return ProgressObservation(
            phase="message-segment",
            summary=(
                f"plan updated ({completed}/{len(steps)} complete)"
                + (f": {current}" if isinstance(current, str) else "")
            ),
            detail={
                "totalSteps": len(steps),
                "completedSteps": completed,
                **({"currentStep": current} if isinstance(current, str) else {}),
            },
        )
    if method in {"item/started", "item/completed"}:
        item = params.get("item")
        if not isinstance(item, dict):
            return None
        started = method == "item/started"
        summary, tool_name, detail = _codex_item_summary(item, started=started)
        item_type = item.get("type")
        if tool_name is not None:
            phase = "tool-call" if started else "tool-result"
        elif item_type == "reasoning":
            phase = "thinking"
        elif item_type == "contextCompaction":
            phase = "compaction"
        else:
            phase = "message-segment"
        return ProgressObservation(
            phase=phase,
            summary=summary,
            tool_call_id=(
                item.get("id") if isinstance(item.get("id"), str) else None
            ),
            tool_name=tool_name,
            detail=detail,
        )
    # Explicit route-B exclusions include item/agentMessage/delta,
    # item/reasoning/*Delta, item/plan/delta, and command/file output deltas.
    return None

_parse_linux_process_stat = parse_linux_process_stat

_process_birth_identity = process_birth_identity

_linux_group_has_live_members = linux_group_has_live_members

_darwin_group_has_live_members = darwin_group_has_live_members

class _OwnedProcessGroup(OwnedProcessGroup):
    """Compatibility surface for existing Codex callers and test seams."""

    def __init__(self) -> None:
        super().__init__(label="Codex app-server")

    @staticmethod
    def _process_birth_identity(pid: int) -> str | None:
        return _process_birth_identity(pid)

    @staticmethod
    def _linux_group_has_live_members(process_group_id: int) -> bool | None:
        return _linux_group_has_live_members(process_group_id)

    @staticmethod
    def _darwin_group_has_live_members(process_group_id: int) -> bool | None:
        return _darwin_group_has_live_members(process_group_id)

def _managed_process_group() -> OwnedProcessGroup:
    if os.name == "nt":
        return create_owned_process_group(label="Codex app-server")
    return _OwnedProcessGroup()

async def _spawn_managed_codex(command, process_group, **options):
    if os.name == "nt":
        if not is_windows_owned_process_group(process_group):
            raise ConnectionError("Windows Codex requires a Job-owned launch")
        return await process_group.spawn(command, **options)
    return await asyncio.create_subprocess_exec(
        *command, start_new_session=True, **options
    )

#: The ordinary per-request deadline for a managed app-server RPC (initialize,
#: notifications, turn control).  Promoted to a named constant because the
#: startup budget below composes from it.
REQUEST_TIMEOUT_SECONDS_DEFAULT = 15.0

def _thread_id(result: object) -> str:
    thread = result.get("thread") if isinstance(result, dict) else None
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        raise ConnectionError("Codex app-server did not return a thread ID")
    return thread_id

class CodexConnector:
    def __init__(self, options: ConnectorOptions | None = None) -> None:
        self.options = options or ConnectorOptions(("codex",))

    def build_argv(self, spec: HarnessLaunchSpec) -> tuple[str, ...]:
        if spec.harness != "codex":
            raise HarnessStartError(spec.harness, (), "Codex connector mismatch")
        provider_args, _provider_environment = codex_provider_configuration(
            spec,
            whitelist_replacement_environment(
                os.environ, self.options.env or {}
            ),
        )
        model_args = ("--model", spec.model) if spec.model is not None else ()
        return (
            *spec.resolved_command(self.options.command),
            *provider_args,
            *model_args,
            *spec.args,
        )

    def launch(
        self,
        spec: HarnessLaunchSpec,
        *,
        complete_launch: ChildEnvironmentLaunch | None = None,
    ) -> PtyHarnessProcess:
        base = (
            complete_launch.environment.for_exec()
            if complete_launch is not None
            else whitelist_replacement_environment(
                os.environ, self.options.env or {}
            )
        )
        _provider_args, provider_environment = codex_provider_configuration(
            spec, base
        )
        model_args = ("--model", spec.model) if spec.model is not None else ()
        argv = (
            *spec.resolved_command(self.options.command),
            *_provider_args,
            *model_args,
            *spec.args,
        )
        environment = (
            {**base, **provider_environment}
            if complete_launch is not None
            else whitelist_replacement_environment(
                os.environ, self.options.env or {}, provider_environment
            )
        )
        return PtyHarnessProcess.spawn(
            "codex",
            argv,
            cwd=spec.cwd,
            env=environment or None,
            complete_environment=complete_launch is not None,
            startup_probe_seconds=self.options.startup_probe_seconds,
            stop_grace_seconds=self.options.stop_grace_seconds,
        )


"""CodexAppServerClient shutdown and force-close behaviors follow: the
client-side process-lifecycle tail, kept beside the process constants it uses.
"""

class _ClientCloseMixin:
        async def _drain_stderr(self) -> None:
            assert self._process is not None and self._process.stderr is not None
            while chunk := await self._process.stderr.read(4096):
                self._stderr_tail.extend(chunk)
                if len(self._stderr_tail) > STDERR_TAIL_BYTES:
                    del self._stderr_tail[: len(self._stderr_tail) - STDERR_TAIL_BYTES]

        async def _watch_process_exit(self) -> None:
            process = self._process
            if process is None:
                return
            returncode = await process.wait()
            if self._stderr_task is not None:
                await self._stderr_task
            if self._logger is not None:
                self._write_exit_log(process, returncode)

        def _write_exit_log(
            self, process: asyncio.subprocess.Process, returncode: int | None
        ) -> None:
            if self._logger is None or self._exit_logged or returncode is None:
                return
            self._exit_logged = True
            event = "worker.stopped" if self._expected_stop else "worker.exited"
            self._logger.log(
                "info" if self._expected_stop else "error",
                event,
                pid=process.pid,
                returnCode=returncode,
                stderrTail=summarize_stderr(bytes(self._stderr_tail)),
            )

        async def _close_process(self) -> None:
            try:
                await self._close_host_process()
            finally:
                if self._execution_runtime is not None:
                    self._execution_runtime.close()

        async def _close_host_process(self) -> None:
            process = self._process
            if process is None:
                return
            process_group_id = self._process_group_id
            if process.stdin is not None:
                process.stdin.close()
            if process.returncode is None:
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=PROCESS_EXIT_GRACE_SECONDS
                    )
                except TimeoutError:
                    pass
    
            # The app-server owns a new process group.  Always drain that group:
            # the leader may exit cleanly on stdin EOF while a tool subprocess that
            # inherited its pipes remains alive.
            if process_group_id is not None and self._process_group.exists(process_group_id):
                self._process_group.signal(process_group_id, signal.SIGTERM)
                exited = await self._wait_for_process_group_exit(
                    process_group_id, timeout=PROCESS_GROUP_TERM_SECONDS
                )
                if not exited:
                    self._process_group.signal(process_group_id, signal.SIGKILL)
                    await self._wait_for_process_group_exit(
                        process_group_id, timeout=PROCESS_GROUP_KILL_SECONDS
                    )
    
            if process.returncode is None:
                if process_group_id is not None:
                    self._process_group.signal(process_group_id, signal.SIGKILL)
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=PROCESS_GROUP_KILL_SECONDS
                    )
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(
                            process.wait(), timeout=PROCESS_GROUP_KILL_SECONDS
                        )
                    except TimeoutError:
                        pass
    
            io_tasks = [
                task
                for task in (self._reader_task, self._stderr_task, self._exit_task)
                if task is not None
            ]
            if io_tasks:
                _done, pending = await asyncio.wait(
                    io_tasks, timeout=PROCESS_IO_DRAIN_SECONDS
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*io_tasks, return_exceptions=True)
            self._write_exit_log(process, process.returncode)
            self._process = None
            if process_group_id is not None:
                self._process_group.release_if_gone(process_group_id)
            self._process_group_id = None
            self._reader_task = None
            self._stderr_task = None
            self._exit_task = None

        async def _finish_rejected_process(
            self, process: asyncio.subprocess.Process
        ) -> None:
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=PROCESS_FORCE_KILL_SECONDS
                )
            except TimeoutError:
                # Retry the entire PGID, never a leader-only kill.  If signalling
                # remains unavailable, retain the unresolved ownership marker so
                # forced-stop completion fails safe instead of claiming success.
                self._process_group._close_unregistered_group(process.pid)
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=PROCESS_FORCE_KILL_SECONDS
                    )
                except TimeoutError:
                    pass
            self._process_group.release_if_gone(process.pid)

        async def _finish_rejected_stderr(
            self, process: asyncio.subprocess.Process
        ) -> None:
            if process.stderr is None:
                return
            try:
                chunk = await asyncio.wait_for(
                    process.stderr.read(), timeout=PROCESS_IO_DRAIN_SECONDS
                )
            except TimeoutError:
                return
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > STDERR_TAIL_BYTES:
                del self._stderr_tail[: len(self._stderr_tail) - STDERR_TAIL_BYTES]

        async def _wait_for_process_group_exit(
            self, process_group_id: int, *, timeout: float
        ) -> bool:
            deadline = asyncio.get_running_loop().time() + timeout
            while self._process_group.exists(process_group_id):
                if asyncio.get_running_loop().time() >= deadline:
                    return False
                await asyncio.sleep(0.01)
            self._process_group.release_if_gone(process_group_id)
            return True
