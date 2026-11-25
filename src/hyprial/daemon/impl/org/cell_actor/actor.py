# Decision notes (OC2 per-org actor):
# - One actor instance per org (`group="org-cell"`, `key=org`), spawned lazily
#   on the daemon's `ActorHostRegistry.host_for("org-cell")` and stopped when
#   left, deleted, unknown or detached on shutdown.
# - Wraps existing `OrgNetworkService` operations (create, repair, invite, join,
#   execute, accept_invite, invite_missing_step, leave, remove, delete, network,
#   publish_self, process_leave_requests) without changing external payloads or
#   error codes.
# - Uses non-blocking `actor.tell(...)` plus bounded `Event.wait(timeout=...)`
#   (never `ask`, never unbounded waits) and guards re-entrant calls on the
#   actor's own handler thread.
# - D5 (contract OC0 §3.9): the actor never ensures the org cell. Only
#   `OrgNetworkService.create` does, during a fresh or resumed create; roster,
#   repair, invite and a repeated create on an existing org only look it up,
#   so a legacy org without a cell is never backfilled.
# - A completed `org_delete` retires the owner's org cell by the result's
#   `spaceId`, not by a post-delete ownership snapshot: a `deleted` marker
#   makes the org read as not writable, and `retire_owner_org_cell` only
#   touches the cell keyed by this node's own authority.
"""Per-org ``OrgCellActor`` wrapping org membership/network services and roster state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Any

from hyprial.daemon.impl.org.cell_actor.sub_org import (
    snapshot_sub_org_status,
    verify_parent_chain_confirmed_member,
)
from hyprial.daemon.impl.org.cell_actor.sync import (
    OrgCellRoster,
    OrgMemberWrite,
    org_ownership,
    read_org_roster,
    remove_owner_org_member_by_uid,
    remove_owner_org_member_by_username,
    retire_owner_org_cell,
    upsert_owner_org_member,
)
from hyprial.daemon.impl.org.network.service import OrgNetworkError
from hyprial.kernel import (
    ActorSpec,
    AdmissionResult,
    STATE_AUTHORITY,
    ipc_errors,
    is_agent_id_segment,
    keyed_cell_id,
)

ORG_CELL_ACTOR_GROUP = "org-cell"
DEFAULT_CALL_TIMEOUT_S = 30.0
DEFAULT_STOP_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class OrgCellActorSnapshot:
    """Immutable state projection for one per-org actor."""

    org: str
    space_id: str | None
    role: str | None
    writable: bool
    retired: bool
    cell_id: str | None = None
    parent_org: str | None = None
    tailnet_id: str | None = None
    out_of_parent_members: tuple[str, ...] = ()
    parent_missing: bool = False


@dataclass(slots=True)
class _RunCommand:
    fn: Callable[[], Any]
    done: threading.Event = field(default_factory=threading.Event)
    outcome: Any = None
    error: BaseException | None = None


class OrgCellActor:
    """Serialized state owner for one organization on this daemon node."""

    def __init__(
        self,
        *,
        org: str,
        app: Any,
        service_factory: Callable[[], Any],
        actor_host: Any = None,
        call_timeout_s: float = DEFAULT_CALL_TIMEOUT_S,
        stop_timeout_s: float = DEFAULT_STOP_TIMEOUT_S,
    ) -> None:
        self._org = org
        self._app = app
        self._service_factory = service_factory
        self._actor_host = actor_host
        self._call_timeout_s = call_timeout_s
        self._stop_timeout_s = stop_timeout_s
        self._guard = threading.RLock()
        self._fallback_lock = threading.RLock()
        self._running_thread_id: int | None = None
        self._closed = False
        self._retired = False
        self._cell: Any = None
        self._snapshot = self._build_snapshot(retired=False)
        if actor_host is not None:
            self._spawn()

    @property
    def org(self) -> str:
        return self._org

    def snapshot(self) -> OrgCellActorSnapshot:
        with self._guard:
            if self._snapshot.parent_org is not None:
                self._snapshot = self._build_snapshot(retired=self._retired)
            return self._snapshot

    def _spawn(self) -> None:
        spec = ActorSpec(
            name=f"{ORG_CELL_ACTOR_GROUP}-{self._org}",
            handler_factory=lambda: self._receive,
            mailbox_capacity=128,
            supervision_profile=STATE_AUTHORITY,
            group=ORG_CELL_ACTOR_GROUP,
            key=self._org,
        )
        self._cell = self._actor_host.spawn(spec)

    def _receive(self, message: object) -> None:
        if not isinstance(message, _RunCommand):
            return
        self._running_thread_id = threading.get_ident()
        try:
            message.outcome = message.fn()
        except BaseException as exc:  # noqa: BLE001 - propagated to caller
            message.error = exc
        finally:
            self._running_thread_id = None
            message.done.set()

    def _dispatch(self, fn: Callable[[], Any]) -> Any:
        with self._guard:
            if self._closed:
                raise OrgNetworkError(
                    "ORG_ACTOR_CLOSED",
                    f"org cell actor for {self._org!r} is closed",
                    {"org": self._org},
                )
            if self._running_thread_id == threading.get_ident():
                return fn()
            cell = self._cell
        if cell is None:
            with self._fallback_lock:
                return fn()
        cmd = _RunCommand(fn=fn)
        admission = cell.tell(cmd)
        if admission is AdmissionResult.CLOSED:
            raise OrgNetworkError(
                "ORG_ACTOR_CLOSED",
                f"org cell actor for {self._org!r} is closed",
                {"org": self._org},
            )
        if admission is AdmissionResult.OVERLOADED:
            raise OrgNetworkError(
                "ORG_ACTOR_BUSY",
                f"org cell actor mailbox for {self._org!r} is full",
                {"org": self._org},
            )
        if not cmd.done.wait(timeout=self._call_timeout_s):
            raise OrgNetworkError(
                "ORG_ACTOR_TIMEOUT",
                f"org cell actor for {self._org!r} did not respond in time",
                {"org": self._org, "timeoutSeconds": self._call_timeout_s},
            )
        if cmd.error is not None:
            raise cmd.error
        return cmd.outcome

    def _build_snapshot(self, *, retired: bool) -> OrgCellActorSnapshot:
        ownership = org_ownership(self._app, self._org)
        cell_id: str | None = None
        if (
            ownership.writable
            and ownership.space_id is not None
            and ownership.authority is not None
        ):
            cell_id = keyed_cell_id("org", ownership.authority, ownership.space_id)
        parent_org, tailnet_id, parent_missing, out_of_parent = (
            snapshot_sub_org_status(
                self._app,
                org=self._org,
                space_id=ownership.space_id,
                writable=ownership.writable and not retired,
            )
        )
        return OrgCellActorSnapshot(
            org=self._org,
            space_id=ownership.space_id,
            role=ownership.role,
            writable=ownership.writable and not retired,
            retired=retired,
            cell_id=cell_id,
            parent_org=parent_org,
            tailnet_id=tailnet_id,
            out_of_parent_members=out_of_parent,
            parent_missing=parent_missing,
        )

    def _refresh_snapshot(self, *, retired: bool | None = None) -> OrgCellActorSnapshot:
        with self._guard:
            effective_retired = self._retired if retired is None else retired
            self._retired = effective_retired
            self._snapshot = self._build_snapshot(retired=effective_retired)
            return self._snapshot

    def _require_writable(self) -> OrgCellActorSnapshot:
        snapshot = self._refresh_snapshot()
        if not snapshot.writable or snapshot.space_id is None:
            raise OrgNetworkError(
                "ORG_ACTION_NOT_AUTHORIZED",
                f"org {self._org!r} member roster is read-only on this machine",
                {"org": self._org, "role": snapshot.role},
            )
        self._service_factory().require_roster_write_authority(
            self._org, snapshot.space_id
        )
        return snapshot

    def roster(self) -> OrgCellRoster:
        def _op() -> OrgCellRoster:
            snapshot = self._refresh_snapshot()
            service = self._service_factory()
            store = getattr(service, "_store", None)
            return read_org_roster(
                self._app,
                org=self._org,
                space_id=snapshot.space_id,
                role=snapshot.role,
                writable=snapshot.writable,
                store=store,
            )

        return self._dispatch(_op)

    def upsert_member(self, member: OrgMemberWrite) -> Any:
        def _op() -> Any:
            snapshot = self._require_writable()
            assert snapshot.space_id is not None
            cell = upsert_owner_org_member(
                self._app,
                org=self._org,
                space_id=snapshot.space_id,
                member=member,
            )
            self._refresh_snapshot()
            return cell

        return self._dispatch(_op)

    def remove_member(self, uid_ref: str) -> Any:
        def _op() -> Any:
            snapshot = self._require_writable()
            assert snapshot.space_id is not None
            cell = remove_owner_org_member_by_uid(
                self._app,
                org=self._org,
                space_id=snapshot.space_id,
                uid_ref=uid_ref,
            )
            self._refresh_snapshot()
            return cell

        return self._dispatch(_op)

    def org_create(self, *, parent: str | None = None) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            result = (
                self._service_factory().create(self._org, parent=parent)
                if parent is not None
                else self._service_factory().create(self._org)
            )
            self._refresh_snapshot(retired=False)
            return result

        return self._dispatch(_op)

    def org_repair(self, *, apply: bool = False) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            result = self._service_factory().repair(self._org, apply=apply)
            self._refresh_snapshot(retired=False)
            return result

        return self._dispatch(_op)

    def org_invite(self, user: str) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            pre_snap = self._refresh_snapshot()
            if pre_snap.writable and pre_snap.space_id is not None:
                from hyprial.daemon.impl.composition.cells import cell_runtime

                verify_parent_chain_confirmed_member(
                    self._app,
                    runtime=cell_runtime(self._app),
                    org=self._org,
                    space_id=pre_snap.space_id,
                    uid_ref=None,
                    username=user.removeprefix("user:").strip(),
                )
            result = self._service_factory().invite(self._org, user)
            self._refresh_snapshot()
            return result

        return self._dispatch(_op)

    def org_join(self, link: str, *, claims: dict[str, Any] | None = None) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            service = self._service_factory()
            result = service.join(link, claims=claims) if claims is not None else service.join(link)
            self._refresh_snapshot(retired=False)
            return result

        return self._dispatch(_op)

    def org_execute(self, link: str, *, claims: dict[str, Any] | None = None) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            service = self._service_factory()
            result = service.execute(link, claims=claims) if claims is not None else service.execute(link)
            self._refresh_snapshot(retired=False)
            return result

        return self._dispatch(_op)

    def org_accept_invite(self, link: str, *, claims: dict[str, Any] | None = None) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            service = self._service_factory()
            result = service.accept_invite(link, claims=claims) if claims is not None else service.accept_invite(link)
            self._refresh_snapshot(retired=False)
            return result

        return self._dispatch(_op)

    def org_invite_missing_step(self, link: str) -> str | None:
        return self._dispatch(lambda: self._service_factory().invite_missing_step(link))

    def org_leave(
        self,
        *,
        confirmed: bool = False,
        operation_id: str | None = None,
        device_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            result = self._service_factory().leave(
                self._org,
                confirmed=confirmed,
                operation_id=operation_id,
                device_ids=device_ids,
            )
            self._refresh_snapshot(retired=result.get("phase") == "complete")
            return result

        return self._dispatch(_op)

    def org_remove(
        self,
        user: str,
        *,
        confirmed: bool = False,
        operation_id: str | None = None,
        device_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            result = self._service_factory().remove(
                self._org,
                user,
                confirmed=confirmed,
                operation_id=operation_id,
                device_ids=device_ids,
            )
            snapshot = self._refresh_snapshot()
            space_id = result.get("spaceId") or snapshot.space_id
            if (
                result.get("phase") == "complete"
                and snapshot.writable
                and isinstance(space_id, str)
                and space_id
            ):
                remove_owner_org_member_by_username(
                    self._app,
                    org=self._org,
                    space_id=space_id,
                    username=user,
                )
            self._refresh_snapshot()
            return result

        return self._dispatch(_op)

    def org_delete(
        self,
        *,
        confirmed: bool = False,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            result = self._service_factory().delete(
                self._org, confirmed=confirmed, operation_id=operation_id
            )
            space_id = result.get("spaceId")
            if result.get("phase") == "complete" and isinstance(space_id, str) and space_id:
                retire_owner_org_cell(self._app, org=self._org, space_id=space_id)
            self._refresh_snapshot(retired=result.get("phase") == "complete")
            return result

        return self._dispatch(_op)

    def org_network(self) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            result = self._service_factory().network(self._org)
            self._refresh_snapshot()
            return result

        return self._dispatch(_op)

    def org_publish_self(self) -> bool:
        def _op() -> bool:
            service = self._service_factory()
            published = bool(service._publish_into(self._org))
            self._refresh_snapshot()
            return published

        return self._dispatch(_op)

    def sync_removed_member(self, username: str) -> None:
        def _op() -> None:
            snapshot = self._refresh_snapshot()
            if snapshot.writable and snapshot.space_id is not None and username:
                remove_owner_org_member_by_username(
                    self._app,
                    org=self._org,
                    space_id=snapshot.space_id,
                    username=username,
                )
            self._refresh_snapshot()

        self._dispatch(_op)

    def org_process_leave_requests(self) -> dict[str, Any]:
        def _op() -> dict[str, Any]:
            service = self._service_factory()
            result = service.process_leave_requests(self._org)
            snapshot = self._refresh_snapshot()
            if snapshot.writable and snapshot.space_id is not None:
                for item in result.get("processed", []):
                    if item.get("org") == self._org:
                        remove_owner_org_member_by_username(
                            self._app,
                            org=self._org,
                            space_id=snapshot.space_id,
                            username=str(item.get("user") or ""),
                        )
            self._refresh_snapshot()
            return result

        return self._dispatch(_op)

    def _local_exclusion_store(self) -> tuple[OrgCellActorSnapshot, Any]:
        from hyprial.daemon.impl.org.network.member_agents.exclusions import (
            OrgAgentExclusionStore,
        )

        snapshot = self._refresh_snapshot()
        if snapshot.space_id is None:
            raise OrgNetworkError(
                "unknown-space",
                f"org {self._org!r} space is unavailable",
                {"org": self._org},
            )
        home = getattr(self._app, "hyprial_home", None)
        if not home:
            raise OrgNetworkError(
                "unavailable",
                f"hyprial_home is unavailable for org {self._org!r}",
                {"org": self._org},
            )
        return snapshot, OrgAgentExclusionStore(Path(home))

    def _republish_member_agents_best_effort(self) -> None:
        if (
            callable(getattr(self._app, "_require_orgfs_runtime", None))
            and getattr(self._app, "agents", None) is not None
            and getattr(self._app, "owner", None)
            and getattr(self._app, "node_id", None)
        ):
            from hyprial.daemon.impl.org.network.member_agents import publish_for_app

            publish_for_app(self._app, [self._org])

    def exclude_agent(self, agent_id: str) -> dict[str, Any]:
        """Add ``(agent_id, directorySpace)`` to local exclusions and republish."""

        def _op() -> dict[str, Any]:
            if not isinstance(agent_id, str) or not is_agent_id_segment(agent_id):
                raise OrgNetworkError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"invalid agent id {agent_id!r}",
                    {"org": self._org, "agentId": agent_id},
                )
            snapshot, store = self._local_exclusion_store()
            assert snapshot.space_id is not None
            changed = store.exclude(agent_id, snapshot.space_id)
            self._republish_member_agents_best_effort()
            self._refresh_snapshot()
            return {
                "org": self._org,
                "spaceId": snapshot.space_id,
                "agentId": agent_id,
                "excluded": True,
                "changed": changed,
            }

        return self._dispatch(_op)

    def include_agent(self, agent_id: str) -> dict[str, Any]:
        """Remove ``(agent_id, directorySpace)`` from local exclusions and republish."""

        def _op() -> dict[str, Any]:
            if not isinstance(agent_id, str) or not is_agent_id_segment(agent_id):
                raise OrgNetworkError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"invalid agent id {agent_id!r}",
                    {"org": self._org, "agentId": agent_id},
                )
            snapshot, store = self._local_exclusion_store()
            assert snapshot.space_id is not None
            changed = store.include(agent_id, snapshot.space_id)
            self._republish_member_agents_best_effort()
            self._refresh_snapshot()
            return {
                "org": self._org,
                "spaceId": snapshot.space_id,
                "agentId": agent_id,
                "excluded": False,
                "changed": changed,
            }

        return self._dispatch(_op)

    def agent_exclusions(self) -> tuple[str, ...]:
        """Return sorted agent ids excluded locally from this org."""

        def _op() -> tuple[str, ...]:
            snapshot, store = self._local_exclusion_store()
            assert snapshot.space_id is not None
            return tuple(sorted(store.excluded_agent_ids(snapshot.space_id)))

        return self._dispatch(_op)

    def agent_excluded(self, agent_id: str) -> bool:
        """Whether ``agent_id`` is excluded locally from this org."""

        def _op() -> bool:
            if not isinstance(agent_id, str) or not is_agent_id_segment(agent_id):
                raise OrgNetworkError(
                    ipc_errors.INVALID_ARGUMENT,
                    f"invalid agent id {agent_id!r}",
                    {"org": self._org, "agentId": agent_id},
                )
            snapshot, store = self._local_exclusion_store()
            assert snapshot.space_id is not None
            return store.is_excluded(agent_id, snapshot.space_id)

        return self._dispatch(_op)

    def close(self) -> bool:
        with self._guard:
            if self._closed:
                return True
            self._closed = True
            cell = self._cell
            self._cell = None
        if cell is None:
            return True
        return bool(cell.stop(timeout=self._stop_timeout_s))


__all__ = [
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_STOP_TIMEOUT_S",
    "ORG_CELL_ACTOR_GROUP",
    "OrgCellActor",
    "OrgCellActorSnapshot",
]
