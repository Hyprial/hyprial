"""One-process-per-gateway Lark adapter worker."""

from __future__ import annotations

import json
import socket
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from hyprial.kernel import Logger
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError

from hyprial.daemon.impl.adapters.lark.contracts.wire import (
    MAX_RUNTIME_TARGET_ITEM_BYTES,
    AgentDirectoryEntry,
    ActorTarget,
    HarnessReceipt,
    HarnessRequest,
    HarnessSubmissionRejected,
    RuntimeTarget,
    is_safe_runtime_target_field,
)

def _ipc(
    socket_path: Path, method: str, params: dict[str, Any], *, timeout: float = 15.0
) -> dict[str, Any]:
    request_id = str(uuid4())
    frame = {"version": 1, "id": request_id, "method": method, "params": params}
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    deadline = time.monotonic() + max(0.0, timeout)
    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("daemon adapter request deadline elapsed")
        return value
    try:
        client.settimeout(remaining())
        client.connect(str(socket_path))
        client.settimeout(remaining())
        client.sendall(json.dumps(frame, separators=(",", ":")).encode() + b"\n")
        buffer = bytearray()
        while b"\n" not in buffer:
            client.settimeout(remaining())
            chunk = client.recv(64 * 1024)
            if not chunk:
                raise RuntimeError("daemon disconnected from Lark adapter")
            buffer.extend(chunk)
        response = json.loads(buffer.partition(b"\n")[0])
    finally:
        client.close()
    if not isinstance(response, dict):
        raise RuntimeError("daemon returned an invalid adapter response")
    if (type(response.get("version")) is not int or response["version"] != 1
            or response.get("id") != request_id
            or (("error" in response) == ("result" in response))):
        raise RuntimeError("daemon adapter response does not match request envelope")
    failure = response.get("error")
    if isinstance(failure, dict):
        # PR #332 F3: keep the envelope's stable wire code on the raised
        # error.  Callers must distinguish a transient restore-gate refusal
        # (DAEMON_RESTORING) from a permanent rejection; a bare RuntimeError
        # carrying only the message made every refusal look permanent.
        # PR #332 F4②: transient codes now deserialise through the shared
        # registry into the SAME class every other client gets (still a
        # DaemonRequestError, so existing handlers keep catching it).
        code = failure.get("code")
        message = str(failure.get("message", "daemon request failed"))
        if isinstance(code, str) and code:
            if code == ipc_errors.SUBMIT_OUTCOME_UNKNOWN:
                raise ipc_errors.SubmitOutcomeUnknownError(message, failure.get("data"))
            transient = ipc_errors.transient_error_from_code(
                code, message, failure.get("data")
            )
            if transient is not None:
                raise transient
            raise DaemonRequestError(code, message, failure.get("data"))
        raise RuntimeError(message)
    result = response.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("daemon adapter response has no result")
    return result


