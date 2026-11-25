# Cell 与 daemon-request 的接口分工（待评审）

供 CLI 与 Cell 开发线认领和纠正。2026-11-24 12:44（北京时间）看板与远端 dev 均指向 `8ccaf513ec23de3bc39a53a1cc23cddf9fb34e15`。请在 PR 留下负责分支、已有 PR 或具体反例。

## 基线与实测

Serving daemon 接受 236 个 IPC 名称。174 个有 MethodSpec（101 写、73 读），60 个只有旧路由，2 个进程控制入口为 `ping`、`shutdown`。建议用 daemon 的业务行为与 MethodSpec 作为共享契约。旧路由仍可调用，说明退役前需要核对消费者及迁移成本。

HTTP 公开 130 个方法，与 GUI 生成表逐名相等，另有 44 个声明方法仅限 IPC。CLI 请求调用点找到 165 个不同字面量，其中 41 个为旧业务方法，另含 2 个控制入口。这是直接调用的下限。扫描未沿包装器传播方法名。`agent/runtime/start.py` 的 `_request_start` 另传入 `agent.start`、`lifecycle.resume.preflight` 和 `lifecycle.start`；CSV 单列这些已核实的位置。`sessions.py` 还有动态分派，实际消费者需逐个核对。7 个 provider 方法已有客户端、缺契约示例，分别为 `agent.provider.allow/revoke/status` 和 `user.provider.list/login/logout/rotate`。

隔离原生 daemon 与 HTTP 服务等共记录 525 条探针，执行 502 条，跳过 23 条。44 个仅 IPC 声明和 60 个旧路由经 HTTP 均返回 404。受信会话调用 `agent.secret.list` 和有效身份的 `conversations.list` 均返回 200。已退役的 `org.receivers.get/set` 在 IPC 返回 METHOD_NOT_FOUND、在 HTTP 返回 404。运行时 OpenAPI 与仓库快照一致。焦点测试 199 通过、1 项在 macOS 默认临时路径失败；该项在前后两个基线均复现，使用短 TMPDIR 均通过。双机 owner、真实飞书投递和完整套件仍待功能验收。

[逐方法清单](cell-methods-8ccaf513ec.csv)含固定基线的注册、调用面与隔离探针返回码。`methodspec_mutates` 对 174 个已声明方法逐名回读注册表，101 项为 True、73 项为 False。旧路由及控制入口在该列留空。空白表示未记录该项返回；参数/权限拒绝只能证明请求到达相应边界。复核可参考[架构边界](architecture.md)及[验证计划](verification.md)。这两份文档和同目录的 methods、exceptions 盘点固定在 `533ef27c`（175 项旧账本、138 项注册、226 项并集）。本稿的操作清单采用 `8ccaf513ec` 的 236 个 serving 名称。

23 项跳过包含 `down`、`lifecycle.start`、`agent.resume.restore`、`agent.ensure-home`、`autoupdate.trigger`。P02/P05/P08/P14 应按各自触达行为准备隔离 home、受控进程或投递目标，补有效请求、权限拒绝及副作用回读，再标验收完成。基线复核命令如下。

```sh
uv run --no-project --python 3.12 python scripts/ci/check_daemon_request_architecture.py --base-ref 8ccaf513ec --json
uv run pytest -q tests/daemon/ipc/router_only tests/daemon/ipc/contracts/test_method_specs.py tests/tooling/architecture/ipc/test_method_registration.py
```

## dev 合入后的增量验证

