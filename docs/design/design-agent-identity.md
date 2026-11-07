# 设计稿:agent 实体化 —— 身份归 PAC,运行归 daemon

> **状态:设计稿。**本文件不授权实现。它的第一份内容是**现状读数 + 目标形状 + 两者的距离**;
> 每一条现状都带可复算的出处,⛔ 不写没核过的断言。
> 起点:Allen 2026-09-08 在飞书上提出的三条复述与理由,以及 hq-adjutant 对当前实现的逐条对照。

## 1. 目标形状(Allen 2026-09-08)

```
PAC 交【身份】,不管【运行】
  身份 = 能「找到这个 actor」并让它把活干对的信息:
         uri · actor · owner · machine · cwd(哪个代码仓库;项目配置层随它自动读)· config(读哪份【个性】skill/配置;G6 见 §3 更正块)
daemon 管【运行】
  运行 = harness · model · provider · 启动参数 · 与网络的通信方式 · 生命周期管理方式
```

**理由(原话转写):** PAC 管的是**任务**。一个前端开发的任务,重要的是让 agent
**在正确的代码仓库、读取正确的 skill、并在之后的任务中可以被通信**;
而具体用哪个 harness、哪个 model **会受环境影响** —— 开发者笔记本上可能装着 Claude 且习惯用它,
而他 AFK 时云端可能只有 pi + ds4。**只要仓库与 skill 正确,完成任务的能力不会欠缺太多。**
反过来,daemon 必须管运行时信息,因为**不同 harness/model 的通信方式与生命周期管理方式都不一样**。

### 1.1 这条理由的可检验形式

上面那段话有一个**可证伪的核心**,把它写出来,后面每一次改动都拿它对:

> **身份是任务的不变量;运行是环境的函数。**
> ⇒ 检验:**同一个任务,换一个 harness/model 仍应当能被满足** —— 不能,就说明有东西被错放在身份侧。

### 1.2 PAC 声明的不是一个档,是【要求 + 退避链】(Allen 2026-09-08 更正)

⛔ **本稿早先写过「`requires.tier` 是唯一承重墙」—— 那句是错的,已删。**
错在把 `tier` 当成**保证**,而它只提供**降级顺序**。Allen 给的形状是:

```
PAC 派发一个 frontend-agent,要求:
  加载 nextjs skill · cwd 在 repo/web/ · 职责是协调者(用一段 prompt 声明)
  要求 harness=claude / model=opus
  不可用 ⇒ 退避到其他 A 级 harness+model
  再不行 ⇒ B 级
  否则报错
⇒ 【可达到的要求】= skill · cwd · 角色      【可退避的方案】= tier
```

**⚠️ 分界线要写清,否则与 §1 自相矛盾:**
```
✅ PAC 说【偏好 + 退避链】——【可被环境否定的意愿】
⛔ PAC 持有 LaunchSpec(harness/args/probes/怎么起)——【一份配方】
⇒ 谁判「不可用」、怎么探测(probes),留在 daemon 侧
```

### 1.3 ⭐ 真正的承重墙:降级必须留痕并传给下游

Allen 的例子:官网设计任务,本预期 cc+opus 一次做完、codex+astra 视觉验收;
环境只剩 pi+glm5.3-flash ⇒ **档位降了但仍可交付**,因为 frontend-agent 的作用变成
「**快速出完整粗糙稿,省强力档 token**」,由 astra 收尾。

**⇒ 这句话成立有一个前提,那才是承重墙:**
```
⭐ 【实际用了哪个档,必须传给下游验收者】
理由:astra 接到的是「粗糙稿」还是「成品」,决定它花多少力气、按什么标准收
  降级【没有留痕】⇒ astra 会拿成品的标准去审粗糙稿,或反过来放过本该返工的东西
  ⇒ 这两种错【都不会报错】—— 它只是让验收变成一场误会
⇒ 风险不在「tier 词表准不准」,在「降级有没有被记录并传下去」。这一格今天【没有】。
```

⚠️ **连带一格:要能区分【被迫降级】与【计划内低档】。**
Allen 那句「省强力档 token」把降级从**妥协**变成了**策略** ——
粗稿归低档、收尾归强档是**故意的分工**,不是退而求其次。
⇒ **两者对下游的含义完全不同,而今天一个 `tier` 字段两种都表达不了。**

### 1.4 退避下限归谁 —— Allen 2026-09-08 已裁:⛔ 不归 PAC

本稿早先把它列成「节点级还是图级」的待定项。**Allen 裁定:两者都不是。**

