"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
from .identity import current_channel_actor
def _safe_tool_error_kind(_entry: dict[str, Any], /, error):
    """Classify nested transport failures without serializing their payloads."""

    pending = [error]
    seen: set[int] = set()
    names: list[str] = []
    rendered: list[str] = []
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        names.append(type(current).__name__)
        rendered.append(str(current).lower())
        nested = getattr(current, "exceptions", ())
        if isinstance(nested, tuple):
            pending.extend(item for item in nested if isinstance(item, BaseException))
        for linked in (current.__cause__, current.__context__):
            if isinstance(linked, BaseException):
                pending.append(linked)

    error_kind = "+".join(dict.fromkeys(names))
    rpc_code = getattr(error, "code", None)
    if isinstance(rpc_code, int):
        error_kind += f":rpc{rpc_code}"
    combined = " ".join(rendered)
    for safe_code in (
        "STALE_SESSION",
        "SESSION_SUPERSEDED",
        "METHOD_NOT_FOUND",
        "INVALID_SESSION_SOURCE",
    ):
        if safe_code.lower() in combined:
            error_kind += f":{safe_code}"
            break
    if (
        "connection closed" in combined
        or any(name in {"EndOfStream", "ClosedResourceError"} for name in names)
    ):
        error_kind += ":CONNECTION_CLOSED"
    elif "peer disconnected" in combined:
        error_kind += ":PEER_DISCONNECTED"
    elif any(name == "ExceptionGroup" for name in names):
        error_kind += ":TASK_GROUP"
    return error_kind


def fake_worker(_entry: dict[str, Any], /, name):
    running = True

    def stop(_signum: int, _frame: object) -> None:
        nonlocal running
        running = False

    _entry['signal'].signal(_entry['signal'].SIGTERM, stop)
    _entry['signal'].signal(_entry['signal'].SIGINT, stop)
    ready_fd = _entry['os'].environ.get("HYPRIAL_LARK_READY_FD")
    if ready_fd is not None:
        payload = _entry['json'].dumps({"status": "online", "name": name}) + "\n"
        _entry['os'].write(int(ready_fd), payload.encode("utf-8"))
    while running:
        _entry['time'].sleep(0.05)
    return 0


async def _run_stdio_client(_entry: dict[str, Any], /, *, command, args, child_env, ready, stop, phase, facts):
    """Hold one real installed stdio Channel open without a Claude account."""

    import anyio
    from mcp import StdioServerParameters
    from mcp.client.session import ClientSession
    from mcp.client import stdio as mcp_stdio

    parameters = StdioServerParameters(command=command, args=args, env=child_env)
    child: object | None = None
    original_create = mcp_stdio._create_platform_compatible_process

    async def capture_child(**kwargs: object) -> object:
        nonlocal child
        child = await original_create(**kwargs)
        print("channel-probe-stage child-started", file=_entry['sys'].stderr, flush=True)
        return child

    def child_facts() -> dict[str, object]:
        return {
            "childPid": getattr(child, "pid", None),
            "childReturnCode": getattr(child, "returncode", None),
            "phase": _entry['_read_phase'](phase),
        }

    mcp_stdio._create_platform_compatible_process = capture_child
    successful_calls = 0
    last_tool_error_kind: str | None = None
    last_readiness_shape: tuple[bool, bool, bool, bool] | None = None
    try:
        print("channel-probe-stage stdio-enter", file=_entry['sys'].stderr, flush=True)
        async with (
            mcp_stdio.stdio_client(parameters) as (read_stream, write_stream),
            ClientSession(read_stream, write_stream) as client,
        ):
            print("channel-probe-stage initialize-start", file=_entry['sys'].stderr, flush=True)
            await client.initialize()
            print("channel-probe-stage initialize-done", file=_entry['sys'].stderr, flush=True)
            while stop is None or not stop.exists():
                try:
                    with anyio.fail_after(5.0):
                        response = await client.call_tool("harness_whoami", {})
                except Exception as error:  # noqa: BLE001 - expected outage seam
                    # Never serialize exception text: it may contain transport data.
                    error_kind = _entry['_safe_tool_error_kind'](error)
                    _entry['_merge_probe_state'](
                        ready,
                        {
                            **child_facts(),
                            "lastToolErrorKind": error_kind,
                        },
                    )
                    if "CONNECTION_CLOSED" in error_kind:
                        raise AssertionError(
                            "CONNECTION_CLOSED "
                            f"phase={_entry['_read_phase'](phase)} "
                            f"childPid={getattr(child, 'pid', None)} "
                            f"childRc={getattr(child, 'returncode', None)}"
                        ) from None
                    if error_kind != last_tool_error_kind:
                        print(
                            f"channel-tool-error kind={error_kind}",
                            file=_entry['sys'].stderr,
                            flush=True,
                        )
                    last_tool_error_kind = error_kind
                else:
                    content = response.structured_content or {}
                    readiness_shape = (
                        response.is_error is not True,
                        content.get("sessionRegistered") is True,
                        content.get("channelCurrentThisGeneration") is True,
                        bool(content.get("channelCurrentEpoch"))
                        and content.get("channelCurrentEpoch")
                        == content.get("daemonEpoch"),
                    )
                    if readiness_shape != last_readiness_shape:
                        print(
                            "channel-readiness-shape "
                            f"toolOk={readiness_shape[0]} "
                            f"registered={readiness_shape[1]} "
                            f"current={readiness_shape[2]} "
                            f"epochMatch={readiness_shape[3]}",
                            file=_entry['sys'].stderr,
                            flush=True,
                        )
                        last_readiness_shape = readiness_shape
                    if all(readiness_shape):
                        successful_calls += 1
                        _entry['_write_probe_state'](
                            ready,
                            {
                                **(facts or {}),
                                **child_facts(),
                                "ready": True,
                                "successfulToolCalls": successful_calls,
                                "daemonEpoch": content.get("daemonEpoch"),
                                "channelCurrentEpoch": content.get(
                                    "channelCurrentEpoch"
                                ),
                                "channelCurrentThisGeneration": True,
                                "lastToolErrorKind": last_tool_error_kind,
                            },
                        )
                        if stop is None:
                            # Fake Claude stays alive for a bounded interval only
                            # after its generated Channel is proven current.
                            await anyio.sleep(1.0)
                            return
                await anyio.sleep(0.05)
    finally:
        mcp_stdio._create_platform_compatible_process = original_create
        if child is not None:
            _entry['_merge_probe_state'](ready, child_facts())


