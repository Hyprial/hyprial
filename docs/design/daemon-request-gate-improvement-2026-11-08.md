Skill-Impact: not-needed: 本改动新增 CI 静态门与测试，不改变 agent 的调用方式。
E2E-Impact: not-needed: 本 PR 不改变 daemon request 契约或场景行为；OpenAPI 行为到场景声明的联动门列为后续包。

# Daemon request 客户端边界门禁

状态：`#2587` 的实现说明。本文描述当前提交中可执行的检查及其证据边界。
基线：`c61bd54b619666da9d1edb860c8ae6daa4ebda24`。
目标分支：`dev`。
Rebase：本 PR 已基于 #2606 合入后的 dev 重建；method-registration 实现采用 dev 中 #2606 的版本，本 PR 不再修改该检查、其测试或旧的 router guard。

## 1. 目标

CLI、Web GUI、Native GUI 以及 GUI adapter/BFF 应只完成：

`argv/form/stdin → 参数序列化 → daemon request → receipt → CliResult/view model`

业务校验、授权、默认值、跨请求不变量、业务 ID、跨方法写编排、durable operation、
恢复语义和业务 read model 由 daemon 负责。把一个业务状态机从浏览器移到 Node、Rust
或 Python 的 GUI 后端进程，仍然属于客户端业务。

本 PR 建立可执行、fail-closed 的静态边界门，使客户端绕过 daemon、增加未声明方法或
扩大本地业务编排时不能静默通过。它不迁移存量业务，也不替代契约测试和 E2E。

## 2. 交付范围

### 2.1 Python/Native 架构扫描器

`scripts/ci/check_daemon_request_architecture.py` 扫描：

- `src/hyprial/shell/impl/cli/commands/**`
- `src/hyprial/shell/impl/gui/{live,adapter,backend/onboarding}/**`
- `clients/native/src-tauri/src/**/*.rs`，排除测试代码

当前 finding 类型：

- `backend-import`、`authority-import`：客户端直接依赖 daemon/biz/identity、SQLite 或
  外部 SDK。
- `file-mutation`、`process-invocation`：客户端本地状态写入与进程副作用。
- `undeclared-method`、`dynamic-method`：请求方法不在当前 OpenAPI catalog 中，或无法
  静态确定；扫描器识别部分 `method` 参数转发包装器。
- `request-fanout`、`request-in-loop`：单函数多请求与循环请求，用于暴露潜在客户端
  saga；finding 本身不等于最终语义判决。
- `command-without-request`：含 Typer command 但文件内没有可识别请求。
- `native-cli-invocation`、`native-checkpoint-write`、`native-process-invocation`：
  Native 端 CLI、checkpoint 和进程调用点。

账本为 `scripts/ci/daemon-request-architecture-baseline.json`，当前审查规模为 606 行，
规范内容 SHA-256 为：

`37a29315ac33988c74e004f00e402256bb71342398d30a5aff3c1e06007ba4eb`

#### cfg(test) 剥离与受限例外

Native 扫描先按顶层项类型判断终止符，再决定是否剥离：带 body 的 inline
`#[cfg(test)]` BLOCK 项（`mod NAME { ... }`、`fn ... { ... }`、`impl`/
`struct`/`enum`/`trait`/`union`）被跳过；以顶层分号结尾的 `#[cfg(test)]` 项
（`mod NAME;`、`use ...;`、`const ...;`、`static ...;`、`type ...;`、
`struct NAME;`、无 body 的 `fn ...;`）原样保留，扫描从该分号之后继续，不越过
分号向后搜 body。保留是有意的保守选择：只可能多计，不可能少计。这份终止符
感知的剥离修的是复核 finding P2-1（见 `/private/tmp/eosr-arch-ratchet-review.md`）。

`REVIEWED_NATIVE_EXCEPTIONS` 的六个条目是精确的 `(kind, path, subject)` 三元组，
以 count 封顶，只在 `check()` 中应用；`scan()` 与 `write_baseline()` 保持 raw。
超出上限的出现次数仍是 violation；站点消失后须删除对应条目。

因为例外在 `--base-ref` 比较之前削减当前 findings，这六个已登记站点按设计不会
触发 `base-architecture-growth`（复核 P3-2 的副作用记录）。`check()` 的 JSON 输出
报告削减后的 findings/summary，并额外报告 `rawSummary` 与 `reviewedExceptions`
（复核 P3-5）。

### 2.2 TypeScript 架构扫描器

`scripts/ci/check_gui_request_architecture.mjs` 使用项目 TypeScript compiler API 扫描
`gui/src` 与 `gui/server`：

