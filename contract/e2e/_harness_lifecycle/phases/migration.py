"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any


def _bind_adapter(_entry, /, daemon, root, name: str, adapter: str) -> str:
    shown = daemon.command("agent", "cell", "show", name, "--json")
    cell = shown.get("cell") if isinstance(shown, dict) else None
    revision = cell.get("revision") if isinstance(cell, dict) else None
    members = cell.get("members") if isinstance(cell.get("members"), list) else []
    params = cell.get("params") if isinstance(cell.get("params"), dict) else {}
    if not isinstance(revision, int):
        raise AssertionError(f"cell show did not answer a revision: {shown}")
    member_data = dict(params.get("cell.members") or {})
    member_data[adapter] = {"adapter": adapter}
    if "cell.entry" not in params and members:
        params["cell.entry"] = members[0]["ref"]
    fragment = root / f"e2e-cell-{name}.json"
    fragment.write_text(
        __import__("json").dumps(
            {
                "members": [*members, {"ref": adapter, "role": "adapter"}],
                "params": {**params, "cell.members": member_data},
            }
        ),
        encoding="utf-8",
    )
    applied = daemon.command(
        "agent", "cell", "apply", name,
        "--expected-revision", str(revision), "--file", str(fragment), "--json",
    )
    if applied.get("ok") is not True:
        raise AssertionError(f"cell apply did not bind the adapter: {applied}")
    listed = daemon.command("adapter", "list", "--json").get("adapters")
    uri = next(
        (
            item.get("agent") for item in listed
            if isinstance(item, dict) and item.get("name") == adapter
        ),
        None,
    ) if isinstance(listed, list) else None
    if not isinstance(uri, str) or not uri:
        raise AssertionError(f"binding is not visible in adapter list: {listed}")
    return uri


