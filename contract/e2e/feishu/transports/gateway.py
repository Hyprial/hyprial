"""E2E-004 transports / gateway; explicit callbacks keep entrypoint seams live."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4
import lark_oapi as lark
from hyprial.daemon.impl.adapters.lark import (
    ActorTarget,
    HarnessDelivery,
    HarnessReceipt,
    LarkAdapter,
    LarkEventStream,
    LarkInboundMessage,
    LarkSdkGateway,
    LarkStateStore,
    normalize_sdk_event,
)
from feishu.evidence import (
    ContractFailure,
    EvidenceLog,
    epoch_ms,
    load_json,
    native_id_label,
    redact_native_ids,
    text_digest,
)


@dataclass(frozen=True)
class TransportsGatewayPorts:
    """Only this module's observed collaborators and configuration."""

    ACTOR: ActorTarget
    APP_ID: str
    APP_NAME: str
    ChildRoutes: Callable[..., Any]
    POLL_SECONDS: float
    entrypoint: str
    load_approved_target: Callable[..., Any]
    wait_for_evidence: Callable[..., Any]
    write_command: Callable[..., Any]


class ChildHarness:
    def __init__(self, evidence: EvidenceLog, nonce: str) -> None:
        self._evidence = evidence
        self._nonce = nonce
        self._sequence = 0

    def send_request(self, request):  # type: ignore[no-untyped-def]
        self._sequence += 1
        harness_message_id = f"harness_{self._nonce}_{os.getpid()}_{self._sequence}"
        self._evidence.emit(
            "harness.request.accepted",
            harness_message_id=harness_message_id,
            native_message_id=request.message_id,
            target_actor_id=request.to.actor_id,
            target_actor_key=request.to.actor_key,
            conversation_label=redact_native_ids(request.conversation_id),
            text_digest=text_digest(request.text),
            text_length=len(request.text),
        )
        return HarnessReceipt(message_id=harness_message_id)

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None:
        self._evidence.emit(
            "harness.delivery.accepted",
            delivery_id=delivery_id,
            native_message_id=native_message_id,
        )

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None:
        self._evidence.emit(
            "harness.delivery.rejected",
            delivery_id=delivery_id,
            deterministic=deterministic,
            error_type=type(error).__name__,
        )


def gateway_child(
    arguments: argparse.Namespace, *, _ports: TransportsGatewayPorts
) -> int:
    target = _ports.load_approved_target(need_identity=False)
    app_id, app_secret, chat_id = target.app_id, target.app_secret, target.chat_id
    evidence = EvidenceLog(Path(arguments.evidence))
    command_dir = Path(arguments.commands)
    command_dir.mkdir(parents=True, exist_ok=True)
    state = LarkStateStore(Path(arguments.state))
    harness = ChildHarness(evidence, arguments.nonce)
    sdk_gateway = LarkSdkGateway.from_credentials(app_id, app_secret)
    adapter = LarkAdapter(
        state=state,
        lark=sdk_gateway,
        harness=harness,
        routes=_ports.ChildRoutes(chat_id, arguments.dm_chat),
        channel_actor_id=f"adapter:e2e:lark:{_ports.APP_NAME}",
    )
    captured: dict[str, LarkInboundMessage] = {}
    captured_lock = threading.Lock()

    def dead_letter_rows() -> list[dict[str, Any]]:
        """Adapter-private dead letters, without their plaintext bodies.

        The 2026-08-13 P0 hid dropped messages here: the main inbox DLQ showed
        nothing.  The parent asserts against this projection, so a body must
        never be copied into the evidence log (A10).
        """

        return [
            {
                "message_id": letter.message_id,
                "reason": letter.reason,
                "detail": letter.detail,
                "message_type": letter.message_type,
                "chat_label": native_id_label(letter.chat_id)
                if letter.chat_id
                else None,
                "text_digest": text_digest(letter.text),
                "text_length": len(letter.text),
            }
            for letter in adapter.dead_letters()
        ]

    def on_message(event: object) -> None:
        try:
            message = normalize_sdk_event(event)
        except Exception as error:  # noqa: BLE001 - normalization boundary
            # Never let a normalization failure die inside the SDK callback:
            # that is precisely how the P0 messages vanished.  Hand the raw
            # event to the adapter so its dead-letter path runs, and leave a
            # legible evidence line instead of an unexplained missing event.
            outcome = adapter.handle_sdk_event(event)
            evidence.emit(
                "sdk.normalize_error",
                error_type=type(error).__name__,
                status=outcome.status,
                dead_lettered=outcome.new_dead_letter,
            )
            return
        with captured_lock:
            captured[message.message_id] = message
        evidence.emit(
            "sdk.event",
            event_id=message.event_id,
            message_id=message.message_id,
            chat_label=native_id_label(message.chat_id),
            chat_type=message.chat_type,
            message_type=message.message_type,
            reply_to=message.reply_to,
            text_digest=text_digest(message.text),
            text_length=len(message.text),
        )
        outcome = adapter.handle_inbound(message)
        evidence.emit(
            "adapter.inbound",
            message_id=message.message_id,
            event_id=message.event_id,
            status=outcome.status,
            harness_message_id=outcome.harness_message_id,
            target_actor_id=outcome.target_actor_id,
            ack_error=outcome.ack_error,
            new_dead_letter=outcome.new_dead_letter,
        )

    def command_loop() -> None:
        while True:
            for path in sorted(command_dir.glob("*.json")):
                command = load_json(path)
                path.unlink(missing_ok=True)
                command_id = command.get("id")
                kind = command.get("kind")
                if kind == "deliver":
                    actor = ActorTarget(**command["from_actor"])
                    outcome = adapter.handle_delivery(
                        HarnessDelivery(
                            delivery_id=command["delivery_id"],
                            message_id=command["message_id"],
                            reply_to=command["reply_to"],
                            from_actor=actor,
                            text=command["text"],
                        )
                    )
                    evidence.emit(
                        "adapter.delivery",
                        command_id=command_id,
                        delivery_id=command["delivery_id"],
                        reply_to=command["reply_to"],
                        status=outcome.status,
                        native_message_id=outcome.native_message_id,
                        cleanup_error=outcome.cleanup_error,
                        error=outcome.error,
                    )
                elif kind == "replay":
                    native_message_id = command["native_message_id"]
                    with captured_lock:
                        message = captured.get(native_message_id)
                    if message is None:
                        evidence.emit(
                            "adapter.replay",
                            command_id=command_id,
                            status="missing-captured-event",
                            native_message_id=native_message_id,
                        )
                    else:
                        outcome = adapter.handle_inbound(message)
                        evidence.emit(
                            "adapter.replay",
                            command_id=command_id,
                            status=outcome.status,
                            native_message_id=native_message_id,
                            event_id=message.event_id,
                        )
                elif kind == "dead-letters":
                    evidence.emit(
                        "adapter.dead_letters",
                        command_id=command_id,
                        letters=dead_letter_rows(),
                    )
                else:
                    evidence.emit(
                        "command.error",
                        command_id=command_id,
                        error="unknown-command",
                    )
            time.sleep(_ports.POLL_SECONDS)

    threading.Thread(target=command_loop, daemon=True).start()

    class ReadyClient(lark.ws.Client):
        async def _connect(self) -> None:
            await super()._connect()
            evidence.emit(
                "gateway.ready", app_name=_ports.APP_NAME, app_id=_ports.APP_ID
            )

    def client_factory(
        client_app_id: str,
        client_app_secret: str,
        dispatcher: object,
    ) -> object:
        return ReadyClient(
            client_app_id,
            client_app_secret,
            log_level=lark.LogLevel.ERROR,
            event_handler=dispatcher,
            auto_reconnect=True,
        )

    LarkEventStream(
        app_id=app_id,
        app_secret=app_secret,
        on_message=on_message,
        client_factory=client_factory,
    ).start()
    return 0


