"""One-process-per-gateway Lark adapter worker."""

from __future__ import annotations

import hashlib
import json
import math
import os
import socket
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from hyprial.log import Logger
from hyprial.home import configured_hyprial_home
from hyprial.persistent_config import PersistentConfigStore
from hyprial.contracts import ipc_errors
from hyprial.contracts.ipc_errors import DaemonRequestError

from .adapter import (
    ACK_EMOJI,
    DEFAULT_DEAD_LETTER_ALERT_THRESHOLD,
    DEFAULT_TRANSIENT_RETRY_BACKOFF_MAX_SECONDS,
    DEFAULT_TRANSIENT_RETRY_BACKOFF_INITIAL_SECONDS,
    DEFAULT_TRANSIENT_RETRY_BUDGET_SECONDS,
    LarkAdapter,
)
from .api import (
    MAX_RUNTIME_TARGET_ITEM_BYTES,
    AgentDirectoryEntry,
    ActorTarget,
    HarnessDelivery,
    HarnessReceipt,
    HarnessRequest,
    ReconcileReport,
    RuntimeTarget,
    is_safe_runtime_target_field,
)
from .health import (
    DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS,
    DEFAULT_RECONCILE_LOOKBACK_SECONDS,
    DEFAULT_REST_PROBE_TIMEOUT_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
    LarkStreamHealthMonitor,
    report_reconcile_failure,
    required_reconcile_lookback,
)
from .reaction_effects import ReactionEffectsRuntime
from .runtime import CONTROL_FD_ENV_VAR, READY_FD_ENV_VAR
from .scopes import LarkScopeClient, LarkScopeRecovery, LarkScopeThrottleStore
from .sdk import LarkEventStream, LarkSdkGateway
from .state import MAX_DEAD_LETTERS, LarkStateStore

STALE_REBUILD_EXIT_CODE = 75


