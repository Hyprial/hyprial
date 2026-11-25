"""Shared fakes and helpers for the slice 4b (target side) tests.

Contract pinned by these tests (docs/design/agent-teleport-protocol.md §2,
§3, §5, §6 "切片 4b"); where the plan leaves a detail open the decision is
recorded here and nowhere else.

Package ``hyprial.daemon.impl.teleport.target`` exports:

- ``TargetHost`` / ``TargetRoutines``: the ``typing.Protocol`` ports exactly as
  written in §5.  ``target/`` depends only on them, ``StagingReceiver``, the
  ``stream`` and ``protocol`` packages, and ``validate_bundle``.
- ``Landed``: frozen dataclass ``Landed(name, uri, entity_token, home, cwd,
  harness)``; ``home`` and ``cwd`` are ``Path`` and ``cwd == home /
  "workspace"`` for every real landing.
- ``TeleportTargetService(*, host, receiver, routines=None,
  reply_margin_seconds=60.0, commit_timeout_seconds=15.0, key_space=None)``
  with ``node_id`` (``host.node_id``), ``prepare``, ``chunk``, ``receive``,
  ``commit``, ``abort`` (each takes the slice 4a request dataclass, or
  ``ChunkRequest`` for ``chunk``, and returns the matching reply dataclass;
  ``chunk`` returns the persisted byte count), ``state(operation_id)``,
  ``handle_query(selector, payload) -> list[tuple[str, bytes]]`` and
  ``close()``.  Direct methods raise ``TeleportError``; ``handle_query``
  never raises for business failures.
- ``bind_target_service(session, service, *, key_space=None)``: declares one
  query handler on ``KeySpace().agent_teleport_all(service.node_id)`` serving
  all five ops and returns the registration (``close()``).

Decisions on open details:

- ``state()`` returns ``"prepared"``, ``"staging"`` (after the first accepted
  chunk), ``"received"``, ``"committed"`` or ``None`` (unknown/forgotten).
- prepare runs exactly the six §2 checks in order.  ``RUNTIME_UNSUPPORTED``
  covers a harness outside ``claude``/``codex``/``pi`` even when the host
  says it is available.  ``ROUTINE_CONFLICT`` consults
  ``routines.existing(names)`` when a routines port is present; slice 6
  refuses a non-empty routine list without one (see ``routines/support.py``).
  A dry run runs every check, and creates no staging and no state.
- receive rejects routines other than those announced by prepare (slice 6,
  ``routines/support.py``) and a harness different from prepare's with
  ``TELEPORT_BAD_FRAME``; like every receive failure this forgets the
  operation and removes its staging, before any host call.
- receive order: ``receiver.finalize`` -> ``unpack_stream`` into the
  operation's staging -> ``validate_bundle`` plus ``manifest_digest``
  comparison -> ``host.land(bundle_dir, name=<prepare agent>,
  harness=<prepare harness>)`` -> ``host.activate`` -> ``host.start(landed,
  args=..., session_ref=..., timeout=strictTimeoutSeconds)``.
- Undo on failure runs in reverse over every step that was entered after a
  successful ``land`` -- including the failing one, because activation and
  start can leave partial effects: start fails -> ``stop``, ``deactivate``,
  ``undo_landing``; activate fails -> ``deactivate``, ``undo_landing``.
  ``land`` is atomic (the registry landing undoes itself), so a failing land
  is not undone.  An undo step that raises does not stop the remaining undo
  steps and does not change the reply code.
- Error mapping: a ``TeleportError`` from a port keeps its code; any other
  exception from ``land`` or ``activate`` is ``TELEPORT_LANDING_FAILED``, from
  ``start`` ``TELEPORT_STRICT_RESUME_FAILED``.  ``start`` returning a ref
  different from a non-null ``sessionRef`` is ``STRICT_RESUME_FAILED``; for a
  null ``sessionRef`` whatever ``start`` returns is echoed in the reply.
- commit on a ``prepared``/``staging`` operation and chunk on a
  ``received``/``committed`` one are ``TELEPORT_OPERATION_CONFLICT`` and leave
  the state unchanged.  commit removes the operation's staging directory.
- A receive while another receive is running is answered at once with
  ``TELEPORT_OPERATION_CONFLICT`` and leaves the second operation untouched.
- ``handle_query`` runs receive (and commit) on a service worker thread, never
  the caller's.  It waits at most ``strictTimeoutSeconds +
  reply_margin_seconds`` for receive (``commit_timeout_seconds`` for commit);
  past that it returns ``[]`` (no reply: the source reports ``UNRESOLVED``)
  while the work finishes and its outcome is kept, so a repeated receive
  after completion returns the stored success reply.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

from hyprial.daemon.impl.transport import KeySpace

from ..stream.samples import make_bundle, stream
from ..support import chunk_requests, teleport

TARGET_PACKAGE = "hyprial.daemon.impl.teleport.target"
PROTOCOL_PACKAGE = "hyprial.daemon.impl.teleport.protocol"
NODE = "node-b"
SOURCE_NODE = "node-a"
OWNER = "owner-1"
AGENT = "worker"
SOURCE_URI = "agent://owner-1/node-a/worker"
TARGET_URI = "agent://owner-1/node-b/worker"
PUBLIC_KEY = bytes(range(32))
SESSION = "sess-1"
CHUNK = 4096


def target() -> ModuleType:
    """Import the slice 4b target package on demand."""

    return importlib.import_module(TARGET_PACKAGE)


def protocol() -> ModuleType:
    return importlib.import_module(PROTOCOL_PACKAGE)


def key(op: str, operation_id: str, node: str = NODE) -> str:
    return KeySpace().agent_teleport(node, op, operation_id)


@dataclass(frozen=True)
class Packed:
    """A real bundle packed as a tar stream, ready to be chunked."""

    bundle: Path
    path: Path
    total: int
    sha256: str
    manifest_digest: str

    @property
    def data(self) -> bytes:
        return self.path.read_bytes()


def manifest_digest(bundle: Path) -> str:
    return json.loads((bundle / "manifest.json").read_text())["manifest_digest"]


def packed_bundle(tmp_path: Path, name: str = "bundle") -> Packed:
    bundle = make_bundle(tmp_path, name)
    packed = stream().pack_bundle(bundle, tmp_path / f"{name}.tar")
    return Packed(
        bundle=bundle,
        path=packed.path,
        total=packed.total,
        sha256=packed.sha256,
        manifest_digest=manifest_digest(bundle),
    )


def raw_stream(tmp_path: Path, data: bytes, name: str = "raw") -> Packed:
    path = tmp_path / f"{name}.tar"
    path.write_bytes(data)
    return Packed(
        bundle=tmp_path,
        path=path,
        total=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        manifest_digest="ab" * 32,
    )


def prepare_request(operation_id: str = "op-1", **overrides):
    values = dict(
        operation_id=operation_id,
        source_node=SOURCE_NODE,
        target_node=NODE,
        owner=OWNER,
        agent=AGENT,
        source_agent_uri=SOURCE_URI,
        harness="claude",
        routines=(),
        dry_run=False,
    )
    values.update(overrides)
    return protocol().PrepareRequest(**values)


def receive_request(operation_id: str, packed: Packed, **overrides):
    values = dict(
        operation_id=operation_id,
        manifest_digest=packed.manifest_digest,
        stream_sha256=packed.sha256,
        harness="claude",
        session_ref=SESSION,
        routines=(),
        args=("--model", "m-1"),
        strict_timeout_seconds=5.0,
    )
    values.update(overrides)
    return protocol().ReceiveRequest(**values)


def chunks(operation_id: str, packed: Packed) -> list:
    return chunk_requests(packed.data, operation_id, chunk_bytes=CHUNK)


def feed(service, operation_id: str, packed: Packed, *, limit: int | None = None) -> int:
    """Send the stream's chunks through ``service.chunk``; return persisted."""

    persisted = 0
    for request in chunks(operation_id, packed)[:limit]:
        persisted = service.chunk(request)
    return persisted


