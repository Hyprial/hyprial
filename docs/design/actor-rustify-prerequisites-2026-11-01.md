# actor-rustify 前置工作规划：ActorRuntime 去持有、引擎端口与黑盒一致性套件

状态：规划稿（2026-11-01），未实施，待 Allen 裁定 §6 决策项。
代码基线：`internal/dev` @ `6c38f71f9b`。未经验证的内容一律标 **UNKNOWN**。

来源（Allen 飞书原话，2026-11-01）：「请规划完成 actor-rustify 的所有前置工作，将 actor-runtime 的持有全部去除。目标是，一个独立开发者开始进行 actor-rustify 的开发，他不需要了解任何 Python 业务侧的逻辑，只要按照要求暴露 pyo3 接口」

## 0. 结论

1. **持有点是 60 处私有构造，不是组合根。** 引擎外有 80 个文件命中 `ActorRuntime|actor_runtime`。真正的持有是 60 个文件里各 1 处 `ActorRuntime()`，与 `ActorSpec(` 一一对应：每个业务 owner 自建一个 runtime，只托管一个 actor。组合根 `daemon/impl/application/wiring/services.py` 命中的是 PAC 的同名物 `DaemonActorRuntime`，并不持有引擎。⇒ 去持有 = 把 60 处私有构造改成由组合根注入能力。
2. **目标边界两层。**
   - `ActorEngine` 端口：句柄式，只有引擎实现（现在 pykka，将来 PyO3）。Rust 开发者只对着更窄的**原生 ABI**（§2.4）写，旁边有纯 Python 参考实现可对照。
   - 业务能力层：`ActorSpawner.spawn(spec) -> ActorCell`，`ActorCell` 提供 tell/ask/snapshot/stop/reset/drain。业务永远拿不到 runtime 或引擎。
3. **验收是黑盒一致性套件。** 放在仓库顶层 `conformance/actor_engine/`（不放 `tests/`：根 conftest 会 import 整个 daemon）。约 50 个用例，按后端参数化 pykka / reference-abi / native，一条命令运行，不依赖 daemon。
4. **“Rust 开发者可以开工”的判据**见 §5.3：端口与 ABI 冻结、套件在 pykka 与参考 ABI 上全绿、持有门禁零豁免、后端选择缝就位（缺 native 时 fail-closed）、CI-I 落地、intg 与新基线同步。
5. **最大风险是与既有裁定冲突。** G0 附录 C 第 2/3 条与 OTP 增补 §1.6.3/§5 规定“端口不回调 Python、kernel 公共面不出现 Callable”；OTP §9 T1 规定“监督只用 Ractor 原生能力”。“独立开发者不需要了解 Python 业务、只暴露 PyO3 接口”实际要求 Rust 引擎回调 Python handler，并完整复刻现有 guardian 的监督语义。需 Allen 以本文取代上述条款（D1/D2），否则本规划不成立。
6. **工作量**：13 个 PR 批次，约 33.5 实施人日；关键路径约 11.5 天；3–4 人并行约 3 周日历（不含评审与裁定等待）。

## 1. 现状（dev@6c38f71f9b）

### 1.1 80 个文件按主类别（每文件计一次）

| 类别 | 判定 | 文件数 | 说明 |
|---|---|---|---|
| (a) 自建 runtime 并 spawn | 含 `ActorRuntime(` 且有 `ActorSpec(` | **60** | 每文件 1 处；18 个已有 `runtime: ActorRuntime \| None = None` 注入参数（仅测试用），42 个硬编码 |
| (b) 透传/持有 runtime 对象 | 字段或属性类型为 `ActorRuntime` | 4 | 均在 orgfs：`composition/vocabulary.py`、`document/model.py`、`docs.py`、`orgfs/runtime.py` |
| (c) 只用 effects | import `actor_runtime.effects` | 1 | `kernel/impl/hooks/bus.py` |
| (d)(e) 只用 scheduler / 监督事件 | — | 0 | 均附着在 (a) 文件上 |
| (f) 类型、文档或重名 | 再导出、注释、PAC 同名物 | 15 | 再导出 2、注释 3、**PAC 重名 10**（`pac/actors/coordinator/types.py` 自定义了名为 `ActorRuntime` 的 Protocol 及其使用方） |

### 1.2 能力维度

| 能力 | 规模 | 备注 |
|---|---|---|
| `tell(` | 141 处 | 无 ask；42 个 owner 用 `threading.Event`/Future 手写 call/reply |
| `start(` / `stop(` | 各 57 处 | — |
| `snapshot(` | 38 处 | 依赖 `generation`/`ActorState`；最重：`inbox/actor/coordinator.py`、`session_actor/actor.py`、`identity/impl/agents/actor/_core.py` |
| `drain(` | 12 处 | 现在 drain 的是“私有 runtime 里唯一的 actor”；共享引擎后须按 cell drain |
| runtime 级 `event_sink=` | 10 个文件 | org/mesh、orgfs×6、turn_runtime、transport/query_actor、kernel log writer |
| `undelivered_sink` | 13 个文件 | 未开始命令的托管归还 |
| `ExpectedActorError` | 4 个文件 | — |
| `EffectLane(` | 48 处 / 37 个文件 | orgfs 9、identity 3、processes 3、dispatch 3… |
| `GenerationScheduler(` | 7 处 | application、biz/usage_actor、autoupdate、home_lease_actor、forwarding/supervisor、casdoor/sync、transport/session_actor |
| 测试中构造 `ActorRuntime(` | 24 个测试文件 | 随对应批次迁移 |

