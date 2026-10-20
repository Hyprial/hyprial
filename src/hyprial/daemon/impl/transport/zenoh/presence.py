"""Liveliness presence projection over zenoh declarations.

A session rebuild clears the authoritative projection, replays observer
history, then rechecks only identities that were present before the rebuild.
Bounded native reads restore confirmed identities and leave absent or
unverifiable peers offline.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass


from hyprial.daemon.impl.transport.api import Registration, TransportSample
from hyprial.daemon.impl.transport.keys import KeySpace
from hyprial.daemon.impl.transport.presence_actor import (
    ActorOnlineTransition,
    PresenceAuthority,
    PresenceProjection,
)
from hyprial.daemon.impl.transport.zenoh.transport import ZenohTransport

logger = logging.getLogger(__name__)

_REBUILD_RECONCILE_ATTEMPTS = 5
_REBUILD_RECONCILE_DELAY_SECONDS = 0.4
_REBUILD_RECONCILE_TIMEOUT_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class PresenceObservation:
    key: str
    kind: str
    change: str
    sample_generation: int | None
    presence_generation: int
    transport_generation: int | None
    history_complete: bool | None
    admission: str


class LivelinessDirectory:
    """Materialized actor/mailbox presence from Zenoh liveliness tokens."""

    def __init__(
        self,
        session: ZenohTransport,
        keys: KeySpace | None = None,
        *,
        on_actor_online: Callable[[str], None] | None = None,
        observation_sink: Callable[[PresenceObservation], None] | None = None,
    ) -> None:
        self._keys = keys or KeySpace()
        self._observation_sink = observation_sink
        self._session = session
        self._authority = PresenceAuthority(confirm_present=self._confirm_present)
        self._legacy_online_unbind: Callable[[], None] | None = None
        self._session_generation = getattr(session, "projection", None)
        # A rebuilt session re-learns presence from the replayed observers'
        # history; carrying the old sets over would keep departed peers online.
        self._stop_rebuild_hook: Callable[[], None] = lambda: None
        self._stop_rebuild_complete_hook: Callable[[], None] = lambda: None
        self._carried_presence: tuple[tuple[str, str], ...] = ()
        self._reconcile_guard = threading.Lock()
        self._reconcile_queue: set[tuple[str, str]] = set()
        self._reconcile_running = False
        self._registrations: list[Registration] = []
        try:
            if on_actor_online is not None:
                self.set_actor_online_callback(on_actor_online)
            self._stop_rebuild_hook = session.on_rebuild(self._forget_presence)
            on_rebuild_complete = getattr(session, "on_rebuild_complete", None)
            if callable(on_rebuild_complete):
                self._stop_rebuild_complete_hook = on_rebuild_complete(
                    self._reestablish_presence
                )
            for key, callback in (
                (f"{self._keys.prefix}/liveliness/actor/*", self._actor_event),
                (self._keys.mailbox_liveliness_all(), self._mailbox_event),
            ):
                self._registrations.append(
                    session.observe_liveliness(key, callback, history=True)
                )
        except BaseException as error:
            # The new query worker must not escape a failed constructor.
            # Attempt every owned cleanup, retaining each failure as evidence.
            cleanups = [
                self._stop_rebuild_complete_hook,
                self._stop_rebuild_hook,
            ]
            cleanups.extend(
                registration.close for registration in reversed(self._registrations)
            )
            for cleanup in cleanups:
                try:
                    if cleanup() is False:
                        error.add_note("presence construction rollback did not drain")
                except BaseException as cleanup_error:
                    error.add_note(
                        f"presence construction rollback: {type(cleanup_error).__name__}"
                    )
            try:
                if not self._authority.close():
                    self._authority.stop_after_drain()
                    error.add_note(
                        "presence rollback retains accepted I/O until automatic stop"
                    )
            except BaseException as cleanup_error:
                error.add_note(
                    f"presence owner rollback: {type(cleanup_error).__name__}"
                )
            raise

    def _actor_event(self, sample: TransportSample) -> None:
        self._update("actor", sample)

    def _confirm_present(self, kind: str, identity: str, timeout: float) -> bool:
        key = (
            self._keys.actor_liveliness(identity)
            if kind == "actor"
            else self._keys.mailbox_liveliness(identity)
        )
        samples = self._session.get_liveliness(key, timeout=timeout)
        return any(sample.key == key and sample.kind == "put" for sample in samples)

    def _mailbox_event(self, sample: TransportSample) -> None:
        self._update("mailbox", sample)

    def _update(self, kind: str, sample: TransportSample) -> None:
        generation = self._authority.projection().generation
        transport_projection = None
        if sample.generation is not None and self._session_generation is not None:
            transport_projection = self._session_generation()
            if sample.generation != transport_projection.generation:
                self._observe(kind, sample, generation, transport_projection, "stale")
                return
        identity = self._keys.decode_identity(sample.key.rsplit("/", 1)[-1])
        admission = self._authority.change(
            kind, identity, sample.kind != "delete", generation=generation
        )
        self._observe(kind, sample, generation, transport_projection, admission.value)
        if admission.value != "accepted":
            logger.error(
                "presence admission %s; generation is unknown", admission.value
            )
            # Production callbacks run under TransportSessionAuthority. Surface
            # the failed domain handoff so its existing callback-failure path
            # advances the generation and replays observer history.
            raise RuntimeError(
                f"presence admission {admission.value}; history replay required"
            )

    def _observe(self, kind, sample, generation, transport_projection, admission):
        sink = self._observation_sink
        if sink is None:
            return
        try:
            sink(
                PresenceObservation(
                    str(sample.key)[:512],
                    kind,
                    str(sample.kind),
                    sample.generation,
                    generation,
                    getattr(transport_projection, "generation", None),
                    getattr(transport_projection, "callbacks_complete", None),
                    admission,
                )
            )
        except Exception:
            # Optional bounded diagnostics never invalidate an otherwise valid
            # presence transition. No payload or exception value is exposed.
            pass

    def _forget_presence(self) -> None:
        projection = self._authority.projection()
        self._carried_presence = tuple(
            ("actor", actor) for actor in projection.actors
        ) + tuple(("mailbox", mailbox) for mailbox in projection.mailboxes)
        self._authority.reset()

    def _reestablish_presence(self) -> None:
        """Recheck pre-rebuild presence after callback replay has settled."""

        carried = self._carried_presence
        self._carried_presence = ()
        if not carried:
            return
        with self._reconcile_guard:
            self._reconcile_queue.update(carried)
            if self._reconcile_running:
                return
            self._reconcile_running = True
        threading.Thread(
            target=self._drain_presence_reconcile,
            name="hyprial-presence-rebuild-reconcile",
            daemon=True,
        ).start()

    def _drain_presence_reconcile(self) -> None:
        while True:
            with self._reconcile_guard:
                batch = tuple(self._reconcile_queue)
                self._reconcile_queue.clear()
                if not batch:
                    self._reconcile_running = False
                    return
            restored = 0
            for kind, identity in batch:
                if self._reconcile_confirms(kind, identity):
                    try:
                        self._authority.change(kind, identity, True)
                    except Exception:  # noqa: BLE001 - authority is closing
                        return
                    restored += 1
            logger.info(
                "presence.rebuild.reconciled carried=%d restored=%d",
                len(batch),
                restored,
                extra={
                    "event": "presence.rebuild.reconciled",
                    "carried": len(batch),
                    "restored": restored,
                },
            )

    def _reconcile_confirms(self, kind: str, identity: str) -> bool:
        for attempt in range(_REBUILD_RECONCILE_ATTEMPTS):
            try:
                if self._confirm_present(
                    kind, identity, _REBUILD_RECONCILE_TIMEOUT_SECONDS
                ):
                    return True
            except Exception:  # noqa: BLE001 - an unanswered read stays offline
                pass
            if attempt + 1 < _REBUILD_RECONCILE_ATTEMPTS:
                time.sleep(_REBUILD_RECONCILE_DELAY_SECONDS)
        return False

    def actor_online(self, actor: str) -> bool:
        return (
            self._callbacks_current() and actor in self._authority.projection().actors
        )

    def presence_projection(self) -> PresenceProjection:
        """Immutable generation-fenced projection for online-wake consumers."""

        return self._authority.projection()

    def set_actor_online_callback(self, callback: Callable[[str], None]) -> None:
        """Preserve the dev observer API over committed presence transitions.

        Callbacks must be nonblocking; production binds the typed wake ingress.
        """
        if self._legacy_online_unbind is not None:
            self._legacy_online_unbind()
        self._legacy_online_unbind = self.bind_actor_online(
            lambda transition: callback(transition.actor)
        )

    def bind_actor_online(
        self, observer: Callable[[ActorOnlineTransition], object]
    ) -> Callable[[], None]:
        """Observe committed online transitions, replaying current actors."""

        return self._authority.bind_actor_online(observer)

    def online_actors(self) -> tuple[str, ...]:
        if not self._callbacks_current():
            return ()
        return tuple(sorted(self._authority.projection().actors))

    def observe_actor_online(
        self, observer: Callable[[ActorOnlineTransition], object]
    ) -> Callable[[], None]:
        """Observe committed presence without replacing the recipient wake sink."""
        return self._authority.observe_actor_online(observer)

    def online_mailboxes(self) -> tuple[str, ...]:
        if not self._callbacks_current():
            return ()
        return tuple(sorted(self._authority.projection().mailboxes))

    def _callbacks_current(self) -> bool:
        if self._session_generation is None:
            return True
        projection = self._session_generation()
        return not projection.closed and projection.callbacks_complete

    def close(self) -> None:
        self._authority.close_admission()
        if self._legacy_online_unbind is not None:
            self._legacy_online_unbind()
            self._legacy_online_unbind = None
        self._stop_rebuild_hook()
        self._stop_rebuild_hook = lambda: None
        self._stop_rebuild_complete_hook()
        self._stop_rebuild_complete_hook = lambda: None
        while self._registrations:
            self._registrations[-1].close()
            self._registrations.pop()
        if not self._authority.close():
            raise TimeoutError("presence authority did not drain")
        self._observation_sink = None
