# 自驱 socialware:常驻 PAC routine + kanban 自驱(设计 + 实现规划)

> 本文保留为设计历史。本集成分支的当前实现契约见
> [PAC workflow unification](spec-pac-workflow-unification.md) 与
> [命令和节点观察](../reference/workflow-node-observation.md)。合入集成分支不代表生产已升级。


- 日期:2026-08-21
- 作者:rsch-ptc(Allen 命题,hq-adjutant 转达)
- 状态:**已实装并在生产运行;本稿为设计记录**(更新于 2026-08-28)
  - 原状态行写的是「设计待评审;纯设计不实现(PAC #229/#230 未合 dev)」。那两条**都已合入 dev**,
    而 §6 的实现清单也**已经做完**:`src/hyprial/routine/`(registry / service / store / schema /
    source / cli)、`routines.sqlite3`(service.py:102)、`hyprial routine` 七个子命令
    (plan / add / list / status / rm / pause / resume);生产上有在跑的 routine。
  - ⚠️ **只更新了这一行状态陈述,设计内容与结论一字未动。** §6 的清单保持原样,
    它记录的是**当时规划了什么**,不是**现在还缺什么** —— 两者不该混写在一处。
  - 📌 而这一行过期了大约一周而没有任何东西提醒:**状态行是一份稿子里唯一会自己变假、
    却不会触发任何检查的部分**,而它恰好是让下游 PR(#298)排队的那个东西。
- 关联:design-pac-workflow(PAC)、notes-pac-v2-dag-analysis(#117,编译→执行分层)、org norms("任务域内自治…超域必须升级",docs/domains/org-context.example.md)
- ⛔⛔ **2026-09-18(U3)取代:载体从「一次性 PAC run」改为「一张 PAC 图」,完成从「回复文字匹配」改为「节点 owner 置 flag」。**
  依据:Allen 2026-09-17「我们需要退役workflow v1,未来全部走pac v2」+ 裁定 1(2026-09-13,⛔ 不搬 DONE nonce 文字匹配)、
  裁定 B/D(固定截止、⛔ 顺延/重试、let it crash 后上报)。映射表与验收:`docs/notes/pac/u3-routine-to-pac-mapping-2026-09-17.md`。
  📌 本稿以下各节**凡出现这四种说法的,都以本行为准**,⛔ 不再成立:
  ① 「产出一次性 PAC run」(§2 判断二、§3)⇒ 产出一张图:`work`(kind=task,owner=被派方)+ `deadline`(kind=clock,
     `guarded_by_node_id=work`,owner=上报接收人),耐久键 `operation_key = routine:<routine>:<task uuid>`;
  ② 「await DONE/BLOCKED match」(§3 每周期伪码)⇒ 无 await、无匹配器:完成 = owner 跑 `hyprial workflow complete <graph> work`;
     派发正文由 routine 自己投递(PAC 通知只带 brief_ref、激活不派根任务),并在正文末尾附上这条任务自己的 flag 命令;
  ③ 「workflow list/status 可查」(§4 第 5 条「可审」)⇒ 改为 `hyprial workflow status <graph>` / 图快照;
  ④ 「`{{nonce}}`」(§3 模板示例)⇒ 已无作用(渲染为空字符串)。schema 仍接受这个占位符,
     ⛔ 只是为了不让生产上已注册的 routine.yaml 在升级后变成 schema_error —— 真正删除它是 U5/U7 的事。
  ⚠️ 「超时」的口径:严格晚于截止(`now > deadline_ms`)才上报,与 reactor `_plan_clocks` 同一条件;
  上报后该任务**直接记为失败**,routine ⛔ 不自动重派 —— 是否重排由被上报的人/agent 决定(Allen 2026-09-17 原话)。
- 命题:协调者到检查点就停、不主动推不卡他的活(hyprial 协调者通病)——用"常驻 routine 定时查 kanban → 找可自驱任务 → 驱动"系统化解决;**对所有 hyprial 协调者通用**。

---

## 0. 核心架构判断(三条,全文支柱)

**判断一:routine 不做判断,只执行分流表。**
"派 worker / 协调者自己做 / 升级给人"这个三分决策若由 routine 计算,routine 就长出了编排智能,失败域回到不可枚举面。正确分工:**判断沉淀在 kanban 的标注里**(由人、或由协调者在它有意识的 turn 里打标),routine 只做"读标注 → 按表路由 → 确定性追踪"。routine 的每个动作是一次 PAC run,失败域保持可枚举——这是把 PAC 的元性质延伸到自驱场景的唯一方式。

**判断二:常驻 = daemon 内 routine 注册表 + reconcile 节拍;不是长驻 run,不是外部 cron。**
- PAC v1 的 run 有确定性终局义务,**长驻 run 直接违反**,否;
- 外部 cron(launchd)起 `hyprial workflow run` 可行但把调度状态推出 daemon(不可观测、不可审、重启无恢复),否;
- daemon 已有全部设施:1s reconcile 节拍(workflow tick 已挂)、持久化惯例、告警通道。**routine 是 reconcile 上的一个到期检查**(对齐 retry_due 形状),产出一次性 PAC run——**周期性由 routine 外壳提供,一次性由 run 保持**,两者各守其界。

**判断三:自驱的载体是 PAC run;"协调者自己做"= 派给它自己的 actor。**
- route:worker:<name> → 正常 PAC 派发;
- route:self → 派发给**协调者自己的 actor**——一次自我派发就是一次确定性唤醒,任务进入协调者的下一个有意识 turn(模型在这层干活,天经地义);
- route:human / 无标注 / 标注模糊 → escalate(AlarmEmitter 通道,PAC M2 已实装)。

## 1. routine 形状(设计要点 1)

`routine.yaml`(daemon 侧注册,每协调者一份或多份):

```yaml
version: 1
name: hq-selfdrive
schedule:
  interval: 15m          # 到期检查挂 reconcile;最小 1m 防自残
source:
  kind: taskwarrior      # v1 唯一;kanban-watcher 接口见 §2 开放点
  filter: "+selfdrive -blocked"   # taskwarrior 查询原样透传
policy:
  routes:                # 分流表:标签 → 去向;第一个命中生效
    - { tag: "route:worker", target_from: "tagarg" }   # route:worker:<name> → PAC 派该 worker
    - { tag: "route:self", target: "self" }            # 派协调者自己 actor
    - { tag: "route:human", escalate_to: "user:allen" }
  default: escalate      # 无 route 标注的 selfdrive 任务:升级,不猜
  task_template: |       # 派发正文;{{task.uuid}}/{{task.description}}/{{nonce}} 等
    【自驱任务 {{nonce}}】{{task.description}}\n完成后回复以 "DONE {{nonce}}" 开头;做不了回复 "BLOCKED {{nonce}} + 原因"。
limits:
  max_in_flight: 3       # 同时在驱的任务上限
  circuit_breaker: { window_runs: 5, escalate_ratio: 1.0, action: "pause+alarm" }
                       # 近 5 个 run 全部升级 ⇒ 停转告警(防标注事故刷屏/空转)
on_task_timeout: { action: escalate, escalate_to: user:allen }
```

**触发节拍**:v1 只 interval(到期检查骑 reconcile,1s 粒度绰绰);v2 候选事件触发(board 变更通知、run 完成事件)——事件源就绪前不投机。

## 2. 查 kanban(要点 2;最大开放点)

**标注约定(org 级,通用性的载体)**:

| 标注 | 含义 |
|---|---|
| `+selfdrive` | **opt-in 总闸**:没它 routine 永不触碰(默认拒绝) |
| `+route:worker:<name>` / `+route:self` / `+route:human` | 去向(由人或有意识协调者在 triage 时打) |
| `blocked` / taskwarrior depends | 不可自驱的既有表达,filter 排除 |

**取数路径(按优先级)**:
1. `task export <filter>` 子进程直读(daemon 本机,确定性,零依赖)——v1 默认;
2. **kanban-watcher 接口是开放点**:它现在是 board-keeper worker(pi runtime,mac-studio),真实接口未查实(能否应答"给我可自驱任务列表"的查询消息?)。若可,routine 的 source 加一个 `kind: watcher-query`——**查询本身就是一次 PAC run**(派查询任务给 kanban-watcher、await reply、超时升级),dogfood 闭环;若不可,维持 task export。开工前第一件查实事项。

## 3. 决策(要点 3)= 查表,不计算

```text
每周期:
  tasks = source.query(filter)                     # 全带 +selfdrive
  for task in tasks:
      if task.uuid 已在在驱集合 → 跳过(幂等,§5-缺④)
      if 在驱数 ≥ max_in_flight → 其余下周期(背压)
      route = 分流表匹配(task.tags)
      route == worker:X → PAC run: dispatch task_template 给 X,await DONE/BLOCKED match
      route == self    → PAC run: 派协调者自己 actor(唤醒式自派)
      route == human / 无匹配 → escalate(BLOCKED 回复 →  escalate 给人)
  熔断检查:近 window_runs 个 run 全 escalate → routine 转 paused + alarm
```

"派 worker 还是自己做"的判断**在被打标那一刻完成**(人或协调者的有意识 turn),routine 时刻只有查表。这正面回应 Allen 的观察:协调者"停了"没关系,routine 的节拍把任务确定性地递到它 inbox——**停不再等于丢**。

## 4. 边界(要点 4,防跑飞)

1. **opt-in 双标**:无 `+selfdrive` 不碰;有 `+selfdrive` 无 route 标注 → escalate 不猜;
2. **分层自治衔接**(automate-all-but-code-execution-consent):需代码执行/变更的任务,约定 `+route:self` 且任务正文自带 consent 提示——协调者 turn 内仍过既有同意门;不可逆/需授权/改产品范围/缺凭据 → 约定 `+route:human`,routine 只升级;
3. **域界**:`source.filter` 就是任务域,域外 routine 物理上看不见;
4. **与 supergraph 的边界**(pac-vs-supergraph-boundary):routine 的分流表是**编译好的静态图**——routine 是执行器不是规划器;任务需要动态拆解时,route:self(交协调者模型)或 route:human,**routine 永不试图自己规划**;
5. **可审**:routine 每个动作是一个 PAC run(workflow list/status 可查)+ daemon 轨迹日志;熔断/暂停本身是显式状态。

## 5. 通用性(要点 5)

routine 是 daemon 设施:任何协调者在自己节点 `hyprial routine add my.yaml` 即得自驱;标注约定在 org-context 层共享(与 norms 同册);`route:self` 的"self"解析为注册者自己 canonical actor——无 per-person 特判。

## 6. 实现规划

**PAC 原语够用**:fan-out、await reply/ack、timeout/retry/escalate、report、hooks(gate 可用于熔断暂停)、run 记录查询、recover-by-state(routine 自身状态同法持久化)。

**缺(PAC 合 dev 后补,按序)**:
1. routine 注册表 + 到期调度(daemon 组件,骑 reconcile;持久化 routines.sqlite3 同惯例);
2. task source adapter:`task export` 子进程读取器(+超时+输出上限;失败=该周期空转并计数,不当灾难);
3. routine.yaml schema(fail-loud,同 workflow schema 家规);
4. **幂等关联**:task UUID → (routine, target) 映射持久化;start 前查在驱集合,重启动不重派;
5. 熔断/并发上限(limits 段语义);
6. CLI:`hyprial routine add|list|pause|resume|status --json`(契约测试);
7. E2E:隔离 daemon + 临时 TASKDATA + 脚本化 worker,跑通"标注→分流→派发→超时升级→熔断→暂停恢复"全链(real_env:false,沿用 E2E-015 §B 模式)。

**落点**:PAC M2(#230)合 dev 之后动工;routine service 与 workflow service 并列,是它的 producer。与 #113(业务场景补全)合流时,B4(定时值班扇出)即 routine 的第一个内建剧本。

## 7. 风险与最脆弱假设

1. **kanban-watcher 接口未查实**(§2)——若它不能应答查询,v1 退化 task export 不受影响,但跨机场景(coordinator 与 board 不同机)需要它;开工先查;
2. **标注纪律是 socialware 的真难点**:工具一夜能建,`+selfdrive/+route:*` 的 triage 纪律要 org 养成——建议先在 hq-adjutant 一家试跑两周再推广;
3. **熔断参数默认值必须保守**(window 5、ratio 1.0、max_in_flight 3)——宁停勿刷;
4. **self 派发的唤醒语义**:协调者若是交互式会话(channel/extension),唤醒路径走 WAKE_PUSH 能力表——cc/pi 交互均 native,但"自我派发"与心跳/轮询的干扰要在 E2E 里实证(别把协调者刷成永动)。

---

## 8. task-association:接口边界(2026-08-21 重裁:逻辑归 devusr/kanban,hyprial 只出扩展点)

> 重裁记录:初版 §8 把判定/存储/提醒都设计在 hyprial 侧;Allen 2026-08-21 裁定"什么算 task、怎么关联"归 devusr/kanban,hyprial 只定义挂点与判定接口。hq-adjutant 同日补一刀 split(待与 devusr 敲定):**强制机制(hook + task_links + 提醒)留 hyprial 是纯机制,真正归 devusr 的是"task_ref↔卡 id 约定 + 什么算有效 task"**——比"hook 全归 devusr"更干净,kanban 侧可无感。本节按"机制 hyprial / 语义 kanban"的候选缝写;缝的最终位置以 hq×devusr 敲定为准。**hyprial 侧当前只定接口,不写实现。**

### 8.1 ownership 划分(钉死)

| 侧 | owns | 不碰 |
|---|---|---|
| **hyprial** | ①hook 挂载点定义(消息路径哪层);②"实质工作动作"的**判定接口**(默认谓词 + 可替换协议);③提醒通道的存在性(system notice);④扩展点的注册/开关机制 | 任务语义、关联记录、建卡、提醒文案、kanban 任何内部 |
| **kanban(devusr)** | 什么算 task、怎么关联、关联存储、提醒/拒绝策略与文案、`task link` 类工具 | daemon 消息路径内部 |

### 8.2 hyprial 侧扩展点(交付 devusr 的接口面)

1. **挂载点**:`message.send` handler,resolve-or-reject 之后、submit 之前(application.py:2141 起;CLI/MCP/worker 三路发送汇聚于此,单点全覆盖)。读取面(pending.list/ack/reply)不经 send,天然不沾;
2. **判定接口**(hyprial 定义的协议,kanban 侧提供实现):

```python
class TaskAssociationPolicy(Protocol):
    """注册于 daemon 的关联策略扩展点;hyprial 只定义形状。"""
    def classify(self, ctx: SendContext) -> AssociationVerdict: ...

@dataclass(frozen=True)
class SendContext:        # hyprial 提供给策略的全部事实,不多给
    sender: str           # canonical actor(已 resolve)
    targets: tuple[str, ...]
    intent: str           # request | reply | system
    conversation_id: str
    topic: str | None     # 线上既有字段,关联载体
    is_workflow_internal: bool   # wf- 前缀会话(PAC run 自带追踪)

# AssociationVerdict = pass | remind(text) —— v1 只有这两值;
# reject 是 hard 模式候选,试点数据出来前不进枚举。
```

3. **hyprial 内置默认谓词**(策略缺省时的判定,也是给 kanban 侧的参考实现语义):`intent == "request"` + 非 workflow 内部 + 目标是 `agent:` + 会话无既有标注 → `remind`;其余一律 `pass`。**宁可漏判不可误判**;
4. **提醒通道**:system notice(`receive_system_notice` → 随 pending.list 捞出,daemon 既有);hyprial 保证通道在,**文案与节流归策略实现**;
5. **注册/开关**:靠 §9 的 depend 机制声明(`task-association` feature `requires: [kanban]`)+ mode 开关(默认 off,off 零行为变化,守卫测试钉死)。

### 8.3 交 devusr 的工作项(按此接口)

- 实现 TaskAssociationPolicy:什么算 task、topic/会话怎么关联、关联存哪、提醒文案与节流;
- 建卡/标卡工具(如 `task link` 类)——CLI 形状由 kanban 侧定,hyprial 可提供挂载建议;
- 与 routine 的衔接协议:routine 需要的"已在驱"信号(task_ref ↔ run 映射)从 kanban 侧关联记录读——**接口形状两边约定**(建议:kanban 侧暴露只读查询,hyprial routine 经 daemon IPC 或直接读约定的存储,待定)。

### 8.4 明确不做的(hyprial 侧)

不写策略实现、不建 task_links 表、不出提醒文案、不做自动建卡。hyprial 对 kanban 的全部知识 = §8.2 的协议文件。

## 9. 组件依赖声明(depend 机制;self-drive 缺口的正式形态,比 self-drive 普适)

**查实现状**:`hyprial socialware install` 仍是恒 `COMING_SOON` 的占位，也无"feature 依赖组件"
声明机制——只有 lark 专用 capability 与 python 包依赖;self-drive 依赖 kanban、task-association
依赖 kanban,都需要它。旧一级 `hyprial install` 已删除。

### 9.1 形状(轻量,照能力表的诚实哲学)

```yaml
# feature 声明(随 feature 注册,例:routine add / task-association 启用)
feature: selfdrive-routine
requires:
  - component: kanban          # 组件注册表里的名字
    on_missing: refuse         # refuse(启用即拒,带 remedy)| degrade(启用但空转+doctor 警告)
```

### 9.2 机制三件套

1. **组件注册表**:daemon persistent config 里 `components:` 段(kanban/squire/lark-adapter 等),记录"装了没、配好没";注册/注销经 CLI(`hyprial component register kanban ...` 或由 install 流程写);
2. **启用时检查**:feature 启用(routine add / 策略注册)时求值 requires:`refuse` 缺依赖 → 报错带 remedy("先 hyprial component register kanban …");`degrade` → 启用但 doctor 挂警告,feature 自身声明降级语义(self-drive 缺 kanban = 不启动调度,不是半跑);
3. **doctor 面**:`hyprial doctor` 增"feature 依赖"段——每 feature 的 requires 满足态一目了然(对齐 §7 告警可观测惯例)。

### 9.3 裁决

- **声明与检查分离**:feature 只声明,检查器只有一处(daemon/CLI 共用),防"声明了没检查"漂移(照 capabilities.py 的表→代码链接测试哲学,加守卫);
- **不做版本约束**:v1 只查"在不在",不做版本/兼容矩阵(超轻量承诺;版本是 v2 且需真实需求驱动);
- **普适性**:任何 hyprial feature 可声明 requires——self-drive 是第一个消费者,task-association(§8)第二个,lark 依赖类 feature 后续可迁入。