- `direct-network`、`browser-storage`、`node-authority-import`；
- `server-business-module`，仅允许 daemon-request transport 与 reference fixture；
- `transport-bypass`、`undeclared-method`、`dynamic-method`；
- `request-fanout`、`request-in-loop`。

扫描器排除 `*.test.*`、`*.spec.*`、声明文件与 `__tests__`。MethodSpec 声明、accessor、
constructor 和各方法本身分别归属；同一对象上的 sibling methods 不再被折叠成一个外层
owner。transport layer 内由参数驱动的通用 `invoke(method)` 不按动态方法误报，普通业务层
里的同类调用仍会失败。

账本为 `scripts/ci/gui-request-architecture-baseline.json`，当前审查规模为 35 行，
规范内容 SHA-256 为：

`3fed7394061f01959dfd05e0e0691452beb6530c9eea255cf3d28b38bc938319`

当前分类为：`browser-storage` 3、`dynamic-method` 3、`node-authority-import` 5、
`request-fanout` 24、`request-in-loop` 4、`server-business-module` 8、
`transport-bypass` 3。

### 2.3 账本与首次采用

两份扫描器使用相同规则：

- finding 必须完整存在于已审查账本中，超出的 finding 使检查失败。
- 删除 finding 后，账本必须在同一 PR 缩小并更新 reviewed digest。
- base ref 存在但账本不存在时，首次采用只允许发生在 `dev` 或 `main`；PR 必须显式传入
  `--allow-first-seed`，且账本 digest 必须匹配源码中的 reviewed digest。
- base ref 不存在属于 `invalid-base-ref`；缺少账本且未授权首 seed 属于
  `missing-base-ledger`，两者都失败。
- 首 seed 不依赖固定 accepted SHA。账本一旦进入受保护分支，后续删除或增长都不能
  重新利用首 seed 放行。
- base/head 比较直接读取 base ref 的账本。即使 head 同时修改账本以隐藏增长，检查仍会
  比较真实 base 与当前结果。

### 2.4 Method registration 误报修复

`scripts/ci/check_method_registration.py` 原先会把
`src/hyprial/daemon/impl/adapters/` 中 Slack 等上游 provider 的
`chat.postMessage`、`files.upload` 等方法当成 daemon IPC 方法，并产生 legacy shadow
误报。修正采用两条窄规则：

- 只排除 `src/hyprial/daemon/impl/adapters/`；adapter 外的同名 bridge 仍须登记。
- 对 `MIGRATED_METHODS` / `MIGRATED_FAMILIES` 的 fail-closed raise 不再视为 legacy
  shadow；若同名分支能继续执行 legacy route，测试仍要求 FAIL。

`router.py` 的 `upgrade.steps.run` legacy 分支也补上与相邻 guard 相同的
`method not in METHOD_REGISTRY` 条件，使 registry 项不会同时进入 legacy 路径。

同一路径上的 `test_bound_link_queries.py` 仍从 parity 测试导入已迁走的 `_ipc`
helper，导致 daemon-request CLI 测试在收集阶段失败。该测试改为从
`tests/integration/cli/isolated_daemon.py` 导入当前共享 helper，测试行为不变。

### 2.5 required CI 接线

`gate-python.yml`：

- 只要 full diff 需要检查，就获取真实 base；Python-only 之外的 GUI/Native 变化也不能
  绕过。
- 在 gate 中运行 Python + GUI adapter + Native 架构扫描。
- 使用 `CONTRACT_BASE_REF`、`CONTRACT_BASE_BRANCH` 与首 seed 标记，保持 fail-closed。
- seed SHA 的读取在安装 Python 依赖之前使用 `uv --no-project`。

`gate-gui.yml`：

- 获取真实 base 并生成 `GUI_ARCHITECTURE_*` 环境变量。
- 运行 `client:reference:check`、`client:fixture:check` 和 Node 架构负例。
- 运行 TypeScript 扫描器，并将参数传给现有 `gui-unit` required job。

新增 workflow step 已同步现有 CI policy、scope、command 和 workflow wiring 测试。

## 3. 证据边界

这些检查回答的问题是：

> 客户端是否绕过 daemon、增加未声明方法或增加本地业务编排？

它们不能证明 daemon 行为正确，不能证明冻结契约兼容，也不能证明 isolated E2E、
controlled E2E、真实 provider 或浏览器场景通过。