> **「这不是 PAC 要做的事情,而是派发 PAC 的 agent 或者人要做的事情;
> PAC 机制本身只是【记录】并将这个 context【传递】。」**

⇒ 与概念稿 §1 的三层一致:**反应器只看 flag,⛔ 不读回复正文、不判内容。**
⇒ **PAC 不判断「该降到哪一档」,它记下派发者的判断,并把它传下去。**

**⭐ 而这条裁定把 §1.3 那面承重墙【收进了 PAC 的本职】,不再是附加需求:**
```
若 PAC 的职责就是【记录 + 传递 context】
⇒ 那么「实际用了哪个档」没有被记录并传给下游验收者,
   就不是「少了个功能」,而是【PAC 没做到它的本职】
⇒ §1.3 与本节是【同一句话的两个方向】
```

**⇒ 于是设计问题也换了形状,而且换成了可回答的那种:**
```
⛔ 旧问法:谁决定退避下限(节点 / 图)     —— 已裁:都不是,归派发者
✅ 新问法:PAC 的节点上【要能记下什么】,才够派发者表达他的判断、并让下游读懂
   至少要能记:① 要求链(claude/opus → A 级 → B 级 → 报错)
              ② 实际落到哪一档,以及它是【被迫降级】还是【计划内低档】(§1.3)
              ③ 谁做的这个判断(派发者身份),以便下游知道该信谁
⇒ 这是【表达能力】问题,不是【决策权】问题
```

## 2. 现状读数(dev `09ff4a3248b0`,intg/pac-graph `a270b01c`)

### 2.1 实体与持久化:已经存在

```
class Agent(src/hyprial/agents/registry.py)
  -- identity(this class is the sole owner)--  uri · actor · owner · machine
  -- configuration independent of any harness -- cwd · provider · model · capabilities
  -- per-harness launch arguments; NOT "which harness is current" -- harness_args · preferred_harness
  -- last session clues(A9 handover)--         last_harness · last_session_id
  pinned_adapters(读时从 pins 表组合)· created_at_ms · hosted_by

CREATE TABLE agents(actor PK · owner · machine · uri UNIQUE · cwd · provider · model
                    · capabilities · harness_args · preferred_harness
                    · last_harness · last_session_id · created_at_ms)  + hosted_by(迁移加)
CREATE TABLE pins(adapter UNIQUE · agent UNIQUE REFERENCES agents(actor) ON DELETE CASCADE)
```

### 2.2 ⛔ 而类自己的断言现在是假的

docstring 原文:
> *"Contains no runtime state whatsoever: **not the harness currently running it**, not its pid,
> not whether it is online."*

**实测**:`src/hyprial/daemon/application.py:6216-6230`,**每次 session 注册**都
`runtime_capabilities(harness, interactive=…, provider=…, model=…, args=…)`,
并把产出写进 `capabilities`(create 时直接传;已存在则并进去)。
该产出含 **`harness` · `interactive`** · tier · proactive_send · tool_injection。
⇒ **`capabilities["harness"]` 与 `capabilities["interactive"]` 恰恰是 docstring 点名说「不在这里」的那两样。**

### 2.3 ⛔ PAC 今天持有的是「怎么运行」,不是「身份」

```python
class LaunchSpec(src/hyprial/pac/lifecycle.py):
    harness: str          # ← 第一个字段
    provider · model · cwd · args · tier · probes
```
且在 `src/hyprial/pac/` 下搜 `AgentRegistry` / `agents.create` / `agents.get`:**代码命中 = 0**
(仅三处注释在说边界:*"outside the frozen daemon-IPC registry"* / *"without instantiating the registry machinery"*)。
⇒ **PAC 起的是【进程/运行实例】,⛔ 它不创建也不删除 `agents` 表那一行。**

**⇒ 真实形状(比"某处疏忽"严重):**
```
目标:PAC 持【身份】· daemon 持【运行】
现状:PAC 持【运行】· daemon 持【身份 + 运行】
⇒ 两层【都】朝运行那边偏,而身份只在 daemon 一侧
```

### 2.4 顺序按入口区分；先建身份与无运行残留分别验收

**撤回原来的全称「进程先起 → session 注册 → daemon 才建记录」。**
在 dev `09ff4a3248b07626edc2fa832def0d38177b745c`：