def _write_probe_state(_entry: dict[str, Any], /, path, document):
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(_entry['json'].dumps(document), encoding="utf-8")
    temporary.replace(path)


def _merge_probe_state(_entry: dict[str, Any], /, path, update):
    document: dict[str, object] = {}
    if path.exists():
        loaded = _entry['json'].loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            document = loaded
    _entry['_write_probe_state'](path, {**document, **update})


def _read_phase(_entry: dict[str, Any], /, path):
    try:
        return path.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _set_phase(_entry: dict[str, Any], /, path, phase):
    path.write_text(phase + "\n", encoding="utf-8")


def installed_python(_entry: dict[str, Any], /, executable):
    """Locate the interpreter that owns the package installed beside ``executable``.

    A uv tool install keeps its interpreter at ``<tool>/hyprial/bin/python``;
    the fallback covers other layouts that place ``python`` next to the script.
    """

    candidates = []
    tool_dir = _entry['os'].environ.get("UV_TOOL_DIR")
    if tool_dir:
        candidates.append(_entry['Path'](tool_dir) / "hyprial" / "bin" / "python")
    candidates.append(_entry['Path'](_entry['os'].path.realpath(executable)).with_name("python"))
    tool_python = next((path for path in candidates if path.is_file()), None)
    if tool_python is None:
        raise AssertionError(
            f"cannot locate the installed hyprial python beside {executable}"
        )
    return tool_python