### 1.3 按域（类别 a）

orgfs 9 · dispatch 6 · adapters(lark) 5 · transport 3 · squire 3 · processes 3 · lifecycle 3 · harnesses 3 · biz 3 · identity 2 · transfer 2 · org 2 · operations 2 · inbox 2 · forwarding 2 · kernel(log) 1 · state_persistence / session_actor / runtime / pac / network / ipc / correlation / configuration / autoupdate 各 1。

最难迁：orgfs（9 个 runtime、4 个文件互传 runtime、9 条 lane、6 个事件接收器）；`identity/impl/agents/actor/_core.py`（每 agent 一个 actor）；inbox coordinator 与 session_actor（重度依赖 generation/状态/DrainReport）；`turn_runtime.py`（事件接收器内有业务逻辑）；`configuration/home_lease_actor.py`。

### 1.4 关键事实

- 引擎 = `ActorGuardian`（监督/代际/托管）+ `ActorBackend`（pykka：mailbox 闸门、线程）+ `GenerationScheduler`（重启计时）；`ActorRuntime` 只是薄门面。
- handler 返回值今天被忽略；命令对象含 `threading.Event` 与任意 `object`，**不可序列化**。
- `ActorSpec.handler_factory` 每次（重新）启动时调用；业务 factory 普遍返回同一绑定方法，owner 状态跨重启保留。
- CPU 计量（`cost_counters`）与默认日志接收器在 Python 侧，留在适配层，不进 Rust。
- kernel 包无对上层的顶层 import，port 模块可以不加载业务独立使用。
- 分支：`intg/actor-rustify` 领先 dev 17（全 docs）、落后 1129，dev 合入**无冲突**（merge-tree 实测）。`ci/rustify-ci-i` 领先 25，合入时 `.forgejo/workflows/tests.yml` 冲突。`release/v0.5.5.3` 尚未合入 dev（不涉及 actor 引擎），“0.5.5.3 之后的 dev”基线**尚未形成**。

## 2. 目标边界

### 2.1 分层

```text
业务模块 ──(构造参数注入)── ActorSpawner ──spawn──▶ ActorCell (tell/ask/snapshot/stop/reset/drain)
                                   ▲
组合根(daemon/CLI/worker 入口) 持有 ActorHost = Spawner + drain_all/shutdown + timers/effect_lane 工厂
                                   │
                         ActorEngine 端口（仅 kernel/impl/actor_runtime 内）
                 ┌─────────────────┼──────────────────────────┐
           PykkaEngine        NativeAbiEngine(Python 适配)
        (guardian+pykka)          │ 原生 ABI (§2.4)
                         ┌────────┴─────────┐
              reference_abi.py(纯 Python)   hyprial._core.actors (PyO3，Rust 开发者交付)
```

### 2.2 业务能力层（kernel 公共面）

| 类型 | 成员 | 语义 |
|---|---|---|
| `ActorSpec` | name、handler_factory、mailbox_capacity=128、supervision_profile、undelivered_sink、**新增 `event_sink`** | 共享引擎后 runtime 级事件接收器下沉到 spec |
| `ActorSpawner`(Protocol) | `spawn(spec) -> ActorCell` | 未知 profile → ValueError；draining → RuntimeError |
| `ActorCell` | `handle`、`tell`、`ask(cmd,*,timeout)`、`snapshot`、`stop(timeout=1.0)`、`reset`、`drain(timeout=5.0)` | 不新增任何队列（遵守“不新建 mailbox”） |
| `ActorHost`（仅组合根） | Spawner + `drain_all`、`shutdown`、`timers()`、`effect_lane(...)` | 只有门禁 allowlist 的文件可构造（`create_actor_host(config)`） |

沿用数据类型：`ActorHandle`、`ActorSnapshot`、`ActorState`、`ActorEvent`、`ActorEventKind`、`AdmissionResult`、`DrainReport`、`ExpectedActorError`、`SupervisionPolicy`/`DEFAULT_POLICIES`。

### 2.3 `ActorEngine` 端口

