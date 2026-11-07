# WP1：link 的模型、控制器与准入策略（实施规格）

- 日期：2026-10-14
- 段：设计（claude / claude-opus-5-5）
- 分支：`wp1/send-links`，base `refactor/org-structure-constrain`
- 依据：[design-send-links-2026-10-14.md](../design-send-links-2026-10-14.md) §3、§4、§5、§14；缝 `src/hyprial/daemon/impl/links/ports.py`（WP0）
- 范围：`src/hyprial/daemon/impl/links/` 下新增 `model.py`、`controller.py`、`policy.py`，全部为纯函数与不可变数据；配套单元测试。不接线、不读写 orgfs、不订阅 hook bus（那是 WP2）。

## 0. 硬约束（实施前先读）

1. 依赖只许：标准库、`hyprial.kernel`（公共面，即 `from hyprial.kernel import ...`）、`hyprial.daemon.impl.links.ports`、以及本包内从纯到脏的前序文件（`policy` 可 import `model` 与 `controller`；`controller` 可 import `model` 的类型；`model` 不 import `controller`/`policy`）。⛔ 不得 import `inbox`、`orgfs`、`application`、`dispatch`、`delivery`、`composition`、`biz`、`shell`。由测试 T-B1 机检。
2. 纯：不读时钟（`time`、`datetime.now`）、不用随机数（`random`、`secrets`）、不做 I/O、不打日志。时刻一律是参数 `now_ms: int`；抖动一律是参数 `jitter: float`（调用方 WP2 从带种子的 RNG 取，范围 `[-1.0, 1.0]`）。由 T-B2 机检。
3. 所有数据类 `@dataclass(frozen=True, slots=True)`；状态推进以「旧状态 → 新状态」返回，不原地改。
4. URI 只经 kernel 的 `uri_scheme`、`parse_agent_uri`、`parse_user_uri` 解构；⛔ 不对 URI 做 `split(":")`（仓库已有 addressing guard）。
5. `ports.py` 的名字一律不改、不重定义。特别是 `LinkState` 已在 ports 定义为 `Literal["pending", "established", "reclaimed"]`：`model.py` 直接 import 并使用它，⛔ 不再定义同名类（见 §5「对缝的建议」第 1 条）。
6. `links/__init__.py` 本包不改：其他包仍只认 ports；新模块由 WP2 与测试按子模块路径导入。
7. 结构门：`links/` 加三个文件后为 5 个直接文件（WP2 再加 `store.py`、`service.py` 到 7）。⛔ 不新增第 6 个源文件；每个文件 ≤ 1000 行（预估 model ≈ 250、controller ≈ 400、policy ≈ 350）。`tests/daemon/links/` 现有 2 个文件，本包新增 3 个测试文件到 5，给 WP2 留 2 个位；⛔ 不新增第 4 个测试文件，劣化条件测试并入 `test_controller.py` 与 `test_policy.py`。

## 1. 改哪些文件

| 文件 | 动作 | 职责 |
| --- | --- | --- |
| `src/hyprial/daemon/impl/links/model.py` | 新建 | link 身份、单份 link 文件、两份互引的判定、生命周期标记、最近使用时刻、文件文档的编解码 |
| `src/hyprial/daemon/impl/links/controller.py` | 新建 | 常数集中处；样本类型；滑动窗口统计；k、W、cwnd 的纯估计与周期推进；放弃阈值与审查样本 |
| `src/hyprial/daemon/impl/links/policy.py` | 新建 | `admit` 发送决策；owner 豁免；回复判定；派发准入；结算提示；回写引用（被铸造方）；淘汰与 k 收缩选择；回收后残余；抖动缓解 |
| `tests/daemon/links/test_model.py` | 新建 | model 的单元测试 |
| `tests/daemon/links/test_controller.py` | 新建 | controller 单元测试 + §5.6 控制面劣化条件 + 依赖/纯度机检 |
| `tests/daemon/links/test_policy.py` | 新建 | policy 单元测试 + §5.6 策略面劣化条件 |

不改：`ports.py`、`links/__init__.py`、任何 `src/` 其他文件、`contract/`。

### 1.1 `model.py`

```python
from hyprial.daemon.impl.links.ports import LINK_DIR, LinkState

Lifecycle = Literal["regular", "pac_task"]
LINK_FILE_SCHEMA = 1          # 文档版本号

@dataclass(frozen=True, slots=True)
class LinkEntry:              # 一份 link 文件里对一个对端的引用（§3.1 每条边携带的字段）
    peer: str                 # 对端 canonical URI（agent: 或非 owner 的 user:）
    minted_at_ms: int
    revision: int             # 该条目被写入时文件的修订号
    lifecycle: Lifecycle = "regular"
    task_id: str | None = None   # lifecycle == "pac_task" 时必填，否则必须为 None
    last_used_ms: int = 0        # 0 表示铸造后未用；见下「最近使用」

@dataclass(frozen=True, slots=True)
class LinkFile:               # link/<actor>.json 的内存形态（§6）
    actor: str
    revision: int
    entries: Mapping[str, LinkEntry]   # peer -> entry；构造时转成只读 MappingProxy

@dataclass(frozen=True, slots=True)
class Link:                   # 从「我的文件 + 对端文件」推导出的一条边（本地视角）
    actor: str
    peer: str
    state: LinkState          # ports.LinkState
    mine: bool                # 我的文件引用了对端
    theirs: bool              # 对端文件引用了我
    entry: LinkEntry | None   # 我这一侧的条目（mine 为 False 时为 None）

def link_key(a: str, b: str) -> tuple[str, str]
def is_link_endpoint(uri: str) -> bool
def owner_of(uri: str) -> str | None
def is_owner_pair(a: str, b: str) -> bool
def link_file_path(actor: str) -> str
def derive_link(actor: str, peer: str, mine: LinkFile | None, theirs: LinkFile | None,
                *, reclaimed: bool = False) -> Link | None
def effective(link: Link | None) -> bool
def links_of(actor: str, mine: LinkFile | None, files: Mapping[str, LinkFile],
             reclaimed: AbstractSet[str] = frozenset()) -> Mapping[str, Link]
def with_reference(file: LinkFile, entry: LinkEntry) -> LinkFile
def without_reference(file: LinkFile, peer: str) -> LinkFile
def touch(file: LinkFile, peer: str, now_ms: int) -> LinkFile
def task_bound_peers(file: LinkFile, task_id: str) -> tuple[str, ...]
def to_document(file: LinkFile) -> dict[str, Any]
def from_document(doc: Mapping[str, Any]) -> LinkFile
```

语义：

- **身份**：一条 link 的身份是两个端点的无序对。`link_key` 返回字典序排好的二元组；两端相同、任一端不是 `is_link_endpoint` 时抛 `ValueError`。`is_link_endpoint`：`agent:` 且 `parse_agent_uri` 成功，或 `user:` 且 `parse_user_uri` 成功。（owner 是否可作端点由 `is_owner_pair` 另判，见下。）
- **owner**：`owner_of(agent_uri)` 返回 `canonical_user_uri(parse_agent_uri(uri)[0])`，非 agent 返回 None。`is_owner_pair(a, b)`：一端是 agent、另一端恰为该 agent 的 `owner_of`。owner 对不写文件：`with_reference` 收到 owner 对时抛 `ValueError`（§3.2：owner link 不写 orgfs）。
- **两份互引才有效**（`derive_link`）：
  - mine 与 theirs 都引用 → `established`；
  - 仅一侧引用 → `pending`（`mine`/`theirs` 标出方向）；
  - 两侧都不引用 → `None`；
  - `reclaimed=True` 且任一侧仍引用 → `reclaimed`（回收是本地已知的事实，文件副本可能还没同步掉）。
  - 文件 `actor` 与参数不符时抛 `ValueError`（防把错文件当成对端）。
  - `effective(link)` 当且仅当 `state == "established"`。
- **生命周期**：`LinkEntry` 构造时校验 `lifecycle == "pac_task"` ⇔ `task_id` 非空。`task_bound_peers(file, task_id)` 返回该任务绑定的对端（字典序），供 WP2 在 PAC 任务结束时回收。
- **最近使用**：`touch` 返回 `last_used_ms = max(旧值, now_ms)` 的新文件，不改 `revision`（使用时刻是本地运行态，是否落 orgfs 由 WP2 定；不得为它推高修订号造成 CRDT 写放大）。
- **修订号**：`with_reference`/`without_reference` 返回 `revision + 1` 的新文件；对已存在的同一 peer 再 `with_reference` 覆盖条目；`without_reference` 对不存在的 peer 原样返回（修订号不变）。
- **编解码**：`to_document` 输出 `{"schema": 1, "actor", "revision", "peers": [{...}, ...]}`，peers 按 peer 字典序，可被 `json.dumps(sort_keys=True)` 稳定序列化；`from_document` 是严格逆：缺键、类型不符、schema 不认识、条目重复、端点非法均抛 `ValueError`。`link_file_path(actor)` 返回 `f"{LINK_DIR}/{actor}.json"`，actor 非合法端点抛 `ValueError`。

### 1.2 `controller.py`