- 静态 `PASS` 只表示当前 finding 与已审查账本一致。
- unit `PASS` 只表示被覆盖的扫描器逻辑符合负例。
- `client:reference:check` 与 `client:fixture:check` 只表示生成物和示例未漂移。
- E2E inventory `PASS` 只表示场景声明完整；场景执行结果必须另列 tested SHA、PASS、
  FAIL 或 BLOCKED。

## 4. 与 #2580 / #2588 的关系

本 PR **不实现** OpenAPI 行为变化到 `E2E-Impact` 声明的联动门。该后续门应复用
`check_daemon_contract_diff.py` 的 merge-base 与结构比较，并依赖：

- `#2580`：E2E readiness、场景漂移和最终执行矩阵。
- `#2588`：`E2E-Impact` 声明格式、scenario inventory 与同步检查。

后续 Gate 的触发条件应是 daemon request OpenAPI 的方法、字段、错误、enum 或示例语义
发生行为变化；纯 annotation 变化不触发。PR 必须声明以下之一：

- `E2E-Impact: not-needed: <为什么不需要修改场景>`
- `E2E-Impact: updated: E2E-003,E2E-007`，并同步对应 `contract/e2e/` 文档、runner
  或断言

声明不能替代真实执行。isolated/controlled E2E 仍由各自 workflow 与 #2580 的最终矩阵
决定。

## 5. 后续阶段

P2，另包交付：

1. 合并 `#2580` / `#2588` 已审查的声明和 inventory 形态。
2. 实现唯一一套 OpenAPI 行为 diff 与 `E2E-Impact` 关系检查，不复制契约 diff 语法。
3. 为正负例、缺失声明、未知 scenario ID 和声明但未同步场景文件建立 fail-closed 测试。
4. 将 inventory 结果与真实 E2E 执行结果分开报告。

P3，按业务风险迁移存量客户端逻辑并缩小账本：

1. CLI agent/session 运行时。Python 账本整体为 606 条，其中最大热点
   `agent/runtime/sessions.py` 为 50 条，`claude.py` 为 24 条、`dsh/launch.py` 为 25 条，
   `interactive.py` 为 24 条；先处理 credential seed、session 选择和本地 process/file
   副作用，避免把独立的 agent harness 生命周期搬进 daemon。
2. 其他高风险 CLI 编排。`agent/grant.py` 21 条，adapter admin 19 条，upgrade、transfer、
   daemon start/stop、workflow 和 squire 路径各有 12–19 条；按 MethodSpec 与 daemon
   read/write model 分簇迁移。
3. PowerPack/Node setup 的重复 saga 与 checkpoint；GUI 最大热点
   `gui/server/setup/checkpoint.mjs` 为 3 条，`setup/daemon.mjs` 为 2 条。审查
   `management.squire.prepare` 是否应进入生产 registry，而不是继续复制状态机。
4. GUI 业务编排。`features/org/domain/live-source.ts` 10 条，
   `app/capabilities.tsx` 6 条，`features/account/connected-account-actions.ts` 6 条，
   `features/user-settings/read-adapters/index.ts` 5 条；由 daemon 提供 plan/read model
   与带版本前置条件的写方法。
5. 最后处理 Native setup 和剩余通用发现。Native 账本集中在 `setup/mod.rs` 的
   24 个 CLI/checkpoint 调用，应复用 daemon 的 setup/upgrade authority。

以上排序来自本轮账本，不是把 606 条 finding 全部视为产品缺陷。先以可验证的 daemon
MethodSpec 和 read/write model 缩小高风险簇，再处理剩余迁移债务。

新客户端能力应先有 daemon MethodSpec 与行为示例，再生成各端 client。

### P3 执行单元与交付门

P3 不应按“删除 finding 数量”直接开工。每个执行包先选定一个有业务边界的 request
簇，列出当前调用方、已有 MethodSpec/handler、缺失的授权与 durable operation、
参数/结果/错误/读写语义，以及确实属于本机生命周期的例外。契约边界明确后，业务 owner
实现 daemon 侧能力，本线审核公共 request 面与 CI；客户端接线单独交付，不能用一个
“迁移全部 CLI/GUI”大 PR 隐去中间语义。

1. **P3-0 契约盘点，不改账本。** 从 CLI agent/session 运行时开始，形成
   `client call → MethodSpec → registry handler` 对照和最薄切片。盘点必须区分可由
   daemon 管理的业务状态与本机 harness process/file 控制；后者保留时必须修正 scanner
   的窄分类并增加负例，不能直接扩大 reviewed baseline。
