"""WP5-C codex interactive: summary-only injection, hint settlement, KEPT, turn reports.

The carrier never answers on the model's behalf (G3c), injects only the
contract's summary block (X1), reports turns with durable retry (X4, X5, X7),
and keeps a ``keep`` hint as a generation-fenced KEPT row (X3, X8, X9).

决策记录 · 2026-10-17 · X9 的 "KEPT 只靠 wait" 改成与时间无关的断言

CI run 22298(shard2)上 ``test_kept_only_waits_and_failed_list_keeps_dedup`` 红:
``len(observation) == 6`` 超过 ``<= 5``。本地 dev 头连跑 6 次全绿、28 核 yes 压到
loadavg 37 再跑 8 次也全绿。归因(已实测证实,产品无回归):原断言数的是"首个
``message.pending.wait`` 之后 ``time.sleep(0.5)`` 窗口内的非 fetched
``message.pending.list`` 次数",即对**墙钟窗口内调用速率**的断言。产品节流是
"每次观察性 list 之前先合并一对不变的 hold"(``carrier.py`` 的
``_IDLE_HOLDS_PER_LIST = 2`` + ``_request_watch`` 的合并循环;
``inbox_watch.py`` 在 ``_list_done`` 置 ``_wait_next``、``_wait_done`` 在 held
时把 ``_list_at`` 置为当时),因此真实 daemon(hold 最长 2s)下 0.5s 窗口最多
一次 list。但本文件的 fake daemon 的 wait 只睡 50ms 却回 ``held: True``,于是
周期约 2×50ms ⇒ 0.5s 内正好 5 次,零余量;``time.sleep(0.5)`` 只保证"至少"
0.5s,调度饥饿时窗口被拉长,计数随之变成 6 起。实测:仅把窗口改成 1.2s(不改
产品)即复现同类失败 ``AssertionError: 11``。

所以退役绝对次数,改为钉住真正的不变量(两者都与时间无关):

1. 节奏不变量:相邻两次观察性 list 之间至少有一对合并的 hold
   (``_KEPT_LIST_COALESCE_HOLDS``)。这把的是"KEPT 期间不得高频轮询 list"
   ——换成 list 前的合并被绕过(如 ``_IDLE_HOLDS_PER_LIST = 1``)就会红。
2. 失败语义不变量:失败的 list 不得被当成"id 消失"(原样保留)。

观察窗口也改成"等到出现第 2 次观察性 list"而不是固定睡 0.5s,消除对调度
延迟的依赖。常数在本文件里钉字面值,不读产品常量 —— 否则把产品节流改坏时
期望值会跟着变,变异控制就失效了。

决策记录 · 2026-11-02 · X7 末尾对 superseded 丢弃日志改回等待真实条件

CI run 43919(shard4)上 ``test_codex_turn_reports_survive_restart_and_retry`` 红:
``assert any(stage == "turn-report" ...)`` 在 store 排空后立即断言,而产品
``TurnReportRelay.flush`` 先 ``delete_turn_report`` 后才记错误日志,两步之间
有窗口;load 下轮询线程正好落在窗口内,事件尚未入 logger 就断言为假。修法:
末条断言包进既有 ``wait_for``(同 WATCHDOG 超时),只等真实条件,不断言含义
不变(superseded 报告必须被丢弃且必须有一条 turn-report 错误)。实测:给
``Logger.error`` 注入 0.3s 延迟即可确定性复现原失败;修复后在注入下仍绿。

决策记录 · 2026-11-03 · R2：第二阶段 carrier 用后即停，消除 turn-9 幽灵消费

CI run 44300(shard4)同测试再红:这次是 X7 的 wait_for 本身 12s 超时——
第二阶段 ``start()`` 返回值被丢弃,该 carrier poll 线程活到 teardown,其 relay
与第三阶段共享同一 store:turn-9 第一次 flush 得 accept_drop(accepted=1 后
ConnectionError)退避,第二次 confirmed → 成功路径静默 ``delete_turn_report``,
而它 ``logger=None``(``_log`` 对 None 直接丢弃),行消失且无处留痕;third 的
pump 首次 flush 到达时行已不在,superseded 丢弃+记日志永不执行,末条
``wait_for`` 必超时。本地快机 third 先到所以绿,CI shard4 负载下 second 先到
所以红。修法:第二阶段断言后 ``second.stop()``(同 ``first.stop()`` 形),
turn-9 唯一消费者回到 third relay;accept_drop 恰好一次确认与 superseded
丢弃+留错误断言逐字保留。判定:注入"second 先消费完 turn-9 再启动 third"
修复前末条必红(同 CI 断言),修复后机制消失。"""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.api import HarnessDelivery
from hyprial.daemon.impl.harnesses import codex
from hyprial.daemon.impl.harnesses.carrier.codex_carrier_store import CodexCarrierStore
from hyprial.daemon.impl.harnesses.carrier.contract import INJECTION_FORMAT
from hyprial.kernel import DaemonRequestError, PortAdmission, ipc_errors

