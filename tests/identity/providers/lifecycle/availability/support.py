"""Contract tests for the provider-availability policy (2026-09-21).

Two claims, and this file carries one positive control for each plus the
mutation contract in the docstring (the mutation itself is applied by hand and
its red reading is recorded in the delivery report):

* **negative** -- no site may *check availability and then adjust*: selection
  is static configuration (PC1), and a diagnostic mark never skips a
  provider (PC3);
* **positive** -- an unavailable or unproductive attempt must *fail loud* to
  the sender that is waiting for the receipt, carrying evidence of that
  attempt (PC2).

The runtime-level tests drive the real :class:`DaemonEventBridge` with fake
process boundaries (a scripted harness farm and an in-memory inbox), so the
notice under assertion is the one the production failure path builds -- not a
notice a test helper wrote.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path


from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl import DaemonEventBridge
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.daemon.impl.api import HarnessResult, HarnessResultStatus
from hyprial.daemon.impl.dispatch import matrix
from hyprial.daemon.impl.inbox.contracts.api import (
    DeliveryLifecycle,
    HarnessFailureSettlement,
    InboxMessage,
    SubmissionResult,
)
from hyprial.daemon.impl.inbox.tracking.progress import ProgressEvent
from hyprial.identity import UserProfileStore
from hyprial.kernel import canonical_agent_uri

from tests.support.runtime._desired_state import write_v1

REQUIRES_REQUESTER = "op/node-reg/requester"
REQUIRES_WORKER = "op/node-reg/squire"


def _uri(name: str) -> str:
    return canonical_agent_uri("op", "node-reg", name)


REQUESTER = _uri("requester")
WORKER = _uri("squire")
OWNER = "owner-someone-else"


# --------------------------------------------------------------------------- #
# PC1 · availability_selection_has_no_runtime_precheck
# --------------------------------------------------------------------------- #

_REPO_ROOT = Path(__file__).resolve().parents[5]
_SOURCE_ROOT = _REPO_ROOT / "src" / "hyprial"

#: Entry points that must resolve a tier.  Keyed by ``<relative path>:<symbol>``
#: so a *new* caller shows up as a test failure.  Deliberately no line numbers:
#: lines move, call sites are the boundary that matters.
_EXPECTED_SELECTION_CALL_SITES = {
    "daemon/impl/application/actors/routines.py:resolve",
    "daemon/impl/application/workflows.py:resolve",
    "daemon/impl/pac/actors/coordinator/types.py:resolve_minimum_tier",
    "daemon/impl/pac/contracts/workflow.py:resolve",
    # The explicit diagnostic command is the one production caller allowed to
    # touch the probe boundary; it is not a selection reader.
    "shell/impl/cli/commands/workflow/internal/dispatch.py:diagnose",
    "shell/impl/cli/commands/workflow/internal/dispatch.py:resolve",
}

_MATRIX_MODULE = "hyprial.daemon.impl.dispatch.matrix"
_DISPATCH_PACKAGE = "hyprial.daemon.impl.dispatch"
_FACADE_MODULE = "hyprial.daemon"
_SELECTION_SYMBOLS = frozenset(
    {"resolve", "resolve_minimum_tier", "minimum_pool", "diagnose"}
)


def _selection_call_sites() -> set[str]:
    """Find every call to a matrix selection symbol, alias-aware.

    Includes ``from hyprial.daemon.impl.dispatch.matrix import resolve as r``,
    ``import hyprial.daemon.impl.dispatch.matrix as m; m.resolve(...)``, and the
    facade-mediated ``from hyprial.daemon import resolve``.  Anything this
    returns must be covered by the table above, so adding a production reader
    of the pool cannot slip past the contract.
    """

    # ``hyprial.daemon`` re-exports the matrix symbols through its lazy
    # binding table; compare identity against the real matrix owner so a
    # facade-mediated reader is still counted as a call site.
    import hyprial.daemon as daemon_facade

    facade_symbols = {
        name
        for name in _SELECTION_SYMBOLS
        if getattr(daemon_facade, name, None) is getattr(matrix, name, None)
    }
    sites: set[str] = set()
    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        symbols: dict[str, str] = {}
        modules: set[str] = set()
        facade_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == _MATRIX_MODULE:
                for alias in node.names:
                    symbols[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.ImportFrom) and node.module == _DISPATCH_PACKAGE:
                for alias in node.names:
                    if alias.name == "matrix":
                        modules.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module == _FACADE_MODULE:
                for alias in node.names:
                    if alias.name in facade_symbols:
                        symbols[alias.asname or alias.name] = alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == _MATRIX_MODULE:
                        modules.add(alias.asname or alias.name)
                    elif alias.name == _FACADE_MODULE:
                        facade_modules.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name: str | None = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in modules:
                    name = func.attr
                elif func.value.id in facade_modules and func.attr in facade_symbols:
                    name = func.attr
            elif isinstance(func, ast.Name) and func.id in symbols:
                name = symbols[func.id]
            if name in _SELECTION_SYMBOLS:
                sites.add(f"{path.relative_to(_SOURCE_ROOT).as_posix()}:{name}")
    return sites


def _dead_run():
    from hyprial.daemon.impl.squire.probe import ProbeRun

    return ProbeRun(1, "quota exhausted")


# --------------------------------------------------------------------------- #
# PC3 · diagnostic_mark_does_not_skip_provider
# --------------------------------------------------------------------------- #


def _profile_store() -> UserProfileStore:
    """A real store at the path the production ``active_profile()`` reads.

    Writing anywhere else would let a mutation that reintroduces a profile
    read pass this file unnoticed: the mutation would find no profile and
    select the head anyway.
    """

    store = UserProfileStore(Path(os.environ["HARNESS_STATE_DIR"]) / "users.json")
    store.ensure(
        owner=os.environ["HYPRIAL_OWNER"],
        owner_key="owner-key",
        login_name="unit",
        machine="test-node",
        machine_key="machine-key",
    )
    return store


# --------------------------------------------------------------------------- #
# PC2 · unavailable_attempt_reaches_sender_with_its_own_evidence
# --------------------------------------------------------------------------- #


class LoudInbox:
    """In-memory InboxPort stand-in that keeps receipts, like the real one.

    ``submit`` dedupes by ``message_id`` exactly as the durable inbox does for
    a deterministic id, so a replayed notice is provably the same notice.
    """

    def __init__(self) -> None:
        self.rows: dict[str, list[InboxMessage]] = {}
        self.sent: list[InboxMessage] = []
        self.submissions: dict[str, int] = {}
        self.settlements: dict[str, HarnessFailureSettlement] = {}
        self.holds: list[str] = []
        self.consumed: set[str] = set()
        self.fetched: set[str] = set()
        self.refuse_submit = False

    # -- fixtures ---------------------------------------------------------
    def add(self, message: InboxMessage) -> None:
        self.rows.setdefault(message.recipient, []).append(message)

    # -- InboxPort-ish ----------------------------------------------------
    def _visible(self, recipient: str) -> tuple[InboxMessage, ...]:
        return tuple(
            message
            for message in self.rows.get(recipient, ())
            if message.message_id not in self.consumed
            and not (
                (settlement := self.settlements.get(message.message_id))
                and settlement.terminal
            )
        )

    def pending_messages(self, recipient: str) -> tuple[InboxMessage, ...]:
        return self._visible(recipient)

    def fetch_pending(
        self,
        recipient: str,
        *,
        only: frozenset[str] | None = None,
        completed_turn: bool = False,
    ) -> tuple[InboxMessage, ...]:
        # Hard cut (send links): the carrier stamps ``fetched`` on the rows it
        # hands to the model; this double only records the stamp.
        del completed_turn
        visible = tuple(
            message for message in self._visible(recipient)
            if only is None or message.message_id in only
        )
        self.fetched.update(message.message_id for message in visible)
        return visible

    def has_fetched(self, message_id: str) -> bool:
        return message_id in self.fetched

    def dispatchable_messages(self, recipient: str, *, now_ms: int | None = None):
        return self._visible(recipient)

    def refresh_hold(self, message_id: str, *, now_ms: int | None = None) -> bool:
        self.holds.append(message_id)
        return True

    def ack(self, recipient: str, message_id: str):
        from hyprial.daemon.impl.inbox.contracts.api import AckResult

        self.consumed.add(message_id)
        return AckResult(message_id=message_id, acknowledged=True)

    def is_acknowledged(self, message_id: str) -> bool:
        return message_id in self.consumed

    def submit(self, message: InboxMessage, *, now_ms: int | None = None) -> SubmissionResult:
        self.submissions[message.message_id] = self.submissions.get(message.message_id, 0) + 1
        if self.refuse_submit:
            return SubmissionResult(message.message_id, False, code="STUB_REFUSED")
        if message.message_id not in {m.message_id for m in self.sent}:
            self.sent.append(message)
            self.add(message)
        return SubmissionResult(message.message_id, True)

    def submit_progress_event(self, event, *, recipient: str) -> bool:
        # The real port persists progress for a recipient; the runtime only
        # needs it to accept the event so the drain/refresh path runs.
        return True

    def settle_harness_failure(
        self,
        recipient: str,
        message_id: str,
        failure_code: str,
        *,
        permanent: bool,
        max_attempts: int,
        backoff_ms: tuple[int, ...],
        now_ms: int | None = None,
    ) -> HarnessFailureSettlement:
        attempts = len(
            [s for s in self.settlements.values() if s.message_id == message_id]
        ) + 1
        terminal = permanent or attempts >= max_attempts
        settlement = HarnessFailureSettlement(
            message_id=message_id,
            recipient=recipient,
            cycle=1,
            failure_code=failure_code,
            attempts=attempts,
            max_attempts=max_attempts,
            next_attempt_ms=None if terminal else (now_ms or 0) + 1000,
            terminal=terminal,
            permanent=permanent,
            terminal_reason="permanent" if permanent else None,
            first_failed_at_ms=now_ms or 0,
            updated_at_ms=now_ms or 0,
            terminal_at_ms=(now_ms or 0) if terminal else None,
        )
        self.settlements[message_id] = settlement
        return settlement

    def harness_failure_settlement(self, message_id: str):
        return self.settlements.get(message_id)

    def harness_failure_original(self, message_id: str):
        for rows in self.rows.values():
            for message in rows:
                if message.message_id == message_id:
                    return message
        return None

    def system_notices(self, recipient: str):
        return ()

    def prune_inbox(self):
        return ()

    def retry_due(self):
        return []

    def close(self) -> None:
        return None


class FakeHarnessFarm:
    """Minimal ManagedHarnessRuntime stand-in with scriptable outcomes."""

    def __init__(self, actors: tuple[str, ...] = ("squire",)) -> None:
        self.actors = actors
        self.results: list[HarnessResult] = []
        self.progress: list[ProgressEvent] = []
        self.enqueued: set[str] = set()
        self.deliveries: list[tuple[str, str]] = []
        self.interrupts: list[str] = []
        #: Every steer request the daemon's timeout path makes, in order.
        self.steers: list[tuple[str, str]] = []
        #: What ``steer`` answers.  Default False keeps every pre-steer test
        #: on the notice path unchanged; steer tests flip it explicitly.
        self.steer_returns = False

    def collect_orphans(self) -> int:
        return 0

    def reconcile(self) -> int:
        return 0

    def reconcile_session_refs(self):
        return {}

    def streaming_actors(self) -> tuple[str, ...]:
        return self.actors

    def dispatch(self, name: str, delivery) -> bool:  # type: ignore[no-untyped-def]
        if delivery.delivery_id in self.enqueued:
            return False
        self.enqueued.add(delivery.delivery_id)
        self.deliveries.append((name, delivery.delivery_id))
        return True

    def drain_progress(self):
        drained, self.progress = tuple(self.progress), []
        return drained

    def drain_results(self):
        drained, self.results = tuple(self.results), []
        return drained

    def drain_failed_events(self):
        return ()

    def drain_readiness_reports(self):
        return ()

    def status(self):
        return ()

    def interrupt(self, delivery_id: str, *, timeout: float = 1.0) -> bool:
        self.interrupts.append(delivery_id)
        return True

    def steer(self, name: str, delivery_id: str, timeout: float = 5.0) -> bool:
        self.steers.append((name, delivery_id))
        return self.steer_returns

    def stop(self, timeout: float = 5.0) -> None:
        return None


def _request(message_id: str, *, created_at_ms: int = 1_000) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"conv-{message_id}",
        sender=REQUESTER,
        recipient=WORKER,
        payload=b'{"message":"please do the thing"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )


def _reply(message_id: str, *, created_at_ms: int = 1_000) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"conv-{message_id}",
        sender=REQUESTER,
        recipient=WORKER,
        payload=b'{"message":"the completed answer"}',
        intent="reply",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=created_at_ms,
    )


def _runtime(
    tmp_path: Path,
    inbox: LoudInbox,
    farm: FakeHarnessFarm,
    now: dict,
    *,
    workflow_outcome=None,
):
    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-reg",
        desired_state=DesiredStateStore(desired_path),
        transport=_NullTransport(),
        inbox=inbox,
        harnesses=farm,
        harness_actor_uri=_uri,
        clock_ms=lambda: now["v"],
        workflow_outcome=workflow_outcome,
    )
    runtime.start()
    return runtime


class _NullTransport:
    def declare_liveliness(self, key: str):
        return _NullRegistration()

    def close(self) -> None:
        return None


class _NullRegistration:
    healthy = True

    def close(self, *, reason: str = "unspecified", initiator: str = "external") -> None:
        return None


def _notices_to(inbox: LoudInbox, recipient: str) -> list[dict]:
    """Fail-loud notices only -- never the ordinary business reply."""

    notices = []
    for message in inbox.sent:
        if message.recipient != recipient:
            continue
        body = json.loads(message.payload)
        if "notification" in body:
            notices.append(body)
    return notices


# --------------------------------------------------------------------------- #
# A fail-loud notice never reaches a human-facing requester.
#
# The notice text is internal (worker URI, harness, silence budget, attempt
# id).  When the waiting "sender" is a chat adapter, a route or a user, that
# sender is a person -- observed: alarm 412b27d3 addressed to
# adapter:lark:hyprial-introducer-feishu mid-way through a customer's Feishu
# DM (it only failed to arrive by accident, RECIPIENT_HOLDER_UNRESOLVED).
# Such notices are diverted to the daemon log instead; agent requesters are
# unchanged (every pc2 test above is the control).
# --------------------------------------------------------------------------- #

HUMAN_FACING = (
    "adapter:lark:hyprial-introducer-feishu",
    "route:hyprial-introducer-feishu:customer-dm",
    "user:bob.smith@example.com",
)


def _human_request(message_id: str, sender: str) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"lark:oc_customer-{message_id}",
        sender=sender,
        recipient=WORKER,
        payload=b'{"message":"customer question"}',
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1_000,
    )


def _logged_runtime(tmp_path: Path, inbox: LoudInbox, farm: FakeHarnessFarm, now: dict):
    events: list[tuple[str, dict]] = []
    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-reg",
        desired_state=DesiredStateStore(desired_path),
        transport=_NullTransport(),
        inbox=inbox,
        harnesses=farm,
        harness_actor_uri=_uri,
        clock_ms=lambda: now["v"],
        logger=lambda level, component, event, **fields: events.append((event, fields)),
    )
    runtime.start()
    return runtime, events


# -- Diverted notices go to the worker's owner (Allen 2026-09-26) -------------
#
# #829 stopped fail-loud notices reaching a person's chat and only logged
# them.  Allen: 「提醒改发负责人」 -- the diverted notice now also reaches the
# owner through the existing owner-DM channel (autoupdate.alert.notify_owner),
# off the runtime loop, never raising.  Agent requesters are unchanged.


def _owner_runtime(
    tmp_path: Path,
    inbox: LoudInbox,
    farm: FakeHarnessFarm,
    now: dict,
    notifier,
    *,
    owner_requester_addresses: tuple[str, ...] = (),
):
    events: list[tuple[str, dict]] = []
    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-reg",
        desired_state=DesiredStateStore(desired_path),
        transport=_NullTransport(),
        inbox=inbox,
        harnesses=farm,
        harness_actor_uri=_uri,
        clock_ms=lambda: now["v"],
        logger=lambda level, component, event, **fields: events.append((event, fields)),
        owner_notifier=notifier,
        owner_requester_addresses=owner_requester_addresses,
    )
    runtime.start()
    return runtime, events


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# -- In-channel notices for human conversations (Allen 2026-10-13) ------------
#
# The 2026-09-26 divert-to-owner rule is retired for human-originated
# conversations: the waiting person must hear about their own stalled/failed
# request IN the chat where they asked (34 diverted notices/24h reached the
# owner 2026-10-12; none reached the requester).  The daemon hands the notice
# to an adapter-side in-channel deliverer -- the adapter owns the chat handle
# -- rate-limited to the first occurrence per (conversation x notice kind)
# plus the terminal outcome.


def _in_channel_runtime(
    tmp_path: Path,
    inbox: LoudInbox,
    farm: FakeHarnessFarm,
    now: dict,
    deliverer,
    *,
    notifier=None,
    owner_requester_addresses: tuple[str, ...] = (),
):
    events: list[tuple[str, dict]] = []
    desired_path = tmp_path / "desired-state.json"
    write_v1(desired_path)
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-reg",
        desired_state=DesiredStateStore(desired_path),
        transport=_NullTransport(),
        inbox=inbox,
        harnesses=farm,
        harness_actor_uri=_uri,
        clock_ms=lambda: now["v"],
        logger=lambda level, component, event, **fields: events.append((event, fields)),
        owner_notifier=notifier,
        owner_requester_addresses=owner_requester_addresses,
        in_channel_notice_delivery=deliverer,
    )
    runtime.start()
    return runtime, events


def _fail_once(runtime, farm, now, message_id: str) -> None:
    runtime.on_timer()
    now["v"] += 1_500
    farm.results.append(
        HarnessResult(
            delivery_id=message_id,
            recipient=WORKER,
            status=HarnessResultStatus.FAILED,
            error="quota exhausted",
            failure_code="PROVIDER_USAGE_LIMIT",
        )
    )
    runtime.on_timer()


# -- A diverted notice never loops back to the person who asked --------------
#
# #829/#844 keep machine text out of a person's chat by handing the diverted
# notice to the owner.  When the waiting sender IS the owner -- their own squire
# adapter, one of its routes, or their ``user:`` address -- "the owner" is that
# same person, so the text still arrives in front of them, one wrapper deeper.
# Observed 2026-09-26: a no-progress notice for the owner's own request 9c82055d
# reached his DM after that request had been answered; the guard was defeated by
# its own fallback.
#
# The owner-facing text is also stripped of the fields only an operator reads:
# the worker URI, the silence budget and the attempt id.  The failure code and
# the requester stay, so the owner still learns who was waiting and what
# happened (pinned by test_a_diverted_notice_reaches_the_owner_once).

OWNER_USER = "user:owner@example.com"


# -- A user-proxy is a person's chat; a notice never begets a notice ----------
#
# 2026-09-26 04:55-05:08Z: 252 fail-loud notices went to allen-proxy (a
# user-proxy relaying everything into Allen's DM) in a self-feeding chain.
# The person-chat guard matched the ADDRESS scheme only, and a user-proxy is an
# ordinary agent: actor; each undeliverable notice then produced a notice
# about itself.  Both halves are pinned here.

PROXY = _uri("proxy")


def _proxy_runtime(tmp_path: Path, inbox: LoudInbox, farm: FakeHarnessFarm, now: dict, notifier=None):
    from hyprial.daemon.impl.desired_state import DesiredState

    events: list[tuple[str, dict]] = []
    desired_path = tmp_path / "desired-state.json"
    DesiredStateStore(desired_path).save(
        DesiredState(
            harnesses=(
                HarnessLaunchSpec(harness="pi", name="squire", headless=True, cwd="/workspace"),
                HarnessLaunchSpec(
                    harness="user-proxy", name="proxy", headless=True,
                    args=("--route", "route:person-app:person-dm"),
                ),
            )
        )
    )
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-reg",
        desired_state=DesiredStateStore(desired_path),
        transport=_NullTransport(),
        inbox=inbox,
        harnesses=farm,
        harness_actor_uri=_uri,
        clock_ms=lambda: now["v"],
        logger=lambda level, component, event, **fields: events.append((event, fields)),
        owner_notifier=notifier,
    )
    runtime.start()
    return runtime, events


def _proxy_request(message_id: str, *, payload: bytes = b'{"message":"relayed"}') -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        conversation_id=f"conv-{message_id}",
        sender=PROXY,
        recipient=WORKER,
        payload=payload,
        intent="request",
        lifecycle=DeliveryLifecycle.DURABLE_SERVICE,
        created_at_ms=1_000,
    )


def _proxy_in_channel_runtime(
    tmp_path: Path, inbox: LoudInbox, farm: FakeHarnessFarm, now: dict, notifier=None
):
    """The proxy fixture of ``_proxy_runtime`` with the owner notifier wired
    (the in-channel contract asserts the owner is NOT the fallback anymore)."""

    from hyprial.daemon.impl.desired_state import DesiredState

    events: list[tuple[str, dict]] = []
    desired_path = tmp_path / "desired-state.json"
    DesiredStateStore(desired_path).save(
        DesiredState(
            harnesses=(
                HarnessLaunchSpec(harness="pi", name="squire", headless=True, cwd="/workspace"),
                HarnessLaunchSpec(
                    harness="user-proxy", name="proxy", headless=True,
                    args=("--route", "route:person-app:person-dm"),
                ),
            )
        )
    )
    runtime = DaemonEventBridge(
        state_dir=tmp_path,
        node_id="node-reg",
        desired_state=DesiredStateStore(desired_path),
        transport=_NullTransport(),
        inbox=inbox,
        harnesses=farm,
        harness_actor_uri=_uri,
        clock_ms=lambda: now["v"],
        logger=lambda level, component, event, **fields: events.append((event, fields)),
        owner_notifier=notifier,
    )
    runtime.start()
    return runtime, events