@dataclass
class FakeHost:
    """A ``TargetHost`` that records every call and tracks what it left behind."""

    node_id: str = NODE
    owner: str = OWNER
    public_key: bytes = PUBLIC_KEY
    existing: set[str] = field(default_factory=set)
    supported: set[str] = field(default_factory=lambda: {"claude", "codex", "pi"})
    failures: dict[str, BaseException] = field(default_factory=dict)
    undo_failures: dict[str, BaseException] = field(default_factory=dict)
    start_result: object = "echo"
    start_gate: threading.Event | None = None
    start_entered: threading.Event = field(default_factory=threading.Event)
    calls: list[str] = field(default_factory=list)
    threads: dict[str, int] = field(default_factory=dict)
    land_args: list[tuple[Path, str, str, str]] = field(default_factory=list)
    start_args: list[tuple[tuple[str, ...], str | None, float]] = field(default_factory=list)
    landed: set[str] = field(default_factory=set)
    active: set[str] = field(default_factory=set)
    running: set[str] = field(default_factory=set)

    def _enter(self, name: str) -> None:
        self.calls.append(name)
        self.threads[name] = threading.get_ident()

    def agent_exists(self, name: str) -> bool:
        return name in self.existing

    def runtime_supported(self, harness: str) -> bool:
        return harness in self.supported

    def recipient_public_key(self) -> bytes:
        return self.public_key

    def land(self, bundle_dir: Path, *, name: str, harness: str):
        self._enter("land")
        manifest = json.loads((Path(bundle_dir) / "manifest.json").read_text())
        self.land_args.append((Path(bundle_dir), name, harness, manifest["actor"]))
        if "land" in self.failures:
            raise self.failures["land"]
        self.landed.add(name)
        home = Path(bundle_dir).parent / "landed-home"
        return target().Landed(
            name=name,
            uri=TARGET_URI,
            entity_token="token-1",
            home=home,
            cwd=home / "workspace",
            harness=harness,
        )

    def undo_landing(self, landed) -> None:
        self._enter("undo_landing")
        self.landed.discard(landed.name)
        if "undo_landing" in self.undo_failures:
            raise self.undo_failures["undo_landing"]

    def activate(self, landed) -> None:
        self._enter("activate")
        self.active.add(landed.name)
        if "activate" in self.failures:
            raise self.failures["activate"]

    def deactivate(self, landed) -> None:
        self._enter("deactivate")
        self.active.discard(landed.name)
        if "deactivate" in self.undo_failures:
            raise self.undo_failures["deactivate"]

    def start(
        self, landed, *, args: Sequence[str], session_ref: str | None, timeout: float
    ) -> str | None:
        self._enter("start")
        self.start_args.append((tuple(args), session_ref, timeout))
        self.running.add(landed.name)
        self.start_entered.set()
        if self.start_gate is not None:
            assert self.start_gate.wait(30.0)
        if "start" in self.failures:
            raise self.failures["start"]
        return session_ref if self.start_result == "echo" else self.start_result

    def stop(self, landed) -> None:
        self._enter("stop")
        self.running.discard(landed.name)


