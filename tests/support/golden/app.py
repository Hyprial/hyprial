"""A real ``DaemonApplication`` with in-process fakes for the G0 goldens.

The application, the custody inbox authority and the link service are the
production objects.  Only the outer edges are faked, each with a double the
existing suites already use: an in-memory OrgFS (``OrgFsWithMeta``), a
memory delivery transport, a recording user-delivery ledger and a Lark chat
gateway.  The link-service thread is suppressed so every pass is an explicit
``tick()``; wall clock and ``uuid4`` are pinned so the recorded responses are
deterministic (``snapshot.py`` lists the few values still normalised).
The inbox is wired like ``application/wiring/transport.py``: a
``LocalFirstDeliveryTransport`` over the memory network leg, bound back to the
custody facade's ``receive``, so same-node mail lands in the durable inbox,
with the production ``CorrelatedInboxEventRouter``, hold policy, logger and a
human-alarm leg, and the same ``InboxDeliveryIoAdapter`` PAC notifications
use.  Presence is the production ``_LocalPresence`` over ``RegisteredRoutes``
(the raw tokens a node publishes for its registered session routes), so a
registered local actor is present, as on a real node.

Harness artifacts, i.e. recorded values that differ from a production home
and that later slices must not preserve on purpose (``manifest.json`` repeats
them under ``harnessArtifacts``):

- no user profile or squire is configured, so a ``user:op`` sender records
  ``replyPathUnavailable: true`` while the ``UserDelivery`` double still
  accepts ``user:`` sends; a real home without a squire refuses those with
  ``TARGET_UNRESOLVED`` (``TARGET_SQUIRE_UNCONFIGURED`` before S10);
- ``_adapters`` is None and there is no ``LarkReplyBridgeTransport``, so
  ``channel:lark`` reply bridging is not exercised;
- agents are minted in name form (``ID_FORM_MINTING`` pinned False): the
  goldens predate release's id-form default (UUID addressing), which
  release's own suites cover;
- the workflow service (``enable_workflows``) runs without the PAC graph
  authority and with its cadence driven by explicit ``_tick`` calls, because
  the authority requires the asynchronous delivery threads.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import sqlite3
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.adapters.lark import LarkSdkGateway
from hyprial.daemon.impl.application import DaemonApplication, _LocalPresence
from hyprial.daemon.impl.configuration.identity import normalize_agent_recipient
from hyprial.daemon.impl.inbox import (
    DeliveryCustodyCoordinator,
    DeliveryCustodyFacade,
    HoldPolicy,
    LocalFirstDeliveryTransport,
    MemoryDeliveryTransport,
)
from hyprial.daemon.impl.inbox.links.io import (
    CorrelatedInboxEventRouter,
    InboxDeliveryIoAdapter,
)
from hyprial.daemon.impl.pac.actors.daemon import DaemonPacNotificationSender
from hyprial.daemon.impl.pac.workflows.runtime import GraphWorkflowService
from hyprial.daemon.impl.user_agent import CREATED, PRESENT, run_user_agent_ensure
from hyprial.daemon.impl.user_agent.local_target import bind_local_user_target
from hyprial.daemon.impl.adapters.user_delivery import UserDeliveryRequest, UserDeliveryResult
from hyprial.identity.impl.agents import addressing
from hyprial.identity import org_space_name
from hyprial.kernel import canonical_agent_uri
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.support.golden.snapshot import Golden
from tests.support.orgfs_fakes.app import OrgFsWithMeta
from tests.support.actor_hosts import new_actor_host
from tests.support.runtime.cell_delivery import AdmittingCellDelivery

OWNER = "op"
NODE = "node-1"
ORG = "group-golden"
ADAPTER = "golden-lark"
ALPHA = canonical_agent_uri(OWNER, NODE, "alpha")
BETA = canonical_agent_uri(OWNER, NODE, "beta")
GAMMA = canonical_agent_uri(OWNER, NODE, "gamma")
OWNER_USER = f"user:{OWNER}"
OWNER_UID = "ou_" + hashlib.sha256(OWNER_USER.encode("utf-8")).hexdigest()[:32]
FROZEN_MS = 1_800_000_000_000
UUID_SEED = 20261103
SESSIONS = {"alpha": "session-alpha", "beta": "session-beta", "gamma": "session-gamma"}
ROUTES = [
    {"name": "dm", "type": "direct", "nativeId": "oc_dm_chat"},
    {"name": "group", "type": "direct", "nativeId": "oc_group_chat"},
]

__all__ = [
    "ALPHA",
    "BETA",
    "GAMMA",
    "GoldenDaemon",
    "OWNER",
    "OWNER_USER",
    "SESSIONS",
    "build_golden_daemon",
    "close_golden_daemon",
    "golden_daemon",
    "pin_determinism",
    "release_determinism",
]


class RegisteredRoutes:
    """The raw liveliness directory a registered daemon would publish.

    Production declares one actor liveliness token per registered session
    route (``route_maintenance``); the real ``_LocalPresence`` then trusts
    the node token and folds in the heartbeat-freshness verdict.  This
    double answers the raw half from the application's own session table, so
    a registered local actor is present exactly as it would be on a node.
    """

    def __init__(self, app: DaemonApplication) -> None:
        self._app = app

    def online_actors(self) -> tuple[str, ...]:
        return tuple(sorted(self._app._registered_interactive_actors()))

    def actor_online(self, actor: str) -> bool:
        return actor in self._app._registered_interactive_actors()

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class HumanAlarms:
    """The human-chat alarm leg; records alarms and has no chat handle."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, alarm: object, text: str) -> bool:
        self.calls.append((str(getattr(alarm, "message_id", "")), text))
        return False


