"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from pathlib import Path
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


def run_lark_phase(_entry: dict[str, Any], /, daemon, root, pinned_case, report, *, chat_id, skip_reply_bridge):
    try:
        code, value, diagnostic = _entry['run_cli'](
            daemon, "adapter", "start", _entry['LARK_APP_NAME'], "--json", timeout=120.0
        )
        if code != 0 or value is None:
            raise AssertionError(f"adapter start failed ({code}): {diagnostic}")

        # The real worker reports `starting` until its WebSocket event stream
        # is connected; poll the status seam instead of trusting the start
        # response (only the fake E2E-007 worker is ready instantly).
        def adapter_status() -> dict[str, Any]:
            value = daemon.command("adapter", "status", _entry['LARK_APP_NAME'], "--json")
            status = value.get("adapter")
            return status if isinstance(status, dict) else {}

        daemon.wait(
            adapter_status,
            lambda status: status.get("status") == "online",
            timeout=90.0,
        )
    except (AssertionError, OSError) as error:
        report.add("BLOCKED", "lark.adapter-online", str(error))
        report.block(_entry['LARK_CHECKS'][1:], "test-App adapter is not online")
        report.block(
            _entry['LARK_BRIDGE_CHECKS'],
            "test-App adapter is not online",
        )
        report.block(
            _entry['LARK_AGENT_ATTACHMENT_CHECKS'],
            "test-App adapter is not online",
        )
        return
    report.add(
        "PASS",
        "lark.adapter-online",
        f"{_entry['LARK_APP_NAME']} worker online over the real event stream",
    )

    pinned_uri = ""
    try:
        pinned_uri = _bind_adapter(
            _entry, daemon, root, pinned_case.name, _entry['LARK_APP_NAME']
        )
        if not pinned_uri.endswith(f":{pinned_case.name}"):
            raise AssertionError(f"binding did not bind the agent: {pinned_uri}")
    except (AssertionError, OSError) as error:
        report.add("FAIL", "lark.pin", str(error))
        report.block(_entry['LARK_CHECKS'][2:], "adapter is not bound")
        report.block(
            _entry['LARK_BRIDGE_CHECKS'],
            "adapter is not bound",
        )
        report.block(
            _entry['LARK_AGENT_ATTACHMENT_CHECKS'],
            "adapter is not bound",
        )
        return
    report.add("PASS", "lark.pin", f"{_entry['LARK_APP_NAME']} -> {pinned_case.name}")

    marker = "E2E013_LARK_OK"
    nonce = f"N{_entry['time'].time_ns()}"
    since_ms = int(_entry['time'].time() * 1000)
    image_source = root / f"e2e013-agent-{nonce}.png"
    file_source = root / f"e2e013-agent-{nonce}.txt"
    image_source.write_bytes(_entry['TINY_PNG'])
    file_source.write_bytes(f"E2E-013 agent file proof {nonce}\n".encode())
    route_target = f"user:{pinned_uri.split(':')[1]}"
    image_command = _entry['shlex'].join(
        [
            str(daemon.executable),
            "message",
            "send",
            "--from",
            pinned_uri,
            "--to",
            route_target,
            "--summary",
            f"E2E013_AGENT_IMAGE {nonce}",
            "--image",
            str(image_source),
            "--json",
            f"E2E013_AGENT_IMAGE {nonce}",
        ]
    )
    file_command = _entry['shlex'].join(
        [
            str(daemon.executable),
            "message",
            "send",
            "--from",
            pinned_uri,
            "--to",
            route_target,
            "--summary",
            f"E2E013_AGENT_FILE {nonce}",
            "--file",
            str(file_source),
            "--json",
            f"E2E013_AGENT_FILE {nonce}",
        ]
    )

    # harness_reply is deliberately text-only this release.  Exercise the
    # public daemon boundary with the pinned agent actor and a real source
    # path: it must reject before pending lookup/ack and must never reinterpret
    # the attachment as a text-only success.
    try:
        rejection = _entry['daemon_ipc_response'](
            root,
            "message.reply",
            {
                "from": pinned_uri,
                "messageId": f"e2e013-resource-boundary-{nonce}",
                "summary": "must not be queued",
                "message": "must not be queued",
                "resourcePaths": [
                    {
                        "path": str(file_source),
                        "kind": "file",
                        "mediaType": "text/plain",
                    }
                ],
            },
        )
        failure = rejection.get("error")
        if not isinstance(failure, dict):
            raise AssertionError(
                "attachment-bearing message.reply was silently accepted"
            )
        if failure.get("code") != "MESSAGE_REPLY_RESOURCES_UNSUPPORTED":
            raise AssertionError(
                f"unexpected error code: {failure.get('code')!r}"
            )
        message = str(failure.get("message") or "")
        if "not acknowledged" not in message or "no reply was queued" not in message:
            raise AssertionError(
                "loud rejection does not state the no-ack/no-queue boundary"
            )
    except (AssertionError, OSError) as error:
        report.add("FAIL", "lark.harness-reply-attachment-rejected", str(error))
    else:
        report.add(
            "PASS",
            "lark.harness-reply-attachment-rejected",
            "message.reply resourcePaths rejected with "
            "MESSAGE_REPLY_RESOURCES_UNSUPPORTED before lookup/ack; no reply queued",
        )

    try:
        if skip_reply_bridge:
            agent_instruction = (
                f"Reply with exactly {marker} {nonce} and nothing else."
            )
        else:
            agent_instruction = (
                "In this turn, use your shell tool to run these two commands "
                "in order and require exit status 0 for each:\n"
                f"1. {image_command}\n"
                f"2. {file_command}\n"
                "After both commands succeed, call harness_reply exactly once "
                f"with the text {marker} {nonce}. Do not call harness_reply "
                "before both sends succeed."
            )
        message_id = _entry['lark_user_send'](
            chat_id,
            (
                f"Harness E2E-013 Lark probe {nonce}. "
                f"{agent_instruction}"
            ),
            f"e2e013-lark-{nonce}",
        )
    except (AssertionError, OSError) as error:
        report.add("BLOCKED", "lark.inbound-onit", str(error))
        report.block(_entry['LARK_CHECKS'][3:], "user DM was not sent")
        report.block(
            _entry['LARK_BRIDGE_CHECKS'],
            "user DM was not sent",
        )
        report.block(
            _entry['LARK_AGENT_ATTACHMENT_CHECKS'][:2],
            "user DM was not sent",
        )
        return

    try:
        daemon.wait(
            lambda: _entry['onit_reactions'](message_id),
            lambda found: len(found) > 0,
            timeout=_entry['ONIT_TIMEOUT'],
        )
    except _entry['ReadbackExhausted'] as error:
        # 发送侧有 receipt(平台已受理 DM 并给了 message_id),回读连续失败
        # 是观测层问题,不是 custody 未被证明 —— 归 BLOCKED。
        report.add(
            "BLOCKED",
            "lark.inbound-onit",
            "send-side receipt exists (platform accepted the DM as "
            f"{message_id}) but the OnIt read-back failed every retry — "
            "observation-layer failure, custody unproven either way: "
            f"{error}",
        )
    except (AssertionError, OSError) as error:
        report.add(
            "FAIL",
            "lark.inbound-onit",
            f"inbound DM never gained the OnIt custody reaction: {error}",
        )
    else:
        report.add(
            "PASS",
            "lark.inbound-onit",
            "inbound user DM gained OnIt: adapter custody + forward proven",
        )

    # "The pinned agent received the DM" is proven by the worker's OWN
    # session transcript carrying the probe nonce (read-only over the real
    # pi sessions directory, content-matched — the same technique E2E-012
    # uses on Codex rollouts).  The reply side is asserted separately by the
    # bridge group per the PR #110 contract (docs/reference/p3-mcp.md, "Correlated
    # Lark reply bridge").
    inbound_queued = False

    def pinned_worker_saw_probe() -> Path | None:
        nonlocal inbound_queued
        if not inbound_queued:
            try:
                inbound_queued = any(
                    isinstance(item.get("summary"), str)
                    and nonce in item["summary"]
                    for item in _entry['pending_for'](root, pinned_uri)
                )
            except (AssertionError, OSError):
                pass
        for path in sorted(_entry['pi_sessions_root']().rglob("*.jsonl")):
            try:
                if path.stat().st_mtime < since_ms / 1000 - 5:
                    continue
                if nonce in path.read_text(encoding="utf-8", errors="replace"):
                    return path
            except OSError:
                continue
        return None

    transcript: Path | None = None
    try:
        transcript = daemon.wait(
            pinned_worker_saw_probe,
            lambda found: found is not None,
            timeout=_entry['LARK_REPLY_TIMEOUT'],
        )
    except (AssertionError, OSError) as error:
        report.add(
            "FAIL",
            "lark.pinned-agent-received",
            "the probe nonce never appeared in the pinned worker's session "
            f"transcript (inbound queued for the agent: {inbound_queued}): "
            f"{error}",
        )
    else:
        assert transcript is not None
        report.add(
            "PASS",
            "lark.pinned-agent-received",
            "pinned agent received the DM: probe nonce in the worker's own "
            f"session transcript {transcript.name} "
            f"(inbound also seen queued under the agent URI: {inbound_queued})",
        )

    if skip_reply_bridge:
        report.block(
            _entry['LARK_BRIDGE_CHECKS'] + _entry['LARK_AGENT_ATTACHMENT_CHECKS'][:2],
            "skipped by --skip-reply-bridge (only meaningful for checkouts "
            "predating PR #110; the full agent-turn attachment step requires "
            "the landed reply bridge)",
        )
        return

    # The two platform messages must be produced by the pinned agent's shell
    # commands in this same turn, then be visible and byte-identical through
    # the independent user identity.  The source names/bytes carry this run's
    # nonce, preventing stale messages from satisfying the assertions.
    for kind, source, file_name, check in (
        ("image", image_source, None, "lark.agent-image-outbound"),
        ("file", file_source, file_source.name, "lark.agent-file-outbound"),
    ):
        try:
            found = daemon.wait(
                lambda kind=kind, file_name=file_name: _entry['bot_resource_message'](
                    chat_id,
                    kind,
                    since_ms,
                    file_name=file_name,
                    route_transcript=transcript,
                    route_source=source,
                ),
                lambda value: value is not None,
                timeout=_entry['LARK_REPLY_TIMEOUT'],
            )
            assert found is not None
            message, resource_key = found
            native_id = _entry['assert_downloaded_resource'](
                root,
                message=message,
                resource_key=resource_key,
                kind=kind,
                source=source,
            )
            if transcript is None:
                raise AssertionError("pinned agent transcript was not located")
            # The platform message can become visible before pi flushes the
            # corresponding shell toolResult to its JSONL transcript.  Wait
            # for all three evidence fields instead of racing that write.
            daemon.wait(
                lambda: _entry['transcript_has_route_delivery'](
                    transcript,
                    source=source,
                    native_message_id=native_id,
                ),
                lambda ready: ready,
                timeout=_entry['LARK_REPLY_TIMEOUT'],
            )
        except _entry['ReadbackExhausted'] as error:
            if _entry['attachment_send_receipt'](transcript, source):
                # 发送有收据(transcript 里已有本轮 resourceDeliveries),
                # 回读连续失败 = 观测层问题,不能报成"没送到"。
                report.add(
                    "BLOCKED",
                    check,
                    "send receipt present (agent transcript carries this "
                    f"run's resourceDeliveries for {source.name}) but the "
                    "Lark read-back failed every retry — observation-layer "
                    f"failure, delivery unproven: {error}",
                )
            else:
                report.add(
                    "FAIL",
                    check,
                    "no send-side receipt AND the Lark read-back failed "
                    f"every retry — the {kind} may genuinely not have been "
                    f"delivered: {error}",
                )
        except (AssertionError, OSError) as error:
            report.add(
                "FAIL",
                check,
                f"agent {kind} send did not reach the user byte-identically: {error}",
            )
        else:
            report.add(
                "PASS",
                check,
                f"native={native_id} file={source.name} size={source.stat().st_size} "
                f"sha256={_entry['hashlib'].sha256(source.read_bytes()).hexdigest()[:12]} "
                f"user_visible_type={kind} content_equal=true",
            )

    # PR #110 contract, first observable: the pinned worker's harness_reply
    # is bridged through the supervised adapter worker into a native Lark
    # reply correlated to the original inbound message.
    try:
        native = daemon.wait(
            lambda: _entry['bot_reply_with_marker'](chat_id, nonce, since_ms),
            lambda found: found is not None,
            timeout=_entry['LARK_REPLY_TIMEOUT'],
        )
    except _entry['ReadbackExhausted'] as error:
        if _entry['reply_send_receipt'](transcript, marker):
            # worker 已产出回复(transcript 里有带 marker 的 harness_reply),
            # 回读连续失败 = 观测层问题,不能报成"没送到"。
            report.add(
                "BLOCKED",
                "lark.native-reply",
                "worker reply receipt present (harness_reply with the "
                "marker in the pinned transcript) but the native read-back "
                "failed every retry — observation-layer failure, native "
                f"delivery unproven: {error}",
            )
        else:
            report.add(
                "FAIL",
                "lark.native-reply",
                "no send-side receipt (worker harness_reply not observed) "
                "AND the native read-back failed every retry — the reply "
                f"may genuinely be missing: {error}",
            )
        report.add(
            "BLOCKED",
            "lark.reply-terminal-state",
            "no native reply was observed",
        )
        return
    except (AssertionError, OSError) as error:
        report.add(
            "FAIL",
            "lark.native-reply",
            f"no native bot reply carrying the marker reached the user: {error}",
        )
        report.add(
            "BLOCKED",
            "lark.reply-terminal-state",
            "no native reply was observed",
        )
        return
    assert native is not None
    parent = native.get("parent_id") or native.get("root_id")
    if isinstance(parent, str) and parent and parent != message_id:
        report.add(
            "FAIL",
            "lark.native-reply",
            "the bot reply is not correlated to the original inbound "
            f"message ({parent} != {message_id})",
        )
    else:
        correlation = (
            "correlated to the inbound message via parent_id"
            if parent == message_id
            else "listing exposes no parent id (marker+nonce matched)"
        )
        report.add(
            "PASS",
            "lark.native-reply",
            f"user received the agent's reply as a native Lark message; "
            f"{correlation}",
        )

    # PR #110 contract, second observable: the sender-side terminal record
    # is `fetched` with reason ACK_RECEIVED, held by the sender daemon
    # (the adapter worker is a supervised local transport endpoint, never a
    # second inbox).
    reply_conversation = f"lark:{chat_id}:{chat_id}"

    def bridged_terminal() -> dict[str, Any] | None:
        value = daemon.command(
            "message", "status", "--from", pinned_uri, "--json"
        )
        records = value.get("records", [])
        return next(
            (
                item
                for item in records
                if isinstance(item, dict)
                and item.get("recipient") == _entry['LARK_CHANNEL_ACTOR']
                and item.get("conversationId") == reply_conversation
            ),
            None,
        )

    try:
        record = daemon.wait(
            bridged_terminal, lambda found: found is not None, timeout=60.0
        )
        assert record is not None
        problems = []
        if record.get("state") != "fetched":
            problems.append(f"state={record.get('state')!r}")
        if record.get("reason") != "ACK_RECEIVED":
            problems.append(f"reason={record.get('reason')!r}")
        holder = str(record.get("holder") or "")
        if daemon.node_id not in holder:
            problems.append(
                f"holder={holder!r} does not name the sender daemon "
                f"{daemon.node_id!r}"
            )
        if problems:
            raise AssertionError(
                "terminal record does not match the #110 contract: "
                + ", ".join(problems)
            )
    except (AssertionError, OSError) as error:
        report.add("FAIL", "lark.reply-terminal-state", str(error))
    else:
        report.add(
            "PASS",
            "lark.reply-terminal-state",
            "bridged reply recorded terminal fetched/ACK_RECEIVED "
            f"held by the sender daemon ({daemon.node_id})",
        )
