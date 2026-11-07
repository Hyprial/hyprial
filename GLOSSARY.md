# GLOSSARY.md — Harness Bridge 词汇表

本仓库的**规范术语表**。写代码、文档、提交信息、契约语料时以本表为准；
提新术语前先查"退役/易混淆"列。命名裁决史见 docs/design/design.md §2 与
docs/reference/naming-audit-channel-adapter-route.md。

## 产品与架构

| 术语 | 定义 | 退役/易混淆 | 锚点 |
|---|---|---|---|
| daemon | 持有传输会话、durable inbox、路由与受管运行时状态的长驻进程；消息与恢复的权威边界 | 不是"server"（去中心，无中心服务器） | src/hyprial/daemon/ |
| harness | 运行 AI agent 的宿主程序（Claude Code、Codex、Pi 等） | 不指本仓代码本身 | src/hyprial/harnesses/ |
| connector | 把一个 harness 会话接到 daemon 的进程管理+注入层 | ~~proxy~~（语义偏同协议转发，已弃用） | design.md §2 命名词表 |
| adapter | 外部平台（Lark 等）接入，映射平台身份到统一寻址/投递模型 | ~~gateway~~、~~room~~（已退役） | src/hyprial/adapters/ |
| channel | 群组会话空间（IRC 频道原语） | 不是"频道功能"，是协议层概念 | design.md §2 |
| link | zenoh 链路（仅实现文档使用） | 不进用户面词汇 | design.md §2 |
| PAC | programmatic agent calling：声明式派单与跟踪层（任务图、条件、等待规则），**不替 worker 执行** | 不是 workflow 引擎；workflow CLI 只是它的编译前端 | src/hyprial/pac/ |
| agent | pac + harness + model 的组合体；有持久身份/配置/存活 | 不等于 harness 进程（进程可重启，身份长存） | src/hyprial/agents/ |
| worker | 被 PAC 派出执行任务的 agent 运行实例 | 与 agent 的关系：worker 是役，agent 是身 | docs/design/design-pac-worker-lifecycle.md |
| sidecar | 由 hyprial 校验并控制的辅助进程（Tailcat 设备密钥与跨网络转发） | 不是 daemon 的一部分；随 wheel 分发（`hyprial/_bin/hyprial-tailcat`） | sidecar/hyprial-tailcat |
| squire | 面向"人"这一端的投递通道（user delivery） | 不是 adapter（adapter 接平台，squire 到 owner） | src/hyprial/squire/ |
| orgfs | 组织共享文件夹：spaces/docs/blob 的 mesh 复制，org 的底层架构 | org 是建在 orgfs 之上的组织上下文，二者不平级 | src/hyprial/orgfs/ |
| resident agent | 常驻 agent（keep 存活），对比一次性任务 worker | — | src/hyprial/agents/keep_actor.py |
| routine | 声明式周期 duty cycle：读看板标注产生一次性 PAC run，**不做判断** | 判断在看板标注里，不在 routine 里 | src/hyprial/routine/ |

## 消息面（inbox/transport）

| 术语 | 定义 | 退役/易混淆 | 锚点 |
|---|---|---|---|
| inbox / outbox | durable 收件箱（收件人本机落库）/ 发件箱（先落盘再投递） | 去中心：无服务端中转队列 | docs/design/design.md §3 |
| envelope | 消息信封（wire 上的统一消息单位） | — | contract/wire/golden/ |
| intent | 消息意图（request/reply/ack/event），决定唤醒与回执行为 | — | contract/wire/golden/expectations.json |
| wake | 投递触发的接收方唤醒 | wake ≠ reply | 同上 |
| custody transfer | 托管移交：mailbox 先落盘再签收（custody-ack），接管重投义务 | 不是转发，是责任转移 | docs/design/design.md §3 |
| mailbox | 志愿承担离线兜底的角色（任意 daemon 可担任，**角色而非特殊节点**） | 无需选主，冗余投递无害 | docs/design/design.md §3 |
| DLQ | 重试超限后的本机死信队列，doctor 可查 | — | docs/design/design.md §3 |
| presence | 上下线真值（zenoh liveliness 为真值，IRC 事件为播报） | 双源合一，liveliness 为准 | docs/design/design.md §2 |
| desired state | daemon 的期望状态存储；干净启动与崩溃启动走同一恢复路径 | — | contract/daemon-lifecycle/ |

## 重构结构（深模块）

| 术语 | 定义 | 备注 | 锚点 |
|---|---|---|---|
| kernel | 基础核心依赖：actor 模型、wire 词汇表、OS 原语 | 不允许出现在依赖环里 | docs/design/refactor-plan-deep-modules-2026-10-01.md |
| identity | agent / user 的封装（身份、配置、存活、registry） | 名词层；"是什么" | 同上 |
| daemon（层） | 平台层：进程生命周期 + 消息权威边界 + 全部能力域 | 动词层；"做什么" | 同上 |
| biz | 最外层业务封装（usage、quota、recap/hooks、routine 等） | 判据：删掉它平台核心承诺仍成立 | 同上 |
| shell | 人机交互接口（cli、onboarding、login、gui_*） | 依赖方向 kernel ← identity ← daemon ← biz ← shell | 同上 |
| interface / impl | 每模块的公共面（审定的 __all__ + lazy 转发）/ 私有实现 | 跨模块 import 经公共面，钻 impl 即违规 | 同上 |
| deep module | 接口远小于实现的模块（Ousterhout） | 本重构的方法论 | 同上 |

## 契约与验证

| 术语 | 定义 | 备注 | 锚点 |
|---|---|---|---|
| contract | 跨实现行为契约语料库：金样本 + 可执行裁判 + CI 门禁 | 不是 protobuf 类 IDL；类 Web Platform Tests | contract/README.md 语料地图 |
| golden | 冻结的期望行为数据（信封 fixture + 声明式期望） | 每条 case 带 sources 证据链 | contract/wire/golden/ |
| scenario | 可执行端到端流程（E2E-001…），带证据闸门 | 发版准入的载体 | contract/e2e/ |
| gate | CI 中 fail-closed 的门禁（不进白名单即红） | 白名单条目必须带理由 | tests/ci_workflow_policy.py |
| PASS / FAIL / BLOCKED | 验证三态；BLOCKED = 未执行任何断言 | **BLOCKED 不是 PASS，skip 不是结论** | ROADMAP.md |
| WORM | write-once-read-many：证据只追加不篡改 | notes/evidence 适用 | docs/notes/ |

## 仓库治理

| 术语 | 定义 | 备注 | 锚点 |
|---|---|---|---|
| ROADMAP.md | 长期目标与发版义务（原名 GOALS.md）：协调者/worker/通道全断时仍可被发现、看懂、执行 | 发版时过一遍；完成不删除、标 ✅；条目自含判据+命令摘要+红线，实测记录归 docs/notes/roadmap/ | ROADMAP.md |
| 失联韧性 | 工作原则：任何关键信息不得只存在于消息通道或某人记忆里，必须落仓库 | 本仓库协作方式的第一原则 | AGENTS.md、ROADMAP.md、design.md |
| uv 强制 | 仓内 Python 一律 `uv run`，裸 python 会被 hook 改写 | Claude/Codex 双 harness 生效 | .agents/hooks/ |
| 五桶 | docs/{design,guides,reference,domains,evidence}（收拢目标） | 证据归 docs/notes/（WORM） | 重构方案 §5.3 |