- daemon managed CREATE 的前向计划为 `agent.create → route.persona.ensure → agent.bind → harness.ensure → [仅 spec.session 非空] session.register → route.managed.ensure`。出处：`src/hyprial/daemon/lifecycle_manager.py:1179–1209`；`:1247–1249` 将 CREATE 接到该计划，`:917–935` 顺序执行，`:1083–1091` 逐步等待完成回执。
- `hyprial start` managed headless 在 `src/hyprial/cli.py:5660–5668` 先调用 `_create_agent_for_start`，`:5690` 才请求 `lifecycle.start`。CLI 托管 TUI 的创建／Popen 站点分别为 claude `4619/4834`、pi `5126/5217`、codex `5327/5374`。
- 外部已运行会话直接 `session.register` 时，`src/hyprial/daemon/application.py:3573–3588` 调 `_ensure_agent`，可补建或更新记录；不能把这个兼容入口概括成全部启动路径。恢复入口另列验收，不能以注册事件作为每条路径都有的锚点。

以上是冻结源码的执行路径证据，**不是 OS 进程时间线实测，也不证明全部崩溃／并发／恢复路径正确**。
三个文件的 blob 依次为 `b42a05832846a19d842c5753b5bc2879e503188e`、`9e1a8590e8c5a1a865ca3616c8603bcda62a3e7d`、`68a5a8421b9fb9381dc1e3f6702423beb37923f4`。

`agent.create` 路径（application.py:4856）也会调 `_ensure_agent` 保存 capabilities。
因此 **「身份先耐久存在」与「实体不混入运行信息」是两条独立验收**；前者不能替代后者。
分入口的落盘、完成围栏、失败路径及 session 锚点见 [完整验收方案](../reference/acceptance-agent-identity.md)。
本次勘误依据 hq `90e0f119afadd944741ae1d146a182bfb7c68343:notes/spec-agent-identity-acceptance-2026-09-08.md` 与 hq-adjutant 的采纳回执 `1961004d-8dc3-57a6-a2d7-333758571af1`。

### 2.5 ✅ 回收这一格基本到位

```
PAC:  _request_down → _reconcile_down → _complete_down · _retire_stale
      ActorRuntime port = observe / start / stop,带 operation_id 与 identity_marker
      模块 docstring:"a reconciler, not another process launcher";
      控制面【就是 daemon 的 start/down】
daemon:dev 上已有 #430 的孤儿进程 GC(orphan_processes.py,四格活性)
```

## 3. `config` 字段(Allen 2026-09-08 提)

> 「skill 实际上应该有一个独立的字段(config)。当前我们启动的 agent 往往都**隐含了**调用系统 config
> 和 cwd 中 config(例如 `.claude/` `.agent/` 目录),未来需要**显式**地指定。」

**⭐ 而这条提议有一个它自己没说、但更硬的理由,写在这里:**
```
今天 cwd 一个字段承担了两个含义:
  ① 在哪个代码仓库干活(任务的对象)
  ② 从哪里读配置/skill(agent 的能力来源)
⇒ 两个量共用一个标签 ⇒ 误读不会报错,而标签会替错读数背书
⇒ 更糟的是:今天 agent 能干对活,部分是因为它【恰好】被起在一个有 .claude/ 的目录里
   —— 这是一条【靠位置性事实活着的保护】,它失效时不会有任何提示
```
⇒ **拆出 `config` 之后,「读哪份 skill」从【位置的副作用】变成【实体上的一个可核字段】。**
⚠️ **G6 更正(2026-09-14):** 上面的「② 从哪里读配置/skill」在分层下要拆开 —— **个性配置层**
不再从 cwd 读(这正是本段结论的射程);**项目配置层**照常随 cwd 自动读,是 G6 判定的**正确行为**,
不是「靠位置性事实活着的保护」。⛔ 不改项目层随 cwd 的加载渠道(见下方更正块)。

Allen 后续裁定 `config` 是**一个单独的显式路径**：需要的材料由派发 PAC 的 agent/人
事先复制进该路径，系统 config 与 cwd 内 `.claude/`、`.agent/` 不再隐式加入（⚠️ **G6 之前的形态**：
「与 cwd 内……不再隐式加入」的射程已被 2026-09-14 G6 收窄到个性配置层，见下方更正块）。
持久形状为 `{sources: [恰好一个元素], discovery: "explicit-only"}`；列表顶层、多路径与
解析链形状必须拒绝并点名违规项。加载清单与内容摘要仍在运行前固定、加载后核对。
（P21 已从 `intg/agent-identity-config@4adc5a8e` 按源移植 `AgentConfig`/
`ConfigManifest`，并接当前 registry/ports/CLI/SQLite；native projection/receipt
内核已冻结但未默认启用。三家真实 loader/auth 验收仍属于后续切片，不能从本句推出。）