class _Harnesses:
    def status(self) -> tuple[object, ...]:
        return ()


class FrozenClock:
    """Wall clock pinned at ``FROZEN_MS``; moved only by ``advance``."""

    def __init__(self, start_ms: int = FROZEN_MS) -> None:
        self.now_ms = start_ms

    def advance(self, delta_ms: int) -> None:
        self.now_ms += int(delta_ms)

    def time(self) -> float:
        return self.now_ms / 1000.0

    def time_ns(self) -> int:
        return self.now_ms * 1_000_000

    def ms(self) -> int:
        return self.now_ms


class SeededUuid4:
    """A ``uuid4`` replacement with one seeded sequence per call site.

    The daemon draws ``uuid4`` on many threads (domain actors, delivery IO
    pools), so a single shared sequence hands values out in whatever order
    the threads interleave, and anything derived from a drawn value (a
    ``uuid5`` message id, a roster digest) changes under load.  Each call
    site, i.e. the calling module and function, gets its own sequence seeded
    from that name instead, so a site's draws do not depend on other sites'
    timing, and a draw added elsewhere by a later slice cannot shift them.
    """

    def __init__(self, seed: int = UUID_SEED) -> None:
        self._seed = seed
        self._streams: dict[str, random.Random] = {}
        self._lock = threading.Lock()
        self._drawn: list[str] = []

    def _stream(self, site: str) -> random.Random:
        stream = self._streams.get(site)
        if stream is None:
            digest = hashlib.sha256(f"{self._seed}:{site}".encode()).digest()
            stream = random.Random(int.from_bytes(digest[:8], "big"))
            self._streams[site] = stream
        return stream

    def __call__(self) -> uuid.UUID:
        caller = sys._getframe(1)
        code = caller.f_code
        site = f"{caller.f_globals.get('__name__', '?')}:{code.co_qualname}"
        with self._lock:
            value = uuid.UUID(int=self._stream(site).getrandbits(128), version=4)
            self._drawn.append(str(value))
        return value

    def drawn(self) -> list[str]:
        """Every value handed out so far, in draw order."""

        with self._lock:
            return list(self._drawn)


