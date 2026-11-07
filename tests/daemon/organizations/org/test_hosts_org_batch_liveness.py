"""hosts/org.fetch 批量路径:身份过滤先行 + 一次请求快照(2026-09-15,IPC 38 s)。

生产读数(dev 2a192f44):一次 ``hosts`` ≈ 38 s(客户端 15 s 超时),sample 显示
IPC 线程 99% CPU,帧为 sorted/rlock/sqlite。成因:``hosts`` 与 ``org.fetch`` 的
候选枚举走 ``_LocalPresence.online_actors()``,对 mesh 上**每一个** actor 逐个
跑 interactive-liveness verdict;无请求快照时每个 verdict 支付一次
``_harnesses.status()`` 往返,逐 connector 再做 ``desired_state.load()``
(每次 load 都拿 ``DesiredStateStore._lock`` 并新开一个 SQLite 连接)。
ps/agent.list(intg U2)与 targets(2026-09-05)已先后修过同一族,这两处漏了。

修法是双保险(hyprial-developer 2026-09-15 批复①:同名碰撞格要逐字保真),
下面的守卫各钉一半:

* 身份过滤先行(hosts/org 只对 HOST 幸存者付 liveness)+ 幸存者按
  presence 自己的 keep 谓词(``liveness_keeps``,即 ``online_actors``
  的 interactive 半句)复核 ``is not False``——在快照里是纯表查找,
  不拿锁、不走 SQLite;这样「输出不变」不再依赖「病态配置不会出现」。
* 计数守卫 -- 一次 hosts / org.fetch 的 ``status()`` 与 ``load()`` 次数
  与远端 actor 数无关。hosts 钉**上界**(≤ 1);org 钉**本数**(== 1,
  把「必须装快照」也钉死)。
* AST 结构守卫 -- ``handle()`` / ``_handle_agent()`` 分发方法体内任何
  presence actor 枚举点(``self._presence`` 链上的 ``online_actors()`` /
  ``raw_online_actors()``)必须位于 ``with self._worker_status_snapshot():``
  块内;新增批量 handler 裸奔即红(形态参照
  test_address_parsing_guard.py 的源扫描)。

变异预测矩阵(批复①落地后重写,实跑前写死;实跑对表):

  M1  去快照 + 去先过滤(枚举回 ``online_actors()``、不包 with)
      ⇒ hosts 计数红(status_calls ≈ N)、org 计数红、AST 红
  M2′  只去快照(保留先过滤 + 幸存者复核)
      ⇒ hosts 计数红(复核走逐调用路径,≈ 2×host 数)、org 计数红、
         AST 红 —— 与批复③所照旧矩阵的 delta:M2 旧预测 hosts 绿(0≤1);
      批复①加了复核后,复核的廉价性依赖快照,M2′ 恰好拆掉它,故转红。
      这正是双保险语义的守卫化:两半各拆一次都得红。
  R   去复核(保留快照与先过滤;即已推的 e9b65293 形态)
      ⇒ 碰撞等价测试红(hosts/org 各多出一个同名 node),其余 5 条绿
  b   复核挪到 host 过滤之前(``keep(a) and classify(a)==HOST``;审查中1)
      ⇒ hosts/org 的 verdict 断言红(verdict 打到 agent 形状 actor 上,
         次数 = 全候选而非 host 幸存者);status/load 计数仍 1、AST 绿、
         等价×3 绿(纯谓词次序交换不改输出)
  e   枚举回 ``online_actors()``(装快照,先枚举后过滤;审查中1)
      ⇒ hosts/org 的 verdict 断言红;其余同 b
  f   别名写法(``presence = self._presence``,hosts/org 均改;审查中2)
      ⇒ 仅 AST 守卫红(快照内直接形态枚举点掉到 0 < 反空转下限 2);
         行为不变,其余 5 条绿
  (dev 基线 2a192f44 上跑本文件 == M1 形态:同一次突变的两种取证法。)

钉界(有意留白,不是没想到):

* ``_actor_status_snapshot`` 里的 ``presence_actors=...`` 不在本守卫射程:它
  不是 IPC 分发方法体,其调用方(ps / targets)已装请求快照,由
  test_ps_desired_state_reload.py 的 T1b 钉住。搬进 helper 的枚举同理在
  AST 射程外(与既有钉界同理)。
* AST 守卫的反空转下限锚在 hosts/org 两个**直接形态**枚举点上:别名写法
  若只出现在**新增** handler 而这两处保持直接形态,下限仍满足,该别名
  站点逃过源扫描 —— 那时的畏角是计数守卫(成本面)与评审;把任意别名
  归一化进源扫描需要名字分析,超出本形态。
* org.fetch 的 ``requestMode == "neighbors"`` 早退分支不做 actor 枚举,不在
  计数 / AST 射程;低3 落地后快照只包枚举,不跨 endpoint.fetch 网络我/O。
* 同名碰撞格(本地 worker 裸名 = 远端 node id,worker 停止)已由
  ``liveness_keeps`` 复核逐字保真,并由
  ``test_hosts_and_org_output_identical_when_worker_name_collides_with_node``
  钉住(批复①);等价测试不再需要回避碰撞名。

桩形态照抄仓里既有读数桩,不自造:计数 supervisor 与计数 load
(test_ps_desired_state_reload.py 的 _CountingHarnesses / _CountingDesiredState
同型),org endpoint 桩(test_daemon_org_context.py 的 _Endpoint 同型);
presence 用**真类** ``_LocalPresence``,只在其 inner(LivelinessDirectory)
一层放 directory 桩。
"""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from hyprial.daemon.impl.application import DaemonApplication, _LocalPresence
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.configuration.identity import classify_target_identity
from hyprial.daemon.impl.org.mesh import OrgFetchResult
from hyprial.kernel import TARGET_KIND_HOST
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies

