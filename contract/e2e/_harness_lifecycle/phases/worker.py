"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def start_worker(_entry: dict[str, Any], /, daemon, report, *, harness, name, nickname, cwd, args, check, model):
    """`hyprial agent start --headless` with E2E-006's BLOCKED/FAIL separation."""

    command = ["agent", "start", harness, "--name", name]
    if nickname is not None:
        command.extend(["--nickname", nickname])
    command.extend(
        [
            "--cwd",
            str(cwd),
            "--headless",
            "--provider",
            _entry['DEEPSEEK_PROVIDER'],
            "--model",
            model,
            "--json",
            "--",
            *args,
        ]
    )
    code, value, diagnostic = _entry['run_cli'](daemon, *command)
    if code != 0 or value is None or value.get("ok") is not True:
        detail = (
            str(value.get("error"))
            if isinstance(value, dict) and value.get("error")
            else diagnostic
        )
        report.add(
            _entry['start_failure_status'](detail),
            check,
            f"start command failed ({code}): {detail}",
        )
        return None
    return value


def run_harness_case(_entry: dict[str, Any], /, daemon, root, case, report):
    prefix = case.harness
    connector_id = f"{case.harness}:{case.name}"

    code, value, diagnostic = _entry['run_cli'](
        daemon,
        "agent",
        "create",
        "--name",
        case.name,
        "--provider",
        _entry['DEEPSEEK_PROVIDER'],
        "--model",
        _entry['DEEPSEEK_MODEL'],
        "--preferred-harness",
        case.harness,
        "--cwd",
        str(root),
        "--json",
    )
    agent_uri: str | None = None
    if code != 0 or value is None or value.get("ok") is not True:
        report.add(
            "FAIL", f"{prefix}.agent-create", f"agent create failed ({code}): {diagnostic}"
        )
    else:
        agent = value.get("agent")
        agent = agent if isinstance(agent, dict) else {}
        expected_model = _entry['DEEPSEEK_MODEL']
        problems = [
            f"{field}={agent.get(field)!r}"
            for field, expectation in (
                ("provider", _entry['DEEPSEEK_PROVIDER']),
                ("model", expected_model),
                ("preferredHarness", case.harness),
            )
            if agent.get(field) != expectation
        ]
        if value.get("created") is not True:
            problems.append(f"created={value.get('created')!r}")
        uri = agent.get("uri")
        if not isinstance(uri, str) or not uri.startswith("agent:"):
            problems.append(f"uri={uri!r}")
        else:
            agent_uri = uri
        if problems:
            report.add(
                "FAIL",
                f"{prefix}.agent-create",
                "record does not echo the creation spec: " + ", ".join(problems),
            )
        else:
            report.add(
                "PASS",
                f"{prefix}.agent-create",
                f"uri={agent_uri} provider={_entry['DEEPSEEK_PROVIDER']} "
                f"model={expected_model} preferredHarness={case.harness}",
            )

    started = _entry['start_worker'](
        daemon,
        report,
        harness=case.harness,
        name=case.name,
        nickname=case.nickname,
        cwd=root,
        args=case.args,
        check=f"{prefix}.launch",
    )
    if started is None:
        report.block(
            tuple(
                f"{prefix}.{check}"
                for check in _entry['HARNESS_CHECKS']
                if check not in {"agent-create", "launch"}
            ),
            "worker launch did not complete",
        )
        return
    report.add("PASS", f"{prefix}.launch", f"id={started.get('id')} managed headless")

    try:
        _entry['wait_running'](daemon, connector_id)
    except (AssertionError, OSError, KeyError) as error:
        report.add("FAIL", f"{prefix}.ps-running", str(error))
    else:
        report.add("PASS", f"{prefix}.ps-running", f"id={connector_id} running=true")

    # OS-level cwd: the worker process must actually run in the configured
    # cwd, not merely record it (lsof, since macOS has no /proc).  Exactly
    # one matching daemon descendant is demanded — phase 1 launches one
    # worker per harness, and the claude probe targets the bundled CLI child
    # (the only process in its chain that is given the spec's cwd).
    try:
        owner = _entry['daemon_pid'](daemon)
        probe = _entry['WORKER_CWD_PID_PROBES'][case.harness]
        pids = daemon.wait(
            lambda: probe(owner), lambda found: len(found) == 1, timeout=45.0
        )
        expected = root.resolve()
        actual = _entry['worker_cwd_checked'](case.harness, pids[0]).resolve()
        if actual != expected:
            raise AssertionError(
                f"worker pid={pids[0]} runs in {actual}, "
                f"but the agent's configured cwd is {expected}"
            )
    except _entry['ProbeObservationError'] as error:
        # 探针失效(pid 易主/lsof 读不到)≠ 被测失败 —— 单独归 BLOCKED。
        report.add("BLOCKED", f"{prefix}.worker-cwd", str(error))
    except (AssertionError, OSError) as error:
        report.add("FAIL", f"{prefix}.worker-cwd", str(error))
    else:
        report.add(
            "PASS",
            f"{prefix}.worker-cwd",
            f"lsof cwd of worker pid={pids[0]} == configured cwd ({expected})",
        )

    if agent_uri is None:
        report.add("BLOCKED", f"{prefix}.registration", "agent URI unknown")
    else:
        try:
            daemon.wait(
                lambda: _entry['target_status'](daemon, agent_uri),
                lambda status: status == "online",
                timeout=20.0,
            )
        except (AssertionError, OSError) as error:
            report.add("FAIL", f"{prefix}.registration", str(error))
        else:
            report.add(
                "PASS", f"{prefix}.registration", f"targets shows {agent_uri} online"
            )

    try:
        _entry['assert_desired_exact'](
            root,
            harness=case.harness,
            name=case.name,
            nickname=case.nickname,
            cwd=root,
            args=case.args,
        )
    except (AssertionError, OSError, _entry['json'].JSONDecodeError) as error:
        report.add("FAIL", f"{prefix}.desired-state", str(error))
    else:
        report.add(
            "PASS",
            f"{prefix}.desired-state",
            f"args={_entry['json'].dumps(list(case.args), ensure_ascii=False)}",
        )

    nonce = f"N{_entry['time'].time_ns()}"
    conversation = f"e2e013-{case.harness}-{nonce}"
    try:
        text = _entry['roundtrip'](
            daemon,
            root,
            to=case.nickname,
            prompt=(
                f"Harness E2E-013 probe {nonce}. "
                f"Reply with exactly {case.marker} and nothing else."
            ),
            conversation=conversation,
            timeout=_entry['REPLY_TIMEOUT'],
        )
        if case.marker not in text:
            raise AssertionError(
                f"correlated reply lacks the demanded marker {case.marker}"
            )
    except (AssertionError, OSError, KeyError) as error:
        report.add("FAIL", f"{prefix}.message-reply", str(error))
    else:
        report.add(
            "PASS",
            f"{prefix}.message-reply",
            f"conversation={conversation} marker={case.marker} acknowledged=true",
        )


