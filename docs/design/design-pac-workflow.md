# PAC:hyprial 声明式编排设计(workflow.yaml + daemon 确定性执行器)

> 本文保留为设计历史。本集成分支的当前实现契约见
> [PAC workflow unification](spec-pac-workflow-unification.md) 与
> [命令和节点观察](../reference/workflow-node-observation.md)。合入集成分支不代表生产已升级。


- 日期:2026-08-20
- 作者:rsch-ptc(调研 + 设计;Allen 命题,hq-adjutant 转达)
- 状态:**待评审**
- 前置:`research-ptc-vs-hyprial.md`(PTC 调研)、dsh code-mode spike(本文 §1)

---

## 0. 定性与边界(Allen 裁定,全文遵守)

- **PAC = Programmatic Agent Calling**(Allen 命名):网络层的 agent 编排,区别于模型层的 PTC(Programmatic Tool Calling,harness 内部模型写程序调工具)。
- **hyprial 是 harness 网络,不是 harness**:workflow 只描述**派发 + 追踪意图**;执行任务的是目标 worker(harness 内 agent);hyprial 只做**确定性派发 + 收应答 + 超时提醒**,不执行任务本身。
- **全程不涉及模型 turn**:执行器是 daemon 内的确定性代码,读 yaml → send → poll → ack → 提醒。失败域可枚举,不需要模型行为取证。

## 1. 动机与证据(为什么不走模型层)

可复现的 dsh 机制证据来自已入库的 E2E-014 契约
(`contract/e2e/14-dsh-worker.md` + `run-dsh-worker.py`)和隔离单测记录:
`uv run --extra test python -m pytest tests/test_dsh_harness.py -q` → **9 passed**。

- 不依赖模型的半段全绿:隔离 host 起服、双 worker 经 hyprial CLI 注册在线、W1 的 PTC preset(`mode: code`)端到端注入——**hyprial↔dsh 集成机制本身无问题**。
- 编排驱动段 FAIL 且**未归因**:driver 600s 未收到编排者终态回复;嫌疑横跨四个不可控面(凭据有效?网络可达?模型太慢?worker 没按指令调工具?),归因需要四套取证(host 日志 / session 历史 / daemon 日志 / 凭据状态),取证窗口一关即永久不可归因。
- **教训**:把"派发 → 等应答 → 决定下一步"放在模型 turn 里,等于把编排的正确性押在模型行为上——漏触发、发错对象、idle 这类失败在模型层**不可结构性排除**,只能事后取证。Allen 当日派发出的事故(漏触发下一步)与本次 spike 的取证困境是同一根因的两端。

**PAC 的回答**:编排逻辑移出模型,变成 daemon 执行的声明式数据。目标 worker 应答不应答,是网络层可观测事实,不需要猜模型。

## 2. 参考调研

### 2.1 Claude Code Dynamic Workflows(ultracode 触发)

出处:https://code.claude.com/docs/en/workflows(+ /agent-teams,2026-08-20 抓取)。

- **形态**:**模型写的 JavaScript 脚本**(非声明式),顶层 `await`,`agent()` 派一个 subagent、`pipeline()` 对列表每项派一个;runtime 隔离执行、后台运行、会话内可 resume。ultracode = `xhigh` 推理档 + 自动为每个实质任务规划 workflow;关键字 opt-in **只在人亲手输入时生效**(`-p`、SDK 非 human origin、定时任务、webhook 均不触发)。
- **官方四象限("who holds the plan")**:subagents(Claude 逐 turn 决定)/ skills(Claude 照指令)/ agent teams(lead 监督对等会话,共享任务表)/ **workflows(脚本持有计划,中间结果留在脚本变量,模型上下文只有最终答案)**。
- **语义细节**:`agent()` 被停或不可恢复错误时 resolve 为 `null`,`pipeline()` 保留 null 由脚本过滤;约束:无 mid-run 用户输入(阶段间 sign-off 需拆多个 workflow)、脚本自身无 fs/shell(执行在 agent)、禁 `import()`、≤16 并发、≤1000 agent/run;**resume 按启动序重放,缓存截止到第一个未完成的 agent**——fan-out 中途停止时,之后启动的 agent 即使已完成也重跑(官方自认昂贵);保存为 `.claude/workflows/` 命令可复用,plugin 可分发;大运行有 25-agent/1.5M-token 警告护栏与 size guideline。

### 2.2 deepseek-harness `workflow` 工具

出处:`packages/workflow/tool-workflow/README.md`(master 141eb6fe)。同一形态:模型向 `workflow` 工具传 `meta + script(plain JS)+ args`,引擎 fan-out subagent;非 completed 的停止映射为 isError、绝不把部分输出当成功;使用策略写明"一两个委派用 subagent,显式大编排才用 workflow"。

