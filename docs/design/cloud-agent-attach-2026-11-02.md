# 云电脑 agent 不重启加入 hyprial 网络：v1 设计稿（拉取模式）

日期：2026-11-02 · 状态：草稿 r2（Allen 认可方向；r2 按其意见收回到 cell 机制） · 工作项：M-emergent-org-structure-refactor-0068

## 0. 结论

v1 让一个已经在运行、无法重启的 agent，用它自己的 shell 能力把自己接入 hyprial 网络。
- 身份、收件箱和在线状态由本地 daemon 持有。
- agent 在 daemon 里表现为一个 **cell 挂载**，`custody=external`：进程不归 daemon 管，回合和权限上报能力全部声明为 false。复用 docs/design/cell-observability-0553-2026-10-30.md §2，不另造进程类型。
- 消息由 agent 自己按需拉取、回复，hyprial 不往 agent 的回合里推送。

r2 变更（Allen 2026-11-02：「这个 attach 边车看起来和 cell 机制差不多？」）：取消 r1 的 python_worker `attached` 侧车 kind。在线由 daemon 自身承担，挂载用 cell。

目标环境（Allen 指定）：Claude duet、GPT dots、Grok bot、Claude Code cloud session。

## 1. 范围

v1 做：
- 一条命令接入：安装 → 起 daemon → 用邀请链接加入组织 → 注册身份 X → 建 cell 挂载。
- 拉取收件：`hyprial message batch --from X`。
- 回复、ack 与发送：`hyprial message reply/ack/send --from X`。
- daemon 持有身份、在线状态与收件箱；cell 挂载只描述“外部托管、无回合上报”，不跑模型。

v1 不做（Allen 2026-11-02 裁定：「先只做拉取即可，依赖 agent 自己的能力去通信，暂时不用支持现在 claude/codex 这样的主动推送」）：
- 不往 agent 回合里注入消息，不借道平台入口推送。
- 不支持 `agent.start/stop/restart`，因为 agent 进程不归 hyprial 管。
- 不托管 agent 的模型凭据。

## 2. 用户路径

1. 组织成员在自己的机器上执行 `hyprial org invite`，得到邀请链接。
2. 把邀请链接和一段接入说明交给云电脑上的 agent（贴进对话即可）。
3. agent 自己执行：
   ```sh
   uv tool install git+https://github.com/Hyprial/hyprial.git@<track>   # 或离线 wheel
   hyprial agent attach --name <X> --invite '<邀请链接>'
   ```
   `attach` 是新命令，内部依次做：
   - 起 daemon；
   - `org join`，复用已有加入流程（E2E-025 已两轮通过）；
   - 注册身份 X；
   - 为 X 建 cell 挂载（custody=external，capabilities 全 false）；
   - 打印一段给 agent 自己读的用法说明，见第 4 节。
4. 此后 agent 每轮结束（或按说明里约定的节奏）执行一次 `hyprial message batch --from <X> --json`，逐条处理并 reply 或 ack。

## 3. 组件

| 组件 | 现状 | v1 改动 |
|---|---|---|
| daemon | 已有；持有 durable inbox、路由、在线状态 | 无 |
| cell 挂载 | 0.5.5.3 已设计挂载层与 `custody: external`（交互终端即此情形）；不新增执行循环 | 为 attach 身份建挂载，能力位全 false；界面显示“已挂载 · 外部托管 · 无回合上报” |
| 收件 | `message batch` 已有，是消费式读取，需逐条 reply/ack | 无 |
| 离线兜底 | 发件方 outbox 自动重试；`asMailbox` 角色托管“双方都不在线”的消息（TTL 7 天，见 design.md 投递流程） | 组织里至少一台常驻节点开 `asMailbox` |
| 加入 | `org invite / join / pending` 已有 | `attach` 编排调用 |

关键：收件的权威在 daemon。daemon 在线只代表“身份在线”，不代表 agent 在处理。

## 4. 给 agent 的用法说明（attach 时打印，agent 自读）

- 你的地址是 `agent:<owner>:<host>:<X>`，别人用它给你发消息。
- 每轮结束执行 `hyprial message batch --from <X> --json`。对每条消息：需要回复就 `hyprial message reply <id> "<text>" --from <X>`；不需要回复就 `hyprial message ack <id> --from <X>`。
- 主动发消息：`hyprial message send "<text>" --to <地址> --from <X>`。
- 不要冒用别的身份；`--from` 只能是 X。

## 5. 四个环境逐项核对（未实测，全部 UNKNOWN）

| 检查项 | Claude duet | GPT dots | Grok bot | Claude Code cloud |
|---|---|---|---|---|
| ① 能执行 shell | UNKNOWN | UNKNOWN | UNKNOWN | 有（Bash 工具），未实测 |
| ② 后台进程能否跨回合存活 | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |
| ③ 出站网络能否到 hyprial 网络（zenoh/tailcat/relay） | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN（多数云环境出站受限） |
| ④ 无 root、HOME 受限时能否安装（uv 用户级安装） | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN |

②决定形态：
- **能存活**：daemon 常驻，身份在线，消息实时进收件箱，agent 拉取即得。
- **不能存活**（每轮结束进程被回收）：退化为“拉取时临时起 daemon → 同步 → 取一批 → 回复 → 退出”。agent 平时显示离线，消息靠发件方 outbox 重试和 mailbox 托管兜底，延迟等于 agent 的拉取间隔。v1 也必须支持这一形态。