14:18（北京时间），远端 dev 已到 `ef7c2e26173d6e2dda59d71b775265af9fb2deb5`。它相对实测基线只合入 [I4a #3135](https://code.hyprial.com/HyprialOS/hyprial/pulls/3135)，16 个变更文件。该版本重新导入的 MethodSpec、旧路由、HTTP、GUI 集合逐名匹配清单，且运行时 OpenAPI 与仓库快照相等。对该提交改动的全部 8 个测试文件在隔离环境执行 pytest，**240 通过**。上节 502 条实际探针的结果属于 8ccaf513ec。

14:41，[S3c #3137](https://code.hyprial.com/HyprialOS/hyprial/pulls/3137) 合入 dev `ea728eebe46981fa53d00e05069abe438f91cf4f`。作者在该提交逐项核对了 174 个 MethodSpec 及其读写标志（101 写、73 读）、60 个旧路由、HTTP 与 GUI 的 130 个方法，并比对运行时 OpenAPI 与提交内快照，均一致。隔离运行涉及 profile 退役、能力存储、tiers 探测、投递与路由的 8 个焦点测试文件，**110 通过**。真实飞书和双机 owner 投递仍待实测。

## 与 Cell 在建分支的交界

| 功能与进度 | 开发线与交接条件 |
| --- | --- |
| S3a 已合入 [#3130](https://code.hyprial.com/HyprialOS/hyprial/pulls/3130) | receivers 公共面、存储与接线已销账，后续维持退役回归。 |
| S3c [#3137](https://code.hyprial.com/HyprialOS/hyprial/pulls/3137) 14:41 已合入 `ea728eebe4`，PR head `470ff781ba`，116 文件 | a-s3c 在同一个 PR 中交付运行能力缓存、owner 投递和 profile authority 退役。迁移指南已记录多机 IM 配置取舍，真实双机与飞书结果 UNKNOWN。两条 CLI 例外保留到将来的 `agent.tiers.probe`。 |
| I4a 已合 [#3135](https://code.hyprial.com/HyprialOS/hyprial/pulls/3135)。I4b [#3136](https://code.hyprial.com/HyprialOS/hyprial/pulls/3136) 在 `ea728eebe4` 上有合并冲突，PR head `1a153d34f6`，87 文件 | a-i4 在同一个 PR 中交付 user cell 附件、命名 route 发送和旧地址退役。保留 `adapter.routes` 管理与会话租约。新增的 `adapter.routes.send` 仅限本地运维经 IPC 调用，要求校验 sender。真实飞书投递 BLOCKED。 |
| channel 收尾由 a-chancap 在建，Cell 侧尚未报告已开 PR | 可基于已合 S3c 开发。合入仍依赖 I4b，待其就绪后清除 channel 项、`check_channel` 和 `CHANNEL_NOT_AUTHORIZED`。`visibility.py` 仍以 `GRANTABLE_CAPABILITIES` 校验其他 Agent grant，应保留有效项或先落等价集合，用 P03 回归验证。 |
| service grant [#3120](https://code.hyprial.com/HyprialOS/hyprial/pulls/3120) 为 WIP，`c334cf7f4c`，19 文件 | emergent-org-structure-refactor 确认负责实现。当前独立的 `service.grant` 信封与 Q4 的复用要求冲突，须先修订设计。等 Cell/channel 全部合入后依次交付 Q1 准入策略、Q2 成员 daemon 发放与 org owner 撤销/覆盖、Q3 owner 写设备 X25519 公钥、Q4 扩展既有 `agent.secret.grant`。 |

14:37 的 Cell 核对以 `ef7c2e2617` 为 dev。14:51 再查，dev 已为 `ea728eebe4`，#3136 的远端 head 仍为 `1a153d34f6`；看板上 `81fc0ceb9` 是组合测试头。Forgejo 报告合并冲突。作者在这两个提交上运行 `git merge-tree --write-tree --name-only`，得到四个冲突文件：

- `docs/reference/cli-command-inventory.json`
- `src/hyprial/daemon/impl/application/messaging/inbox_surface/send_targets/user.py`
- `src/hyprial/shell/impl/cli/commands/message/observe/targets.py`
- `tests/daemon/routing/test_route_attribution_targets.py`

I4b 由原开发线在新 dev 上解决冲突并重跑路由、发送目标与 CLI 清单回归。每个新实现 PR 仍限 49 个文件。进度见 [Cell 看板](http://[redacted]:18826/)，Q1–Q4 与收尾顺序见 [Allen 裁决](http://[redacted]:18826/reports/top/TRACK-C-QUESTIONS-FOR-ALLEN.md)。

#3120 的 CLI 草案扩展既有的 `hyprial org service` 命令组，IPC 接口在本机使用。P03 管的是 Agent 能力授权，Q4 处理的是密钥授权；两线对齐准入和审计约定。服务授权不能直接把旧信封切为四个 PR，还需设计与既有 secret grant 的衔接。

**待指定的实测**：cm-coord3 协调 owner 侧提供同一 owner 的两台带 Lark 机器和具名验收人，分别验证两台均配置 adapter 时的重复推送、单台配置时另一台缺少本机推送副本，并记录实际投递。#3120 的本地 Magpie → 远端成员 daemon 验收另行指定机对和负责人。Cell 对接人已向 hq-adjutant 提交转达请求，随后确认 cm-coord3 是终端会话，没有可投递的 Harness 地址，转达未成功。此项保留在本 PR，待 Cell 原有交接渠道或 owner 在看板指定验收机对和人选。

## daemon-request 与 CLI 的建议实施单元

P02–P15 列全 60 个既有旧路由。对每个保留入口先明确调用者、输入、输出、权限和副作用，再决定客户端与 HTTP 曝光面。每个新 PR 的实际变更最多 49 个文件，计入测试、文档、生成物与删除。

| 编号 | 单一功能 | 既有旧路由 | 预计文件数 | 验收或待裁决项 |
| --- | --- | --- | ---: | --- |
| P01 | Provider 契约示例 | — | 8–16 | 补 7 个已存在 HTTP 方法的成功/失败/权限示例；不重复造客户端或 IPC。 |
| P02 | Agent 查询与 home 准备 | `agent.get`、`agent.readiness`、`agent.runtime-context`、`agent.ensure-home` | 18–30 | 四项既有 IPC 均补 `MethodSpec`，绑定现有处理函数，均为 IPC-only。前两项只读，后两项有写入。验收现有 CLI 的结果、错误和副作用，以及四项经 HTTP 请求均返回 404。 |
| P03 | Agent capability grant 管理 | `agent.grant`、`agent.grants`、`agent.revoke` | 12–24 | 保留真实 grant ledger 语义，补 schema/owner/admission/幂等；不与 secret 或 provider grant 混为一谈。 |
| P04 | Agent home 迁移事务 | `agent.config.rebind`、`agent.migrate.preflight`、`agent.migrate.execute`、`agent.migrate.rollback` | 22–40 | 声明现有迁移事务，CLI 不重复校验或写存储；回滚、并发与 home 路径测试；默认 IPC-only。 |
| P05a | 停止与批量停机 | `down` | 15–32 | 先声明 all/target/agentsOnly、恢复期准入与清理范围。CLI agent stop --all 用 agentsOnly=true，只停 Agent；daemon 内核另有 adapter 等清理分支。agent.stop 与 Cell 重启共用 _ipc_down，保留该内核。单目标 CLI 迁 agent.stop 前比较参数、错误、结果与副作用。 |
| P05b | 启动预检与恢复 | `agent.start.preflight`、`agent.unblock`、`agent.restore-policy`、`agent.restore-threshold`、`agent.resume.restore`、`lifecycle.resume.preflight`、`lifecycle.start` | 20–42 | CLI 与 daemon 联合验证交互启动、恢复策略、重试与失败结果；包括 _request_start 包装器发出的有效请求。 |
| P06 | Runtime launch 租约 | `agent.runtime-launch.acquire`、`agent.runtime-launch.custody`、`agent.runtime-launch.release` | 14–26 | 声明租约权威、custody、释放与过期；CLI/harness 只传凭证，IPC-only，不增加 GUI 入口。 |
| P07 | Harness session 绑定 | `session.heartbeat`、`session.refresh`、`session.register`、`session.register_guarded`、`session.turn.ended`、`session.turn.started`、`session.unregister`、`worker.channel.request` | 24–42 | 声明 harness 内部协议、会话冲突拒绝与 worker channel；显式 IPC-only；现有 session.turn.refresh 不重复实现。 |
| P08 | Inbox 消费与结算 | `message.ack`、`message.reply`、`message.pending.list`、`message.pending.wait`、`message.read_body`、`message.settle_hint` | 24–42 | 保留本组 IPC-only；harness 使用 pending/wait/read_body/settle。围绕同一 durable inbox 声明读取/回复/ack/等待；与 pending.batch/history/query 比较分页和结算语义；保留一次结算与重投递。 |
| P09 | Workflow 规划与检查 | `workflow.plan`、`workflow.expansion.plan`、`workflow.status`、`workflow.history.status`、`workflow.node.inspect`、`workflow.remote.current`、`dispatch.matrix.resolve` | 24–42 | 将规划、查询和资源解析纳入声明；明确远端 current 与本地 status 的不同 authority；默认不开放内部方法 HTTP。 |
| P10 | Workflow 执行与结果 | `workflow.start`、`workflow.cancel`、`workflow.complete`、`workflow.fail`、`workflow.worker.restart`、`workflow.worker.stop` | 24–42 | 保留 requestId、owner binding、结果 durable 队列和异步 accepted/pending；按角色声明写入。 |
| P11 | PAC 内部调度控制 | `pac.actor.stop`、`pac.flag.reset`、`pac.flag.set`、`pac.graph.activate`、`pac.graph.close` | 18–32 | 声明并限制 coordinator/internal audience；不作为公开 HTTP API。 |
| P12 | Routine 模板增删改 | `routine.add`、`routine.remove`、`routine.set` | 15–28 | 将 router 的 _workflow_caller 与 owner/actor 准入整体迁入同一 handler，覆盖拒绝和允许。与已声明 list/status/plan/audit/pause/resume 完成一组契约；幂等和正在执行时的更新语义。 |
| P13 | Outbox 清理 | `outbox.prune` | 10–18 | 补 dry-run/范围/确认/结果 schema，保留 outbox.list，避免把 destructive cleanup 当只读。 |
| P14 | 自动升级触发 | `autoupdate.trigger` | 10–22 | 声明触发与 receipt；实际安装/重启仍归本机 supervisor 的明确边界，不允许网页任意执行。 |
| P15 | 声明 ps 状态投影 | `ps` | 10–24 | 先冻结现有响应与恢复期可读性。为 11 处直接调用加 doctor 1 处列出字段 → 承接方法/字段 → 调用点矩阵，覆盖 restore、maintenance、adapters、interactiveSessions 等。daemon.status 只含简要字段，top.snapshot 的 daemon 块未冻结。12 处完成回归后才单列可选退役片。 |
| P16 | 平台接入及授权 CLI | — | 24–42 | 先扩 daemon operation 的 apply / interactive / link-only；现有设备授权 begin 对 capabilities 要求至少 3 项，按 mode 分别校验，验证 scope 查验及仅生成链接的行为。设备入网走 adapter.onboard.*，手填 App 走 adapter.register，CLI 负责读取 secret-file。迁移将给 GUI 的现有 operation 增加 CLI 消费者。 |
| P17 | 平台身份目录 CLI | — | 14–26 | list/find/sync 目前由 CLI 读写身份库。list/sync 可复用 daemon 契约；find 需支持 name 子串、id 精确匹配，或在授权返回集上纯过滤。迁移前由 owner 确认 list/find 在 daemon 离线时的可用性要求。 |
| P18 | 平台媒体取回的消费者 | — | 18–32（条件） | adapter media get 已在 CLI 用 App 凭据下载到私有目录。现有消费端仅 CLI，先保留该路径。确认第二消费者后才排 adapter.media.get，传 name/ref，验证解析、权限、0700 目录及脱敏错误，兼容 path/size/contentType/fileName。未来网页下载需单独授权并传输内容。 |
| P19 | 设置的离线写入 | — | 按需，至多 32 | 保留已初始化 home 的 config set，本地复用 allowlist、writer 与 appliesOn。当前没有 config get 命令。出现在线消费者时再评估共享契约和单次一致写入。 |
| P20 | 日志的离线读取 | — | 按需，至多 30 | CLI log 保留本地 JSONL 扫描、时间/组件/级别/会话等过滤与 skippedLines。GUI 使用 log.page。出现远端筛选需求再独立设计 log.query 的授权范围。 |
| P21 | doctor 的离线诊断 | — | 按需，至多 40 | 保留 ensure_daemon=False 的组合诊断及离线状态回报，包括 unknown 退出码 10。ps 只补在线事实，失败仍返回本地检查。doctor.inspect 仅在有消费者时考虑在线子集；--fix 保持现有 upgrade steps 范围。 |
| P22 | 本地工具边界与收敛门禁 | — | 18–35 | 逐项记录 help/version、GUI launcher、service/supervisor、profile home 选择及本地输入文件处理；给 direct backend import/file mutation 精确归属，不设整模块豁免；socialware install 明确保留 COMING_SOON tombstone。验证动态调用和新方法不再漏声明。 |

优先复核 P01、P03、P08、P12。生命周期线按 P02 → P04/P05 → P06/P07，工作流按 P09 → P10 → P11，P22 汇总离线例外。每个功能以有效请求验证 IPC 回读、身份、权限及错误；涉及 CLI、HTTP 时加对应真实入口回归，新增 HTTP 须指明实际消费者。

**P02 决定（2026-11-25）**　CLI 恢复会话用 `agent.get` 查单个 Agent，查无结果时得到 `AGENT_NOT_FOUND`。GUI 概览通过 `agent.list` 读列表。`agent.readiness` 为 CLI 提供工具、权限和目录的逐项诊断。

后台启动在 daemon 内直接准备运行环境，交互式 CLI 通过 `agent.runtime-context` 复用这项能力。`agent.start.preflight` 检查启动条件。P2 运行环境准备成功后返回的 `launchToken` 用来申请启动租约，防止同名 Agent 被删除重建后误用旧环境。

GUI 调用 `agent.create` 成功时，受管 home 已在启动前建好。默认 `agent-home-setup` routine 随后指导 Agent 完善配置。`agent.ensure-home` 仍服务于旧 Agent 的 home 修复与受管 Claude 的旧版 PTY 启动。等这两处调用均有替代并通过回归后，再评估退役。隔离验证和实现见 [P02 #3154](https://code.hyprial.com/HyprialOS/hyprial/pulls/3154)。

按 `ea728eebe4` 更新工作量：I4a 与 S3c 均已合入，原先的 3 个 S3c 功能单元已全部计入 #3137。余下 23 个收敛单元（含 P05a/P05b）、已在 #3136 开发的 3 个 I4b 单元、channel 收尾和未来的 tiers 方法，共 28 个。若另立 ps 退役片为 29 个；另算 #3120 的 4 片为 32–33 个。这是功能单元数。I4b 已有 PR，P18–P21 的新增接口也取决于消费者，不能按这个数字直接新开 PR。

## CLI 初审与分工建议

11 月 24 日，hyprial-cli-developer 给出“需修改”。审核人静态复核 236/174/60/2、HTTP/GUI 130 和 60 个旧路由的分组，未重复运行原隔离探针。其开发线上尚无可销账的 P02–P22 PR。以上 P05、P15–P21 和 CSV 已按初审修订。

| 建议对接线 | 评审或回归范围 |
| --- | --- |
| CLI | P15 字段矩阵、P19–P22 离线行为，P02–P04/P12–P14 的真实 CLI 消费者回归。 |
| daemon-request | P01 契约示例，P02–P14 的 MethodSpec、handler 与授权回归。 |
| harness / worker | P06 custody、P07 session 与 worker channel、P08 inbox 等待与结算。P07/P08/P11 均保留 IPC-only，P11 仅限 internal/coordinator。 |
| 联合 | P05 停止、启动和恢复，P16 授权模式，P17 目录；P18 在有第二消费者时再由双方设计。 |

CLI 复审接受固定提交 `5d8b69e23f` 的修订，认领表中 CLI 行的设计与消费者回归范围。P15 的逐字段矩阵和回归是后续实际交付物。Cell 线确认上述在建 PR 与 #3120 的实现归属；P05/P16/P17/P18 的联合验收及真实环境负责人仍待相关方确认。

## 待复核的问题

1. P17 迁移前请确认 daemon 的过滤契约和离线行为，契约就绪前保留 CLI 本地 find。P05/P16/P17/P18 的联合验收须由对应开发线共同确认。
2. 请 cm-coord3 在看板补充双机 owner、飞书真实投递的验收人和机对，回填实测结果。
3. 对媒体取回、运行时设置写入、日志查询和复合诊断，请标注“沿用既有方法 / 扩展既有方法 / 新增 IPC / 保留离线”，附对应消费者。双方认领后再安排实现。
