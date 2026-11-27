# Provider 登录与密钥管理交给 Magpie（WIP，访谈中）

状态：只有方向和待定问题，没有实现，不改变现行契约。设计由负责人逐问裁决，全部确认之前不写实现。
本线在「与其他线的关系」一节列出的 PR 合入之前不开工。

## 1. 负责人已定的方向

2026-11-26：

1. 模型密钥交给 Magpie 和 `hyprial provider` 处理。
2. 交互终端里不出现密钥，由 Magpie 管理和投影。终端里只有网关地址和模型 ID。
3. `hyprial agent provider login` 全部改为 `hyprial provider login`，登录通过调用 Magpie 完成。
4. hyprial 共用用户自己的 Magpie 实例，不打包进 wheel。daemon 复用已在运行的网关，没有运行时再启动它。

2026-11-27：

5. hyprial 后端不再承担 provider 的管理职能，直接依赖 Magpie。daemon 仍提供 provider login 的方法，CLI 和 GUI 仍提供入口，后端调用的是 Magpie 的能力。
6. 各 harness 的原生订阅登录（OAuth 类）也经 Magpie。`hyprial provider login <harness>` 由 daemon 调用 Magpie 的登录流程，harness 一律指向网关。hyprial 不再按 agent 保存原生登录文件，`agent.provider.login`、`agent.provider.logout`、`agent.provider.default` 这组方法移除。

7. hyprial 不保存任何模型 Key。无头启动与交互启动拿到的都只是网关地址和模型 ID。用户添加的模型连接条目、owner-key 和机器默认凭据里的模型 Key 不再保留；保存和测试模型连接的 daemon 方法 `agent.secret.provider-configure`、`agent.secret.provider-test` 移除；启动时不再向 CLI 进程或子进程下发 Key。Magpie 因此成为必需组件：没有安装或网关不可用时，agent 无法调用模型。已保存的 Key 由用户按迁移文档重新录入 Magpie。这一条只针对模型 Key：非模型服务的 Key 怎样存放由服务授权线决定，不在本线范围。
8. agent 的进程只拿占位符，不拿任何网关 key。本机所有 agent 共用 Magpie 里的全部账号和模型。绑定里的模型 ID 是 hyprial 一侧的约定，网关不强制；额度和限流按 Magpie 的全局设置。按 agent 限制账号和模型不在本线范围内。
9. 服务授权交付的模型网关 Key 写进接收方的 Magpie，hyprial 不留副本。接收方的 daemon 在本机 Magpie 里登记一个自定义 provider，地址是本机映射端口，Key 是授权来的那把；授权被撤销或过期时由 daemon 从 Magpie 删除。接收方不再需要把 Key 绑给 agent，也不需要手工建模型连接。授权方一侧由 daemon 向自己的 Magpie 申请一把网关 key 交出，可以限定模型、账号和额度，不从 hyprial 的条目里取。

这些方向与模型连接设计 §6（暂不把 Magpie 作为必需组件）和 daemon 级 provider 登录设计（不新增顶层 `hyprial provider` 命令族）的结论不同，是在它们之后做出的决定。

## 2. 尚未决定

- `hyprial provider login` 的命令形态；一条绑定怎样选定 Magpie 的分组和模型。
- 第 9 条由哪条线实现、何时实现，以及与服务授权线怎样交接。
- 第 9 条有两点尚未核实：授权方的网关是否校验交出的那把 key（流量经转发后从授权方本机回环进入网关时）；daemon 写 Magpie 所需的管理 key 怎样取得。
- daemon 级 provider 登录设计（PR #68）的处置。
- Windows 的范围。

## 3. 与其他线的关系

- 模型连接线的 PR #61、#81、#82、#83、#96：本线等它们合入后开工，并基于合入后的代码设计。不要求它们停下或改方向。
- PR #84（原生登录的精确绑定）：它扩展的部分会被第 6 条移除，已请该 PR 等本线的实现，不先合入。
- PR #85、#86：叠在 #84 之上，与 #84 一起等本线的实现，不先合入（负责人 2026-11-27 裁决）。#85 的连接页把地址和 Key 存进 hyprial 自己的存储，与第 7 条不符。
- 服务授权线：本线等它合入。
- 交互终端宿主线：不等待。两边只在启动参数处交汇。

## 4. 约束

- 业务逻辑在 daemon，CLI 和 GUI 只请求和渲染。
- 不写迁移代码，迁移以文档形式给出。
- 能做成运行时数据的不写死。