def start_gateway(
    root: Path,
    state_path: Path,
    evidence_path: Path,
    command_dir: Path,
    nonce: str,
    generation: int,
    dm_chat_id: str | None = None,
    *,
    _ports: TransportsGatewayPorts,
) -> tuple[subprocess.Popen[str], Any, Any]:
    stdout_handle = (root / f"gateway-{generation}.stdout").open("w", encoding="utf-8")
    stderr_handle = (root / f"gateway-{generation}.stderr").open("w", encoding="utf-8")
    environment = dict(os.environ)
    environment.update(
        {
            "HYPRIAL_HOME": str(root / "home"),
            "HARNESS_STATE_DIR": str(root / "state"),
        }
    )
    process = subprocess.Popen(
        [
            sys.executable,
            str(Path(_ports.entrypoint).resolve()),
            "--gateway-child",
            "--state",
            str(state_path),
            "--evidence",
            str(evidence_path),
            "--commands",
            str(command_dir),
            "--nonce",
            nonce,
            *(("--dm-chat", dm_chat_id) if dm_chat_id else ()),
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=stdout_handle,
        stderr=stderr_handle,
        text=True,
    )
    return process, stdout_handle, stderr_handle


def stop_gateway(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired as error:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired as final_error:
            raise ContractFailure(
                "gateway child did not stop after TERM/INT"
            ) from final_error
        raise ContractFailure("gateway child required SIGINT after SIGTERM") from error


def write_command(command_dir: Path, kind: str, **fields: Any) -> str:
    command_id = uuid4().hex
    final = command_dir / f"{epoch_ms()}-{command_id}.json"
    temporary = final.with_suffix(".tmp")
    temporary.write_text(
        json.dumps({"id": command_id, "kind": kind, **fields}, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, final)
    return command_id


def issue_delivery(
    command_dir: Path,
    harness_message_id: str,
    text: str,
    suffix: str,
    *,
    _ports: TransportsGatewayPorts,
) -> tuple[str, str]:
    delivery_id = f"delivery_{suffix}"
    command_id = _ports.write_command(
        command_dir,
        "deliver",
        delivery_id=delivery_id,
        message_id=f"message_{suffix}",
        reply_to=harness_message_id,
        from_actor=asdict(_ports.ACTOR),
        text=text,
    )
    return command_id, delivery_id


def dump_dead_letters(
    command_dir: Path,
    evidence_path: Path,
    process: subprocess.Popen[str] | None,
    *,
    _ports: TransportsGatewayPorts,
) -> list[dict[str, Any]]:
    """Read the adapter's private dead-letter store through the child."""

    command_id = _ports.write_command(command_dir, "dead-letters")
    record = _ports.wait_for_evidence(
        evidence_path,
        "adapter.dead_letters",
        lambda row: row.get("command_id") == command_id,
        process=process,
    )
    letters = record.get("letters")
    if not isinstance(letters, list):
        raise ContractFailure("dead-letter dump did not return a list")
    return [letter for letter in letters if isinstance(letter, dict)]
