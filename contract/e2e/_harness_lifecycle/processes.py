"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def run_cli(_entry: dict[str, Any], /, daemon, *args, timeout):
    try:
        completed = _entry['subprocess'].run(
            [str(daemon.executable), *args],
            env=daemon.env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except _entry['subprocess'].TimeoutExpired as error:
        return 124, None, f"command timed out after {error.timeout}s"
    value: dict[str, Any] | None = None
    try:
        decoded = _entry['json'].loads(completed.stdout)
    except _entry['json'].JSONDecodeError:
        decoded = None
    if isinstance(decoded, dict):
        value = decoded
    diagnostic = completed.stderr or completed.stdout or "no diagnostic output"
    return completed.returncode, value, diagnostic[-4000:]


def start_failure_status(_entry: dict[str, Any], /, diagnostic):
    """Separate missing live prerequisites from Python/CLI contract failures."""

    lowered = diagnostic.lower()
    blocked_markers = (
        "not logged in",
        "authentication",
        "api key",
        "credential",
        "operation not permitted",
        "permission denied",
        "network is unreachable",
        "connection refused",
        "timed out",
    )
    return "BLOCKED" if any(item in lowered for item in blocked_markers) else "FAIL"


def daemon_ipc_response(_entry: dict[str, Any], /, root, method, params):
    """One request over the isolated daemon's own socket (never production's).

    This exposes both success and typed-error frames.  The success wrapper is
    used for ``message.pending.list``; E2E-013 also inspects one intentional
    ``message.reply`` failure to pin the text-only harness_reply boundary.
    """

    frame = {
        "version": 1,
        "id": str(_entry['uuid4']()),
        "method": method,
        "params": params,
    }
    client = _entry['socket'].socket(_entry['socket'].AF_UNIX, _entry['socket'].SOCK_STREAM)
    client.settimeout(15)
    try:
        client.connect(str(root / "state" / "daemon.sock"))
        client.sendall(_entry['json'].dumps(frame, separators=(",", ":")).encode() + b"\n")
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise AssertionError("daemon disconnected during IPC read")
            buffer.extend(chunk)
    finally:
        client.close()
    response = _entry['json'].loads(buffer.partition(b"\n")[0])
    if not isinstance(response, dict):
        raise AssertionError("daemon IPC returned a non-object")
    return response


def daemon_ipc(_entry: dict[str, Any], /, root, method, params):
    response = _entry['daemon_ipc_response'](root, method, params)
    failure = response.get("error")
    if isinstance(failure, dict):
        raise AssertionError(
            f"daemon IPC {method} failed: {failure.get('message')}"
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise AssertionError(f"daemon IPC {method} returned no result")
    return result


def pending_for(_entry: dict[str, Any], /, root, actor):
    value = _entry['daemon_ipc'](root, "message.pending.list", {"actor": actor})
    messages = value.get("messages")
    return [item for item in messages if isinstance(item, dict)] if isinstance(
        messages, list
    ) else []


def daemon_pid(_entry: dict[str, Any], /, daemon):
    value = daemon.ps()
    metadata = value.get("daemon")
    pid = metadata.get("pid") if isinstance(metadata, dict) else None
    if not isinstance(pid, int) or pid <= 0:
        raise AssertionError(f"daemon ps returned invalid pid: {metadata}")
    return pid


def connector_status(_entry: dict[str, Any], /, value, connector_id):
    connectors = value.get("connectors", [])
    if not isinstance(connectors, list):
        return None
    return next(
        (
            item
            for item in connectors
            if isinstance(item, dict) and item.get("id") == connector_id
        ),
        None,
    )


def wait_running(_entry: dict[str, Any], /, daemon, connector_id, timeout):
    return daemon.wait(
        lambda: daemon.ps(),
        lambda value: bool(
            (status := _entry['connector_status'](value, connector_id))
            and status.get("running") is True
        ),
        timeout=timeout,
    )


def process_rows(_entry: dict[str, Any], /):
    completed = _entry['subprocess'].run(
        ["ps", "-ww", "-axo", "pid=,ppid=,command="],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or "ps process inspection failed")
    rows: list[tuple[int, int, str]] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        try:
            rows.append((int(fields[0]), int(fields[1]), fields[2]))
        except ValueError:
            continue
    return rows


def _is_pi_worker_command(_entry: dict[str, Any], /, command):
    return command == "pi" or command.startswith("pi ") or "--mode rpc" in command


def _is_codex_appserver_command(_entry: dict[str, Any], /, command):
    return "codex" in command.lower() and "app-server" in command


def _is_claude_cli_command(_entry: dict[str, Any], /, command):
    head = command.split(maxsplit=1)[0]
    return (
        head.rsplit("/", 1)[-1] == "claude"
        and "--output-format stream-json" in command
        and "--input-format stream-json" in command
    )


def pi_worker_pids(_entry: dict[str, Any], /, owner_pid):
    """PIDs of pi worker children that descend from the isolated daemon.

    Filtering to descendants keeps the check immune to unrelated processes
    the developer's own tools may run (same technique as E2E-012).  The pi
    CLI rewrites its process title to a bare ``pi`` (measured: ``ps -ww``
    shows no argv at all), so the match accepts either the bare title or an
    argv-preserving spelling that still carries ``--mode rpc``.
    """

    return _entry['daemon_descendant_pids'](owner_pid, _entry['_is_pi_worker_command'])


def codex_appserver_pids(_entry: dict[str, Any], /, owner_pid):
    """PIDs of `codex app-server` children owned by the isolated daemon.

    Same filter as E2E-012: descendants of the daemon PID whose command names
    a codex app-server, immune to any unrelated developer-owned instance.
    """

    return _entry['daemon_descendant_pids'](owner_pid, _entry['_is_codex_appserver_command'])


def daemon_descendant_pids(_entry: dict[str, Any], /, owner_pid, matcher):
    """PIDs descending from the isolated daemon whose command satisfies ``matcher``.

    The same PPID-chain walk as ``pi_worker_pids``/``codex_appserver_pids``,
    factored for the additional per-harness probes; descendant filtering keeps
    every probe immune to unrelated developer-owned processes.
    """

    rows = _entry['process_rows']()
    descendants = {owner_pid}
    changed = True
    while changed:
        changed = False
        for pid, parent, _command in rows:
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return tuple(
        sorted(
            pid
            for pid, _parent, command in rows
            if pid in descendants and pid != owner_pid and matcher(command)
        )
    )


def claude_cli_pids(_entry: dict[str, Any], /, owner_pid):
    """PIDs of the Agent SDK's bundled Claude Code CLI child.

    The managed claude worker is a three-deep chain: the daemon spawns a
    ``uv run --isolated`` wrapper, which runs the python SDK worker, and the
    SDK spawns the actual ``claude`` CLI.  Only the CLI child is given the
    spec's cwd (measured: the uv/python wrappers keep the daemon's own cwd),
    so the cwd assertion must target it.  Its argv0 basename is ``claude``,
    which excludes both wrappers (their commands merely *mention*
    ``claude-agent-sdk``) and the worker's
    ``python -m hyprial.daemon.impl.mcp.entry agent-channel``
    python child.
    """

    return _entry['daemon_descendant_pids'](owner_pid, _entry['_is_claude_cli_command'])


def process_command(_entry: dict[str, Any], /, pid):
    """The live command line of ``pid``, or None when the pid is dead."""

    completed = _entry['subprocess'].run(
        ["ps", "-o", "command=", "-p", str(pid)],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    if completed.returncode != 0:
        return None
    command = completed.stdout.strip()
    return command or None


def worker_cwd_checked(_entry: dict[str, Any], /, harness, pid):
    """process_cwd with a pid-reuse guard (run 971).

    The pid comes from the daemon moments before lsof runs; a short-lived
    worker can exit in between and the OS can hand the pid to an UNRELATED
    process — run 971's lsof then reported the CI runner's own hostexecutor
    cwd, and the probe "failed" against a process that was never the
    worker.  Verify the pid is alive and its command still matches the
    harness family before trusting lsof: a mismatch means the PROBE lost
    its subject (BLOCKED via ProbeObservationError), never that the worker
    ran in the wrong directory (FAIL).
    """

    command = _entry['process_command'](pid)
    if command is None:
        raise _entry['ProbeObservationError'](
            f"worker pid={pid} exited before the cwd probe ran — "
            "pid 已易主,探针失效"
        )
    if not _entry['WORKER_CWD_NAME_MATCHERS'][harness](command):
        raise _entry['ProbeObservationError'](
            f"worker pid={pid} was reused by an unrelated process "
            f"({command[:120]!r}) — pid 已易主,探针失效"
        )
    try:
        return _entry['process_cwd'](pid)
    except AssertionError as error:
        raise _entry['ProbeObservationError'](
            f"lsof could not read the cwd of worker pid={pid} after the "
            "liveness check (process may have exited mid-probe) — "
            f"pid 已易主,探针失效: {error}"
        ) from error


def process_cwd(_entry: dict[str, Any], /, pid):
    """The OS-level working directory of ``pid`` via lsof (macOS has no /proc)."""

    completed = _entry['subprocess'].run(
        ["lsof", "-w", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    for line in completed.stdout.splitlines():
        if line.startswith("n"):
            return _entry['Path'](line[1:])
    raise AssertionError(
        f"lsof exposed no cwd for pid {pid} "
        f"(exit={completed.returncode}): {completed.stderr.strip()[-300:]}"
    )


def agent_record(_entry: dict[str, Any], /, daemon, name):
    value = daemon.command("agent", "list", "--json")
    agents = value.get("agents")
    if not isinstance(agents, list):
        raise AssertionError(f"agent list returned no agents array: {value}")
    matches = [
        item
        for item in agents
        if isinstance(item, dict) and item.get("actor") == name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one agent record named {name!r}, found {len(matches)}"
        )
    return matches[0]


def target_status(_entry: dict[str, Any], /, daemon, uri):
    row = _entry['target_row'](daemon, uri)
    return row.get("status") if isinstance(row, dict) else None


def target_row(_entry: dict[str, Any], /, daemon, uri):
    value = daemon.command("message", "targets", "--json")
    targets = value.get("targets", [])
    if not isinstance(targets, list):
        return None
    row = next(
        (
            item
            for item in targets
            if isinstance(item, dict) and item.get("targetUri") == uri
        ),
        None,
    )
    return row if isinstance(row, dict) else None


def desired_entry(_entry: dict[str, Any], /, root, harness, name):
    document = _entry['desired_state_document'](root)
    providers = document.get("providers", [])
    matches = [
        item
        for item in providers
        if isinstance(item, dict)
        and item.get("provider") == harness
        and item.get("name") == name
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected one desired {harness}:{name} entry, found {len(matches)}"
        )
    return matches[0]


def desired_absent(_entry: dict[str, Any], /, root, harness, name):
    """True when schema-v1 desired state carries no entry for this worker."""


    document = _entry['desired_state_document'](root)
    providers = document.get("providers", [])
    return not any(
        isinstance(item, dict)
        and item.get("provider") == harness
        and item.get("name") == name
        for item in providers
    )


def assert_desired_exact(_entry: dict[str, Any], /, root, *, harness, name, nickname, cwd, args, model):
    persisted = _entry['desired_entry'](root, harness, name)
    expected = {
        "provider": harness,
        "name": name,
        "nickname": nickname,
        "cwd": str(cwd.resolve()),
        "headless": True,
        "ownership": "managed",
        "args": list(args),
        "modelProvider": _entry['DEEPSEEK_PROVIDER'],
        "model": model,
    }
    mismatches = {
        key: {"expected": value, "actual": persisted.get(key)}
        for key, value in expected.items()
        if persisted.get(key) != value
    }
    if mismatches:
        raise AssertionError(f"desired-state mismatch: {mismatches}")


def roundtrip(_entry: dict[str, Any], /, daemon, root, *, to, prompt, conversation, timeout):
    """Send one probe, await the correlated reply, ack it, return its text."""

    sent = daemon.command(
        "message",
        "send",
        "--from",
        _entry['DRIVER'],
        "--to",
        to,
        "--conversation",
        conversation,
        "--summary",
        prompt,
        prompt,
        "--json",
    )
    if sent.get("ok") is not True or not isinstance(sent.get("messageId"), str):
        raise AssertionError(f"send was not accepted: {sent}")
    request_id = sent["messageId"]

    def probe() -> dict[str, Any] | None:
        return next(
            (
                item
                for item in _entry['pending_for'](root, _entry['DRIVER'])
                if item.get("conversationId") == conversation
                and item.get("messageId") != request_id
            ),
            None,
        )

    reply = daemon.wait(probe, lambda found: found is not None, timeout=timeout)
    assert reply is not None
    ack = daemon.command(
        "message", "ack", "--from", _entry['DRIVER'], str(reply["messageId"]), "--json"
    )
    if ack.get("acknowledged") is not True:
        raise AssertionError(f"reply acknowledgement failed: {ack}")
    text = reply.get("summary")
    return text if isinstance(text, str) else ""
