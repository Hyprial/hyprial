# 跨机 agent 转移：支持矩阵、支持边界与回滚（AT12 / 卡 361）

日期：2026-09-30。机器可读矩阵：`docs/evidence/transfer-at12-support-matrix.json`（守卫测试 `tests/test_transfer_support_matrix.py`）。

## 0. 一句话

**今天没有任何一条跨机落地组合被验证过。** 本文件把该跑的矩阵、边界、回滚和运维窗口写清楚，并如实标注每行现在是什么状态（`PASS` / `UNSUPPORTED` / `NOT_RUN`）。owner 于 2026-09-30 裁定「跨机测试可以跳过」，所以卡 361 的 item 0（真实两机冷迁移）本会话未跑；本文件不把它写成 PASS。

当前矩阵 7 行：`NOT_RUN` 5 行、`UNSUPPORTED` 2 行、`PASS` **0** 行。单测级读数只能当 `NOT_RUN` 的依据，不构成 `PASS`。

## 1. 支持矩阵

来源：`docs/design/design-agent-standard-form.md` §4（Allen 2026-09-26：发布前在 CI 建 harness × 系统 × 落地方式的兼容矩阵，走真实 worker、用测试凭据跑真实对话；拆箱时比对矩阵，**不在矩阵内只 warning、照常启动**，另跑几秒钟、不调模型不用凭据的沙箱冒烟，**冒烟明确失败才拒绝启动**）。

| harness | 版本 | 系统 / 架构 | 落地方式 | 状态 | 依据 |
|---|---|---|---|---|---|
| dir | any | Darwin 25.5.0 arm64 | 目录还原 | `NOT_RUN` | 仅单测级：真实 `export → validate → receive` 链在 Darwin arm64 上用合成 home 跑通（2026-09-30，#1061 + #1076），保留作证据。**它不是跨机落地、不是真实 worker 回合、也不是第二台机器**，所以按下面的口径标 `NOT_RUN`，不标 `PASS`。 |
| dir | any | Linux x86_64 | 目录还原 | `NOT_RUN` | 没有配备目标机；卡 361 item 0 按 owner 裁定跳过。 |
| claude | claude-code-2.1.283+sdk-0.2.125 | Darwin 25.5.0 arm64 | 目录还原 | `NOT_RUN` | 没有在「已转移的 home」上跑真实 worker 回合。`src/hyprial/agents/migration-support-matrix.json` 里 claude 的行是同机 agent-home 迁移，不是跨机落地。 |
| codex | 0.158.x | Darwin 25.5.0 arm64 | 目录还原 | `NOT_RUN` | 原「版本门」前提已**消解**、非放宽：硬编码的 codex-cli 版本名单（验证债 `6094e515`）已不在树里，没有可放宽的门，且放宽本身也不构成支持证据。codex 的真实跨机旅程 + strict resume 尚未跑；在真跑拿到读数之前，本行维持 `NOT_RUN`，不预判结果。 |
| pi | pi-rpc | Darwin 25.5.0 arm64 | 目录还原 | `NOT_RUN` | org 2026-09-30 的 Allen 裁定已允许 Pi/Kimi 准入，**准入不等于安全已验证**；没有跑转移后 home 的真实回合。 |
| smolvm | 1.19.0 | Darwin 25.5.0 arm64 | 容器 | `UNSUPPORTED` | AT08 有界观察（2026-09-28，#832/#835）：容器路径没有跨机验收，C 映射未接到落地；外层不额外加系统级沙箱。 |
| docker | any | Linux x86_64 | 容器 | `UNSUPPORTED` | D-B/D-C（`docs/design/design-transfer-container.md`）仍有效，但无代码承载，且 Q3 可能修订；什么都没跑。 |

写进矩阵的口径：

- 只有**真跑过并留了读数**的行才写 `PASS`；单测级证据一定要在依据里写清楚「不是真机/不是真 worker」，而且**单测级读数不足以把一行写成 `PASS`**，最多是 `NOT_RUN` 的依据。
- 设计上就不做的组合写 `UNSUPPORTED`，并写出裁定出处。
- 该跑但没跑写 `NOT_RUN`，并写出为什么没跑。
- 没有 `PASS` 的行不许当成「已验证支持」。

## 2. 支持边界（今天对外承诺的边界）

