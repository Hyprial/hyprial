"""E2E-004 flow; explicit callbacks keep entrypoint seams live."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4
from _daemon_runner import short_temp_root
from hyprial.daemon.impl.adapters.lark import (
    ActorTarget,
    LarkSdkGateway,
)
from feishu.content import _lark_message_content, expected_post_text, post_content
from feishu.evidence import (
    ContractBlocked,
    ContractFailure,
    _FailurePreservingTempDir,
    assert_sanitized_evidence,
    epoch_ms,
    native_id_label,
    read_evidence,
    redact_native_ids,
    text_digest,
    utc_now,
)


@dataclass(frozen=True)
class FlowPorts:
    """Only this module's observed collaborators and configuration."""

    ACTOR: ActorTarget
    APP_ID: str
    APP_NAME: str
    ASSERTIONS: tuple[str, ...]
    ATTACHMENT_FILE_NAME: str
    LARK_CLI: Path
    OUTBOUND_SENDER: str
    Reporter: Callable[..., Any]
    TEST_ROUTE_NAME: str
    TINY_PNG: bytes
    assert_live_unsupported_alarm: Callable[..., Any]
    assert_merge_forward_not_dropped: Callable[..., Any]
    assert_provider_failure: Callable[..., Any]
    assert_unsupported_type_is_loud: Callable[..., Any]
    expected_file_text: Callable[..., Any]
    expected_image_text: Callable[..., Any]
    get_message: Callable[..., Any]
    hyprial_cli: Callable[..., Any]
    issue_delivery: Callable[..., Any]
    lark_cli: Callable[..., Any]
    load_approved_target: Callable[..., Any]
    nontext_roundtrip: Callable[..., Any]
    prepare_outbound_daemon: Callable[..., Any]
    resolve_bot_dm_chat_id: Callable[..., Any]
    start_gateway: Callable[..., Any]
    stop_gateway: Callable[..., Any]
    stop_outbound_daemon: Callable[..., Any]
    user_reply: Callable[..., Any]
    user_send: Callable[..., Any]
    user_send_attachment: Callable[..., Any]
    user_send_content: Callable[..., Any]
    verify_user_identity: Callable[..., Any]
    wait_for_evidence: Callable[..., Any]
    wait_for_no_onit: Callable[..., Any]
    wait_for_onit: Callable[..., Any]
    write_command: Callable[..., Any]