```python
@dataclass(frozen=True, slots=True)
class ControllerConstants:     # §5.5：所有常数集中在这里，且只在这里
    alpha: float = 1.0
    beta: float = 3.0
    probe_gain: float = 1.25          # BBR 探测增益
    abandon_factor: float = 4.0       # 放弃阈值 = 4 × RTT_max
    window_factor: float = 10.0       # 窗口与周期 = 10 × T_min
    min_window_samples: int = 10
    jitter_ratio: float = 0.1         # 周期抖动比例（设计未给值，见 §5 第 4 条）
    ewma_gain: float = 0.125          # T_bar/RTT_bar 的指数平滑增益（设计未给值）
    stale_rtt_factor: float = 2.0     # cwnd：超过 2 × RTT_min 无回复按比例收（§5.4）
    grad_streak: int = 2              # 连续两周期 grad > 0 才按比例收（§5.3）
    min_link_lifetime_periods: float = 1.0   # 最短存活期，以周期 P 计（设计未给值）
    churn_limit_per_period: int = 0          # 0 表示取 k；见 policy 抖动缓解

DEFAULTS: Final = ControllerConstants()

@dataclass(frozen=True, slots=True)
class DwellSample:   at_ms: int; dwell_ms: int; censored: bool = False   # T
@dataclass(frozen=True, slots=True)
class RttSample:     peer: str; at_ms: int; rtt_ms: int; censored: bool = False
@dataclass(frozen=True, slots=True)
class ReplySample:   at_ms: int; peer: str | None = None   # 本节点发出一条回复（吞吐）；peer 用于每 link 吞吐

@dataclass(frozen=True, slots=True)
class WindowStats:
    n: int; t_min_ms: int | None; t_max_ms: int | None; t_bar_ms: float | None
    mu_max_per_ms: float | None; w_target: int | None; window_ms: int | None

@dataclass(frozen=True, slots=True)
class LoopState:               # 一个 Vegas/CDG 回路（节点 W 或某条 link 的 cwnd 共用）
    value: int                 # W 或 cwnd
    prev_t_min_ms: int | None  # 上一周期窗口的 T_min（求 grad）
    grad_pos_streak: int
    next_eval_ms: int | None   # None：尚未开始计时（冷启动）
    last_diff: float | None
    last_grad: int | None

@dataclass(frozen=True, slots=True)
class ControllerInput:
    n_active: int
    dwell: Sequence[DwellSample]
    replies: Sequence[ReplySample]
    rtt: Sequence[RttSample]
    links: AbstractSet[str]               # 当前非 owner、非 reclaimed 的对端
    unanswered: Mapping[str, int]         # peer -> 最老一条未回复出站请求的发出时刻（无则缺省）

@dataclass(frozen=True, slots=True)
class ControllerState:
    w: LoopState
    cwnd: Mapping[str, LoopState]

@dataclass(frozen=True, slots=True)
class ControllerOutput:
    n_active: int; k: int; w: int; cwnd: Mapping[str, int]
    period_ms: int | None
    node: WindowStats
    links: Mapping[str, WindowStats]
    diff: float | None; grad: int | None

def compute_k(n_active: int, w: int) -> int
def window_stats(dwell: Sequence[DwellSample], replies: Sequence[ReplySample], now_ms: int,
                 c: ControllerConstants = DEFAULTS) -> WindowStats
def rtt_window_stats(rtt: Sequence[RttSample], replies: Sequence[ReplySample], peer: str,
                     now_ms: int, c: ControllerConstants = DEFAULTS) -> WindowStats
def period_ms(t_min_ms: int, jitter: float, c: ControllerConstants = DEFAULTS) -> int
def abandon_threshold_ms(rtt_max_ms: int, c: ControllerConstants = DEFAULTS) -> int
def censor(open_since_ms: int, now_ms: int, threshold_ms: int) -> int | None
def vegas_step(loop: LoopState, stats: WindowStats, *, floor: int, now_ms: int,
               jitter: float, c: ControllerConstants = DEFAULTS) -> LoopState
def shrink_proportional(value: int, base_ms: int, observed_ms: float, *, floor: int) -> int
def cap_cwnd(cwnd: Mapping[str, int], w: int) -> Mapping[str, int]
def initial_state(n_active: int, c: ControllerConstants = DEFAULTS) -> ControllerState
def evaluate(state: ControllerState, inp: ControllerInput, now_ms: int, jitter: float,
             c: ControllerConstants = DEFAULTS) -> tuple[ControllerState, ControllerOutput]
```

算法（设计 §5 原样落地，以下是实施时不得自行发挥的部分）：

- **k**（§5.2）：`compute_k(n, w) = min(max(2, ceil(ln n)), max(2, w))`；`n <= 1` 时 `ln` 不取，按 2 计。`n < 0` 抛 `ValueError`。读数锚点：`n=23 → 4`，`n=350 → 6`，`n=3 → 2`，`n=23, w=3 → 3`。
- **窗口**（§5.3）：样本按 `at_ms` 取窗口 `[now − window_factor × T_min, now]`，但不少于最近 `min_window_samples` 个（不足则取全部）。T_min 取自窗口内样本求得的最小 `dwell_ms`；先用全部样本中最近 `min_window_samples` 个求初始 T_min 以确定窗口长度，再在窗口内重算——两步求法写在 docstring 里，测试锁定。窗口外的样本不参与任何统计（这就是「基线到期重测」与「基线统一到期」）。
- **T_bar**：对窗口内样本按 `at_ms` 升序做 EWMA，`t_bar = (1−g)·t_bar + g·x`，首样本初值 = 该样本值，`g = ewma_gain`。审查样本（`censored=True`）照常计入，值即阈值。
- **mu_max 与 W_target**：把窗口按 `T_min` 长度从窗口起点切桶，`mu_max = max(桶内 ReplySample 数) / T_min`；`W_target = ceil(mu_max × T_min)`，即「任一 T_min 桶内最多回复数」。窗口内无回复时 `mu_max = 0`、`W_target = None`（不设上限之外的探测，见下）。
- **grad**：每次周期评估时 `grad = t_min(本窗口) − prev_t_min`；首个周期 `grad = None`，按 `<= 0` 处理。
- **周期**（§5.5）：`period_ms(t_min, jitter) = max(1, round(window_factor × t_min × (1 + jitter_ratio × jitter)))`，`jitter ∉ [-1, 1]` 抛 `ValueError`。周期只在 `now_ms >= next_eval_ms` 时推进；推进后 `next_eval_ms = now_ms + period_ms(...)`。
- **Vegas/CDG 一步**（`vegas_step`，W 与 cwnd 共用）：
  1. 样本数 `< min_window_samples` → 冷启动：值保持不变（W 停在 k、cwnd 停在 1），`next_eval_ms` 保持 None；
  2. 未到期 → 原样返回；
  3. `diff = value × (1 − T_min / T_bar)`；`grad > 0` 则 `streak + 1` 否则清零；
  4. 依次判（命中一条即止）：`streak >= grad_streak` → `value = max(floor, floor_div(value × T_min, T_bar))`，streak 清零；`diff > beta` → `value − 1`；`diff < alpha 且 grad <= 0` → `value + 1`，再以 `ceil(probe_gain × W_target)` 封顶（`W_target` 为 None 时不加）；否则不变；
  5. 结果不低于 `floor`。每周期至多 +1（服务率随负载下降的缓解）。
- **W**：`floor = k`，初值 `k`（`initial_state` 用 `compute_k(n_active, w=2**31)` 的无上限值作初始 W，再按 `compute_k(n, W)` 取 k）。每次 `evaluate` 先用上一轮 W 算 k，再推进 W，再把 W 抬到不低于新 k。
- **cwnd**（§5.4）：对 `inp.links` 中每个对端一个 `LoopState`，初值 1，`floor = 1`，T 换成该 link 的 RTT 样本，W_target 换成该 link 的回复吞吐（`ReplySample.peer == peer` 指本节点在该 link 上**收到**的回复——WP2 喂样本时须区分；见 §5 第 3 条）。新出现的对端插入初值，消失的对端（回收）直接删除。`unanswered[peer]` 早于 `now − stale_rtt_factor × RTT_min` 时，`cwnd = shrink_proportional(cwnd, RTT_min, now − 发出时刻, floor=1)`，即 `max(1, floor_div(cwnd × RTT_min, 观测时长))`。最后 `cap_cwnd`：若 `sum(cwnd) > W`，按比例缩到 `sum ≤ W`（每条先 `floor(cwnd × W / sum)`、不低于 1；仍超则从最大者起逐个减 1，同值按 peer 字典序，结果确定）。
- **放弃阈值与审查样本**（§5.1）：`abandon_threshold_ms(rtt_max) = ceil(abandon_factor × rtt_max)`；`censor(open_since, now, threshold)` 在 `now − open_since >= threshold` 时返回 `threshold`（按阈值计入的样本值），否则 None。入站 T 的审查阈值：发起方 link 有 RTT 时用 `4 × 该 link 的 RTT_max`，无则用 `4 × 本节点窗口内 T_max`；两者都无（冷启动）不审查。此取法设计未写死，见 §5 第 2 条。
- **输出**：`ControllerOutput` 带齐 `link stats` 要显示的分量（§9）：N_active、k、W 及 W_target、T_min、T_bar、diff、grad、各 link 的 RTT 统计与 cwnd。

### 1.3 `policy.py`

