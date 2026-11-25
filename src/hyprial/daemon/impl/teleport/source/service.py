# Decision record (file header; no inline comments by repository rule):
# - The state machine is docs/design/agent-teleport-protocol.md §4.  Steps
#   0-1 change nothing on this node; step 3 onward stops the agent, so every
#   later failure either puts it back (``_restored``) or, when the outcome on
#   the target is unknown, deliberately leaves it stopped (``_unresolved``).
# - Failures before ``receive`` is issued restart the source and send a
#   best-effort abort.  A ``receive`` verdict (``ok:false``) restarts without
#   abort, because the target already undid everything.  A receive with no or
#   an unreadable answer, or any unexpected exception, is ``UNRESOLVED``: the
#   target may have landed the agent, so restarting could create two live
#   agents.
# - A restart that fails never replaces the original code: its failure is
#   appended to the message so the operator knows to start the agent by hand.
# - Slice 6 routines: after prepare and before the home is touched, every
#   originally enabled routine is paused, ``wait_drained`` waits for all
#   migrating routines (a disabled one may still hold in-flight rows) and every
#   definition is exported, so an unreadable routine fails while nothing is
#   stopped.  Those failures resume what this run paused, abort, and never
#   restart (the agent was not stopped).  A later failure restarts the agent
#   first and then resumes the routines this run paused.  ``UNRESOLVED``
#   resumes and removes nothing and lists the routines it left paused.
# - Cleanup removes every migrated routine before the agent; the first failure
#   stops it without commit, so the target keeps its routines paused and
#   ``pausedRoutines`` lists the target routines that were enabled.  A commit
#   failure is still a success carrying a warning; a routine that was disabled
#   on the source and stays paused on the target is not a warning.
# - One teleport per agent at a time, held across dry runs too, in memory.
#   ``AgentTeleportService`` must therefore be a process-wide singleton.
# - The export directory lives under ``workdir_root`` and is deleted in every
#   outcome; the stream inside it carries sealed credentials.
"""The source-side agent teleport service: preflight, move, clean up."""

from __future__ import annotations

import shutil
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.daemon.impl.teleport.protocol import (
    AbortRequest,
    CommitRequest,
    PrepareRequest,
    ReceiveReply,
    ReceiveRequest,
    RoutinePayload,
)
from hyprial.daemon.impl.teleport.sender import SendResult
from hyprial.daemon.impl.teleport.source.errors import TeleportRunError
from hyprial.daemon.impl.teleport.source.link import TeleportLink
from hyprial.daemon.impl.teleport.source.ports import (
    ExportedStream,
    RoutineRef,
    SourceAgent,
    SourceHost,
    SourceRoutines,
)
from hyprial.daemon.impl.teleport.source.preflight import (
    check_agent,
    select_routines,
)
from hyprial.kernel import TELEPORT_BUDGET, ipc_errors

STRICT_TIMEOUT_SECONDS = TELEPORT_BUDGET.default_strict_seconds
ROUTINE_DRAIN_TIMEOUT_SECONDS = TELEPORT_BUDGET.routine_drain_seconds


@dataclass(frozen=True, slots=True)
class _Plan:
    paused: tuple[str, ...] = ()
    payloads: tuple[RoutinePayload, ...] = ()


def _run_error(error: TeleportError) -> TeleportRunError:
    return TeleportRunError(error.code, error.message)