#### G6 更正（2026-09-14）：单路径只约束个性配置层，项目层随 cwd

Allen 2026-09-14 G6 两句原话（逐字）：

1. 「我赞成B，agent自己的独立目录维护的是自己的“个性”，项目相关的内容放在具体的项目cwd中，如何组合则是派发任务时pac编写者的职责」
2. 「是的,“派一个 worker 去 harness-bridge 仓干活”,显然应该自动读取那个仓的配置,“worker 启动时屏蔽” 指的是屏蔽使用系统/user的.claude/.agent 等系统级文件夹,而使用该agent自己的配置文件夹,以免被系统环境内容污染」

- **个性配置层**：唯一来源是该 agent 自己的配置目录（推荐 `C=A/config`）。09-09 单路径裁定（4adc5a8e）继续约束**这一层**：恰好一个显式绝对路径、材料先复制进去、禁止从用户级/系统级全局目录（`~/.claude`、`~/.agents`、`~/.codex`、`~/.pi` 等）隐式加载。
- **项目配置层**：仓内 `CLAUDE.md`、`AGENTS.md`、`.claude/`、`.pi/`、`.codex/`、`.agents/skills` **随 cwd 照常自动读**。选哪个仓、个性与项目内容如何组合，是派发任务时 PAC 编写者的职责。
- **屏蔽对象只有用户级 / 系统级**（含越出仓根向上到 HOME 的祖先文件，如 `~/CLAUDE.md`、HOME 级 `.claude`）。⛔ 不包括项目层。
- 冲突处置沿用各 harness 原生的项目覆盖用户优先序；C 站在被替换的「用户级」槽位上，不由 daemon 另造一层 merge。

⇒ 上文 §3 的 09-08 原文、以及下面 §4 表格里同源的旧表述（已就地改为「用户级/系统级」），都是 **G6 之前的形态**，保留不删。

出处与对应设计章节：#510 设计 `design/agent-home-dirs@e0e410c2` §4.2（分层与冲突规则）、§4.2.1、§8 的 T09 / T18。
验收侧更正落在 `docs/reference/acceptance-agent-identity.md` §0.4 / §4.1 / T-C02 / T-C03。

## 4. 距离与顺序(⛔ 本稿不实施)

| 格 | 距离 | 动什么 | 不动什么 |
| --- | --- | --- | --- |
| 回收 | ✅ 已到位 | — | — |
| 顺序 | ⚠️ 分入口验证，非全局倒序 | 受管入口保留并验证既有先建机制；外部 session 接入与恢复补建分别验收；具体入口先给反例再定修法 | ⛔ 不重排整个 managed CREATE，不让 PAC 接管启动；session.register 不是通用锚点 |
| 运行期字段 | ⚠️ 中等 | 把 `capabilities` 里的 harness/interactive/tier/proactive_send/tool_injection 挪进一张由 daemon 写、按 daemon generation 归属的运行期表 | ⛔ 不需要 PAC 越界 |
| 谁持有什么 | ⛔ 最远 | PAC 节点上的 `LaunchSpec` → `requires` | ⚠️ 会与 probes / digest / operation identity 的现有用法相撞 |
| `config` 字段 | ⚠️ 新增 | 实体加单独显式路径 `config`（**个性配置层**）;启动路径显式传;⛔ 不再依赖**用户级/系统级**的隐式副作用。**项目配置层随 cwd 照常自动读**（G6，见 §3 更正块） | ⛔ 不加多路径或解析链;⛔ 不关项目层发现 |

⭐ **一条已经开了头的事实**:Allen 2026-09-08 裁的 route (a) 保留 `requires.tier`、
拿掉 role/context_min/proactive,**方向与本设计一致 —— PAC 只说「要什么」,不说「怎么起」**。
`requires.tier` 是已经跟上的那一半;`LaunchSpec` 是还没跟上的那一半,两者今天**并存**。

## 5. 本稿【不】做的事

- ⛔ 不实现任何一格;⛔ 不改 `agents` 表;⛔ 不动 PAC 与 daemon 的分层。
- ⛔ 不重新扩展已经裁定的单路径 `config` 为多路径或解析链;⛔ 也不把项目配置层拉进这条单路径合同（项目层随 cwd，G6 见 §3 更正块）。
- ⛔ 不定「PAC 节点要能记下哪些字段」的具体 schema(§1.4 的新问法)。
- ⛔ 不假设一个 `tier` 字段能同时表达【被迫降级】与【计划内低档】(§1.3)。