```python
class ActorEngine(Protocol):
    def spawn(self, spec: ActorSpec) -> ActorHandle: ...
    def tell(self, handle: ActorHandle, command: object) -> AdmissionResult: ...
    def ask(self, handle: ActorHandle, command: object, timeout: float) -> object: ...
    def snapshot(self, handle: ActorHandle) -> ActorSnapshot: ...
    def stop(self, handle: ActorHandle, timeout: float) -> bool: ...
    def reset(self, handle: ActorHandle) -> bool: ...
    def drain(self, handles: Sequence[ActorHandle] | None, timeout: float) -> DrainReport: ...
    def shutdown(self, timeout: float) -> bool: ...
```

必须保持的语义（全部进一致性套件）：

- **准入**：只按 queued 计容量，in_flight 不计；queued≥capacity → OVERLOADED；未知/伪造句柄、非 RUNNING、draining → CLOSED（不抛异常）。
- **顺序**：同一 actor 严格 FIFO，任一时刻最多一个 handler 执行；跨 actor 不承诺顺序。
- **`ExpectedActorError`** → COMMAND_REJECTED(code, detail)，actor 继续运行，generation 不变。
- **其他异常** → CHILD_FAILED，丢弃该代 queued，未开始命令按准入顺序交 `undelivered_sink(cmd, "ACTOR_RESTARTED_UNDELIVERED")`；窗口内失败 > max_restarts → QUARANTINED（原因码 `ACTOR_QUARANTINED_UNDELIVERED`），否则 generation+1 → RESTART_PENDING → 延迟到期重新调用 factory → CHILD_RESTARTED；factory 抛异常也消耗预算。
- **代际围栏**：generation 或状态不匹配的迟到计时器/失败回报丢弃。
- **stop**：generation+1、STOPPED、取消待重启；确认停止后才归还 `ACTOR_STOPPED_UNDELIVERED`；超时返回 False 并保留端点可重试；重复 stop 只发一次后端停止。
- **reset**：仅 QUARANTINED 时有效。
- **drain**：关准入 → 硬截止内等 load=(0,0) → 停空闲 actor；返回 `DrainReport(complete, elapsed, remaining)`，可重试。
- 事件/undelivered 接收器的异常一律吞掉。
- **ask（新增）**：返回 handler 返回值；超时抛 `TimeoutError`（命令仍在托管中可能继续执行）；准入失败抛 `ActorAdmissionError`；`ExpectedActorError` 原样抛出；其他失败抛 `ActorFailedError`（D4）。

### 2.4 原生 ABI：Rust 开发者唯一要实现的面

```text
hyprial._core.actors.Engine(*, policies: dict[str, (max_restarts:int, window:float, base:float, max:float, jitter:float)],
                            jitter_seed: int | None, event_sink: Callable[[tuple], None],
                            expected_error_type: type)
  .spawn(name, handler_factory, capacity:int, profile:str, undelivered_sink|None, event_sink|None) -> actor_id:str
  .tell(actor_id, name, command) -> int        # 0 accepted / 1 overloaded / 2 closed
  .ask(actor_id, name, command, timeout:float) -> object
  .snapshot(actor_id, name) -> (state:str, generation:int, queued:int, in_flight:int, failures:int)  # 未知 → KeyError
  .stop(actor_id, name, timeout) -> bool ; .reset(actor_id, name) -> bool
  .drain(actor_ids: list[str] | None, timeout) -> (complete:bool, elapsed:float, remaining:list[str])
  .shutdown(timeout) -> bool
event tuple = (kind, actor_id, name, generation, command_type|None, code|None, detail|None, restart_delay|None)
```

| 议题 | 规定 |
|---|---|
| 消息 | 不透明 Python 对象（`Py<PyAny>`），引擎不检查、不序列化 |
| 回调 | handler_factory、handler、event_sink、undelivered_sink 由引擎线程在持 GIL、**不持任何引擎内部锁**时调用 |
| 线程与 GIL | 支持 handler 内 tell 重入；tell 非阻塞；stop/drain/ask 等待时释放 GIL；一个 handler 阻塞不得拖延其他 actor；只支持标准 GIL 的 CPython，free-threaded 构建明确报错 |
| 错误映射 | `isinstance(e, expected_error_type)` 判预期错误；非法规格/未知 profile → ValueError；draining 时 spawn → RuntimeError；ask 超时 → TimeoutError；Rust panic 不越过 FFI，转 RuntimeError |
| 截止时间 | 相对秒数、单调时钟；≤0 只检查一次；测试容差 ±50ms |
| 监督数值 | 由 policies 数据驱动；jitter_ratio=0 时延迟精确等于名义值 |
| 进程退出 | 适配层经 atexit 调 `shutdown`；finalize 后引擎线程不得再取 GIL |
| 不进 Rust | CPU 计量、默认日志接收器、ActorCell/Host、EffectLane、业务计时器（v1，见 D3） |

## 3. 去持有与门禁