@dataclass
class FakeRoutines:
    """A ``TargetRoutines`` whose ``existing`` reports names in ``taken``."""

    taken: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def existing(self, names: Sequence[str]) -> list[str]:
        self.calls.append("existing")
        return [name for name in names if name in self.taken]

    def create_paused(self, *, yaml_text: str, source_uri: str, target_uri: str) -> str:
        self.calls.append("create_paused")
        raise AssertionError("slice 4b never creates routines")

    def resume(self, name: str) -> None:
        self.calls.append("resume")

    def remove(self, name: str) -> None:
        self.calls.append("remove")


def make_service(tmp_path: Path, host: FakeHost | None = None, **kwargs):
    """A service over a fresh staging root; returns ``(service, host, receiver)``."""

    host = host or FakeHost()
    receiver = teleport().StagingReceiver(teleport().staging_root(tmp_path / "target-home"))
    service = target().TeleportTargetService(host=host, receiver=receiver, **kwargs)
    return service, host, receiver


def staged(service, tmp_path: Path, operation_id: str = "op-1", **prepare_overrides) -> Packed:
    """Prepare ``operation_id`` and stream a real bundle into it."""

    packed = packed_bundle(tmp_path, f"bundle-{operation_id}")
    service.prepare(prepare_request(operation_id, **prepare_overrides))
    feed(service, operation_id, packed)
    return packed


def error_code(callable_, *args, **kwargs) -> str:
    try:
        callable_(*args, **kwargs)
    except teleport().TeleportError as error:
        return error.code
    raise AssertionError("expected a TeleportError")


def staging_entries(receiver) -> list[str]:
    root = Path(receiver.root)
    return sorted(child.name for child in root.iterdir()) if root.is_dir() else []


class RecordingSession:
    """Fake ``TransportSession`` that records ``declare_query_handler`` calls."""

    def __init__(self, on_declare=None) -> None:
        self.declared: list[tuple[str, object]] = []
        self.registrations: list[_Registration] = []
        self._on_declare = on_declare

    def declare_query_handler(self, key_expr: str, handler) -> "_Registration":
        if self._on_declare is not None:
            self._on_declare(key_expr)
        self.declared.append((key_expr, handler))
        registration = _Registration()
        self.registrations.append(registration)
        return registration

    def handler(self):
        assert len(self.declared) == 1
        return self.declared[0][1]


@dataclass
class _Registration:
    closed: bool = False

    def close(self, **_kwargs: object) -> None:
        self.closed = True