ACTOR = "agent:op:node-1:codex-tui"
THREAD_ID = "thread-11111111-2222-3333-4444-555555555555"
SENTINEL = "BODY-SENTINEL"
SRC = Path(codex.__file__).parent
WATCHDOG = 12.0


class Logger:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.entries.append(("info", event, fields))

    def warn(self, event: str, **fields: object) -> None:
        self.entries.append(("warn", event, fields))

    def error(self, event: str, **fields: object) -> None:
        self.entries.append(("error", event, fields))

    def events(self, name: str) -> list[dict[str, object]]:
        return [fields for _, event, fields in self.entries if event == name]


class Server:
    """Fake app-server: every prompt starts a turn; completion is switchable."""

    pid = 321

    def __init__(self, *, status: str = "completed", hold: bool = False) -> None:
        self.prompts: list[str] = []
        self.status = status
        self.hold = hold
        self.held_turns: set[str] = set()

    def start_turn(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return f"turn-{len(self.prompts)}"

    def read_turn(self, turn_id: str) -> dict[str, object]:
        if turn_id in self.held_turns or self.hold:
            return {"id": turn_id, "status": "inProgress", "items": []}
        if self.status != "completed":
            return {"id": turn_id, "status": self.status, "items": []}
        return {
            "id": turn_id,
            "status": "completed",
            "items": [
                {"type": "agentMessage", "phase": "final_answer", "text": f"final-{turn_id}"}
            ],
        }

    def interrupt_turn(self, turn_id: str) -> None:
        self.hold = False
        self.held_turns.discard(turn_id)


class GatedServer(Server):
    """A failed server whose first turn result waits for the test.

    The pump starts the native turn, then blocks in ``read_turn`` until the
    test releases it.  That lets the test remove the pending delivery while
    turn-1 is genuinely in flight, so the race is deterministic instead of
    load-dependent.
    """

    def __init__(self) -> None:
        super().__init__(status="failed")
        self.turn_started = threading.Event()
        self.release_turn = threading.Event()

    def start_turn(self, prompt: str) -> str:
        turn_id = super().start_turn(prompt)
        self.turn_started.set()
        return turn_id

    def read_turn(self, turn_id: str) -> dict[str, object]:
        self.release_turn.wait(timeout=WATCHDOG)
        return super().read_turn(turn_id)


def row(message_id: str, *, summary: str | None = "S", intent: str = "request") -> dict[str, object]:
    return {
        "messageId": message_id,
        "conversationId": f"c-{message_id}",
        "from": "requester",
        "to": ACTOR,
        "intent": intent,
        "kind": "message",
        "summary": summary,
        "bodyBytes": 13,
        "message": SENTINEL,
    }


class Daemon:
    """Scriptable daemon: pending rows, hints and turn-report behaviour."""

    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.lock = threading.RLock()
        self.rows: list[dict[str, object]] = list(rows or [])
        self.hints: dict[str, str] = {}
        self.frames: list[tuple[str, dict[str, object]]] = []
        self.observation_list_mode = "ok"  # ok | transport | ok_false | invalid
        self.fetched_list_mode = "ok"
        self.ended_mode = "ok"  # ok | fail | accept_drop | superseded
        self.started_mode = "ok"
        self.accepted: dict[tuple[str, str], int] = {}
        self.accepted_at: dict[tuple[str, str], int] = {}
        self.consumed_on_ack = True
        #: Inbox batch custody: cursor -> ordered members, plus text override.
        self.batch_text: str | None = None
        self._batch_members: dict[str, tuple[str, ...]] = {}

    def methods(self, name: str) -> list[dict[str, object]]:
        with self.lock:
            return [params for method, params in self.frames if method == name]

    def order(self) -> list[str]:
        with self.lock:
            return [method for method, _ in self.frames]

    def frames_snapshot(self) -> list[tuple[str, dict[str, object]]]:
        with self.lock:
            return [(method, dict(params)) for method, params in self.frames]

    def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        with self.lock:
            self.frames.append((method, dict(params)))
            rows = list(self.rows)
        if method in {"session.register", "session.refresh", "session.heartbeat"}:
            return {"ok": True, "daemonEpoch": "e1"}
        if method == "message.pending.list":
            fetched = params.get("fetched") is True
            mode = self.fetched_list_mode if fetched else self.observation_list_mode
            if mode == "transport":
                raise ConnectionError("daemon unreachable")
            if mode == "ok_false":
                return {"ok": False, "code": "DAEMON_BUSY", "messages": []}
            if mode == "invalid":
                return {"ok": True, "messages": "not-a-list"}
            return {"ok": True, "daemonEpoch": "e1", "messages": rows}
        if method == "message.pending.batch":
            return self._batch(params, rows)
        if method == "message.pending.wait":
            time.sleep(0.05)
            return {"ok": True, "daemonEpoch": "e1", "held": True, "changed": False}
        if method == "message.settle_hint":
            hint = self.hints.get(str(params["messageId"]), "ack")
            if hint == "raise":
                raise ConnectionError("hint transport")
            return {"ok": True, "hint": hint, "pending": True}
        if method == "message.ack":
            message_id = str(params["messageId"])
            with self.lock:
                present = any(r["messageId"] == message_id for r in self.rows)
                if present:
                    self.rows = [r for r in self.rows if r["messageId"] != message_id]
            if not present:
                return {
                    "ok": False,
                    "acknowledged": False,
                    "code": ipc_errors.MESSAGE_ACK_UNAVAILABLE,
                }
            return {"ok": True, "acknowledged": True}
        if method == "message.status":
            return {"ok": True, "state": "fetched"}
        if method == "session.turn.started":
            return self._turn_report("started", str(params["reportId"]), self.started_mode)
        if method == "session.turn.ended":
            ids = params["reportIds"]
            assert isinstance(ids, list)
            return self._turn_report("ended", str(ids[0]), self.ended_mode)
        if method == "message.reply":
            return {"ok": True, "replied": True, "acknowledged": True}
        raise AssertionError(method)

    def _batch(
        self, params: dict[str, object], rows: list[dict[str, object]]
    ) -> dict[str, object]:
        cursor_param = params.get("cursor")
        if isinstance(cursor_param, str):
            cursor = cursor_param
            admitted = set(self._batch_members.get(cursor, ()))
            selected = [row for row in rows if row["messageId"] in admitted]
        else:
            selected = list(rows)
            cursor = f"cursor-codex-{len(self._batch_members) + 1}"
            self._batch_members[cursor] = tuple(
                str(row["messageId"]) for row in selected
            )
        text = self.batch_text or (
            "[Fake daemon inbox batch]\n"
            + "\n".join(str(row["messageId"]) for row in selected)
        )
        return {
            "ok": True,
            "schemaVersion": 1,
            "format": "inbox-batch-v1",
            "cursor": cursor,
            "daemonEpoch": "e1",
            "actor": ACTOR,
            "itemCount": len(selected),
            "totalBodyBytes": 0,
            "inlineLimitBytes": 16384,
            "items": [dict(row) for row in selected],
            "delivery": {
                "mode": "inline",
                "text": text,
                "spillPath": None,
                "spillBytes": None,
                "spillSha256": None,
            },
        }

    def _turn_report(self, phase: str, report_id: str, mode: str) -> dict[str, object]:
        key = (phase, report_id)
        if mode == "fail":
            raise ConnectionError("authority timeout")
        if mode == "superseded":
            raise DaemonRequestError(ipc_errors.SESSION_SUPERSEDED, "superseded")
        with self.lock:
            first = key not in self.accepted
            self.accepted[key] = self.accepted.get(key, 0) + 1
            if first:
                self.accepted_at[key] = 1
        if mode == "accept_drop" and self.accepted[key] == 1:
            raise ConnectionError("accepted but the response was lost")
        return {"ok": True, "startedAtMs": 1, "endedAtMs": 2}

    def remove(self, message_id: str) -> None:
        with self.lock:
            self.rows = [r for r in self.rows if r["messageId"] != message_id]


def wait_for(predicate: Any, timeout: float = WATCHDOG) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


def hold_first_turn_started(
    carrier: codex.CodexInteractiveCarrier,
) -> list[tuple[object, float]]:
    """Accept the first turn-start fact without applying it to the actor.

    The real ``submit`` is called from ``release_held_turn_started`` once the
    test has forced the pump result through the removal path.  This models a
    loaded actor whose mailbox lags the pump thread.
    """

    from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime import (
        CarrierTurnStarted,
    )

    held: list[tuple[object, float]] = []
    real_submit = carrier._carrier_runtime.submit

    def submit(command: object, *, timeout: float = 0.25) -> object:
        if isinstance(command, CarrierTurnStarted) and not held:
            held.append((command, timeout))
            return PortAdmission.ACCEPTED
        return real_submit(command, timeout=timeout)

    carrier._carrier_runtime.submit = submit  # type: ignore[method-assign]
    return held


def release_held_turn_started(
    carrier: codex.CodexInteractiveCarrier, held: list[tuple[object, float]]
) -> None:
    if not held:
        return
    command, timeout = held.pop()
    carrier._carrier_runtime.submit(command, timeout=timeout)


def wait_for_turn_result(logger: Logger, timeout: float = WATCHDOG) -> bool:
    return wait_for(
        lambda: any(
            event == "worker.carrier.error" and fields.get("stage") == "turn-result"
            for _, event, fields in logger.entries
        ),
        timeout=timeout,
    )


@pytest.fixture
def start() -> Any:
    carriers: list[codex.CodexInteractiveCarrier] = []

    def make(
        server: Server,
        daemon: Daemon,
        path: Path,
        logger: Logger | None = None,
        *,
        session_ref: str = THREAD_ID,
    ) -> codex.CodexInteractiveCarrier:
        carrier = codex.CodexInteractiveCarrier(
            server,
            actor=ACTOR,
            session_ref=session_ref,
            cwd=Path("/tmp/codex-contract"),
            command=["codex", "--remote"],
            daemon_request=daemon.request,
            logger=logger,
            state_path=path,
            poll_seconds=0.01,
            settlement_retry_seconds=0.01,
        )
        carriers.append(carrier)
        carrier.start()
        return carrier

    yield make
    for carrier in carriers:
        carrier.stop()


def deliveries(path: Path) -> dict[str, dict[str, Any]]:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        cursor = db.execute("SELECT * FROM codex_interactive_delivery")
        return {r["message_id"]: dict(r) for r in cursor}


def turn_reports(path: Path) -> list[dict[str, Any]]:
    with closing(sqlite3.connect(path)) as db:
        db.row_factory = sqlite3.Row
        try:
            cursor = db.execute("SELECT * FROM codex_turn_report ORDER BY created_ms, phase")
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in cursor]


