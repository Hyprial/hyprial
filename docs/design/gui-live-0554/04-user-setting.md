# user-setting 线：开发要求与 prompt

负责人：**huangjiajia**。工作包：**U1/U2**。
统一审核：codex-daemon-request；合入统筹：emergent（coordinator / linyilun）。
先读 [总架构](00-architecture.md)，本线开发遵循 [并行与合入流程](01-coordinator.md)。

> **S2d 状态说明（2026-11-22）**：表中历史 `SquireStep.tsx`、`prepareSquire`、`squire.ensure` 与 `squireState=name-frozen` 路径已于 S2d 删除；现行首启路径为 `account → device → 普通 Agent 运行配置（configureAgent / DeepSeek 凭据）→ conversation（P3 replyConfirmation）`。下文保留当时缺口与交接表。

## 1. 当前能力、缺口与具体修改文件

路径约定：request_catalog/、application/、ipc/ 均展开为 src/hyprial/daemon/impl/；shell/ 位于 src/hyprial/；user-settings/ 位于 gui/src/features/。表中省略前缀的文件沿用所在 feature 目录，公共链路的具体文件见 [公共线](02-common.md)。

| 能力 | 当前 GUI/调用文件 | Spec → API → request → daemon 事实 | 应补内容/具体文件 |
|---|---|---|---|
| 账号登录/注册、设备、首次初始化 | `gui/src/features/onboarding/{auth-actions.ts,actions.ts,useFirstRunController.ts,AccountStep.tsx,FirstRunOnboarding.tsx}`；`gui/src/pages/FirstRunPage.tsx`；`gui/src/platform/auth/index.ts` | 专用 auth worker → desktop_network → shell login；无相应 MethodSpec/HTTP；注册无已验证独立本地方法 | 改为 P0 生成的 onboarding/auth client；新增 `gui/src/features/onboarding/api/index.ts` 作为薄 adapter。移除产品对 `gui/server/auth/{middleware.mjs,runner.mjs,worker.py}` 的业务依赖；安全会话握手留公共层 |
| 模型 key、secret grant、管理 agent 建立/启动 | `ModelStep.tsx`、`SquireStep.tsx`、`useFirstRunConversation.ts`（均位于 onboarding）；`gui/src/platform/setup/index.ts`；`gui/server/setup/{worker.py,service.mjs,daemon.mjs,checkpoint.mjs}` | agent.list/create 已有 HTTP；`agent.secret.provider-write`、`agent.secret.grant`、`management.squire.ensure` 为现有 legacy IPC，未进对应 HTTP 契约 | 在 `request_catalog` **新增子包** `setup/{__init__.py,specs.py,examples.py}`，核写权限/receipt；绑定 `application/actors/agents_admin/registry_ops.py` 与 `ipc/router.py` 现有 handler。setup adapter 使用同 client，pending/unknown 恢复不得丢 |
| provider inventory/login/logout/default | `gui/src/features/agent-provider/api/index.ts` | agent.list/secret.list/provider.default/provider.login/provider.logout 已声明并暴露；普通 host 的凭据文件导入能力不能假定存在 | 改为注入公共 client，不自造 token/身份；凭据输入必须走声明的写方法，不让浏览器读宿主路径 |
| hosts、诊断 | `gui/src/features/user-settings/hostsApi.ts`、`diagnostic/api.ts` | hosts、top.snapshot 已走生成客户端，daemon handler 存在 | 复用公共 client。hosts 保持 `hosts({})`，不得用 refresh:true 偷渡写操作；诊断保留真实错误/空态 |
| 用户/绑定/override/resolve | `user-settings/bindings/{api.ts,writes.ts,resolve.ts}` | identity.users.list/add、identity.bindings.list、identity.resolve、identity.override.set/clear 存在于 `daemon/impl/identity/ipc.py` 与 router，但没有这些方法的 HTTP Spec | **新增** `request_catalog/identity/{__init__.py,specs.py,examples.py}`，按现有真实签名/作用域接 public handler；生成后接 GUI。浏览器不得自选可信 caller |
| upgrade status/steps | `user-settings/{api.ts,upgradeApi.ts}` | autoupdate.status、upgrade.steps.status 有 legacy router 路由，缺 Spec/HTTP；UI view contract 不等于 backend Spec | 补声明与读取映射，建议 **新增** `request_catalog/maintenance/{__init__.py,read_specs.py,examples.py}`；禁止把“状态查询”扩成未经授权的自升级执行 |
| profiles/settings/logs/doctor/lifecycle/user-proxy | `gui/src/platform/{profile,settings}/index.ts`；`user-settings/{logs/api.ts,doctor/client.ts,lifecycle/client.ts,user-proxy/api.ts,user-proxy/controller.ts,views/contracts.ts}` | 存在 CLI/域局部能力，但不能从 UI interface 推导出同名 daemon 方法；profile CLI、doctor CLI、squire/user_proxy 是编排，不是已完整暴露 HTTP | 每个 UI 动作先映射具体 handler/语义；可复用的补小 Spec，不可对应的显式 unavailable。CLI依据：`shell/impl/cli/commands/profile/commands.py`、`doctor/commands.py`、`squire/user_proxy.py`。不把这些非关键页全部作为登录后的总闸门 |

