"""PAC request delegation over the existing trusted tailnet message plane.

The mesh is NOT an adversarial identity boundary (design.md trust model). Local callers
must authenticate to their own daemon. A request-scoped capability never enters
an actor prompt; it binds forwarding to the exact origin/owner/input/deadline.
This is not a general remote IPC proxy or a second graph executor.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import secrets
import sqlite3
import threading
from time import time_ns, monotonic, sleep
from uuid import uuid4

from hyprial.kernel import ActorRuntime, ActorSpec, AdmissionResult
from hyprial.kernel import ipc_errors
from hyprial.daemon.impl.dispatch.admission import dispatch_gate
from hyprial.daemon.impl.dispatch.identity import dispatch_message_id
from hyprial.identity import PacError
from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore, default_database_path
from hyprial.daemon.impl.pac.workflows.graphs import input_token
from hyprial.daemon.impl.pac.workflows.outputs import validate_workflow_output_text
from hyprial.kernel import parse_agent_uri
from hyprial.daemon.impl.dispatch.remote.wire import (
    RemoteWire,
    _PendingRemoteState,
    _RemoteEffect,
    _RemoteEffectCompleted,
    _RemoteStateCommand,
    _RemoteStateCompleted,
    encoded,
)
from hyprial.daemon.impl.dispatch.remote.expansion import (
    enqueue_outcome_row,
    expansion_outcome_fields,
    forward_expansion_preview,
    preview_origin_expansion,
    validate_expansion_outcome,
)

__all__ = ["RemoteWorkflow", "RemoteWire"]


class RemoteWorkflow:
    def __init__(self, application, transport):
        self.app = application
        self.database = default_database_path(application.state_dir)
        self.authority = application._dispatch_service_actor
        self.graph_authority = getattr(application, "_pac_graph_authority", None)
        self.outcome_lock = threading.Lock()
        self._return_lock = threading.Lock()
        self._returning: set[str] = set()
        self.wire = None
        self.pump = None
        self.stopping = threading.Event()
        self._state_lock = threading.Lock()
        self._state_pending: dict[str, _PendingRemoteState] = {}
        self._state_runtime = ActorRuntime()
        self._state_handle = None
        self._effect_queue: queue.Queue[_RemoteEffect] = queue.Queue(maxsize=8)
        self._effect_workers: tuple[threading.Thread, ...] = ()
        self._effect_overloads = 0
        store = None
        try:
            if self.graph_authority is not None:
                self.secret = self.graph_authority.ensure_remote_key()
            else:
                store = PacGraphStore(self.database)
                with store.write():
                    store._db.execute(
                        "INSERT OR IGNORE INTO remote_workflow_key VALUES (1,?)",
                        (secrets.token_bytes(32),),
                    )
                    self.secret = bytes(
                        store._db.execute(
                            "SELECT secret FROM remote_workflow_key"
                        ).fetchone()[0]
                )
            self._state_handle = self._state_runtime.start(
                ActorSpec(
                    name="remote-workflow-authority",
                    handler_factory=lambda: self._run_remote_state,
                    mailbox_capacity=64,
                    supervision_profile="state_authority",
                )
            )
            self._effect_workers = tuple(
                threading.Thread(
                    target=self._remote_effect_loop,
                    name=f"pac-remote-effect-{index}", daemon=True,
                )
                for index in range(2)
            )
            for worker in self._effect_workers:
                worker.start()
            self.wire = RemoteWire(transport, self.authority, self.handle)
            self.pump = threading.Thread(
                target=self._pump, name="pac-remote-outcomes", daemon=True
            )
            self.pump.start()
        except Exception:
            self.stopping.set()
            if self.wire is not None:
                try:
                    self.wire.stop()
                    self.wire.drain(1.0)
                except Exception:
                    pass
            if self.pump is not None:
                self.pump.join(1.0)
            for worker in self._effect_workers:
                worker.join(1.0)
            if self._state_handle is not None:
                self._state_runtime.drain(1.0)
            raise
        finally:
            if store is not None:
                store.close()

    def close_registrations(self):
        self.stopping.set()
        self.wire.stop()

    def shutdown(self, timeout):
        deadline = monotonic() + max(0, timeout)
        drained = self.wire.drain(max(0, deadline - monotonic()))
        self.pump.join(max(0, deadline - monotonic()))
        for worker in self._effect_workers:
            worker.join(max(0, deadline - monotonic()))
        state_drained = self._state_runtime.drain(max(0, deadline - monotonic()))
        return (
            drained and not self.pump.is_alive()
            and not any(worker.is_alive() for worker in self._effect_workers)
            and state_drained.complete
        )

    def _local_workflow(self):
        workflow = self.app._workflow_service
        if workflow is None:
            raise PacError(
                "WORKFLOW_REMOTE_UNAVAILABLE",
                "local workflow service is not running",
            )
        return workflow

    def close(self):
        self.close_registrations()
        if not self.shutdown(5.0):
            raise RuntimeError("remote workflow handlers did not drain")

    def _pump(self):
        while not self.stopping.wait(1):
            try:
                store = PacGraphStore(self.database, read_only=True)
                try:
                    pending = [
                        row[0]
                        for row in store._db.execute(
                            "SELECT request_id FROM remote_workflow_outbox WHERE result_json IS NULL ORDER BY attempted_at,rowid LIMIT 32"
                        )
                    ]
                finally:
                    store.close()
            except (OSError, sqlite3.Error):
                self.app._log(
                    "warn",
                    "pac",
                    "workflow.remote_returns_deferred",
                    reason="database unavailable",
                )
                continue
            for request_id in pending:
                if self.stopping.is_set():
                    break
                try:
                    self._flush(request_id)
                except Exception:
                    # The durable outbox retains custody. Only an explicit
                    # authority decision is terminal; transport faults retry.
                    continue

    def _flush(self, request_id):
        with self._return_lock:
            if request_id in self._returning:
                return {"ok": True, "requestId": request_id, "returnState": "pending"}
            self._returning.add(request_id)
        try:
            return self._flush_once(request_id)
        finally:
            with self._return_lock:
                self._returning.discard(request_id)

    def _flush_once(self, request_id):
        grant = self._lookup(request_id=request_id)
        store = PacGraphStore(self.database, read_only=True)
        try:
            row = dict(
                store._db.execute(
                    "SELECT * FROM remote_workflow_outbox WHERE request_id=?",
                    (request_id,),
                ).fetchone()
            )
        finally:
            store.close()
        if row["result_json"] is not None:
            return json.loads(row["result_json"])
        if self.graph_authority is not None:
            self.graph_authority.mark_remote_attempt(
                request_id, time_ns() // 1_000_000
            )
        else:
            store = PacGraphStore(self.database)
            try:
                with store.write():
                    store._db.execute(
                        "UPDATE remote_workflow_outbox SET attempted_at=? WHERE request_id=?",
                        (time_ns() // 1_000_000, request_id),
                    )
            finally:
                store.close()
        try:
            result = self.wire.call(
                grant["origin"],
                "outcome",
                {
                    "grant": grant,
                    "action": row["action"],
                    "reasonRef": row["reason_ref"],
                    "outputText": row["output_text"],
                    **(
                        {
                            "expansion": row["expansion_text"],
                            "expansionDigest": row["expansion_digest"],
                            "attemptNo": row["attempt_no"],
                        }
                        if row["expansion_text"] is not None
                        else {}
                    ),
                },
            )
            result = {**result, "returnState": "accepted"}
        except PacError as error:
            if error.code in (
                "WORKFLOW_REMOTE_UNAVAILABLE", "PAC_GRAPH_OVERLOADED", "PAC_GRAPH_PENDING",
            ):
                return {"ok": True, "requestId": request_id, "returnState": "pending"}
            result = {
                "ok": False,
                "requestId": request_id,
                "returnState": "rejected",
                "error": {"code": error.code, "message": str(error)},
            }
        if self.graph_authority is not None:
            self.graph_authority.record_remote_result(
                request_id, encoded(result).decode(), int(row["attempt_no"])
            )
        else:
            store = PacGraphStore(self.database)
            try:
                with store.write():
                    store._db.execute(
                    "UPDATE remote_workflow_outbox SET result_json=? WHERE request_id=? "
                    "AND attempt_no=? AND result_json IS NULL",
                    (encoded(result).decode(), request_id, row["attempt_no"]),
                    )
            finally:
                store.close()
        return result

    def _enqueue(
        self, grant, action, reason, output_text=None, *, expansion=None, flush=True
    ):
        if (
            action not in ("complete", "fail")
            or not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > 16384
        ):
            raise PacError(
                "WORKFLOW_REMOTE_INVALID",
                "explicit complete/fail and evidence reference required",
            )
        try:
            output_text = validate_workflow_output_text(output_text)
        except ValueError as error:
            raise PacError("WORKFLOW_OUTPUT_INVALID", str(error)) from error
        expansion_fields = (
            expansion_outcome_fields(expansion, attempt_no=0)
            if expansion is not None
            else {}
        )
        if expansion is not None and action != "complete":
            raise PacError(
                "PAC_EXPANSION_INVALID",
                "only remote completion may carry expansion",
                {"field": "expansion", "reason": "unexpected"},
            )
        if expansion is not None and "pac-expansion-v1" not in grant.get(
            "features", ()
        ):
            raise PacError(
                "WORKFLOW_REMOTE_INVALID",
                "remote peer does not support PAC expansion",
                {"field": "expansion", "reason": "unsupported-peer"},
            )
        if self.graph_authority is not None:
            self.graph_authority.enqueue_remote_outcome(
                grant["requestId"],
                action,
                reason,
                output_text,
                expansion_fields.get("expansion"),
                expansion_fields.get("expansionDigest"),
            )
        else:
            store = PacGraphStore(self.database)
            try:
                with store.write():
                    enqueue_outcome_row(
                        store._db,
                        request_id=grant["requestId"],
                        action=action,
                        reason=reason,
                        output_text=output_text,
                        expansion_text=expansion_fields.get("expansion"),
                        expansion_digest_value=expansion_fields.get(
                            "expansionDigest"
                        ),
                    )
            finally:
                store.close()
        if not flush:
            # A native turn outcome is delivered by the daemon cadence.  The
            # committed outbox row retains custody while the pump performs
            # the remote round trip on its own I/O thread.
            return {"ok": True, "requestId": grant["requestId"], "returnState": "pending"}
        return self._flush(grant["requestId"])

    def request_pruned(self, message_id: str, recipient: str) -> bool:
        """Durably queue failure of a pruned remote workflow request."""

        grant = self._lookup(message_id=message_id, actor=recipient)
        if grant is None:
            return False
        if self.graph_authority is not None:
            return self.graph_authority.prune_remote_request(grant["requestId"])
        store = PacGraphStore(self.database)
        try:
            with store.write():
                row = store._db.execute(
                    "SELECT * FROM remote_workflow_outbox WHERE request_id=?",
                    (grant["requestId"],),
                ).fetchone()
                if row and (row["action"], row["reason_ref"]) != (
                    "fail",
                    "pac:request-expired",
                ):
                    return False
                store._db.execute(
                    "INSERT OR IGNORE INTO remote_workflow_outbox"
                    "(request_id,action,reason_ref,result_json,output_text,"
                    "expansion_text,expansion_digest,attempt_no) "
                    "VALUES (?,?,?,NULL,NULL,NULL,NULL,0)",
                    (grant["requestId"], "fail", "pac:request-expired"),
                )
        finally:
            store.close()
        return True

    def remote(self, actor):
        principal = parse_agent_uri(actor)
        return principal is not None and principal[:2] != (
            self.app.owner,
            self.app.node_id,
        )

    def _mac(self, grant):
        return hmac.new(
            self.secret,
            encoded({k: v for k, v in grant.items() if k != "capability"}),
            hashlib.sha256,
        ).hexdigest()

    def _verify(self, grant):
        if (
            grant.get("origin") != self.authority
            or not isinstance(grant.get("capability"), str)
            or not hmac.compare_digest(self._mac(grant), grant["capability"])
        ):
            raise PacError(
                ipc_errors.CALLER_NOT_AUTHORIZED, "invalid remote request capability"
            )

    def admit_local(self, data):
        actor = data.get("owner")
        principal = parse_agent_uri(actor) if isinstance(actor, str) else None
        if principal is None or principal[:2] != (self.app.owner, self.app.node_id):
            raise PacError("WORKFLOW_NOT_OWNER", "actor is not local to this daemon")
        recipient = self.app._resolve_send_sender(actor)
        entity = self.app.agents.get(recipient)
        if entity is None:
            raise PacError(
                "WORKFLOW_REMOTE_ACTOR_NOT_FOUND", "borrowed actor does not exist"
            )
        dispatch_gate(
            target=actor,
            capabilities=entity.capabilities,
            role=data.get("role"),
            first_output_eta=data.get("firstOutputEta"),
            human_gates_declared=data.get("humanGatesDeclared") is True,
            emit=self.app._log,
            source="pac.remote.admission",
        )
        return {
            "ok": True,
            "owner": actor,
            "features": ["pac-expansion-v1"],
        }

    def admit(self, node):
        result = self.wire.call(
            node.owner,
            "admit",
            {
                "owner": node.owner,
                "role": node.role,
                "firstOutputEta": node.first_output_eta,
                "humanGatesDeclared": node.human_gates is not None,
            },
        )
        if node.expands is not None and "pac-expansion-v1" not in result.get(
            "features", ()
        ):
            raise PacError(
                "WORKFLOW_REMOTE_INVALID",
                "remote peer does not support PAC expansion",
                {"field": "expansion", "reason": "unsupported-peer"},
            )
        return result

    def preview_expansion(self, params, caller):
        return forward_expansion_preview(self, params, caller)

    def send(self, request, *, text, idempotency_key):
        if not self.remote(request["owner"]):
            return None
        grant = {
            **request,
            "origin": self.authority,
            "messageId": dispatch_message_id(f"pac:{idempotency_key}"),
            "effectId": f"pac:{idempotency_key}",
            "textDigest": hashlib.sha256(text.encode()).hexdigest(),
            **(
                {"features": ["pac-expansion-v1"]}
                if request.get("expands") is not None
                else {}
            ),
        }
        grant["capability"] = self._mac(grant)
        result = self.wire.call(
            request["owner"], "offer", {"grant": grant, "text": text}
        )
        if result.get("messageId") != grant["messageId"]:
            raise PacError(
                "WORKFLOW_REMOTE_INVALID", "remote delivery identity mismatch"
            )
        return result["messageId"]

    def _lookup(
        self,
        *,
        graph_id=None,
        node_id=None,
        request_id=None,
        message_id=None,
        actor=None,
    ):
        store = PacGraphStore(self.database, read_only=True)
        try:
            clauses, args = [], []
            for key, value in (
                ("graph_id", graph_id),
                ("node_id", node_id),
                ("request_id", request_id),
                ("message_id", message_id),
                ("owner", actor),
            ):
                if value is not None:
                    clauses.append(key + "=?")
                    args.append(value)
            row = store._db.execute(
                "SELECT grant_json FROM remote_workflow_requests WHERE "
                + " AND ".join(clauses)
                + " ORDER BY rowid DESC LIMIT 1",
                args,
            ).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            store.close()

    def current(self, grant):
        self._verify(grant)
        store = PacGraphStore(self.database, read_only=True)
        try:
            with store.read():
                graph = store.graph(grant["graphId"])
                node = store.node(grant["graphId"], grant["nodeId"])
                row = store._db.execute(
                    "SELECT * FROM workflow_nodes WHERE graph_id=? AND node_id=?",
                    (grant["graphId"], grant["nodeId"]),
                ).fetchone()
                valid = bool(
                    graph
                    and graph["closed_at"] is None
                    and node
                    and node.owner == grant["owner"]
                    and not node.flag
                    and row
                    and row["state"] == "requested"
                    and row["request_id"] == grant["requestId"]
                    and row["deadline_ms"] is not None
                    and row["deadline_ms"]
                    == grant["deadlineMs"]
                    >= time_ns() // 1_000_000
                    and row["input_token"]
                    == grant["inputToken"]
                    == input_token(store, grant["graphId"], grant["nodeId"])
                )
                return {"ok": True, "current": valid}
        finally:
            store.close()

    def _receipt(
        self, grant, action, reason, output_text=None, expansion_digest=None
    ):
        store = PacGraphStore(self.database, read_only=True)
        try:
            row = store._db.execute(
                "SELECT * FROM workflow_outcome_receipts WHERE request_id=?",
                (("reset:" if action == "reset" else "") + grant["requestId"],),
            ).fetchone()
            if row:
                if (
                    row["actor"],
                    row["action"],
                    row["reason_ref"],
                    row["output_text"],
                    row["expansion_digest"],
                ) != (
                    grant["owner"],
                    action,
                    reason,
                    output_text,
                    expansion_digest,
                ):
                    raise PacError(
                        "WORKFLOW_OUTCOME_CONFLICT",
                        "request already has a different accepted outcome",
                    )
                return json.loads(row["result_json"])
        finally:
            store.close()
        return None

    def _state_call(self, method: str, data: dict) -> object:
        handle = self._state_handle
        if handle is None:
            raise PacError("WORKFLOW_REMOTE_UNAVAILABLE", "remote state authority is closed")
        correlation = f"remote-state-{uuid4().hex}"
        command = _RemoteStateCommand(correlation, method, encoded(data))
        pending = _PendingRemoteState()
        with self._state_lock:
            if len(self._state_pending) >= 128 or self.stopping.is_set():
                raise PacError("WORKFLOW_REMOTE_UNAVAILABLE", "remote state authority is overloaded")
            self._state_pending[correlation] = pending
            admission = self._state_runtime.tell(handle, command)
            if admission is not AdmissionResult.ACCEPTED:
                del self._state_pending[correlation]
                raise PacError("WORKFLOW_REMOTE_UNAVAILABLE", f"remote state admission: {admission.value}")
        if not pending.event.wait(4.0):
            # The accepted command continues.  Its durable request/receipt
            # identity fences a caller retry; a timeout never cancels it.
            with self._state_lock:
                self._state_pending.pop(correlation, None)
            raise PacError("WORKFLOW_REMOTE_UNAVAILABLE", "remote state operation remains pending")
        with self._state_lock:
            completion = self._state_pending.pop(correlation).completion
        assert completion is not None
        if completion.error is not None:
            raise completion.error
        return completion.result

    def _complete_remote_state(self, completion: _RemoteStateCompleted) -> None:
        with self._state_lock:
            pending = self._state_pending.get(completion.correlation_id)
            if pending is not None:
                pending.completion = completion
                pending.event.set()

    def _run_remote_state(
        self, command: _RemoteStateCommand | _RemoteEffectCompleted
    ) -> None:
        if isinstance(command, _RemoteEffectCompleted):
            handle = self._state_handle
            if (
                handle is not None
                and command.generation == self._state_runtime.snapshot(handle).generation
            ):
                self._complete_remote_state(_RemoteStateCompleted(
                    command.correlation_id, command.result, command.error,
                ))
            return
        try:
            data = json.loads(command.payload)
            if self.graph_authority is not None and command.method in {
                "reset",
                "outcome",
                "expansion_preview",
            }:
                grant = data.get("grant")
                if not isinstance(grant, dict):
                    raise PacError("WORKFLOW_REMOTE_INVALID", "request grant is required")
                self._verify(grant)
                handle = self._state_handle
                assert handle is not None
                try:
                    self._effect_queue.put_nowait(_RemoteEffect(
                        self._state_runtime.snapshot(handle).generation, command,
                    ))
                except queue.Full as error:
                    with self._state_lock:
                        self._effect_overloads += 1
                    raise PacError(
                        "WORKFLOW_REMOTE_UNAVAILABLE", "remote effect lane is full"
                    ) from error
                return
            if command.method == "stage_offer":
                result = self._stage_offer_direct(data)
            else:
                result = self._handle_direct(command.method, data)
            completion = _RemoteStateCompleted(command.correlation_id, result)
        except Exception as error:
            completion = _RemoteStateCompleted(command.correlation_id, error=error)
        self._complete_remote_state(completion)

    def _remote_effect_loop(self) -> None:
        while not self.stopping.is_set():
            try:
                effect = self._effect_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                result = self._handle_direct(
                    effect.command.method, json.loads(effect.command.payload)
                )
                completion = _RemoteEffectCompleted(
                    effect.generation, effect.command.correlation_id, result,
                )
            except Exception as error:
                completion = _RemoteEffectCompleted(
                    effect.generation, effect.command.correlation_id,
                    error=error,
                )
            handle = self._state_handle
            while not self.stopping.is_set() and handle is not None:
                admission = self._state_runtime.tell(handle, completion)
                if admission is AdmissionResult.ACCEPTED:
                    break
                if admission is AdmissionResult.CLOSED:
                    break
                sleep(0.01)

    def state_stats(self) -> dict[str, int]:
        with self._state_lock:
            pending = len(self._state_pending)
            overloads = self._effect_overloads
        return {
            "pendingRequests": pending,
            "effectQueued": self._effect_queue.qsize(),
            "effectCapacity": self._effect_queue.maxsize,
            "effectOverloads": overloads,
        }

    def _stage_offer_direct(self, grant: dict) -> None:
        store = PacGraphStore(self.database)
        try:
            with store.write():
                old = store._db.execute(
                    "SELECT grant_json FROM remote_workflow_requests WHERE request_id=?",
                    (grant["requestId"],),
                ).fetchone()
                if old and json.loads(old[0]) != grant:
                    raise PacError(
                        "WORKFLOW_REMOTE_INVALID", "request grant changed on replay"
                    )
                store._db.execute(
                    "INSERT OR IGNORE INTO remote_workflow_requests VALUES (?,?,?,?,?,?,?,?)",
                    (
                        grant["requestId"], grant["graphId"], grant["nodeId"],
                        grant["owner"], grant["origin"], grant["messageId"],
                        grant["deadlineMs"], encoded(grant).decode(),
                    ),
                )
        finally:
            store.close()

    def handle(self, method, data):
        if method in {
            "current", "inspect", "reset", "outcome", "expansion_preview"
        }:
            return self._state_call(method, data)
        return self._handle_direct(method, data)

    def _handle_direct(self, method, data):
        if method == "admit":
            return self.admit_local(data)
        grant = data.get("grant")
        if not isinstance(grant, dict):
            raise PacError("WORKFLOW_REMOTE_INVALID", "request grant is required")
        if method == "offer":
            self.admit_local(grant)
            text = data.get("text")
            if not isinstance(text, str) or hashlib.sha256(
                text.encode()
            ).hexdigest() != grant.get("textDigest"):
                raise PacError(
                    "WORKFLOW_REMOTE_INVALID", "request body does not match grant"
                )
            if not self.wire.call(grant["origin"], "current", {"grant": grant})[
                "current"
            ]:
                raise PacError(
                    "WORKFLOW_REQUEST_STALE", "remote request was withdrawn or expired"
                )
            if dispatch_message_id(grant["effectId"]) != grant["messageId"]:
                raise PacError("WORKFLOW_REMOTE_INVALID", "invalid delivery binding")
            if self.graph_authority is None:
                self._state_call("stage_offer", grant)
            else:
                self.graph_authority.stage_remote_offer(encoded(grant).decode())
            delivered = self.app._pac_notification_io.deliver(
                effect_id=grant["effectId"],
                sender=grant["origin"],
                target=grant["owner"],
                conversation_id=f"pac-{grant['graphId']}",
                text=text,
                expires_at_ms=grant["deadlineMs"],
            )
            return {"ok": True, "messageId": str(delivered.message_id)}
        self._verify(grant)
        if method == "current":
            return self.current(grant)
        if method == "expansion_preview":
            if "pac-expansion-v1" not in grant.get("features", ()):
                raise PacError(
                    "WORKFLOW_REMOTE_INVALID",
                    "remote peer does not support PAC expansion",
                    {"field": "expansion", "reason": "unsupported-peer"},
                )
            if not self.current(grant)["current"]:
                raise PacError(
                    "WORKFLOW_REQUEST_STALE", "remote planner request is no longer current"
                )
            return preview_origin_expansion(self.app, self.database, grant, data)
        if method == "inspect":
            # Only the exact delegated node is disclosed, never other actors'
            # progress or a general-purpose status/list proxy.
            graph = self._local_workflow().status(run_id=grant["graphId"])
            node = next(n for n in graph["nodes"] if n["nodeId"] == grant["nodeId"])
            return {
                "ok": True,
                "graphId": grant["graphId"],
                "node": node,
                "requestId": node["requestId"],
                "state": graph["state"],
                "sender": graph["sender"],
                "current": self.current(grant)["current"],
            }
        if method == "reset":
            reason = data.get("reasonRef")
            if reason is not None and (
                not isinstance(reason, str) or len(reason) > 16384
            ):
                raise PacError(
                    "WORKFLOW_REMOTE_INVALID", "invalid reset evidence reference"
                )
            workflow = self._local_workflow()
            with self.outcome_lock:
                replay = self._receipt(grant, "reset", reason)
                if replay is not None:
                    return replay
                if self.graph_authority is not None:
                    self.graph_authority.reset_flag(
                        grant["graphId"], grant["nodeId"], actor=grant["owner"],
                        reason_ref=reason, expected_request=grant["requestId"],
                    )
                else:
                    store = PacGraphStore(self.database)
                    try:
                        PacReactor(store).reset_flag(
                            grant["graphId"], grant["nodeId"],
                            actor=grant["owner"], reason_ref=reason,
                            expected_request=grant["requestId"],
                        )
                    finally:
                        store.close()
                workflow.submit_timer(time_ns() // 1_000_000)
                return self._receipt(grant, "reset", reason)
        if method == "outcome":
            action, reason = data.get("action"), data.get("reasonRef")
            if (
                action not in ("complete", "fail")
                or not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 16384
            ):
                raise PacError(
                    "WORKFLOW_REMOTE_INVALID",
                    "explicit complete/fail and evidence reference required",
                )
            try:
                output_text = validate_workflow_output_text(data.get("outputText"))
            except ValueError as error:
                raise PacError("WORKFLOW_OUTPUT_INVALID", str(error)) from error
            workflow = self._local_workflow()
            expansion = data.get("expansion")
            expansion_digest = (
                data.get("expansionDigest") if expansion is not None else None
            )
            if expansion is not None:
                if action != "complete" or "pac-expansion-v1" not in grant.get(
                    "features", ()
                ):
                    raise PacError(
                        "WORKFLOW_REMOTE_INVALID",
                        "remote expansion capability is absent",
                        {"field": "expansion", "reason": "unsupported-peer"},
                    )
            with self.outcome_lock:
                replay = self._receipt(
                    grant, action, reason, output_text, expansion_digest
                )
                if replay is not None:
                    return replay
                if expansion is not None:
                    expansion = validate_expansion_outcome(data)
                    expansion_digest = data["expansionDigest"]
                if not self.current(grant)["current"]:
                    raise PacError(
                        "WORKFLOW_REQUEST_STALE",
                        "request no longer authorizes an outcome",
                    )
                if action == "complete":
                    context = None
                    if expansion is not None:
                        from hyprial.daemon.impl.pac.graphs.authority.expansion import (
                            prepare_expansion,
                        )

                        prepare = (
                            self.graph_authority.prepare_expansion
                            if self.graph_authority is not None
                            else prepare_expansion
                        )
                        context = prepare(
                            database=self.database,
                            home=self.app.hyprial_home,
                            raw=expansion,
                            graph_id=grant["graphId"],
                            node_id=grant["nodeId"],
                            request_id=grant["requestId"],
                            actor=grant["owner"],
                            at=time_ns() // 1_000_000,
                            machine=self.app.node_id,
                            local_owner=self.app.owner,
                        )
                        self.app._workflow_admit(context.spec, grant["owner"])
                    if self.graph_authority is not None:
                        self.graph_authority.set_flag(
                            grant["graphId"], grant["nodeId"],
                            actor=grant["owner"], reason_ref=reason,
                            expected_request=grant["requestId"],
                            output_text=output_text,
                            expansion=expansion,
                            expansion_context=context,
                        )
                    else:
                        store = PacGraphStore(self.database)
                        try:
                            PacReactor(store, expansion_context=context).set_flag(
                                grant["graphId"], grant["nodeId"],
                                actor=grant["owner"], reason_ref=reason,
                                expected_request=grant["requestId"],
                                output_text=output_text,
                                expansion=expansion,
                            )
                        finally:
                            store.close()
                else:
                    workflow.fail(
                        graph_id=grant["graphId"],
                        node_id=grant["nodeId"],
                        actor=grant["owner"],
                        request_id=grant["requestId"],
                        reason_ref=reason,
                        output_text=output_text,
                    )
                workflow.submit_timer(time_ns() // 1_000_000)
                return self._receipt(
                    grant, action, reason, output_text, expansion_digest
                )
        raise PacError("WORKFLOW_REMOTE_INVALID", "unsupported remote operation")

    def forward(self, method, params, caller):
        grant = self._lookup(
            graph_id=params.get("graphId") or params.get("runId"),
            node_id=params.get("nodeId") or params.get("target"),
            request_id=params.get("requestId"),
            actor=caller,
        )
        if grant is None:
            return None
        if method == "pac.flag.reset":
            return self.wire.call(
                grant["origin"],
                "reset",
                {"grant": grant, "reasonRef": params.get("reasonRef")},
            )
        if method in ("workflow.complete", "workflow.fail"):
            result = self._enqueue(
                grant,
                method.split(".")[-1],
                params["reasonRef"],
                params.get("outputText"),
                expansion=params.get("expansion"),
            )
            if result.get("returnState") == "rejected":
                raise PacError(result["error"]["code"], result["error"]["message"])
            return result
        if method == "workflow.node.inspect":
            store = PacGraphStore(self.database, read_only=True)
            try:
                row = store._db.execute(
                    "SELECT result_json FROM remote_workflow_outbox WHERE request_id=?",
                    (grant["requestId"],),
                ).fetchone()
                submission = (
                    (json.loads(row[0]) if row[0] else {"returnState": "pending"})
                    if row
                    else {"returnState": "not-submitted"}
                )
            finally:
                store.close()
            try:
                result = self.wire.call(grant["origin"], "inspect", {"grant": grant})
            except PacError as error:
                if error.code != "WORKFLOW_REMOTE_UNAVAILABLE":
                    raise
                result = {
                    "ok": True,
                    "graphId": grant["graphId"],
                    "requestId": grant["requestId"],
                    "current": None,
                    "authorityAvailable": False,
                }
            return {**result, "return": submission}
        return None

    def delivery_current(self, message_id):
        grant = self._lookup(message_id=message_id)
        if grant is None:
            return {"current": True}
        if time_ns() // 1_000_000 > grant["deadlineMs"]:
            return {"current": False}
        return self.wire.call(grant["origin"], "current", {"grant": grant})

    def outcome(self, result):
        grant = self._lookup(message_id=result.delivery_id, actor=result.recipient)
        if grant is None:
            return False
        if result.status.value == "completed":
            return True  # a completed native turn remains NOT business completion
        code = result.failure_code or "HARNESS_TURN_FAILED"
        try:
            self._enqueue(grant, "fail", f"harness:{code}", flush=False)
        except PacError as error:
            if error.code not in (
                "WORKFLOW_REQUEST_STALE",
                "WORKFLOW_OUTCOME_CONFLICT",
            ):
                raise
        return True
