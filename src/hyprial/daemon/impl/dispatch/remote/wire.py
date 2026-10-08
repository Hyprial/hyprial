"""Remote wire framing, capability encoding and the request/response port."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from time import monotonic
from uuid import uuid4

from hyprial.identity import PacError
from hyprial.daemon.impl.transport.keys import KeySpace
from hyprial.kernel import parse_agent_uri

MAX_FRAME = 256 * 1024
PREFIX = "hyprial/v1/pac-workflow"


@dataclass(frozen=True, slots=True)
class _RemoteStateCommand:
    correlation_id: str
    method: str
    payload: bytes


@dataclass(frozen=True, slots=True)
class _RemoteStateCompleted:
    correlation_id: str
    result: object = None
    error: Exception | None = None


@dataclass(frozen=True, slots=True)
class _RemoteEffect:
    generation: int
    command: _RemoteStateCommand


@dataclass(frozen=True, slots=True)
class _RemoteEffectCompleted:
    generation: int
    correlation_id: str
    result: object = None
    error: Exception | None = None


class _PendingRemoteState:
    def __init__(self) -> None:
        self.event = threading.Event()
        self.completion: _RemoteStateCompleted | None = None


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
                raise PacError(
                    reply["error"]["code"],
                    reply["error"]["message"],
                    reply["error"].get("data"),
                )
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
                            **(
                                {"data": error.data}
                                if getattr(error, "data", None) is not None
                                else {}
                            ),
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