- **定义**：只有 `src/hyprial/kernel/impl/actor_runtime/**` 与显式列名的组合根文件可以 import/构造 `ActorRuntime`、`ActorEngine`、`ActorGuardian`、`create_actor_host`、`create_engine`。组合根候选：`daemon/impl/application/wiring/*`、`daemon/impl/composition/domains.py`、CLI/MCP/worker 进程入口、`kernel/impl/log/__init__.py`。哪些进程实例化这 60 个 owner：**UNKNOWN**，P1 盘点。业务只经构造参数拿 `ActorSpawner`。
- **门禁（P4）**：`tests/support/gates/actor_boundary_gate.py` 新增 AST 规则 `engine-holding`；清单 `tests/actor_engine_holding_exemptions.json`（shrinking-only，初始 64 条）；测试内手抄独立 recall 名单防扫描器失明；PAC Protocol `ActorRuntime` 改名 `PacWorkerRuntime` 消除重名。
- **收尾（P8）**：清单归零后删除，`hyprial.kernel` 不再导出 `ActorRuntime`/`GenerationScheduler`。
- 现有 3 条 legacy 豁免（AgentRegistry/DaemonApplication/InboxService）针对锁式 owner，与引擎持有无关，建议不纳入（D10）。

## 4. 一致性套件

- 目录 `conformance/actor_engine/`，自带 conftest/pytest.ini，`--actor-backend=pykka|reference-abi|native`。只允许 import port/contracts/policies；含 import 闭包用例，断言不加载 `hyprial.(daemon|identity|biz|shell)`。
- Rust 开发者运行（拟定，取决于 D8）：`maturin develop -m crates/hyprial-py/Cargo.toml && uv run --python 3.12 pytest conformance/actor_engine --actor-backend=native -q`。
- CI：pykka 与 reference-abi 为必过 job；native job 在 intg 上由 CI-I 规则新增；缺 `_core` 时 fail-closed，不得 skip 成绿。计时用例极小 backoff、jitter_ratio=0、连跑 3 次。

| 组 | 用例 |
|---|---|
| ADM 准入 | 接纳执行；queued 满 → OVERLOADED（in_flight 不计）；stop 后/未知/伪造 → CLOSED；RESTART_PENDING/QUARANTINED → CLOSED；非法 spec → ValueError |
| ORD 顺序 | 1 万条 FIFO；单 actor 无并发；多生产者各自有序；handler 内 tell 不死锁 |
| LIV/GIL | 一个 handler 阻塞 2s 其他推进；多线程 tell 时 handler sleep；stop/drain 等待释放 GIL；200 actor 停止后资源回到基线 |
| EXP | 预期错误 → REJECTED，actor 存活、generation 不变；子类同样 |
| SUP 监督 | 失败→重启 generation+1；只重启失败子；超强度隔离；窗口外失败剪除；factory 失败耗预算；延迟公式与 jitter 边界；reset 仅隔离态；RESTART_PENDING 中 stop 后不再重启；BaseException 语义（D9） |
| CUS 托管 | 重启按序归还；隔离原因码；确认停止才归还；stop 超时保留端点可重试；接收器异常吞掉；重复 stop 只发一次 |
| STP/DRN | 幂等；事件与 generation；handler 内 stop 自己不死锁；drain 完成/超时/重试/之后 spawn 抛错/按 cell 不影响他者 |
| EVT/SNP | 事件字段与顺序；接收器异常隔离；spec 级与默认接收器路由；COMPLETED 在 handler 返回后；snapshot 准确、未知 KeyError |
| ASK | 返回结果；超时后仍执行；错误映射；CLOSED → AdmissionError |
| LIFE | 有活 actor 时子进程干净退出；shutdown 幂等 |
| TMR/EFF | scheduler 与 effect lane 用例（D3 决定 v1 是否对 native 强制） |

交付给 Rust 开发者的规格文档：`docs/design/actor-engine-native-abi-spec-*.md`（范围、ABI 签名、状态机与代际表、事件与原因码字典、线程/GIL/重入/退出规则、错误映射、用例与条款对照、运行方式与通过判据、pykka 性能对照值）。文档不出现任何业务名词。

## 5. 批次（每批 ≤ 约 1000 行，测试先红后绿）