## 2. 可以并行开展的工作

U1 页面/状态机、已有 provider/hosts/diagnostic adapter 与 U2 业务 Spec 可同时推进。auth/onboarding 的公共契约由公共线冻结后，在该接口上实现 GUI，不等待其运行时全部完成；未冻结前先准备本线行为/负例。首次身份、锁、执行器、IPC/HTTP 属公共线，不在 GUI worker 再做一套。setup/identity/maintenance 的业务 Spec/handler 映射由本线实现、公共线审核并接核心 registry；它们可分小 PR。

## 3. 文件责任与接口交付

本线拥有 features/onboarding/、features/user-settings/、features/agent-provider/、platform/auth、platform/setup 与对应设置 platform adapter；request_catalog/setup、identity、maintenance 是本线业务契约包。公共登录契约/身份执行器由公共线实现；不重复实现 auth.login.begin。agent create/start 使用 agent 线/现有生成方法，squire.ensure 特有语义只保留一处后端权威。

交付 adapter 导出类型、需要的生成方法集合、错误/receipt/phase 依赖、本线方法工厂和必要注册项。新增契约先固定 schema/examples/授权/幂等，再生成 client；不得以页面 DTO 代替 MethodSpec。接口变化写入本线 PR，让 coordinator 更新依赖，不能由消费线私改同名类型。

## 4. 验证与交接

空 home 的真实登录/注册不可用、取消/超时/刷新、身份成功设备失败、初始化接受但响应丢失、owner mismatch、模型 key/grant/squire readiness；hosts({}) 只读负例；绑定/升级读方法的授权与错误。非关键 settings unavailable 不阻断已就绪业务。

通用 G1–G8 与命令见总架构。daemon 与 Contract Mock 必须使用同一构建、普通产品页面、adapter/client/URL；Contract Mock 与仅测试替身不算真实 backend 证据。先焦点红→绿，再全量适用检查与 CI；外部能力不可访问写 BLOCKED，未测写 UNKNOWN，skip 不算 PASS。完成后提交固定 SHA 给 codex-daemon-request 审核；REWORK 由本线修复，审核通过由 emergent 合入。

## 5. 可直接分派的 prompt

> 负责人 huangjiajia，你承接 user-setting 线（U1/U2）。以总架构的固定 source SHA 为事实起点，执行从 coordinator 公布的 intg/gui-live-0554 固定 head 建本线短分支，PR base 为该容器。只实现本线范围，不直接推 intg、不发布、不代 merge、不访问日常 home/凭据/daemon。所有后端业务经注入的同一 GeneratedClient → HttpTransport → 同源转发 → HTTP FastAPI → daemon request → daemon 方法；MethodSpec 是契约唯一源。没有方法/契约就明确 unavailable，不能前端 fixture 成功兜底。模式只由宿主 HYPRIAL_GUI_BACKEND_DRIVER=daemon|contract-mock 决定，功能层不配 URL/token/principal。共享文件由公共线装配，生成物由生成器产生。可以在冻结接口上与其他线并行实现并使用仅测试替身；最终 live 验收依赖公共运行时，替身不算 PASS。提交精确 SHA、PR、改动路径、红→绿测试、CI、浏览器证据和未覆盖项给 codex-daemon-request；审核通过后交 emergent 统筹合入，修改 head 后重新复核。
>
> 负责 U1/U2。按公共线冻结的 bootstrap/auth/init 接口和本线 setup 契约并行实现，修改 FirstRunPage、features/onboarding 与 platform/auth/setup 的调用，使首次用户从 GUI 登录/注册入口走到设备、daemon 初始化、模型 key、secret grant、管理 agent readiness。不要保留 GUI auth/setup worker 作为业务旁路，也不要要求用户先 CLI login/init。保护登录成功但设备失败、取消、超时、刷新恢复、owner mismatch 与幂等操作。hosts 只读保持 hosts({})。bindings/upgrade 有 legacy handler 的由本线补业务契约，经公共线审核登记后接入；profile/logs/doctor/lifecycle/user-proxy 无等价契约的明确 unavailable，不调用假方法。具体文件按本文件能力表；在独立 home 完成 U1 的真实与 Contract Mock 两模式浏览器证据，不能用 fixture first-run 测试充当真实验收。
>
> 并行边界：U1 页面/状态机、已有 provider/hosts/diagnostic adapter 与 U2 业务 Spec 可同时推进。auth/onboarding 的公共契约由公共线冻结后，在该接口上实现 GUI，不等待其运行时全部完成；未冻结前先准备本线行为/负例。首次身份、锁、执行器、IPC/HTTP 属公共线，不在 GUI worker 再做一套。setup/identity/maintenance 的业务 Spec/handler 映射由本线实现、公共线审核并接核心 registry；它们可分小 PR。文件归属：本线拥有 features/onboarding/、features/user-settings/、features/agent-provider/、platform/auth、platform/setup 与对应设置 platform adapter；request_catalog/setup、identity、maintenance 是本线业务契约包。公共登录契约/身份执行器由公共线实现；不重复实现 auth.login.begin。agent create/start 使用 agent 线/现有生成方法，squire.ensure 特有语义只保留一处后端权威。所有修改依本文件缺口表与总架构，不把缺能力改成成功、不新增第二套业务权威。