def seed(path: Path, message_id: str, *states: str, intent: str = "request") -> None:
    store = CodexCarrierStore(path)
    store.record_fetched(
        ACTOR,
        THREAD_ID,
        HarnessDelivery(
            delivery_id=message_id,
            conversation_id=f"c-{message_id}",
            sender="requester",
            recipient=ACTOR,
            message=f"seeded {message_id}",
        ),
        intent,
    )
    store.record_turn_started(ACTOR, message_id, f"turn-{message_id}")
    store.record_final(ACTOR, message_id, f"final-{message_id}")
    store.record_kept(ACTOR, message_id)
    store.close()


# --------------------------------------------------------------------- X1


def test_codex_prompt_is_the_daemon_batch_text(tmp_path: Path, start: Any) -> None:
    path = tmp_path / "c.sqlite3"
    daemon = Daemon([row("m1")])
    expected = "[Harness Network inbox batch cursor=c1]\nBYTE-EXACT-codex"
    daemon.batch_text = expected
    server = Server(hold=True)
    start(server, daemon, path)
    assert wait_for(lambda: server.prompts)
    # The daemon renderer is byte-authoritative; the carrier adds nothing and
    # the stored body sentinel never reaches a model.
    assert server.prompts == [expected]
    assert SENTINEL not in server.prompts[0]
    assert wait_for(lambda: deliveries(path).get("m1", {}).get("state") == "TURN_STARTED")
    stored = deliveries(path)["m1"]
    assert stored["message"] == expected
    assert SENTINEL not in stored["message"]
    assert stored["injection_format"] == "inbox-batch-v1"