class _Harness:
    def __init__(self, socket_path: Path, *, logger: Logger | None = None) -> None:
        self.socket_path = socket_path
        self._logger = logger

    def send_request(
        self, request: HarnessRequest, *, timeout: float | None = None
    ) -> HarnessReceipt:
        operation_id = f"lark-inbound:{request.from_actor_id}:{request.message_id}"
        result = _ipc(
            self.socket_path,
            "message.send",
            {
                "actor": request.from_actor_id,
                "to": [request.to.actor_id],
                "message": request.text,
                "conversationId": request.conversation_id,
                "providerMetadata": dict(request.provider_metadata),
                "idempotencyKey": operation_id,
            },
            **({"timeout": timeout} if timeout is not None else {}),
        )
        deliveries = result.get("deliveries")
        expected_message_id = str(uuid5(
            NAMESPACE_URL, f"hyprial:send:{operation_id}:{request.to.actor_id}:0"
        ))
        if (result.get("operationId") != operation_id
                or result.get("conversationId") != request.conversation_id
                or result.get("messageId") != expected_message_id
                or not isinstance(deliveries, list) or len(deliveries) != 1
                or not isinstance(deliveries[0], dict)
                or deliveries[0].get("target") != request.to.actor_id
                or deliveries[0].get("messageId") != expected_message_id
                or type(deliveries[0].get("accepted")) is not bool
                or type(deliveries[0].get("queued")) is not bool):
            raise RuntimeError("daemon submission receipt does not match frozen request")
        if (result.get("outcomeKnown") is False
                or deliveries[0].get("outcomeKnown") is False
                or deliveries[0].get("code") == ipc_errors.SUBMIT_OUTCOME_UNKNOWN):
            raise ipc_errors.SubmitOutcomeUnknownError(
                "daemon submission result remains unconfirmed",
                {"messageId": expected_message_id, "outcomeKnown": False},
            )
        if (result.get("ok") is False and deliveries[0]["accepted"] is False
                and deliveries[0]["queued"] is False):
            raise HarnessSubmissionRejected("daemon explicitly rejected the Lark request")
        if (result.get("ok") is not True or not isinstance(result.get("messageId"), str)
                or deliveries[0]["accepted"] is not True):
            raise RuntimeError("daemon did not accept the Lark request")
        return HarnessReceipt(str(result["messageId"]))

    def accept_delivery(self, delivery_id: str, native_message_id: str) -> None:
        _ipc(
            self.socket_path,
            "message.ack",
            {"actor": "lark-adapter", "messageId": delivery_id},
        )

    def reject_delivery(
        self, delivery_id: str, error: str, *, deterministic: bool
    ) -> None:
        # The durable outbox is daemon-owned; settlement happens over IPC.
        # The reply failure reason must still reach the adapter log -- an
        # unlogged ``del error`` here is exactly what made harness_reply
        # failures look silent (the code/msg the platform returned was
        # dropped on the floor).  ``error`` carries only the summary
        # (operation, code, platform msg); never the reply body.
        if self._logger is not None:
            try:
                self._logger.log(
                    "warn",
                    "delivery.rejected",
                    messageId=delivery_id,
                    reason=error,
                    deterministic=deterministic,
                )
            except (NameError, ImportError):
                raise
            except OSError:
                pass

