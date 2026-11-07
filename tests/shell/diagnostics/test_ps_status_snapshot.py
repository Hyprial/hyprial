"""ps/agent.list 共享一次 worker 状态快照 — ps desired-state load 风暴修复。

设计:daemon 就绪三阶段实施 plan 的 U2。改造前 ps 里每个 agent 的活性
verdict 各自支付一次 ``_harnesses.status()`` actor 往返,而
``_canonical_harness_uri`` 的 fallback 再对每个 connector 各做一次
``desired_state.load()`` —— 58 agents × 20 connectors 一次 ps 就是约
1200 次全量 load。改造后一次 ps / agent.list / agent.get 请求共享一份
请求级快照:常数次的 load 与一次 status() 往返,且同一响应里每一行读到
的是同一份状态(行内自洽,这是一次有意的语义选择)。

doubles 与 test_agent_entity.py 同型:这里钉的是 DaemonApplication 的
ps/agent 状态面与调用次数上界,不是 supervisor 语义。
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import threading
from pathlib import Path
from typing import Any

import pytest

from hyprial.identity.impl.agents import AGENT_HEARTBEAT_TTL_SECONDS
from hyprial.kernel.impl.contracts.ports import PortAdmission
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.desired_state import DesiredStateStore
from hyprial.kernel import HarnessLaunchSpec, LaneProjection
from hyprial.daemon.impl.lifecycle_manager import (
    LifecycleKind,
    LifecycleOperation,
    LifecycleResult,
    LifecycleState,
)
from hyprial.daemon.impl.inbox import (
    AckResult,
    InboxMessage,
    SubmissionResult,
)
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

OWNER = "op"
NODE = "node-1"


# --------------------------------------------------------------------------
# doubles
# --------------------------------------------------------------------------


class FakeInbox:
    def __init__(self, pending: tuple[InboxMessage, ...] = ()) -> None:
        self.pending = list(pending)

    def pending_all(self) -> tuple[InboxMessage, ...]:
        return tuple(self.pending)

    def submit(self, message: InboxMessage) -> SubmissionResult:
        return SubmissionResult(message.message_id, accepted=True)

    def ack(self, actor: str, message_id: str) -> AckResult:
        return AckResult(message_id, acknowledged=False)

    def outbox_count(self) -> int:
        return 0

    def custody_count(self) -> int:
        return 0


class CountingSupervisor:
    """ManagedHarnessRuntime 的最小替代,额外记录 status() 调用次数。"""

    def __init__(self) -> None:
        self.running: dict[tuple[str, str], bool] = {}
        self.session_refs: dict[tuple[str, str], str] = {}
        self.status_calls = 0

    def start(self, spec: Any) -> bool:
        self.running[(spec.harness, spec.name)] = True
        return True

    def remove(self, harness: str, name: str) -> bool:
        return self.running.pop((harness, name), None) is not None

    def status(self) -> tuple[dict[str, object], ...]:
        self.status_calls += 1
        return tuple(
            {
                "id": f"{harness}:{name}",
                "runtime": harness,
                "name": name,
                "running": running,
            }
            for (harness, name), running in sorted(self.running.items())
        )

    def projected_worker_session_refs(self) -> dict[tuple[str, str], str]:
        return dict(self.session_refs)


class StubPresence:
    def __init__(self, online: tuple[str, ...] = ()) -> None:
        self._online = online

    def actor_online(self, actor: str) -> bool:
        return actor in self._online

    def online_actors(self) -> tuple[str, ...]:
        return self._online

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _InlineLifecycleManager:
    """Synchronous outer-shell double; 与 test_agent_entity.py 同型。"""

    def __init__(self, app: DaemonApplication) -> None:
        self._app = app
        self._results: dict[str, LifecycleResult] = {}
        # Same surface as the real manager's ps-health projection.
        self.crashed = False
        self.is_running = True
        self.last_error: str | None = None

    def drain(self, _timeout: float) -> bool:
        return True

    def submit(self, operation: LifecycleOperation) -> PortAdmission:
        projection = operation.target.harness
        spec = HarnessLaunchSpec.from_json(projection.to_payload(), "provider")
        if operation.kind is LifecycleKind.CREATE:
            self._app._bind_agent(
                operation.target.actor,
                harness=spec.harness,
                runtime="headless",
                session_id=spec.session_ref,
                cwd=spec.cwd,
                args=spec.args,
            )
            self._app.desired_state.upsert_harness(spec)
            assert self._app._harnesses is not None
            self._app._harnesses.start(spec)
            effects = ("agent.create", "agent.bind", "harness.ensure", "route.ensure")
        elif operation.kind is LifecycleKind.DEACTIVATE:
            assert self._app._harnesses is not None
            self._app._harnesses.remove(spec.harness, spec.name)
            self._app.desired_state.remove_harness(spec.harness, spec.name)
            self._app._release_agent_binding(operation.target.actor)
            effects = ("harness.remove", "agent.release")
        else:
            raise AssertionError(f"unexpected unit lifecycle kind: {operation.kind}")
        self._results[operation.operation_id] = LifecycleResult(
            operation.operation_id,
            LifecycleState.COMPLETED,
            effects,
            (),
        )
        return PortAdmission.ACCEPTED

    def wait(self, operation_id: str, _timeout: float) -> LifecycleResult:
        return self._results[operation_id]


_TEST_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_test_applications():  # type: ignore[no-untyped-def]
    yield
    while _TEST_APPLICATIONS:
        _TEST_APPLICATIONS.pop().agents.close()


def application(tmp_path: Path) -> DaemonApplication:
    app = DaemonApplication(
        state_dir=tmp_path,
        socket_path=tmp_path / "daemon.sock",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    app._inbox = FakeInbox()  # type: ignore[assignment]
    app._harnesses = CountingSupervisor()  # type: ignore[assignment]
    app._presence = StubPresence()  # type: ignore[assignment]
    app._lifecycle_manager = _InlineLifecycleManager(app)  # type: ignore[assignment]
    _TEST_APPLICATIONS.append(app)
    return app


class LoadCounter:
    """实例级包装 DesiredStateStore.load — SessionActor 与 application
    持有同一个 store 对象,实例包装对两条读路径都计数。"""

    def __init__(
        self, monkeypatch: pytest.MonkeyPatch, store: DesiredStateStore,
        session: Any | None = None,
    ) -> None:
        self.count = 0
        self.desired_count = 0
        self.session_count = 0
        real_load = store.load

        def counting_load() -> Any:
            self.count += 1
            self.desired_count += 1
            return real_load()

        monkeypatch.setattr(store, "load", counting_load)
        if session is not None:
            real_sessions = session.read_sessions

            def counting_sessions() -> Any:
                self.count += 1
                self.session_count += 1
                return real_sessions()

            monkeypatch.setattr(session, "read_sessions", counting_sessions)


def supervisor(app: DaemonApplication) -> CountingSupervisor:
    harnesses = app._harnesses
    assert isinstance(harnesses, CountingSupervisor)
    return harnesses


def test_ps_exposes_hook_lane_overflow_counters(tmp_path: Path) -> None:
    app = application(tmp_path)
    lane = LaneProjection(
        name="consumer:test",
        events=("turn.completed",),
        accepted=3,
        overloaded=2,
        closed=0,
        delivered=1,
        failed=0,
        shut=False,
    )
    app._daemon_hooks = type(
        "HookStatus", (), {"projection": lambda self: (lane,)}
    )()

    assert app._ipc_ps({})["daemon"]["hooks"] == [
        {
            "name": "consumer:test",
            "events": ["turn.completed"],
            "accepted": 3,
            "overloaded": 2,
            "closed": 0,
            "delivered": 1,
            "failed": 0,
            "shut": False,
        }
    ]


def fleet(app: DaemonApplication, *, agents: int, connectors: int) -> None:
    """N 个注册 agent × M 个 desired connector 的风暴形态。

    生产基线是 58×20(每个 agent 的 verdict 都要扫一遍 connector 清单,
    交叉乘积来自扫描,不要求名字对应)。
    """

    for index in range(agents):
        app.handle("agent.create", {"name": f"agent-{index}"})
    harnesses = supervisor(app)
    for index in range(connectors):
        spec = HarnessLaunchSpec(harness="pi", name=f"worker-{index}", headless=True)
        app.desired_state.upsert_harness(spec)
        harnesses.running[("pi", f"worker-{index}")] = True


def start_headless(app: DaemonApplication, harness: str, name: str) -> dict[str, Any]:
    return app.handle(
        "lifecycle.start",
        {"provider": harness, "name": name, "headless": True, "cwd": "/work"},
    )


def register_interactive(app: DaemonApplication, actor: str) -> dict[str, Any]:
    return app.handle(
        "session.register",
        {
            "actor": actor,
            "sessionRef": f"session-{actor.rsplit(':', 1)[-1]}",
            "cwd": "/work",
            "command": ["claude"],
            "source": "claude-channel",
            "runtime": "claude_interactive",
            "channelConfirmed": True,
        },
    )


# --------------------------------------------------------------------------
# 等价性:把现状行为冻进快照测试再改
# --------------------------------------------------------------------------


def test_ps_agent_rows_frozen_across_the_snapshot_refactor(tmp_path: Path) -> None:
    """改造前后 ps 的 agents[] 必须逐字段相等(createdAtMs 是墙钟,除外)。

    覆盖五类 verdict 分支:worker 信号 True/False、无任何信号、交互会话
    心跳新鲜/陈旧;另放一个 pinned 容器化 connector 在快照构建里行使
    D-D 映射(它在本地没有 agent 行)。
    """

    app = application(tmp_path)
    clock = {"now": 1000.0}
    app._clock = lambda: clock["now"]  # type: ignore[assignment]
    register_interactive(app, f"agent:{OWNER}:{NODE}:web")
    clock["now"] += AGENT_HEARTBEAT_TTL_SECONDS + 1.0
    register_interactive(app, f"agent:{OWNER}:{NODE}:tui")
    start_headless(app, "pi", "foo")
    start_headless(app, "codex", "bar")
    supervisor(app).running[("codex", "bar")] = False
    app.agents.create("qux")
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(
            harness="codex",
            name="dock",
            headless=True,
            containerized=True,
            pinned_owner="source",
        )
    )
    supervisor(app).running[("codex", "dock")] = True

    rows = app.handle("ps", {})["agents"]
    for row in rows:
        # C adds a diagnostic object; all pre-existing fields remain frozen.
        assert row.pop("runState")["state"] == "unknown"
        row.pop("createdAtMs")
        if row["capabilities"]:
            assert isinstance(row["capabilities"].pop("updated_at_ms"), int)

    def capabilities(harness: str, interactive: bool) -> dict[str, object]:
        # CAPMX intentionally adds runtime facts; all other snapshot fields
        # remain frozen. Only the observed timestamp is nondeterministic.
        return {"creationOrigin": "managed-launch", "harness": harness,
                "interactive": interactive, "provider": None,
                "model": None, "tier": None, "proactive_send": True, "tool_injection": True}

    assert rows == [
        {
            # worker 信号 False ⇒ offline
            "uri": f"agent:{OWNER}:{NODE}:bar",
            "actor": "bar",
            "owner": OWNER,
            "machine": NODE,
            "cwd": "/work",
            "config": None,
            "provider": None,
            "model": None,
            "capabilities": capabilities("codex", False),
            "harnessArgs": {},
            "preferredHarness": "codex",
            "lastHarness": "codex",
            "lastSessionId": None,
            "pinnedAdapters": [],
            "hosted": False,
            "hostedBy": None,
            "status": "offline",
            "harness": "codex",
            "runtime": "headless",
            "sessionId": None,
            "heartbeatAgeSeconds": 0.0,
        },
        {
            # worker 信号 True ⇒ online
            "uri": f"agent:{OWNER}:{NODE}:foo",
            "actor": "foo",
            "owner": OWNER,
            "machine": NODE,
            "cwd": "/work",
            "config": None,
            "provider": None,
            "model": None,
            "capabilities": capabilities("pi", False),
            "harnessArgs": {},
            "preferredHarness": "pi",
            "lastHarness": "pi",
            "lastSessionId": None,
            "pinnedAdapters": [],
            "hosted": False,
            "hostedBy": None,
            "status": "online",
            "harness": "pi",
            "runtime": "headless",
            "sessionId": None,
            "heartbeatAgeSeconds": 0.0,
        },
        {
            # 没有任何信号 ⇒ offline
            "uri": f"agent:{OWNER}:{NODE}:qux",
            "actor": "qux",
            "owner": OWNER,
            "machine": NODE,
            "cwd": None,
            "config": None,
            "provider": None,
            "model": None,
            "capabilities": {},
            "harnessArgs": {},
            "preferredHarness": None,
            "lastHarness": None,
            "lastSessionId": None,
            "pinnedAdapters": [],
            "hosted": False,
            "hostedBy": None,
            "status": "offline",
            "harness": None,
            "runtime": None,
            "sessionId": None,
            "heartbeatAgeSeconds": None,
        },
        {
            # 交互会话心跳新鲜 ⇒ online
            "uri": f"agent:{OWNER}:{NODE}:tui",
            "actor": "tui",
            "owner": OWNER,
            "machine": NODE,
            "cwd": "/work",
            "config": None,
            "provider": None,
            "model": None,
            "capabilities": capabilities("claude", True),
            "harnessArgs": {},
            "preferredHarness": "claude",
            "lastHarness": "claude",
            "lastSessionId": "session-tui",
            "pinnedAdapters": [],
            "hosted": False,
            "hostedBy": None,
            "status": "online",
            "harness": "claude",
            "runtime": "interactive",
            "sessionId": "session-tui",
            "heartbeatAgeSeconds": 0.0,
        },
        {
            # 交互会话心跳陈旧 ⇒ offline
            "uri": f"agent:{OWNER}:{NODE}:web",
            "actor": "web",
            "owner": OWNER,
            "machine": NODE,
            "cwd": "/work",
            "config": None,
            "provider": None,
            "model": None,
            "capabilities": capabilities("claude", True),
            "harnessArgs": {},
            "preferredHarness": "claude",
            "lastHarness": "claude",
            "lastSessionId": "session-web",
            "pinnedAdapters": [],
            "hosted": False,
            "hostedBy": None,
            "status": "offline",
            "harness": "claude",
            "runtime": "interactive",
            "sessionId": "session-web",
            "heartbeatAgeSeconds": AGENT_HEARTBEAT_TTL_SECONDS + 1.0,
        },
    ]

    # agent.list 与 ps 的 agents[] 是同一个循环:逐字段一致。
    listed = app.handle("agent.list", {})["agents"]
    for row in listed:
        assert row.pop("runState")["state"] == "unknown"
        row.pop("createdAtMs")
        assert "lastActiveAtMs" in row
        assert set(row.pop("activityHints")) == {
            "createdAtMs",
            "hasLastSessionId",
            "agentHomeMtimeMs",
        }
        row.pop("lastActiveAtMs")
        if row["capabilities"]:
            assert isinstance(row["capabilities"].pop("updated_at_ms"), int)
    assert listed == rows


# --------------------------------------------------------------------------
# 计数:常数界,不是渐近界
# --------------------------------------------------------------------------


def test_ps_loads_desired_state_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一次 ps 全程的 desired-state load 是常数 2:请求级 worker 快照一次,
    会话投影(read_sessions,同一个 store)一次。改造前是
    O(agents × connectors) 次。
    """

    app = application(tmp_path)
    fleet(app, agents=5, connectors=4)
    loads = LoadCounter(
        monkeypatch, app.desired_state, app._agent_session_domains.session
    )

    app.handle("ps", {})

    assert loads.count == 2
    assert loads.desired_count == 1
    assert loads.session_count == 1