# --------------------------------------------------------------------- X2


def test_codex_final_settles_by_hint_and_never_replies(tmp_path: Path, start: Any) -> None:
    path = tmp_path / "c.sqlite3"
    daemon = Daemon([row("m1"), row("m2", intent="reply")])
    daemon.hints = {"m1": "ack", "m2": "ack"}
    start(Server(), daemon, path)
    assert wait_for(lambda: not daemon.rows and not deliveries(path))
    assert daemon.methods("message.reply") == []
    order = daemon.order()
    for message_id in ("m1", "m2"):
        hint_at = next(
            i
            for i, (m, p) in enumerate(daemon.frames)
            if m == "message.settle_hint" and p["messageId"] == message_id
            and "session.turn.ended" in order[:i]
        )
        assert "session.turn.ended" in order[:hint_at]
        ack_at = next(
            i
            for i, (m, p) in enumerate(daemon.frames)
            if m == "message.ack" and p["messageId"] == message_id
        )
        assert hint_at < ack_at


# --------------------------------------------------------------------- X3


def test_codex_keep_is_not_fetched_again(tmp_path: Path, start: Any) -> None:
    path = tmp_path / "c.sqlite3"
    daemon = Daemon([row("m1")])
    daemon.hints = {"m1": "keep"}
    server = Server()
    start(server, daemon, path)
    assert wait_for(lambda: deliveries(path).get("m1", {}).get("state") == "KEPT")
    kept = deliveries(path)["m1"]
    assert kept["final_output"] is None
    time.sleep(0.4)
    assert [
        p for p in daemon.methods("message.pending.list") if p.get("fetched") is True
    ] == []
    batches = daemon.methods("message.pending.batch")
    assert len([p for p in batches if "cursor" not in p]) == 1
    assert [p["cursor"] for p in batches if "cursor" in p] == ["cursor-codex-1"] * 2
    assert len(server.prompts) == 1
    assert daemon.methods("message.ack") == []
    assert daemon.methods("message.reply") == []
    assert deliveries(path)["m1"]["state"] == "KEPT"
    daemon.remove("m1")  # the model replied or acked on its own
    assert wait_for(lambda: "m1" not in deliveries(path))


# --------------------------------------------------------------------- X4


