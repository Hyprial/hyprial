# MethodSpec 并行补齐与评审安排（2026-11-02）

> 2026-11-08 集成注记：本文保留当时的分工与未完成状态；其中 release-first 路线已由
> [dev-first 发布规则](../../guides/release-rules.md) 取代。旧 SHA 的计数与测试结果不作为
> 当前 dev 候选的覆盖率或验收结论，冻结映射不覆盖现有产品声明及生成物。

用户确认：本线继续完成 MethodSpec，补齐 GUI 四条业务线分工之外的部分；CLI 迁移以后另开 intg。评审请求优先于本线实现协调。此安排替代旧“每个小片/返工都跑全仓”的执行要求，不改既有业务语义或最终验收门。

## 容器和所有权

沿用 #2257：`intg/daemon-request` → `release/v0.5.5.4`，产品基线 `84e3fd609ad426807c60b82e9e6215d2eb2ef0a5`。本次计划只追加文档和冻结 C00 盘点，不宣称产品已经合入。
GUI 使用 `intg/gui-v1`；其开工包明确排除 workflow 与 routine。共享方法以方法粒度登记，不能整族抢占。

- GUI 四线保留已分配方法实现；本线负责全局台账、公共规则、独立方法评审及未认领方法。user-settings 已联系但对方尚未承接，资源冲突须由用户/其协调方决定。
- C01/C02 完整候选已交 chat 线核对继承，源分支分别 `feat/c01-message-status-contract`、`feat/c02-pending-batch-contract`。不派第二组重复实现；继承后按新 SHA 评审。
- move 的真实 daemon 业务与契约由 B 线负责；Org/lifecycle 领域授权、状态机和公共接口归其业务方。本线记录缺口，不注册空 handler。
- 启动安装、显式离线维护、纯显示保留本地路径；不为覆盖数字强迁成 daemon 方法。

## 现在并行的两个模块批次

| 批次 | 方法 | 工作边界 |
| --- | --- | --- |
| C03 workflow 查询 | workflow.list / status / node.inspect / history.list / history.status | 真实查询/观察/历史 handler，保留参与者可见性与远端转发；写命令留后续批次 |
| C04 routine | routine.add / list / status / audit / remove / set / pause / resume | 真实 routine handler 与共享 caller/owner 检查；不改调度业务、不启动真实 agent |

两批分别使用 PAC 独立 worktree，execute 使用 fast tier，协调者用冻结提交独立 review。首个可核验检查点 20 分钟：方法/handler/消费者/错误/副作用表及第一条负控。执行超时 6 小时，评审节点 24 小时容纳外部优先队列；超时不是通过，不自动重建重复 worker。
图定义见同目录 `c03-methodspec-2026-11-02.json` 和 `c04-methodspec-2026-11-02.json`。当前覆盖与状态见 `methodspec-coverage-2026-11-02.json`。

## 协调者和共享文件

收到业务线评审立即安排固定 SHA 检查；worker 在各自 checkout 独立开发，不等待协调者实现。协调者维护台账、处理单路由 CI 候选交付，不占用长时间全仓验证槽。
共享 registry/router 仅允许追加自身绑定、移除自身旧分支和必要共同 authority 接点；不得同时做通用重构。两个方法包分别放 workflow/routine 子目录。合并串行，后合片 merge-forward，再重新生成 OpenAPI/client，不手工解决生成物冲突。

## 验证节奏

开发中：结构/模块边界早查，受影响焦点测试及真实隔离 IPC，合法 producer 结果、公开错误 code/data、身份与副作用必须覆盖。
模块完成：一次集中模块验证、必要 RED/GREEN、Ruff/契约/生成检查，固定 SHA 独立 review；证据未变可复用。
最终集成 head：集中全仓及适用跨实现验证、远端 CI 与独立验收。公开 HTTP 的方法另验准入/就绪与 Swagger；IPC-only 不为 Swagger 放宽元数据。所有测试与日常 daemon/账号/凭据隔离。

## 已知失败和剩余义务

C01 的独立焦点 36P/1 warning；完整隔离测试超过 4h38 后在 38% exit137，无正常终态摘要，途中 F/E 尚未归因。证据 `a23106ff588703cc9781446a91fee5d52a485ce9`。这是 BLOCKED/FAIL gate，不能说 PASS，不能凭猜测说与本批无关。
C02 `e32c56917ba99d975946fdc0e75cadd5f3440721` 为候选，作者 102 焦点通过；全量/live/cross-node 未验收。保留真实错误与 operator origin.onBehalfOf round-trip，消费读取不能当普通只读。
CI `f602a87151dc180406abf18d0cb6312c844d8175` 只有单片焦点接受（9 控制、15 焦点、Ruff、真实 registry），未因此合入 dev。已有证据复用；远端/集成交付单独跟踪。
C00 冻结盘点 `bae04bdaa4ab2ccb87b10e23e1b4a2fc0bfd6feb` 已独立接受；复制原字节供本容器使用。231 是入口数，不是待迁方法数；110 是该冻结盘点未解析语义数，未随归档清零。后续逐条加方法/业务 owner/消费者/证据/合入 SHA 才关闭关系。
HM-02 的 #2250 仍 Open，不因 GUI-v1 包含其内容而归档。旧 held PAC 不重启；卡片历史注记只追加更正。
