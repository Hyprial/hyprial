# 模型连接：记录与请求契约阶段

用户 2026-11-24 授权先实现，daemon-request 后续可补充接线和架构梳理，不互相等待。
总体设计与逐轮评审见 https://code.hyprial.com/HyprialOS/hyprial/pulls/3134 。
本阶段从 dev 1ad698b8f2925c01472c59f33b7152875f230198 开始，容器 intg/model-connections-contract。
为遵守用户要求，本阶段只使用一个 WIP PR #3141，最多 49 文件；45 文件预警。
后续 Pi、desired-state、Cell 和 GUI 属后续独立阶段，不能累积成超限容器。

验收：
- [ ] 原子 Secret 记录 dual-read flat/v1/v2；显式通用连接、稳定引用与配置修订；旧写入不能剥除连接。
- [ ] 四个既有 provider-secret 方法兼容旧输入，新输入精确测试手填 ID；不暗中授予 Agent。
- [ ] 显式 timeoutSeconds 1..60，探针期限及该方法专属 carrier D+5；缺省仍 10/15 秒，其他方法不变。
- [ ] 新错误码、MethodSpec、OpenAPI、客户端和 Contract Mock 同步，权限和脱敏回归。
- [ ] 新连接在 Pi 及真实绑定尚未验收前不能通过 legacy 启动入口绕过准入。
- [ ] 焦点先失败再通过、结构/模块/契约门、全 tests 与 Ruff；环境失败逐项报告。

当前已完成 PR1 本地实现，处于独立实现评审准备阶段。验收矩阵见
[PR1 证据](../evidence/model-connections-pr1-20261124/README.md)。真实模型/GUI 验收未开展；不读取生产凭据。

交付纪律（用户 2026-11-24 最新指示）：本 agent 为开发位，cc-gui-orgs 为独立评审位。
本地实现和验证齐备后交精确 worktree/SHA；本机评审期间 PR 保持 WIP。
本机独立评审通过后才去掉 WIP 并进入 CI；修复、复评、CI 逐片循环。
CLI developer 的设计复核不能替代本机实现评审；不提前提交可合并实现 PR。

用户补充：设计评审结论是独立实现评审的焦点和验收依据，不是可忽略的背景。
每片交付必须提供“设计条目→实现位置→测试/实测证据→PASS/FAIL/BLOCKED/UNKNOWN”矩阵。
计划保留 6 个实现 PR，每片一个 WIP PR，当前第一片沿用 #3141；本地 feat 分支只用于
隔离实现与精确 SHA 评审，不再为本片额外建立重复实施/容器 PR。
第 2–6 片分别为 Pi 适配、绑定存储、统一启动及 Cell/CLI、连接 GUI、Agent GUI。
每片评审确认前保持 WIP；最终跨片验收必须证明完整用户旅程，不用单测代替。

首轮独立实现评审 FAIL，修复边界：旧 `provider-list {}` 仅隐藏其无法解释的新连接字段，
保留同 home/同 entry 中所有 legacy 字段与路由；新调用显式 `connectionVersion:1` 读取全量。
新连接字段不能投影成普通 Key。旧 DeepSeek configure 可更新未迁移的字段，但不能覆盖
已迁移的新连接；原子 v2 文件不降级。无效新连接/Key 在写入前拒绝。
JSON Schema integer 的整数浮点形式统一接受并归一为整数，布尔/非整数拒绝。
新 GUI 的默认30秒由 PR5 显式发送；本片缺省调用保留10秒探针/15秒载体。
本片负责 HTTP→IPC→探针取消传播；只取消无持久业务写入的单次探针，不扩展到其他方法。
探针已发出的付费请求不能退款，也不能保证上游停止已接收的计算；本机停止等待、关闭流、
释放槽位均须验收。缺失/不可读 saved authority 用 authorize/missing|unreadable，不猜测 Agent grant 状态。
所有超时由当前 handler 附 probe/deadline-exceeded；schema 保留接受旧无该字段结果的兼容窗口。