OWNER = "op"
NODE = "node-1"

_TEST_APPLICATIONS: list[DaemonApplication] = []


@pytest.fixture(autouse=True)
def _close_test_applications() -> None:
    # 与 test_ps_status_snapshot.py 同型:agents 注册表开着 sqlite fd,
    # 不关会触发仓里的 fd 泄漏守卫(card #77)。
    yield
    while _TEST_APPLICATIONS:
        _TEST_APPLICATIONS.pop().agents.close()

#: 生产形态的规模量级:mesh 上远端 host 的数量。守卫断言的是与它无关的上界。
REMOTE_HOSTS = 50
SUPERVISOR_ROWS = 3


# --------------------------------------------------------------------------
# doubles(形态照抄 test_ps_desired_state_reload.py / test_daemon_org_context.py)
# --------------------------------------------------------------------------


class _CountingHarnesses:
    """supervisor 桩:``status()`` 计数 -- M1 下它就是 N 次往返的那台计数器。"""

    def __init__(self, rows: tuple[dict[str, object], ...]) -> None:
        self._rows = list(rows)
        self.status_calls = 0

    def status(self) -> tuple[dict[str, object], ...]:
        self.status_calls += 1
        return tuple(self._rows)

    def add_row(self, row: dict[str, object]) -> None:
        self._rows.append(row)


class _StubDirectory:
    """LivelinessDirectory 桩:只实现 ``_LocalPresence`` 读的那三张面。"""

    def __init__(self, actors: tuple[str, ...]) -> None:
        self._actors = actors

    def online_actors(self) -> tuple[str, ...]:
        return self._actors

    def actor_online(self, actor: str) -> bool:
        return actor in self._actors

    def online_mailboxes(self) -> tuple[str, ...]:
        return ()