def channel_spawn(_entry: dict[str, Any], /, executable, inner_args):
    """Resolve the installed build's Channel server process entry (R11 seam).

    R11 moved the Channel server out of the CLI tree: candidate builds expose
    ``hyprial.daemon.impl.mcp.entry`` and no ``mcp`` command, while the old
    build this runner keeps loaded across the upgrade only understands
    ``hyprial mcp claude-channel``.  Probe the *installed* interpreter so each
    side of the upgrade is started with the spelling that build supports.
    """

    python = _entry['installed_python'](_entry['Path'](executable))
    probe = _entry['subprocess'].run(
        [
            str(python),
            "-c",
            "import importlib.util\n"
            "print(importlib.util.find_spec('hyprial.daemon.impl.mcp.entry') is not None)",
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if probe.returncode == 0 and probe.stdout.strip() == "True":
        return str(python), [
            "-m",
            "hyprial.daemon.impl.mcp.entry",
            "claude-channel",
            *inner_args,
        ]
    return str(executable), ["mcp", "claude-channel", *inner_args]


def read_target_process_identity(_entry: dict[str, Any], /, executable, pid):
    """Read the owner birth marker with the SAME hyprial build the child runs.

    The marker is re-read and compared by the spawned channel child, so it
    must come from the child's own package: this probe runs the workspace
    code, which may carry a newer marker format than a seeded old install.
    Comparing across marker formats is exactly the upgrade-seam defect this
    scenario must not reintroduce through its own harness.
    """

    tool_python = _entry['installed_python'](_entry['Path'](executable))
    result = _entry['subprocess'].run(
        [
            str(tool_python),
            "-c",
            "import importlib.util, sys\n"
            "if importlib.util.find_spec('hyprial.daemon.interface') is not None:\n"
            " from hyprial.daemon import read_process_identity as _read_process_identity\n"
            "else:\n"
            " from hyprial.mcp.channel import _read_process_identity\n"
            "identity = _read_process_identity(int(sys.argv[1]))\n"
            "print('' if identity is None else identity)",
            str(pid),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    identity = result.stdout.strip()
    if result.returncode != 0 or not identity:
        raise AssertionError(
            "cannot determine isolated Channel owner identity from "
            f"{tool_python} (rc={result.returncode}, "
            f"stderr={result.stderr[-500:]!r})"
        )
    return identity


def channel_client_mode(_entry: dict[str, Any], /, argv):
    """Process mode: use the installed package to own an MCP child across upgrade."""

    import anyio

    executable, actor, session_ref, cwd, ready_raw, stop_raw, phase_raw = argv
    owner_pid = _entry['os'].getpid()
    owner_identity = _entry['read_target_process_identity'](_entry['Path'](executable), owner_pid)
    command, args = _entry['channel_spawn'](executable, [
        "--actor",
        actor,
        "--session-ref",
        session_ref,
        "--cwd",
        cwd,
        "--command",
        "claude",
        "--owner-pid",
        str(owner_pid),
        "--owner-identity",
        owner_identity,
    ])
    anyio.run(
        _entry['partial'](
            _entry['_run_stdio_client'],
            command=command,
            args=args,
            child_env=dict(_entry['os'].environ),
            ready=_entry['Path'](ready_raw),
            stop=_entry['Path'](stop_raw),
            phase=_entry['Path'](phase_raw),
        )
    )
    return 0


def fake_claude_mode(_entry: dict[str, Any], /, argv):
    """Exercise the generated MCP config while keeping all traffic isolated."""

    import anyio

    config_path = _entry['Path'](argv[argv.index("--mcp-config") + 1])
    config = _entry['json'].loads(config_path.read_text(encoding="utf-8"))
    server = config["mcpServers"]["harness-bridge"]
    args = list(server["args"])
    actor = args[args.index("--actor") + 1]
    owner_pid = args[args.index("--owner-pid") + 1]
    facts_path = _entry['Path'](_entry['os'].environ["HYPRIAL_E2E_CHANNEL_FACTS"])
    facts = {
        "actor": actor,
        # #362 F1: the launch persists the BARE name; the daemon mints the
        # canonical agent:<owner>:<node>:<name> from the current owner and node
        # at every connect, so an owner migration can never strand a running
        # session on a frozen URI.  The stability this fence guards therefore
        # lives daemon-side now; the config must carry the bare name and no
        # `agent:` prefix (the golden test on #362 asserts the same).
        "bareActor": actor == _entry['CHANNEL_NAME'] and not actor.startswith("agent:"),
        "ownerPidPresent": bool(owner_pid),
        "ownerPidMatchesLauncher": owner_pid == str(_entry['os'].getppid()),
        "ownerIdentityPresent": bool(args[args.index("--owner-identity") + 1]),
    }
    child_env = {**_entry['os'].environ, **server.get("env", {})}
    anyio.run(
        _entry['partial'](
            _entry['_run_stdio_client'],
            command=str(server["command"]),
            args=args,
            child_env=child_env,
            ready=facts_path,
            stop=None,
            phase=_entry['Path'](_entry['os'].environ["HYPRIAL_E2E_CHANNEL_PHASE"]),
            facts=facts,
        )
    )
    return 0


def wait_for_path(_entry: dict[str, Any], /, path, process, error_log, *, stage, timeout):
    deadline = _entry['time'].monotonic() + timeout
    while _entry['time'].monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise AssertionError(
                f"Channel probe exited during {stage} readiness "
                f"(pid={process.pid}, code={process.returncode}, "
                f"diagnostic={_entry['_channel_diagnostic'](error_log)})"
            )
        _entry['time'].sleep(0.05)
    raise AssertionError(
        f"Channel probe timed out during {stage} readiness "
        f"(pid={process.pid}, code={process.poll()}, "
        f"diagnostic={_entry['_channel_diagnostic'](error_log)})"
    )


def start_channel_client(_entry: dict[str, Any], /, *, executable, env, root, actor, session_ref, phase):
    ready = root / f"ready-{session_ref}.json"
    stop = root / f"stop-{session_ref}"
    error_log = root / f"stderr-{session_ref}.log"
    _entry['_set_phase'](phase, "initial-register")
    with error_log.open("wb") as error_stream:
        process = _entry['subprocess'].Popen(
            [
                _entry['sys'].executable, str(_entry['Path'](_entry['__file__']).resolve()), "--channel-client",
                str(executable), actor, session_ref, str(root), str(ready), str(stop),
                str(phase),
            ],
            env=env,
            stdin=_entry['subprocess'].DEVNULL,
            stdout=error_stream,
            stderr=_entry['subprocess'].STDOUT,
        )
    _entry['wait_for_path'](
        ready, process, error_log, stage=f"stdio-client/{session_ref}"
    )
    return process, stop, error_log, ready


def stop_channel_client(_entry: dict[str, Any], /, process, stop, facts_path, phase_path):
    _entry['_set_phase'](phase_path, "teardown")
    stop.touch()
    try:
        process.wait(timeout=10)
    except _entry['subprocess'].TimeoutExpired:
        process.terminate()
        process.wait(timeout=5)
        raise AssertionError("test-owned Channel probe required forced termination")
    if process.returncode != 0:
        raise AssertionError(
            f"Channel probe teardown failed (pid={process.pid}, code={process.returncode})"
        )
    deadline = _entry['time'].monotonic() + 5.0
    latest: dict[str, object] = {}
    while _entry['time'].monotonic() < deadline:
        latest = _entry['json'].loads(facts_path.read_text(encoding="utf-8"))
        if latest.get("childReturnCode") is not None:
            break
        _entry['time'].sleep(0.05)
    child_rc = latest.get("childReturnCode")
    if child_rc is None:
        # The zombie defect this guard exists for: the MCP SDK's close
        # escalates grace(2s) -> SIGTERM -> SIGKILL and waits for the reap
        # before the wrapper may exit, so a child with no return code after
        # the wrapper returned is a kill-escalation survivor holding an
        # inherited write-end — a leaked process, never acceptable.
        raise AssertionError(
            "stdio child survived the client's kill escalation at teardown "
            f"(pid={latest.get('childPid')})"
        )
    if child_rc != 0:
        # A nonzero code with the wrapper already exited means the SDK's
        # escalation reaped the child: the child's Python teardown completes
        # in milliseconds once stdin closes, but the SDK hardcodes a 2s
        # grace (PROCESS_TERMINATION_TIMEOUT) before SIGTERM and interpreter
        # exit of a fully-imported child under scenario load crosses it.
        # The child is verifiably dead — the no-leak property this guard
        # protects — so a reaped child passes with a note; only a surviving
        # one fails.
        pid = latest.get("childPid")
        if isinstance(pid, int):
            try:
                _entry['os'].kill(pid, 0)
            except ProcessLookupError:
                pass
            else:
                raise AssertionError(
                    f"stdio child still alive after teardown (pid={pid})"
                )
        print(
            f"note: channel child reaped by SDK escalation at teardown "
            f"(pid={latest.get('childPid')}, code={child_rc})",
            flush=True,
        )


def wait_for_channel_child_alive(_entry: dict[str, Any], /, facts_path, process, error_log, *, child_pid, phase, timeout):
    """Require one unchanged stdio child and a live stable verifier owner."""

    deadline = _entry['time'].monotonic() + timeout
    latest: dict[str, object] = {}
    while _entry['time'].monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"Channel probe exited during {phase} "
                f"(pid={process.pid}, code={process.returncode}, "
                f"diagnostic={_entry['_channel_diagnostic'](error_log)})"
            )
        loaded = _entry['json'].loads(facts_path.read_text(encoding="utf-8"))
        latest = loaded if isinstance(loaded, dict) else {}
        if (
            latest.get("phase") == phase
            and latest.get("childPid") == child_pid
            and latest.get("childReturnCode") is None
        ):
            return latest
        _entry['time'].sleep(0.05)
    raise AssertionError(
        f"stdio child continuity not observed during {phase} "
        f"(ownerPid={process.pid}, ownerRc={process.poll()}, expectedChildPid={child_pid}, "
        f"facts={latest}, diagnostic={_entry['_channel_diagnostic'](error_log)})"
    )


def wait_for_channel_current(_entry: dict[str, Any], /, executable, env, *, actor, session_ref, build_version, process, error_log, daemon_epoch, stage, protocol_version, generation, require_operational_liveness, timeout):
    identity = _entry.get('actor_identity')
    expected_actor = actor if identity is None else current_channel_actor(actor, identity)
    deadline = _entry['time'].monotonic() + timeout
    latest: list[object] = []
    while _entry['time'].monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise AssertionError(
                "Channel client exited before current-generation confirmation "
                f"during {stage} (pid={process.pid}, code={process.returncode}, "
                f"diagnostic={_entry['_channel_diagnostic'](error_log)})"
            )
        latest = _entry['ps'](env).get(
            "interactiveSessions", []
        )  # type: ignore[assignment]
        for item in latest:
            if (
                isinstance(item, dict)
                and item.get("actor") == expected_actor
                and item.get("sessionRef") == session_ref
                and item.get("channelBuildVersion") == build_version
                and item.get("channelProtocolVersion") == protocol_version
                and item.get("ownerFence") is True
                and item.get("channelGeneration") == generation
                and item.get("channelCurrentThisGeneration") is True
                and item.get("channelCurrentEpoch") == daemon_epoch
                and (
                    not require_operational_liveness
                    or (
                        item.get("channelRecentlyObserved") is True
                        and item.get("channelAlive") is True
                    )
                )
            ):
                return item
        _entry['time'].sleep(0.1)
    raise AssertionError(
        "Channel did not refresh expected non-secret generation telemetry; "
        f"stage={stage} daemonEpoch={daemon_epoch} "
        f"pid={process.pid if process is not None else None} "
        f"code={process.poll() if process is not None else None} "
        f"diagnostic={_entry['_channel_diagnostic'](error_log)} sessions={latest}"
    )


def _channel_diagnostic(_entry: dict[str, Any], /, path):
    if path is None or not path.exists():
        return "unavailable"
    text = path.read_text(encoding="utf-8", errors="replace")[-2000:]
    return " ".join(text.replace("\x00", "").split()) or "empty"


def wait_for_new_tool_call(_entry: dict[str, Any], /, path, previous, *, daemon_epoch, process, error_log, stage, timeout):
    """Prove the same held-open MCP client reached the current daemon."""

    deadline = _entry['time'].monotonic() + timeout
    while _entry['time'].monotonic() < deadline:
        document = _entry['json'].loads(path.read_text(encoding="utf-8"))
        observed = int(document.get("successfulToolCalls", 0))
        if (
            observed > previous
            and document.get("daemonEpoch") == daemon_epoch
            and document.get("channelCurrentEpoch") == daemon_epoch
            and document.get("channelCurrentThisGeneration") is True
        ):
            return observed
        if process.poll() is not None:
            raise AssertionError(
                f"held-open MCP client exited during {stage} "
                f"(pid={process.pid}, code={process.returncode}, "
                f"document={document}, diagnostic={_entry['_channel_diagnostic'](error_log)})"
            )
        _entry['time'].sleep(0.05)
    raise AssertionError(
        f"held-open MCP client did not prove a current-generation tool call "
        f"during {stage} (daemonEpoch={daemon_epoch}, pid={process.pid}, "
        f"code={process.poll()}, document={document}, "
        f"diagnostic={_entry['_channel_diagnostic'](error_log)})"
    )


def write_fake_claude_launcher(_entry: dict[str, Any], /, path, *, facts, phase):
    path.write_text(
        "#!/bin/sh\n"
        f"export HYPRIAL_E2E_CHANNEL_FACTS='{facts}'\n"
        f"export HYPRIAL_E2E_CHANNEL_PHASE='{phase}'\n"
        f'exec "{_entry['sys'].executable}" "{_entry['Path'](_entry['__file__']).resolve()}" --fake-claude "$@"\n',
        encoding="utf-8",
    )
    path.chmod(0o700)