def _bounded_seconds(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or not minimum <= value <= maximum:
        return default
    return value


def _bounded_integer(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if not minimum <= value <= maximum:
        return default
    return value


def _recovery_timing_from_environment() -> tuple[float, float, float, float]:
    """Resolve all recovery clocks with finite, operationally safe bounds."""

    return (
        _bounded_seconds(
            "HYPRIAL_LARK_STALE_AFTER_SECONDS",
            DEFAULT_STALE_AFTER_SECONDS,
            minimum=60.0,
            maximum=86_400.0,
        ),
        _bounded_seconds(
            "HYPRIAL_LARK_HEALTH_CHECK_INTERVAL_SECONDS",
            DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
            minimum=1.0,
            maximum=300.0,
        ),
        _bounded_seconds(
            "HYPRIAL_LARK_REST_PROBE_TIMEOUT_SECONDS",
            DEFAULT_REST_PROBE_TIMEOUT_SECONDS,
            minimum=0.1,
            maximum=120.0,
        ),
        _bounded_seconds(
            "HYPRIAL_LARK_IDLE_RECONCILE_TIMEOUT_SECONDS",
            DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS,
            minimum=1.0,
            maximum=300.0,
        ),
    )


def _reconcile_health_result(report: ReconcileReport | None) -> int:
    """Translate a sweep into a fail-closed, detail-free health result.

    Fail-closed is deliberate and unchanged: a sweep that might have missed
    messages must not report health.  What changed is *which* failures count.

    ``blocked_chats`` are chats this app is not permitted to read; retrying
    cannot clear them, so raising here produced a restart loop that ended in
    quarantine -- the adapter died of a condition no restart could fix, and
    every other chat died with it.  Those are reported by the sweep and
    logged, and they do not fail the probe.

    A permanently-refused chat no longer fails the probe by itself -- it is
    retired out of the scan set (``retired_chats``) -- but a sweep that reached
    *no* live chat at all is still a total failure and must stay stale.

    A missing history scope is the one exception among the *retryable* errors:
    a mention-only Feishu app cannot replay messages it was never permitted to
    read, but its websocket subscription can still receive new @-mentions.
    Keep that live path up rather than restarting forever; all other incomplete
    reconciliation outcomes remain terminal.
    """

    if report is None or any(
        not error.endswith(": history-permission-unavailable")
        for error in report.retryable_errors
    ):
        raise RuntimeError("Lark history reconciliation incomplete")
    if report.chats_scanned > 0 and len(report.retired_chats) >= report.chats_scanned:
        raise RuntimeError("Lark history reconciliation reached no live chat")
    return report.forwarded + report.dead_lettered


def _rebuild_after_stale(
    reason: str,
    *,
    pause: Callable[[float], None] = time.sleep,
    exit_process: Callable[[int], object] = os._exit,
) -> None:
    """Leave one telemetry observation window, then request supervised restart."""

    del reason
    pause(1.0)
    exit_process(STALE_REBUILD_EXIT_CODE)


class ReconnectSweepCoordinator:
    """Coalesce reconnect storms into one deadline-bound history pipeline."""

    def __init__(
        self,
        *,
        name: str,
        reconcile: Callable[[], ReconcileReport | None],
        timeout: float,
        health: LarkStreamHealthMonitor,
        report: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.name = name
        self._reconcile = reconcile
        self.timeout = timeout
        self._health = health
        self._report = report
        self._lock = threading.Lock()
        self._active = False
        self._pending = False
        self._terminal_failure = False

    def request(self) -> bool:
        """Start one sweep or coalesce this callback into the active sweep."""

        with self._lock:
            if self._terminal_failure or self._health.rebuild_latched():
                self._terminal_failure = True
                return False
            if self._active:
                if not self._health.history_reconcile_started(coalesced=True):
                    self._terminal_failure = True
                    self._pending = False
                    return False
                self._pending = True
                return False
            if not self._health.history_reconcile_started():
                self._terminal_failure = True
                return False
            self._active = True
            self._pending = False
            threading.Thread(
                target=self._run,
                name=f"lark-reconcile-{self.name}",
                daemon=True,
            ).start()
            return True

    def _run(self) -> None:
        while True:
            if self._health.rebuild_latched():
                with self._lock:
                    self._terminal_failure = True
                    self._active = False
                    self._pending = False
                return
            failed, report = self._call_once()
            with self._lock:
                if self._terminal_failure:
                    self._active = False
                    self._pending = False
                    return
            if not failed:
                try:
                    _reconcile_health_result(report)
                except RuntimeError:
                    failed = True
            if failed:
                with self._lock:
                    self._terminal_failure = True
                    self._active = False
                    self._pending = False
                self._health.history_probe_failed()
                return

            with self._lock:
                if self._pending:
                    # Any number of callbacks while the sweep was active need
                    # exactly one final pass covering the latest reconnect.
                    self._pending = False
                    if not self._health.history_reconcile_started(coalesced=True):
                        self._terminal_failure = True
                        self._active = False
                        return
                    continue
                # Keep the coordinator lock while publishing healthy so a new
                # callback cannot slip between settlement and connected().
                if not self._health.connected():
                    self._terminal_failure = True
                    self._active = False
                    self._pending = False
                    return
                self._active = False
                return

    def _call_once(self) -> tuple[bool, ReconcileReport | None]:
        completed = threading.Event()
        outcome: dict[str, object] = {"failed": False, "report": None}

        def invoke() -> None:
            try:
                outcome["report"] = self._reconcile()
            except (NameError, ImportError):
                # Re-raise programming/import defects for visibility, while
                # also making the coordinator fail closed instead of
                # accidentally settling this sweep as healthy.
                outcome["failed"] = True
                raise
            except BaseException as error:  # never surface SDK URL/token details
                outcome["failed"] = True
                # Exception from the sweep never reaches here (the wrapper
                # registered as ``reconcile`` folds it into None first), so
                # this names the non-Exception BaseExceptions that bypass
                # that wrapper: KeyboardInterrupt, SystemExit, CancelledError.
                report_reconcile_failure(self._report, error, stage="sweep-call")
            finally:
                completed.set()

        threading.Thread(
            target=invoke,
            name=f"lark-reconcile-call-{self.name}",
            daemon=True,
        ).start()
        if not completed.wait(self.timeout):
            return True, None
        report = outcome["report"]
        if report is not None and not isinstance(report, ReconcileReport):
            return True, None
        return bool(outcome["failed"]), report


def _ipc(socket_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    request_id = str(uuid4())
    frame = {"version": 1, "id": request_id, "method": method, "params": params}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(15)
    try:
        client.connect(str(socket_path))
        client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        buffer = bytearray()
        while b"\n" not in buffer:
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise RuntimeError("daemon disconnected from Lark adapter")
            buffer.extend(chunk)
        response = json.loads(buffer.partition(b"\n")[0])
    finally:
        client.close()
    if not isinstance(response, dict):
        raise RuntimeError("daemon returned an invalid adapter response")
    failure = response.get("error")
    if isinstance(failure, dict):
        # PR #332 F3: keep the envelope's stable wire code on the raised
        # error.  Callers must distinguish a transient restore-gate refusal
        # (DAEMON_RESTORING) from a permanent rejection; a bare RuntimeError
        # carrying only the message made every refusal look permanent.
        # PR #332 F4②: transient codes now deserialise through the shared
        # registry into the SAME class every other client gets (still a
        # DaemonRequestError, so existing handlers keep catching it).
        code = failure.get("code")
        message = str(failure.get("message", "daemon request failed"))
        if isinstance(code, str) and code:
            transient = ipc_errors.transient_error_from_code(
                code, message, failure.get("data")
            )
            if transient is not None:
                raise transient
            raise DaemonRequestError(code, message, failure.get("data"))
        raise RuntimeError(message)
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("daemon adapter response has no result")
    return result


class _Harness:
    def __init__(self, socket_path: Path, *, logger: Logger | None = None) -> None:
        self.socket_path = socket_path
        self._logger = logger

    def send_request(self, request: HarnessRequest) -> HarnessReceipt:
        result = _ipc(
            self.socket_path,
            "message.send",
            {
                "actor": request.from_actor_id,
                "to": [request.to.actor_id],
                "message": request.text,
                "conversationId": request.conversation_id,
                "providerMetadata": dict(request.provider_metadata),
                "idempotencyKey": (
                    f"lark-inbound:{request.from_actor_id}:{request.message_id}"
                ),
            },
        )
        if result.get("ok") is not True or not isinstance(result.get("messageId"), str):
            raise RuntimeError("daemon did not accept the Lark request")
        return HarnessReceipt(str(result["messageId"]))

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None:
        _ipc(
            self.socket_path,
            "message.ack",
            {"actor": "lark-adapter", "messageId": delivery_id},
        )

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None:
        # The durable outbox is daemon-owned; settlement happens over IPC.
        # The reply failure reason must still reach the adapter log -- an
        # unlogged ``del error`` here is exactly what made harness_reply
        # failures look silent (the code/msg the platform returned was
        # dropped on the floor).  ``error`` carries only the summary
        # (operation, code, platform msg); never the reply body.
        if self._logger is not None:
            try:
                self._logger.log(
                    "warn",
                    "delivery.rejected",
                    messageId=delivery_id,
                    reason=error,
                    deterministic=deterministic,
                )
            except (NameError, ImportError):
                raise
            except OSError:
                pass


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


class _Routes:
    """The worker's read-only view of daemon-owned routing state.

    The TS-era pieces are gone on purpose: ``legacyConversationPins`` had no
    Python writer and an empty production value; the ``default``-route
    fallback and route-name mention matching both returned a Lark chat id
    where an actor id belongs, and could never match anything real.  What
    remains is one question -- "which agent is this adapter pinned to?" --
    answered by the daemon, which owns the adapter->agent index.
    """

    def __init__(
        self,
        *,
        gateway_name: str,
        socket_path: Path,
        report: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.gateway_name = gateway_name
        self.socket_path = socket_path
        self._report = report or self._report_to_stderr
        self._has_cached_pin = False
        self._cached_pin: str | None = None

    @staticmethod
    def _report_to_stderr(payload: dict[str, object]) -> None:
        print(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )

    @staticmethod
    def _target(actor: str, display_name: str | None = None) -> ActorTarget:
        return ActorTarget(
            actor_id=actor,
            actor_key=actor,
            display_name=display_name or actor,
        )

    def actor_by_key(self, actor_key: str) -> ActorTarget | None:
        return self._target(actor_key)

    def pinned_actor(self, conversation_id: str) -> ActorTarget | None:
        """The agent this adapter is pinned to, per the daemon's pin index.

        Pins live in the daemon's agents database (``pins`` table); the
        worker never reads daemon state files itself.  Every message queries
        the daemon; a successful empty result replaces the cache (so unpin is
        immediate), while a transient query failure reuses the last successful
        value and emits a warning through the worker telemetry stream.
        """

        del conversation_id  # a pin is per adapter, never per conversation
        try:
            result = _ipc(self.socket_path, "adapter.pins", {})
            pins = result.get("pins")
            if not isinstance(pins, dict):
                raise RuntimeError("daemon pins response must contain an object")
            actor = pins.get(self.gateway_name)
            if self.gateway_name in pins and (
                not isinstance(actor, str) or not actor
            ):
                raise RuntimeError("daemon returned an invalid adapter pin")
        except (
            OSError,
            RuntimeError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as error:
            fallback = "no-successful-query"
            if self._has_cached_pin:
                fallback = (
                    "last-successful-pin"
                    if self._cached_pin is not None
                    else "last-successful-unpinned"
                )
            warning: dict[str, object] = {
                "status": "warning",
                "event": "lark.pin.query_failed",
                "errorType": type(error).__name__,
                "fallback": fallback,
            }
            if self._cached_pin is not None:
                warning["cachedActor"] = self._cached_pin
            self._report(warning)
            actor = self._cached_pin
        else:
            self._cached_pin = actor
            self._has_cached_pin = True
        if actor is None:
            return None
        assert isinstance(actor, str)
        return self._target(actor)

    def list_runtime_targets(self) -> tuple[RuntimeTarget, ...]:
        result = _ipc(self.socket_path, "targets", {"kind": "agent"})
        raw_targets = result.get("targets")
        if not isinstance(raw_targets, list):
            raise RuntimeError("daemon targets response must contain an array")
        targets: list[RuntimeTarget] = []
        for item in raw_targets:
            if not isinstance(item, dict) or item.get("targetKind") != "agent":
                continue
            target_uri = item.get("targetUri")
            actor = item.get("actor")
            status = item.get("status")
            if not all(
                is_safe_runtime_target_field(value)
                for value in (target_uri, actor, status)
            ):
                continue
            assert isinstance(target_uri, str)
            assert isinstance(actor, str)
            assert isinstance(status, str)
            if (
                len(f"- {target_uri} [{status}]".encode("utf-8"))
                > MAX_RUNTIME_TARGET_ITEM_BYTES
            ):
                continue
            from hyprial.uri import short_actor_name

            alias = short_actor_name(actor)
            targets.append(RuntimeTarget(target_uri, actor, alias, status))
        return tuple(
            sorted(
                targets,
                key=lambda item: (item.target_uri, item.actor, item.status),
            )
        )

    def list_agent_directory(self) -> tuple[AgentDirectoryEntry, ...]:
        """Join daemon entity records with its live target union.

        ``ps.agents`` is the entity/configuration projection backed by the
        agent registry.  ``targets`` is the daemon's existing entity-plus-
        presence union and owns the current running/down verdict.  The Lark
        worker consumes both public IPC projections and never opens SQLite.
        """

        ps = _ipc(self.socket_path, "ps", {})
        raw_agents = ps.get("agents")
        if not isinstance(raw_agents, list):
            raise RuntimeError("daemon ps response must contain an agents array")
        targets = self.list_runtime_targets()
        target_status = {target.target_uri: target.status for target in targets}
        entries: dict[str, AgentDirectoryEntry] = {}
        for item in raw_agents:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            name = item.get("actor")
            if not isinstance(uri, str) or not is_safe_runtime_target_field(uri):
                continue
            if not isinstance(name, str) or not is_safe_runtime_target_field(name):
                name = uri
            raw_pins = item.get("pinnedAdapters", ())
            pins = (
                tuple(
                    pin
                    for pin in raw_pins
                    if isinstance(pin, str) and is_safe_runtime_target_field(pin)
                )
                if isinstance(raw_pins, list)
                else ()
            )
            preferred = item.get("preferredHarness")
            if not isinstance(preferred, str) or not is_safe_runtime_target_field(
                preferred
            ):
                preferred = None
            raw_status = target_status.get(uri, item.get("status", "offline"))
            status = raw_status if raw_status in {"online", "offline"} else "offline"
            entries[uri] = AgentDirectoryEntry(
                name=name,
                status="running" if status == "online" else "down",
                pinned_adapters=tuple(sorted(set(pins))),
                preferred_harness=preferred,
            )
        for target in targets:
            entries.setdefault(
                target.target_uri,
                AgentDirectoryEntry(
                    name=target.actor or target.target_uri,
                    status="running" if target.status == "online" else "down",
                ),
            )
        return tuple(entries[key] for key in sorted(entries))

    def resolve_runtime_target(self, token: str) -> tuple[ActorTarget, ...]:
        if not is_safe_runtime_target_field(token):
            return ()
        targets = self.list_runtime_targets()
        exact = tuple(
            target.as_actor_target()
            for target in targets
            if token in {target.target_uri, target.actor}
        )
        if exact:
            return exact
        folded = token.casefold()
        return tuple(
            target.as_actor_target()
            for target in targets
            if target.alias.casefold() == folded
        )


def main(arguments: list[str] | None = None) -> int:
    values = sys.argv[1:] if arguments is None else arguments
    if len(values) != 1 or not values[0]:
        raise SystemExit("usage: python -m hyprial.adapters.lark.worker NAME")
    name = values[0]
    home = configured_hyprial_home()[0]
    state_dir = Path(os.environ.get("HARNESS_STATE_DIR", home / "state")).resolve()
    logger = Logger.adapter(state_dir, name=name)
    socket_path = Path(os.environ.get("HARNESS_SOCKET_PATH", state_dir / "daemon.sock"))
    config_store = PersistentConfigStore(home, state_dir)
    config = config_store.load()
    gateway = next((item for item in config.channels.gateways if item.name == name), None)
    if gateway is None:
        raise RuntimeError(f"Lark adapter is not configured: {name}")
    secret = config_store.lark_app_secret(gateway.credential_ref)
    scope_client = LarkScopeClient(gateway.app_id, secret)
    notification_gateway = LarkSdkGateway.from_credentials(
        gateway.app_id,
        secret,
        logger=logger,
        gateway_name=name,
    )
    default_route = next(
        (item for item in gateway.routes if item.name == gateway.default_route), None
    )
    notification_routes = (
        (default_route,)
        if default_route is not None and default_route.type == "direct"
        else tuple(
            item
            for item in gateway.routes
            if default_route is not None
            and default_route.type == "fanout"
            and item.name in default_route.members
            and item.type == "direct"
        )
    )

    def notify_operator(text: str, idempotency_key: str) -> bool:
        sent = False
        for route in notification_routes:
            if route.native_id is not None:
                notification_gateway.send_chat(
                    route.native_id,
                    text,
                    idempotency_key=f"{idempotency_key}:{route.name}",
                )
                sent = True
        return sent

    def notify_scope_authorization(text: str) -> None:
        digest = hashlib.sha256(text.encode()).hexdigest()[:16]
        notify_operator(text, f"scope-authorization:{digest}")

    scope_recovery = LarkScopeRecovery(
        app_id=gateway.app_id,
        apply=scope_client.apply_scopes,
        notify=notify_scope_authorization,
        throttle=LarkScopeThrottleStore(
            state_dir / "adapters" / "lark" / "scope-authorization.json"
        ),
    )
    lark_gateway = LarkSdkGateway.from_credentials(
        gateway.app_id,
        secret,
        logger=logger,
        gateway_name=name,
    )
    # ACK-add and reply-cleanup each own a client and a bounded native-effect
    # lane. A blocked ACK endpoint therefore cannot hold reply cleanup behind
    # either the reply client's serialization or a shared reaction lock.
    ack_reaction_gateway = LarkSdkGateway.from_credentials(
        gateway.app_id,
        secret,
        logger=logger,
        gateway_name=name,
    )
    reply_reaction_gateway = LarkSdkGateway.from_credentials(
        gateway.app_id,
        secret,
        logger=logger,
        gateway_name=name,
    )
    configure_recovery = getattr(lark_gateway, "set_permission_recovery", None)
    if callable(configure_recovery):
        configure_recovery(scope_recovery.handle)
    # One shared real-SQLite database for every adapter, namespaced by
    # the adapter column.  The per-adapter JSON-era file (if present) is
    # renamed ``*.retired`` by the store — never read, never deleted.
    state = LarkStateStore(
        state_dir / "adapters.sqlite3",
        adapter=f"lark:{name}",
        legacy_path=state_dir / "adapters" / "lark" / name / "state.sqlite3",
    )

    telemetry_lock = threading.Lock()

    def emit(payload: dict[str, object]) -> None:
        payload = {**payload, "name": name}
        event = str(
            payload.get("event")
            or (
                "lark.adapter.online"
                if payload.get("status") == "online"
                else "lark.inbound.health"
            )
        )
        fields = {
            key: value
            for key, value in payload.items()
            if key not in {"event", "name", "status"}
        }
        logger.log(
            "warn" if payload.get("status") == "warning" else "info",
            event,
            **fields,
        )
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n"
        ready_fd = os.environ.get(READY_FD_ENV_VAR)
        if ready_fd:
            try:
                with telemetry_lock:
                    os.write(int(ready_fd), encoded)
            except OSError:
                pass
        else:
            print(encoded.decode("utf-8").rstrip(), flush=True)

    reaction_effects = ReactionEffectsRuntime(
        state_dir
        / "adapters"
        / "lark"
        / name
        / "reaction-effects.sqlite3",
        ack_effect=lambda native_message_id: ack_reaction_gateway.add_reaction(
            native_message_id, ACK_EMOJI
        ),
        reply_effect=lambda native_message_id: reply_reaction_gateway.clear_reaction(
            native_message_id, ACK_EMOJI
        ),
        overflow_report=emit,
    )

    def resolve_sender_display_name(platform_id: str) -> str | None:
        # Read-only identities lookup (this adapter's namespace) so expanded
        # merge-forward children say who spoke.  Never writes, and never
        # raises: an unanswerable lookup degrades to the raw platform id.
        # The degradation is not silent though: a warning event lands on the
        # worker telemetry stream (same practice as lark.pin.query_failed),
        # because an always-failing lookup -- e.g. an identities schema from
        # before the h2b_owner -> hyprial_owner rename -- otherwise breaks
        # display-name resolution forever with no signal anywhere.
        try:
            for identity in state.find_identities(platform_id=platform_id):
                if identity.display_name:
                    return identity.display_name
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - identity lookup must never break inbound
            emit(
                {
                    "status": "warning",
                    "event": "lark.identity.lookup_failed",
                    "errorType": type(error).__name__,
                    # Local sqlite diagnostic (e.g. "no such column"), not
                    # platform-controlled text; it names the failure's cause.
                    "error": str(error),
                    "fallback": "raw-platform-id",
                }
            )
            return None
        return None

    configure_sender_resolver = getattr(lark_gateway, "set_sender_resolver", None)
    if callable(configure_sender_resolver):
        configure_sender_resolver(resolve_sender_display_name)
    adapter = LarkAdapter(
        state=state,
        lark=lark_gateway,
        reaction_effects=reaction_effects,
        harness=_Harness(socket_path, logger=logger),
        routes=_Routes(
            gateway_name=name,
            socket_path=socket_path,
            report=emit,
        ),
        # New mints use the current adapter: spelling (2026-08-24 naming
        # migration, PAC NAMB efeeaf0a01ba); historical channel:lark:
        # identities already on the wire/in storage keep parsing via
        # lark_reply_adapter's dual-read, never rewritten.
        channel_actor_id=f"adapter:lark:{name}",
        org_context_path=home / "org-context.md",
        logger=logger,
        notify_operator=(
            notify_operator
            if any(route.native_id is not None for route in notification_routes)
            else None
        ),
        dead_letter_alert_threshold=_bounded_integer(
            "HYPRIAL_LARK_DEAD_LETTER_ALERT_THRESHOLD",
            DEFAULT_DEAD_LETTER_ALERT_THRESHOLD,
            minimum=1,
            maximum=MAX_DEAD_LETTERS,
        ),
        # PR #332 F3: inbound forwarding must wait out the daemon's restore
        # gate instead of dead-lettering on the first DAEMON_RESTORING
        # refusal; the budget/backoff bounds keep a wedged restore from
        # pinning the inbound lane forever.
        transient_retry_budget=_bounded_seconds(
            "HYPRIAL_LARK_TRANSIENT_RETRY_BUDGET_SECONDS",
            DEFAULT_TRANSIENT_RETRY_BUDGET_SECONDS,
            minimum=0.0,
            maximum=900.0,
        ),
        transient_retry_backoff_initial=_bounded_seconds(
            "HYPRIAL_LARK_TRANSIENT_RETRY_BACKOFF_INITIAL_SECONDS",
            DEFAULT_TRANSIENT_RETRY_BACKOFF_INITIAL_SECONDS,
            minimum=0.01,
            maximum=60.0,
        ),
        transient_retry_backoff_max=_bounded_seconds(
            "HYPRIAL_LARK_TRANSIENT_RETRY_BACKOFF_MAX_SECONDS",
            DEFAULT_TRANSIENT_RETRY_BACKOFF_MAX_SECONDS,
            minimum=0.01,
            maximum=300.0,
        ),
    )
    control_fd = os.environ.get(CONTROL_FD_ENV_VAR)
    if control_fd:
        threading.Thread(
            target=_run_reply_bridge,
            args=(adapter, int(control_fd)),
            name=f"lark-reply-bridge-{name}",
            daemon=True,
        ).start()

    stream_holder: list[LarkEventStream] = []
    (
        stale_after,
        health_interval,
        probe_timeout,
        reconcile_timeout,
    ) = _recovery_timing_from_environment()
    reconcile_lookback = max(
        DEFAULT_RECONCILE_LOOKBACK_SECONDS,
        required_reconcile_lookback(
            stale_after=stale_after,
            health_interval=health_interval,
            probe_timeout=probe_timeout,
            reconcile_timeout=reconcile_timeout,
        ),
    )

    def rebuild(reason: str) -> None:
        # A fresh worker recreates the SDK client, websocket and event
        # subscription from scratch.  Exiting is more reliable than trying to
        # repair the official client's private asyncio state from this watchdog
        # thread; daemon desired-state reconciliation immediately respawns it.
        _rebuild_after_stale(reason)

    health = LarkStreamHealthMonitor(
        name=name,
        stale_after=stale_after,
        rest_probe=lambda: stream_holder[0].probe_rest_endpoint(),
        reconcile_idle=lambda: reconcile_missed(),
        probe_timeout=probe_timeout,
        reconcile_timeout=reconcile_timeout,
        rebuild=rebuild,
        report=emit,
        monotonic=time.monotonic,
        utcnow=lambda: datetime.now(UTC),
        events=emit,
    )

    def ready() -> None:
        # Signal readiness on the dedicated fd handed over by the daemon so the
        # signal never shares stdout with the Lark SDK's own connection logs.
        # Fall back to stdout only when run outside the daemon (debugging).
        emit({"status": "online"})
        # A rebuilt worker is a reconnect from the platform's perspective.
        # Sweep history on the first successful connection as well, so messages
        # missed while the stale process was alive are redriven idempotently.
        reconnect()

    reconcile_lock = threading.Lock()

    def reconcile_once() -> ReconcileReport | None:
        # Websocket events are not replayed: after a reconnect, pull recent
        # history per chat and re-drive anything missed.  Dedup by native
        # message id inside the adapter keeps this idempotent.
        if not reconcile_lock.acquire(blocking=False):
            return  # a previous sweep is still running
        try:
            return adapter.reconcile_recent(lookback_seconds=reconcile_lookback)
        except (NameError, ImportError):
            raise
        except Exception as error:  # noqa: BLE001 - a failed sweep must never kill the stream
            # ⭐ The load-bearing point: this is where the sweep's exception
            # leaves the world (`return None` below is unchanged).  Naming it
            # here is the only way the class reaches the log -- the two
            # outer discard points never see it.  No text, no locals.
            report_reconcile_failure(emit, error)
            return None
        finally:
            reconcile_lock.release()

    def reconcile_missed() -> int:
        report = reconcile_once()
        return _reconcile_health_result(report)

    reconnect_sweeps = ReconnectSweepCoordinator(
        name=name,
        reconcile=reconcile_once,
        timeout=reconcile_timeout,
        health=health,
        report=emit,
    )

    def reconnect() -> None:
        # The SDK callback remains non-blocking. Reconnect storms coalesce into
        # one active sweep plus one final pass; every pass owns a hard deadline.
        reconnect_sweeps.request()

    stream = LarkEventStream(
        app_id=gateway.app_id,
        app_secret=secret,
        on_message=adapter.handle_sdk_event,
        on_ready=ready,
        on_reconnect=reconnect,
        on_transport_activity=health.transport_activity,
        on_event_activity=health.event_activity,
        on_member_change=adapter.handle_member_change_event,
    )
    stream_holder.append(stream)

    def watch_health() -> None:
        while True:
            time.sleep(health_interval)
            health.check_once()

    threading.Thread(
        target=watch_health, name=f"lark-health-{name}", daemon=True
    ).start()
    try:
        stream.start()
    finally:
        close_adapter = getattr(adapter, "close", None)
        if callable(close_adapter):
            close_adapter()
        reaction_effects.close()
        state.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