### 2.3 对比与定位

| | 计划载体 | 执行者 | 编排对象 | 模型在环路内? |
|---|---|---|---|---|
| CC dynamic workflows | 模型写的 JS | harness 内 runtime | 同会话 subagent | 写计划时在;执行时不在 |
| dsh workflow | 模型写的 JS | harness 内 engine | 进程内 subagent | 同上 |
| **PAC(本文)** | **声明式 YAML(数据)** | **daemon 确定性代码** | **跨 harness/跨机 worker** | **全程不在** |

两家参考实现都把"计划移出上下文"做了一半(移进模型写的代码);PAC 推到头:**计划是数据,执行是确定性代码**。差异的根因是层不同——它们的 agent 是会话内 subagent(同产品同进程),PAC 的 agent 是 hyprial 网络上的 worker(异构 harness、跨机、各自有 inbox)。

### 2.4 可借鉴 / 可避

借鉴:
1. **可重跑的编排即资产**(CC 的 save-as-command):workflow.yaml 入库、可版本化、可复读;
2. **规模护栏**(CC 的 agent 上限/大运行警告):PAC 的 targets 上限 + 显式确认;
3. **批准门**(CC 的 per-run prompt):`hyprial workflow run` 默认 `--dry-run` 打印解析后的派发计划;
4. **空结果显式**(CC 的 null 语义):target 超时/无应答是**显式状态**,不是隐式缺席;
5. **opt-in 边界**(CC 关键字只认人输入):PAC run 只能由注册身份(人或 agent)经 CLI/IPC 发起,拒绝来自消息正文的间接触发(防注入式编排)。

可避:
1. **模型写编排**的不确定性(本文 §1);
2. **start-order 重放**的 resume 代价:PAC per-target 状态机各自持久化,恢复按状态而非启动序;
3. **无 mid-run 输入**的僵硬:PAC 允许运行中 `workflow cancel` / 单 target 级重试(执行器是 daemon 代码,随时可干预);
4. 编排与执行耦合:CC/dsh 的 workflow 与执行同进程同产品,PAC 保持派发/执行分离(hyprial 永不执行任务)。

## 3. workflow.yaml schema v1

```yaml
version: 1                      # 必填;schema 演进之门
name: probe-fleet               # 必填;run 的显示名
summary: 探活全部夜班 worker     # 可选,人读
task: |                         # 必填;发给每个 target 的任务正文
  探活 {{nonce}}。收到请回复 PONG {{nonce}}。   # 支持 {{nonce}} / {{target}} 插值
targets:                        # 必填,非空;元素 = actor uri / nickname 字符串,
                                # 或 {name, task?} 映射(per-target 任务覆盖,见下)
  - spike-w2-probe
  - name: agent:bobadmin:mac-studio:night-watch
    task: "评审 PR #208,回复以 REVIEW {{nonce}} {{target}} 开头"
await:
  kind: reply                   # reply(默认)| ack
  timeout: 600s                 # 每 target 独立计时,从投递被 inbox 接受起算
  match: "PONG {{nonce}}"       # 可选;reply 需包含的子串(模板展开后)。缺省 = 该会话内任何 reply
on_timeout:
  action: retry                 # retry | escalate | report
  max_attempts: 3               # action=retry 时有效,默认 1(不重试)
  backoff: [30s, 120s]          # 可选;逐次重试间隔,缺省立即
  escalate_to: user:allen       # action=escalate 时必填;经 AlarmEmitter 递达
report_to: user:allen           # 可选;终局汇总投递对象,缺省 = run 发起者
limits:
  max_targets: 50               # 可选;护栏,超出拒绝载入(默认 50)
hooks: {}                       # 可选;v1 只接受空 mapping——该段是 v2 扩展位,
                                # 非空即 schema 错误并点名原因(fail-loud,不静默忽略)
```

**评审裁定记录(2026-08-20,Allen 经 hq-adjutant 转达)**:①无 DAG 够,v1 不加;DAG 可能归 amyws 的 supergraph(上层编排),归属确认中,不影响本设计。②执行器留 hook 扩展点(见 §4.7),v1 空实现。

逐字段裁决:

- **`targets` 只是名单,不做 selector**:v1 不支持“按能力/正则选 worker”——派发意图必须显式可审(可避清单 4;selector 留 v2,且必须经过 `workflow plan` 展开成名单人审后才可 run)。
- **运行图保持冻结**：`role: plan` 节点只能通过受信策略校验后的
  expansion completion 绑定一个独立子图；不能原地增删节点，也不能把
  `hold` 当作重规划。子图 worktree 只负责隔离执行，不自动 merge/push/PR。