| ID | 内容 | 依赖 | 人日 | 可并行 |
|---|---|---|---|---|
| P0a | dev → `intg/actor-rustify` 同步（无冲突） | — | 0.5 | — |
| P0b | 落地 CI-I：`ci/rustify-ci-i` 变基到 intg，解 `tests.yml` 冲突 | P0a | 1 | 与 P1 |
| P1 | ABI 规格文档 + 60 个 spawn 点/进程拓扑/BaseException 现状清单。**Allen 决策门** | P0a | 1.5 | — |
| P2 | `port.py`（Engine/Cell/Spawner/Host/Config）、`ActorSpec.event_sink`、ask、按 cell drain、PykkaEngine；`ActorRuntime` 暂留兼容门面 | P1 | 2.5 | — |
| P3a | 套件骨架 + ADM/ORD/EXP/ASK/SNP/STP/DRN/EVT + import 闭包 | P2 | 2.5 | 与 P3b/P4/P5/P6 |
| P3b | SUP/CUS/LIV/GIL/LIFE/TMR/EFF；迁入 tests/kernel/actors 用例并去 pykka 耦合 | P2 | 2.5 | 同上 |
| P4 | 门禁 `engine-holding` + 64 条清单 + recall 名单 + PAC 改名 | P2 | 1.5 | 同上 |
| P5 | `create_actor_host`、组合根接线、`actor_host` fixture；先用**隔离模式**（每次 spawn 独立引擎，行为零变化） | P2、P1 | 2 | 同上 |
| B1 | dispatch 6 + ipc + correlation + runtime/settlement + processes 3 | P4、P5 | 2 | B1–B6 互相并行 |
| B2 | orgfs 9 + 透传 4（事件接收器移到 spec） | P4、P5 | 2.5 | 同上（必要时拆 2 PR） |
| B3 | lark 5 + transport 3 + transfer 2 + network 1 | P4、P5 | 2 | 同上 |
| B4 | lifecycle 3 + operations 2 + squire 3 + forwarding 2 + configuration/autoupdate/state_persistence | P4、P5 | 2 | 同上 |
| B5 | identity 2 + session_actor + inbox 2 + harnesses 3 + org 2 + pac 1 | P4、P5 | 2.5 | 同上 |
| B6 | biz 3 + kernel log writer + hooks bus；7 处 scheduler 与 48 处 EffectLane 改走 host 工厂 | P4、P5 | 1.5 | 同上 |
| P6 | `backends/native.py` 适配 + `backends/reference_abi.py` + 后端选择 `HYPRIAL_ACTOR_BACKEND`（缺 native fail-closed） | P2、P3a | 2.5 | 与 B* |
| P7 | 隔离模式 → 每进程一个共享引擎；附线程数、tell 延迟与全量测试证据 | B1–B6 | 1.5 | — |
| P8 | 删兼容门面与 `runtime:` 注入参数，公共面不导出，清单归零后删除 | P7 | 1 | — |
| P9 | 交接骨架：最小 `crates/` workspace（对齐 S1-A）、PyO3 存根、构建接线、native job | P0b、P6、D8 | 2 | 与 P7/P8 |

合计约 **33.5 人日**；关键路径 P0a→P1→（裁定）→P2→P5→B5→P7→P8 约 **11.5 天**。

每批的“红”：B* 先从清单删条目使门禁变红，迁移后转绿，域内行为测试不改断言；P2/P3 新语义用例先红；P4 变异用例先红。

### 5.2 分支与基线

- 新基线 = 合入 0.5.5.3 之后的 dev；在此之前 P0a 先对齐当前 dev，0.5.5.3 合入 dev 后再同步一次。
- 本规划**取代 G0 ① “以 #1892 合入 SHA 为最终 BASE”**：#1892 已于 2026-10-30 关闭、未合并。附录 A/B/D/H 在新 BASE 上重新生成，“61 个 ActorSpec 点”更新为当前 60。
- D7（建议）：P2–P8、B* 是不含原生代码、行为不变的重构，走 `release/v<next>` → dev，避免 intg 再分叉；intg 只承载 crates、CI-I、native job。这与 G0 ⑭“内部 PR 只进 intg”冲突，需裁定。

### 5.3 “Rust 开发者可以开工”检查表

1. D1–D9 已裁定；ABI 规格获批，`ENGINE_ABI_VERSION=1` 冻结。
2. 套件在 pykka 与 reference-abi 上连续 3 次全绿，可一条命令独立运行，import 闭包用例通过。
3. `engine-holding` 零豁免；`hyprial.kernel` 不导出 ActorRuntime。
4. P7 共享引擎已上线，全量 `tests/` 在 pykka 上绿。
5. `HYPRIAL_ACTOR_BACKEND=native` 缝就位，缺 `_core` fail-closed；P9 存根可 import，套件对它稳定显示红。
6. CI-I 已在 intg 落地；intg 与新 BASE 同步；Rust 工具链与平台 job 就位。
7. 交接包：规格文档、套件运行说明、性能对照基线、术语表；不需要任何业务文档。

## 6. 待 Allen 裁定与风险

| # | 决策 | 建议 |
|---|---|---|
| D1 | FFI 模型：回调型引擎（Rust 调 Python handler，消息不透明），还是 G0/OTP 的 data-only 端口（需重写业务） | **回调型**；以本文取代 G0 附录 C 第 2/3 条与 OTP §1.6.3/§5，范围限本目标 |
| D2 | 监督语义：Rust 完整复刻 guardian | **复刻**；允许在 Ractor 监督事件之上实现这层固定策略，需放宽 T1 |
| D3 | Rust v1 范围 | 只做 actor 引擎与内部重启计时；EffectLane 与业务计时器走 host 工厂、保留 Python，留 v2 |
| D4 | ask 进入端口及失败异常 | 现在加入；42 处手写 reply 迁 ask 不作为前置；失败抛 `ActorFailedError` |
| D5 | 注入方式 | 显式构造参数注入，不用进程级全局 host |
| D6 | 每进程一个共享引擎 | 共享（P7） |
| D7 | 落地路径 | 去持有走 release→dev，原生部分留 intg |
| D8 | 原生模块名与打包 | 沿用 `hyprial._core`，暴露 `actors.Engine`；是否换 maturin/拆 wheel：**UNKNOWN** |
| D9 | 线程模型、BaseException 语义 | 每 actor 可独立阻塞不饿死他者；BaseException 按子失败处理；pykka 现行行为 **UNKNOWN**，P1 实测 |
| D10 | 3 条 legacy 豁免 | 不纳入 |
| D11 | 纯 Python 参考 ABI 后端 | 提供 |

