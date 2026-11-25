# CLI 收敛到 daemon request：盘点与分批开工计划

状态：方案已于2026-11-14获用户批准，第二阶段 ACTIVE。历史静态盘点和待决项保留作审计快照；当前批准记录、基线和进度以[执行 goal](cli-daemon-request-convergence-2026-11-14/execution-goal.md)为准。
截至实施基线 `25835754bab1994d1559fd97b9537ec86b21e7ef`，#2763 已合入，
用户已批准进入第二阶段；已合与进行中切片、当前读数、预测和验证状态见执行 goal。
下文 417 边、a/b/c、方法表、旧开工顺序和原待决条款保留为盘点时历史快照，
不得将协议 exception 减账解释为业务已迁移。

Owner：Allen。目标分支：`dev`。日期：2026-11-14（Asia/Shanghai）。
设计 PR：[#2763](https://code.hyprial.com/HyprialOS/hyprial/pulls/2763)。

## 1. 基线与范围

权威仓库：<https://code.hyprial.com/HyprialOS/hyprial/>。
盘点基线：`533ef27ca42e53230224d55e58ba074f93d0eb15`。
读取时 #2727 与 #2755 均 open、未合并；本盘点没有算入它们的预计增量。
交付前再次读取 dev 为 `b15393e3933b6eda6fbeffe503761876a77e6b0b`：#2755已合入，
#2727仍open。Forgejo compare显示新增差异仅为GUI启动设计文档，台账和产品源码未变；
本附件保持最初完整SHA，避免把未执行的新基线测试写成已执行。
#2727 合入后，实施分支必须基于最新 dev 重新核对 catalog、legacy、imports 与示例；
不能按其 PR 正文的“新增15、变更9”推算最终集合。

#2755 的设计文件尚未进入最初盘点基线 `533ef27c`，当时已从其 head
`d342f5d379aad86a5e86731f94d16696bc1e308c` 只读核对。
本批不修改 `gui/`、`src/hyprial/shell/impl/gui/`、
`contract/daemon-request/contract_mock/`、`clients/`、`packaging/`。
`commands/gui` 虽不是前述 implementation 目录，也只记录，避免与启动器改造抢写。
当前417边中没有落在禁改目录的条目；零 import 边不代表 GUI 或其他组已完成收敛。

| 文件 | 读取结果 | 口径 |
| --- | ---: | --- |
| `contract/daemon-request/openapi.json` | 138 / 102 | 完整 MethodSpec / HTTP paths |
| `scripts/ci/import-ratchet-baseline.json` | 417 / 470 | 精确三元组 / import occurrence 总和 |
| `scripts/ci/legacy-method-baseline.json` | 175 / 7 | 字面方法名 / 前缀 |
| `scripts/ci/import-ratchet-exceptions.json` | 20 | 既有精确例外，与417项无交集 |

实际 import scanner 同时覆盖 kernel、identity、daemon、biz：417项中分别为
129、53、223、12，不能将标题的“daemon/biz/identity”理解为可忽略 kernel。
覆盖 `commands` 之外的 `runtime` 和 `output`，共69个涉边文件。
KP-028 已在当前 known-problems.md 闭合；message history/reader 的 `ipc_errors`
已登记例外，不重复修复。旧设计的195方法、旧命令数及旧文件布局仅作历史背景。

## 2. 唯一边界与兼容要求

CLI：参数解析 → `services.runtime.request` / `execute` → `CliResult` / `CliStream`。
底层保持 `kernel/impl/ipc/client.py::daemon_request` 本地 IPC。
GUI 使用同一 MethodSpec 的 HTTP 投影，本任务不把 CLI 改为 HTTP。
业务校验、配置合并、授权、业务状态、跨方法写编排、重试/补偿、恢复语义属于 daemon。
允许 CLI 调多个 request 不意味着允许 CLI 持有业务事务状态机。

默认保持已发布文本、JSON、stdout/stderr、退出码、错误码和错误顺序。
`--json` stdout 恰好一个含 `ok` 的对象；`ok:false` 退出1；进度、警告和确认走 stderr。
旧 IPC handler 的未知字段容忍、coercion 和恢复期准入不得被新 schema 悄悄收紧；
HTTP-only paramsConstraints 不反向作用于 IPC。每个新声明都绑定真实 handler，并含
成功、空、错误示例；无自然空值的写方法用该域合法 no-op/不存在结果或错误例覆盖，
必须说明语义，不能伪造空成功或缺例。写响应丢失的 unknown 不自动重发。

不把业务搬到 kernel、共享 helper 或隐藏 re-export；不 skip、不 xfail、不扩 baseline。
普通声明迁移不顺带引入 preview operationId 等新确认语义；若确需改变，另作行为切片，
owner 决定后写 Contract-Impact、同步 contract/cli 并保存先失败后通过证据。

## 3. Import 全量分类

逐条原始三元组、count、源码导入/引用行、理由与迁移方向：
[审阅表](cli-daemon-request-convergence-2026-11-14/imports.md) / [机器可读数据](cli-daemon-request-convergence-2026-11-14/imports.json)。

- a：业务依赖，或未证明必要客户端职责的保守待删复核。173项中1项
  `runtime/services.py::tcp_probe` 未找到活跃消费者，单列待删，不声称其执行业务。
- b：仅类型、常量、错误词汇。纯 URI parser 不算 b。192项也不是192条永久许可：
  服务于本地业务的异常/类型必须随 a 路径一起删除，剩余真实协议消费逐项审查。
- c：标准参数/IPC/呈现职责和必要本地候选。52项中15项为正常客户端职责，
  37项是新增本地边界候选；均给精确条件，后37项须 owner 决定，不整组豁免。

| 命令组/公共层 | a 业务/待删复核 | b 类型/词汇 | c 客户端/本地候选 | 合计 | 涉边文件 |
| --- | ---: | ---: | ---: | ---: | ---: |
| adapter | 25 | 21 | 0 | 46 | 5 |
| agent | 52 | 54 | 7 | 113 | 14 |
| common | 8 | 15 | 14 | 37 | 4 |
| config | 5 | 5 | 0 | 10 | 1 |
| daemon | 2 | 11 | 5 | 18 | 4 |
| doctor | 6 | 4 | 1 | 11 | 2 |
| fs | 5 | 1 | 1 | 7 | 1 |
| gui | 0 | 0 | 0 | 0 | 0 |
| host | 0 | 0 | 0 | 0 | 0 |
| init | 5 | 4 | 2 | 11 | 2 |
| link | 0 | 5 | 0 | 5 | 2 |
| log | 0 | 2 | 0 | 2 | 2 |
| login | 7 | 9 | 4 | 20 | 2 |
| message | 0 | 7 | 2 | 9 | 6 |
| meta | 0 | 0 | 1 | 1 | 1 |
| org | 8 | 4 | 0 | 12 | 2 |
| output | 0 | 5 | 0 | 5 | 2 |
| profile | 0 | 3 | 3 | 6 | 1 |
| routine | 2 | 5 | 1 | 8 | 1 |
| runtime | 7 | 2 | 5 | 14 | 3 |
| service | 0 | 0 | 0 | 0 | 0 |
| socialware | 0 | 1 | 0 | 1 | 1 |
| squire | 9 | 12 | 4 | 25 | 2 |
| transfer | 1 | 0 | 1 | 2 | 1 |
| upgrade | 9 | 12 | 0 | 21 | 2 |
| user | 0 | 1 | 0 | 1 | 1 |
| work | 0 | 0 | 0 | 0 | 0 |
| workflow | 22 | 9 | 1 | 32 | 7 |
| **合计** | **173** | **192** | **52** | **417** | **69** |

记录 a/b/c 的单位为三元组；例如某条 count=4 只占表内一项。单条同时含本地 I/O 与
业务副作用时按 a。代码阅读发现并更正了7条过宽 c 判断；8条无状态 URI 函数由 b
移到 c。未通过删除台账、搬路径或机械按名称判类得到这些计数。

## 4. Legacy 方法与前缀

逐条领域、真实绑定/旧分派证据与处置：
[方法表](cli-daemon-request-convergence-2026-11-14/methods.md) / [完整 metadata](cli-daemon-request-convergence-2026-11-14/methods.json)。

| 方法领域 | 已同名声明 | 需声明现有操作 | 已证可删除 |
| --- | ---: | ---: | ---: |
| adapter | 8 | 0 | 0 |
| agent | 9 | 24 | 0 |
| autoupdate | 1 | 2 | 0 |
| dispatch | 0 | 1 | 0 |
| down | 0 | 1 | 0 |
| hosts | 1 | 0 | 0 |
| identity | 6 | 3 | 0 |
| lifecycle | 0 | 3 | 0 |
| link | 4 | 0 | 0 |
| management | 0 | 2 | 0 |
| message | 5 | 6 | 0 |
| network | 0 | 4 | 0 |
| org | 15 | 0 | 0 |
| orgfs | 30 | 0 | 0 |
| outbox | 0 | 2 | 0 |
| pac | 0 | 6 | 0 |
| ping | 0 | 1 | 0 |
| progress | 1 | 0 | 0 |
| ps | 0 | 1 | 0 |
| routine | 2 | 6 | 0 |
| service | 0 | 4 | 0 |
| session | 0 | 7 | 0 |
| shutdown | 0 | 1 | 0 |
| targets | 1 | 0 | 0 |
| top | 1 | 0 | 0 |
| upgrade | 2 | 0 | 0 |
| worker | 0 | 1 | 0 |
| workflow | 1 | 13 | 0 |
| **合计** | **87** | **88** | **0** |

175个旧账本名中87已存在同名真实 MethodSpec，88有分派证据但无同名声明；
另51个 registry-only 方法使完整 registry 达到138。scanner 报告226个名字，
恰为两个集合的并集，不是226个仍在执行的 legacy 分支。

87项优先复用；只移除已经证明不可达的旧路由，不能删除仍用于政策、错误或恢复期守卫的
同名常量。88项的当前建议是声明现有操作；若后续证明无消费者或全语义等价可再提删除，
本轮没有批准直接删除的完整方法。88不是承诺新增88个不同能力。
7个前缀逐条列为“精确绑定或等价拒绝完成后删除”，不是通配 MethodSpec；
`orgfs.` 当前是未知名拒绝，`identity.binding.`/`routine.` 包含权限选择，不能直接删。

必须保留的非等价案例：

| 现有操作 | 近似声明 | 尚未覆盖的语义 |
| --- | --- | --- |
| `down` | `agent.stop` | all/provider/adapter、残留 binding 清理 |
| `ping` / `ps` | `daemon.status` | 恢复期可达性及身份、网络、队列等字段 |
| `lifecycle.start` 等 | `agent.start` | interactive、resume、replaceInteractive、smolvm |
| CLI log | `log.page` | 多类筛选与完整记录，当前声明只有 limit/cursor 和精简字段 |
| `management.squire.ensure` | `management.squire.prepare` | 不是别名；prepare 目前有 SPEC 源码但未真实绑定 |

详细架构与源码依据见 [architecture.md](cli-daemon-request-convergence-2026-11-14/architecture.md)。
静态同名/行证据不代表逐方法参数、结果、错误、权限和副作用已完成行为等价验收。

## 5. 成本与并行批次（历史计划）

[逐命令组成本与施工安排](cli-daemon-request-convergence-2026-11-14/cost-and-waves.md) 包含当前涉边文件/家族文件数、
预计 CLI/daemon/catalog+contract/tests/ledger 文件范围、输出和错误码风险、新方法判定。
以下范围是盘点时估算，产品当时实际改动为零；当前状态见执行 goal。无边的 host/service/work/gui 也单列；service 仍有
未声明方法，不能因 import=0 就跳过。

| 波次 | 每 PR 的最小单元 | 前置与并行方式 |
| --- | --- | --- |
| W0 | 本设计与owner裁决 | 只读盘点、例外确认、#2727重基规则 |
| W1 | 一类 b 词汇；link 剩余边；socialware 错误词汇；service 四方法声明各独立 PR | b 先区分随 a 删除/真实词汇保留；不要从已登记20项虚报减账。meta 等本地例外确认后再做 |
| W2 | user/identity、fs/orgfs、message、workflow、routine、config、log 的独立薄切片 | 日志筛选缺口、长轮询、fetched副作用、caller授权需真实契约；一个命令组可再拆多个小PR |
| W3 | adapter、agent、org，随后 squire | #2727相关方法先重基；资源所有权/权限/会话恢复独立审，禁止CLI跨请求业务编排 |
| W4 | daemon、doctor、profile、init、login、upgrade | 生命周期/初始化/升级后置；先定义无daemon阶段、停机证据、锁和失败恢复 |
| W5 | 残余内部方法、共享CliServices消退和关账 | 最终业务执行边界审查；只在临时隔离目录验安装产物，不动禁改路径 |

架构及跨线协调用 astra；施工规划用 sol；具体施工用 terra；最后由 sol 验收。
独立命令组、handler 和测试可并行施工。共享 `runtime/services`、注册、output、catalog、
OpenAPI、两个台账及 reviewed digest 由单一集成人串行合入。每个后合 PR 更新到最新 dev，
重生成投影和台账、跑焦点和适用门，防止陈旧快照覆盖。通过仓库保护要求后按已授权范围
及时合入 dev；此句为盘点时的历史计划，后续已获授权及实际合入状态见执行 goal。

## 6. 精确例外与当时的 owner 待决（历史记录）

[逐项例外表](cli-daemon-request-convergence-2026-11-14/exceptions.md) / [条件和退出标准](cli-daemon-request-convergence-2026-11-14/exceptions.json)
列出现有20项和52条 c 的精确 path/module/symbol/count。它是设计台账，不是 CI 新许可。
主类包括：daemon自身进程/服务引导、首次初始化最低前提、安装/重启、终端本地资源、
profile选择、纯安装/本地探测/模板展示，以及正常IPC/语法/输出职责。
GUI启动器由 #2755 处理，本批只记录，不申请整目录豁免。

现有 `OfflineMembership` 明确允许 daemon 运行且没有 ownership fence；doctor 的
read_start_failures/read_degraded_redemption 当前无条件读持久诊断文件。公共装配项 I417 的多个消费者尚需逐一界定，已改为 owner 待审。不能将它们重述成
“只在 daemon 不在时且已上锁”。保留原条件供 owner 决定，不通过分类名称洗成安全例外。

待 owner 决定，必须记录具体批准 ID/条件：

1. 是否接受例外表的15项标准客户端职责登记与37项本地候选，哪些保留、收窄或转 daemon；
   尤其 terminal插件、profile创建、installation updates，以及上述既有在线/无锁条目。
2. 是否批准 W1 词汇/link/socialware 与 service 声明作为首批并行薄切片；
   W2以后逐切片冻结方法形状，禁止直接采纳旧设计的历史方法名。
3. 离线 config/routine plan、确认协议等无法保证零行为变化的切片，是否继续保持旧能力
   或接受明确契约变更；没有具体变更批准就后置。#2727合入后先刷新计数和消费者再施工。

用户任务明确“等 owner 确认后再进入第二步”；创建 WIP 和后续普通合并已获执行授权，
当时尚未获得上述具体方案/例外的确认；2026-11-14 用户已批准 #2763，实施条件见执行 goal。待决不是工具或技能要求新增的权限流程。

## 7. 门禁、digest 与验收

本轮改动仅 docs/design。sol 的[独立验收记录](cli-daemon-request-convergence-2026-11-14/verification.md)判定设计阶段 PASS。以下在固定基线及相同产品源码的设计工作树实跑：

| 检查 | 实际结果 |
| --- | --- |
| `uv sync --extra test --frozen` | PASS；锁定依赖未改 |
| `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` | PASS，0 violation |
| `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py` | PASS，0 violation；静态分析局限仍存在 |
| `uv run --no-project --python 3.12 python scripts/ci/check_import_ratchet.py` | PASS |
| `uv run python scripts/ci/check_request_contracts.py --base-ref 533ef27ca42e53230224d55e58ba074f93d0eb15 --base-branch dev` | PASS；import/legacy/OpenAPI均通过 |
| `uv run python scripts/ci/check_method_registration.py --base-ref 533ef27ca42e53230224d55e58ba074f93d0eb15` | PASS，138 registry / 226 observed names |
| `uv run python scripts/verify/dump_daemon_openapi.py --check` | PASS |
| `uv run python scripts/ci/check_daemon_request_architecture.py --base-ref 533ef27ca42e53230224d55e58ba074f93d0eb15 --base-branch dev` | PASS |
| 逐项集合/引用校验 | PASS，417边、175方法、7前缀一对一；无缺项或重复 |
| 产品行为、真实provider/TTY/GUI/升级/E2E | UNKNOWN；纯文档未执行，不用静态门代替 |

初次用 no-project 运行方法注册因缺 pykka 未能测量；uv sync 后重跑上表成功。
Cloud 环境无 fj/tea，内部 SSH 主机不可解析；Forgejo认证API已验证可用，WIP通过API保存。
这是 Git transport 的 BLOCKED，不是 request-contract 失败。未绕分支保护、未读生产配置。

每个实施 PR 正文必须给：import triples/occurrences 与 legacy methods/prefixes 的前后数值、
精确移除项、前后文件 SHA-256、真实 base/head SHA、所有实跑门及结果、UNKNOWN/BLOCKED。
适用 exception manifest 和 architecture baseline 变更要同步各自 reviewed digest。
`check_request_contracts.py` 的 A03 seed SHA/digest 验证历史不可变种子，**不随缩账重写**。
普通台账缩减由真实 base 比较证明；本轮固定字节摘要如下。

| 文件 | SHA-256 |
| --- | --- |
| `scripts/ci/import-ratchet-baseline.json` | `2ded53e923bb12f08d83a295647dc84d23beb76f3356c17b0fd97329871fe6b4` |
| `scripts/ci/legacy-method-baseline.json` | `c945b2896bedfc5978db381e87cdf1bb92dfd9c82bbab1c47acc07197ea358cc` |
| `scripts/ci/import-ratchet-exceptions.json` | `4c377e0949727cfb13156aadc06d876bbd166310347bdfbf617e4fea55eb38cf` |
| `contract/daemon-request/openapi.json` | `5e85ab76e2fb88ad971507dae63114a6aee63e6ef61f0bc8f27413673a8309ea` |

行为变更先失败再通过；等价迁移用既有输出契约与真实隔离 IPC 测试证明兼容。
按最新 AGENTS，src/hyprial 改动执行焦点测试后追加全量
`uv run --with libcst==1.9.0 pytest tests/` 与 `uv run ruff check .`；跑适用契约及隔离场景。
不为了已知问题扩大基线、skip或隐去FAIL，KP项只按当前状态引用，未测保留UNKNOWN/BLOCKED。
新方法还要核验示例导出供GUI后续单一JS mock消费，不修改正在退场的contract_mock实现。

最终完成：业务确实在 daemon 进程内；import baseline 无未登记业务边（按当前不重叠
exception机制应清零非例外基线，获准项独立登记），legacy清空或每个残留都有精确理由。
只减少表格数字、schema存在或前端调用了一次request，均不构成完成。

Skill-Impact: not-needed: 仅保存架构盘点和施工计划，不改变 agent 操作方式。
Contract-Impact: 文档阶段不改变 CLI、IPC、HTTP、JSON、退出码和错误契约。
E2E-Impact: not-needed: 文档阶段不改变产品行为。