```python
@dataclass(frozen=True, slots=True)
class PendingRequest:  message_id: str; requester: str; received_at_ms: int   # 入站、未回复
@dataclass(frozen=True, slots=True)
class OutboundRequest: message_id: str; peer: str; sent_at_ms: int           # 出站、未收到回复、未放弃

@dataclass(frozen=True, slots=True)
class NodeView:                       # 发送方 daemon 为一个 actor 组装的本地快照（WP2 组装）
    actor: str
    links: Mapping[str, Link]         # 非 owner 对端 -> model.Link
    pending_inbound: Mapping[str, PendingRequest]   # message_id -> 请求
    drained: AbstractSet[str]         # 因回收而排干（已 ack）的入站请求 message_id
    outbound: Sequence[OutboundRequest]
    limits: ControllerOutput
    recent_reclaims_ms: Sequence[int] # 本 actor 近期回收（淘汰/drop/收缩）的时刻
    rtt_max_ms: Mapping[str, int]     # 每 link 的 RTT_max，给放弃阈值
    constants: ControllerConstants = DEFAULTS

@dataclass(frozen=True, slots=True)
class Residue:                        # 回收后残余（§4.4）
    peer: str
    drain_ack: tuple[str, ...]        # 入站来自该对端、仍 pending 的请求：结算为 ack
    released_outbound: tuple[str, ...]  # 出站到该对端的未闭合请求：计数清零（不重发）

def governed(sender: str, target: str) -> bool
def admit(view: NodeView, *, sender: str, target: str, intent: str,
          reply_to: str | None, now_ms: int) -> SendDecision
def admit_dispatch(view: NodeView, *, sender: str, message_id: str, now_ms: int) -> bool
def settle_hint(view: NodeView, *, message_id: str) -> SettleHint
def accept_reference(view: NodeView, *, requester: str, now_ms: int) -> bool
def select_victim(view: NodeView, *, now_ms: int) -> str | None
def shrink_to_k(view: NodeView, *, now_ms: int) -> tuple[str, ...]
def abandoned(view: NodeView, *, now_ms: int) -> tuple[str, ...]
def residue(view: NodeView, *, peer: str) -> Residue
def degree(view: NodeView) -> int
def unclosed(view: NodeView) -> int
def churning(view: NodeView, *, now_ms: int) -> bool
```

决策表（`admit`，在发送方 daemon 对 `view.actor == sender` 求值；`sender != view.actor` 抛 `ValueError`）：

| 次序 | 条件 | 结果 |
| --- | --- | --- |
| 1 | `governed` 为假：目标为 `route:`、或 `is_owner_pair(sender, target)`（agent→自己的 owner，或 owner→自己的 agent），或发送者为 `system:` | `Allow(peer=None)`；不占 k、不查窗口（owner 豁免 §3.2，优先级反转缓解） |
| 2 | `reply_to` 非空（只认 reply-to，不看 intent，R17） | 见下「回复」 |
| 3 | 到 target 的 link `effective` | 在途 `>= cwnd[target]` → `Hold(LINK_WINDOW_FULL, target, "cwnd")`；否则 `unclosed(view) >= W` → `Hold(LINK_WINDOW_FULL, target, "W")`；否则 `Allow(peer=target)` |
| 4 | link 存在且 `pending`、`mine=True`（我已引用、对端未回引） | `Hold(LINK_PEER_FULL, target, "awaiting-peer")` |
| 5 | 无 link，或 `pending` 且 `mine=False`（对端已引用我）或 `reclaimed` | `degree < k` → `Mint(target)`；否则 `select_victim` 非空且 `not churning` → `Mint(target)`（WP2 先回收 victim 再写引用，见 §5 第 5 条）；否则 `Hold(LINK_SELF_FULL, target, "k")`，`churning` 时 detail 为 `"churn"` |

回复（次序 2）：
- `reply_to in view.pending_inbound` 且 `requester == target`，且（owner 对 或 link `effective`）→ `Allow(peer=target 或 None)`；不查 cwnd、不查 W、不占 k（§4.5）。
- `reply_to in view.pending_inbound` 但 `requester != target` → 抛 `DaemonRequestError(ipc_errors.INVALID_ARGUMENT, ...)`（reply-to 与目标不一致，不是 link 问题）。
- `reply_to in view.pending_inbound` 且 link 不 `effective`（已回收、或互引失效）→ 抛 `DaemonRequestError(ipc_errors.LINK_RECLAIMED, ..., data={"message_id": reply_to, "peer": target})`。
- `reply_to in view.drained` → 同上 `LINK_RECLAIMED`（排干后的残余回复，R11）。
- 其余（不在 pending 也不在 drained：已回复、已 ack、从未存在）→ 抛 `DaemonRequestError(ipc_errors.MESSAGE_REPLY_UNAVAILABLE, ...)`，沿用既有语义。
- 不带 `reply_to` 的消息永远走次序 3–5：daemon 不替 agent 认定「这其实是回复」。

其他函数：

- `degree(view)`：`mine=True` 且非 reclaimed 的 link 数（含我单边引用的 pending；不含 owner、不含对端单边引用我的）。
- `unclosed(view)`：`len(pending_inbound) + len(outbound)`（§2：入站未回加出站未收到回复）。来自 owner 的入站请求计入（§3.2：它引发的回复计入 W），但 owner 发来的消息本身不受准入（见 `admit_dispatch`）。
- `admit_dispatch`（§4.6、§5.3）：`governed(sender, view.actor)` 为假 → True（owner/system 不经 W 准入）；link 非 `effective` → False（互引未同步：先落库不派发）；`len(pending_inbound) >= W`（本条不在其中时）→ False；否则 True。`message_id` 已在 `pending_inbound` 中（已注入过）→ True（幂等）。
- `settle_hint`：`message_id in drained` → `"ack"`；`in pending_inbound` → `"keep"`；否则 `"ack"`（已回复/已 ack/从未注入）。
- `accept_reference`（被铸造方是否写回引用，§4.2）：`degree < k` 且 `not churning` → True；否则 False（发送方看到的是 `LINK_PEER_FULL`）。被铸造方满时不为了接受新引用而淘汰。
- `select_victim`（§4.2、§5.6 link 抖动）：候选 = `mine=True`、`state` 为 established 或 pending、`lifecycle == "regular"`、无在途出站请求、无来自该对端的 pending 入站、`now − minted_at_ms >= min_link_lifetime_periods × limits.period_ms`（period 为 None 时按 `limits` 不可用处理：不淘汰任何 link）。取 `last_used_ms` 最小者（LRU），并列按 peer 字典序。无候选返回 None。PAC 任务绑定的 link 不参与 LRU 淘汰，只随任务结束回收（§5 第 6 条）。
- `shrink_to_k`：`degree > k` 时按 `select_victim` 的同一序选出 `degree − k` 个可回收者；有在途或未过最短存活期者跳过（下次重算再收），返回可能少于差额。
- `abandoned`（队头阻塞缓解）：返回 `outbound` 中 `now − sent_at >= abandon_threshold_ms(rtt_max_ms[peer])` 的 message_id（无 RTT_max 的 link 不放弃）。只释放名额；⛔ 不产生任何重发决策（函数返回的只是 id，签名里没有重发）。
- `residue(view, peer)`：`drain_ack` = `pending_inbound` 中 `requester == peer` 的 id（按 `received_at_ms`、再按 id 排序）；`released_outbound` = `outbound` 中 `peer` 的 id。
- `churning`：`recent_reclaims_ms` 中落在 `[now − period_ms, now]` 的个数 `> limit`，`limit = churn_limit_per_period or k`；`period_ms` 为 None 时返回 False。

## 2. 公共面与接口

- **ports 不变**。WP1 的产物与 ports 的对位：
  - `policy.admit` 的参数与 `LinkGate.admit_send` 一一对应（多一个首位 `view`），返回类型就是 `ports.SendDecision`（`Allow | Mint | Hold`），⛔ 不引入第四种返回值。WP2 的 `LinkGate.admit_send` 实现 = 组装 `NodeView` + 调 `policy.admit`。拒绝（`LINK_RECLAIMED`、`MESSAGE_REPLY_UNAVAILABLE`、`INVALID_ARGUMENT`）以 kernel 的 `DaemonRequestError(code, message, data)` 抛出，这是 IPC 层既有的拒绝通道，WP2 原样透传。
  - `policy.admit_dispatch` 对位 `LinkGate.admit_dispatch`；`policy.settle_hint` 对位 `LinkGate.settle_hint`，返回 `ports.SettleHint`。
  - `LinkGate.sender_known` 只是「本地副本里有没有发送方的文件」，属 WP2 store 的存在性查询，WP1 不提供。
  - `Hold.reason` 只用 `HOLD_REASONS` 里的三个；`Hold.detail` 固定词表：`"cwnd"`、`"W"`、`"awaiting-peer"`、`"k"`、`"churn"`（WP6 CLI 与 WP8 skill 可据此解释）。
  - `model.Link.state` 的类型就是 `ports.LinkState`。
  - `model.link_file_path` 以 `ports.LINK_DIR` 为前缀。
- **新增的模块级公共名**（`__all__` 写全，供 WP2/测试按子模块导入）：
  - `model`：`Lifecycle`、`LINK_FILE_SCHEMA`、`LinkEntry`、`LinkFile`、`Link`、以及 §1.1 全部函数。
  - `controller`：`ControllerConstants`、`DEFAULTS`、`DwellSample`、`RttSample`、`ReplySample`、`WindowStats`、`LoopState`、`ControllerInput`、`ControllerState`、`ControllerOutput`、以及 §1.2 全部函数。
  - `policy`：`PendingRequest`、`OutboundRequest`、`NodeView`、`Residue`、以及 §1.3 全部函数。
- 不加入 `links/__init__.py` 的导出，`daemon/interface.py` 不变：这三模块只给 links 包内（WP2）用。

## 3. 测试清单