| 风险 | 缓解 |
|---|---|
| 共享引擎改变语义（单锁争用、drain 范围、事件归属） | P5 先隔离模式，P7 单独切换并附证据 |
| GIL 死锁或 finalize 后 PyO3 abort | ABI 硬规则 + GIL/LIFE 用例 |
| 计时用例在 CI 抖动 | 容差、极小 backoff、连跑 3 次 |
| intg 再次分叉 | D7 + 每周同步 |
| cost_counters/ipc_stats 依赖现有计量点 | 计量包装留在 Python 适配层并加回归 |
| actor 数随 agent/session 增长 | Rust 线程模型须满足 LIV-02 |
| orgfs B2 超 1000 行 | 拆 2 PR |
| CLI/worker 进程里的 owner：UNKNOWN | P1 盘点后再冻结组合根清单 |

## 7. 决策日志

### D1 FFI 模型 —— 已裁：回调型（Allen 2026-11-01）

- 原话：「同意回调型」。
- 裁定内容：Rust 引擎提供 actor model（mailbox、顺序、调度、监督、生命周期），回调 Python handler，消息对 Rust 为不透明对象。业务开发者主语言保持 Python。本文在本目标范围内取代 G0 附录 C 第 2/3 条与 OTP 增补 §1.6.3/§5 的字面；G0 禁回调的原意（Rust 侧不得按领域消息分派）保留：Rust 只调用不透明 handler，不检查消息内容。
- 讨论中确认的路线：第一步 Rust 做骨架、Python handler 做叶子，性能收益有限（handler 仍在 Python 且受 GIL 限制）；之后按实测热点把 handler 逐个换成 Rust 原生 handler 获得提速。
- 由此新增的要求：
  1. **ABI v1 预留原生 handler**：同一个 `spawn` 接受 Python handler 或 Rust 原生 handler（Rust 侧 trait 注册），引擎对两者的准入、顺序、监督语义完全一致；一致性套件对原生 handler 增加同一组用例（以 Rust 测试 actor 实现）。归入 D3/ABI 规格。
  2. **P1 实测**：用现有 CPU 计量（`cost_counters`）测引擎开销与 handler 耗时占比，按数字排定后续原生化顺序。

### GIL 调研（Allen 2026-11-01 要求：「py3.15 应该已经做了 GIL 的改进，也请调研」）

| 事实 | 来源 |
|---|---|
| Python 3.15 已于 2026-10-09 发布（当前 3.15.2） | docs.python.org/3.15/whatsnew/3.15.html |
| 默认解释器仍带 GIL；free-threaded 是单独的可选构建（`python3.15t`），3.13 实验、3.14 起受支持（PEP 779），3.15 未改为默认 | docs.python.org/3.15/howto/free-threading-python.html |
| 3.15 新增 PEP 803 `abi3t`：free-threaded 构建的稳定 ABI，一个扩展可服务多个 3.15+ free-threaded 版本；Maturin 已支持 | whatsnew 3.15 |
| 3.15 官方 macOS 安装包默认附带 free-threading 支持（仍是另一个解释器） | whatsnew 3.15 |
| free-threaded 单线程开销：pyperformance 平均约 1%（macOS aarch64）到 8%（x86-64 Linux）；内存占用更高 | howto/free-threading-python |
| 导入未声明支持 free-threading 的扩展时，解释器会在运行时重新启用 GIL 并告警 | howto/free-threading-python |
| PyO3 0.28 起模块默认声明线程安全（`gil_used=false`），可以 `gil_used=true` 退出 | pyo3.rs/latest/free-threading |
| 本仓原生依赖（2026-11-01 查 PyPI）：`cryptography`、`pydantic-core` 有 cp314t 轮子；**`eclipse-zenoh`（钉 1.9.0，最新 1.10.2）与 `pycrdt`（钉 0.14.9，最新 0.14.10）没有任何 free-threaded/abi3t 轮子** | PyPI JSON API |

结论：

1. 3.15 没有去掉默认 GIL；“GIL 改进”指 free-threaded 构建更成熟（abi3t、安装包默认附带）。
2. 本仓目前**无法**在 free-threaded 解释器上无 GIL 运行：zenoh 与 pycrdt 缺 free-threaded 轮子，导入即重新启用 GIL（或需源码构建且未经审计）。
3. 对回调型的意义：一旦 GIL 真正去掉，回调型引擎可以让不同 actor 的 Python handler 真并行，无需先把 handler 换成 Rust——这是回调型相对 data-only 的额外收益，强化 D1。

