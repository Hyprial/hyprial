"""The lark adapter port and its desired-state projection."""

from __future__ import annotations

from __future__ import annotations
from __future__ import annotations
import uuid
import json
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.adapters.lark.contracts.wire import HarnessDelivery
from hyprial.daemon.impl.adapters.lark.ports.ports import (
    AdapterIoCompleted,
    AdapterHealthEventsCompleted,
    AdapterTimerElapsedCommand,
    AdapterMutationCompleted,
    DeliverLarkAlarmCommand,
    DeliverLarkMessageCommand,
    DrainAdapterHealthCommand,
    LarkCommand,
    ReloadAdaptersCommand,
    StartAdapterCommand,
    StopAdapterCommand,
)
from hyprial.daemon.impl.desired_state  import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec

from .events import (
    CorrelatedDomainEvents,
    DomainCommandError,
    _EventT,
)


class LarkDesiredStatePort:
    """Adapter-effect persistence for daemon-restart recovery intent.

    The Adapter actor is the only caller.  Application handlers never mutate
    Lark desired specs independently from the process effect they requested.
    """

    def __init__(self, store: DesiredStateStore) -> None:
        self._store = store

    def activate(self, name: str) -> bool:
        state = self._store.load()
        if any(
            spec.harness == "lark" and spec.name == name
            for spec in state.harnesses
        ):
            return False
        self._store.upsert_harness(
            HarnessLaunchSpec(harness="lark", name=name, headless=True)
        )
        return True

    def deactivate(self, name: str) -> bool:
        state = self._store.load()
        existed = any(
            spec.harness == "lark" and spec.name == name
            for spec in state.harnesses
        )
        if existed:
            self._store.remove_harness("lark", name)
        return existed


class LarkPortClient:
    """System-edge adapter over Lark frozen commands and projections."""

    def __init__(
        self,
        commands: object,
        events: CorrelatedDomainEvents,
        *,
        timeout: float = 15.0,
    ) -> None:
        self._commands = commands
        self._events = events
        self._timeout = timeout

    def call(self, command: LarkCommand, expected: type[_EventT]) -> _EventT:
        admission = self._commands.submit(command)
        if admission is not PortAdmission.ACCEPTED:
            raise DomainCommandError(
                f"PORT_{admission.value.upper()}",
                f"lark command admission is {admission.value}",
            )
        return self._events.wait(
            command.correlation_id, expected, timeout=self._timeout
        )

    def start(self, name: str) -> AdapterMutationCompleted:
        return self.call(
            StartAdapterCommand(f"lark:start:{uuid.uuid4().hex}", name),
            AdapterMutationCompleted,
        )

    def stop(self, name: str) -> AdapterMutationCompleted:
        return self.call(
            StopAdapterCommand(f"lark:stop:{uuid.uuid4().hex}", name),
            AdapterMutationCompleted,
        )

    def reload(self) -> AdapterMutationCompleted:
        return self.call(
            ReloadAdaptersCommand(f"lark:reload:{uuid.uuid4().hex}"),
            AdapterMutationCompleted,
        )

    def timer(self, observed_at_ms: int) -> AdapterMutationCompleted:
        return self.call(
            AdapterTimerElapsedCommand(
                f"lark:timer:{uuid.uuid4().hex}",
                self._commands.generation,
                self._commands.version,
                observed_at_ms,
            ),
            AdapterMutationCompleted,
        )

    def drain_health_events(self) -> tuple[dict[str, object], ...]:
        event = self.call(
            DrainAdapterHealthCommand(f"lark:health:{uuid.uuid4().hex}"),
            AdapterHealthEventsCompleted,
        )
        return tuple(dict(item) for item in event.events)

    def read_adapter(self, name: str) -> object | None:
        return self._commands.read_adapter(name)

    def read_adapters(self) -> tuple[object, ...]:
        return tuple(self._commands.read_adapters())

    def reply_online(self, adapter: str) -> bool:
        projection = self._commands.read_adapter(adapter)
        return bool(projection is not None and projection.online)

    def deliver_reply(self, adapter: str, delivery: HarnessDelivery) -> bool:
        payload = json.dumps(
            {
                "deliveryId": delivery.delivery_id,
                "messageId": delivery.message_id,
                "replyTo": delivery.reply_to,
                "fromActor": {
                    "actorId": delivery.from_actor.actor_id,
                    "actorKey": delivery.from_actor.actor_key,
                    "displayName": delivery.from_actor.display_name,
                },
                "text": delivery.text,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        event = self.call(
            DeliverLarkMessageCommand(
                f"lark:deliver:{delivery.delivery_id}:{uuid.uuid4().hex}",
                adapter,
                delivery.reply_to,
                payload,
            ),
            AdapterIoCompleted,
        )
        return event.succeeded

    def deliver_alarm(
        self,
        adapter: str,
        message_id: str,
        text: str,
        *,
        idempotency_key: str,
    ) -> bool:
        event = self.call(
            DeliverLarkAlarmCommand(
                f"lark:alarm:{message_id}:{uuid.uuid4().hex}",
                adapter,
                message_id,
                text,
                idempotency_key,
            ),
            AdapterIoCompleted,
        )
        return event.succeeded