def test_codex_turn_start_is_reported_with_delivery_id(tmp_path: Path, start: Any) -> None:
    path = tmp_path / "c.sqlite3"
    daemon = Daemon([row("m1")])
    server = Server(hold=True)
    start(server, daemon, path)
    assert wait_for(lambda: daemon.methods("session.turn.started"))
    params = daemon.methods("session.turn.started")[0]
    assert params["reportId"] == "turn-1"
    assert params["deliveryId"] == "m1"
    assert params["actor"] == ACTOR and params["sessionRef"] == THREAD_ID
    assert wait_for(lambda: not turn_reports(path))


# --------------------------------------------------------------------- X5


def test_codex_failed_turn_reports_failed(tmp_path: Path, start: Any) -> None:
    path = tmp_path / "c.sqlite3"
    logger = Logger()
    server = Server(status="failed")
    daemon = Daemon([row("m1")])
    carrier = start(server, daemon, path, logger)
    held = hold_first_turn_started(carrier)
    try:
        # The pump result has reached the removal path while the actor still
        # has not applied turn-1's start fact.
        assert wait_for_turn_result(logger)
    finally:
        release_held_turn_started(carrier, held)
    assert wait_for(lambda: daemon.methods("session.turn.ended"), timeout=2.0)
    ended = daemon.methods("session.turn.ended")[0]
    assert ended["reportIds"] == ["turn-1"]
    assert ended["status"] == "failed"
    time.sleep(0.2)
    assert daemon.methods("message.ack") == []
    assert daemon.methods("message.reply") == []


def test_a_delayed_turn_start_cannot_reorder_the_failed_ended_reports(
    tmp_path: Path, start: Any
) -> None:
    path = tmp_path / "c.sqlite3"
    logger = Logger()
    server = Server(status="failed")
    daemon = Daemon([row("m1")])
    carrier = start(server, daemon, path, logger)
    held = hold_first_turn_started(carrier)
    try:
        assert wait_for_turn_result(logger)
    finally:
        release_held_turn_started(carrier, held)
    assert wait_for(lambda: len(server.prompts) >= 2, timeout=2.0)
    assert wait_for(
        lambda: len(daemon.methods("session.turn.ended")) >= 2, timeout=2.0
    )
    ended = [p["reportIds"][0] for p in daemon.methods("session.turn.ended")]
    assert ended[:2] == ["turn-1", "turn-2"], ended


def test_a_turn_that_loses_its_delivery_still_reports_ended(
    tmp_path: Path, start: Any
) -> None:
    path = tmp_path / "c.sqlite3"
    logger = Logger()
    server = GatedServer()
    daemon = Daemon([row("m1")])
    carrier = start(server, daemon, path, logger)
    held = hold_first_turn_started(carrier)
    try:
        assert server.turn_started.wait(timeout=WATCHDOG)
        # The delivery disappears while turn-1 is in flight, so no later
        # attempt can adopt the delayed turn-start fact.
        daemon.remove("m1")
        server.release_turn.set()
        assert wait_for_turn_result(logger)
    finally:
        release_held_turn_started(carrier, held)
    assert wait_for(lambda: daemon.methods("session.turn.ended"), timeout=2.0)
    ended = daemon.methods("session.turn.ended")[0]
    assert ended["reportIds"] == ["turn-1"]
    assert ended["status"] == "failed"


# One turn can be ended by two different removal paths:
#   * settlement reports ``ended`` before it submits ``CarrierRemoved``
#     (``_reconcile_inflight_turns``' failed/interrupted branch);
#   * the pump-result removal ``else`` branch reports the durable turn mapping
#     (``persisted_turn_id``) before it drops the delivery.
# Both go through the same ``_report_turn_ended``, whose per-carrier
# ``_carrier_reported_ended`` set is what makes them idempotent: the journal is
# keyed on (actor, reportId, phase), so a second write would reach the daemon as
# a second ``session.turn.ended`` for a turn that ended once.
#
# The two sites are driven directly below because the field each reads -- the
# snapshot's turn id (settlement) and the durable mapping of a delivery whose
# snapshot lags (removal ``else``) -- cannot both hold for one live delivery.
# The shared seam is the invariant boundary, so both orders are pinned there.
_SETTLEMENT_REMOVAL = "settlement then CarrierRemoved"
_PUMP_RESULT_REMOVAL = "pump-result removal else"