class AgentTeleportService:
    """Moves one agent to another node; ``run`` is both the dry run and the move."""

    def __init__(
        self,
        host: SourceHost,
        routines: SourceRoutines,
        link: TeleportLink,
        *,
        workdir_root: Path,
        new_operation_id: Callable[[], str],
        drain_timeout_seconds: float = ROUTINE_DRAIN_TIMEOUT_SECONDS,
    ) -> None:
        self._host = host
        self._routines = routines
        self._link = link
        self._workdir_root = Path(workdir_root)
        self._new_operation_id = new_operation_id
        self._drain_timeout = drain_timeout_seconds
        self._guard = threading.Lock()
        self._active: set[str] = set()

    def run(self, agent: str, *, to: str, dry_run: bool = False) -> dict[str, Any]:
        with ExitStack() as stack:
            stack.enter_context(self._claim(agent))
            described = self._describe(agent)
            if described.name != agent:
                stack.enter_context(self._claim(described.name))
            listed = self._preflight(described, to)
            return self._teleport(described, to, select_routines(listed), dry_run)

    @contextmanager
    def _claim(self, name: str) -> Iterator[None]:
        with self._guard:
            if name in self._active:
                raise TeleportRunError(
                    "TELEPORT_OPERATION_CONFLICT",
                    f"a teleport of agent {name!r} is already in progress",
                )
            self._active.add(name)
        try:
            yield
        finally:
            with self._guard:
                self._active.discard(name)

    def _describe(self, agent: str) -> SourceAgent:
        try:
            return self._host.describe(agent)
        except LookupError as error:
            raise TeleportRunError(
                ipc_errors.AGENT_NOT_FOUND, f"no agent {agent!r} on this node"
            ) from error
        except TeleportError as error:
            raise _run_error(error) from error

    def _preflight(self, agent: SourceAgent, to: str) -> list[RoutineRef]:
        check_agent(agent, to=to, node_id=self._host.node_id)
        try:
            self._host.require_resumable(agent)
        except TeleportError as error:
            raise _run_error(error) from error
        return list(self._routines.executed_by(agent.uri))

    def _teleport(
        self,
        agent: SourceAgent,
        to: str,
        migrating: tuple[RoutineRef, ...],
        dry_run: bool,
    ) -> dict[str, Any]:
        operation_id = self._new_operation_id()
        request = PrepareRequest(
            operation_id=operation_id,
            source_node=self._host.node_id,
            target_node=to,
            owner=self._host.owner,
            agent=agent.name,
            source_agent_uri=agent.uri,
            harness=agent.harness,
            routines=tuple(item.name for item in migrating),
            dry_run=dry_run,
        )
        try:
            prepared = self._link.prepare(to, request)
        except TeleportError as error:
            raise _run_error(error) from error
        result = _result(
            agent, self._host.node_id, to, operation_id, migrating, dry_run
        )
        if dry_run:
            return result
        workdir = Path(tempfile.mkdtemp(prefix="teleport-", dir=self._workdir_root))
        try:
            return self._move(
                agent,
                to,
                operation_id,
                prepared.recipient_public_key,
                workdir,
                result,
                migrating,
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _move(
        self,
        agent: SourceAgent,
        to: str,
        operation_id: str,
        recipient_public_key: bytes,
        workdir: Path,
        result: dict[str, Any],
        migrating: tuple[RoutineRef, ...],
    ) -> dict[str, Any]:
        plan = self._quiet_routines(to, operation_id, migrating)
        self._stop(agent, to, operation_id, plan)
        exported = self._export(
            agent, to, operation_id, recipient_public_key, workdir, plan
        )
        sent = self._send(agent, to, operation_id, exported, plan)
        landed = self._receive(agent, to, operation_id, exported, sent, plan)
        self._remove_source(agent, to, operation_id, landed, migrating)
        result["bytes"] = sent.total
        result["to"] = {"node": to, "uri": landed.agent_uri}
        result["commit"] = self._commit(to, operation_id, landed)
        return result

    def _quiet_routines(
        self, to: str, operation_id: str, migrating: tuple[RoutineRef, ...]
    ) -> _Plan:
        if not migrating:
            return _Plan()
        names = [item.name for item in migrating]
        paused: list[str] = []
        try:
            for item in migrating:
                if item.enabled:
                    self._routines.pause(item.name)
                    paused.append(item.name)
        except Exception as error:
            self._routines_failed(
                to,
                operation_id,
                paused,
                "TELEPORT_SOURCE_NOT_QUIESCED",
                f"could not pause routine: {str(error) or type(error).__name__}",
            )
        try:
            busy = list(self._routines.wait_drained(names, self._drain_timeout))
            cause = ""
        except Exception as error:
            busy = []
            cause = str(error) or type(error).__name__
        if busy or cause:
            detail = (
                f"routines still have work in flight: {', '.join(busy)}"
                if busy
                else f"waiting for in-flight routine work failed: {cause}"
            )
            self._routines_failed(
                to, operation_id, paused, "TELEPORT_IN_FLIGHT_TIMEOUT", detail
            )
        payloads: list[RoutinePayload] = []
        for item in migrating:
            try:
                exported = self._routines.export(item.name)
            except Exception as error:
                self._routines_failed(
                    to,
                    operation_id,
                    paused,
                    "TELEPORT_SOURCE_NOT_QUIESCED",
                    f"could not export routine {item.name!r}: "
                    f"{str(error) or type(error).__name__}",
                )
            payloads.append(RoutinePayload(item.name, exported.yaml, item.enabled))
        return _Plan(tuple(paused), tuple(payloads))

    def _routines_failed(
        self,
        to: str,
        operation_id: str,
        paused: Sequence[str],
        code: str,
        message: str,
    ) -> NoReturn:
        message += self._resume_routines(paused)
        try:
            self._link.abort(to, AbortRequest(operation_id, code))
        except Exception:
            message += "; the abort request to the target failed"
        raise TeleportRunError(code, message)

    def _resume_routines(self, paused: Sequence[str]) -> str:
        failed: list[str] = []
        for name in paused:
            try:
                self._routines.resume(name)
            except Exception:
                failed.append(name)
        if not failed:
            return ""
        return (
            f"; routines that could not be resumed and stay paused: "
            f"{', '.join(failed)} (resume them with `hyprial routine resume <name>`)"
        )

    def _stop(
        self, agent: SourceAgent, to: str, operation_id: str, plan: _Plan
    ) -> None:
        try:
            quiescent = self._host.quiesce(agent)
            cause = "" if quiescent else "no proof that it stopped"
        except Exception as error:
            cause = str(error) or type(error).__name__
        if cause:
            self._restored(
                agent,
                to,
                operation_id,
                "TELEPORT_SOURCE_NOT_QUIESCED",
                f"could not stop agent {agent.name!r}: {cause}",
                plan,
                abort=True,
            )

    def _export(
        self,
        agent: SourceAgent,
        to: str,
        operation_id: str,
        recipient_public_key: bytes,
        workdir: Path,
        plan: _Plan,
    ) -> ExportedStream:
        try:
            return self._host.export_stream(
                agent, recipient_public_key=recipient_public_key, workdir=workdir
            )
        except Exception as error:
            self._restored(
                agent,
                to,
                operation_id,
                "TELEPORT_SOURCE_NOT_QUIESCED",
                f"could not export agent {agent.name!r}: "
                f"{str(error) or type(error).__name__}",
                plan,
                abort=True,
            )

    def _send(
        self,
        agent: SourceAgent,
        to: str,
        operation_id: str,
        exported: ExportedStream,
        plan: _Plan,
    ) -> SendResult:
        try:
            sent = self._link.send(to, operation_id, exported.path)
        except TeleportError as error:
            self._restored(
                agent, to, operation_id, error.code, error.message, plan, abort=True
            )
        except Exception as error:
            self._restored(
                agent,
                to,
                operation_id,
                "TELEPORT_INCOMPLETE",
                f"sending to {to!r} failed: {str(error) or type(error).__name__}",
                plan,
                abort=True,
            )
        if sent.total != exported.total or sent.stream_sha256 != exported.sha256:
            self._restored(
                agent,
                to,
                operation_id,
                "TELEPORT_STREAM_MISMATCH",
                "the sent stream differs from the exported bundle",
                plan,
                abort=True,
            )
        return sent

    def _receive(
        self,
        agent: SourceAgent,
        to: str,
        operation_id: str,
        exported: ExportedStream,
        sent: SendResult,
        plan: _Plan,
    ) -> ReceiveReply:
        request = ReceiveRequest(
            operation_id=operation_id,
            manifest_digest=exported.manifest_digest,
            stream_sha256=exported.sha256,
            harness=agent.harness,
            session_ref=agent.session_ref,
            routines=plan.payloads,
            args=tuple(agent.args),
            strict_timeout_seconds=STRICT_TIMEOUT_SECONDS,
        )
        try:
            return self._link.receive(to, request)
        except TeleportError as error:
            if error.code != "TELEPORT_UNRESOLVED":
                self._restored(
                    agent,
                    to,
                    operation_id,
                    error.code,
                    error.message,
                    plan,
                    abort=False,
                )
            raise self._unresolved(
                agent, to, operation_id, sent, plan, error.message
            ) from error
        except Exception as error:
            raise self._unresolved(
                agent, to, operation_id, sent, plan, str(error) or type(error).__name__
            ) from error

    def _remove_source(
        self,
        agent: SourceAgent,
        to: str,
        operation_id: str,
        landed: ReceiveReply,
        migrating: tuple[RoutineRef, ...],
    ) -> None:
        what = ""
        try:
            for item in migrating:
                what = f"routine {item.name!r}"
                self._routines.remove(item.name)
            what = "the stopped source copy"
            self._host.remove(agent)
        except Exception as error:
            cause = str(error) or type(error).__name__
            raise TeleportRunError(
                "TELEPORT_SOURCE_CLEANUP_FAILED",
                f"agent {agent.name!r} now runs on {to!r} as {landed.agent_uri}, "
                f"but removing {what} failed: {cause}; the target routines "
                "stay paused",
                {
                    "operationId": operation_id,
                    "agent": agent.name,
                    "targetNode": to,
                    "targetAgentUri": landed.agent_uri,
                    "pausedRoutines": _left_paused(landed),
                },
            ) from error

    def _commit(
        self, to: str, operation_id: str, landed: ReceiveReply
    ) -> dict[str, Any]:
        try:
            committed = self._link.commit(to, CommitRequest(operation_id))
        except Exception:
            return {"ok": False, "pausedRoutines": _left_paused(landed)}
        paused = [
            item.name for item in committed.routines if item.enabled and not item.resumed
        ]
        return {"ok": not paused, "pausedRoutines": paused}

    def _restored(
        self,
        agent: SourceAgent,
        to: str,
        operation_id: str,
        code: str,
        message: str,
        plan: _Plan,
        *,
        abort: bool,
    ) -> NoReturn:
        try:
            self._host.restart(agent)
        except Exception as error:
            message += (
                f"; the source agent could not be restarted "
                f"({str(error) or type(error).__name__}), start it with "
                f"`hyprial agent start {agent.name}`"
            )
        message += self._resume_routines(plan.paused)
        if abort:
            try:
                self._link.abort(to, AbortRequest(operation_id, code))
            except Exception:
                message += "; the abort request to the target failed"
        raise TeleportRunError(code, message)

    def _unresolved(
        self,
        agent: SourceAgent,
        to: str,
        operation_id: str,
        sent: SendResult,
        plan: _Plan,
        cause: str,
    ) -> TeleportRunError:
        manual = (
            f"Check node {to!r}: run `hyprial agent list` there and look for "
            f"{agent.name!r}. If it exists and its session is intact, remove "
            f"the stopped source agent here with `hyprial agent destroy "
            f"{agent.name}`. If it does not exist, start the source agent "
            f"again with `hyprial agent start {agent.name}`."
        )
        return TeleportRunError(
            "TELEPORT_UNRESOLVED",
            f"the outcome of moving {agent.name!r} to {to!r} is unknown ({cause}); "
            "the source agent stays stopped. " + manual,
            {
                "operationId": operation_id,
                "targetNode": to,
                "agent": agent.name,
                "sourceAgentUri": agent.uri,
                "bytesSent": sent.total,
                "sourceState": "stopped",
                "pausedRoutines": list(plan.paused),
                "manualCheck": manual,
            },
        )


def _left_paused(landed: ReceiveReply) -> list[str]:
    return [item.name for item in landed.routines if item.restore_enabled]


def _result(
    agent: SourceAgent,
    source_node: str,
    to: str,
    operation_id: str,
    migrating: tuple[RoutineRef, ...],
    dry_run: bool,
) -> dict[str, Any]:
    return {
        "ok": True,
        "operationId": operation_id,
        "dryRun": dry_run,
        "agent": agent.name,
        "from": {"node": source_node, "uri": agent.uri},
        "to": {"node": to, "uri": None},
        "bytes": 0,
        "routines": [{"name": item.name, "enabled": item.enabled} for item in migrating],
    }