- **per-target task 覆盖**(2026-08-20 增补,E2E-015 ergonomics 证据驱动):targets 项允许 `{name, task?}` 映射,task 缺省回落全局 task。动机:第一份真实值班剧本(三 worker 各审一个不同 PR)证明“每个 target 一段不同任务正文”是日常刚需;无此字段时只能在共享 task 里让 worker 按 `{{target}}` 间接查分配,协调者不会这么写。增量、向后兼容。
- **`await.kind: ack`**:只要求目标 worker 确认收到(harness_ack),不要求内容应答——探活/回执场景。
- **`await.match`**:reply 的内容判定。**关联的确定性不靠它**——见 §4.3,会话隔离是主机制,match 只是内容加强。
- **`on_timeout` 三动作**:`retry`(重发,独立计时)、`escalate`(立即提醒人/协调者,不重试)、`report`(记入终局汇总,不提醒)。retry 耗尽显式转 escalate(若给了 escalate_to)否则转 report——**重试预算耗尽永不静默**。
- **无 DAG / 无步骤依赖**:v1 是 fan-out + 收集,不是工作流引擎。"B 等 A 的结果"用两个 run 或 report 后的下一次派发表达——与"不执行任务"边界一致(条件分支 = 编排智能,是模型/人的职责,PAC 只保证每次派发的确定性)。

## 4. daemon 确定性执行器设计

### 4.1 模块与集成点

```text
src/hyprial/workflow/
├── schema.py      # yaml 载入 + 校验(fail-loud,未知字段拒绝;模板 {{nonce}}/{{target}} 展开)
├── executor.py    # 执行器:per-target 状态机驱动,daemon 内一个 fiber 统一调度
└── store.py       # sqlite 持久化(<state>/workflows.sqlite3):runs / targets 两表
```

- **CLI**:`hyprial workflow run <file.yaml> [--from <identity>] [--dry-run]` / `hyprial workflow status <run-id> --json` / `hyprial workflow list` / `hyprial workflow cancel <run-id>`(对齐现有 `--json` 契约,进 CLI 黑盒契约测试)。
- **IPC**:`workflow.start` / `workflow.status` / `workflow.list` / `workflow.cancel`,挂进 `daemon/application.py` 的 `handle()` 分发(现有 `message.send`/`message.pending.list` 同层,application.py:1508 起)。
- **发起身份**:`--from` 必须是注册身份(对齐 E2E-014 的 resolve-or-reject 裁决);执行器以该身份作为 sender 发出所有派发,reply 也回到该身份的 inbox。
- **零模型依赖**:执行器只调 InboxService(投递)与 pending 查询(收应答)与 AlarmEmitter(提醒),不触碰任何 harness connector。

### 4.2 per-target 状态机

```text
pending ──send──► dispatched ──reply/ack 匹配──► done
                   │  │
                   │  └─timeout──► (retry: attempts-1 后回 pending)
                   │               (escalate: emit alarm ──► escalated)
                   │               (report: ──► timed_out)
                   └─send 失败(目标离线等)──► 按 on_timeout 同路径处理
                                                              │
                                       run 终局 = 全部 target ∈ {done, timed_out, escalated}
                                                              ▼
                                            report_to 收到汇总(每 target 状态/耗时/尝试数/回复摘要)
```

`escalated` 不是终局静默态:alarm 被人/agent claim 后,发起者可 `workflow cancel` 或对单 target 手工重试(v2 指令);v1 中 escalated 计入终局但 alarm 记录永久可查。

### 4.3 关联与判定的确定性(核心设计决策)

- **每 (run, target) 一个独立 conversation**:`conv_id = "wf-<run_id>-<target_name>"`。reply 判定 = 该 conversation 内出现来自该 target actor的消息。**会话隔离是关联确定性的主机制**——不依赖目标 worker 带 nonce、不解析语义;nonce 插值与 `match` 只是内容层加强(防同会话串话、给人读)。
- **reply 观察是“按序找首个匹配”,不是“只看最旧”**(2026-08-20 增补,E2E-015 实跑抓出):一条不满足 `match` 的回复留在 pending 供取证,但**不遮挡**后续匹配的回复——否则一条 off-format 回复会把 target 卡死到超时。
- **ack 判定** = daemon inbox 观测到目标对该 messageId 的消费(消息从 pending 消失且非 expired);与工作队列语义一致(daemon inbox 永远是真相源,design-harness-capability-abstraction.md §3)。
- **send 的 accepted/queued** 取自 `SubmissionResult`(inbox/api.py:46):queued=目标离线入队——计时从 accepted 起,离线排队本身吃 timeout(这是诚实语义:派发 SLA 包含可达性)。

### 4.4 持久化与恢复