@pytest.mark.parametrize(
    "order",
    [
        (_SETTLEMENT_REMOVAL, _PUMP_RESULT_REMOVAL),
        (_PUMP_RESULT_REMOVAL, _SETTLEMENT_REMOVAL),
    ],
)
def test_a_turn_reports_ended_once_whichever_removal_path_runs_first(
    tmp_path: Path, start: Any, order: tuple[str, str]
) -> None:
    path = tmp_path / "c.sqlite3"
    daemon = Daemon()
    # ``fail`` keeps the ended row pending: an accepted report is deleted from
    # codex_turn_report, so the durable row can only be read while unconfirmed.
    daemon.ended_mode = "fail"
    carrier = start(Server(status="failed"), daemon, path)

    writes: list[tuple[str, str, str]] = []
    record = carrier._turn_reports.record

    def observe(
        report_id: str, phase: str, status: str | None, delivery_id: str
    ) -> None:
        writes.append((report_id, phase, str(status)))
        record(report_id, phase, status, delivery_id)

    carrier._turn_reports.record = observe  # type: ignore[method-assign]

    for site in order:
        assert site in {_SETTLEMENT_REMOVAL, _PUMP_RESULT_REMOVAL}
        # Both sites report the same failed turn-1 with its delivery id; only
        # the order differs, which is exactly what the race can change.
        carrier._report_turn_ended("turn-1", "failed", "m1")

    ended = [
        row
        for row in turn_reports(path)
        if row["report_id"] == "turn-1" and row["phase"] == "ended"
    ]
    assert len(ended) == 1, ended
    # The durable write happens once: breaking the dedupe doubles THIS list
    # while the journal's (actor, reportId, phase) key would still hide it.
    assert len(writes) == 1, writes
    assert ended[0]["status"] == writes[0][2] == "failed"


# --------------------------------------------------------------------- X6

_LEGACY_SCHEMA = """
CREATE TABLE codex_interactive_delivery (
    actor TEXT NOT NULL, message_id TEXT NOT NULL, session_ref TEXT NOT NULL,
    conversation_id TEXT NOT NULL, sender TEXT NOT NULL,
    message TEXT NOT NULL DEFAULT '', intent TEXT NOT NULL, state TEXT NOT NULL,
    turn_id TEXT, final_output TEXT, settlement_attempts INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (actor, message_id))
"""


def _legacy_store(path: Path) -> None:
    with closing(sqlite3.connect(path)) as db:
        db.execute(_LEGACY_SCHEMA)
        for message_id, state, turn_id, final in (
            ("m-fetched", "FETCHED", None, None),
            ("m-turn", "TURN_STARTED", "turn-old", None),
            ("m-final", "FINAL_OBSERVED", "turn-fin", "old final text"),
        ):
            db.execute(
                "INSERT INTO codex_interactive_delivery VALUES (?,?,?,?,?,?,?,?,?,?,0)",
                (ACTOR, message_id, THREAD_ID, f"c-{message_id}", "requester",
                 SENTINEL, "request", state, turn_id, final),
            )
        db.commit()


def test_legacy_store_rows_never_inject_the_body(tmp_path: Path, start: Any) -> None:
    path = tmp_path / "c.sqlite3"
    _legacy_store(path)
    rows = [row("m-fetched"), row("m-turn"), row("m-final")]
    server = Server()
    server.held_turns = {"turn-old"}

    # Phase 1: the daemon cannot give metadata -> FETCHED must not start a turn.
    down = Daemon(rows)
    down.observation_list_mode = "transport"
    down.fetched_list_mode = "transport"
    first = start(server, down, path)
    time.sleep(0.5)
    first.stop()
    assert server.prompts == []
    assert deliveries(path)["m-fetched"]["state"] == "FETCHED"
    assert down.methods("message.reply") == []

    # Phase 2: metadata available -> the body is never injected.
    up = Daemon(rows)
    start(server, up, path)
    assert wait_for(lambda: server.prompts)
    assert all(SENTINEL not in prompt for prompt in server.prompts)
    assert server.prompts[0].startswith("[Fake daemon inbox batch]")
    assert "m-fetched" in server.prompts[0]
    assert wait_for(lambda: any(
        p["messageId"] == "m-final" for p in up.methods("message.settle_hint")
    ))
    assert up.methods("message.reply") == []
    assert "old final text" not in repr(up.frames)
    migrated = deliveries(path).get("m-fetched")
    assert migrated is None or (
        migrated["injection_format"] in {INJECTION_FORMAT, "inbox-batch-v1"}
        and SENTINEL not in migrated["message"]
    )


# --------------------------------------------------------------------- X7


def test_codex_turn_reports_survive_restart_and_retry(tmp_path: Path, start: Any) -> None:
    path = tmp_path / "c.sqlite3"
    first_daemon = Daemon([row("m1")])
    first_daemon.ended_mode = "fail"
    first = start(Server(), first_daemon, path)
    assert wait_for(lambda: not first_daemon.rows)
    assert wait_for(lambda: any(r["phase"] == "ended" for r in turn_reports(path)))
    first.stop()
    pending = [r for r in turn_reports(path) if r["phase"] == "ended"]
    assert [r["report_id"] for r in pending] == ["turn-1"]
    assert pending[0]["status"] == "completed" and pending[0]["delivery_id"] == "m1"

    # Restart: accepted-but-response-lost, then confirmed. One new acceptance.
    second_daemon = Daemon()
    second_daemon.ended_mode = "accept_drop"
    second = start(Server(), second_daemon, path)
    assert wait_for(lambda: not [r for r in turn_reports(path) if r["phase"] == "ended"])
    sent = second_daemon.methods("session.turn.ended")
    assert len(sent) >= 2
    assert {p["reportIds"][0] for p in sent} == {"turn-1"}  # type: ignore[index]
    assert second_daemon.accepted[("ended", "turn-1")] >= 1
    assert second_daemon.accepted_at[("ended", "turn-1")] == 1
    second.stop()

    # A superseded report is dropped and logged, not retried forever.
    store = CodexCarrierStore(path)
    store.add_turn_report(ACTOR, THREAD_ID, "turn-9", "ended", "completed", "m9", 5)
    store.close()
    logger = Logger()
    third_daemon = Daemon()
    third_daemon.ended_mode = "superseded"
    start(Server(), third_daemon, path, logger)
    assert wait_for(lambda: not turn_reports(path))
    assert wait_for(
        lambda: any(
            f.get("stage") == "turn-report"
            for f in logger.events("worker.carrier.error")
        )
    )