def run_contract(*, _ports: FlowPorts) -> int:
    reporter = _ports.Reporter()
    current_assertion = _ports.ASSERTIONS[0]
    process: subprocess.Popen[str] | None = None
    handles: list[Any] = []
    cleanup_message_ids: list[str] = []
    app_secret: str | None = None
    outbound_executable: Path | None = None
    outbound_environment: dict[str, str] | None = None
    try:
        if not _ports.LARK_CLI.is_file() or not os.access(_ports.LARK_CLI, os.X_OK):
            raise ContractBlocked(f"required executable is missing: {_ports.LARK_CLI}")
        target = _ports.load_approved_target(need_identity=True)
        app_secret = target.app_secret
        chat_id = target.chat_id
        identity = _ports.verify_user_identity()
        # Resolved at preflight so a missing DM blocks in the first seconds
        # instead of after nine live assertions have already run.
        target = replace(target, dm_chat_id=_ports.resolve_bot_dm_chat_id())
        dm_chat_id = str(target.dm_chat_id)

        nonce = f"{int(time.time())}-{uuid4().hex[:8]}"
        # A16 nests an isolated daemon below this root.  Keep the base path
        # short enough for macOS's 104-byte AF_UNIX socket limit instead of
        # inheriting the long /private/var/folders/... system temp path.
        with _FailurePreservingTempDir(
            prefix="hyprial-feishu-e2e-004-", dir=short_temp_root()
        ) as temporary:
            root = Path(temporary)
            (root / "home").mkdir()
            (root / "state").mkdir()
            command_dir = root / "commands"
            command_dir.mkdir()
            evidence_path = root / "evidence.ndjson"
            state_path = root / "state" / "lark-state.json"

            process, stdout_handle, stderr_handle = _ports.start_gateway(
                root, state_path, evidence_path, command_dir, nonce, 1, dm_chat_id
            )
            handles.extend((stdout_handle, stderr_handle))
            ready = _ports.wait_for_evidence(
                evidence_path, "gateway.ready", process=process, timeout=30
            )
            reporter.pass_(
                current_assertion,
                f"app={_ports.APP_NAME} route={_ports.TEST_ROUTE_NAME} "
                f"chat={native_id_label(chat_id)} mode={target.chat_mode} "
                f"dm={native_id_label(dm_chat_id)} "
                f"identity_verified={identity['verified']} "
                f"token_status={identity['user.tokenStatus']} "
                f"ready_at={ready['ts']}",
            )

            current_assertion = _ports.ASSERTIONS[1]
            request_text = f"CONTRACT_LARK_REQUEST_{nonce}"
            request_message_id = _ports.user_send(
                chat_id,
                request_text,
                f"e2e004-request-{nonce}"[:50],
                mention=target.mention,
            )
            cleanup_message_ids.append(request_message_id)
            sdk_request = _ports.wait_for_evidence(
                evidence_path,
                "sdk.event",
                lambda row: row.get("message_id") == request_message_id,
                process=process,
            )
            harness_request = _ports.wait_for_evidence(
                evidence_path,
                "harness.request.accepted",
                lambda row: row.get("native_message_id") == request_message_id,
                process=process,
            )
            inbound = _ports.wait_for_evidence(
                evidence_path,
                "adapter.inbound",
                lambda row: row.get("message_id") == request_message_id,
                process=process,
            )
            if (
                sdk_request.get("chat_label") != native_id_label(chat_id)
                or sdk_request.get("chat_type") != target.chat_mode
                or sdk_request.get("message_type") != "text"
                or inbound.get("status") != "forwarded"
                or harness_request.get("target_actor_id") != _ports.ACTOR.actor_id
            ):
                raise ContractFailure(
                    "real user request did not forward to target actor"
                )
            reporter.pass_(
                current_assertion,
                f"native={request_message_id} harness={harness_request['harness_message_id']} "
                f"event={sdk_request['event_id']} received_at={sdk_request['ts']}",
            )

            current_assertion = _ports.ASSERTIONS[2]
            reactions = _ports.wait_for_onit(request_message_id)
            action_times = [
                int(item.get("action_time", "0"))
                for item in reactions
                if str(item.get("action_time", "0")).isdigit()
            ]
            if not action_times or min(action_times) < int(harness_request["ts_ms"]):
                raise ContractFailure("OnIt action_time preceded Harness acceptance")
            reporter.pass_(
                current_assertion,
                f"native={request_message_id} harness_accepted_at={harness_request['ts']} "
                f"onit_action_time_ms={min(action_times)}",
            )

            current_assertion = _ports.ASSERTIONS[3]
            reply_text = f"CONTRACT_LARK_REPLY_{nonce}"
            command_id, delivery_id = _ports.issue_delivery(
                command_dir,
                str(harness_request["harness_message_id"]),
                reply_text,
                f"initial_{nonce}",
            )
            delivery = _ports.wait_for_evidence(
                evidence_path,
                "adapter.delivery",
                lambda row: row.get("command_id") == command_id,
                process=process,
            )
            if delivery.get("status") != "accepted" or not isinstance(
                delivery.get("native_message_id"), str
            ):
                raise ContractFailure(f"adapter reply was not accepted: {delivery}")
            reply_message_id = str(delivery["native_message_id"])
            reply_message = _ports.get_message(reply_message_id)
            content = reply_message.get("content")
            if (
                reply_message.get("reply_to") != request_message_id
                or not isinstance(content, str)
                or f"Agent: {_ports.ACTOR.display_name}" not in content
                or "Replied-At:" not in content
                or f"Reply-To: {request_message_id}" not in content
                or reply_text not in content
            ):
                raise ContractFailure(
                    "bot response was not a native, headed adapter reply"
                )
            accepted = _ports.wait_for_evidence(
                evidence_path,
                "harness.delivery.accepted",
                lambda row: row.get("delivery_id") == delivery_id,
                process=process,
            )
            reporter.pass_(
                current_assertion,
                f"native={reply_message_id} reply_to={request_message_id} "
                f"delivery={delivery_id} accepted_at={accepted['ts']} headers=true",
            )

            current_assertion = _ports.ASSERTIONS[4]
            if delivery.get("cleanup_error") is not None:
                raise ContractFailure(f"adapter reported cleanup error: {delivery}")
            _ports.wait_for_no_onit(request_message_id)
            reporter.pass_(
                current_assertion,
                f"native={request_message_id} cleared_after_delivery={delivery_id}",
            )

            current_assertion = _ports.ASSERTIONS[5]
            followup_text = f"CONTRACT_LARK_FOLLOWUP_{nonce}"
            followup_message_id = _ports.user_reply(
                reply_message_id,
                followup_text,
                f"e2e004-followup-{nonce}"[:50],
                mention=target.mention,
            )
            cleanup_message_ids.append(followup_message_id)
            followup = _ports.wait_for_evidence(
                evidence_path,
                "harness.request.accepted",
                lambda row: row.get("native_message_id") == followup_message_id,
                process=process,
            )
            followup_sdk = _ports.wait_for_evidence(
                evidence_path,
                "sdk.event",
                lambda row: row.get("message_id") == followup_message_id,
                process=process,
            )
            if (
                followup.get("target_actor_id") != _ports.ACTOR.actor_id
                or followup_sdk.get("reply_to") != reply_message_id
            ):
                raise ContractFailure(
                    "native follow-up did not route to original actor"
                )
            # The delivery must not race the OnIt add: the child's
            # handle_delivery clears by LISTING current reactions, so a
            # delivery processed before the child's add_reaction lands
            # clears nothing and never retries (the 2026-08-23 A8 red).
            _ports.wait_for_onit(followup_message_id)
            cleanup_command, _ = _ports.issue_delivery(
                command_dir,
                str(followup["harness_message_id"]),
                f"CONTRACT_LARK_FOLLOWUP_ACK_{nonce}",
                f"followup_{nonce}",
            )
            cleanup_delivery = _ports.wait_for_evidence(
                evidence_path,
                "adapter.delivery",
                lambda row: row.get("command_id") == cleanup_command,
                process=process,
            )
            if cleanup_delivery.get("status") != "accepted":
                raise ContractFailure("follow-up cleanup delivery was not accepted")
            if cleanup_delivery.get("cleanup_error") is not None:
                raise ContractFailure(
                    f"adapter reported cleanup error: {cleanup_delivery}"
                )
            _ports.wait_for_no_onit(followup_message_id)
            reporter.pass_(
                current_assertion,
                f"native={followup_message_id} reply_to={reply_message_id} "
                f"harness={followup['harness_message_id']} actor={_ports.ACTOR.actor_id}",
            )

            current_assertion = _ports.ASSERTIONS[6]
            before = len(
                [
                    row
                    for row in read_evidence(evidence_path)
                    if row.get("kind") == "harness.request.accepted"
                    and row.get("native_message_id") == request_message_id
                ]
            )
            replay_command = _ports.write_command(
                command_dir,
                "replay",
                native_message_id=request_message_id,
            )
            replay = _ports.wait_for_evidence(
                evidence_path,
                "adapter.replay",
                lambda row: row.get("command_id") == replay_command,
                process=process,
            )
            after = len(
                [
                    row
                    for row in read_evidence(evidence_path)
                    if row.get("kind") == "harness.request.accepted"
                    and row.get("native_message_id") == request_message_id
                ]
            )
            evidence_text = evidence_path.read_text(encoding="utf-8")
            if (
                replay.get("status") != "duplicate"
                or before != after
                or request_text in evidence_text
            ):
                raise ContractFailure("event replay was not silent and plaintext-free")
            reporter.pass_(
                current_assertion,
                f"event={sdk_request['event_id']} status=duplicate harness_count={after} "
                f"plaintext_logged=false",
            )

            current_assertion = _ports.ASSERTIONS[7]
            _ports.stop_gateway(process)
            process = None
            for handle in handles:
                handle.close()
            handles.clear()
            process, stdout_handle, stderr_handle = _ports.start_gateway(
                root, state_path, evidence_path, command_dir, nonce, 2, dm_chat_id
            )
            handles.extend((stdout_handle, stderr_handle))
            restart_started = epoch_ms()
            restarted = _ports.wait_for_evidence(
                evidence_path,
                "gateway.ready",
                lambda row: int(row.get("ts_ms", 0)) >= restart_started,
                process=process,
                timeout=30,
            )
            restart_text = f"CONTRACT_LARK_RESTART_FOLLOWUP_{nonce}"
            restart_message_id = _ports.user_reply(
                reply_message_id,
                restart_text,
                f"e2e004-restart-{nonce}"[:50],
                mention=target.mention,
            )
            cleanup_message_ids.append(restart_message_id)
            restart_sdk = _ports.wait_for_evidence(
                evidence_path,
                "sdk.event",
                lambda row: row.get("message_id") == restart_message_id,
                process=process,
            )
            restart_request = _ports.wait_for_evidence(
                evidence_path,
                "harness.request.accepted",
                lambda row: row.get("native_message_id") == restart_message_id,
                process=process,
            )
            if (
                restart_sdk.get("reply_to") != reply_message_id
                or restart_request.get("target_actor_id") != _ports.ACTOR.actor_id
            ):
                raise ContractFailure(
                    "persisted reply route failed after gateway restart"
                )
            # Same clear-before-add race guard as the A6 follow-up above —
            # post-restart the child's first app API calls also pay the cold
            # tenant-token fetch, which is exactly when the add lags behind.
            _ports.wait_for_onit(restart_message_id)
            restart_cleanup, _ = _ports.issue_delivery(
                command_dir,
                str(restart_request["harness_message_id"]),
                f"CONTRACT_LARK_RESTART_ACK_{nonce}",
                f"restart_{nonce}",
            )
            restart_delivery = _ports.wait_for_evidence(
                evidence_path,
                "adapter.delivery",
                lambda row: row.get("command_id") == restart_cleanup,
                process=process,
            )
            if restart_delivery.get("status") != "accepted":
                raise ContractFailure(
                    "restart follow-up cleanup delivery was not accepted"
                )
            if restart_delivery.get("cleanup_error") is not None:
                raise ContractFailure(
                    f"adapter reported cleanup error: {restart_delivery}"
                )
            _ports.wait_for_no_onit(restart_message_id)
            reporter.pass_(
                current_assertion,
                f"gateway_ready={restarted['ts']} native={restart_message_id} "
                f"reply_to={reply_message_id} actor={_ports.ACTOR.actor_id}",
            )

            current_assertion = _ports.ASSERTIONS[8]
            negative = _ports.assert_provider_failure(root, chat_id)
            if (
                negative["status"] != "rejected"
                or negative["accepted"]
                or len(negative["rejected"]) != 1
                or not negative["reaction_retained"]
                or negative["clear_calls"] != 0
            ):
                raise ContractFailure(f"provider failure ordering violated: {negative}")
            reporter.pass_(
                current_assertion,
                "delivery_rejected=true harness_acked=false onit_retained=true clear_calls=0",
            )

            # --- Non-text coverage -------------------------------------------
            # A text-only contract cannot see the 2026-08-13 P0: the adapter
            # dropped every non-text message while text kept working.  The
            # shared group cannot host these assertions -- it only delivers
            # messages that @mention the bot, and image/file messages cannot
            # carry a mention -- so they run in the bot's own DM.
            current_assertion = "A11-richtext-post-roundtrip"
            group_post_message_id = _ports.user_send_content(
                chat_id,
                "post",
                post_content(nonce, target.bot_open_id),
                f"e2e004-gpost-{nonce}"[:50],
            )
            cleanup_message_ids.append(group_post_message_id)
            # In the group the post carries an @mention whose rendered form
            # depends on the bot's platform display name, so this half asserts
            # structurally: the mention path must not swallow the body.
            group_post = _ports.nontext_roundtrip(
                label="grouppost",
                message_id=group_post_message_id,
                expected_message_type="post",
                expected_text=None,
                minimum_text_length=len(expected_post_text(nonce)),
                chat_id=chat_id,
                chat_type=target.chat_mode,
                nonce=nonce,
                command_dir=command_dir,
                evidence_path=evidence_path,
                process=process,
            )
            dm_post_message_id = _ports.user_send_content(
                dm_chat_id,
                "post",
                post_content(nonce, None),
                f"e2e004-dmpost-{nonce}"[:50],
            )
            cleanup_message_ids.append(dm_post_message_id)
            dm_post = _ports.nontext_roundtrip(
                label="dmpost",
                message_id=dm_post_message_id,
                expected_message_type="post",
                expected_text=expected_post_text(nonce),
                minimum_text_length=len(expected_post_text(nonce)),
                chat_id=dm_chat_id,
                chat_type="p2p",
                nonce=nonce,
                command_dir=command_dir,
                evidence_path=evidence_path,
                process=process,
            )
            reporter.pass_(
                current_assertion,
                f"group_native={group_post_message_id} "
                f"group_body_length={group_post['text_length']} "
                f"dm_native={dm_post_message_id} "
                f"dm_body_digest={text_digest(expected_post_text(nonce))} "
                f"title+paragraphs+link_label_preserved=true "
                f"reply_native={dm_post['reply_native']} dead_lettered=false",
            )

            current_assertion = "A12-file-attachment-roundtrip"
            attachment = root / _ports.ATTACHMENT_FILE_NAME
            attachment.write_text(
                f"CONTRACT_LARK_FILE_BODY_{nonce}\n", encoding="utf-8"
            )
            file_message_id = _ports.user_send_attachment(
                dm_chat_id, "--file", attachment, f"e2e004-file-{nonce}"[:50]
            )
            cleanup_message_ids.append(file_message_id)
            file_stand_in = _ports.expected_file_text(file_message_id)
            file_result = _ports.nontext_roundtrip(
                label="file",
                message_id=file_message_id,
                expected_message_type="file",
                expected_text=file_stand_in,
                minimum_text_length=len(file_stand_in),
                chat_id=dm_chat_id,
                chat_type="p2p",
                nonce=nonce,
                command_dir=command_dir,
                evidence_path=evidence_path,
                process=process,
            )
            reporter.pass_(
                current_assertion,
                f"native={file_message_id} harness={file_result['harness']} "
                f"agent_text_digest={text_digest(file_stand_in)} "
                f"file_name_visible=true media_ref_visible=true "
                f"reply_native={file_result['reply_native']} dead_lettered=false",
            )

            current_assertion = "A13-image-attachment-roundtrip"
            picture = root / "hyprial-contract-pixel.png"
            picture.write_bytes(_ports.TINY_PNG)
            image_message_id = _ports.user_send_attachment(
                dm_chat_id, "--image", picture, f"e2e004-image-{nonce}"[:50]
            )
            cleanup_message_ids.append(image_message_id)
            image_stand_in = _ports.expected_image_text(image_message_id)
            image_result = _ports.nontext_roundtrip(
                label="image",
                message_id=image_message_id,
                expected_message_type="image",
                expected_text=image_stand_in,
                minimum_text_length=len(image_stand_in),
                chat_id=dm_chat_id,
                chat_type="p2p",
                nonce=nonce,
                command_dir=command_dir,
                evidence_path=evidence_path,
                process=process,
            )
            reporter.pass_(
                current_assertion,
                f"native={image_message_id} harness={image_result['harness']} "
                f"agent_text_digest={text_digest(image_stand_in)} "
                f"media_ref_visible=true "
                f"reply_native={image_result['reply_native']} dead_lettered=false",
            )

            # The public outbound seam starts the same dedicated App through
            # its own isolated daemon.  Release the inbound gateway first:
            # two live consumers for one App compete for the event stream and
            # can make the second worker fail during daemon startup.  All
            # remaining assertions are outbound or deterministic and no
            # longer need this gateway child.
            _ports.stop_gateway(process)
            process = None
            for handle in handles:
                handle.close()
            handles.clear()

            current_assertion = "A16-outbound-file-route-roundtrip"
            outbound_executable, outbound_environment, hyprial_version = (
                _ports.prepare_outbound_daemon(root, chat_id=dm_chat_id)
            )
            outbound_text = f"CONTRACT_LARK_OUTBOUND_FILE_{nonce}"
            outbound_attachment = root / "outbound-workspace" / attachment.name
            outbound_attachment.write_bytes(attachment.read_bytes())
            outbound = _ports.hyprial_cli(
                outbound_executable,
                outbound_environment,
                "message",
                "send",
                "--from",
                _ports.OUTBOUND_SENDER,
                "--to",
                outbound_environment["E2E_OUTBOUND_USER_TARGET"],
                "--summary",
                outbound_text,
                "--file",
                str(outbound_attachment),
                "--json",
                outbound_text,
            )
            deliveries = outbound.get("deliveries")
            if not isinstance(deliveries, list) or len(deliveries) != 1:
                raise ContractFailure(
                    "outbound hyprial message send did not return exactly one user delivery"
                )
            route_delivery = deliveries[0]
            if not isinstance(route_delivery, dict):
                raise ContractFailure("outbound user delivery must be an object")
            resource_deliveries = route_delivery.get("resourceDeliveries")
            if (
                not isinstance(resource_deliveries, list)
                or len(resource_deliveries) != 1
            ):
                raise ContractFailure(
                    "outbound user delivery did not return one pushed file delivery"
                )
            file_delivery = resource_deliveries[0]
            if not isinstance(file_delivery, dict):
                raise ContractFailure(
                    "outbound file delivery evidence must be an object"
                )
            outbound_message_id = file_delivery.get("nativeMessageId")
            outbound_file_key = file_delivery.get("fileKey")
            if (
                file_delivery.get("kind") != "file"
                or file_delivery.get("fileName") != _ports.ATTACHMENT_FILE_NAME
                or file_delivery.get("mediaType") != "text/plain"
                or file_delivery.get("sizeBytes") != attachment.stat().st_size
                or not isinstance(outbound_message_id, str)
                or not outbound_message_id
                or not isinstance(outbound_file_key, str)
                or not outbound_file_key
            ):
                raise ContractFailure(
                    "outbound file delivery evidence is incomplete or inconsistent"
                )
            cleanup_message_ids.extend(
                message_id
                for message_id in (
                    route_delivery.get("nativeMessageId"),
                    outbound_message_id,
                )
                if isinstance(message_id, str) and message_id
            )
            user_visible = _ports.get_message(outbound_message_id)
            if user_visible.get("msg_type") != "file":
                raise ContractFailure("user-side message is not a file attachment")
            user_content = _lark_message_content(user_visible)
            if (
                not isinstance(user_content, dict)
                or user_content.get("file_key") != outbound_file_key
            ):
                raise ContractFailure(
                    "user-side attachment key does not match route delivery evidence"
                )
            downloaded = root / "user-downloaded-outbound-file.txt"
            _ports.lark_cli(
                "im",
                "+messages-resources-download",
                "--as",
                "user",
                "--message-id",
                outbound_message_id,
                "--file-key",
                outbound_file_key,
                "--type",
                "file",
                "--output",
                downloaded.name,
                "--json",
                cwd=root,
            )
            if (
                not downloaded.is_file()
                or downloaded.read_bytes() != attachment.read_bytes()
            ):
                raise ContractFailure(
                    "user-side downloaded attachment content differs from the sent file"
                )
            _ports.stop_outbound_daemon(outbound_executable, outbound_environment)
            outbound_executable = None
            outbound_environment = None
            reporter.pass_(
                current_assertion,
                f"hyprial_version={hyprial_version.get('version')} "
                f"native={outbound_message_id} file_name={_ports.ATTACHMENT_FILE_NAME} "
                f"size={attachment.stat().st_size} "
                f"sha256={hashlib.sha256(attachment.read_bytes()).hexdigest()[:12]} "
                "user_visible_type=file content_equal=true",
            )

            current_assertion = "A14-merge-forward-not-dropped"
            merge = _ports.assert_merge_forward_not_dropped(root, chat_id)
            if (
                merge["status"] != "forwarded"
                or merge["message_type"] != "merge_forward"
                or merge["content_status"] != "supported"
                or merge["harness_requests"] != 1
                or merge["dead_letters"] != 0
                or not merge["forwarded_text"]
                or "merged-forward" not in str(merge["forwarded_text"])
            ):
                raise ContractFailure(
                    f"merge_forward message was not forwarded intact: {merge}"
                )
            reporter.pass_(
                current_assertion,
                "type=merge_forward status=forwarded harness_requests=1 "
                f"agent_text={merge['forwarded_text']!r} dead_letters=0 "
                "live_send=unavailable(user-token-unsupported-99991668)",
            )

            current_assertion = "A15-unsupported-type-loud"
            unsupported_anchor_id = _ports.user_send(
                dm_chat_id,
                f"CONTRACT_LARK_UNSUPPORTED_ANCHOR_{nonce}",
                f"e2e004-unsupported-{nonce}"[:50],
            )
            cleanup_message_ids.append(unsupported_anchor_id)
            live_alarm = _ports.assert_live_unsupported_alarm(
                root,
                app_secret=app_secret,
                chat_id=dm_chat_id,
                original_message_id=unsupported_anchor_id,
            )
            if (
                live_alarm["status"] != "unsupported-message-type"
                or live_alarm["harness_requests"] != 0
                or live_alarm["reply_to"] != unsupported_anchor_id
                or "unsupported-message-type" not in str(live_alarm["content"])
            ):
                raise ContractFailure(
                    f"live unsupported failure was not a native reply: {live_alarm}"
                )
            loud = _ports.assert_unsupported_type_is_loud(root, chat_id)
            unknown = loud["unknown_type"]
            invalid = loud["invalid_content"]
            broken = loud["normalize_error"]
            if (
                unknown["status"] != "unsupported-message-type"
                or not unknown["new_dead_letter"]
                or unknown["harness_requests"] != 0
                or not unknown["recorded"]
                or unknown["normalized_type"] != "unknown"
                or unknown["reply_count"] != 1
                or "unsupported-message-type" not in str(unknown["reply_text"])
            ):
                raise ContractFailure(f"unsupported type was not loud: {unknown}")
            if (
                invalid["status"] != "invalid-message-content"
                or not invalid["new_dead_letter"]
                or invalid["harness_requests"] != 0
                or not invalid["recorded"]
                or invalid["reply_count"] != 1
                or "invalid-message-content" not in str(invalid["reply_text"])
            ):
                raise ContractFailure(f"invalid content was not loud: {invalid}")
            if (
                broken["status"] != "normalize-error"
                or broken["harness_requests"] != 0
                or not broken["recorded"]
                or broken["reason"] != "normalize-error"
                or broken["reply_count"] != 1
                or "normalize-error" not in str(broken["reply_text"])
            ):
                raise ContractFailure(f"unparseable event was not preserved: {broken}")
            reporter.pass_(
                current_assertion,
                f"unsupported_type={unknown['status']}/{unknown['reason']} "
                f"invalid_content={invalid['status']}/{invalid['reason']} "
                f"normalize_error={broken['status']}/{broken['reason']} "
                "harness_requests=0 recoverable_dead_letters=3 "
                "fake_native_failure_replies=3 live_native_failure_replies=1 "
                "live_event_shape=adapter-bound(platform-rejects-unknown-msg-type) "
                "silent_drops=0",
            )

            current_assertion = _ports.ASSERTIONS[-1]
            final_evidence = evidence_path.read_text(encoding="utf-8")
            forbidden = (
                app_secret,
                request_text,
                reply_text,
                followup_text,
                restart_text,
                chat_id,
                dm_chat_id,
                target.bot_open_id,
                expected_post_text(nonce),
                f"CONTRACT_LARK_FILE_BODY_{nonce}",
                outbound_text,
                group_post["reply_text"],
                dm_post["reply_text"],
                file_result["reply_text"],
                image_result["reply_text"],
            )
            assert_sanitized_evidence(final_evidence, forbidden_values=forbidden)
            reporter.pass_(
                current_assertion,
                "app_secret_logged=false access_token_logged=false "
                "plaintext_logged=false raw_native_identity_logged=false",
            )

            _ports.stop_gateway(process)
            process = None
            for handle in handles:
                handle.close()
            handles.clear()
            print(
                "RESULT PASS E2E-004 feishu-roundtrip "
                f"verified={len(reporter.passed)}/{len(_ports.ASSERTIONS)} "
                f"nonce={nonce} request_native={request_message_id} "
                f"request_harness={harness_request['harness_message_id']} "
                f"reply_native={reply_message_id} completed_at={utc_now()}",
                flush=True,
            )
            return 0
    except ContractBlocked as error:
        reporter.block_remaining(str(error))
        # Exit code 2 for BLOCKED is the existing convention across the
        # executable runners (run-four-lark-adapters, run-resident-providers,
        # run-upgrade-continuity); it is kept, and made audible.
        print(
            "RESULT BLOCKED E2E-004 feishu-roundtrip "
            f"verified={len(reporter.passed)}/{len(_ports.ASSERTIONS)} "
            f"{redact_native_ids(str(error))}",
            file=sys.stderr,
            flush=True,
        )
        reporter.announce_blocked(str(error))
        return 2
    except (ContractFailure, OSError, KeyError, TypeError, ValueError) as error:
        if current_assertion not in reporter.reported:
            reporter.fail(current_assertion, str(error))
        reporter.block_remaining("prior assertion failed")
        print(
            f"RESULT FAIL E2E-004 feishu-roundtrip "
            f"verified={len(reporter.passed)}/{len(_ports.ASSERTIONS)} "
            f"{redact_native_ids(str(error))}",
            flush=True,
        )
        return 1
    finally:
        try:
            _ports.stop_outbound_daemon(outbound_executable, outbound_environment)
        except (ContractFailure, OSError) as error:
            print(
                "CLEANUP WARN E2E-004 outbound-daemon "
                f"error_type={type(error).__name__}",
                file=sys.stderr,
            )
        try:
            _ports.stop_gateway(process)
        except ContractFailure:
            pass
        for handle in handles:
            handle.close()
        if app_secret is not None:
            gateway = LarkSdkGateway.from_credentials(_ports.APP_ID, app_secret)
            for message_id in cleanup_message_ids:
                try:
                    gateway.clear_reaction(message_id, "OnIt")
                except Exception as error:  # noqa: BLE001 - external cleanup boundary
                    print(
                        "CLEANUP WARN E2E-004 "
                        f"native={message_id} error_type={type(error).__name__}",
                        file=sys.stderr,
                    )