@pytest.mark.parametrize("agents", [1, 58])
def test_ps_status_call_count_constant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, agents: int
) -> None:
    """N=1 与 N=58 必须断言同一组常数 — 渐近界会在回归里悄悄退化。"""

    app = application(tmp_path)
    fleet(app, agents=agents, connectors=20)
    loads = LoadCounter(
        monkeypatch, app.desired_state, app._agent_session_domains.session
    )
    supervisor(app).status_calls = 0

    app.handle("ps", {})

    assert supervisor(app).status_calls == 1
    assert loads.count == 2
    assert loads.desired_count == 1
    assert loads.session_count == 1


def test_agent_list_and_get_share_the_request_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """agent.list/agent.get 与 ps 的 agents[] 走同一个循环,必须一起改,
    否则风暴只是挪了个位置。"""

    app = application(tmp_path)
    fleet(app, agents=4, connectors=3)
    loads = LoadCounter(monkeypatch, app.desired_state)

    supervisor(app).status_calls = 0
    loads.count = 0
    app.handle("agent.list", {})
    assert supervisor(app).status_calls == 1
    assert loads.count == 1

    supervisor(app).status_calls = 0
    loads.count = 0
    app.handle("agent.get", {"name": "agent-0"})
    assert supervisor(app).status_calls == 1
    assert loads.count == 1


