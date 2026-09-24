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
import secrets
import sqlite3
import threading
from time import time_ns, monotonic
from uuid import uuid4

from hyprial.contracts import ipc_errors
from hyprial.dispatch.admission import dispatch_gate
from hyprial.dispatch.identity import dispatch_message_id
from hyprial.pac.errors import PacError
from hyprial.pac.reactor import PacReactor
from hyprial.pac.store import PacGraphStore, default_database_path
from hyprial.pac.workflow_graph import input_token
from hyprial.transport.keys import KeySpace
from hyprial.uri import parse_agent_uri

MAX_FRAME = 256 * 1024
PREFIX = "hyprial/v1/pac-workflow"


def encoded(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


def endpoint(actor):
    principal = parse_agent_uri(actor)
    if principal is None:
        raise PacError(
            "WORKFLOW_REMOTE_INVALID", "remote authority must be a canonical agent URI"
        )
    return f"{PREFIX}/{KeySpace.encode_identity(principal[0])}/{KeySpace.encode_identity(principal[1])}"


class RemoteWire:
    """Bounded request/reply channel; correlation IDs confer no authority."""

    def __init__(self, transport, authority, handler):
        self.transport, self.handler = transport, handler
        self.root = endpoint(authority)
        self.pending = {}
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(8)
        self.closed = False
        self.registrations = [
            transport.subscribe(self.root + "/request", self.receive),
            transport.subscribe(self.root + "/reply/*", self.reply),
        ]

    def call(self, target, method, data, *, timeout=4.0):
        token = uuid4().hex
        request = encoded(
            {
                "id": token,
                "method": method,
                "data": data,
                "reply": self.root + "/reply/" + token,
            }
        )
        if len(request) > MAX_FRAME:
            raise PacError(
                "WORKFLOW_REMOTE_INVALID", "remote request exceeds frame limit"
            )
        event, box = threading.Event(), []
        with self.lock:
            if self.closed or len(self.pending) >= 32:
                raise PacError(
                    "WORKFLOW_REMOTE_UNAVAILABLE", "remote channel unavailable or busy"
                )
            self.pending[token] = (event, box)
        try:
            self.transport.put(endpoint(target) + "/request", request)
            if not event.wait(timeout) or not box:
                raise PacError(
                    "WORKFLOW_REMOTE_UNAVAILABLE",
                    "remote daemon did not acknowledge the request",
                )
            reply = box[0]
            if "error" in reply:
                raise PacError(reply["error"]["code"], reply["error"]["message"])
            return reply["result"]
        finally:
            with self.lock:
                self.pending.pop(token, None)

    def reply(self, sample):
        if len(sample.payload) > MAX_FRAME:
            return
        try:
            value = json.loads(sample.payload)
            token = sample.key.rsplit("/", 1)[-1]
            if not isinstance(value, dict) or value.get("id") != token:
                return
            with self.lock:
                pending = self.pending.get(token)
                if pending and not pending[1]:
                    pending[1].append(value)
                    pending[0].set()
        except (ValueError, TypeError):
            return

    def receive(self, sample):
        if self.closed or len(sample.payload) > MAX_FRAME:
            return
        try:
            request = json.loads(sample.payload)
            token, reply = request["id"], request["reply"]
            if (
                not isinstance(token, str)
                or len(token) != 32
                or not isinstance(reply, str)
                or not reply.startswith(PREFIX + "/")
                or not reply.endswith("/reply/" + token)
                or any(c in reply for c in "*?#[ ]")
            ):
                return
            if not isinstance(request["data"], dict) or not isinstance(
                request["method"], str
            ):
                return
        except (KeyError, ValueError, TypeError):
            return
        with self.lock:
            if self.closed or not self.slots.acquire(blocking=False):
                return

        def run():
            try:
                try:
                    response = {
                        "result": self.handler(request["method"], request["data"])
                    }
                except (sqlite3.Error, OSError):
                    response = {
                        "error": {
                            "code": "WORKFLOW_REMOTE_UNAVAILABLE",
                            "message": "remote service temporarily unavailable",
                        }
                    }
                except Exception as error:
                    response = {
                        "error": {
                            "code": getattr(error, "code", "WORKFLOW_REMOTE_INVALID"),
                            "message": str(error)
                            if hasattr(error, "code")
                            else "invalid remote request",
                        }
                    }
                payload = encoded({"id": token, **response})
                if len(payload) <= MAX_FRAME and not self.closed:
                    self.transport.put(reply, payload)
            finally:
                self.slots.release()

        threading.Thread(target=run, name="pac-remote-rpc", daemon=True).start()

    def stop(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            for event, _ in self.pending.values():
                event.set()
        for registration in self.registrations:
            registration.close()

    def drain(self, timeout):
        deadline = monotonic() + max(0, timeout)
        acquired = 0
        try:
            for _ in range(8):
                if not self.slots.acquire(timeout=max(0, deadline - monotonic())):
                    return False
                acquired += 1
            return True
        finally:
            for _ in range(acquired):
                self.slots.release()

    def close(self, timeout=5.0):
        self.stop()
        return self.drain(timeout)


class RemoteWorkflow:
    def __init__(self, application, transport):
        self.app = application
        self.database = default_database_path(application.state_dir)
        self.authority = application._dispatch_service_actor
        self.outcome_lock = threading.Lock()
        self.wire = None
        self.pump = None
        self.stopping = threading.Event()
        store = None
        try:
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
        return drained and not self.pump.is_alive()

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
                },
            )
            result = {**result, "returnState": "accepted"}
        except PacError as error:
            if error.code == "WORKFLOW_REMOTE_UNAVAILABLE":
                return {"ok": True, "requestId": request_id, "returnState": "pending"}
            result = {
                "ok": False,
                "requestId": request_id,
                "returnState": "rejected",
                "error": {"code": error.code, "message": str(error)},
            }
        store = PacGraphStore(self.database)
        try:
            with store.write():
                store._db.execute(
                    "UPDATE remote_workflow_outbox SET result_json=? WHERE request_id=? AND result_json IS NULL",
                    (encoded(result).decode(), request_id),
                )
        finally:
            store.close()
        return result

    def _enqueue(self, grant, action, reason):
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
        store = PacGraphStore(self.database)
        try:
            with store.write():
                row = store._db.execute(
                    "SELECT * FROM remote_workflow_outbox WHERE request_id=?",
                    (grant["requestId"],),
                ).fetchone()
                if row and (row["action"], row["reason_ref"]) != (action, reason):
                    raise PacError(
                        "WORKFLOW_OUTCOME_CONFLICT",
                        "request already has a queued or accepted outcome",
                    )
                store._db.execute(
                    "INSERT OR IGNORE INTO remote_workflow_outbox(request_id,action,reason_ref,result_json) VALUES (?,?,?,NULL)",
                    (grant["requestId"], action, reason),
                )
        finally:
            store.close()
        return self._flush(grant["requestId"])

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
        return {"ok": True, "owner": actor}

    def admit(self, node):
        return self.wire.call(
            node.owner,
            "admit",
            {
                "owner": node.owner,
                "role": node.role,
                "firstOutputEta": node.first_output_eta,
                "humanGatesDeclared": node.human_gates is not None,
            },
        )

    def send(self, request, *, text, idempotency_key):
        if not self.remote(request["owner"]):
            return None
        grant = {
            **request,
            "origin": self.authority,
            "messageId": dispatch_message_id(f"pac:{idempotency_key}"),
            "effectId": f"pac:{idempotency_key}",
            "textDigest": hashlib.sha256(text.encode()).hexdigest(),
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

    def _receipt(self, grant, action, reason):
        store = PacGraphStore(self.database, read_only=True)
        try:
            row = store._db.execute(
                "SELECT * FROM workflow_outcome_receipts WHERE request_id=?",
                (("reset:" if action == "reset" else "") + grant["requestId"],),
            ).fetchone()
            if row:
                if (row["actor"], row["action"], row["reason_ref"]) != (
                    grant["owner"],
                    action,
                    reason,
                ):
                    raise PacError(
                        "WORKFLOW_OUTCOME_CONFLICT",
                        "request already has a different accepted outcome",
                    )
                return json.loads(row["result_json"])
        finally:
            store.close()
        return None

    def handle(self, method, data):
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
                            grant["requestId"],
                            grant["graphId"],
                            grant["nodeId"],
                            grant["owner"],
                            grant["origin"],
                            grant["messageId"],
                            grant["deadlineMs"],
                            encoded(grant).decode(),
                        ),
                    )
            finally:
                store.close()
            delivered = self.app._pac_notification_io.deliver(
                effect_id=grant["effectId"],
                sender=grant["origin"],
                target=grant["owner"],
                conversation_id=f"pac-{grant['graphId']}",
                text=text,
            )
            return {"ok": True, "messageId": str(delivered.message_id)}
        self._verify(grant)
        if method == "current":
            return self.current(grant)
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
                store = PacGraphStore(self.database)
                try:
                    PacReactor(store).reset_flag(
                        grant["graphId"],
                        grant["nodeId"],
                        actor=grant["owner"],
                        reason_ref=reason,
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
            workflow = self._local_workflow()
            with self.outcome_lock:
                replay = self._receipt(grant, action, reason)
                if replay is not None:
                    return replay
                if not self.current(grant)["current"]:
                    raise PacError(
                        "WORKFLOW_REQUEST_STALE",
                        "request no longer authorizes an outcome",
                    )
                if action == "complete":
                    store = PacGraphStore(self.database)
                    try:
                        PacReactor(store).set_flag(
                            grant["graphId"],
                            grant["nodeId"],
                            actor=grant["owner"],
                            reason_ref=reason,
                            expected_request=grant["requestId"],
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
                    )
                workflow.submit_timer(time_ns() // 1_000_000)
                return self._receipt(grant, action, reason)
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
            result = self._enqueue(grant, method.split(".")[-1], params["reasonRef"])
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
            self._enqueue(grant, "fail", f"harness:{code}")
        except PacError as error:
            if error.code not in (
                "WORKFLOW_REQUEST_STALE",
                "WORKFLOW_OUTCOME_CONFLICT",
            ):
                raise
        return True