1. **只在同一 owner 内转移。** 跨 owner 是改名，不是迁移；`transfer.land` 会以 `LandingOwnerMismatch` 拒绝 owner 不一致的 bundle。
2. **访客默认只是托管状态。** 没有 grant 的托管访客只能投递给自己（owner 2026-09-30 裁定 (i)）；MQ 也不给「发给自己 owner」开例外。上线配套：迁移/重启**之前**给现存访客发 `send-to`（`hyprial agent grant <actor> --capability send-to --scope '["user:<owner>"]'`，可选 `see-actors`）。
3. **沙箱由 harness 负责，外层只打包、运输、映射**（#820 / Allen 2026-09-26）。外层不加系统级沙箱，也不重复 harness 的工作。
4. **凭据默认用原用户（agent 所属用户）的**；只有 host owner 明确授权才允许退回 host key。信封是原型（`PROTOTYPE_PROTOCOL`），CLI 入口要显式带 `--prototype`。
5. **不做**：账号合并、跨 owner 改名、运行中（live）迁移、基于 VM 快照搬数据、真机双机验收（本会话跳过）。
6. **已知限制**：`targets` 只读视图对 native 名字冒用不可分离（#1066 正文「已知限制」）；bundle manifest 的封条是自校验和，不是密钥签名（#1076）。
7. **L0 同 uid 边界（AT09 攻击清单点名的一条：「无 `sessionRef` 冒充 `user:<host owner>`」）**：受信 socket 的边界是 **uid**。同一个 uid 下的进程可以直连 socket 并声明 `user:<owner>` —— 这正是 operator/CLI 自己的 host-local 路径。user 主体**没有会话可绑**，所以 #1073 的 sender custody 对它不成立：那套绑定只对**已注册 agent** 的声明做「claim 必须配本 daemon 铸的 session」（且是 rollout 开关，默认 `observe` 只记不拒，enforce 下才拒，见 `tests/test_authenticated_message_sender.py`）。因此这一条是**已知限制**、不是已验证的防线；真正的边界要等 L2 身份通道。用例：`tests/test_daemon_visibility_wiring.py::test_a_sessionless_claim_of_the_host_owner_user_is_the_l0_boundary`。
8. **平台二进制不随包搬，依赖与凭据信封要显式声明。** export 会跳过 ELF / Mach-O / PE 这类平台二进制，并在 manifest 的 `dependencies` 里自动记一条 `kind=binary, present=false`（「目标自备」）；外置配置、cwd 快照要由导出方用 `--depends KIND=PATH`（已随包）或 `--depends KIND=!说明`（目标自备）声明，凭据信封用 `--credential-envelope <bundle 内相对路径>` 声明。**声明 `present=true` 而包里没有，或信封不在声明的位置，`validate`/`receive` 一律以 `bundle_declaration_missing` 拒绝**，不会退化成「这个包本来就没凭据」；`present=false` 的条目原样列进 receive 回执的 `target_must_provide`。


## 3. 回滚

三条路径，按发生阶段分：

1. **拆箱前（bundle 还没落地）**：什么都不用回滚。`agent transfer bundle-validate` / `receive` 在写盘之前就拒（路径逃逸、篡改、错 owner/actor、协议版本、目标非空）。**重试**：修好参数或换目标目录后重跑。
2. **拆箱中（落地失败）**：`land_agent_home`（#1065）和 `registry_landing`（#1068）都做「失败逆序全撤」，并且只回撤**本次真正创建**的东西——不是无条件 `drop_agent`（这是 #1068 的阻塞项，已修：create 抛错时 destroy 零调用）。原始输出与退出码要留在目标机的日志里。
3. **落地后（agent 已经起了）**：按 `hyprial agent destroy` 的正常路径销毁；源端的 bundle 与 receipt 保持不动，源 agent 若已从源机清空，则以源的收据为准恢复。**不要**手工改配置绕过登录拒绝（见 `docs/notes/review-host-owner-alias-collision-2026-09-28.md`：把手改回来的配置撤回即可恢复原身份启动，不需要删访客）。

## 4. 运维窗口

谁在什么时候做，取决于当前阶段：

- **现在（本会话）**：合 PR 由 hot-fixer 串行执行，固定顺序落地，每笔先把 base 改到 dev；`codex-sw` 不自行合、不勾卡。
- **上线到 HQ**：需要 HQ 侧运维窗口（`monitor` / `infra-ops`）。前置 grant（`hyprial agent grant <actor> --capability <cap> --scope <scope>`，真实入口在 `src/hyprial/agents/enforcement.py`）：
  - `send-to`，scope `["user:<owner>"]`：现存托管访客在迁移/重启**之前**补发（agent/user 地址只由 send-to 决定，路由不到 channel）。
  - `channel`，scope 精确到 route，写成 `["route:<adapter>:<route>"]`（只有 `route:` 前缀的目标走 channel ledger；adapter 级放行不算，AT10 / 卡 359）。
  - `org-context`，scope 固定 `"accepted"`：只覆盖 accepted 文档，不按空间授予（AT10 / 卡 359）。
  另需确认 `dev` 的 CI 终态（含 `mock-model-harness-e2e` job（原 `hosted-agent-e2e`））与落地树一致。
- **真双机验收（AT12 item 0）**：需要两台真机 + 测试身份/凭据的使用授权（卡 357 item 0），一次冷迁移窗口、目标机可回滚、源机先停写再切换。**本会话按 owner 裁定跳过，写在这里供下一次排期。**
- **发布前**：矩阵里没有 `PASS` 的组合一律按 §1 口径当 `NOT_RUN` 处理；拆箱时矩阵外只 warning，冒烟失败才拒绝启动。

## 5. 本文件没做的事

- 没有跑 CI 矩阵（卡 361 item 1 的「矩阵」现在是**清单**，不是跑出来的结果）。
- 没有跑真双机（item 0，owner 跳过）。
- 没有把矩阵接进拆箱代码（那属于 AT08 / 卡 357 item 3）。
