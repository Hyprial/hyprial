"""Best-effort Zenoh propagation for owner-sovereign organization context.

The network can offer candidates, but it cannot adopt them.  Every inbound
path in this module ends at :meth:`OrgContextStore.stage`; only ``hyprial org
import`` calls the accepted-slot mutation.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import time
import uuid
import weakref

from hyprial.daemon.impl.transport.session_actor import TransportSessionAuthority
from hyprial.daemon.impl.transport import (
    KeySpace,
    Registration,
    TransportSample,
    TransportSession,
    ZenohTransport)
from hyprial.kernel import (
    ActorEvent,
    ActorEventKind,
    ActorHandle,
    ActorRuntime,
    ActorSpec,
    AdmissionResult)
from hyprial.kernel import EffectCompleted, EffectLane, EffectRequest

from hyprial.daemon.impl.org.document  import OrgDocumentError, parse_document, serialize_document
from hyprial.daemon.impl.org.store  import OrgContextStore, OrgStoreError, OrgVersionError, PendingRecord


ORG_WIRE_VERSION = 1
ORG_WIRE_TYPE = "org-context"
_ENVELOPE_KEYS = frozenset({"schemaVersion", "type", "sourceNode", "document"})
_MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
_ORG_CONTEXT_CAPACITY = 64
MeshLogger = Callable[..., None]


@dataclass(frozen=True, slots=True)
class OrgFetchResult:
    requested_sources: tuple[str, ...]
    response_count: int
    staged: tuple[PendingRecord, ...]

    @property
    def rejected_count(self) -> int:
        return self.response_count - len(self.staged)

    def to_json(self) -> dict[str, object]:
        return {
            "requestedSources": list(self.requested_sources),
            "responseCount": self.response_count,
            "receivedCount": len(self.staged),
            "rejectedCount": self.rejected_count,
            "candidates": [item.to_json() for item in self.staged],
        }


@dataclass(frozen=True, slots=True)
class _OrgContextCommand:
    operation_id: str
    generation: int
    action: str
    payload: object


@dataclass(frozen=True, slots=True)
class _OrgContextEffectResult:
    value: object = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _StageCandidate:
    payload: bytes
    expected_source: str | None
    allowed_sources: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class _PublishAccepted:
    pass


@dataclass(frozen=True, slots=True)
class _FetchCandidates:
    source: str | None
    allowed_sources: tuple[str, ...] | None
    timeout: float


class OrgContextMesh:
    """Publish, answer, fetch, validate, and stage org-context candidates."""

    def __init__(
        self,
        session: TransportSession,
        store: OrgContextStore,
        node_id: str,
        *,
        logger: MeshLogger | None = None,
        keys: KeySpace | None = None,
    ) -> None:
        if not node_id or any(character in node_id for character in "\r\n"):
            raise ValueError("org-context source node must be a non-empty single line")
        self._session = session
        self._store = store
        self.node_id = node_id
        self._logger = logger
        self._keys = keys or KeySpace()
        self._lock = threading.RLock()
        self._generation = 1
        self._closing = False
        self._closed = False
        self._ingress_dropped = 0
        self._waiters: dict[str, tuple[threading.Event, list[object]]] = {}
        self._calls: dict[str, _OrgContextCommand] = {}
        self._submitted: set[str] = set()
        self._deferred: list[_OrgContextCommand] = []
        owner_ref = weakref.ref(self)

        def handle_command(command: object) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_authority_command(command)

        def actor_event(event: ActorEvent) -> None:
            owner = owner_ref()
            if owner is not None:
                owner._on_actor_event(event)

        def execute_effect(command: _OrgContextCommand) -> _OrgContextEffectResult:
            owner = owner_ref()
            if owner is None:
                return _OrgContextEffectResult(error="org context closed")
            try:
                return _OrgContextEffectResult(value=owner._execute_effect(command))
            except BaseException as error:
                return _OrgContextEffectResult(
                    error=f"{type(error).__name__}: {error}"
                )

        def complete_effect(completion: EffectCompleted[_OrgContextEffectResult]) -> AdmissionResult:
            owner = owner_ref()
            if owner is None:
                return AdmissionResult.CLOSED
            return owner._runtime.tell(owner._authority, completion)

        self._runtime = ActorRuntime(event_sink=actor_event)
        self._authority: ActorHandle = self._runtime.start(
            ActorSpec(
                "org-context-authority",
                lambda: handle_command,
                mailbox_capacity=_ORG_CONTEXT_CAPACITY,
                supervision_profile="state_authority",
            )
        )
        self._effects: EffectLane[_OrgContextCommand, _OrgContextEffectResult] = EffectLane(
            name="org-context-effects",
            execute=execute_effect,
            complete=complete_effect,
            capacity=_ORG_CONTEXT_CAPACITY,
            workers=1,
        )
        self._registrations: list[Registration] = []
        self._accepted_payload = self._wire_payload()
        self._closed = False
        ingress = (
            self._admit_announcement
            if isinstance(session, (ZenohTransport, TransportSessionAuthority))
            else self._on_announcement
        )
        subscription = session.subscribe(self._keys.org_context_any(), ingress)
        self._registrations.append(subscription)
        try:
            self._registrations.append(
                session.declare_queryable(
                    self._keys.org_request(node_id), self._answer_request
                )
            )
        except BaseException:
            subscription.close()
            raise

    def _log(self, level: str, event: str, **fields: object) -> None:
        if self._logger is not None:
            self._logger(level, event, **fields)

    def _on_authority_command(self, command: object) -> None:
        if isinstance(command, EffectCompleted):
            self._complete_authority(command)
            self._effects.acknowledge(command.operation_id, command.generation)
            self._pump_deferred()
            return
        if not isinstance(command, _OrgContextCommand):
            raise TypeError("org-context authority received an invalid command")
        with self._lock:
            if command.generation != self._generation:
                return
            admitted = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admitted is AdmissionResult.ACCEPTED:
                self._deferred = [
                    item
                    for item in self._deferred
                    if item.operation_id != command.operation_id
                ]
                self._submitted.add(command.operation_id)
            elif admitted is AdmissionResult.OVERLOADED:
                if all(
                    item.operation_id != command.operation_id
                    for item in self._deferred
                ):
                    self._deferred.append(command)
        if admitted is AdmissionResult.CLOSED:
            self._complete_authority(
                EffectCompleted(
                    command.operation_id,
                    command.generation,
                    result=_OrgContextEffectResult(
                        error="org-context effect lane is closed"
                    ),
                )
            )

    def _pump_deferred(self) -> None:
        with self._lock:
            if not self._deferred:
                return
            command = self._deferred[0]
            admitted = self._effects.submit(
                EffectRequest(command.operation_id, command.generation, command)
            )
            if admitted is AdmissionResult.ACCEPTED:
                self._deferred.pop(0)
                self._submitted.add(command.operation_id)

    def _complete_authority(
        self, completion: EffectCompleted[_OrgContextEffectResult]
    ) -> None:
        result = completion.result
        with self._lock:
            call = self._calls.get(completion.operation_id)
            if call is None or call.generation != completion.generation:
                return
            pending = self._waiters.pop(completion.operation_id, None)
            self._calls.pop(completion.operation_id, None)
            self._submitted.discard(completion.operation_id)
        if pending is None:
            if completion.error is not None or (
                isinstance(result, _OrgContextEffectResult) and result.error is not None
            ):
                self._ingress_dropped += 1
            return
        done, value = pending
        if completion.error is not None:
            value.append(OrgStoreError(f"org-context effect failed: {completion.error}"))
        elif isinstance(result, _OrgContextEffectResult):
            value.append(
                OrgStoreError(result.error) if result.error is not None else result.value
            )
        else:
            value.append(OrgStoreError("org-context effect returned an invalid result"))
        done.set()

    def _on_actor_event(self, event: ActorEvent) -> None:
        if (
            event.handle.name != "org-context-authority"
            or event.kind is not ActorEventKind.CHILD_RESTARTED
        ):
            return
        with self._lock:
            if event.generation <= self._generation:
                return
            self._generation = event.generation
            deferred_ids = {command.operation_id for command in self._deferred}
            replay = tuple(
                _OrgContextCommand(
                    call.operation_id, event.generation, call.action, call.payload
                )
                for operation_id, call in self._calls.items()
                if operation_id not in self._submitted
            )
            for call in replay:
                self._calls[call.operation_id] = call
            replacements = {command.operation_id: command for command in replay}
            self._deferred = [
                replacements[command.operation_id]
                for command in self._deferred
                if command.operation_id in replacements
            ]
        # Reassociate only commands lost in the old actor mailbox. EffectLane
        # work keeps its original generation and completion custody.
        self._pump_deferred()
        for call in replay:
            if call.operation_id in deferred_ids:
                continue
            admission = self._runtime.tell(self._authority, call)
            if admission is not AdmissionResult.ACCEPTED:
                with self._lock:
                    if all(
                        item.operation_id != call.operation_id
                        for item in self._deferred
                    ):
                        self._deferred.append(call)
                self._pump_deferred()

    def _execute_effect(self, command: _OrgContextCommand) -> object:
        payload = command.payload
        if isinstance(payload, _StageCandidate):
            return self._receive_candidate(
                payload.payload,
                expected_source=payload.expected_source,
                allowed_sources=(
                    None
                    if payload.allowed_sources is None
                    else frozenset(payload.allowed_sources)
                ),
            )
        if isinstance(payload, _PublishAccepted):
            value = self._wire_payload()
            with self._lock:
                self._accepted_payload = value
            if value is None:
                return False
            self._session.put(self._keys.org_context(self.node_id), value)
            return True
        if isinstance(payload, _FetchCandidates):
            return self._fetch_candidates(payload)
        raise TypeError(f"unsupported org-context operation: {type(payload).__name__}")

    @property
    def ingress_dropped(self) -> int:
        return self._ingress_dropped

    def _submit(self, payload: object, *, wait: bool) -> object:
        operation_id = uuid.uuid4().hex
        waiter: tuple[threading.Event, list[object]] | None = None
        with self._lock:
            if self._closing or self._closed:
                if not wait:
                    return AdmissionResult.CLOSED
                raise OrgStoreError("org-context mesh is closing")
            if len(self._calls) >= _ORG_CONTEXT_CAPACITY:
                if not wait:
                    return AdmissionResult.OVERLOADED
                raise OrgStoreError("org-context total custody is full")
            generation = self._generation
            command = _OrgContextCommand(
                operation_id, generation, type(payload).__name__, payload
            )
            if wait:
                waiter = (threading.Event(), [])
                self._waiters[operation_id] = waiter
            self._calls[operation_id] = command
        while True:
            admission = self._runtime.tell(self._authority, command)
            if admission is AdmissionResult.ACCEPTED:
                break
            with self._lock:
                current = self._calls.get(operation_id)
                if (
                    admission is AdmissionResult.CLOSED
                    and current is not None
                    and current.generation != command.generation
                ):
                    command = current
                    continue
                self._waiters.pop(operation_id, None)
                self._calls.pop(operation_id, None)
            if waiter is None:
                return admission
            raise OrgStoreError(f"org-context admission {admission.value}")
        if waiter is None:
            return AdmissionResult.ACCEPTED
        if not waiter[0].wait(60.0):
            raise OrgStoreError(f"org-context operation remains in custody: {operation_id}")
        result = waiter[1][0]
        if isinstance(result, BaseException):
            raise result
        return result

    def _admit_announcement(self, sample: TransportSample) -> None:
        source_from_key: str | None = None
        try:
            source_from_key = self._keys.decode_identity(sample.key.rsplit("/", 1)[-1])
        except (UnicodeDecodeError, ValueError):
            return
        admission = self._submit(
            _StageCandidate(bytes(sample.payload), source_from_key, None), wait=False
        )
        if admission is not AdmissionResult.ACCEPTED:
            self._ingress_dropped += 1

    def _wire_payload(self) -> bytes | None:
        document = self._store.load_accepted()
        if document is None:
            return None
        return json.dumps(
            {
                "schemaVersion": ORG_WIRE_VERSION,
                "type": ORG_WIRE_TYPE,
                "sourceNode": self.node_id,
                "document": serialize_document(document),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def publish_accepted(self) -> bool:
        """Announce only the local accepted slot; pending is never considered."""
        return bool(self._submit(_PublishAccepted(), wait=True))

    def _answer_request(self, _selector: str) -> bytes | None:
        with self._lock:
            return self._accepted_payload

    def _on_announcement(self, sample: TransportSample) -> None:
        source_from_key: str | None = None
        try:
            source_from_key = self._keys.decode_identity(sample.key.rsplit("/", 1)[-1])
        except (UnicodeDecodeError, ValueError) as error:
            self._reject(
                "invalid-envelope", f"announcement key source is invalid: {error}"
            )
            return
        self._receive_candidate(sample.payload, expected_source=source_from_key)

    def _reject(
        self, reason: str, detail: str, *, source_node: str | None = None
    ) -> None:
        fields: dict[str, object] = {"reason": reason, "detail": detail}
        if source_node:
            fields["sourceNode"] = source_node
        self._log("warn", "org.context.rejected", **fields)

    def receive(
        self,
        payload: bytes,
        *,
        expected_source: str | None = None,
        allowed_sources: frozenset[str] | None = None,
    ) -> PendingRecord | None:
        return self._submit(
            _StageCandidate(
                bytes(payload),
                expected_source,
                None if allowed_sources is None else tuple(allowed_sources),
            ),
            wait=True,
        )  # type: ignore[return-value]

    def _receive_candidate(
        self,
        payload: bytes,
        *,
        expected_source: str | None = None,
        allowed_sources: frozenset[str] | None = None,
    ) -> PendingRecord | None:
        """Validate one wire candidate and stage it without ever adopting it."""

        source_node: str | None = None
        if len(payload) > _MAX_ENVELOPE_BYTES:
            self._reject("invalid-envelope", "org-context envelope exceeds 2 MiB")
            return None
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            self._reject("invalid-envelope", f"invalid JSON envelope: {error}")
            return None
        if not isinstance(raw, dict) or frozenset(raw) != _ENVELOPE_KEYS:
            self._reject(
                "invalid-envelope", "org-context envelope fields do not match v1"
            )
            return None
        if (
            raw.get("schemaVersion") != ORG_WIRE_VERSION
            or raw.get("type") != ORG_WIRE_TYPE
        ):
            self._reject(
                "invalid-envelope", "unsupported org-context envelope version or type"
            )
            return None
        raw_source = raw.get("sourceNode")
        source = raw.get("document")
        if (
            not isinstance(raw_source, str)
            or not raw_source
            or any(character in raw_source for character in "\r\n")
            or not isinstance(source, str)
        ):
            self._reject(
                "invalid-envelope", "sourceNode and document must be valid strings"
            )
            return None
        source_node = raw_source
        if expected_source is not None and source_node != expected_source:
            self._reject(
                "source-mismatch",
                f"envelope source {source_node!r} does not match {expected_source!r}",
                source_node=source_node,
            )
            return None
        if source_node == self.node_id:
            return None
        if allowed_sources is not None and source_node not in allowed_sources:
            self._reject(
                "source-unreachable",
                "response source was not present in hyprial targets",
                source_node=source_node,
            )
            return None
        # parse_document is the phase-1 parser and enforces schema before
        # any local version or filesystem action.
        try:
            document = parse_document(source)
        except (OrgDocumentError, TypeError) as error:
            self._reject("schema-invalid", str(error), source_node=source_node)
            return None

        error: OrgVersionError | None = None
        with self._lock:
            accepted = self._store.load_accepted()
            if accepted is not None and document.meta.version <= accepted.meta.version:
                error = OrgVersionError(
                    f"incoming version {document.meta.version} must be newer than "
                    f"accepted version {accepted.meta.version}"
                )
                record = None
            else:
                record = self._store.stage(document, source=source_node)
        if error is not None:
            self._reject("non-monotonic", str(error), source_node=source_node)
            return None
        self._log(
            "info",
            "org.context.staged",
            sourceNode=source_node,
            version=document.meta.version,
            publisher=document.meta.publisher,
        )
        return record

    def fetch(
        self,
        *,
        source: str | None = None,
        allowed_sources: Iterable[str] | None = None,
        timeout: float = 2.0,
    ) -> OrgFetchResult:
        """Ask one source or all sources, optionally restricted by targets."""

        allowed = None if allowed_sources is None else tuple(allowed_sources)
        if source is not None and allowed is not None and source not in allowed:
            raise ValueError(f"org-context source {source!r} is not reachable")
        if source is None and allowed is not None and not allowed:
            return OrgFetchResult((), 0, ())
        return self._submit(
            _FetchCandidates(source, allowed, timeout), wait=True
        )  # type: ignore[return-value]

    def _fetch_candidates(self, request: _FetchCandidates) -> OrgFetchResult:
        source = request.source
        allowed = (
            None
            if request.allowed_sources is None
            else frozenset(request.allowed_sources)
        )
        requested = (source,) if source is not None else tuple(sorted(allowed or ()))
        key = (
            self._keys.org_request(source)
            if source is not None
            else self._keys.org_request_any()
        )
        replies = self._session.get(
            key, timeout=request.timeout, all_replies=source is None
        )
        staged: list[PendingRecord] = []
        for reply in replies:
            record = self._receive_candidate(
                reply.payload, expected_source=source, allowed_sources=allowed
            )
            if record is not None:
                staged.append(record)
        return OrgFetchResult(requested, len(replies), tuple(staged))

    def close(self) -> None:
        if self._closed:
            return
        for registration in reversed(self._registrations):
            registration.close()
        self._registrations.clear()
        with self._lock:
            self._closing = True
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            snapshot = self._runtime.snapshot(self._authority)
            if snapshot.queued == 0 and snapshot.in_flight == 0 and not self._deferred:
                break
            time.sleep(0.005)
        else:
            raise TimeoutError("org-context authority did not drain")
        if not self._effects.close(max(0.0, deadline - time.monotonic())):
            raise TimeoutError("org-context effects did not drain")
        self._generation += 1
        if not self._runtime.stop(
            self._authority, timeout=max(0.0, deadline - time.monotonic())
        ):
            raise TimeoutError("org-context authority did not stop")
        self._closed = True

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
