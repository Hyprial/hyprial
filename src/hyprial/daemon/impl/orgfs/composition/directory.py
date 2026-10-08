from __future__ import annotations

from collections.abc import Callable


import json
import random




import time

from typing import Any

import uuid

import weakref

from hyprial.kernel import (
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    AdmissionResult)

from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from hyprial.daemon.impl.transport import KeySpace, TransportSample, TransportSession

from hyprial.daemon.impl.transport.presence_actor import ActorOnlineTransition

from hyprial.daemon.impl.orgfs.api  import OrgFsError


from hyprial.daemon.impl.orgfs.projection.checkout_authority  import CheckoutAuthority


from hyprial.daemon.impl.orgfs.network.protocol  import (
    ORGFS_ANNOUNCE_BUFFER_LIMIT,
    ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT,
    ORGFS_DIRECTORY_SYNC_INTERVAL_SECONDS,
    ORGFS_DIRECTORY_SYNC_JITTER_RATIO)

from hyprial.identity import is_org_acl_space, is_org_directory_space






from hyprial.daemon.impl.orgfs.composition.vocabulary import _DirectoryCommand, _DirectoryEffect, _DirectoryEffectBatch


def _directory_sync_jitter() -> float:
    """A per-deadline spread in [-ratio, +ratio] (patched to 0 in exact-boundary tests)."""

    return random.uniform(
        -ORGFS_DIRECTORY_SYNC_JITTER_RATIO, ORGFS_DIRECTORY_SYNC_JITTER_RATIO
    )


def _next_directory_sync_due(now: float) -> float:
    return now + ORGFS_DIRECTORY_SYNC_INTERVAL_SECONDS * (1.0 + _directory_sync_jitter())