运行：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links -q`。实施时先提交只含测试的红提交（或在 PR 正文贴红跑输出），再写实现。新模块不存在时，三个测试文件整体在收集期红：`ModuleNotFoundError: No module named 'hyprial.daemon.impl.links.model'`（`controller`、`policy` 同）；这是第一道红。下表「先红形状」指模块骨架存在（名字已导出、函数体 `raise NotImplementedError`）之后每条的失败形状——实施者应先落骨架再逐条转绿，PR 证据里两种红都要有。

URI 固定用：`A = "agent:o:m:a"`、`B = "agent:o:m:b"`、`C = "agent:o:m:c"`、`OWNER = "user:o"`、`X = "agent:p:m:x"`（另一 owner 的 agent）、`U = "user:p"`。

### 3.1 `tests/daemon/links/test_model.py`

| ID | 测什么 | 先红形状 |
| --- | --- | --- |
| M1 | `link_key(B, A) == link_key(A, B) == (A, B)`；`link_key(A, A)`、`link_key(A, "bad")`、`link_key(A, "route:x:y")` 抛 `ValueError` | `NotImplementedError`；之后误实现为有序对时 `AssertionError: ('agent:o:m:b', 'agent:o:m:a') != (...)` |
| M2 | `is_owner_pair(A, OWNER)` 与 `(OWNER, A)` 为真；`(A, U)`、`(X, OWNER)`、`(A, B)` 为假；`owner_of(A) == OWNER`、`owner_of(OWNER) is None` | `AssertionError: assert False` |
| M3 | 两份互引才有效：双引用 → `established` 且 `effective`；只我引用 → `pending, mine=True, theirs=False`；只对端引用 → `pending, mine=False, theirs=True`；都无 → `None`；`reclaimed=True` 且仍有引用 → `reclaimed` 且不 `effective` | `AssertionError: assert 'pending' == 'established'` 一类 |
| M4 | 分区与过期副本：对端文件缺失（`theirs=None`）只得 `pending`，不得 `established`，也不得 None（我侧引用仍在，「只 hold 不丢」） | `AssertionError: assert None == Link(...)` |
| M5 | `derive_link` 传入 `actor` 不符的文件抛 `ValueError` | `Failed: DID NOT RAISE <class 'ValueError'>` |
| M6 | `LinkEntry(lifecycle="pac_task", task_id=None)` 与 `LinkEntry(lifecycle="regular", task_id="t1")` 都抛 `ValueError`；`task_bound_peers` 只返回该任务的对端、字典序 | `DID NOT RAISE` / `AssertionError: assert ('c', 'b') == ('b', 'c')` 形 |
| M7 | 修订号：`with_reference` +1、覆盖同 peer；`without_reference` 移除 +1，移除不存在者修订号不变；原对象不变（frozen） | `AssertionError: assert 1 == 2` |
| M8 | `touch` 只推 `last_used_ms`（取 max，时间不回退），`revision` 不变 | `AssertionError: assert 6 == 5` |
| M9 | owner 对不写文件：`with_reference(file_of_A, LinkEntry(peer=OWNER, ...))` 抛 `ValueError` | `DID NOT RAISE` |
| M10 | 编解码往返：`from_document(to_document(f)) == f`；`json.dumps(to_document(f), sort_keys=True)` 对条目插入顺序不敏感；坏文档（缺 `schema`、`schema=2`、重复 peer、非法 URI、`revision` 为字符串）各抛 `ValueError` | `AssertionError` / `DID NOT RAISE` |
| M11 | `link_file_path(A) == "link/agent:o:m:a.json"`，且前缀来自 `ports.LINK_DIR`；非法 actor 抛 `ValueError` | `AssertionError` |
| M12 | `links_of` 汇总：给定我的文件与若干对端文件，返回所有涉及我的 link（含对端单边引用我的 pending），owner 不在其中，`reclaimed` 集合内的标为 reclaimed | `AssertionError: assert {...} == {...}` |

### 3.2 `tests/daemon/links/test_controller.py`

基础：

| ID | 测什么 | 先红形状 |
| --- | --- | --- |
| C1 | 常数集中：`DEFAULTS` 的 `alpha=1, beta=3, probe_gain=1.25, abandon_factor=4, window_factor=10, min_window_samples=10, stale_rtt_factor=2, grad_streak=2`；`ControllerConstants` 是 frozen（赋值抛 `FrozenInstanceError`）；源码 AST 中这些字面量只出现在 `ControllerConstants` 定义里（`controller.py`/`policy.py` 其余位置不得出现 `1.25`、`4.0`、`10.0` 等常数字面量） | `ImportError: cannot import name 'DEFAULTS'` → 之后 `AssertionError` 列出越界字面量位置 |
| C2 | `compute_k`：`(23, 100) → 4`、`(350, 100) → 6`、`(0, 100) → 2`、`(1, 100) → 2`、`(3, 100) → 2`、`(23, 3) → 3`、`(1000, 2) → 2`；`(-1, 5)` 抛 `ValueError` | `AssertionError: assert 3 == 4`（若误用 floor） |
| C3 | 窗口：样本跨越 `10 × T_min` 时窗口外的旧样本不进 T_min/T_bar；样本不足 10 个时取全部；窗口长度以两步法求得（固定一组数据锁定 `window_ms`） | `AssertionError: assert 100 == 400` 形 |
| C4 | `mu_max` 与 `W_target`：构造窗口内某一 T_min 桶有 5 条回复、其余桶 ≤ 3 → `w_target == 5`；无回复 → `w_target is None` | `AssertionError: assert None == 5` |
| C5 | T_bar 为 EWMA（`g=0.125`），首样本为初值；手算三样本序列锁定数值（`pytest.approx`） | `AssertionError: assert 120.0 == approx(...)` |
| C6 | `period_ms(1000, 0) == 10000`；`jitter=±1` → `9000/11000`；`jitter=1.5` 抛 `ValueError` | `AssertionError` / `DID NOT RAISE` |
| C7 | Vegas 增：`diff < 1` 且 `grad <= 0` → W+1，且不超过 `ceil(1.25 × W_target)`；W_target 为 None 时不加 | `AssertionError: assert 4 == 5` |
| C8 | Vegas 减：`diff > 3` → W−1，不低于 k | `AssertionError: assert 8 == 7` |
| C9 | CDG：`grad > 0` 仅一周期不收；连续两周期 → `W = max(k, floor(W × T_min / T_bar))`，streak 清零 | `AssertionError: assert 10 == 5` |
| C10 | 未到期不推进：`now_ms < next_eval_ms` 时 `vegas_step` 原样返回（`is` 或 `==`） | `AssertionError` |
| C11 | cwnd：初值 1、下限 1；`unanswered` 超过 `2 × RTT_min` → `max(1, floor(cwnd × RTT_min / age))`；对端从 `links` 消失即从 `cwnd` 删除 | `AssertionError: assert {'b': 4} == {'b': 2}` |
| C12 | `cap_cwnd`：`{b:4, c:4}` 在 `W=4` 下得 `{b:2, c:2}`；`{b:3, c:1, d:1}` 在 `W=4` 下和 ≤ 4、每条 ≥ 1、结果确定（重复调用相同） | `AssertionError: assert 8 <= 4` |
| C13 | `evaluate` 整体：输出含 `n_active, k, w, cwnd, period_ms, node, links, diff, grad`；`w >= k`；`sum(cwnd.values()) <= w`；同输入同 jitter 两次调用输出相等（纯、确定） | `AttributeError: 'ControllerOutput' object has no attribute 'diff'` / `AssertionError` |
| C14 | `abandon_threshold_ms(250) == 1000`；`censor(0, 999, 1000) is None`；`censor(0, 1000, 1000) == 1000`；`censor(0, 5000, 1000) == 1000`（按阈值计入，不按实际时长） | `AssertionError: assert 5000 == 1000` |

§5.6 劣化条件（控制面）——每条用一个确定的合成「节点模型」在循环里驱动 `evaluate`，jitter 取固定序列：

| ID | 劣化条件 | 测什么 | 先红形状 |
| --- | --- | --- | --- |
| D1 | 服务率随负载下降 | 合成节点：W ≤ c=6 时 `T = 1000ms`、吞吐随 W 线性；W > c 时 `T = 1000 × (1 + (W−c))`、吞吐反降。驱动 60 周期。断言：每周期 W 增量 ≤ 1；只有 `grad <= 0` 的周期出现增加；W 从未超过 `ceil(1.25 × W_target)`；末 20 周期 W ∈ `[k, c + 2]`，不单调爬升 | `AssertionError: W climbed to 14 > 8` |
| D2 | 非拥塞慢被当拥塞 | dwell 在第 20 周期起从 1000ms 阶跃到 30000ms 且无排队（每个样本相同，T_bar ≈ T_min）。断言：W 始终 ≥ k；阶跃后一个新窗口（`10 × 30000ms`）内 `diff` 回到 `< alpha`，W 不再下降；阶跃前后 W 的最低值 ≥ k | `AssertionError: W fell below k: 1 < 2` 或 `diff 27.3 not < 1` |
| D3 | 后来者不公平 | link b 在 t=0 起以 RTT=100ms 积累样本；t=T1 起 b 与新 link c 都只见到 RTT=400ms（共同拥塞）。断言：经过一个窗口长度后 `links['b'].t_min_ms == links['c'].t_min_ms == 400`（b 的旧基线到期，不保留 100），且此后两条 cwnd 之差 ≤ 1 | `AssertionError: assert 100 == 400` |
| D4 | 同步振荡 | 两个初始状态与输入完全相同的控制器，jitter 序列分别为 `+0.7` 与 `−0.7`：断言下一次 `next_eval_ms` 不同、都在 `[0.9P, 1.1P]`；jitter 序列相同则两者全程一致（确定性）；`controller.py` 源码不 import `random`/`time`（与 T-B2 同机检） | `AssertionError: 10000 == 10000`（未施加抖动） |
| D5 | 审查偏差 | 10 条快回复（100ms）+ 5 条从不回复的请求（RTT_max=100 → 阈值 400）。断言：计入审查样本后 `t_bar` 严格大于只算快回复时的 `t_bar`；审查样本值恰为 400；`evaluate` 的 W 不高于不计审查样本时的 W | `AssertionError: assert 100.0 > 100.0` |
| D6 | 冷启动 | `initial_state(23)` → `w == k == 4`、无 cwnd；样本 < 10 个时连续 5 次 `evaluate` W 不变、新 link 的 cwnd 为 1、`period_ms is None`；第 10 个样本后才开始按周期调节，且首周期至多 +1 | `AssertionError: assert 5 == 4` |

依赖与纯度机检（放在 `test_controller.py`，同时覆盖三个模块）：

| ID | 测什么 | 先红形状 |
| --- | --- | --- |
| T-B1 | AST 扫 `model.py`、`controller.py`、`policy.py` 的全部 import：只允许标准库（`__future__`、`collections.abc`、`dataclasses`、`math`、`typing`、`types`、`json` 中所需者）、`hyprial.kernel`、`hyprial.daemon.impl.links.ports`、以及包内前序模块（`model ← controller ← policy` 方向）；出现 `inbox`、`orgfs`、`application`、`dispatch`、`delivery`、`composition`、`biz`、`shell` 即失败 | 文件缺失时 `FileNotFoundError`；越界时 `AssertionError: forbidden import hyprial.daemon.impl.inbox in policy.py` |
| T-B2 | 同一 AST 扫描：不 import `time`、`datetime`、`random`、`secrets`、`os`、`logging`；不出现名为 `open`/`print` 的调用 | `AssertionError: impure import 'time' in controller.py` |

### 3.3 `tests/daemon/links/test_policy.py`

测试内用小工厂（`_view(...)`）造 `NodeView`：默认 `k=2`、`W=4`、`period_ms=10000`、`cwnd` 每 link 2。

| ID | 测什么 | 先红形状 |
| --- | --- | --- |
| P1 | owner 豁免：`admit(A→OWNER)`、owner 视角 `OWNER→A`（`governed` 为假）、`A→route:lark:x` 返回 `Allow(peer=None)`，即使 `degree == k` 且 `unclosed >= W` | `AssertionError: assert Hold(...) == Allow(peer=None)` |
| P2 | owner link 不计入 k：`degree(view)` 不数 owner；有 2 条 agent link（k=2）时再发 owner 仍 Allow，发新 agent C 走淘汰/满 | `AssertionError: assert 3 == 2` |
| P3 | 其他 `user:`（`U`）按 agent 规则：无 link → `Mint(U)` | `AssertionError: assert Allow(peer=None) == Mint(peer='user:p')` |
| P4 | 有效 link 且窗口有余 → `Allow(peer=B)` | `AssertionError` |
| P5 | cwnd 满 → `Hold(LINK_WINDOW_FULL, B, "cwnd")`；W 满（`pending_inbound + outbound >= W`）→ `Hold(LINK_WINDOW_FULL, B, "W")`；Hold 构造本身经 ports 校验（reason ∈ HOLD_REASONS） | `AssertionError: assert Allow(peer='agent:o:m:b') == Hold(...)` |
| P6 | 单边已铸造等待中 → `Hold(LINK_PEER_FULL, B, "awaiting-peer")`，不重复 Mint | `AssertionError: assert Mint(...) == Hold(...)` |
| P7 | 无 link 且有空位 → `Mint(C)`；对端单边引用我（`mine=False`）时也 `Mint`（写回即成立） | `AssertionError` |
| P8 | 自己满、有可淘汰者 → `Mint(C)` 且 `select_victim` 返回最久未用者；自己满、全部有在途 → `Hold(LINK_SELF_FULL, C, "k")` | `AssertionError: assert Mint(...) == Hold(... 'k')` |
| P9 | 回复放行：`reply_to` 在 pending、requester == target、link 有效 → `Allow(peer=B)`，即使 cwnd 与 W 都满；对 owner 的请求回复 → `Allow(peer=None)` | `AssertionError: assert Hold(...) == Allow(...)` |
| P10 | 只认 reply-to（R17）：`intent="reply"` 但 `reply_to=None` 按新请求处理（cwnd 满时 Hold）；`intent="request"` 但带有效 `reply_to` 按回复放行 | `AssertionError` |
| P11 | `LINK_RECLAIMED`：请求仍 pending 但 link 已 reclaimed；以及 `reply_to in drained` —— 都抛 `DaemonRequestError`，`.code == ipc_errors.LINK_RECLAIMED`，`.data["message_id"] == reply_to` | `Failed: DID NOT RAISE <class 'DaemonRequestError'>` |
| P12 | 非 pending 非 drained 的 reply_to → `.code == MESSAGE_REPLY_UNAVAILABLE`；reply_to 的 requester ≠ target → `.code == INVALID_ARGUMENT` | `AssertionError: assert 'LINK_RECLAIMED' == 'MESSAGE_REPLY_UNAVAILABLE'` |
| P13 | `admit` 的 `sender != view.actor` 抛 `ValueError`；返回值类型 ∈ `(Allow, Mint, Hold)`（与 `ports.SendDecision` 一致，枚举全部分支） | `DID NOT RAISE` |
| P14 | `admit_dispatch`：owner 发来 → True（即使 W 满，优先级反转缓解）；link 仅 pending → False（等互引同步）；有效但 `pending_inbound >= W` → False；已注入过的 message_id → True | `AssertionError` |
| P15 | `settle_hint`：pending → `"keep"`；drained → `"ack"`；不存在 → `"ack"` | `AssertionError: assert 'keep' == 'ack'` |
| P16 | `accept_reference`：`degree < k` → True；`degree == k` → False（不为接受而淘汰）；churning → False | `AssertionError` |
| P17 | `residue(view, B)`：`drain_ack` 恰为来自 B 的 pending 入站 id（按到达时刻序）；`released_outbound` 恰为到 B 的出站 id；不碰其他对端 | `AssertionError: assert () == ('m1', 'm2')` |
| P18 | `shrink_to_k`：k 从 3 降到 2、三条中一条在途 → 返回一条最久未用且空闲者；全部在途 → 返回空元组（等下次） | `AssertionError` |
| P19 | `unclosed` 计入 owner 发来的 pending 入站（§3.2） | `AssertionError: assert 1 == 2` |

§5.6 劣化条件（策略面）：

| ID | 劣化条件 | 测什么 | 先红形状 |
| --- | --- | --- | --- |
| D7 | 队头阻塞 | 到 B 的出站请求超过 `4 × rtt_max_ms[B]` → 出现在 `abandoned(...)`；把它从 `outbound` 移除后（WP2 的动作，测试里手工模拟）对 B 的新请求从 `Hold(..."cwnd")` 变为 `Allow`；`abandoned` 的返回只是 id 元组（`all(isinstance(x, str))`），policy 模块 `__all__` 中没有任何名字含 `resend`/`retry` | `AssertionError: assert () == ('m1',)` |
| D8 | link 抖动 · 最短存活期 | 自己满，唯一空闲 link 铸造于 `now − 0.5P` → `select_victim is None` → `Hold(LINK_SELF_FULL, ..., "k")`；铸造于 `now − 1.5P` → 可淘汰 | `AssertionError: assert 'agent:o:m:b' is None` |
| D9 | link 抖动 · 抖动高时 hold 不铸造 | 上一周期内回收次数 > k（`recent_reclaims_ms`）→ 自己满时 `Hold(LINK_SELF_FULL, C, "churn")` 而不是淘汰再 Mint；抖动退去（时刻移出周期）后恢复 Mint | `AssertionError: assert Mint(...) == Hold(... 'churn')` |
| D10 | link 抖动 · LRU 与 PAC | 多个可淘汰者按 `last_used_ms` 最小选；并列按 peer 字典序；`pac_task` 生命周期的 link 即使最久未用也不选 | `AssertionError: assert 'agent:o:m:c' == 'agent:o:m:b'` |
| D11 | 分区与过期副本 | 我侧 established 的 link 因对端副本缺失降成 pending（`mine=True`）→ 新请求 `Hold(LINK_PEER_FULL)`，既不 Mint 也不抛错（只 hold 不丢）；`admit_dispatch` 对该发送方 False | `AssertionError` |
| D12 | 优先级反转 | owner 消息在 `W` 与 `k` 全满时 `admit`/`admit_dispatch` 均放行（与 P1、P14 交叉，单列一条场景测试，读起来对应 §5.6） | `AssertionError` |

覆盖对照（§5.6 → 测试）：服务率随负载下降 D1；非拥塞慢 D2；后来者不公平 D3；同步振荡 D4；队头阻塞 D7；审查偏差 D5；link 抖动 D8、D9、D10；冷启动 D6；分区与过期副本 M4、D11；优先级反转 P1、P14、D12。

## 4. 验收标准（可机检）

全部在 worktree 根目录执行，全部退出码 0：

```sh
PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links -q
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run ruff check src/hyprial/daemon/impl/links tests/daemon/links
uv run --with libcst==1.9.0 pytest tests/ -q          # 全量，确认未破坏其他测试
```

逐条：

1. §3 列出的 M1–M12、C1–C14、D1–D12、P1–P19、T-B1、T-B2 全部存在且通过（测试函数名以 ID 小写开头，例如 `test_d3_latecomer_baseline_expires`，便于验收者按 ID grep）。WP0 的 6 条仍通过。
2. `git diff --name-only refactor/org-structure-constrain...HEAD` 恰为六个文件：三个源文件、三个测试文件；`ports.py` 与 `links/__init__.py` 无改动。
3. `ls src/hyprial/daemon/impl/links | wc -l` = 5；`ls tests/daemon/links | wc -l` = 5（不含 `__pycache__`）；每文件 `wc -l` ≤ 1000。
4. `rg -n "split\(\":\"\)|\.split\(':'\)" src/hyprial/daemon/impl/links` 无输出。
5. `rg -n "^(from|import) " src/hyprial/daemon/impl/links/{model,controller,policy}.py` 的结果只含 §0 第 1 条允许的模块（T-B1 已机检，此条供人工复核）。
6. 设计常数（`1.25`、`4`、`10`、`α=1`、`β=3`）在 `controller.py` 中只出现于 `ControllerConstants`（C1 机检）。
7. 先红证据存在（见 §5）。

## 5. 要留的证据

PR 正文（到 `refactor/org-structure-constrain`）须带：

1. **先红**：只含测试的提交 sha 及其 pytest 输出尾部（收集期 `ModuleNotFoundError`）；骨架提交后的红跑摘要（每文件失败数与代表性断言）。
2. **转绿**：最终提交上 §4 五条命令的原样输出尾部（pytest 计数行、结构门 JSON 的 `violations: []` 摘要、ruff 的 `All checks passed!`）。
3. **劣化条件对照表**：§3 末的「覆盖对照」逐条附测试函数名与 PASS。
4. **读数锚点**：`compute_k(23, ·) = 4`、`compute_k(350, ·) = 6` 的测试名（与设计 §5.2 的数对上）。
5. **文件计数**：`ls` 两个目录的输出（5 与 5）。
6. 未测或无法机检的项写 UNKNOWN（例如合成节点模型与真实负载的拟合度：WP1 只证明算法按设计推进，不证明真实网络上收敛——那是上线后复测，见设计 §11）。

## 6. 对缝的建议（不在 WP1 改；供评审与 WP0 owner 裁）

1. **`LinkState` 的名字冲突**。派单里说 model 要有「LinkState 数据类」，而 ports 已把 `LinkState` 定为状态字面量。本规格选择不改 ports：状态沿用 `ports.LinkState`，节点级聚合视图叫 `policy.NodeView`，单边记录叫 `model.LinkEntry`。若确需一个名为 `LinkState` 的数据类，应在 ports 把字面量改名（如 `LinkStatus`），那是缝变更，需另裁。
2. **入站 T 的审查阈值取哪条 link 的 RTT_max**。设计 §5.1 写「4 倍该 link 的 RTT_max」，但 T 是入站停留，接收方未必在该 link 上发过请求（没有 RTT）。本规格取「该 link 有 RTT 则用之，否则 4 × 本节点窗口内 T_max，冷启动不审查」。建议在设计 §5.1 补一句定下。
3. **cwnd 的吞吐基线**。设计 §5.4 说「同一套 Vegas 规则」，未明言 cwnd 的 W_target 用什么吞吐。本规格用「该 link 上收到的回复率 × RTT_min」。WP2 喂样本时须把「本节点发出的回复」（喂 W）与「本节点在 link 上收到的回复」（喂 cwnd）分开；建议在 ports 或 WP2 规格里把这两类样本的来源事件写死。
4. **设计未给值的常数**：`jitter_ratio`（取 0.1）、`ewma_gain`（取 0.125，TCP SRTT 惯例）、`min_link_lifetime_periods`（取 1 个周期）、抖动阈值（取每周期回收数 > k）。都已集中在 `ControllerConstants`，建议评审确认后在设计 §5.5 补数值。
5. **`Mint` 不携带淘汰对象**。自己满且需淘汰时，`admit` 只返回 `Mint(peer)`，WP2 须再调 `select_victim` 取同一个 victim（纯函数，同一 view 同一结果）。建议 ports 给 `Mint` 加可选字段 `evict: str | None = None`（加默认值，向后兼容），让决策自含，免得 WP2 两次求值之间 view 漂移。
6. **拒绝没有决策类型**。`SendDecision` 只有 Allow/Mint/Hold；回复路径的 `LINK_RECLAIMED` 等拒绝本规格以 kernel 的 `DaemonRequestError` 抛出，与 `LinkGate.admit_send` 的返回类型一致。若希望拒绝也是值（便于 `--json` 统一与测试），可在 ports 加 `Refuse(code: str, peer: str | None, detail: str = "")` 并把 `SendDecision` 扩为四元——这会让每个 `match` 的消费者（WP3/WP4/WP6）多一个分支，需在其他包开工前定。
7. **PAC 绑定 link 不参与 LRU**。本规格让 PAC 绑定的 link 只随任务结束回收；若某 agent 的 k 个名额全被长任务占住，新发送会持续 `LINK_SELF_FULL`。设计未说 PAC link 是否计入 k；建议在设计 §3.1 或 §4.2 明确（计入且不可淘汰，或不计入 k）。
8. **回复到「从未存在」的 reply_to**。设计只给了 `LINK_RECLAIMED`；本规格把「不在 pending、也不在排干集合」归为既有的 `MESSAGE_REPLY_UNAVAILABLE`，避免把普通的重复回复误报为 link 回收。需要 view 携带 `drained` 集合，WP2 的 store/service 须保存「因回收而排干」的 id（建议至少保留一个放弃阈值时长）。

## 7. 评审

结论：`BLOCKING:12 ADVISORY:4`。实施时必须采用 B1–B12 的修正规格；原正文与修正规格冲突时，以修正规格为准。A1–A4 为“建议”项。

依据为设计 §3–§6、§14，当前 `ports.py`，以及本目录 README 的“缝的裁定”。README 已裁定的 PAC 豁免属于设计扩展，仍待 Allen 确认；本评审按 README 的当前实施指令检查，不把扩展写成 Allen 已确认。

### B1〔阻断〕派发缺少回复参数，只数入站，幂等顺序也不符缝

位置：§1.3 的 `admit_dispatch` 签名与语义、P14。依据：设计 §2、§4.5、§5.3；ports 的 `LinkGate.admit_dispatch`。

原规格只有 `sender/message_id/now_ms`，不能识别闭合出站事项的回复。只检查 `len(pending_inbound)` 会在 W 全被出站占满时继续注入新请求。先检查有效 link 再检查已注入 ID，也违反 ports 的“已放行直到结算一直为 True”。

**修正规格：**

- `policy.admit_dispatch` 增加 `intent: str = "request"`、`reply_to: str | None = None`，与 ports 同名参数一致。
- `NodeView` 增加 `admitted_ids: AbstractSet[str]`。集合只保存本 actor 已准入且未结算的消息 ID，包含回复；不能仅用 `pending_inbound` 代替。
- 已在 `admitted_ids` 的 ID 直接返回 True，不重复占 W。回收排干属于结算，WP2 必须同步移除该 ID。
- 新 ID 若属于 README 第 25 条的入站豁免，返回 True。其余新 ID 必须先满足本地互引有效。
- `intent == "reply"` 且 `reply_to` 匹配本节点仍 open、`peer == sender` 的出站请求时，返回 True，不受 W 约束。无 `reply_to` 或请求不匹配时，不得取得回复豁免。
- 其他新请求以 `unclosed(view) < limits.w` 准入。`unclosed` 同时数已准入未回复入站与未闭合出站，不数 inbox 中尚未注入的等待行。
- WP1 只作纯判定。WP2 在 inbox actor 的内存状态中原子记录首次准入，再处理下一条请求；不得为原子记录在 policy 中 import inbox 或执行 I/O。

**补测：**扩展 P14：`pending_inbound=0, outbound=W` 时新请求 False；同状态下有效回复 True；错误 peer/未知 `reply_to` 不取得豁免；没有 `reply_to` 的 `intent="reply"` 仍受 W；已准入 ID 在 W 满或副本暂失效时仍 True。模拟连续两次首次准入，第一条占最后名额后第二条 False。先红形状为缺参数 `TypeError` 或错误的 True/False。

### B2〔阻断〕owner 先放行绕过回复校验，副本延迟被误报回收

位置：§1.3 发送决策表次序 1、回复第三条；P9–P12、D11。依据：设计 §4.4–§4.6、§5.6；README 第 8 条。

owner 的 `Allow` 发生在检查 `reply_to` 之前，未知或已结算请求也会放行。`not effective` 可能只是分区或副本延迟，不能单凭这个条件返回 `LINK_RECLAIMED`。原规格也没有验证重新铸造后，旧请求仍不得回复。

**修正规格：**

- 校验 `sender == view.actor` 后，先处理非空 `reply_to`，再处理新请求的 owner/route 豁免。owner 豁免授权和窗口，不豁免原请求存在性及目标匹配。
- `reply_to in drained` 必须返回 `LINK_RECLAIMED`，即使当前到 target 的 link 已重新成立。WP2 按 README 第 8 条保留排干记录至消息 TTL，不采用正文 §6 第 8 条的更短保留期。
- 请求不在 pending 或 drained 时，返回 `MESSAGE_REPLY_UNAVAILABLE`。pending 请求的 requester 与 target 不符时，返回 `INVALID_ARGUMENT`。
- pending 请求属于 owner 对，或对应 link 有效时，允许回复，不占 cwnd、不增 W。
- 明确处于 `reclaimed` 的 link 返回 `LINK_RECLAIMED`。仅因文件缺失或互引未同步而无有效 link 时，回复保持 outbox hold；按 B3 选择等待原因，不铸造、不淘汰、不排干。
- 不带 `reply_to` 的消息仍是新请求。允许回复后的闭合与计数释放由 WP2 执行，不能暗中把新请求改成回复。

**补测：**对 owner 的未知 ID、已 ack ID、错误 target；副本缺失时回复 Hold 而非异常；恢复互引后回复 Allow；回收后重铸 link 再回旧 ID 仍为 `LINK_RECLAIMED`；普通 send 不闭合原请求。先红形状为 owner 错误 Allow 或副本延迟错误的 `LINK_RECLAIMED`。

### B3〔阻断〕Mint 丢失淘汰对象，等待码落后于 ports

位置：§1.3 决策表次序 4–5、§2、P6/P8、D11、§6 第 5 条。依据：设计 §4.2–§4.3、§9；README 第 5、14 条；ports 的 `Mint.evict`、`HOLD_REASONS`。

当前 ports 已有 `Mint.evict` 与 `LINK_MINTING`。正文仍让 WP2 二次选 victim，并把所有单边等待都报 `LINK_PEER_FULL`。仅见单边引用不能证明接收方满。

**修正规格：**

- 需要淘汰时返回 `Mint(peer=target, evict=victim)`。不需要淘汰时 `evict=None`。WP2 只执行决策携带的 victim，不再选择。
- `NodeView` 增加 `peer_full: AbstractSet[str]`，由 WP2 提供当前已知的对端容量事实；policy 不查询其他节点。
- 已有我侧引用而缺少对端回引时，不重复 Mint。target 在 `peer_full` 时返回 `Hold(LINK_PEER_FULL, target, "awaiting-peer")`；否则返回 `Hold(LINK_MINTING, target, "awaiting-peer")`。
- 文件暂缺导致的等待同样使用上述分类。不得因为 pending 就断言对端满。
- `Hold.reason` 必须取当前 ports 的 4 个 `HOLD_REASONS`，不得在 WP1 重定义集合。`Mint` 对应 send 的 `holdReason=LINK_MINTING`，序列化由 WP2/发送入口完成。

**补测：**P8 断言 `Mint.evict` 恰为选中的 LRU；P6 分别覆盖未知对端容量与已知满；D11 改断言 `LINK_MINTING`；覆盖 4 个 hold 原因。先红形状为 `evict is None` 或错误的 `LINK_PEER_FULL`。

### B4〔阻断〕PAC 绑定 link 仍占 k，与已裁定实施规则冲突

位置：§1.3 `degree`、`accept_reference`、`shrink_to_k`；P2/P16/P18、D10。依据：README 第 7 条（设计 §3.2 的待确认扩展）。

正文只免除 PAC 的 LRU 淘汰，却仍在 `degree` 中数 PAC。长任务因此耗尽所有常规名额。

**修正规格：**

- `degree` 只数我侧已引用、非 reclaimed、`entry.lifecycle == "regular"` 的非 owner link，包含常规 pending 引用。
- PAC 任务绑定 link 不计入 k，不参加常规 LRU 或 `shrink_to_k`。其出站在途仍占对应 cwnd，其入站与出站未闭合事项仍占 W。
- `accept_reference` 增加 `lifecycle: Lifecycle = "regular"` 参数。常规新引用须有 k 空位；PAC 引用不使用常规 k 名额。任务绑定事实由 WP2 提供，policy 不 import PAC 或 biz。
- PAC 任务结束时仍通过 `residue` 回收排干，不因 k 豁免取得回复、cwnd 或 W 豁免。

**补测：**常规 2 条加 PAC 3 条时 degree 为 2；只有 PAC 时常规新引用仍有空位；常规满时 PAC 引用不报自己满；PAC 在途达到 cwnd/W 仍 Hold；任务结束释放出站并排干入站。先红形状为 degree 变成 5 或 PAC 请求错误 Allow。

### B5〔阻断〕把 cwnd 上限之和压到 W，约束对象错误且算法无解

位置：§1.2 `cap_cwnd` 与 evaluate、C12/C13。依据：设计 §2、§5.4。

设计约束的是“实际未闭合出站之和不超过 W”。正文改为 `sum(cwnd) <= W`，会把空闲 link 的未来窗口也当成当前占用。若有 5 条 PAC link、W=2，则“每条 cwnd≥1”与“和≤2”不能同时满足。

**修正规格：**

- 删除 `cap_cwnd` 的全局比例裁剪及 C12/C13 的 `sum(cwnd) <= W` 验收要求。WP1 新模块尚未发布，不需要保留这个错误的新函数。
- 每条 cwnd 独立按本 link 样本推进，保持下限 1。空闲 link 的 cwnd 不占 W。
- 实际总量由 policy 的 `unclosed(view)` 与每 peer 的实际在途数准入。普通新请求必须同时满足 `inflight(peer) < cwnd[peer]` 与 `unclosed(view) < W`。
- 计数仅在新请求真正放行后增加。outbox hold 不算已发出请求；回复按 B1/B2 取得豁免。

**补测：**5 条 PAC link、W=2、无在途时 evaluate 正常结束，每条 cwnd≥1；多个 link 的 cwnd 之和大于 W 时仍能使用未占用的 W 名额；放行 2 个请求后第 3 个 Hold。先红形状为比例裁剪无法满足下限、错误地改小独立 cwnd，或无限减量循环。

### B6〔阻断〕同一 replies 输入混用发出与收到的回复

位置：§1.2 `ReplySample`、`ControllerInput.replies`、`rtt_window_stats` 和 cwnd 算法；C4、D3。依据：设计 §5.1、§5.4；README 第 3 条。

`ReplySample` 注释定义为本节点发出的回复，cwnd 段又把 `peer` 非空解释成本节点收到的回复。当前输入形状没有方向字段，W 会误用收到的回复提高吞吐基线。

**修正规格：**

- `ReplySample` 增加 `direction: Literal["sent", "received"] = "sent"`，保留 `peer`。`received` 时 peer 必须为合法非 owner 对端。
- `window_stats` 的吞吐仅数 `direction == "sent"` 的回复。
- `rtt_window_stats(peer)` 的吞吐仅数 `direction == "received" and sample.peer == peer` 的回复。
- DwellSample 只代表本节点入站到显式回复的 T。RttSample 只代表本节点已发出请求到收到匹配回复的 RTT。
- 审查样本参加时长统计，但不得凭空生成 ReplySample 增加吞吐。

**补测：**同一 at_ms 混入 sent/received 及两个 peer，分别手算节点和各 link 的 `mu_max/w_target`；只增加收到的回复时节点 W_target 不变；只增加发出的回复时 cwnd 的吞吐基线不变。先红形状为类型缺 direction 或吞吐互相污染。

### B7〔阻断〕缺少 hold 触发的 cwnd 收缩，重复 tick 会重复收缩

位置：§1.2 `ControllerInput`、evaluate 的 unanswered 段；C10/C11。依据：设计 §5.4–§5.5。

输入没有 hold 信号，无法按 hold 收缩 cwnd。超时收缩在周期判定之外，每次重评都可能再乘一次比例，结果依赖 runtime tick 频率。回收也只有删除状态，没有测试请求释放与旧事件不能复活状态。

**修正规格：**

- `ControllerInput` 增加 `held_peers: AbstractSet[str]`。WP2 把仍存在容量 hold 的 peer 放入集合；不能把未知互引的等待当作 RTT 样本。
- 到达该 link 的评估周期时，有 hold 或最老在途请求年龄严格大于 `stale_rtt_factor × RTT_min`，执行一次 `shrink_proportional`，不执行同周期的增窗分支。
- 收缩的 base 为 RTT_min。仅 stale 时，observed 为 `max(RTT_min, RTT_bar, oldest_age)`。hold 时 observed 至少为 `RTT_min × probe_gain`，并取 RTT_bar/oldest_age 中的较大值，使 cwnd>1 时确实收缩；这是控制比例，不记录为 RTT 样本。无最老请求时省略 oldest_age。无 RTT 样本时保持初值/下限 1，不制造时长样本。
- 同一周期只收缩一次。未到 `next_eval_ms` 的重复 evaluate 不再次收缩；跨过下一周期且压力仍在时可再收缩。
- 回收时删除该 peer 的 cwnd 状态；由 B11/`residue` 释放实际出站占用。仍输入旧 RTT 或 hold 事件，不能为不在 `inp.links` 的 peer 恢复状态。

**补测：**只有 hold、没有 stale 请求时 cwnd 收缩；年龄恰为 `2×RTT_min` 不触发，超过才触发；同周期重复 100 个 tick 与 1 个 tick 得同一状态；下一周期压力仍在可再收；回收后旧样本不复活 link。先红形状为输入缺字段、hold 不收缩或重复 tick 继续减窗。

### B8〔阻断〕最近 10 个样本兜底可无限保留已过期基线

位置：§1.2 窗口段、C3、D2/D3/D6。依据：设计 §5.3、§5.6 的基线到期重测与后来者不公平缓解。

正文同时要求“至少保留最近 10 个”和“窗口外样本不参与统计”。稀疏回复时两者冲突，旧 100ms 样本能在很久以后继续压低后来 400ms 样本的基线。

**修正规格：**

- 两步法可用最近最多 `min_window_samples` 个非未来样本估算初始 T_min，确定时间窗口；随后必须按时间窗口筛选统计样本。
- 过期样本不得为了凑满 10 个而重新加入 T_min、T_max、T_bar 或吞吐统计。`min_window_samples` 是控制器开始调节的样本数下限。
- 筛选后不足 10 个时，不执行 Vegas/CDG 增减，继续收集有效样本；W 仍受 k 下限约束。压力收缩按 B7 处理。
- 窗口内重算的所有统计必须使用同一组筛选结果。到期重测不保留窗口外的历史最小值。

**补测：**9 个很旧的 100ms 样本加 1 个当前 400ms 样本，断言旧值不进入统计且不足 10 个不探测增窗；后来补到 10 个有效 400ms 样本时恢复调节；节点与每条 link 都覆盖该形状。先红形状为 T_min 仍是 100ms 或过期样本令 n 达到 10。

### B9〔阻断〕放弃无 RTT 请求缺少回退，入站审查也没有可执行释放规格

位置：§1.2 放弃段、§1.3 `abandoned`、D5/D7。依据：设计 §4.5、§5.1、§5.6；README 第 2 条。

`abandoned` 写死“无 RTT 不放弃”，已有本节点 T_max 时仍会永久占位。入站只给 `censor` 数值帮助函数，没有规定哪些 ID 释放，以及同一请求是否会反复成为审查样本。

**修正规格：**

- 增加纯函数 `request_threshold_ms(view, peer) -> int | None`。优先取 `abandon_factor × rtt_max_ms[peer]`；该 link 无 RTT 时取 `abandon_factor × limits.node.t_max_ms`。两者都无时返回 None，不制造冷启动时长常数。
- `abandoned` 使用该阈值检查出站，达到阈值即返回 ID，不重发。
- 增加纯函数 `censored_inbound(view, *, now_ms) -> Mapping[str, int]`，返回达到阈值的 pending 入站 ID 及阈值样本值。
- WP2 对每个返回的入站 ID 释放 W、结算放弃状态，并只记录 1 个 `censored=True` 的 DwellSample。样本值取阈值，不取实际等待时长；不增加回复吞吐。
- WP2 对出站 ID 释放 cwnd/W 占用。已释放 ID 的迟到回复不能再次扣计数或闭合新的事项。
- owner 入站使用本节点 T_max 回退。两类基线都不存在的冷启动请求暂不审查，必须作为真实进展限制记录，不能标为已解决。

**补测：**无 link RTT、有节点 T_max 时入站与出站均能到期释放；恰在阈值与阈值前 1ms；两类基线都无时返回空；同 ID 多次扫描只计 1 个审查样本；迟到回复不使计数为负、不自动重发。先红形状为无 RTT 请求永不进入 abandoned 或审查样本重复增加。

### B10〔阻断〕有 k 空位时仍铸造，绕过高抖动 hold

位置：§1.3 决策表次序 5、`accept_reference`、D9。依据：设计 §5.6；README 第 4 条。

`degree < k` 比 `churning` 先判断，刚回收出空位时仍会 Mint。D9 只测自己满，漏掉最容易反复铸造的有空位场景。`accept_reference` 也没有区分新回引与已有引用的幂等重评。

**修正规格：**

- 所有需要新写常规引用的分支，先检查 `churning(view, now_ms)`。高抖动时返回 `Hold(LINK_SELF_FULL, target, "churn")`，无论 degree 是否小于 k，不选择 victim、不 Mint。
- 已有效 link、合法回复、owner/route 豁免继续按原准入处理，不因抖动暂停全部通信。
- `accept_reference` 对已经存在的我侧引用返回 True，不增加 degree、不重复写入。只有需要新写的常规引用才检查抖动和 k。
- PAC 任务绑定新引用按 B4 的任务授权处理，不把常规自动铸造扩展成 PAC 豁免。

**补测：**高抖动且 degree=0、degree<k、degree=k，普通新目标全部 Hold；抖动退去恢复 Mint；已有有效 link 仍可发送；已有我侧引用的回引重评不因满额拒绝；`recent_reclaims` 的窗口端点及次数等于 k/大于 k。先红形状为有空位时返回 Mint。

### B11〔阻断〕k 收缩沿用只淘汰空闲的规则，不能满足拓扑上限

位置：§1.3 `shrink_to_k`、P18。依据：设计 §2、§4.2、§4.4。

“自己满、尝试新铸造”必须等在途请求排干。但 k 已收缩属于回收事件；正文把两者合并，全部在途时永不收回多余 link。设计 §4.4 明确规定回收后清零出站和排干入站。

**修正规格：**

- `select_victim` 保留普通自动铸造的限制：无在途、已过最短存活期；无候选时 Hold。
- `shrink_to_k` 单独处理硬上限收缩。按 B4 计算常规 degree，并确定返回 `max(0, degree-k)` 个常规我侧引用。
- 收缩优先选择空闲 link，再选择仍在途 link；组内按 `(last_used_ms, peer)` 排序。为恢复硬上限，k 收缩可回收未过最短存活期的常规 link，不回收 PAC link。
- WP2 执行每个回收决策时调用 `residue`：出站释放，入站排干 ack 并记录 drained。之后仍回旧请求必须得到 `LINK_RECLAIMED`。

**补测：**全部常规 link 在途且 k 从 3 降到 2，仍返回 1 个回收对象；模拟 residue 后 degree≤k、出站清零、入站 ack；年轻 link 与 PAC 同时存在时只回收常规；普通铸造在全部在途时仍 Hold。替换 P18 的“全部在途返回空”断言。先红形状为 shrink_to_k 返回空或回收未排干。

### B12〔阻断〕缺少未加入 org 的发送边界，入站与出站 user 规则混用

位置：§1.3 `NodeView`、`governed`、admit/accept_reference；P1/P3/P14。依据：设计 §4.1、§6；README 第 25 条与 ports 的调用点豁免约定。

空 links 无法区分“已加入 org、可铸造”与“未加入 org、能力为零”。现规格对两者都返回 Mint。`governed` 又没有明确入站其他 user、自发自收及内部来源的豁免，易让两个方向共用错误分类。

**修正规格：**

- `NodeView` 增加必填 `org_available: bool`，由 WP2 根据本节点 org 状态提供。policy 不 import orgfs。
- 已加入 org 且有空位时才允许普通自动 Mint。未加入 org 时，受 link 约束的目标不得 Allow 或 Mint，返回 `Hold(LINK_SELF_FULL, target, "org-unavailable")`，等待本节点加入；相应扩展 detail 词表，不新增错误码。
- 自己的 owner 与 route 沿用发送豁免。route 的 channel 能力仍由既有入口检查；policy 的 Allow 不能取代能力检查。
- 出站 agent→其他 `user:` 按 link 规则处理；入站非 owner `user:` 按 README 第 25 条豁免，不能要求人提供 link 文件。两方向必须分开表达，不得只凭对称 `governed` 复用判断。
- 自发自收、adapter/channel/system 与 daemon 内部来源按 ports/README 由入站调用点筛选；纯 policy 不为这些来源铸造文件。被管理的 native 与 hosted agent 规则一致，不增加 native 豁免。
- `sender_known` 仍由 WP2 从内存副本返回文件存在性。WP1 不查询文件，也不把“文件存在”改成“有效互引”。

**补测：**未加入 org 的新 agent/其他 user 目标 Hold，自己的 owner 和 route 不受影响；加入后可 Mint；入站其他 user 与出站其他 user 的判断不同；self-send 不 Mint；native/hosted 在相同 view 下决策相同。先红形状为无 org 返回 Mint 或入站 user 被 link 拦截。

### A1〔“建议”〕补不可变容器与非法数据边界测试

§0 已要求不可变数据，但 frozen 数据类中的 dict/list 仍可修改。宜对全部 Mapping 复制后包只读映射，对 Sequence/Set 转 tuple/frozenset。补测试：修改调用方原始容器不能改变旧 view/state；推进函数不能修改旧对象。model 文档解码宜补 self-peer、owner 条目、未知 lifecycle、peer 键与 entry.peer 不一致、负 revision，以及 bool 冒充 int 的拒绝测试。controller 宜补零/负时长、非法平滑增益与未来样本边界。

### A2〔“建议”〕合成场景不得额外宣称收敛保证

D1 的“每时刻不超过新 W_target 上限”和 D3 的“到期后 cwnd 差≤1”不是设计给出的全局不变量。不同历史状态、样本率与正梯度可让差值保持多个周期。宜锁定设计保证：增窗分支最多 +1、满足 grad 条件、增窗探测受限、基线过期、每条执行同一规则。若保留稳定区间断言，必须固定样本生成器和观察期。结果只作为该合成场景的实测，不能宣称真实网络已收敛。

### A3〔“建议”〕更新验收命令与历史缝裁定

WP0 当前有 7 条测试，§4 的“6 条”已过时。分支 diff 已包含本规格，不能要求总 diff 恰为 6 个代码文件；宜分别核对新增代码与文档。目录计数用结构门或只计直接文件的 find，不能把缓存目录算入。当前环境无 rg，可用 grep/AST 检查。§6 的“对缝的建议”已由 README 裁定，宜保留为历史并引用裁定，不再要求新增 Refuse 或改 ports。当前派单要求只本地提交、不得 push/开 PR；证据必须写仓库文件和 workflow output，不能只存在于 PR 正文。

### A4〔“建议”〕明确无基线冷启动的进展限制

§1.3 在 `period_ms is None` 时不淘汰任何 link；B9 在 T/RTT 基线都无时也不能计算放弃阈值。如果首次 k 条 link 从未回复，节点可能一直无法替换目标。设计没有给无基线的绝对时长常数，本评审不自行添加。宜把该组合加入确定性测试并记录 BLOCKED/UNKNOWN 的进展结论；若要设置冷启动存活期或放弃上限，必须先裁定，再集中进 `ControllerConstants`。

### 评审验证记录

- 2026-10-14：仅追加本节，未改产品或测试文件。依赖规格未发现跨域越层 import；T-B1 必须继续守住 kernel 公共面及包内单向依赖。B1/B4/B9 的修正不允许 links import inbox、identity impl、biz 或 shell。
- PASS：`PYTHONPATH=$PWD/src uv run --extra test pytest tests/daemon/links/test_wp0_seams.py -q`，7 passed，2 条第三方弃用 warning。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py`，退出码 0。
- PASS：`uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py`，退出码 0；这是静态检查，不证明运行行为。
- N/A：`uv run --with ruff ruff check --force-exclude --exclude '*.md' docs/design/send-links-impl/wp1.md`，退出码 0；提示没有 Python 文件，未作 Python lint 覆盖。
- UNKNOWN：WP1 新增模型、控制器与策略测试尚未实施；本次未运行全量测试或真实网络 E2E，未验证运行行为。
