"""Per-agent hook configuration and daemon turn-event publication."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hyprial.daemon.impl.api import (
    HarnessDelivery,
    HarnessResult,
    HarnessResultStatus,
)
from hyprial.daemon.impl.harnesses.turn_delivery.turn.turn_ports import (
    TurnDeliveryProjection,
)
from hyprial.kernel import (
    HookBus,
    HookEvent,
    HookRegistration,
    HooksConfig,
    LaneProjection,
    turn_event_payload,
)


_TURN_EVENTS = ("turn.started", "turn.completed", "turn.failed")
HOOK_CONFIG_REFRESH_SECONDS = 1.0
HOOK_CONFIG_REFRESH_CAPACITY = 64
HOOK_CONFIG_ERROR_LOG_INTERVAL_SECONDS = 60.0
HOOK_CONFIG_ERROR_TRACKING_CAPACITY = 256


class HookConsumer(Protocol):
    def observe(self, event: HookEvent, options: Mapping[str, Any]) -> None: ...


HandlerNotifier = Callable[..., None]
TurnCompletedObserver = Callable[..., None]


@dataclass(frozen=True, slots=True)
class _CachedConfig:
    path: Path | None
    signature: tuple[int, int] | None
    config: HooksConfig | None
    dependencies: tuple[_ConfigDependency, ...]
    refresh_after: float


@dataclass(frozen=True, slots=True)
class _ConfigDependency:
    actor: str
    path: Path | None
    signature: tuple[int, int] | None


class _ConsumerLane:
    def __init__(
        self, service: DaemonHookService, name: str, consumer: HookConsumer
    ) -> None:
        self._service = service
        self._name = name
        self._consumer = consumer

    def observe(self, event: HookEvent) -> None:
        for registration in self._service.registrations(event):
            if registration.consumer == self._name:
                self._consumer.observe(event, dict(registration.options))


class _HandlerLane:
    def __init__(self, service: DaemonHookService) -> None:
        self._service = service

    def observe(self, event: HookEvent) -> None:
        document = {
            "event": event.event,
            "schemaVersion": event.schema_version,
            "emittedAtMs": event.emitted_at_ms,
            "seq": event.seq,
            "source": event.source,
            "actor": event.actor,
            "payload": dict(event.payload),
        }
        for registration in self._service.registrations(event):
            if registration.handler is None:
                continue
            try:
                self._service.notify_handler(
                    registration.handler,
                    document,
                    timeout_seconds=registration.timeout_ms / 1_000,
                )
            except Exception as error:  # noqa: BLE001 - hooks never own turns
                self._service.log_handler_failure(event, registration, error)


class DaemonHookService:
    """Route one application-owned bus using reloadable per-agent config."""

    def __init__(
        self,
        *,
        bus: HookBus,
        config_path_for_agent: Callable[[str], Path],
        consumers: Mapping[str, HookConsumer],
        handler_notifier: HandlerNotifier,
        logger: Callable[..., None] | None = None,
        clock_ms: Callable[[], int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bus = bus
        self._config_path_for_agent = config_path_for_agent
        self._consumers = dict(consumers)
        self.notify_handler = handler_notifier
        self._logger = logger
        self._clock_ms = clock_ms or (lambda: time.time_ns() // 1_000_000)
        self._monotonic = monotonic
        self._sequence = 0
        self._sequence_lock = threading.Lock()
        self._configs: dict[str, _CachedConfig] = {}
        self._config_refreshing: set[str] = set()
        self._config_error_logged_at: dict[tuple[str, str], float] = {}
        self._config_lock = threading.Lock()
        self._handler_lane_lock = threading.Lock()
        self._handler_lane_registered = False
        for name, consumer in sorted(self._consumers.items()):
            bus.register(
                f"consumer:{name}",
                _ConsumerLane(self, name, consumer),
                _TURN_EVENTS,
            )

    def observe_started(
        self, delivery: TurnDeliveryProjection, *, started_at_ms: int
    ) -> None:
        if delivery.hook_request or not self._has_route(
            delivery.recipient, "turn.started"
        ):
            return
        self._publish(
            event="turn.started",
            actor=delivery.recipient,
            payload=turn_event_payload(
                delivery_id=delivery.delivery_id,
                sender=delivery.sender,
                conversation=delivery.conversation_id,
                started_at_ms=started_at_ms,
                ended_at_ms=started_at_ms,
                status="started",
                tool_names=(),
                output="",
                prompt=delivery.message,
                tool_calls=(),
                fidelity="observed",
            ),
            emitted_at_ms=started_at_ms,
        )

    def observe_turn(
        self,
        delivery: HarnessDelivery,
        result: HarnessResult,
        *,
        started_at_ms: int,
        ended_at_ms: int,
        tool_names: tuple[str, ...] = (),
    ) -> None:
        if delivery.hook_request:
            return
        completed = result.status is HarnessResultStatus.COMPLETED
        event = "turn.completed" if completed else "turn.failed"
        if not self._has_route(delivery.recipient, event):
            return
        self._publish(
            event=event,
            actor=delivery.recipient,
            payload=turn_event_payload(
                delivery_id=delivery.delivery_id,
                sender=delivery.sender,
                conversation=delivery.conversation_id,
                started_at_ms=started_at_ms,
                ended_at_ms=ended_at_ms,
                status=result.status.value,
                tool_names=tool_names,
                output=result.output,
                prompt=delivery.message,
                tool_calls=tuple((name, completed) for name in tool_names),
                failure_code=(
                    result.failure_code
                    if result.status is HarnessResultStatus.FAILED
                    else None
                ),
                fidelity="inferred" if tool_names else "observed",
            ),
            emitted_at_ms=ended_at_ms,
        )

    def registrations(self, event: HookEvent) -> tuple[HookRegistration, ...]:
        actor = event.actor
        if actor is None:
            return ()
        config = self._configuration(actor)
        if config is None:
            return ()
        return tuple(
            registration
            for registration in config.hooks
            if registration.mode == "observe" and event.event in registration.events
        )

    def _has_route(self, actor: str, event: str) -> bool:
        config = self._configuration(actor)
        if config is None:
            return False
        registrations = tuple(
            registration
            for registration in config.hooks
            if registration.mode == "observe" and event in registration.events
        )
        if any(registration.handler is not None for registration in registrations):
            self._ensure_handler_lane()
        return bool(registrations)

    def _ensure_handler_lane(self) -> None:
        with self._handler_lane_lock:
            if self._handler_lane_registered:
                return
            try:
                self._bus.register(
                    "agent-handlers",
                    _HandlerLane(self),
                    _TURN_EVENTS,
                )
            except RuntimeError:
                # Shutdown may close the bus while a final turn settles. Hook
                # routing never changes that turn's outcome.
                return
            self._handler_lane_registered = True

    def projection(self) -> tuple[LaneProjection, ...]:
        return self._bus.projection()

    def _publish(
        self,
        *,
        event: str,
        actor: str,
        payload: Mapping[str, Any],
        emitted_at_ms: int,
    ) -> None:
        with self._sequence_lock:
            sequence = self._sequence
            self._sequence += 1
        self._bus.publish(
            HookEvent(
                event=event,
                kind="observe",
                emitted_at_ms=emitted_at_ms,
                seq=sequence,
                source="daemon.turns",
                actor=actor,
                payload=payload,
            )
        )

    def _configuration(self, actor: str) -> HooksConfig | None:
        now = self._monotonic()
        schedule_refresh = False
        with self._config_lock:
            cached = self._configs.get(actor)
            if (
                (cached is None or now >= cached.refresh_after)
                and actor not in self._config_refreshing
                and len(self._config_refreshing) < HOOK_CONFIG_REFRESH_CAPACITY
            ):
                self._config_refreshing.add(actor)
                schedule_refresh = True
            config = cached.config if cached is not None else None
        if schedule_refresh:
            thread = threading.Thread(
                target=self._refresh_configuration,
                args=(actor,),
                name="hook-config-refresh",
                daemon=True,
            )
            try:
                thread.start()
            except RuntimeError as error:
                with self._config_lock:
                    self._config_refreshing.discard(actor)
                self._log_config_invalid(actor, error)
        return config

    def _refresh_configuration(self, actor: str) -> None:
        try:
            self._load_configuration(actor)
        finally:
            with self._config_lock:
                self._config_refreshing.discard(actor)

    def _load_configuration(self, actor: str) -> None:
        try:
            path = self._config_path_for_agent(actor)
        except Exception as error:  # noqa: BLE001 - hook config fails closed
            self._log_config_invalid(actor, error)
            self._cache_configuration(actor, None, None, None, ())
            return
        try:
            stat = path.stat()
        except FileNotFoundError:
            self._cache_configuration(actor, path, None, None, ())
            return
        except OSError as error:
            self._log_config_invalid(actor, error)
            self._cache_configuration(actor, path, None, None, ())
            return
        signature = (stat.st_mtime_ns, stat.st_size)
        with self._config_lock:
            cached = self._configs.get(actor)
        if (
            cached is not None
            and cached.path == path
            and cached.signature == signature
            and self._dependencies_unchanged(cached.dependencies)
        ):
            self._cache_configuration(
                actor,
                path,
                signature,
                cached.config,
                cached.dependencies,
            )
            return
        try:
            value = json.loads(path.read_text("utf-8"))
            config, dependencies = self._parse_configuration(actor, value)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
            self._log_config_invalid(actor, error)
            config = None
            dependencies = ()
        self._cache_configuration(actor, path, signature, config, dependencies)

    def _cache_configuration(
        self,
        actor: str,
        path: Path | None,
        signature: tuple[int, int] | None,
        config: HooksConfig | None,
        dependencies: tuple[_ConfigDependency, ...],
    ) -> None:
        refresh_after = self._monotonic() + HOOK_CONFIG_REFRESH_SECONDS
        with self._config_lock:
            self._configs[actor] = _CachedConfig(
                path,
                signature,
                config,
                dependencies,
                refresh_after,
            )

    def _parse_configuration(
        self, actor: str, value: object
    ) -> tuple[HooksConfig, tuple[_ConfigDependency, ...]]:
        if not isinstance(value, dict) or value.get("schemaVersion") != 2:
            raise ValueError("hooks config requires schemaVersion 2")
        raw_hooks = value.get("hooks", [])
        if not isinstance(raw_hooks, list):
            raise ValueError("hooks config 'hooks' must be a list")
        hooks: list[HookRegistration] = []
        dependencies: dict[str, _ConfigDependency] = {}
        for index, item in enumerate(raw_hooks):
            try:
                parsed = HooksConfig.from_json(
                    {"schemaVersion": 2, "hooks": [item]}
                ).hooks[0]
                self._validate_registration(actor, parsed, dependencies)
            except (IndexError, ValueError) as error:
                self._log_config_invalid(
                    actor,
                    ValueError(f"hooks[{index}] disabled: {error}"),
                )
                continue
            hooks.append(parsed)
        return HooksConfig(tuple(hooks)), tuple(
            dependencies[key] for key in sorted(dependencies)
        )

    def _validate_registration(
        self,
        actor: str,
        registration: HookRegistration,
        dependencies: dict[str, _ConfigDependency],
    ) -> None:
        if registration.consumer is not None:
            if registration.consumer not in self._consumers:
                raise ValueError(
                    f"hook consumer {registration.consumer!r} is not registered"
                )
            return
        handler = registration.handler
        assert handler is not None
        if handler == actor:
            raise ValueError("hook handler must differ from the emitting agent")
        points_back, dependency = self._handler_points_back(handler, actor)
        dependencies[handler] = dependency
        if points_back:
            raise ValueError("hook handler configuration points back to the agent")

    def _handler_points_back(
        self, handler: str, actor: str
    ) -> tuple[bool, _ConfigDependency]:
        try:
            path = self._config_path_for_agent(handler)
        except Exception:  # noqa: BLE001 - remote handlers have no local path
            return False, _ConfigDependency(handler, None, None)
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_size)
            value = json.loads(path.read_text("utf-8"))
        except Exception:  # noqa: BLE001 - absent handler config is not a loop
            return False, _ConfigDependency(handler, path, None)
        dependency = _ConfigDependency(handler, path, signature)
        if not isinstance(value, dict) or value.get("schemaVersion") != 2:
            return False, dependency
        raw_hooks = value.get("hooks", [])
        if not isinstance(raw_hooks, list):
            return False, dependency
        for item in raw_hooks:
            try:
                registrations = HooksConfig.from_json(
                    {"schemaVersion": 2, "hooks": [item]}
                ).hooks
            except ValueError:
                continue
            if registrations and registrations[0].handler == actor:
                return True, dependency
        return False, dependency

    def _dependencies_unchanged(
        self, dependencies: tuple[_ConfigDependency, ...]
    ) -> bool:
        return all(
            self._config_dependency(dependency.actor) == dependency
            for dependency in dependencies
        )

    def _config_dependency(self, actor: str) -> _ConfigDependency:
        try:
            path = self._config_path_for_agent(actor)
        except Exception:  # noqa: BLE001 - dependency stays unresolved
            return _ConfigDependency(actor, None, None)
        try:
            stat = path.stat()
        except OSError:
            return _ConfigDependency(actor, path, None)
        return _ConfigDependency(actor, path, (stat.st_mtime_ns, stat.st_size))

    def _log_config_invalid(self, actor: str, error: BaseException) -> None:
        now = self._monotonic()
        key = (actor, type(error).__name__)
        with self._config_lock:
            expired = tuple(
                candidate
                for candidate, logged_at in self._config_error_logged_at.items()
                if now - logged_at >= HOOK_CONFIG_ERROR_LOG_INTERVAL_SECONDS
            )
            for candidate in expired:
                self._config_error_logged_at.pop(candidate, None)
            previous = self._config_error_logged_at.get(key)
            if previous is not None:
                return
            if (
                len(self._config_error_logged_at)
                >= HOOK_CONFIG_ERROR_TRACKING_CAPACITY
            ):
                oldest = min(
                    self._config_error_logged_at,
                    key=self._config_error_logged_at.__getitem__,
                )
                self._config_error_logged_at.pop(oldest, None)
            self._config_error_logged_at[key] = now
        if self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "hooks.config_invalid",
                actor=actor,
                errorType=type(error).__name__,
                detail=str(error)[:300],
            )

    def log_handler_failure(
        self,
        event: HookEvent,
        registration: HookRegistration,
        error: BaseException,
    ) -> None:
        if self._logger is not None:
            self._logger(
                "warn",
                "daemon",
                "hooks.handler_failed",
                actor=event.actor,
                handler=registration.handler,
                event=event.event,
                errorType=type(error).__name__,
            )


class CombinedTurnObserver:
    """Keep the H1 bus additive while the legacy recap service stays in place."""

    def __init__(
        self, legacy: TurnCompletedObserver, hooks: DaemonHookService
    ) -> None:
        self._legacy = legacy
        self._hooks = hooks

    def __call__(
        self,
        delivery: HarnessDelivery,
        result: HarnessResult,
        *,
        started_at_ms: int,
        ended_at_ms: int,
        tool_names: tuple[str, ...],
    ) -> None:
        self._legacy(
            delivery,
            result,
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            tool_names=tool_names,
        )
        self._hooks.observe_turn(
            delivery,
            result,
            started_at_ms=started_at_ms,
            ended_at_ms=ended_at_ms,
            tool_names=tool_names,
        )

    def turn_started(
        self, delivery: TurnDeliveryProjection, *, started_at_ms: int
    ) -> None:
        self._hooks.observe_started(delivery, started_at_ms=started_at_ms)


__all__ = [
    "HOOK_CONFIG_ERROR_TRACKING_CAPACITY",
    "HOOK_CONFIG_ERROR_LOG_INTERVAL_SECONDS",
    "HOOK_CONFIG_REFRESH_CAPACITY",
    "HOOK_CONFIG_REFRESH_SECONDS",
    "CombinedTurnObserver",
    "DaemonHookService",
]