- `store.py` 两表:`runs(run_id, name, yaml_hash, sender, report_to, state, created_at, finished_at)` / `targets(run_id, target, conversation_id, state, attempts, deadline_ms, last_message_id, reply_excerpt)`。
- **daemon 重启恢复**:启动时扫 `state ∈ {dispatched, pending}` 的 target,按持久化的 `deadline_ms` 恢复计时(墙钟;重启耗时诚实计入 timeout——与"离线排队吃 timeout"同一哲学),已完成 target 不重发。**按状态恢复,而非按启动序重放**(可避清单 2)。
- 恢复时 conversation 内的迟到 reply 仍按 §4.3 匹配,幂等(done 态不再迁移)。

### 4.5 超时提醒(escalate)投递

复用 `hyprial/alarm.py` 的 `AlarmEmitter`(:43;已支持 human/agent 双通道 + claim 语义):`escalate_to: user:<owner>` 走 human 通道(经接收方 Squire adapter),`agent:...` 走 agent 通道(daemon 系统通知入目标 inbox)。**不新造提醒机制**。

### 4.6 护栏

- `limits.max_targets` 默认 50,超出拒绝载入;
- `workflow run` 默认先打印展开后的派发计划(目标名单、每 target 任务正文、超时策略)要求确认,`--yes` 跳过(脚本化场景);
- 单 run 总时长上限 = `max(timeout × max_attempts + Σbackoff) × 1`(targets 并行),由执行器在 start 时计算并写入 run 记录,超出即强制 report 收尾(防配置错误造出永动 run)。

## 4.7 hook 扩展点(Allen 2026-08-20 裁定加入)

执行器在每个 per-target 关键转换点开一个可注入钩子;**v1 只发 `NoopHooks` 空实现**,不是插件框架:

```python
class WorkflowHooks(Protocol):
    def gate_dispatch(self, run, target) -> bool: ...      # 派发前闸;False = 本轮留 PENDING
    def on_dispatched(self, run, target) -> None: ...      # 通知:已成功投递
    def on_reply(self, run, target, view) -> None: ...     # 通知:匹配到回复(ack 前)
    def on_timeout(self, run, target) -> TimeoutOverride | None: ...  # 决策:覆盖声明策略;None = 走 yaml
    def on_terminal(self, run, target) -> None: ...        # 通知:进入 done/timed_out/escalated
```

两个未来扩展的挂点(Allen 点名):**DAG 的“依赖满足才 dispatch”** = `gate_dispatch` 返 False 直到依赖满足;**自定义 on_timeout 策略** = `on_timeout` 返回 `TimeoutOverride(action, escalate_to)` 覆盖 yaml 声明。hook 与核心同纪律:确定性、零模型 turn。schema 的 `hooks` 段(§3)v1 只接受空 mapping,占住扩展位。

## 5. 明确不做(v1)

1. **不执行任务**:workflow 只有派发与追踪;任务正文是不透明字符串。
2. **不做智能汇总**:report 是状态聚合,不是内容综合;要"读所有回复写摘要"就派一个 worker 做(那是一次普通派发)。
3. **不做 DAG/条件分支**(§3 裁决);**不做 target selector**(显式名单);**不做 mid-run 修改**(只有 cancel 与单 target 重试,v2)。
4. **不保证目标 worker 会干活的"质量"**:PAC 保证的是"派了、等了、超时了有人知道"——worker 干活好坏回到 harness 层与人的评审。

## 6. 测试与里程碑

- **契约**:schema 校验金身(合法/非法样本)、CLI 黑盒(run/status/list/cancel --json)、状态机单测(每迁移 + 超时三分支 + retry 耗尽)。
- **E2E**:隔离 daemon + 两个 dsh worker(沿用 spike 资产):全应答、单 target 不应答→retry→escalate、daemon 重启中 run 恢复、ack 模式。
- 里程碑建议:M1 schema+执行器+CLI(单机 local 投递)→ M2 持久化恢复 + escalate 通道 → M3 契约/E2E 入库。

## 7. 风险与最脆弱假设

1. **conversation-per-target 的判定依赖 worker 回复落在同会话**——pump 语义现状如此(streaming.py 的 HarnessResult 回链),已在 E2E-014 验证;若未来某 connector 的回复路径变化,此假设最先破,契约测试锁住。
2. **ack 判定的 inbox 可观测性**:消费位点标记是 inbox 内部状态,需要确认 pending 消失对 daemon 可查(message.pending.list 已有;ack 记录查询面可能需小扩)。
3. **escalate 到人依赖 Squire 通道配置**——未配置时 AlarmEmitter 的 human 通道退化行为需显式声明(不能假装送达)。
4. 最大遗留赌注:**v1 无 DAG 是否够用**——若真实编排需求普遍是"A 的结果决定 B 的任务正文",则 PAC v1 只解决"派发可靠性"一半命题,多步编排需 run 链接(report 触发下一 run),该模式的 ergonomics 需 Allen 用一个真实值班剧本验证后再定 v2。