class _Routes:
    """The worker's read-only view of daemon-owned routing state.

    The TS-era pieces are gone on purpose: ``legacyConversationPins`` had no
    Python writer and an empty production value; the ``default``-route
    fallback and route-name mention matching both returned a Lark chat id
    where an actor id belongs, and could never match anything real.  What
    remains is one question -- "which agent is this adapter pinned to?" --
    answered by the daemon, which owns the adapter->agent index.
    """

    def __init__(
        self,
        *,
        gateway_name: str,
        socket_path: Path,
        report: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        self.gateway_name = gateway_name
        self.socket_path = socket_path
        self._report = report or self._report_to_stderr
        self._has_cached_pin = False
        self._cached_pin: str | None = None

    @staticmethod
    def _report_to_stderr(payload: dict[str, object]) -> None:
        print(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            file=sys.stderr,
            flush=True,
        )

    @staticmethod
    def _target(actor: str, display_name: str | None = None) -> ActorTarget:
        return ActorTarget(
            actor_id=actor,
            actor_key=actor,
            display_name=display_name or actor,
        )

    def actor_by_key(self, actor_key: str) -> ActorTarget | None:
        return self._target(actor_key)

    def pinned_actor(self, conversation_id: str) -> ActorTarget | None:
        """The agent this adapter is pinned to, per the daemon's pin index.

        Pins live in the daemon's agents database (``pins`` table); the
        worker never reads daemon state files itself.  Every message queries
        the daemon; a successful empty result replaces the cache (so unpin is
        immediate), while a query failure can reuse a last successful pin.
        Without a known destination, the failure propagates to the inbound
        retry/custody boundary; unavailable authority is not proof of no pin.
        """

        del conversation_id  # a pin is per adapter, never per conversation
        try:
            result = _ipc(self.socket_path, "adapter.pins", {})
            pins = result.get("pins")
            if not isinstance(pins, dict):
                raise RuntimeError("daemon pins response must contain an object")
            actor = pins.get(self.gateway_name)
            if self.gateway_name in pins and (
                not isinstance(actor, str) or not actor
            ):
                raise RuntimeError("daemon returned an invalid adapter pin")
        except (
            OSError,
            RuntimeError,
            json.JSONDecodeError,
            UnicodeDecodeError,
        ) as error:
            fallback = "no-successful-query"
            if self._has_cached_pin:
                fallback = (
                    "last-successful-pin"
                    if self._cached_pin is not None
                    else "last-successful-unpinned"
                )
            warning: dict[str, object] = {
                "status": "warning",
                "event": "lark.pin.query_failed",
                "errorType": type(error).__name__,
                "fallback": fallback,
            }
            if self._cached_pin is not None:
                warning["cachedActor"] = self._cached_pin
            self._report(warning)
            if self._cached_pin is None:
                raise
            actor = self._cached_pin
        else:
            self._cached_pin = actor
            self._has_cached_pin = True
        if actor is None:
            return None
        assert isinstance(actor, str)
        return self._target(actor)

    def list_runtime_targets(self) -> tuple[RuntimeTarget, ...]:
        result = _ipc(self.socket_path, "targets", {"kind": "agent"})
        raw_targets = result.get("targets")
        if not isinstance(raw_targets, list):
            raise RuntimeError("daemon targets response must contain an array")
        targets: list[RuntimeTarget] = []
        for item in raw_targets:
            if not isinstance(item, dict) or item.get("targetKind") != "agent":
                continue
            target_uri = item.get("targetUri")
            actor = item.get("actor")
            status = item.get("status")
            if not all(
                is_safe_runtime_target_field(value)
                for value in (target_uri, actor, status)
            ):
                continue
            assert isinstance(target_uri, str)
            assert isinstance(actor, str)
            assert isinstance(status, str)
            if (
                len(f"- {target_uri} [{status}]".encode("utf-8"))
                > MAX_RUNTIME_TARGET_ITEM_BYTES
            ):
                continue
            from hyprial.kernel import short_actor_name

            alias = short_actor_name(actor)
            targets.append(RuntimeTarget(target_uri, actor, alias, status))
        return tuple(
            sorted(
                targets,
                key=lambda item: (item.target_uri, item.actor, item.status),
            )
        )

    def list_agent_directory(self) -> tuple[AgentDirectoryEntry, ...]:
        """Join daemon entity records with its live target union.

        ``ps.agents`` is the entity/configuration projection backed by the
        agent registry.  ``targets`` is the daemon's existing entity-plus-
        presence union and owns the current running/down verdict.  The Lark
        worker consumes both public IPC projections and never opens SQLite.
        """

        ps = _ipc(self.socket_path, "ps", {})
        raw_agents = ps.get("agents")
        if not isinstance(raw_agents, list):
            raise RuntimeError("daemon ps response must contain an agents array")
        targets = self.list_runtime_targets()
        target_status = {target.target_uri: target.status for target in targets}
        entries: dict[str, AgentDirectoryEntry] = {}
        for item in raw_agents:
            if not isinstance(item, dict):
                continue
            uri = item.get("uri")
            name = item.get("actor")
            if not isinstance(uri, str) or not is_safe_runtime_target_field(uri):
                continue
            if not isinstance(name, str) or not is_safe_runtime_target_field(name):
                name = uri
            raw_pins = item.get("pinnedAdapters", ())
            pins = (
                tuple(
                    pin
                    for pin in raw_pins
                    if isinstance(pin, str) and is_safe_runtime_target_field(pin)
                )
                if isinstance(raw_pins, list)
                else ()
            )
            preferred = item.get("preferredHarness")
            if not isinstance(preferred, str) or not is_safe_runtime_target_field(
                preferred
            ):
                preferred = None
            raw_status = target_status.get(uri, item.get("status", "offline"))
            status = raw_status if raw_status in {"online", "offline"} else "offline"
            entries[uri] = AgentDirectoryEntry(
                name=name,
                status="running" if status == "online" else "down",
                pinned_adapters=tuple(sorted(set(pins))),
                preferred_harness=preferred,
            )
        for target in targets:
            entries.setdefault(
                target.target_uri,
                AgentDirectoryEntry(
                    name=target.actor or target.target_uri,
                    status="running" if target.status == "online" else "down",
                ),
            )
        return tuple(entries[key] for key in sorted(entries))

    def resolve_runtime_target(self, token: str) -> tuple[ActorTarget, ...]:
        if not is_safe_runtime_target_field(token):
            return ()
        targets = self.list_runtime_targets()
        exact = tuple(
            target.as_actor_target()
            for target in targets
            if token in {target.target_uri, target.actor}
        )
        if exact:
            return exact
        folded = token.casefold()
        return tuple(
            target.as_actor_target()
            for target in targets
            if target.alias.casefold() == folded
        )