# --------------------------------------------------------------------- X8


def _kept_world(tmp_path: Path, start: Any) -> tuple[Path, Daemon]:
    path = tmp_path / "c.sqlite3"
    daemon = Daemon([row("m1")])
    daemon.hints = {"m1": "keep"}
    start(Server(), daemon, path)
    assert wait_for(lambda: deliveries(path).get("m1", {}).get("state") == "KEPT")
    return path, daemon


def _assert_still_pending(daemon: Daemon, server: Server, path: Path) -> None:
    time.sleep(0.4)
    assert [r["messageId"] for r in daemon.rows] == ["m1"]
    assert daemon.methods("message.reply") == []
    assert daemon.methods("message.ack") == []
    assert server.prompts == []
    assert deliveries(path)["m1"]["state"] == "KEPT"


def test_kept_survives_three_breakpoints(tmp_path: Path, start: Any, monkeypatch: Any) -> None:
    # Breakpoint 1: the actor dies after the KEPT row was written and before
    # its projection was released.
    path = tmp_path / "c.sqlite3"
    daemon = Daemon([row("m1")])
    daemon.hints = {"m1": "keep"}
    real = CodexCarrierStore.record_kept
    crashed: list[bool] = []

    def crash_after_write(self: CodexCarrierStore, actor: str, message_id: str) -> None:
        real(self, actor, message_id)
        if not crashed:
            crashed.append(True)
            raise RuntimeError("actor crash after the KEPT write")

    monkeypatch.setattr(CodexCarrierStore, "record_kept", crash_after_write)
    server = Server()
    carrier = start(server, daemon, path)
    assert wait_for(lambda: crashed)
    assert wait_for(lambda: deliveries(path).get("m1", {}).get("state") == "KEPT")
    time.sleep(0.4)
    assert len(server.prompts) == 1
    assert daemon.methods("message.reply") == []
    carrier.stop()
    monkeypatch.setattr(CodexCarrierStore, "record_kept", real)

    # Breakpoint 2: carrier process restart.
    server2 = Server()
    carrier2 = start(server2, daemon, path)
    _assert_still_pending(daemon, server2, path)
    carrier2.stop()

    # Breakpoint 3: same actor, a new sessionRef.
    server3 = Server()
    start(server3, daemon, path, session_ref="thread-99999999-aaaa-bbbb-cccc-dddddddddddd")
    _assert_still_pending(daemon, server3, path)

    # An explicit reply/ack by the model clears the KEPT row.
    daemon.remove("m1")
    assert wait_for(lambda: "m1" not in deliveries(path))


def test_carrier_kept_command_is_generation_fenced(tmp_path: Path) -> None:
    from hyprial.daemon.impl.harnesses.carrier.interactive_carrier_runtime import (
        CarrierFetched,
        CarrierFinalObserved,
        CarrierKept,
        CarrierTurnStarted,
        InteractiveCarrierRuntime,
    )

    path = tmp_path / "c.sqlite3"
    store = CodexCarrierStore(path)
    runtime = InteractiveCarrierRuntime(
        name="t", actor=ACTOR, session_ref=THREAD_ID, store=store
    )
    delivery = HarnessDelivery(
        delivery_id="m1", conversation_id="c", sender="s", recipient=ACTOR, message="x"
    )
    store.record_fetched(ACTOR, THREAD_ID, delivery, "request")
    runtime.submit(CarrierFetched(delivery, "request"))
    assert wait_for(lambda: runtime.snapshot("m1") is not None)
    generation = runtime.generation()
    attempt = runtime.snapshot("m1").attempt
    runtime.submit(CarrierTurnStarted(generation, "m1", "t1", attempt))
    runtime.submit(CarrierFinalObserved(generation, "m1", "out"))
    assert wait_for(lambda: getattr(runtime.snapshot("m1"), "stage", "") == "FINAL_OBSERVED")
    runtime.submit(CarrierKept(generation + 7, "m1"))  # stale generation: dropped
    time.sleep(0.2)
    assert store.kept_ids(ACTOR) == frozenset()
    assert runtime.snapshot("m1") is not None
    runtime.submit(CarrierKept(generation, "m1"))
    assert wait_for(lambda: runtime.snapshot("m1") is None)
    assert store.kept_ids(ACTOR) == frozenset({"m1"})
    assert deliveries(path)["m1"]["final_output"] is None
    assert runtime.drain(2.0)
    store.close()