class _Endpoint:
    """org endpoint 桩,与 test_daemon_org_context._Endpoint 同型。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def fetch(self, **kwargs: Any) -> OrgFetchResult:
        self.calls.append(kwargs)
        return OrgFetchResult(tuple(sorted(kwargs.get("allowed_sources") or ())), 0, ())

    def publish_accepted(self) -> bool:
        return True

    def close(self) -> None:
        pass


def _mixed_directory_actors() -> tuple[str, ...]:
    """混合 presence:远端 host 裸 node id ×N + agent URI + host 显示拼写 + 坏形状。"""

    hosts = tuple(f"remote-node-{index:02d}" for index in range(REMOTE_HOSTS))
    return (
        *hosts,
        "agent:peer-owner:remote-node-00:pi-dev",  # agent:远端 worker,HOST 过滤剔除
        "agent:another:host-x:claude-1",
        "agent:remote-node-07",  # agent:<bare node> 显示拼写,classify 同判 HOST
        "agent:owner:machine",  # unknown 形状,两侧都剔除
    )


def _worker_row(name: str, *, running: bool, pid: int) -> dict[str, object]:
    return {
        "id": f"pi:{name}",
        "runtime": "pi",
        "name": name,
        "running": running,
        "pid": pid,
    }


def _seed_workers(app: DaemonApplication) -> tuple[dict[str, object], ...]:
    """desired harness specs + 匹配的 supervisor 行;无真实进程(同 reload 测试)。"""

    rows: list[dict[str, object]] = []
    for index in range(SUPERVISOR_ROWS):
        name = f"worker-{index}"
        app.desired_state.upsert_harness(
            HarnessLaunchSpec(
                harness="pi",
                name=name,
                headless=True,
                command=("/nonexistent/hyprial-hosts-fix-probe",),
            )
        )
        rows.append(_worker_row(name, running=index == 0, pid=24_000 + index))
    return tuple(rows)


class _CountingVerdicts:
    """把 interactive-liveness seam 包一层，逐 actor 记录调用(中1 的计数器)。

    只记名字不改行为;套在真 seam 或测试 stub 外均可。"""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def __call__(self, actor: str) -> bool | None:
        self.calls.append(actor)
        return self._inner(actor)


def _application(
    tmp_path: Path,
    *,
    actors: tuple[str, ...] | None = None,
    interactive: Any = "real",
) -> tuple[DaemonApplication, _CountingHarnesses, _Endpoint, _CountingVerdicts]:
    app = DaemonApplication(
        state_dir=tmp_path / "state",
        socket_path=tmp_path / "state" / "daemon.sock",
        hyprial_home=tmp_path / "home",
        node_id=NODE,
        owner=OWNER,
    **_deep_module_daemon_dependencies())
    harnesses = _CountingHarnesses(_seed_workers(app))
    endpoint = _Endpoint()
    verdicts = _CountingVerdicts(
        app._interactive_actor_liveness if interactive == "real" else interactive
    )
    app._inbox = object()  # type: ignore[assignment]
    app._harnesses = harnesses  # type: ignore[assignment]
    # 真类 presence:枚举形态、self 并集、interactive 过滤都走生产代码,
    # 桩只放在 inner(directory)一层。默认接真 liveness seam(套计数),
    # 这样 M1(枚举回 online_actors)时逐 actor verdict 会真的打到计数 supervisor。
    app._presence = _LocalPresence(
        _StubDirectory(actors if actors is not None else _mixed_directory_actors()),
        NODE,
        interactive_liveness=verdicts,
    )  # type: ignore[assignment]
    app._org_endpoint = endpoint  # type: ignore[assignment]
    with app._worker_status_snapshot():
        app._status_refresh.record_observation(
            "host-presence", app._online_host_nodes()
        )
    harnesses.status_calls = 0
    verdicts.calls.clear()
    _TEST_APPLICATIONS.append(app)
    return app, harnesses, endpoint, verdicts


def _count_loads(
    monkeypatch: pytest.MonkeyPatch, app: DaemonApplication
) -> list[int]:
    """实例级包装 DesiredStateStore.load(与 test_ps_status_snapshot 同型)。"""

    counts = [0]
    real_load = app.desired_state.load

    def counting_load() -> Any:
        counts[0] += 1
        return real_load()

    monkeypatch.setattr(app.desired_state, "load", counting_load)
    return counts


# --------------------------------------------------------------------------
# 计数守卫:与远端 actor 数无关
# --------------------------------------------------------------------------


def test_hosts_enumeration_pays_no_per_actor_liveness(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """hosts 是批量路径:status()/load() 有与 N 无关的常数上界;
    且 liveness 只落在 host 形状的幸存者上(中1:先过滤后复核这条也钉住)。

    红形态(基线 / M1):枚举走 online_actors() 且不装快照,每个 actor 一次
    supervisor 往返 + 逐 connector 的 desired-state load。
    M2′(只去快照、留先过滤+幸存者复核)也必须红:复核离开快照后走
    逐调用路径,~2 次 status() / host——上界由快照与先过滤**共同**深证。
    变异 (b)(复核挪到 host 过滤之前)/(e)(枚举回 online_actors(),装快照):
    status/load 计数仍是 1,但 verdict 会打到 agent 形状的 actor 上
    ⇒ 本测试的 verdict 断言红(预测先行,读数见证据文件)。
    """

    app, harnesses, _, verdicts = _application(tmp_path)
    loads = _count_loads(monkeypatch, app)

    result = app.handle("hosts", {})

    # 真类 presence 会把 self.node_id 并进候选集(生产形态:本机也在 hosts 里)。
    expected_nodes = sorted(
        (
            NODE,
            "agent:remote-node-07",
            *[f"remote-node-{index:02d}" for index in range(REMOTE_HOSTS)],
        )
    )
    assert [
        {"nodeId": row["nodeId"], "status": row["status"]}
        for row in result["hosts"]
    ] == [{"nodeId": node, "status": "online"} for node in expected_nodes]
    assert all(
        isinstance(row["observedAtMs"], int)
        and isinstance(row["ageMs"], int)
        and 0 <= row["ageMs"] < 1_000
        and row["stale"] is False
        and row["lastError"] is None
        for row in result["hosts"]
    )
    assert harnesses.status_calls <= 1, harnesses.status_calls
    assert loads[0] <= 1, loads[0]
    # 中1:verdict 只落在 host 形状的幸存者上——与 agent 形状的数量无关
    # (presence 里含 3 个非 host 形状:2 个 agent URI + 1 个 unknown)。
    expected_verdicts = sorted(
        (
            "agent:remote-node-07",
            *[f"remote-node-{index:02d}" for index in range(REMOTE_HOSTS)],
        )
    )
    assert sorted(verdicts.calls) == expected_verdicts, verdicts.calls
    assert not any(
        classify_target_identity(actor) != TARGET_KIND_HOST for actor in verdicts.calls
    ), verdicts.calls


def test_org_fetch_enumeration_installs_the_request_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """org.fetch 是批量路径:钉**本数** -- 快照的那一次 status()/load();
    且 verdict 同样只落在 host 形状的幸存者上(中1,同 hosts)。

    先过滤把候选压到 host 子集,幸存者复核与快照把任何后续行加工压回常数;
    这里钉后者:M2′(只去快照、留先过滤+复核)下计数随 host 数增长,必须红。
    低3 落地后快照只包枚举,不跨 endpoint.fetch 的网络 I/O —— 本测试同时钉:
    快照仍在(==1)且仅包枚举(fetch 挪到 with 外不影响计数与输出)。
    变异 (b)/(e) 下本测试的 verdict 断言红(预测先行)。
    """

    app, harnesses, endpoint, verdicts = _application(tmp_path)
    loads = _count_loads(monkeypatch, app)

    result = app.handle("org.fetch", {"timeoutSeconds": 0.25})

    expected_sources = frozenset(
        (
            "agent:remote-node-07",
            *[f"remote-node-{index:02d}" for index in range(REMOTE_HOSTS)],
        )
    )
    assert endpoint.calls == [
        {"source": None, "allowed_sources": expected_sources, "timeout": 0.25}
    ]
    assert result["requestedSources"] == sorted(expected_sources)
    assert harnesses.status_calls == 1, harnesses.status_calls
    assert loads[0] == 1, loads[0]
    # 中1:verdict 只落在 host 形状的幸存者上(self 由 != node_id 提前剔除,
    # 不到 seam)。
    assert sorted(verdicts.calls) == sorted(expected_sources), verdicts.calls
    assert not any(
        classify_target_identity(actor) != TARGET_KIND_HOST for actor in verdicts.calls
    ), verdicts.calls


# --------------------------------------------------------------------------
# 输出不变:先过滤 + 跳过 host 身份的 liveness,改前后逐字节相同
# --------------------------------------------------------------------------


def _interactive_stub(actor: str) -> bool | None:
    """混合 verdict:一个死的 interactive session(False),其余 None。

    None = 「不归本机裁」-- 裸 node id 与远端 peer 的常态;False 模拟心跳
    过期的 interactive session(旧形态会把它从 online_actors() 剔除)。
    """

    return False if actor == "agent:peer-owner:remote-node-00:pi-dev" else None


def test_hosts_output_identical_to_prefilter_shape(tmp_path: Path) -> None:
    """修法 5:混合 presence 下,hosts 输出与旧形态(先 liveness 后过滤)逐字节相同。

    参照侧在测试体内用**修前表达式**重算(online_actors() + HOST 过滤),
    与 test_ps_desired_state_reload.py 的 T3 同型。
    已知限度(审查④,写进 PR 正文):参照侧是重构后的 online_actors(),
    不是原始代码;等价靠「单写者 + 谓词可交换」的构造论证兜底。
    """

    app, _, _, _ = _application(tmp_path, interactive=_interactive_stub)

    reference = {"hosts": [
        {"nodeId": node, "status": "online"}
        for node in sorted(
            actor
            for actor in app._presence.online_actors()
            if classify_target_identity(actor) == TARGET_KIND_HOST
        )
    ]}
    actual = app.handle("hosts", {})
    legacy_actual = {
        "hosts": [
            {"nodeId": row["nodeId"], "status": row["status"]}
            for row in actual["hosts"]
        ]
    }

    assert json.dumps(legacy_actual, sort_keys=True) == json.dumps(reference, sort_keys=True)


def test_org_candidates_identical_to_prefilter_shape(tmp_path: Path) -> None:
    """修法 5(org 侧):candidates 集合与旧形态逐元素相同。"""

    app, _, endpoint, _ = _application(tmp_path, interactive=_interactive_stub)

    reference = frozenset(
        actor
        for actor in app._presence.online_actors()
        if actor != app.node_id
        and classify_target_identity(actor) == TARGET_KIND_HOST
    )
    app.handle("org.fetch", {"timeoutSeconds": 0.25})

    assert endpoint.calls[0]["allowed_sources"] == reference


def test_hosts_and_org_output_identical_when_worker_name_collides_with_node(
    tmp_path: Path,
) -> None:
    """批复①:本地 worker 裸名 = 远端 node id,worker 停止 ⇒ 改前后输出一致。

    旧形态:verdict 对该 node 答 False(裸名命中停止的 worker 行),
    hosts 行与 org candidates 都把它剔除;新形态:幸存者复核(快照内
    表查找,running_by_actor 同样裸名认领 False)也剔除 ⇒ 逐字节一致。
    R 变异(去掉复核)下本条红:该 node 被保留,输出多出一行。
    """

    collide = "collide-node"
    app, harnesses, endpoint, _ = _application(tmp_path, actors=(collide, "remote-node-00"))
    app.desired_state.upsert_harness(
        HarnessLaunchSpec(
            harness="pi",
            name=collide,
            headless=True,
            command=("/nonexistent/hyprial-hosts-fix-collide",),
        )
    )
    harnesses.add_row(_worker_row(collide, running=False, pid=25_000))

    # 参照侧:修前表达式(online_actors() + HOST 过滤),无快照、逐调用
    # verdict -- 与旧 handler 形态一致;同名碰撞在这里就剔除。
    reference_nodes = sorted(
        actor
        for actor in app._presence.online_actors()
        if classify_target_identity(actor) == TARGET_KIND_HOST
    )
    assert collide not in reference_nodes  # 旧形态确实剔除(前提自证)
    actual = app.handle("hosts", {})
    legacy_actual = {
        "hosts": [
            {"nodeId": row["nodeId"], "status": row["status"]}
            for row in actual["hosts"]
        ]
    }

    assert json.dumps(legacy_actual, sort_keys=True) == json.dumps(
        {"hosts": [{"nodeId": node, "status": "online"} for node in reference_nodes]},
        sort_keys=True,
    )

    reference_candidates = frozenset(
        actor
        for actor in app._presence.online_actors()
        if actor != app.node_id
        and classify_target_identity(actor) == TARGET_KIND_HOST
    )
    app.handle("org.fetch", {"timeoutSeconds": 0.25})

    assert endpoint.calls[0]["allowed_sources"] == reference_candidates
    assert collide not in reference_candidates


# --------------------------------------------------------------------------
# 结构守卫:分发方法体内的 presence 枚举必须装请求快照
# --------------------------------------------------------------------------

_DISPATCH_METHODS = ("handle", "_handle_agent")
#: 枚举形态:一次调用就要对全部候选付代价的那几个名字。
_ENUMERATION_ATTRS = {"online_actors", "raw_online_actors"}
#: F6 后 _ipc_hosts 的枚举委托给 _online_host_nodes —— 该调用点本身也计入
#: 快照内形态(委托不豁免);反空转下限改由 hosts(PsProject)与 org 的
#: 真实枚举共同满足,不再硬编码 2。
_WATCHED_ATTRS = _ENUMERATION_ATTRS | {"liveness_keeps", "actor_online", "_online_host_nodes"}


def _self_attribute_chain(call: ast.Call) -> list[str]:
    """``self.a.b.c()`` 的属性链(近端在前);非 self 根的调用返回空。"""

    node: ast.expr = call.func
    attrs: list[str] = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id != "self":
        return []
    return attrs


def _is_presence_watched(call: ast.Call) -> bool:
    attrs = _self_attribute_chain(call)
    return bool(attrs) and attrs[0] in _WATCHED_ATTRS and "_presence" in attrs


def _installs_snapshot(node: ast.With) -> bool:
    for item in node.items:
        expr = item.context_expr
        if (
            isinstance(expr, ast.Call)
            and _self_attribute_chain(expr) == ["_worker_status_snapshot"]
        ):
            return True
    return False


def _watched_sites(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[list[tuple[int, str]], int]:
    """(裸奔的被监视调用, snapshot 内的被监视形态点数)。

    返回的第二项是反空转下限的分子:hosts 委托给 _online_host_nodes 后,
    该调用点在快照内计 1,org.fetch 的直接枚举计 1 ⇒ 下限 = 2 依旧成立。
    若 hosts 被改写成别名等抓不到的形态,这个数会掉到 2 以下,守卫红 ——
    而不是「零违规地绿着」。
    """

    bare: list[tuple[int, str]] = []
    inside_enumerations = 0

    def visit(node: ast.AST, inside_snapshot: bool) -> None:
        nonlocal inside_enumerations
        for child in ast.iter_child_nodes(node):
            child_inside = inside_snapshot or (
                isinstance(node, ast.With) and _installs_snapshot(node)
            )
            if isinstance(child, ast.Call):
                attrs = _self_attribute_chain(child)
                name = attrs[0] if attrs else None
                watched = (
                    _is_presence_watched(child)
                    or (name == "_online_host_nodes" and "_self" in attrs)
                )
                if watched:
                    if child_inside:
                        # F6 后 _ipc_hosts 委托给 _online_host_nodes —— 委托调用
                        # 在快照内与直接枚举等价(整链在快照保护下),同样计数。
                        inside_enumerations += 1
                    else:
                        bare.append((child.lineno, name))
            visit(child, child_inside)

    visit(fn, False)
    return bare, inside_enumerations


def test_dispatch_presence_enumeration_sits_under_a_request_snapshot() -> None:
    """AST 守卫:handle()/_handle_agent() 里的被监视调用必须在快照内;
    且快照内的枚举点**至少 2 个**(hosts + org)—— 反空转下限(审查中2)。

    变异:M1/M2′ 枚举点裸奔红;变异 f(别名写法 presence = self._presence)
    下直接形态的枚举点消失,计数掉到 2 以下 ⇒ 红(预测先行,实跑读数见
    证据文件)。
    钉界(有意留白):别名写法若只出现在**新增** handler 而 hosts/org 保持
    直接形态,反空转下限仍满足,该别名站点逃过本守卫 —— 那时的畏角是
    计数守卫(成本面)与评审;把任意别名归一化进本守卫需要名字分析,
    超出源扫描形态。搬进 helper 的枚举同样在射程外(与既有钉界同理)。
    """

    from tests.daemon.startup.test_startup_event_reconciliation import _native_application_source
    tree = ast.parse(_native_application_source())
    # The two former handle() branches became exact native IPC callees.
    # Include them in their original logical handler, keeping the original
    # two-method denominator and >=2 real enumeration lower bound.
    native = {node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    handler = native["handle"]
    dispatch = Path(DaemonApplication.handle.__code__.co_filename).read_text(encoding="utf-8")
    for method in ("_ipc_org_fetch", "_ipc_hosts"):
        if f"self.{method}(" not in dispatch:
            raise AssertionError(f"Native IPC dispatch no longer calls {method}")
        handler.body.extend(native[method].body)
    methods = {
        item.name: item
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name in _DISPATCH_METHODS
    }
    assert set(methods) == set(_DISPATCH_METHODS), sorted(methods)
    bare: dict[str, list[tuple[int, str]]] = {}
    inside_total = 0
    for name, fn in methods.items():
        sites, inside = _watched_sites(fn)
        bare[name] = sites
        inside_total += inside
    assert all(not sites for sites in bare.values()), (
        f"presence watched calls outside a _worker_status_snapshot() block: {bare}"
    )
    assert inside_total >= 2, (
        "vacuous guard: fewer than 2 enumeration sites inside snapshots "
        f"(hosts + org expected), found {inside_total}"
    )