class ChatGateway:
    """A Lark chat gateway double that records every route send."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def send_chat(self, chat_id: str, text: str, *, idempotency_key: str) -> str:
        self.calls.append((chat_id, text, idempotency_key))
        return f"om-route-{len(self.calls)}"


class UserDelivery:
    """Per-key durable result ledger shaped like the real ``user_delivery`` port."""

    def __init__(self) -> None:
        self.requests: list[UserDeliveryRequest] = []
        self.results: dict[str, UserDeliveryResult] = {}

    def deliver(self, request: UserDeliveryRequest) -> UserDeliveryResult:
        self.requests.append(request)
        previous = self.results.get(request.idempotency_key)
        if previous is not None:
            return dataclasses.replace(previous, duplicate=True)
        result = UserDeliveryResult(
            request.message_id,
            True,
            native_message_id=f"om-user-{len(self.results) + 1}",
            code=None,
        )
        self.results[request.idempotency_key] = result
        return result


_REAL_UUID4 = uuid.uuid4
_REAL_TIME = time.time
_REAL_TIME_NS = time.time_ns


def _pinned_kind(value: object) -> str | None:
    if isinstance(value, SeededUuid4):
        return "uuid4"
    if isinstance(getattr(value, "__self__", None), FrozenClock):
        return getattr(value, "__name__", None)
    return None


def release_determinism() -> None:
    """Restore the real ``uuid4`` and clock in modules imported while pinned.

    A module first imported during a golden binds the seeded ``uuid4`` or the
    frozen clock at import, and monkeypatch only restores what it set; left
    alone, every later test in the same process would run on them.
    """

    real = {"uuid4": _REAL_UUID4, "time": _REAL_TIME, "time_ns": _REAL_TIME_NS}
    for name, module in list(sys.modules.items()):
        if not name.startswith("hyprial") or module is None:
            continue
        namespace = getattr(module, "__dict__", {})
        for attribute, value in list(namespace.items()):
            kind = _pinned_kind(value)
            if kind in real:
                setattr(module, attribute, real[kind])


def pin_determinism(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FrozenClock, SeededUuid4]:
    """Freeze the wall clock and seed ``uuid4`` everywhere it was imported.

    A pinned double still bound in a module (one imported while another
    golden was pinned) is re-pinned as well as the originals;
    ``release_determinism`` undoes those at fixture teardown.
    """

    clock = FrozenClock()
    seeded = SeededUuid4()
    original_uuid4 = uuid.uuid4
    original_time = time.time
    original_time_ns = time.time_ns
    monkeypatch.setattr(uuid, "uuid4", seeded)
    monkeypatch.setattr(time, "time", clock.time)
    monkeypatch.setattr(time, "time_ns", clock.time_ns)
    for name, module in list(sys.modules.items()):
        if not name.startswith("hyprial") or module is None:
            continue
        namespace = getattr(module, "__dict__", {})
        for attribute, value in list(namespace.items()):
            kind = _pinned_kind(value)
            if value is original_uuid4 or kind == "uuid4":
                monkeypatch.setattr(module, attribute, seeded)
            elif value is original_time or kind == "time":
                monkeypatch.setattr(module, attribute, clock.time)
            elif value is original_time_ns or kind == "time_ns":
                monkeypatch.setattr(module, attribute, clock.time_ns)
    return clock, seeded


@dataclass
class GoldenDaemon:
    """The assembled application plus the fakes a scenario can inspect."""

    app: DaemonApplication
    clock: FrozenClock
    uuids: SeededUuid4
    fs: OrgFsWithMeta
    gateway: ChatGateway
    user_delivery: UserDelivery
    transport: MemoryDeliveryTransport
    alarms: HumanAlarms
    root: Path
    workflows: GraphWorkflowService | None = None

    def enable_workflows(self) -> GraphWorkflowService:
        """Attach a real ``GraphWorkflowService`` driven by explicit ticks.

        Production builds it in ``wiring/services.py`` with the same sender,
        admission and logger.  The PAC graph authority is left out: it
        requires the asynchronous delivery threads, and a golden needs every
        dispatch to happen at a recorded step (``workflow_tick``).
        """

        if self.workflows is None:
            self.workflows = GraphWorkflowService(
                state_dir=self.app.state_dir,
                owner=OWNER,
                machine=NODE,
                sender=DaemonPacNotificationSender(self.app),
                admit=self.app._workflow_admit,
                clock_ms=self.clock.ms,
                logger=self.app._log,
                start_thread=False,
            )
            self.app._workflow_service = self.workflows
        return self.workflows

    def workflow_tick(self) -> None:
        """One synchronous workflow cadence pass (dispatch and completion)."""

        assert self.workflows is not None, "enable_workflows first"
        self.workflows._tick()

    def outbox_keys(self) -> list[dict[str, str | None]]:
        """The durable outbox rows' identity columns (read-only SQLite view).

        The wire never echoes a row's idempotency key, yet S2 classifies by
        it (``reply:workflow-<uuid>``), so the goldens observe it here.
        """

        database = self.app.state_dir / "inbox.sqlite3"
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT message_id, sender, recipient, intent, idempotency_key"
                " FROM outbox ORDER BY created_at_ms, message_id"
            ).fetchall()
        finally:
            connection.close()
        return [
            {"messageId": row[0], "sender": row[1], "recipient": row[2],
             "intent": row[3], "idempotencyKey": row[4]}
            for row in rows
        ]

    def tick(self) -> None:
        """Retained no-op hook after T1c removed the internal link service."""

        return None

    def golden(self, group: str, name: str) -> Golden:
        """A recorder for one scenario against this daemon."""

        return Golden(self, group, name, drawn=self.uuids.drawn)


def _write_home(home: Path) -> None:
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": ADAPTER,
                        "appId": "cli_golden_app",
                        "credentialRef": f"lark-{ADAPTER}",
                        "routes": ROUTES,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (home / "secrets" / f"lark-{ADAPTER}.json").write_text(
        json.dumps({"appSecret": "isolated-golden-secret"}), encoding="utf-8"
    )


def _register(app: DaemonApplication, name: str, root: Path) -> None:
    app.handle(
        "session.register",
        {
            "actor": name,
            "sessionRef": SESSIONS[name],
            "cwd": str(root / "work"),
            "command": ["golden-carrier"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
        },
    )


def build_golden_daemon(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    bypass: bool = True,
    links: bool = True,
    id_form: bool = False,
    user_agent: bool = False,
    node: str = NODE,
) -> GoldenDaemon:
    """Assemble one isolated golden daemon; the caller closes ``app``.

    ``id_form`` mints agents at release's ``a.`` id addresses instead of the
    pinned name form; no golden records under it.  ``user_agent`` creates
    the owner's user agent the way the daemon's ensure does, so
    ``user:<owner>`` is bound to local delivery (UA2); no golden records
    under it either.  ``node`` names another machine of the same owner
    (UA2b: two machines derive one conversation); its sessions are minted
    on that node, so ALPHA/BETA/GAMMA name the default node's agents.
    """

    clock, seeded = pin_determinism(monkeypatch)
    # Re-sync with release after #2547: release/v0.5.5.4 mints agents in id
    # form by default (UUID addressing, #2546).  The goldens were recorded
    # with name-form addresses (ALPHA/BETA/GAMMA) and gate the cell-model
    # refactor, not addressing, so the harness pins the name form; release's
    # own suites cover the id-form default.  Listed under harnessArtifacts.
    monkeypatch.setattr(addressing, "ID_FORM_MINTING", id_form)
    monkeypatch.setenv("HYPRIAL_SENDER_AUTH_MODE", "enforce")
    del bypass, links
    gateway = ChatGateway()
    monkeypatch.setattr(
        LarkSdkGateway, "from_credentials", lambda app_id, secret, **_: gateway
    )
    home = tmp_path / "home"
    _write_home(home)
    (tmp_path / "work").mkdir(exist_ok=True)
    state = home / "state"
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id=node,
        owner=OWNER,
        user_id=OWNER_UID,
        hyprial_home=home,
        **daemon_dependencies(),
    )
    try:
        database = state / "inbox.sqlite3"
        transport = MemoryDeliveryTransport()
        local_first = LocalFirstDeliveryTransport(
            transport, origin_node=node, local_node_id=node, local_owner=OWNER
        )
        events = CorrelatedInboxEventRouter()
        alarms = HumanAlarms()
        coordinator = DeliveryCustodyCoordinator(
            database,
            local_first,
            events,
            node_id=node,
            service_options={
                "hold_policy": HoldPolicy(),
                "logger": app._logger,
                "alarm_human_delivery": alarms,
                "interactive_recipient": lambda recipient: recipient
                in app._registered_interactive_actors(),
                "notice_recipient_resolver": lambda name: app._resolve_agent_alias(
                    normalize_agent_recipient(name)
                ),
            }, actor_host=new_actor_host(),
        )
        app._inbox = DeliveryCustodyFacade(coordinator, database)
        app._inbox_coordinator = coordinator
        local_first.bind_receiver(app._inbox.receive)
        app._local_delivery = local_first
        app._pac_notification_io = InboxDeliveryIoAdapter(
            coordinator,
            events,
            resolve_target=lambda name: app._resolve_agent_alias(
                normalize_agent_recipient(name)
            ),
            deliver_user=app._deliver_report_to_user,
        )
        app._presence = _LocalPresence(
            RegisteredRoutes(app),  # type: ignore[arg-type]
            node,
            interactive_liveness=app._interactive_actor_liveness,
        )
        app._harnesses = _Harnesses()  # type: ignore[assignment]
        app._cell_delivery = AdmittingCellDelivery()
        app._adapters = None
        user_delivery = UserDelivery()
        app._user_delivery = user_delivery
        fs = OrgFsWithMeta()
        space = org_space_name(ORG)
        fs.add_space(space, name=space, owner=OWNER_USER)
        daemon = GoldenDaemon(
            app=app,
            clock=clock,
            uuids=seeded,
            fs=fs,
            gateway=gateway,
            user_delivery=user_delivery,
            transport=transport,
            alarms=alarms,
            root=tmp_path,
        )
        for name in SESSIONS:
            app.handle("agent.create", {"name": name})
        if user_agent:
            assert run_user_agent_ensure(app) in (CREATED, PRESENT)
            assert bind_local_user_target(app)
        for name in SESSIONS:
            _register(app, name, tmp_path)
    except BaseException:
        close_golden_daemon(app)
        raise
    return daemon


def close_golden_daemon(app: DaemonApplication) -> None:
    """Close like daemon shutdown: the application, then its home lease."""

    try:
        app._close()
    finally:
        app._close_home_lease()


@pytest.fixture
def golden_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Callable[..., GoldenDaemon]]:
    """A builder for isolated golden daemons, all closed at teardown."""

    built: list[GoldenDaemon] = []

    def build(**options: Any) -> GoldenDaemon:
        root = tmp_path / f"daemon-{len(built) + 1}"
        root.mkdir()
        daemon = build_golden_daemon(root, monkeypatch, **options)
        built.append(daemon)
        return daemon

    try:
        yield build
    finally:
        try:
            for daemon in reversed(built):
                if daemon.workflows is not None:
                    daemon.app._workflow_service = None
                    daemon.workflows.close()
                close_golden_daemon(daemon.app)
        finally:
            release_determinism()