# --------------------------------------------------------------------------
# D-D:pinned owner 经 spec 传递,不再走 fallback load
# --------------------------------------------------------------------------


def test_canonical_harness_uri_uses_the_passed_spec_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """容器化 spec 的 pinned owner 保留 source 身份(决策 D-D);批量路径
    传入 spec 后不再发生 fallback load。spec=None 的单点路径改为单行
    harness_spec 读取,同样不做整文档 load,两者结论必须一致。"""

    app = application(tmp_path)
    spec = HarnessLaunchSpec(
        harness="codex",
        name="dock",
        headless=True,
        containerized=True,
        pinned_owner="source",
    )
    app.desired_state.upsert_harness(spec)
    loads = LoadCounter(monkeypatch, app.desired_state)

    assert app._canonical_harness_uri("dock", spec) == f"agent:source:{NODE}:dock"
    assert loads.count == 0

    assert app._canonical_harness_uri("dock") == f"agent:source:{NODE}:dock"
    assert loads.count == 0


def test_worker_status_snapshot_maps_pinned_owner_uri_and_session_ref(
    tmp_path: Path,
) -> None:
    """请求级快照下,pinned 容器化 connector 的 running/sessionRef 都按
    D-D 的 source 身份键入;快照之外的单点路径仍是 live 查询,结论一致。"""

    app = application(tmp_path)
    spec = HarnessLaunchSpec(
        harness="codex",
        name="dock",
        headless=True,
        containerized=True,
        pinned_owner="source",
    )
    app.desired_state.upsert_harness(spec)
    harnesses = supervisor(app)
    harnesses.running[("codex", "dock")] = True
    harnesses.session_refs[("codex", "dock")] = "session-ref-1"
    pinned = f"agent:source:{NODE}:dock"

    with app._worker_status_snapshot():
        assert app._managed_worker_running(pinned) is True
        assert app._worker_session_ref(pinned) == "session-ref-1"

    assert app._managed_worker_running(pinned) is True
    assert app._worker_session_ref(pinned) == "session-ref-1"