def run_skill_probe_phase(_entry: dict[str, Any], /, daemon, root, case, report, token):
    """Behavioral proof that ``--skill`` loads from the given path (pi only).

    pi is the one harness exposing an explicit skill *launch flag*
    (``--skill <path>``); claude discovers skills through settings/cwd
    conventions with no path flag on the CLI, and codex has no skill
    mechanism at all — so this assertion group applies only to pi (the
    scenario document records the reason).  The pi worker was started with
    ``--skill`` pointing at the runner-generated CFGPROBE skill; a reply
    carrying the nonce token proves the file at that path was loaded AND
    changed behavior, not merely that the flag round-tripped into desired
    state.
    """

    nonce = f"N{_entry['time'].time_ns()}"
    try:
        text = _entry['roundtrip'](
            daemon,
            root,
            to=case.nickname,
            prompt=(
                f"CFGPROBE Harness E2E-013 config probe {nonce}. Apply your "
                "loaded skill's CFGPROBE reply protocol exactly, then say OK."
            ),
            conversation=f"e2e013-skill-{nonce}",
            timeout=_entry['REPLY_TIMEOUT'],
        )
        if token not in text:
            raise AssertionError(
                f"CFGPROBE reply does not carry the skill token {token} "
                "(the token exists only inside the runner-generated skill "
                "file, so its absence means the skill did not shape the reply)"
            )
    except (AssertionError, OSError, KeyError) as error:
        report.add("FAIL", "skill.pi-loaded", str(error))
    else:
        placement = (
            "at reply start"
            if text.strip().startswith(token)
            else "in the reply body"
        )
        report.add(
            "PASS",
            "skill.pi-loaded",
            f"CFGPROBE reply carries {token} ({placement}): the skill at the "
            "--skill path was loaded and shaped behavior",
        )
