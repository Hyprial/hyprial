"""Routine service surface: default agent routines, coordinator reconcile, admission and routine IPC families."""

from __future__ import annotations

from __future__ import annotations
import hashlib
from collections.abc import Mapping
from typing import Any, TYPE_CHECKING
from uuid import uuid4
from hyprial.identity import (
    Agent,
)
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.kernel import PortAdmission
from hyprial.daemon.impl.pac.actors.reconcile import AssignReconcileReport
from hyprial.daemon.impl.application.ports import (
    RoutinePortError,
    RoutinePortTimeout,
    RoutineSchemaPortError,
)
from hyprial.daemon.impl.dispatch.admission import dispatch_gate
from hyprial.daemon.impl.alias_resolver import AliasSurface
from hyprial.kernel import (
    canonical_agent_uri,
    canonical_user_uri,
    legacy_user_uri,
    parse_agent_uri,
)
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)


class _RoutinesMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    def _migrate_stored_routine_address(self, name: str) -> str | None:
        """Resolve one bare actor name against THIS machine's agents registry.

        Migration rule (approved Q1): rewrite a stored bare-name address only
        when the local agents roster answers with exactly one agent; no
        profile inference, no presence scan, no guessing. Unknown or
        ambiguous names return None and the routine quarantines loudly
        instead of being silently resumed or rewritten.
        """

        if ":" in name or not name.strip():
            return None
        try:
            agent = self.agents.get(name.strip())
        except Exception:
            return None
        if agent is None:
            return None
        candidate = agent.uri
        return candidate if parse_agent_uri(candidate) is not None else None

    def _deliver_routine_task(
        self, *, target: str, conversation_id: str, text: str, sender: str
    ) -> bool:
        """Deliver one routine task message through the durable inbox.

        PAC notifications carry a reference, never a body, and activating a
        graph does not dispatch its root task -- so the task text travels
        here, on the same inbox IO the PAC notifications use.  The delivery
        is keyed by the conversation, so a replayed dispatch for the same
        task reuses the same message id instead of queueing a second copy.
        """

        io = self._pac_notification_io
        if io is None:
            return False
        try:
            io.deliver(
                # Naming debt, not a workflow dependency: the id minted here
                # still carries the old prefix (inbox/io.py message_id), and
                # renaming a durable message id belongs with U7.
                effect_id=f"routine-task:{conversation_id}",
                sender=sender,
                target=target,
                conversation_id=conversation_id,
                text=text,
            )
        except Exception as error:  # noqa: BLE001 - reported as a dispatch failure
            self._log(
                "warn",
                "routine",
                "routine.task.delivery_failed",
                target=target,
                conversationId=conversation_id,
                error=f"{type(error).__name__}: {error}",
            )
            return False
        return True

    def _assign_routine_probe(self, name: str) -> bool:
        """§C.1.1 三态例程探针,供 assign 周期性核对读「这条 routine 还在吗」。

        True = 存在;False = 查询【成功】且确定没有 —— 删除是决定,
        所以这是核对里唯一算死的「不存在」(§C.1.1① 明确停止);
        读失败让它抛 —— 判定层把异常读成 unknown,⧗ 不算死。
        刻意不看 ``enabled``:disable 是暂停,退役是另一个未落地的决定
        (design-assign §G2),把暂停当死会因一次误操作释放一批 agent。
        打开/关闭一次 store 是刻意的:probe 只在有 routine 边时才会被调,
        而今天没有任何写入方会写 routine 边(assign.py producer registry),
        为此常驻一个连接是伪装成优化的一笔生命周期责任。
        """

        probe = self._routine_exists_probe
        if probe is None:
            raise DaemonRequestError(
                ipc_errors.ROUTINE_UNAVAILABLE,
                "routine runtime is not running",
            )
        return probe(name)

    def _on_assign_reconcile_report(self, report: AssignReconcileReport) -> None:
        """Make each §C.1 pass visible: releases and unknowns both get a line.

        ``unknown`` is why this sink exists: 查不到不释放是安全侧,但安全侧
        的沉默会把一次持续性的读失败藏成永远没人看见的盲区 —— 所以 unknown
        为零且无释放时才静默。Runs in the workflow actor; structlog is
        thread-safe, and the sink must never raise (it is called after the
        stamps are committed, but its own failure must not ripple into the
        timer either).
        """

        try:
            if report.error is not None:
                self._log(
                    "error",
                    "daemon",
                    "assign.reconcile_failed",
                    detail=report.error[:500],
                )
                return
            if not report.dead and not report.unknown:
                return
            self._log(
                "info",
                "daemon",
                "assign.reconciled",
                released=len(report.dead),
                unknown=len(report.unknown),
                alive=report.alive,
                deadRows=[f"{a}|{k}|{r}" for a, k, r in report.dead][:20],
                unknownRows=[f"{a}|{k}|{r}" for a, k, r in report.unknown][:20],
            )
        except Exception:  # noqa: BLE001 - observer is best-effort by contract
            pass

    @staticmethod
    def _routine_binding(routine: Mapping[str, object]) -> str | None:
        """Return the actor a routine binds, from its declared ownership form."""

        ownership = routine.get("actorOwnership")
        if ownership == "borrowed":
            actor = routine.get("actor")
            return actor if isinstance(actor, str) else None
        if ownership == "routine":
            produced = routine.get("produces")
            return produced if isinstance(produced, str) else None
        return None

    def _routines_bound_to(self, actor: str) -> list[dict[str, object]]:
        if self._routine_service is None:
            return []
        return [
            routine
            for routine in self._routine_service.list()["routines"]
            if isinstance(routine, dict) and self._routine_binding(routine) == actor
        ]

    @staticmethod
    def _default_agent_routine_name(actor: str) -> str:
        return f"agent-home-setup-{hashlib.sha256(actor.encode()).hexdigest()[:16]}"

    def _register_default_agent_routine(
        self, agent: Agent
    ) -> dict[str, object] | None:
        """Bind the default routine, retaining accepted timeout custody."""

        if self._routine_service is None:
            warning = {
                "code": "AGENT_DEFAULT_ROUTINE_UNAVAILABLE",
                "message": (
                    "agent was created without a routine because the routine "
                    "service is unavailable; repair the service, then bind one "
                    "with `hyprial routine add`"
                ),
            }
            self._log(
                "info", "agents", "agent.default_routine.unavailable",
                actor=agent.uri, **warning,
            )
            return warning
        if self._routines_bound_to(agent.uri):
            return None
        name = self._default_agent_routine_name(agent.uri)
        yaml_text = self._routine_template_renderer(
            "agent-home-setup",
            owner=agent.uri,
            escalate_to=legacy_user_uri(agent.owner),
            name=name,
        )
        spec = self._routine_text_loader(yaml_text)
        self._routine_admit(spec)
        coordinator = self._routine_coordinator
        if coordinator is None:
            if self._transport is not None:
                raise DaemonRequestError(
                    ipc_errors.ROUTINE_UNAVAILABLE,
                    "routine coordinator is not running",
                )
            self._routine_service.add(
                yaml_text=yaml_text,
                owner=legacy_user_uri(agent.owner),
                enabled=True,
            )
            return None
        operation_id = f"agent-default-routine:{agent.entity_token}"
        admission = coordinator.begin_add(
            operation_id=operation_id,
            name=name,
            yaml_text=yaml_text,
            owner=legacy_user_uri(agent.owner),
            produces=None,
            agent_compensation={
                "actor": agent.actor,
                "expected_entity_token": agent.entity_token,
                "settlement_id": (
                    f"agent-default-compensation:{agent.entity_token}"
                ),
            },
        )
        if admission is not PortAdmission.ACCEPTED:
            raise DaemonRequestError(
                ipc_errors.ROUTINE_UNAVAILABLE,
                f"default routine admission {admission.value}",
            )
        coordinator.wait(operation_id, timeout=70.0)
        return None

    def _finish_resident_agent_creation(
        self, agent: Agent
    ) -> dict[str, object] | None:
        try:
            return self._register_default_agent_routine(agent)
        except RoutinePortTimeout as error:
            raise DaemonRequestError(
                ipc_errors.AGENT_DEFAULT_ROUTINE_PENDING,
                f"agent default routine remains accepted: {error.operation_id}",
                {"actor": agent.uri, "operationId": error.operation_id},
            ) from error
        except RoutinePortError as error:
            if error.code == "ROUTINE_COMMAND_TIMEOUT":
                raise DaemonRequestError(
                    ipc_errors.AGENT_DEFAULT_ROUTINE_PENDING,
                    "agent default routine remains accepted",
                    {"actor": agent.uri},
                ) from error
            self._compensate_default_agent_creation(agent)
            raise DaemonRequestError(
                ipc_errors.AGENT_DEFAULT_ROUTINE_FAILED,
                f"agent creation rolled back because its default routine "
                f"could not be registered ({error.code}): {error}",
                {"actor": agent.uri, "routineError": error.code},
            ) from error
        except Exception as error:
            # Only a terminal refusal compensates. Entity CAS protects a
            # same-name successor if the caller raced recreation.
            self._compensate_default_agent_creation(agent)
            code = getattr(error, "code", type(error).__name__)
            raise DaemonRequestError(
                ipc_errors.AGENT_DEFAULT_ROUTINE_FAILED,
                f"agent creation rolled back because its default routine "
                f"could not be registered ({code}): {error}",
                {"actor": agent.uri, "routineError": str(code)},
            ) from error

    def _compensate_default_agent_creation(self, agent: Agent) -> bool:
        return self._compensate_default_agent_incarnation(
            f"agent-default-compensation:{agent.entity_token}",
            agent.actor,
            agent.entity_token,
        )

    def _compensate_default_agent_incarnation(
        self, settlement_id: str, actor: str, expected_entity_token: str
    ) -> bool:
        settled = self.agents.settle_destroy(
            settlement_id,
            actor,
            expected_entity_token=expected_entity_token,
        )
        return settled.disposition != "stale-incarnation"

    def _remove_registered_routine(self, name: str, *, enforce_last: bool, operation_id: str | None = None) -> JsonObject:
        """Keep the dev entry point through the same typed removal saga."""
        if self._routine_service is None:
            raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, 'routine service is not running')
        coordinator = self._routine_coordinator
        if coordinator is not None:
            operation_id = operation_id or uuid4().hex
            try:
                admitted = coordinator.begin_remove(operation_id=operation_id, name=name, enforce_last=enforce_last)
                if admitted is not PortAdmission.ACCEPTED:
                    raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, f'routine admission {admitted.value}')
                return coordinator.wait(operation_id, timeout=70.0)
            except RoutinePortTimeout as error:
                raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, f'routine operation {error.operation_id} remains accepted') from error
            except RoutinePortError as error:
                raise DaemonRequestError(
                    error.code, str(error), dict(error.data) if error.data else None
                ) from error
        if self._transport is not None:
            raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, 'routine coordinator is not running')
        with self._routine_legacy_lock:
            reservation_id = f'legacy-routine-remove:{uuid4().hex}'
            reserved = False
            try:
                routine = {**self._routine_service.reserve_remove(name=name, reservation_id=reservation_id, enforce_last=enforce_last), 'name': name}
                reserved = True
                if routine.get('enabled') is True:
                    self._routine_service.pause(name=name)
                in_flight = tuple(routine.get('inFlight', []))
                authority = self._pac_graph_authority
                if in_flight and authority is None:
                    raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, 'PAC graph authority is not running')
                from hyprial.identity import PAC_GRAPH_NOT_FOUND, PacError

                for task in in_flight:
                    graph_id = task['runId']
                    try:
                        authority.close_graph(graph_id, actor=str(routine['owner']))
                    except PacError as error:
                        if error.code != PAC_GRAPH_NOT_FOUND:
                            raise
                coordinator = self._retire_routine_coordinator(routine)
                result = self._routine_service.remove(name=name, reservation_id=reservation_id)
                reserved = False
                return {**result, **({'coordinator': coordinator} if coordinator is not None else {})}
            except RoutinePortError as error:
                raise DaemonRequestError(
                    error.code, str(error), dict(error.data) if error.data else None
                ) from error
            finally:
                if reserved:
                    self._routine_service.cancel_remove(name=name, reservation_id=reservation_id)

    def _reconcile_routine_coordinators(self) -> None:
        """Isolate persisted routine faults so daemon startup remains operable."""
        assert self._routine_service is not None
        try:
            routines = self._routine_service.list()["routines"]
        except Exception as error:  # noqa: BLE001 - corrupt rows must not brick daemon
            self._log(
                "error",
                "daemon",
                "routine.coordinator.read_failed",
                errorType=type(error).__name__,
                detail=str(error),
            )
            return
        for routine in routines:
            assert isinstance(routine, dict)
            try:
                coordinator = self._ensure_routine_coordinator(
                    routine, recovering=True
                )
            except Exception as error:  # noqa: BLE001 - isolate one bad routine row
                self._log(
                    "error",
                    "daemon",
                    "routine.coordinator.reconcile_failed",
                    routine=routine.get("name"),
                    errorType=type(error).__name__,
                    detail=str(error),
                )
                continue
            if coordinator is not None:
                self._log(
                    "info",
                    "daemon",
                    "routine.coordinator.reconciled",
                    routine=routine["name"],
                    **coordinator,
                )

    def _ensure_routine_coordinator(self, routine: dict[str, object], *, recovering: bool) -> dict[str, object] | None:
        schema_error = routine.get("schemaError")
        if isinstance(schema_error, str):
            raise DaemonRequestError("ROUTINE_SCHEMA_ERROR", schema_error)
        produced = routine.get("produces")
        if not isinstance(produced, str):
            return None
        parsed = parse_agent_uri(produced)
        if parsed is None:
            raise DaemonRequestError("ROUTINE_COORDINATOR_INVALID", "produces must be a canonical agent URI")
        if parsed[:2] != (self.owner, self.node_id):
            raise DaemonRequestError("ROUTINE_COORDINATOR_IDENTITY_MISMATCH",
                                     f"coordinator {produced} must belong to agent:{self.owner}:{self.node_id}:*")
        name, actor_name = str(routine["name"]), parsed[2]
        marker = self._routine_coordinator_marker(name, routine.get("registrationId"))
        existing = [item for item in self.desired_state.load().harnesses if item.name == actor_name]
        if existing:
            if existing[0].nickname != marker:
                raise DaemonRequestError("ROUTINE_COORDINATOR_CONFLICT",
                                         f"actor {produced} is not owned by routine {name}")
            return {"actor": produced, "restored": recovering, "changed": False}
        launch = routine.get("launch")
        if not isinstance(launch, dict):
            from hyprial.daemon.impl.dispatch.matrix import resolve
            try:
                choice = resolve("fast")
            except (RuntimeError, ValueError) as error:
                raise DaemonRequestError("ROUTINE_COORDINATOR_UNAVAILABLE", str(error)) from error
            launch = {"harness": choice.harness, "model": choice.model, "provider": choice.provider}
        launched = self.handle("lifecycle.start", {
            "provider": launch["harness"], "name": actor_name, "headless": True,
            "nickname": marker, "model": launch.get("model"),
            **({"modelProvider": launch["provider"]} if launch.get("provider") is not None else {}),
            **({"cwd": launch["cwd"]} if launch.get("cwd") is not None else {}),
            **({"args": launch["args"]} if launch.get("args") else {}),
            "operationId": f"{marker}:start",
        })
        if not isinstance(launched, dict) or launched.get("actor") != produced:
            self.handle("down", {"target": actor_name, "provider": launch["harness"]})
            raise DaemonRequestError("ROUTINE_COORDINATOR_IDENTITY_MISMATCH",
                                     f"coordinator did not resolve as {produced}")
        return {**launched, "restored": False}

    def _retire_routine_coordinator(self, routine: dict[str, object]) -> dict[str, object] | None:
        produced = routine.get("produces")
        name = str(routine["name"])
        marker = self._routine_coordinator_marker(name, routine.get("registrationId"))
        if isinstance(routine.get("schemaError"), str):
            # The schema is unreadable, so its raw ``produces`` value cannot
            # authorize a stop. The durable ownership marker still can.
            matches = [
                item
                for item in self.desired_state.load().harnesses
                if item.nickname == marker
            ]
            if not matches:
                return None
            item = matches[0]
            actor = canonical_agent_uri(self.owner, self.node_id, item.name)
            result = self.handle(
                "down", {"target": item.name, "provider": item.harness}
            )
            return {
                "actor": actor,
                "retired": True,
                "changed": bool(result.get("removed")),
            }
        if not isinstance(produced, str):
            return None
        parsed = parse_agent_uri(produced)
        if parsed is None:
            raise DaemonRequestError("ROUTINE_COORDINATOR_INVALID", "produces must be a canonical agent URI")
        actor_name = parsed[2]
        matches = [item for item in self.desired_state.load().harnesses if item.name == actor_name]
        if not matches:
            return {"actor": produced, "retired": False, "changed": False}
        item = matches[0]
        if item.nickname != self._routine_coordinator_marker(name, routine.get("registrationId")):
            raise DaemonRequestError("ROUTINE_COORDINATOR_CONFLICT",
                                     f"actor {produced} is not owned by routine {name}")
        result = self.handle("down", {"target": actor_name, "provider": item.harness})
        return {"actor": produced, "retired": True, "changed": bool(result.get("removed"))}

    def _ipc_routine_add(self, params) -> Any:
        if self._routine_service is None:
            raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
        yaml_text = _required_string(params.get("yaml"), "yaml")
        import yaml
        try:
            spec = self._routine_text_loader(yaml_text)
        except RoutineSchemaPortError as error:
            raise DaemonRequestError("ROUTINE_SCHEMA_ERROR", str(error)) from error
        source = self._workflow_caller(params)
        if spec.actor is None and spec.produces is None:
            document = yaml.safe_load(yaml_text)
            from hashlib import sha256
            actor_name = f"routine-{spec.name}" if len(spec.name) <= 48 else "routine-" + sha256(spec.name.encode()).hexdigest()[:16]
            document["produces"] = canonical_agent_uri(self.owner, self.node_id, actor_name)
            yaml_text = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
            spec = self._routine_text_loader(yaml_text)
        self._routine_admit(spec)
        coordinator = self._routine_coordinator
        if coordinator is not None:
            operation_id = str(params.get("operationId") or uuid4().hex)
            try:
                admitted = coordinator.begin_add(
                    operation_id=operation_id,
                    name=spec.name,
                    yaml_text=yaml_text,
                    owner=source,
                    produces=spec.produces,
                )
                if admitted is not PortAdmission.ACCEPTED:
                    raise DaemonRequestError(
                        ipc_errors.ROUTINE_UNAVAILABLE,
                        f"routine admission {admitted.value}",
                    )
                return coordinator.wait(operation_id, timeout=70.0)
            except RoutinePortTimeout as error:
                raise DaemonRequestError(
                    ipc_errors.ROUTINE_UNAVAILABLE,
                    f"routine operation {error.operation_id} remains accepted",
                ) from error
            except RoutinePortError as error:
                raise DaemonRequestError(error.code, str(error)) from error
        if self._transport is not None:
            raise DaemonRequestError(
                ipc_errors.ROUTINE_UNAVAILABLE,
                "routine coordinator is not running",
            )
        with self._routine_legacy_lock:
            if spec.produces is not None and any(
                item.get("produces") == spec.produces
                for item in self._routine_service.list()["routines"]
            ):
                raise DaemonRequestError("ROUTINE_COORDINATOR_CONFLICT",
                                         f"coordinator already owned: {spec.produces}")
            try:
                result = self._routine_service.add(yaml_text=yaml_text, owner=source, enabled=False)
            except RoutinePortError as error:
                raise DaemonRequestError(error.code, str(error)) from error
            routine = {**self._routine_service.status(name=spec.name), "name": spec.name}
            try:
                coordinator = self._ensure_routine_coordinator(routine, recovering=False)
                self._routine_service.resume(name=spec.name, align_schedule=True)
                result["enabled"] = True
            except Exception:
                self._routine_service.remove(name=spec.name)
                raise
            return {**result, **({"coordinator": coordinator} if coordinator is not None else {})}

    def _ipc_routine_list(self, params, routine_caller) -> Any:
        if self._routine_service is None:
            raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
        all_callers = params.get("all", False)
        if type(all_callers) is not bool:
            raise DaemonRequestError(
                ipc_errors.INVALID_ARGUMENT, "all must be a boolean"
            )
        routines = self._routine_service.list()["routines"]
        last_dispatches = (
            self._workflow_service.routine_last_dispatches()
            if self._workflow_service is not None
            else {}
        )
        visible = []
        for routine in routines:
            owner = routine.get("owner")
            actor = routine.get("actor")
            if not (
                all_callers
                or routine_caller == canonical_user_uri(self.owner)
                or routine_caller in (owner, actor)
            ):
                continue
            outcomes = routine.get("outcomes")
            visible.append(
                {
                    **routine,
                    "lastDispatchAtMs": last_dispatches.get(routine.get("name")),
                    "lastOutcome": (
                        outcomes[-1]
                        if isinstance(outcomes, list) and outcomes
                        else None
                    ),
                }
            )
        return {"routines": visible}

    def _ipc_routine_status(self, params) -> Any:
        if self._routine_service is None:
            raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
        name = _required_string(params.get("name"), "name")
        try:
            return self._routine_service.status(name=name)
        except RoutinePortError as error:
            raise DaemonRequestError(error.code, str(error)) from error

    def _ipc_routine_audit(self) -> Any:
        if self._routine_service is None:
            raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
        # Doctor surface (approved Q1): quarantined routines and the
        # stored-spec address-migration ledger, both machine-readable.
        quarantined = [
            item
            for item in self._routine_service.list()["routines"]
            if isinstance(item, dict) and item.get("quarantined") is True
        ]
        return {
            "quarantined": quarantined,
            "addressMigrations": self._routine_service.address_migrations(),
        }

    def _ipc_routine_remove(self, params) -> Any:
        if self._routine_service is None:
            raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
        name = _required_string(params.get("name"), "name")
        coordinator = self._routine_coordinator
        if coordinator is not None:
            operation_id = str(params.get("operationId") or uuid4().hex)
            try:
                admitted = coordinator.begin_remove(
                    operation_id=operation_id, name=name, enforce_last=True,
                )
                if admitted is not PortAdmission.ACCEPTED:
                    raise DaemonRequestError(
                        ipc_errors.ROUTINE_UNAVAILABLE,
                        f"routine admission {admitted.value}",
                )
                return coordinator.wait(operation_id, timeout=70.0)
            except RoutinePortTimeout as error:
                raise DaemonRequestError(
                    ipc_errors.ROUTINE_UNAVAILABLE,
                    f"routine operation {error.operation_id} remains accepted",
                ) from error
            except RoutinePortError as error:
                raise DaemonRequestError(
                    error.code, str(error), dict(error.data) if error.data else None
                ) from error
        if self._transport is not None:
            raise DaemonRequestError(
                ipc_errors.ROUTINE_UNAVAILABLE,
                "routine coordinator is not running",
            )
        with self._routine_legacy_lock:
            reservation_id = f"legacy-routine-remove:{uuid4().hex}"
            reserved = False
            try:
                routine = {
                    **self._routine_service.reserve_remove(
                        name=name,
                        reservation_id=reservation_id,
                        enforce_last=True,
                    ),
                    "name": name,
                }
                reserved = True
                if routine.get("enabled") is True:
                    self._routine_service.pause(name=name)
                in_flight = tuple(routine.get("inFlight", []))
                authority = self._pac_graph_authority
                if in_flight and authority is None:
                    raise DaemonRequestError(
                        ipc_errors.ROUTINE_UNAVAILABLE,
                        "PAC graph authority is not running",
                    )
                from hyprial.identity import PAC_GRAPH_NOT_FOUND, PacError

                for task in in_flight:
                    graph_id = task["runId"]
                    try:
                        authority.close_graph(
                            graph_id, actor=str(routine["owner"])
                        )
                    except PacError as error:
                        if error.code != PAC_GRAPH_NOT_FOUND:
                            raise
                coordinator = self._retire_routine_coordinator(routine)
                result = self._routine_service.remove(
                    name=name, reservation_id=reservation_id
                )
                reserved = False
                return {**result, **({"coordinator": coordinator} if coordinator is not None else {})}
            except RoutinePortError as error:
                raise DaemonRequestError(
                    error.code, str(error), dict(error.data) if error.data else None
                ) from error
            finally:
                if reserved:
                    self._routine_service.cancel_remove(
                        name=name, reservation_id=reservation_id
                    )

    def _ipc_routine_set(self, params) -> Any:
        if self._routine_service is None:
            raise DaemonRequestError(
                ipc_errors.ROUTINE_UNAVAILABLE,
                "routine service is not running",
            )
        name = _required_string(params.get("name"), "name")
        yaml_text = _required_string(params.get("yaml"), "yaml")
        try:
            spec = self._routine_text_loader(yaml_text)
        except RoutineSchemaPortError as error:
            raise DaemonRequestError("ROUTINE_SCHEMA_ERROR", str(error)) from error
        current = self._routine_service.status(name=name)
        if spec.name != name:
            raise DaemonRequestError(
                ipc_errors.ROUTINE_NAME_IMMUTABLE,
                f"replacement name must remain {name!r}",
            )
        if (spec.actor or spec.produces) != self._routine_binding(current):
            raise DaemonRequestError(
                ipc_errors.ROUTINE_BINDING_IMMUTABLE,
                "routine set keeps the existing actor/produces binding",
            )
        self._routine_admit(spec)
        try:
            return self._routine_service.set(name=name, yaml_text=yaml_text)
        except RoutinePortError as error:
            raise DaemonRequestError(error.code, str(error)) from error

    def _ipc_routine_pause(self, params) -> Any:
        if self._routine_service is None:
            raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
        name = _required_string(params.get("name"), "name")
        try:
            return self._routine_service.pause(name=name)
        except RoutinePortError as error:
            raise DaemonRequestError(error.code, str(error)) from error

    def _ipc_routine_resume(self, params) -> Any:
        if self._routine_service is None:
            raise DaemonRequestError(ipc_errors.ROUTINE_UNAVAILABLE, "routine service is not running")
        name = _required_string(params.get("name"), "name")
        try:
            return self._routine_service.resume(name=name)
        except RoutinePortError as error:
            raise DaemonRequestError(error.code, str(error)) from error

    def _resolve_routine_principal(self, actor: str) -> str:
        principal = parse_agent_uri(actor)
        if principal is not None and principal[:2] != (self.owner, self.node_id):
            return actor  # explicit target; workflow admission checks its home daemon
        return self._resolve_send_sender(actor)

    def _routine_admit(self, spec: Any) -> None:
        """Apply workflow dispatch admission to a routine's target actor.

        Borrowed actors are resolve-or-reject.  A routine-owned ``produces``
        actor may not exist yet, so absent capabilities are observable but are
        not guessed to be either interactive or headless, matching
        :func:`dispatch_gate`.
        """

        self._validate_routine_aliases(spec)
        target = spec.actor or spec.produces
        if target is None:
            return
        if spec.actor is not None:
            local_entity = self.agents.get(target)
            if local_entity is not None and local_entity.uri == target:
                target = local_entity.uri
            elif self._remote_workflow is not None and self._remote_workflow.remote(target):
                from types import SimpleNamespace
                from hyprial.identity import PacError

                try:
                    self._remote_workflow.admit(
                        SimpleNamespace(
                            owner=target,
                            role=spec.role,
                            first_output_eta=None,
                            human_gates=None,
                        )
                    )
                except PacError as error:
                    raise DaemonRequestError(error.code, str(error)) from error
                return
            else:
                target = self._resolve_send_sender(target)
        entity = self.agents.get(target)
        capabilities = entity.capabilities if entity is not None else {}
        dispatch_gate(
            target=target,
            capabilities=capabilities,
            role=spec.role,
            emit=self._log,
            source="routine.add",
        )

    def _validate_routine_aliases(self, spec: Any) -> None:
        """Validate every address-bearing routine field without rewriting it."""

        # ``produces`` declares the new routine-owned actor which this very
        # operation creates; it is not an alias lookup for an existing holder.
        if isinstance(spec.actor, str):
            # Existing actor admission already rejects unknown principals as
            # SENDER_UNRESOLVED; keep its code and data shape unchanged.
            self._validate_input_alias(
                spec.actor,
                surface=AliasSurface.ROUTINE,
                preserve_unknown=True,
            )
        targets = [getattr(spec, "escalate_to", None)]
        for route in getattr(spec, "routes", ()):
            if getattr(route, "kind", None) == "escalate":
                targets.append(getattr(route, "value", None))
        for target in targets:
            if isinstance(target, str):
                self._validate_input_alias(target, surface=AliasSurface.ROUTINE)
