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

from hyprial.transport import KeySpace, Registration, TransportSample, TransportSession

from .document import OrgDocumentError, parse_document, serialize_document
from .store import OrgContextStore, OrgVersionError, PendingRecord


ORG_WIRE_VERSION = 1
ORG_WIRE_TYPE = "org-context"
_ENVELOPE_KEYS = frozenset({"schemaVersion", "type", "sourceNode", "document"})
_MAX_ENVELOPE_BYTES = 2 * 1024 * 1024
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
        self._registrations: list[Registration] = []
        subscription = session.subscribe(
            self._keys.org_context_any(), self._on_announcement
        )
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

        payload = self._wire_payload()
        if payload is None:
            return False
        self._session.put(self._keys.org_context(self.node_id), payload)
        return True

    def _answer_request(self, _selector: str) -> bytes | None:
        return self._wire_payload()

    def _on_announcement(self, sample: TransportSample) -> None:
        source_from_key: str | None = None
        try:
            source_from_key = self._keys.decode_identity(sample.key.rsplit("/", 1)[-1])
        except (UnicodeDecodeError, ValueError) as error:
            self._reject(
                "invalid-envelope", f"announcement key source is invalid: {error}"
            )
            return
        self.receive(sample.payload, expected_source=source_from_key)

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

        with self._lock:
            accepted = self._store.load_accepted()
            if accepted is not None and document.meta.version <= accepted.meta.version:
                error = OrgVersionError(
                    f"incoming version {document.meta.version} must be newer than "
                    f"accepted version {accepted.meta.version}"
                )
                self._reject("non-monotonic", str(error), source_node=source_node)
                return None
            record = self._store.stage(document, source=source_node)
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

        allowed = None if allowed_sources is None else frozenset(allowed_sources)
        if source is not None and allowed is not None and source not in allowed:
            raise ValueError(f"org-context source {source!r} is not reachable")
        requested = (source,) if source is not None else tuple(sorted(allowed or ()))
        if source is None and allowed is not None and not allowed:
            return OrgFetchResult(requested, 0, ())
        key = (
            self._keys.org_request(source)
            if source is not None
            else self._keys.org_request_any()
        )
        replies = self._session.get(key, timeout=timeout, all_replies=source is None)
        staged: list[PendingRecord] = []
        for reply in replies:
            record = self.receive(
                reply.payload,
                expected_source=source,
                allowed_sources=allowed,
            )
            if record is None:
                continue
            staged.append(record)
        return OrgFetchResult(requested, len(replies), tuple(staged))

    def close(self) -> None:
        for registration in reversed(self._registrations):
            registration.close()