# --------------------------------------------------------------------- X9

# The coalesced holds the product performs before re-listing an unchanged
# inbox (``carrier.py``: ``_IDLE_HOLDS_PER_LIST``).  Pinned as a literal on
# purpose: reading the product constant here would let a throttling regression
# move the expectation along with it and the mutation control below would pass.
_KEPT_LIST_COALESCE_HOLDS = 2


def _observational_lists(
    frames: list[tuple[str, dict[str, object]]],
) -> list[dict[str, object]]:
    return [
        params
        for method, params in frames
        if method == "message.pending.list" and not params.get("fetched")
    ]


def _waits_between_observational_lists(
    frames: list[tuple[str, dict[str, object]]],
) -> list[int]:
    gaps: list[int] = []
    waits = 0
    listed = 0
    for method, _params in frames:
        if method == "message.pending.wait":
            waits += 1
        elif method == "message.pending.list" and not _params.get("fetched"):
            if listed:
                gaps.append(waits)
            listed += 1
            waits = 0
    return gaps


def _assert_kept_polls_by_coalesced_holds(
    frames: list[tuple[str, dict[str, object]]],
) -> None:
    """A KEPT-only inbox may not be re-listed before a coalesced hold pair.

    This is the time-independent form of "KEPT is served by wait, not by
    list polling": however long the observation window runs (a starved
    scheduler stretches any ``sleep``), every observational list after the
    first must sit behind at least the holds the product coalesces.
    """

    gaps = _waits_between_observational_lists(frames)
    assert len(_observational_lists(frames)) >= 2, (
        "the window observed fewer than two observational lists, so the "
        "cadence invariant would be vacuous"
    )
    assert gaps and min(gaps) >= _KEPT_LIST_COALESCE_HOLDS, (
        "two observational lists were separated by fewer coalesced holds "
        f"than the product promises ({_KEPT_LIST_COALESCE_HOLDS}); gaps={gaps}"
        " -- KEPT deliveries must be discovered by wait, not by list polling"
    )


def test_kept_only_waits_and_failed_list_keeps_dedup(tmp_path: Path, start: Any) -> None:
    path = tmp_path / "c.sqlite3"
    seed(path, "m1")
    daemon = Daemon([row("m1")])
    server = Server()
    start(server, daemon, path)
    # Time-independent window: wait for cadence evidence, never for a fixed
    # wall-clock slice (``time.sleep`` is a lower bound, not a deadline).
    assert wait_for(lambda: len(_observational_lists(daemon.frames_snapshot())) >= 2)
    frames = daemon.frames_snapshot()
    _assert_kept_polls_by_coalesced_holds(frames)
    assert [
        params
        for method, params in frames
        if method == "message.pending.list" and params.get("fetched")
    ] == []
    assert server.prompts == []

    # Failed lists never count as "the id is gone".
    daemon.remove("m1")
    for mode in ("transport", "ok_false", "invalid"):
        daemon.observation_list_mode = mode
        before = len(daemon.methods("message.pending.list"))
        assert wait_for(lambda: len(daemon.methods("message.pending.list")) >= before + 2)
        assert deliveries(path)["m1"]["state"] == "KEPT", mode
    # A wait increment alone is not evidence either.
    assert deliveries(path)["m1"]["state"] == "KEPT"
    daemon.observation_list_mode = "ok"
    assert wait_for(lambda: "m1" not in deliveries(path))


def test_a_throttle_regression_rereads_the_inbox_without_waiting(
    tmp_path: Path, start: Any, monkeypatch: Any
) -> None:
    """破坏正控:把产品节流改坏,新的节奏断言必须红。

    ``_IDLE_HOLDS_PER_LIST = 1`` drops the coalesced hold pair, so the carrier
    re-lists after a single hold -- exactly the list polling the invariant
    forbids.  A guard that has only ever run on the correct shape is an untested
    discriminator.
    """

    from hyprial.daemon.impl.harnesses.codex import carrier as codex_carrier

    monkeypatch.setattr(codex_carrier, "_IDLE_HOLDS_PER_LIST", 1)
    path = tmp_path / "c.sqlite3"
    seed(path, "m1")
    daemon = Daemon([row("m1")])
    start(Server(), daemon, path)
    assert wait_for(lambda: len(_observational_lists(daemon.frames_snapshot())) >= 2)
    with pytest.raises(AssertionError, match="coalesced holds"):
        _assert_kept_polls_by_coalesced_holds(daemon.frames_snapshot())


# --------------------------------------------------------------------- G3c


def test_codex_never_replies_or_reads_body() -> None:
    forbidden = (
        '"message.reply"',
        "_settle_reply_unavailable",
        "reply_lost",
        'item.get("message")',
    )
    for name in ("settlement.py", "carrier.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        for needle in forbidden:
            assert needle not in text, (name, needle)