### D12（新增，待裁）free-threading 就绪

建议：
- 引擎 v1 **按无 GIL 正确性设计**：Rust 内部同步不依赖 GIL；PyO3 ≥0.28 默认线程安全声明；回调不假设跨 actor 串行。这几乎不增加成本，避免以后返工。
- G0 ⑬“CI 只跑标准 GIL 的 3.12/3.14，free-threaded 明确报错”在 v1 **改为**：必过 job 仍是标准 GIL；新增一个**非阻断**的 3.15t job 只跑一致性套件（不加载 zenoh/pycrdt），积累数据。
- 业务全量切到 free-threaded 另立门：zenoh、pycrdt 发布 free-threaded 轮子，且业务跨 actor 共享状态完成线程安全审计之后。

### 讨论记录：为什么需要监督语义（2026-11-01）

- 起因是 2026-08-23 事故（约 30 分钟人类通道中断，UPGRADE_RESTART_FAILED ×3）：启动即死的 worker 在与 accept 循环共用的 reconcile 周期里串行重试，每轮阻塞约 60 秒。修复 `aae92b2c69` 引入退避与隔离，`e9aad3ca53`（actor 运行时核心）将其收归 guardian。
- 四项各防一类问题：重启次数窗口 + 退避（防死循环重试）、单 actor 隔离（防拖垮全局）、未处理消息不悄悄丢（9-28 `937b78887b` 加入）、代际围栏（防重复重启）。
- Allen 也问过是否改用 Elixir/OTP 或 Go：结论不采用（Python handler 同解释器下 OTP 的隔离/抢占优势用不上；嵌入受 GIL 与整 VM 崩溃限制，外部进程需重写业务；Go 运行时与 CPython 同进程有线程/GIL/信号冲突）。

### D2 监督语义 —— 已裁：全部基于 Ractor 自行组合，不引入第三方监督库（Allen 2026-11-01）

- 原话：「同意，既然如此，我建议我们自己实现这四项语义，不用第三方库，全部基于 ractor 自己组合实现」。
- 事实依据（ractor 0.16.5 文档）：Ractor 只提供链接与 `SupervisionEvent`（ActorStarted / ActorFailed / ActorTerminated / ProcessGroupChanged）、进程组、定时发送；默认任一子 actor 退出则监督者退出；没有自动重启、重启策略、次数窗口、退避。kameo、ractor-supervisor 不采用。
- 实现方式：在 `handle_supervisor_evt` 中组合——ActorFailed → 按策略经同一工厂 `spawn_linked` 重启；监督者记录失败时间戳实现次数窗口，用定时发送实现退避 + 抖动；超限即隔离（不再重启并发事件，`reset` 时重新 spawn）；定时器消息携带代际编号，过期丢弃。范围限这一张固定状态机，不做通用监督树、OneForAll 或可配置策略。本条在本目标内修订 OTP 增补 §9 T1（原裁“只用 Ractor 原生能力、不自建”）：允许上述固定组合。
- **托管归还改为标准死信语义**（Allen 同意上一条建议，使引擎只懂 actor model）：
  1. 重启时**保留邮箱**（Akka 语义），导致失败的那条消息丢弃并发事件；不再“清空再归还”。
  2. actor 停止或隔离时，剩余消息投递到**死信回调**（消息 + 通用原因 `stopped` / `quarantined`），引擎不解释内容。
  3. 命名由“托管归还 / undelivered custody”改为“死信 / dead letters”；`ACTOR_RESTARTED_UNDELIVERED` 原因取消。
  4. 依赖现有回调的 13 个业务文件：回调逻辑不变，但重启路径不再触发；P1 逐个确认正确性不受影响，有影响的列出改法。
  这是对现有行为的有意改变，按 RED→GREEN 单独提交（P2），不混入等价迁移；一致性套件 CUS 组相应改为死信用例。

### D3 Rust v1 范围 —— 已裁：actor + 监督 + 死信 + 定时器 + 工作池；EffectLane / GenerationScheduler 收编为 actor 模式（Allen 2026-11-01）

- 原话：「同意」（针对下述修订建议）。讨论起点是 Allen 的问题：「effectlane 和 generatioscheduler 看起来也只是一种 actor？…为什么不能直接合入 actor 中去？」
- 认定：
  - `GenerationScheduler`（约 95 行）= actor model 的标准定时发消息（OTP `erlang:send_after`、Akka Timers、Ractor `send_after`/`send_interval`），加代际围栏。⇒ 收为引擎自带能力。
  - `EffectLane`（约 220 行）= 手写有界工作池（自带队列 + 线程），把阻塞 I/O 移出 owner actor，完成后回消息。不能合进 owner 本身（handler 内做 I/O 会卡住邮箱，停止/状态/其他命令进不来）；应是 owner 的**子 actor / 工作池 actor**（对应 Ractor Factory）。