def run_morph_phase(_entry: dict[str, Any], /, daemon, root, report):
    code, value, diagnostic = _entry['run_cli'](
        daemon,
        "agent",
        "create",
        "--name",
        _entry['MORPH_NAME'],
        "--provider",
        _entry['DEEPSEEK_PROVIDER'],
        "--model",
        _entry['CLAUDE_MODEL'],
        "--preferred-harness",
        "claude",
        "--cwd",
        str(root),
        "--json",
    )
    if code != 0 or value is None or value.get("ok") is not True:
        report.add(
            "FAIL", "morph.agent-create", f"agent create failed ({code}): {diagnostic}"
        )
        report.block(_entry['MORPH_CHECKS'][1:], "morph agent was not created")
        return
    agent = value.get("agent")
    agent = agent if isinstance(agent, dict) else {}
    morph_uri = str(agent.get("uri"))
    report.add("PASS", "morph.agent-create", f"uri={morph_uri}")

    try:
        bound = _bind_adapter(
            _entry, daemon, root, _entry['MORPH_NAME'], _entry['PIN_PROBE_ADAPTER']
        )
        if bound != morph_uri:
            raise AssertionError(f"binding does not bind the probe adapter: {bound}")
    except (AssertionError, OSError) as error:
        report.add("FAIL", "morph.pin-setup", str(error))
    else:
        report.add(
            "PASS", "morph.pin-setup", f"{_entry['PIN_PROBE_ADAPTER']} -> {morph_uri}"
        )

    started = _entry['start_worker'](
        daemon,
        report,
        harness="claude",
        name=_entry['MORPH_NAME'],
        nickname=None,
        cwd=root,
        args=(),
        check="morph.launch-claude",
    )
    if started is None:
        report.block(_entry['MORPH_CHECKS'][3:], "morph claude leg did not launch")
        return
    if "harnessHandover" in started:
        report.add(
            "FAIL",
            "morph.launch-claude",
            f"first start must not carry a handover: {started.get('harnessHandover')}",
        )
    else:
        try:
            _entry['wait_running'](daemon, f"claude:{_entry['MORPH_NAME']}")
        except (AssertionError, OSError) as error:
            report.add("FAIL", "morph.launch-claude", str(error))
        else:
            report.add(
                "PASS",
                "morph.launch-claude",
                "claude leg running, no handover on first start",
            )

    context_token = f"CTX{_entry['time'].time_ns()}"
    try:
        text = _entry['roundtrip'](
            daemon,
            root,
            to=_entry['MORPH_NAME'],
            prompt=(
                f"Remember this secret token for later: {context_token}. "
                "Reply with exactly E2E013_SEEDED and nothing else."
            ),
            conversation=f"e2e013-morph-seed-{context_token}",
            timeout=_entry['REPLY_TIMEOUT'],
        )
        if "E2E013_SEEDED" not in text:
            raise AssertionError("seed reply lacks the demanded marker E2E013_SEEDED")
    except (AssertionError, OSError, KeyError) as error:
        report.add("FAIL", "morph.context-seed", str(error))
        report.add(
            "BLOCKED", "morph.context-zeroed", "context was never seeded"
        )
        context_token = ""
    else:
        report.add(
            "PASS", "morph.context-seed", "claude leg acknowledged the seeded token"
        )

    before: dict[str, Any] = {}
    try:
        before = _entry['agent_record'](daemon, _entry['MORPH_NAME'])
        if before.get("lastHarness") != "claude":
            raise AssertionError(
                f"lastHarness before the switch is {before.get('lastHarness')!r}, "
                "expected 'claude'"
            )
    except (AssertionError, OSError) as error:
        report.add("FAIL", "morph.record-before-switch", str(error))
    else:
        report.add(
            "PASS",
            "morph.record-before-switch",
            f"lastHarness=claude lastSessionId={before.get('lastSessionId')!r} "
            f"createdAtMs={before.get('createdAtMs')}",
        )

    try:
        downed = daemon.command(
            "agent", "stop", _entry['MORPH_NAME'], "--json", exits=(0,)
        )
        if f"claude:{_entry['MORPH_NAME']}" not in (downed.get("removed") or []):
            raise AssertionError(f"down did not remove the claude leg: {downed}")
    except (AssertionError, OSError) as error:
        report.add("FAIL", "morph.down-claude", str(error))
        report.block(_entry['MORPH_CHECKS'][6:], "claude leg was not stopped")
        return
    report.add("PASS", "morph.down-claude", f"removed claude:{_entry['MORPH_NAME']}")

    switched = _entry['start_worker'](
        daemon,
        report,
        harness="pi",
        name=_entry['MORPH_NAME'],
        nickname=None,
        cwd=root,
        args=("--approve",),
        check="morph.switch-start-pi",
    )
    if switched is None:
        report.block(_entry['MORPH_CHECKS'][7:], "pi leg did not launch")
        return
    try:
        _entry['wait_running'](daemon, f"pi:{_entry['MORPH_NAME']}")
    except (AssertionError, OSError) as error:
        report.add("FAIL", "morph.switch-start-pi", str(error))
    else:
        report.add(
            "PASS",
            "morph.switch-start-pi",
            f"same name restarted on pi ({_entry['PI_PROVIDER']}/{_entry['PI_MODEL']})",
        )

    handover = switched.get("harnessHandover")
    try:
        if not isinstance(handover, dict):
            raise AssertionError("switch response carries no harnessHandover")
        problems = []
        if handover.get("previousHarness") != "claude":
            problems.append(f"previousHarness={handover.get('previousHarness')!r}")
        if "previousSessionId" not in handover:
            problems.append("previousSessionId key missing")
        elif handover.get("previousSessionId") != before.get("lastSessionId"):
            problems.append(
                f"previousSessionId={handover.get('previousSessionId')!r} != "
                f"recorded clue {before.get('lastSessionId')!r}"
            )
        if handover.get("contextCarriedOver") is not False:
            problems.append(
                f"contextCarriedOver={handover.get('contextCarriedOver')!r}"
            )
        notice = handover.get("notice")
        if (
            not isinstance(notice, str)
            or "claude" not in notice
            or "pi" not in notice
        ):
            problems.append("notice does not name both harnesses")
        if problems:
            raise AssertionError("handover mismatch: " + ", ".join(problems))
    except AssertionError as error:
        report.add("FAIL", "morph.handover-notice", str(error))
    else:
        report.add(
            "PASS",
            "morph.handover-notice",
            "previousHarness=claude contextCarriedOver=false "
            f"previousSessionId={handover.get('previousSessionId')!r}",
        )

    try:
        after = _entry['agent_record'](daemon, _entry['MORPH_NAME'])
        problems = []
        if after.get("uri") != before.get("uri"):
            problems.append(f"uri changed: {before.get('uri')} -> {after.get('uri')}")
        if after.get("createdAtMs") != before.get("createdAtMs"):
            problems.append(
                f"createdAtMs changed: {before.get('createdAtMs')} -> "
                f"{after.get('createdAtMs')}"
            )
        if problems:
            raise AssertionError("; ".join(problems))
    except (AssertionError, OSError) as error:
        report.add("FAIL", "morph.same-entity", str(error))
        after = {}
    else:
        report.add(
            "PASS",
            "morph.same-entity",
            "one record; uri and createdAtMs unchanged across the switch",
        )

    try:
        if after.get("lastHarness") != "pi":
            raise AssertionError(
                f"lastHarness after the switch is {after.get('lastHarness')!r}, "
                "expected 'pi'"
            )
    except AssertionError as error:
        report.add("FAIL", "morph.last-harness-updated", str(error))
    else:
        report.add(
            "PASS",
            "morph.last-harness-updated",
            f"lastHarness=pi lastSessionId={after.get('lastSessionId')!r}",
        )

    try:
        listed = daemon.command("adapter", "list", "--json").get("adapters")
        pins = {
            item.get("name"): item.get("agent")
            for item in listed
            if isinstance(item, dict) and isinstance(item.get("agent"), str)
        } if isinstance(listed, list) else {}
        if pins.get(_entry['PIN_PROBE_ADAPTER']) != morph_uri:
            raise AssertionError(
                f"binding no longer binds the same agent URI after the switch: {pins}"
            )
        shown = daemon.command("agent", "cell", "show", _entry['MORPH_NAME'], "--json")
        cell = shown.get("cell") if isinstance(shown, dict) else {}
        members = cell.get("members")
        if (
            not isinstance(members, list)
            or not any(
                isinstance(item, dict)
                and item.get("ref") == _entry['PIN_PROBE_ADAPTER']
                and item.get("role") == "adapter"
                for item in members
            )
        ):
            raise AssertionError(
                f"agent cell lost its adapter member: {members}"
            )
    except (AssertionError, OSError) as error:
        report.add("FAIL", "morph.pin-survives-switch", str(error))
    else:
        report.add(
            "PASS",
            "morph.pin-survives-switch",
            f"{_entry['PIN_PROBE_ADAPTER']} -> {morph_uri} before and after; "
            "pin binds the agent, not the harness",
        )

    if context_token:
        try:
            text = _entry['roundtrip'](
                daemon,
                root,
                to=_entry['MORPH_NAME'],
                prompt=(
                    "Earlier in this conversation you may have been given a "
                    "secret token starting with CTX. If you know that exact "
                    "token, reply with only that token. If you do not know "
                    "it, reply with exactly E2E013_NO_CONTEXT and nothing else."
                ),
                conversation=f"e2e013-morph-zero-{_entry['time'].time_ns()}",
                timeout=_entry['REPLY_TIMEOUT'],
            )
        except (AssertionError, OSError, KeyError) as error:
            report.add("FAIL", "morph.post-switch-roundtrip", str(error))
            report.add(
                "BLOCKED", "morph.context-zeroed", "post-switch roundtrip failed"
            )
        else:
            report.add(
                "PASS",
                "morph.post-switch-roundtrip",
                "correlated reply on the new harness, acknowledged",
            )
            if context_token in text:
                report.add(
                    "FAIL",
                    "morph.context-zeroed",
                    "the pi leg reproduced the claude-seeded token: context "
                    "leaked across the harness switch",
                )
            else:
                report.add(
                    "PASS",
                    "morph.context-zeroed",
                    "seeded token is not reproducible on the new harness",
                )
    else:
        report.add(
            "BLOCKED", "morph.post-switch-roundtrip", "context was never seeded"
        )

    try:
        downed = daemon.command("agent", "stop", _entry['MORPH_NAME'], "--json")
        if f"pi:{_entry['MORPH_NAME']}" not in (downed.get("removed") or []):
            raise AssertionError(f"down did not remove the pi leg: {downed}")
    except (AssertionError, OSError) as error:
        report.add("FAIL", "morph.down-pi", str(error))
    else:
        report.add("PASS", "morph.down-pi", f"removed pi:{_entry['MORPH_NAME']}")