③若出站只放行 HTTPS，需要走 relay 的 HTTPS 通道（labrelay 现状需核）。若完全不能出站，此方案不适用，需标为不支持。

需要谁提供：四个环境各一个测试账号或会话（Allen 或环境 owner）。拿到后每个环境约 1 小时实测，结果回填本表。

## 6. 安全与身份

- 身份 X 由邀请加入时绑定到邀请发起人的组织；X 只能以自己的身份发送。daemon 侧已强制，与现有身份规则一致。
- 云电脑是不受信宿主：凭据只存 daemon 的本地状态目录，不写进 agent 可见的说明或对话。
- 撤销：组织成员执行 `hyprial org remove` 移除该设备。这是设计要求：移除后该身份应立即不能收发；尚未实测，列入 E2E。

## 7. 风险与边界（会写进用户文档）

- 在线只代表 daemon 与 cell 挂载在线，不代表 agent 在处理。界面复用 cell 状态：“已挂载 · 外部托管 · 无回合上报”，不显示为“运行中”。
- agent 不拉取，消息就一直排队。超过 mailbox TTL（7 天）会丢，需在界面上提示。
- 无法中断或停止 agent 的回合。

## 8. 交付计划（0.5.5.4）

1. `hyprial agent attach` 命令：起 daemon、加入组织、建 cell 挂载（custody=external）。不新增进程类型。
2. 临时形态 `hyprial agent attach --ephemeral`：拉取时起、拉完退出。
3. 契约：在 contract/ 中声明 cell 挂载 custody=external 的状态字段与在线语义（与 cell 观测切片对齐）。
4. E2E：在隔离 HOME 和网络里模拟“先起 agent、后接入、进程不常驻”两种形态，跑收发闭环。
5. 四个环境实测，回填第 5 节。

## 9. 需要 Allen 定的

- 第 5 节四个环境的测试账号由谁提供。
- （已按 r2 定：界面复用 cell 状态，不另起称呼。）

## 10. 2026-11-03 实测结论（Allen 的云电脑，Claude Code cloud session，Linux x86_64）

- §5 ④ 安装：**PASS**。0.5.5.3.3 的 stable wheel 下载 HTTP 200、sha256 一致；`uv tool install` 成功；`hyprial version` 显示 0.5.5.3.3。没有 root 也没有 systemd：doctor 报 daemon-service 为 UNKNOWN，这是预期的。
- §5 ① shell：PASS（Bash 工具）。
- §5 ③ 出站网络：**容器只放行经过 HTTPS proxy（CONNECT）的流量**。用 strace 看 0.5.5.3.3 的 tailcat sidecar：DERP 中转全部是按 IP 直连（sfo/nyc/fra/tok:443），DNS 也是自己直连 8.8.8.8；经过 proxy 的连接数为 0，所以全部失败。修复：sidecar 遵循 `HTTPS_PROXY`，用 CONNECT 连中转的域名，DNS 交给 proxy 解析（PAC tailcat-derp-proxy，进行中）。
- daemon 启动：上一次 join 留下了没兑现完的邀请，daemon 启动时先去兑现，authority 不可达就整体退出（AUTHORITY_UNREACHABLE）。修复：兑现失败时降级启动，后台重试（同一个 PAC）。
- §5 ② 后台进程能否跨回合存活：清理状态后 daemon 能起来（phase serving），但跨回合存活还没测。

## 11. 实现说明（给实现者，0.5.5.4）

> 2026-11-03 Allen 裁定：「先做通用 cell 运行时，再做 attach」。执行顺序：先做 S3 通用 cell 运行时（见 cell-thread-conversation-model §9.3、§10.1），合入后再做 attach（S8），attach 创建的是 agent cell，成员为 `custody=external` 的 harness，而不是只在观测挂载上登记。

前提：tailcat-derp-proxy 的修复合入以后，attach 才能端到端通过；在那之前先实现命令本身和测试。

范围：
1. `hyprial agent attach --name <X> --invite '<链接>' [--ephemeral]`，依次执行：
   - 起 daemon（常驻形态；`--ephemeral` 时只在命令期间运行）；
   - `org join`（复用现有流程）；
   - 注册身份 X（agent 身份，`a.` id）；
   - 为 X 建 cell 挂载：`custody=external`，回合与权限上报能力全部为 false，界面显示“已挂载 · 外部托管 · 无回合上报”；
   - 打印 §4 的用法说明。
2. 取消挂载：`hyprial agent detach --name X`，保留身份，删除挂载。
3. 命名遵守 `docs/design/cell-thread-conversation-model-2026-11-03.md`：attach 出来的是一个 agent cell；这次不引入 thread 或 conversation 的新概念。
4. 契约：在 `contract/` 声明 cell 挂载 `custody=external` 的状态字段和在线语义。
5. 测试：
   - 单元测试：命令的编排顺序，以及每一步失败时的回退（不得留下半挂载）；
   - 隔离 E2E：两个 disposable daemon，加上本地 labrelay，跑“先起 agent、后接入”的常驻和临时两种形态，完成 message batch / reply 闭环；
   - 受限网络 E2E 等 proxy 修复合入后再补：只允许经过本地 CONNECT proxy 出站。

纪律：
- 从 `origin/dev` 新建 `cloud/<slug>` 分支，PR 合到 dev；
- 不 force-push，不改 `.forgejo/workflows/**`；
- 提交带 `Co-Authored-By`；PR 正文首行写 `Submitted-by`，并带 `Skill-Impact` 和 `Contract-Impact`；
- 真实环境无法验证的项写 BLOCKED，不能写成 PASS。