def test_worker_status_snapshot_is_scoped_to_the_request_thread(
    tmp_path: Path,
) -> None:
    """快照句柄是 thread-local:并发 IPC 请求互不串台 — 「请求作用域必须
    真的按请求建」是本次改造的风险栏条目,这里钉死它。"""

    app = application(tmp_path)
    spec = HarnessLaunchSpec(harness="pi", name="foo", headless=True)
    app.desired_state.upsert_harness(spec)
    harnesses = supervisor(app)
    harnesses.running[("pi", "foo")] = True
    uri = f"agent:{OWNER}:{NODE}:foo"

    child_verdicts: list[bool | None] = []
    entered = threading.Event()
    proceed = threading.Event()

    def child() -> None:
        harnesses.running[("pi", "foo")] = False
        with app._worker_status_snapshot():
            entered.set()
            assert proceed.wait(5.0)
            child_verdicts.append(app._managed_worker_running(uri))

    with app._worker_status_snapshot():
        thread = threading.Thread(target=child)
        thread.start()
        assert entered.wait(5.0)
        # 子线程已装入自己的快照(running=False),主线程仍读自己的 True。
        assert app._managed_worker_running(uri) is True
        proceed.set()
        thread.join(5.0)

    assert child_verdicts == [False]
    # 快照之外的单点路径仍是 live 查询,读到的是翻转后的 False。
    assert app._managed_worker_running(uri) is False