def run_model_switch_phase(_entry: dict[str, Any], /, daemon, root, case, report):
    connector_id = f"{case.harness}:{case.name}"
    alt_args: tuple[str, ...] = ()
    try:
        downed = daemon.command("agent", "stop", case.name, "--json")
        if connector_id not in (downed.get("removed") or []):
            raise AssertionError(f"down did not remove the worker: {downed}")
    except (AssertionError, OSError) as error:
        report.add("FAIL", "model-switch.down", str(error))
        report.block(_entry['MODEL_SWITCH_CHECKS'][1:], "worker was not stopped")
        return
    report.add("PASS", "model-switch.down", f"removed {connector_id}")

    restarted = _entry['start_worker'](
        daemon,
        report,
        harness=case.harness,
        name=case.name,
        nickname=case.nickname,
        cwd=root,
        args=alt_args,
        check="model-switch.restart",
        model=_entry['CLAUDE_MODEL_ALT'],
    )
    if restarted is None:
        report.block(_entry['MODEL_SWITCH_CHECKS'][2:], "worker did not restart")
        return
    try:
        _entry['wait_running'](daemon, connector_id)
    except (AssertionError, OSError) as error:
        report.add("FAIL", "model-switch.restart", str(error))
    else:
        report.add(
            "PASS",
            "model-switch.restart",
            f"same harness restarted with --model {_entry['CLAUDE_MODEL_ALT']}",
        )

    if "harnessHandover" in restarted:
        report.add(
            "FAIL",
            "model-switch.no-handover",
            "a same-harness model switch must not emit an A9 handover: "
            f"{restarted.get('harnessHandover')}",
        )
    else:
        report.add(
            "PASS",
            "model-switch.no-handover",
            "same-harness restart carries no harnessHandover",
        )

    try:
        _entry['assert_desired_exact'](
            root,
            harness=case.harness,
            name=case.name,
            nickname=case.nickname,
            cwd=root,
            args=alt_args,
            model=_entry['CLAUDE_MODEL_ALT'],
        )
    except (AssertionError, OSError, _entry['json'].JSONDecodeError) as error:
        report.add("FAIL", "model-switch.desired-state", str(error))
    else:
        report.add(
            "PASS",
            "model-switch.desired-state",
            f"args={_entry['json'].dumps(list(alt_args), ensure_ascii=False)}",
        )

    nonce = f"N{_entry['time'].time_ns()}"
    try:
        text = _entry['roundtrip'](
            daemon,
            root,
            to=case.nickname,
            prompt=(
                f"Harness E2E-013 model-switch probe {nonce}. "
                "Reply with exactly E2E013_MODEL_SWITCH_OK and nothing else."
            ),
            conversation=f"e2e013-model-switch-{nonce}",
            timeout=_entry['REPLY_TIMEOUT'],
        )
        if "E2E013_MODEL_SWITCH_OK" not in text:
            raise AssertionError("reply lacks the demanded marker")
    except (AssertionError, OSError, KeyError) as error:
        report.add("FAIL", "model-switch.message-reply", str(error))
    else:
        report.add(
            "PASS",
            "model-switch.message-reply",
            f"correlated roundtrip on --model {_entry['CLAUDE_MODEL_ALT']}, acknowledged",
        )