class RuntimeDirectory:
    """Responsibility methods on the sole OrgFsRuntime state host.

    This class never constructs, copies, or persists an independent host.
    """

    @staticmethod
    def _directory_mailbox_capacity() -> int:
        """Reserve the overflow envelope beside the announcement budget."""
        return ORGFS_ANNOUNCE_BUFFER_LIMIT + 1

    @staticmethod
    def _new_directory_sync_offset() -> int:
        """Give every runtime a different starting point in holder rotations."""

        return random.randrange(1 << 63)

    @property
    def directory_ingress_dropped(self) -> int:
        return self._directory_dropped


    def _start_directory_ingress(self) -> None:
        if self._directory_effects is not None:
            return
        owner_ref = weakref.ref(self)

        def handle_command(command: object) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_directory_command(command)

        def execute_effect(effect: _DirectoryEffectBatch) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._execute_directory_effect(effect)

        runtime = ActorRuntime()
        handle = runtime.start(
            ActorSpec(
                "orgfs-directory-ingress",
                lambda: handle_command,
                # The custody bound includes one overflow envelope so the
                # state owner can evict/report a full retained buffer. Admit
                # that same bounded envelope even before the worker dequeues.
                mailbox_capacity=self._directory_capacity,
                supervision_profile="state_authority",
            )
        )
        self._directory_runtime = runtime
        self._directory_actor = handle
        self._directory_effects = EffectLane(
            name="orgfs-directory-effects",
            execute=execute_effect,
            complete=lambda completion: runtime.tell(handle, completion),
            capacity=ORGFS_ANNOUNCE_BUFFER_LIMIT,
            workers=1,
        )


    @property
    def directory_ingress_pending(self) -> int:
        with self._directory_custody_lock:
            return len(self._directory_pending)


    def _release_directory_credit(self, operation_id: str, generation: int) -> None:
        pending: tuple[ActorRuntime, ActorHandle, _DirectoryCommand] | None = None
        with self._directory_custody_lock:
            self._directory_pending.discard((operation_id, generation))
            runtime = self._directory_runtime
            actor = self._directory_actor
            if (
                self._directory_rebuild_pending
                and runtime is not None
                and actor is not None
                and len(self._directory_pending) < self._directory_capacity
            ):
                command = _DirectoryCommand(
                    uuid.uuid4().hex,
                    self._directory_generation,
                    "rebuild-complete",
                    TransportSample("transport/rebuild", b""),
                )
                self._directory_pending.add(
                    (command.operation_id, command.generation)
                )
                self._directory_rebuild_pending = False
                pending = (runtime, actor, command)
        if pending is None:
            return
        runtime, actor, command = pending
        if runtime.tell(actor, command) is AdmissionResult.ACCEPTED:
            return
        with self._directory_custody_lock:
            self._directory_pending.discard(
                (command.operation_id, command.generation)
            )
            if not self._directory_closing:
                self._directory_rebuild_pending = True
            else:
                self._directory_dropped += 1


    def _admit_directory_sample(self, action: str, sample: TransportSample) -> None:
        runtime = self._directory_runtime
        actor = self._directory_actor
        with self._directory_custody_lock:
            if (runtime is None or actor is None or self._directory_closing
                or len(self._directory_pending) >= self._directory_capacity):
                if (
                    action == "rebuild-complete"
                    and runtime is not None
                    and actor is not None
                    and not self._directory_closing
                ):
                    self._directory_rebuild_pending = True
                else:
                    self._directory_dropped += 1
                return
            command = _DirectoryCommand(
                uuid.uuid4().hex, self._directory_generation, action, sample
            )
            token = (command.operation_id, command.generation)
            self._directory_pending.add(token)
            if runtime.tell(actor, command) is not AdmissionResult.ACCEPTED:
                self._directory_pending.discard(token)
                if action == "rebuild-complete":
                    self._directory_rebuild_pending = True
                else:
                    self._directory_dropped += 1


    def _on_directory_command(self, command: object) -> None:
        if isinstance(command, EffectCompleted):
            effects = self._directory_effects
            if effects is not None:
                if effects.acknowledge(command.operation_id, command.generation):
                    self._release_directory_credit(command.operation_id, command.generation)
                self._pump_directory_effects()
            if command.error is not None:
                self._directory_dropped += 1
            return
        if not isinstance(command, _DirectoryCommand):
            raise TypeError("orgfs directory ingress received an invalid command")
        lane = self._directory_effects
        if lane is None or command.generation != self._directory_generation:
            self._directory_dropped += 1
            self._release_directory_credit(command.operation_id, command.generation)
            return
        if command.action == "announce":
            effects = self._apply_announcement_command(command.sample)
        elif command.action == "liveliness":
            effects = self._apply_liveliness_command(command.sample)
        elif command.action == "supplier-online":
            peer = self._host_peer_from_liveliness(command.sample)
            effects = []
            if peer and peer != self.node_id:
                if self._supplier_online(peer):
                    with self._pending_announces_lock:
                        self._deferred_supplier_online.discard(peer)
                        pending = self._pending_announces.pop(peer, None)
                    # An announce buffered while the peer was offline names the
                    # spaces it holds; without it the holder filter below skips them.
                    if pending is not None:
                        self._apply_announce(peer, pending)
                    effects = self._supplier_sync_effects(peer)
                else:
                    self._defer_supplier_online(peer)
        elif command.action == "rebuild-complete":
            effects = self._post_rebuild_effects()
        else:
            self._directory_dropped += 1
            self._release_directory_credit(command.operation_id, command.generation)
            return
        if not effects:
            self._release_directory_credit(command.operation_id, command.generation)
            return
        batch = _DirectoryEffectBatch(tuple(effects))
        request = EffectRequest(command.operation_id, command.generation, batch)
        if not any(item.operation_id == request.operation_id for item in self._directory_deferred):
            self._directory_deferred.append(request)
        self._pump_directory_effects()


    def _pump_directory_effects(self) -> None:
        lane = self._directory_effects
        if lane is None:
            return
        while self._directory_deferred:
            request = self._directory_deferred[0]
            admission = lane.submit(request)
            if admission is AdmissionResult.OVERLOADED:
                return
            self._directory_deferred.pop(0)
            if admission is AdmissionResult.CLOSED:
                self._directory_dropped += 1
                self._release_directory_credit(request.operation_id, request.generation)


    def _apply_announcement_command(
        self, sample: TransportSample
    ) -> list[_DirectoryEffect]:
        try:
            value = json.loads(sample.payload)
            prefix = f"{KeySpace().prefix}/org/fs/announce/"
            if not sample.key.startswith(prefix):
                return []
            node = sample.key[len(prefix) :]
            if (
                not isinstance(value, dict)
                or value.get("schemaVersion") != 1
                or value.get("type") != "orgfs-announce"
                or value.get("node") != node
                or not node
                or "/" in node
                or not isinstance(value.get("spaces"), list)
            ):
                return []
            with self._pending_announces_lock:
                observed_live = node in self._lively_peers
            if not observed_live and not self._supplier_online(node):
                diagnostics: list[dict[str, object]] = []
                self._buffer_announce(node, value, diagnostics=diagnostics)
                return [
                    _DirectoryEffect(
                        "log",
                        details=tuple(sorted(fields.items())),
                    )
                    for fields in diagnostics
                ]
            with self._pending_announces_lock:
                self._pending_announces.pop(node, None)
            newly_held = self._apply_announce(node, value)
            # A live peer whose online edge skipped a space (it was not a
            # known holder yet) pulls that space once its announce lands.
            with self._pending_announces_lock:
                catch_up = tuple(
                    space_id
                    for space_id in sorted(newly_held)
                    if (space_id, node) in self._edge_skipped_holdings
                )
                self._edge_skipped_holdings.difference_update(
                    (space_id, node) for space_id in catch_up
                )
            if not catch_up:
                return []
            return [_DirectoryEffect("sync", peer=node, space_ids=catch_up)]
        except Exception:
            self._directory_dropped += 1
            return []


    def _apply_liveliness_command(
        self, sample: TransportSample
    ) -> list[_DirectoryEffect]:
        peer, pending = self._settle_peer_liveliness(sample)
        if not peer or peer == self.node_id:
            return []
        if sample.kind == "delete":
            return []
        if pending is not None:
            self._apply_announce(peer, pending)
        effects = [_DirectoryEffect("announce")]
        checkout_spaces = tuple(self._checkouts)
        if checkout_spaces:
            effects.append(_DirectoryEffect("reconcile", space_ids=checkout_spaces))
        if not self._sync_after_presence:
            effects.extend(self._supplier_sync_effects(peer))
        return effects


    def _supplier_sync_effects(self, peer: str) -> list[_DirectoryEffect]:
        sync_spaces: list[str] = []
        for space_id in self.stores.snapshot_ids():
            with self._pending_announces_lock:
                # This node's own announce is not evidence about the peer: a
                # restarted node that announced first must still pull.
                holders = frozenset(self._holders.get(space_id, ())) - {self.node_id}
                if holders and peer not in holders:
                    self._edge_skipped_holdings.add((space_id, peer))
                    continue
            sync_spaces.append(space_id)
        if sync_spaces:
            return [
                _DirectoryEffect("sync", peer=peer, space_ids=tuple(sync_spaces))
            ]
        return []


    def _defer_supplier_online(self, peer: str) -> None:
        with self._pending_announces_lock:
            if peer in self._deferred_supplier_online:
                return
            if len(self._deferred_supplier_online) >= ORGFS_ANNOUNCE_BUFFER_LIMIT:
                self._directory_dropped += 1
                return
            self._deferred_supplier_online.add(peer)


    def _post_rebuild_effects(self) -> list[_DirectoryEffect]:
        with self._pending_announces_lock:
            deferred = tuple(sorted(self._deferred_supplier_online))
        online_deferred = tuple(
            peer for peer in deferred if self._supplier_online(peer)
        )
        with self._pending_announces_lock:
            self._deferred_supplier_online.difference_update(online_deferred)
        with self._resource_lock:
            resident_spaces = tuple(sorted(self._replicas))
        targets = {
            (space_id, effect.peer)
            for peer in online_deferred
            for effect in self._supplier_sync_effects(peer)
            for space_id in effect.space_ids
            if effect.peer is not None
        }
        holder_pairs = set()
        for space_id in resident_spaces:
            with self._pending_announces_lock:
                holders = tuple(self._holders.get(space_id, ()))
            for peer in holders:
                if peer != self.node_id and self._supplier_online(peer):
                    holder_pairs.add((space_id, peer))
        targets.update(holder_pairs)
        by_peer: dict[str, list[str]] = {}
        for space_id, peer in sorted(targets):
            by_peer.setdefault(peer, []).append(space_id)
        effects = [
            _DirectoryEffect("post-rebuild-sync", peer=peer, space_ids=tuple(spaces))
            for peer, spaces in sorted(by_peer.items())
        ]
        effects.append(_DirectoryEffect(
            "post-rebuild-log",
            details=tuple(sorted({
                "deferredPeers": len(deferred),
                "onlineHolderPairs": len(holder_pairs),
                "residentSpaces": len(resident_spaces),
                "syncTargets": len(targets),
            }.items())),
        ))
        return effects


    def anti_entropy_tick(self, now: float) -> int:
        """Schedule one bounded pull per online org space when due.

        Never raises: daemon maintenance runs other phases after this one, so a
        failure is logged as ``orgfs.anti_entropy.failed`` and counts as zero.
        """

        try:
            return self._anti_entropy_tick(now)
        except Exception as exc:  # noqa: BLE001 - must not skip later maintenance
            if self.logger is not None:
                self.logger("warn", "orgfs.anti_entropy.failed", error=repr(exc))
            return 0

    def _anti_entropy_tick(self, now: float) -> int:

        with self._pending_announces_lock:
            due = self._directory_sync_due
            if due is None:
                self._directory_sync_due = _next_directory_sync_due(now)
                return 0
            if now < due:
                return 0
            self._directory_sync_due = _next_directory_sync_due(now)

        excluded_orgs = self._excluded_orgs()
        targets: dict[str, str] = {}
        for space in self.facade.spaces():
            is_directory = is_org_directory_space(space.name)
            is_acl = is_org_acl_space(space.name)
            if not (is_directory or is_acl):
                continue
            org = space.name if is_directory else space.name.removesuffix("-acl")
            if org in excluded_orgs:
                continue
            with self._pending_announces_lock:
                holders = tuple(sorted(
                    peer
                    for peer in self._holders.get(space.space_id, ())
                    if peer != self.node_id
                ))
            online = tuple(peer for peer in holders if self._supplier_online(peer))
            if not online:
                continue
            with self._pending_announces_lock:
                last = self._directory_sync_last.get(space.space_id)
                peer = (
                    online[self._directory_sync_offset % len(online)]
                    if last is None
                    else next((name for name in online if name > last), online[0])
                )
                self._directory_sync_last[space.space_id] = peer
            targets[space.space_id] = peer

        # The periodic path alone is linear. Presence edges, announce catch-up,
        # and post-rebuild catch-up remain eager, so a join or restart already
        # receives a full pull without duplicating that work in this tick.
        # Each space remembers the NAME of the last holder it pulled from and
        # takes the first online holder after it in sorted order (wrapping).
        # A holder that stays online cannot be stepped over, so it is reached
        # within one turn of the names however the others flap.  An integer
        # cursor cannot give that: a liveliness delete removes a peer from
        # ``_holders`` (``_settle_peer_liveliness``), so any index shifts with
        # the flapping and A-online-on-even / Z-on-odd ticks starve an
        # always-online H.  Worst case for one row on one holder: k holders,
        # one per due tick of 60 s +20% jitter, about (k-1) x 72 s.
        scheduled = 0
        for space_id, peer in sorted(targets.items()):
            mesh = self._mesh(space_id)
            if mesh is not None and mesh.schedule_sync_from(peer):
                scheduled += 1
        return scheduled


    def _execute_directory_effect(self, batch: _DirectoryEffectBatch) -> None:
        scheduled_count = 0
        for effect in batch.effects:
            if effect.action == "announce":
                self._announce_all()
            elif effect.action == "reconcile":
                for space_id in effect.space_ids:
                    with self._checkout_lock:
                        checkout = self._checkouts.get(space_id)
                    if checkout is None:
                        continue
                    self._request_checkout_reconcile(checkout)
            elif effect.action == "sync" and effect.peer is not None:
                for space_id in effect.space_ids:
                    mesh = self._mesh(space_id)
                    if mesh is not None:
                        mesh.schedule_sync_from(effect.peer)
            elif effect.action == "post-rebuild-sync" and effect.peer is not None:
                for space_id in effect.space_ids:
                    mesh = self._mesh(space_id)
                    if mesh is not None and mesh.schedule_sync_from(effect.peer):
                        scheduled_count += 1
            elif effect.action == "post-rebuild-log" and self.logger is not None:
                self.logger(
                    "info",
                    "orgfs.sync.post-rebuild",
                    **dict(effect.details),
                    scheduledCount=scheduled_count,
                )
            elif effect.action == "log" and self.logger is not None:
                self.logger("warn", "orgfs.announce.buffer-dropped", **dict(effect.details))


    @staticmethod
    def _request_checkout_reconcile(checkout: Any) -> None:
        if isinstance(checkout, CheckoutAuthority):
            checkout.request_reconcile()
        else:
            # Test/offline protocol adapters may expose only the frozen legacy
            # reconcile seam; production entries are always CheckoutAuthority.
            checkout.reconcile()


    def _close_directory_ingress(self, timeout: float = 5.0) -> bool:
        with self._directory_custody_lock:
            self._directory_closing = True
        runtime = self._directory_runtime
        actor = self._directory_actor
        effects = self._directory_effects
        if runtime is None or actor is None or effects is None:
            return True
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snapshot = runtime.snapshot(actor)
            if (
                snapshot.queued == 0
                and snapshot.in_flight == 0
                and not self._directory_deferred
                and self.directory_ingress_pending == 0
                and not self._directory_rebuild_pending
            ):
                break
            time.sleep(0.005)
        else:
            return False
        if not effects.close(max(0.0, deadline - time.monotonic())):
            return False
        self._directory_generation += 1
        stopped = runtime.stop(actor, timeout=max(0.0, deadline - time.monotonic()))
        if stopped:
            self._directory_runtime = None
            self._directory_actor = None
            self._directory_effects = None
        return stopped


    def bind_transport(
        self,
        session: TransportSession,
        *,
        supplier_online: Callable[[str], bool] | None = None,
        holder_candidates: Callable[[], tuple[str, ...]] | None = None,
        observe_supplier_online: Callable[
            [Callable[[ActorOnlineTransition], object]], Callable[[], None]
        ] | None = None,
    ) -> None:
        with self._resource_lock:
            if self._closing:
                raise OrgFsError("unavailable", {"message": "orgfs runtime is closing"})
            self._session = session
        if supplier_online is not None:
            self._supplier_online = supplier_online
        if holder_candidates is not None:
            self._holder_candidates = holder_candidates
        self._sync_after_presence = observe_supplier_online is not None
        self._start_directory_ingress()
        self._announce_registration = session.subscribe(
            f"{KeySpace().prefix}/org/fs/announce/*",
            lambda sample: self._admit_directory_sample("announce", sample),
        )
        observe_liveliness = getattr(session, "observe_liveliness", None)
        if callable(observe_liveliness):
            self._liveliness_registration = observe_liveliness(
                f"{KeySpace().prefix}/liveliness/actor/*",
                lambda sample: self._admit_directory_sample("liveliness", sample),
                history=True,
            )
        for space_id in self.stores.snapshot_ids():
            self._mesh(space_id)
        if observe_supplier_online is not None:
            # Subscribe after restored meshes exist. The committed projection
            # is replayed at binding, covering presence settled during startup.
            # Raw liveliness still maintains holder/delete state, but only this
            # committed wake may launch catch-up when a presence gate is wired.
            self._stop_supplier_online = observe_supplier_online(
                self._supplier_became_online
            )
        on_rebuild_complete = getattr(session, "on_rebuild_complete", None)
        if callable(on_rebuild_complete):
            self._stop_rebuild_complete = on_rebuild_complete(
                self._transport_rebuild_completed
            )


    def _supplier_became_online(self, transition: ActorOnlineTransition) -> None:
        # Presence replays agents as well as hosts. Discard unrelated identities
        # before they can consume directory credit needed by real announces.
        if transition.actor == self.node_id or not self._is_host_peer(transition.actor):
            return
        self._admit_directory_sample(
            "supplier-online",
            TransportSample(KeySpace().actor_liveliness(transition.actor), b""),
        )


    def _transport_rebuild_completed(self) -> None:
        self._admit_directory_sample(
            "rebuild-complete", TransportSample("transport/rebuild", b"")
        )


    def _on_peer_liveliness(self, sample: TransportSample) -> None:
        self._process_peer_liveliness(sample)


    def _settle_peer_liveliness(
        self, sample: TransportSample
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Track host presence and settle holder hints on liveliness delete."""
        peer = self._host_peer_from_liveliness(sample)
        if not peer or peer == self.node_id:
            return None, None
        with self._pending_announces_lock:
            if sample.kind == "delete":
                self._lively_peers.discard(peer)
                for holders in self._holders.values():
                    holders.discard(peer)
                return peer, None
            self._lively_peers.add(peer)
            return peer, self._pending_announces.pop(peer, None)


    @staticmethod
    def _host_peer_from_liveliness(sample: TransportSample) -> str | None:
        prefix = f"{KeySpace().prefix}/liveliness/actor/"
        if not sample.key.startswith(prefix):
            return None
        peer = KeySpace().decode_identity(sample.key[len(prefix) :])
        return peer if RuntimeDirectory._is_host_peer(peer) else None


    @staticmethod
    def _is_host_peer(peer: str) -> bool:
        # Import here to avoid making the orgfs module initialize the daemon
        # package while DaemonApplication is importing OrgFsRuntime.
        from hyprial.daemon.impl.configuration.identity import classify_target_identity
        from hyprial.kernel import TARGET_KIND_HOST

        return classify_target_identity(peer) == TARGET_KIND_HOST

    def _process_peer_liveliness(self, sample: TransportSample) -> None:
        """F5/F6: admit buffered discovery and schedule bounded anti-entropy."""

        peer, pending = self._settle_peer_liveliness(sample)
        if peer and sample.kind != "delete":
            self._announce_all()
            if pending is not None:
                self._apply_announce(peer, pending)
            # An announce can race the first pop while the liveliness callback
            # is applying the previous buffered value.  Recheck once after the
            # presence verdict has flipped so that value cannot be stranded
            # until a later liveliness transition.
            with self._pending_announces_lock:
                landed_during_flip = self._pending_announces.pop(peer, None)
            if landed_during_flip is not None:
                self._apply_announce(peer, landed_during_flip)
            with self._checkout_lock:
                checkouts = tuple(self._checkouts.values())
            for checkout in checkouts:
                self._request_checkout_reconcile(checkout)
            for space_id in self.stores.snapshot_ids():
                # F6: only schedule anti-entropy for spaces the peer actually
                # announced when holder hints exist.  A freshly restarted
                # node has no in-memory hints yet, so it retains the P1
                # liveliness fallback until the first announce arrives.
                with self._pending_announces_lock:
                    holders = frozenset(self._holders.get(space_id, ()))
                if holders and peer not in holders:
                    continue
                mesh = self._mesh(space_id)
                if mesh is not None:
                    mesh.schedule_sync_from(peer)


    def _announcement_spaces(self) -> tuple[dict[str, object], ...]:
        spaces: list[dict[str, object]] = []
        for info in sorted(self.facade.spaces(), key=lambda item: item.space_id):
            mode = next(
                (
                    member.mode
                    for member in self.facade.members(info.space_id)
                    if member.user == self.author
                ),
                None,
            )
            if mode is None:
                continue
            roles = ["member"]
            with self._resource_lock:
                resident = info.space_id in self._replicas
            if resident:
                roles.append("resident")
            spaces.append({"spaceId": info.space_id, "roles": roles, "mode": mode})
        return tuple(spaces)


    def _announce_all(self) -> None:
        with self._resource_lock:
            if self._closing or self._session is None:
                return
            mesh = next(iter(self._meshes.values()), None)
        if mesh is None:
            return
        mesh.announce()


    def _on_announce(self, sample: TransportSample) -> None:
        self._process_announce(sample)


    def _process_announce(self, sample: TransportSample) -> None:
        try:
            value = json.loads(sample.payload)
            prefix = f"{KeySpace().prefix}/org/fs/announce/"
            if not sample.key.startswith(prefix):
                return
            key_node = sample.key[len(prefix) :]
            if (
                not isinstance(value, dict)
                or value.get("schemaVersion") != 1
                or value.get("type") != "orgfs-announce"
                or value.get("node") != key_node
                or not key_node
                or "/" in key_node
                or not isinstance(value.get("spaces"), list)
            ):
                return
            with self._pending_announces_lock:
                observed_live = key_node in self._lively_peers
            if not observed_live and not self._supplier_online(key_node):
                self._buffer_announce(key_node, value)
                return
            with self._pending_announces_lock:
                self._pending_announces.pop(key_node, None)
            self._apply_announce(key_node, value)
        except Exception:
            return


    def _buffer_announce(
        self,
        node: str,
        value: dict[str, Any],
        *,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> None:
        with self._pending_announces_lock:
            spaces = value["spaces"]
            previous = self._pending_announces.get(node)
            if (
                previous is None
                and len(self._pending_announces) >= ORGFS_ANNOUNCE_BUFFER_LIMIT
            ):
                evicted = next(iter(self._pending_announces))
                self._pending_announces.pop(evicted, None)
                self._record_announce_buffer_drop(
                    reason="announcer-limit",
                    node=node,
                    dropped=1,
                    diagnostics=diagnostics,
                )
            if not spaces or previous is None or not previous["spaces"]:
                retained = list(spaces[:ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT])
                dropped = len(spaces) - len(retained)
                self._pending_announces[node] = {**value, "spaces": retained}
                if dropped:
                    self._record_announce_buffer_drop(
                        reason="space-limit",
                        node=node,
                        dropped=dropped,
                        space_count=len(retained),
                        diagnostics=diagnostics,
                    )
                return
            by_space = {
                str(space["spaceId"]): space
                for space in previous["spaces"]
                if isinstance(space, dict) and isinstance(space.get("spaceId"), str)
            }
            by_space.update(
                {
                    str(space["spaceId"]): space
                    for space in spaces
                    if isinstance(space, dict) and isinstance(space.get("spaceId"), str)
                }
            )
            merged = list(by_space.values())
            retained = merged[:ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT]
            self._pending_announces[node] = {**value, "spaces": retained}
            dropped = len(merged) - len(retained)
            if dropped:
                self._record_announce_buffer_drop(
                    reason="space-limit",
                    node=node,
                    dropped=dropped,
                    space_count=len(retained),
                    diagnostics=diagnostics,
                )


    def _record_announce_buffer_drop(
        self,
        *,
        reason: str,
        node: str,
        dropped: int,
        space_count: int = 0,
        diagnostics: list[dict[str, object]] | None = None,
    ) -> dict[str, object] | None:
        self._announce_buffer_dropped += dropped
        bucket = self._announce_buffer_dropped.bit_length()
        if bucket == self._announce_buffer_log_bucket:
            return None
        self._announce_buffer_log_bucket = bucket
        fields: dict[str, object] = {
            "reason": reason,
            "node": node,
            "droppedCount": self._announce_buffer_dropped,
            "announcerCount": len(self._pending_announces),
            "announcerLimit": ORGFS_ANNOUNCE_BUFFER_LIMIT,
            "spaceCount": space_count,
            "spacesPerEntryLimit": ORGFS_ANNOUNCE_SPACES_PER_ENTRY_LIMIT,
        }
        if diagnostics is not None:
            diagnostics.append(fields)
        elif self.logger is not None:
            self.logger("warn", "orgfs.announce.buffer-dropped", **fields)
        return fields


    def _apply_announce(self, node: str, value: dict[str, Any]) -> frozenset[str]:
        """Record ``node``'s holdings; return the spaces it newly holds."""

        spaces = value["spaces"]
        newly_held: set[str] = set()
        with self._pending_announces_lock:
            if not spaces:
                for holders in self._holders.values():
                    holders.discard(node)
                return frozenset()
            for space in spaces:
                if isinstance(space, dict) and isinstance(space.get("spaceId"), str):
                    space_id = str(space["spaceId"])
                    holders = self._holders.setdefault(space_id, set())
                    if node not in holders:
                        newly_held.add(space_id)
                    holders.add(node)
                    self._known_holders.setdefault(space_id, set()).add(node)
        return frozenset(newly_held)