2. **P3-1 CLI agent/session 最小切片。** 优先迁移 credential seed、session selection
   和可版本化的 read/write model。每个行为 PR 同时加入 `MethodSpec`、真实 handler、
   generated client/OpenAPI 更新、客户端薄化及其焦点测试；账本在同一 PR 精确缩小并更新
   reviewed digest。验收要求 CLI 文本、`--json`、stderr 和 exit code 保持契约，隔离
   daemon 下覆盖重试/unknown/receipt，不访问真实凭据。
3. **P3-2 CLI 管理编排。** `agent.grant`、adapter admin、upgrade/transfer、daemon
   start/stop、workflow 和 squire 按 request 簇拆包。先补 daemon plan/read model 与
   operation precondition，再接客户端；不得为了通过 scanner 把这些业务状态机改藏在另一个
   Python/Node/Rust client process 内。
4. **P3-3 setup。** 收敛 PowerPack/Node/GUI/Native 重复的 setup 与 checkpoint saga。
   先决定应由 `management.squire.prepare`、现有 setup 方法还是新的版本化 operation
   承担，再同步 GUI、Native 和各入口；恢复、断点续作和重启语义必须有隔离实测。
5. **P3-4 GUI 业务编排。** 将 org live-source、capabilities、connected-account actions
   与 user-settings read adapters 消费的聚合/写序列下沉为 daemon read model 和带版本前置
   条件的写方法。GUI 只保留展示、交互和 transport，静态扫描、生成物检查与浏览器焦点测试
   分开报告。
6. **P3-5 Native 与关账。** Native setup 复用已冻结的 request 面；剩余账本逐行有
   owner 和删除条件。只有两份账本都缩到零、scanner 对零 finding 全量 fail closed，且
   客户端生成物、隔离 daemon、适用 E2E 与真实入口组合验收通过，才移除 reviewed
   baseline。在此之前 baseline 是迁移边界，不是产品合规证明。

每个执行包的合并门是：新增架构 finding 为零；目标 finding 实际减少；scanner 负例证明
旧回退会变红；MethodSpec/handler/OpenAPI/generated client 同 SHA 对齐；错误、read/write
和恢复语义有测试；全仓结构/边界/Ruff 与精确 PR head CI 通过。行为变化涉及的 E2E 场景
另列真实执行结果，unit/静态 PASS 不能替代。

## 6. CI 收口顺序

`#2587` 只完成第一层静态边界门。daemon request CI 的完整收口按以下顺序，不把各层证据
混写：

1. **本 PR：静态边界与 shrink-only 账本。** 防止新客户端绕过 daemon、未登记方法和本地
   业务编排增长；当前 606/35 行账本仍表示存量债务。
2. **E2E 影响声明。** 等 `#2580` / `#2588` 统一 schema、inventory 与 base-owned checker
   后，只在 OpenAPI 行为变化时 fail closed；声明只证明覆盖处置完整，不证明场景执行成功。
3. **逐包迁移。** 按 P3-0 到 P3-5 交付并缩小账本；每个包独立 review、组合和合入，
   不等待全部 M2/业务线完成。
4. **零容差关账。** reviewed baseline 归零并移除后，任何 scanner finding 都直接失败；
   同时保留 generated client、隔离 daemon、适用 E2E、安装入口和精确 head 证据。完成这一
   步才可称 daemon request 架构 CI 完整，而不仅仅是 `#2587` 完成。

## 7. 交付判据

`#2587` 改标 ready 前必须满足：

- Python/Native 与 TypeScript 扫描器、全部负例和两份 reviewed digest 通过。
- base 增长、账本增长/删除、缺失 base、错误首 seed 分支均有失败控制。
- required CI 接线与 CI policy 测试通过。
- `client:reference:check`、`client:fixture:check`、`ruff`、结构门、模块边界与全量
  pytest 有精确 head 结果。
- PR 正文列出实际执行命令、tested SHA、结果和未执行项；未执行项写 `NOT_RUN`，未知项
  写 `UNKNOWN`。
- 远端 CI 在新 head 全绿后才可改标 ready。静态/unit PASS 不得写成 E2E PASS 或
  release GO。

## 8. 存量债务

本 PR 不修复 KP-028、KP-029、KP-036、KP-037，也不修改
`import-ratchet-exceptions.json`。账本记录迁移中的现状，不表示这些路径已经符合目标架构。

KP-028 的两条 `ipc_errors` import 应按现有精确 `protocol` exception 机制处理并更新
reviewed digest。不得改回契约门禁止的字符串、通过 support/runtime re-export 隐藏依赖，
或重生成 import baseline 求绿。修复后还需同时验证 import ratchet、canonical IPC error
门和相关 CLI 错误/回退契约。
