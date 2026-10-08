"""One-process-per-gateway Lark adapter worker."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from hyprial.kernel import Logger
from hyprial.kernel import configured_hyprial_home
from hyprial.kernel import PersistentConfigStore
from hyprial.identity import LazyUserStore

from hyprial.daemon.impl.adapters.lark.inbound.adapter import LarkAdapter
from hyprial.daemon.impl.adapters.lark.inbound.pipeline import (
    DEFAULT_TRANSIENT_RETRY_BACKOFF_MAX_SECONDS,
    DEFAULT_TRANSIENT_RETRY_BACKOFF_INITIAL_SECONDS,
    DEFAULT_TRANSIENT_RETRY_BUDGET_SECONDS,
)
from hyprial.daemon.impl.adapters.lark.inbound.reconcile import DEFAULT_DEAD_LETTER_ALERT_THRESHOLD
from hyprial.daemon.impl.adapters.lark.outbound.reactions import ACK_EMOJI
from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    ReconcileReport,
)
from hyprial.daemon.impl.adapters.lark.runtime.health import (
    DEFAULT_HEALTH_CHECK_INTERVAL_SECONDS,
    DEFAULT_IDLE_RECONCILE_TIMEOUT_SECONDS,
    DEFAULT_RECONCILE_LOOKBACK_SECONDS,
    DEFAULT_REST_PROBE_TIMEOUT_SECONDS,
    DEFAULT_STALE_AFTER_SECONDS,
    LarkStreamHealthMonitor,
    report_reconcile_failure,
    required_reconcile_lookback,
)
from hyprial.daemon.impl.adapters.lark.outbound.reactions import ReactionEffectsRuntime
from hyprial.daemon.impl.adapters.lark.worker.process import (
    CONTROL_FD_ENV_VAR,
    READY_FD_ENV_VAR,
)
from hyprial.daemon.impl.adapters.lark.credentials.scopes import (
    LarkScopeClient,
    LarkScopeRecovery,
    LarkScopeThrottleStore,
)
from hyprial.daemon.impl.adapters.lark.outbound.sdk import LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.outbound.stream import LarkEventStream
from hyprial.daemon.impl.adapters.lark.state.records import MAX_DEAD_LETTERS
from hyprial.daemon.impl.adapters.lark.state.store import LarkStateStore


from hyprial.daemon.impl.adapters.lark.worker.bridge import _run_reply_bridge
from hyprial.daemon.impl.adapters.lark.worker.daemon_link import (
    _Harness,
    _Routes,
)
from hyprial.daemon.impl.adapters.lark.worker.sweep import (
    ReconnectSweepCoordinator,
    STALE_REBUILD_EXIT_CODE,
    _reconcile_health_result,
)
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

def main(arguments: list[str] | None = None) -> int:
    values = sys.argv[1:] if arguments is None else arguments
    if len(values) != 1 or not values[0]:
        raise SystemExit("usage: python -m hyprial.daemon.impl.adapters.lark.worker.entry NAME")
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
        # because an always-failing lookup otherwise breaks display-name
        # resolution forever with no signal anywhere.
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
        users=LazyUserStore(
            state_dir / "users.sqlite3",
            on_open_failure=lambda error: emit(
                {
                    "status": "warning",
                    "event": "lark.users.open_failed",
                    "errorType": type(error).__name__,
                    # Local sqlite diagnostic, not platform-controlled text.
                    "error": str(error),
                    "fallback": "identities-only",
                }
            ),
        ),
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