- v1 Rust 范围：actor 引擎（邮箱、顺序、调度、ask）+ 四项监督（D2）+ 死信 + 带代际的定时器 + 工作池 actor + Python / Rust 两种 handler。
- Python 侧：`EffectLane` 与 `GenerationScheduler` 两个独立组件取消；48 处 EffectLane、7 处计时器在去持有批次 B1–B6 中改写为子 actor / 定时消息。B1–B6 合计估时增加约 3–4 人日。
- EffectLane 的两项额外语义在 P1 逐站点核对等价改法：① 先预留容量、再持久化提交、再提交任务（防“已落盘承诺却无容量执行”）→ 有界邮箱 OVERLOADED + 业务两段式；② 完成消息投递失败重试至 owner 收下 → 子 actor 回消息 + 死信兜底。
- 本条与 G0 第②条“EffectLane 与手写 mailbox 属旧引擎”一致，无需修订 G0②；原 §6 D3 建议（“EffectLane 留 Python 到 v2”）作废。

### D7 落地路径 —— 已裁（Allen 2026-11-01）

- 原话：「同意，实际上我准备尽快派独立开发者去完成 actor-rustify 侧的 rust 代码，完成后合入集成分支，与当前 hyprial 主开发线路不重合，rust 代码开发完后直接替换当前核心引擎」。
- Python 侧去持有（P2–P8、B1–B6，不含原生代码）按批次走 `release/*` → dev；容器为 WIP PR #2248（`infra-ops/pre-actor-rustify` → release/v0.5.5.4）。
- Rust 侧（crates、PyO3 模块、CI-I、native job）由独立开发者在 `intg/actor-rustify` 完成，与主开发线不重合；完成后经后端选择缝直接替换当前引擎。
- 修订 G0 第⑭条：“内部 PR 只进 intg”改为“仅 Rust/原生代码只进 intg；不含原生代码的去持有重构走 release→dev”。周同步（release → intg）保留并执行。

### 重新排序：先解锁 Rust 开发者（2026-11-01）

Rust 开发者开工只依赖“接口 + 验收”，不依赖业务侧去持有。因此关键路径拆成两条：

| 线 | 批次 | 交付给谁 | 估时 |
|---|---|---|---|
| 解锁线（先做） | P1 ABI 规格冻结 → P2 端口 + PykkaEngine → P3a/P3b 一致性套件 → P6 参考 ABI + 后端选择缝 → P9 crates 骨架与 PyO3 存根 + P0a/P0b intg 同步与 CI-I | Rust 开发者 | 约 9–10 人日；P1 冻结后（约 2 天）即可开工写 Rust，套件随后分批到位 |
| 替换线（并行） | P4 门禁 → P5 组合根 → B1–B6 去持有（含 EffectLane / 计时器收编）→ P7 共享引擎 → P8 清理 | 主线，随 release 发布 | 约 20 人日 |

“直接替换当前核心引擎”的前提：替换线 P7 完成（全仓只经组合根持有引擎）+ Rust 实现在一致性套件 native 后端全绿 + 平台硬格（G0⑧）通过。替换本身是切换 `HYPRIAL_ACTOR_BACKEND` 默认值的一个小提交。

### D12 free-threading 就绪 —— 已裁（Allen 2026-11-01）

- 原话：「授权 Verifier 帮我操作，另外两件事按你的建议去处理」（“另外两件事”之一即本项）。
- 引擎 v1 按“无 GIL 也正确”设计（内部同步不依赖 GIL；PyO3 ≥0.28 默认线程安全声明；回调不假设跨 actor 串行）。
- 修订 G0 第⑬条：必过 CI 仍为标准 GIL CPython 3.12 / 3.14；新增**不阻断**的 3.15t job，只跑一致性套件（不加载 zenoh / pycrdt）。
- 业务整体切换到 free-threaded 另立门：zenoh、pycrdt 发布 free-threaded 轮子，且跨 actor 共享状态完成线程安全审计之后再议。

### 第一步目标改判（Allen 2026-11-06）

- 目标版本改为 `release/v0.5.5.4`（HQ 11-03 B1“rustify 进 0.5.5.5”作废）；`intg/actor-rustify` 直接合入 release，不经过 `intg/cell-model`，合入时间由 Allen 决定。
- 上文“直接替换”前提中的“平台硬格（G0⑧）”改为：**5 个平台全部默认 native**（darwin/arm64、darwin/amd64、linux/amd64、linux/arm64、windows/amd64），每个平台都要构建 native wheel 并实际安装、调用通过。
- 本节修订 D7 的落地路径：去持有批次不再走 release → dev，而是每段开发单独开一个 PR，base 为 `intg/actor-rustify`。
- 批次排期、平台/CI 方案与待确认项见 [actor-rustify-step1-plan-2026-11-06.md](actor-rustify-step1-plan-2026-11-06.md)。
