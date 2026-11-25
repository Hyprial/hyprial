# agent 线：开发要求与 prompt

负责人：**linyilun**。工作包：**A**。
统一审核：codex-daemon-request；合入统筹：emergent（coordinator / linyilun）。
先读 [总架构](00-architecture.md)，本线开发遵循 [并行与合入流程](01-coordinator.md)。

## 1. 当前能力、缺口与具体修改文件

路径约定：request_catalog/、application/、ipc/ 均展开为 src/hyprial/daemon/impl/；shell/ 位于 src/hyprial/；user-settings/ 位于 gui/src/features/。表中省略前缀的文件沿用所在 feature 目录，公共链路的具体文件见 [公共线](02-common.md)。

| 能力 | 当前文件 | 契约/后台事实 | 应改文件与边界 |
|---|---|---|---|
| 目录/详情/凭据清单 | `gui/src/pages/ContactsPage.tsx`；`gui/src/features/agents/api/{index.ts,contract-gateway.ts,use-agent-directory.ts}` | agent.list、secret.list、provider.default 有 HTTP；不是所有项都代表已认证/可运行 | 改为公共 client 注入，清楚区分 inventory、credential、running readiness |
| 创建/停止/启动 | `gui/src/features/agents/api/agent-writes.ts`、`AgentMutationGateway.ts` | agent.create/stop/start 已有 MethodSpec+HTTP；`request_catalog/agent/{lifecycle.py,start.py}`；既有 daemon registry/lifecycle authority | 保留原确认、错误和状态语义，不新造 /api/agents REST 服务；首次 setup 与此共享生成方法 |
| home census、关联 links/routines/workflows | `contract-gateway.ts`、`detail/RelationsTab.tsx`（features/agents 下） | agent.home-census、link.list、routine.list/status、workflow.list 有 Spec/HTTP，见 `request_catalog/agent/relations.py` | 共用 client/catalog；缺对象时返回真实空态或错误 |
| 更新/删除、内嵌聊天 | `contract-gateway.ts`；`gui/src/features/agent-chat/api/{chat-controller.ts,use-contract-runtime.ts}` | 更新/删除当前 gateway 明确 unavailable；聊天与 chat 主线共享组件/控制器 | 不因为有其他近似方法就解除 unavailable；聊天接 chat 线产出的统一契约 adapter，不再建设 agent-chat 独立网络层 |

## 2. 可以并行开展的工作

可立即重构现有 agent.list/create/start/stop、secret/provider 与 relations adapter 的依赖注入和测试；不等待 org 或 user-setting 全部页面完成。公共 client 接口稳定后对接真实入口；可用已初始化隔离 home 验证，但最终组合须跑 U1 冷启动。agent-chat 的容器/页面可先改，聊天 API/控制器实现归 chat 线，按其冻结导出集成，避免两线同时改 api 文件。

## 3. 文件责任与接口交付

本线拥有 gui/src/features/agents/ 与 ContactsPage；features/agent-chat/api/ 由 chat 线维护，本线消费。features/agent-provider/ 由 user-setting 维护，agent 页面消费既有公共能力；capabilities/main/AppShell 改动提交公共线统一装配。

交付 adapter 导出类型、需要的生成方法集合、错误/receipt/phase 依赖、本线方法工厂和必要注册项。新增契约先固定 schema/examples/授权/幂等，再生成 client；不得以页面 DTO 代替 MethodSpec。接口变化写入本线 PR，让 coordinator 更新依赖，不能由消费线私改同名类型。

## 4. 验证与交接

真实列表/详情、创建/停止/启动与拒绝、owner 不匹配、unknown、不存在 agent、provider 未就绪，以及 relations 空态/失败。update/delete unavailable 不被 fixture 掩盖。

通用 G1–G8 与命令见总架构。daemon 与 Contract Mock 必须使用同一构建、普通产品页面、adapter/client/URL；Contract Mock 与仅测试替身不算真实 backend 证据。先焦点红→绿，再全量适用检查与 CI；外部能力不可访问写 BLOCKED，未测写 UNKNOWN，skip 不算 PASS。完成后提交固定 SHA 给 codex-daemon-request 审核；REWORK 由本线修复，审核通过由 emergent 合入。

## 5. 可直接分派的 prompt

> 负责人 linyilun，你承接 agent 线（A）。以总架构的固定 source SHA 为事实起点，执行从 coordinator 公布的 intg/gui-live-0554 固定 head 建本线短分支，PR base 为该容器。只实现本线范围，不直接推 intg、不发布、不代 merge、不访问日常 home/凭据/daemon。所有后端业务经注入的同一 GeneratedClient → HttpTransport → 同源转发 → HTTP FastAPI → daemon request → daemon 方法；MethodSpec 是契约唯一源。没有方法/契约就明确 unavailable，不能前端 fixture 成功兜底。模式只由宿主 HYPRIAL_GUI_BACKEND_DRIVER=daemon|contract-mock 决定，功能层不配 URL/token/principal。共享文件由公共线装配，生成物由生成器产生。可以在冻结接口上与其他线并行实现并使用仅测试替身；最终 live 验收依赖公共运行时，替身不算 PASS。提交精确 SHA、PR、改动路径、红→绿测试、CI、浏览器证据和未覆盖项给 codex-daemon-request；审核通过后交 emergent 统筹合入，修改 head 后重新复核。
>
> 负责 A。在 ContactsPage、features/agents/api、详情 tabs 中复用现有 generated agent.list/create/stop/start、secret/provider 与 relations 方法，通过公共 client 注入；不要重建 /api/agents 或第二套 transport。与 U1 约定创建/启动/凭据公共能力，避免双写；区分可列出、已认证、可启动和运行中。update/delete 没有已确认等价契约时保持 unavailable。agent-chat 使用 C1 的统一控制器，不自己接 bridge。验证真实创建/停止/启动及拒绝、unknown、跨身份负例；具体文件按本文件能力表。
>
> 并行边界：可立即重构现有 agent.list/create/start/stop、secret/provider 与 relations adapter 的依赖注入和测试；不等待 org 或 user-setting 全部页面完成。公共 client 接口稳定后对接真实入口；可用已初始化隔离 home 验证，但最终组合须跑 U1 冷启动。agent-chat 的容器/页面可先改，聊天 API/控制器实现归 chat 线，按其冻结导出集成，避免两线同时改 api 文件。文件归属：本线拥有 gui/src/features/agents/ 与 ContactsPage；features/agent-chat/api/ 由 chat 线维护，本线消费。features/agent-provider/ 由 user-setting 维护，agent 页面消费既有公共能力；capabilities/main/AppShell 改动提交公共线统一装配。所有修改依本文件缺口表与总架构，不把缺能力改成成功、不新增第二套业务权威。
