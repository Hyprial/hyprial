"""One-process-per-gateway Lark adapter worker."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Callable


from hyprial.daemon.impl.adapters.lark.inbound.adapter import LarkAdapter
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    ActorTarget,
    HarnessDelivery,
)

from hyprial.daemon.impl.adapters.lark.worker.sweep import STALE_REBUILD_EXIT_CODE
def _bridge_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _handle_reply_bridge_frame(
    adapter: LarkAdapter, frame: object
) -> dict[str, object]:
    if not isinstance(frame, dict):
        raise ValueError("reply bridge frame must be an object")
    if frame.get("kind") == "alarm":
        delivery_id = _bridge_string(frame.get("deliveryId"), "deliveryId")
        outcome = adapter.handle_alarm_delivery(
            _bridge_string(frame.get("correlationId"), "correlationId"),
            _bridge_string(frame.get("text"), "text"),
            idempotency_key=_bridge_string(
                frame.get("idempotencyKey"), "idempotencyKey"
            ),
        )
        if outcome.status != "accepted" or outcome.native_message_id is None:
            return {
                "deliveryId": delivery_id,
                "ok": False,
                "error": outcome.error or "Lark alarm rejected",
            }
        return {
            "deliveryId": delivery_id,
            "ok": True,
            "nativeMessageId": outcome.native_message_id,
        }
    raw_actor = frame.get("fromActor")
    if not isinstance(raw_actor, dict):
        raise ValueError("fromActor must be an object")
    delivery = HarnessDelivery(
        delivery_id=_bridge_string(frame.get("deliveryId"), "deliveryId"),
        message_id=_bridge_string(frame.get("messageId"), "messageId"),
        reply_to=_bridge_string(frame.get("replyTo"), "replyTo"),
        from_actor=ActorTarget(
            actor_id=_bridge_string(raw_actor.get("actorId"), "fromActor.actorId"),
            actor_key=_bridge_string(raw_actor.get("actorKey"), "fromActor.actorKey"),
            display_name=_bridge_string(
                raw_actor.get("displayName"), "fromActor.displayName"
            ),
        ),
        text=_bridge_string(frame.get("text"), "text"),
    )
    outcome = adapter.handle_delivery(delivery, settle=False)
    if outcome.status != "accepted" or outcome.native_message_id is None:
        return {
            "deliveryId": delivery.delivery_id,
            "ok": False,
            "error": outcome.error or "Lark reply rejected",
        }
    return {
        "deliveryId": delivery.delivery_id,
        "ok": True,
        "nativeMessageId": outcome.native_message_id,
    }


def _serve_reply_bridge(adapter: LarkAdapter, control_fd: int) -> None:
    """Serve the private daemon/worker socket until either side exits."""

    control = socket.socket(fileno=control_fd)
    buffer = bytearray()
    try:
        while True:
            chunk = control.recv(64 * 1024)
            if not chunk:
                return
            buffer.extend(chunk)
            while b"\n" in buffer:
                raw, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                try:
                    frame = json.loads(raw)
                    response = _handle_reply_bridge_frame(adapter, frame)
                except (NameError, ImportError):
                    raise
                except (
                    ValueError,
                    TypeError,
                    json.JSONDecodeError,
                    UnicodeDecodeError,
                ) as error:
                    response = {"ok": False, "error": str(error)}
                if (
                    isinstance(frame, dict)
                    and frame.get("kind") != "alarm"
                    and response.get("ok") is True
                    and isinstance(frame.get("replyTo"), str)
                ):
                    persist = getattr(adapter, "persist_delivery_reaction", None)
                    if callable(persist):
                        # Local SQLite custody is established before the reply
                        # receipt. Native cleanup remains asynchronous, so a
                        # blocked reaction endpoint cannot delay settlement.
                        persist(frame["replyTo"])
                control.sendall(
                    json.dumps(response, separators=(",", ":")).encode() + b"\n"
                )
                # The positive control receipt is the durable reply outcome.
                # ACK-emoji cleanup is best effort and may take another
                # platform roundtrip; perform it only after the daemon has the
                # receipt so cleanup latency cannot turn a successful native
                # reply into an inbox submit timeout and duplicate retry.
                if (
                    isinstance(frame, dict)
                    and frame.get("kind") != "alarm"
                    and response.get("ok") is True
                    and isinstance(frame.get("replyTo"), str)
                ):
                    cleanup = getattr(adapter, "clear_delivery_reaction", None)
                    if callable(cleanup):
                        cleanup(frame["replyTo"])
    finally:
        control.close()


def _run_reply_bridge(
    adapter: LarkAdapter,
    control_fd: int,
    *,
    exit_process: Callable[[int], object] = os._exit,
) -> None:
    """Make the end of the daemon link fatal to the worker.

    The bridge runs on a background thread because the SDK owns the main
    thread.  Merely re-raising there would leave an apparently-online worker
    whose reply consumer had died, so programming/import defects request the
    same supervised rebuild used by terminal stream failures.

    The same holds when the bridge simply ENDS: EOF, or a reset/broken pipe,
    means the daemon end of the private socket is gone -- the daemon exited,
    crashed, or was replaced.  Returning quietly used to leave the main thread
    in ``stream.start()`` holding the Feishu connection forever as an orphan
    (PPID 1), beside the replacement daemon's own worker: every adapter ran
    twice (production 2026-09-20, and again 2026-09-23: 10 routes x 2).  A
    live daemon rebuilds the worker on exit; a dead one needs nothing to.
    """

    try:
        _serve_reply_bridge(adapter, control_fd)
    except (NameError, ImportError):
        exit_process(STALE_REBUILD_EXIT_CODE)
        return
    except OSError:
        pass
    exit_process(STALE_REBUILD_EXIT_CODE)
