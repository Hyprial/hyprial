# org 线：开发要求与 prompt

负责人：**yaoshengyue**。工作包：**O**。
统一审核：codex-daemon-request；合入统筹：emergent（coordinator / linyilun）。
先读 [总架构](00-architecture.md)，本线开发遵循 [并行与合入流程](01-coordinator.md)。

## 1. 当前能力、缺口与具体修改文件

路径约定：request_catalog/、application/、ipc/ 均展开为 src/hyprial/daemon/impl/；shell/ 位于 src/hyprial/；user-settings/ 位于 gui/src/features/。表中省略前缀的文件沿用所在 feature 目录，公共链路的具体文件见 [公共线](02-common.md)。

| 能力 | 当前文件 | 契约/后台事实 | 应改文件与边界 |
|---|---|---|---|
| 普通产品注入 | `gui/src/app/capabilities.tsx`、`gui/src/app/AppShell.tsx`、`gui/src/pages/OrgPage.tsx` | 普通 App 未注入 orgSource；DEV 退回 legacy fixture，PROD unavailable | capabilities 注入 `createOrgLiveSource`，AppShell/OrgPage 移除隐式 fixture 回退。只由公共连接决定模式 |
| 已接组织/文件/链接动作 | `gui/src/features/org/domain/{live-source.ts,context.tsx,unwired.ts}`；`api/{org-request-api.ts,orgfs-request-api.ts,link-request-api.ts}` | org.list/fetch/create/invite/join/network/repair/leave/remove/delete/publish、orgfs 读写、link.list/add/drop、hosts/top.snapshot 等已有声明和生成调用；daemon 使用原 org/orgfs/link 权威 | API adapter 改接注入的 client，保留现有业务映射。Spec位于 `request_catalog/org/{network_specs.py,removal_specs.py,view_specs.py,wire.py}`、`org/orgfs/{read_specs.py,write_specs.py,wire.py}`、`request_catalog/link.py` |
| 17 个 unwired gateway 动作 | `gui/src/features/org/domain/unwired.ts` | setCurrentOrg、claimInvite、renameOrg、listTemplates、previewImport、validateConfig、publishCandidate、adoptCandidate、joinAgentOrg、leaveAgentOrg、checkMigration、planMigration、submitMigration、startMigration、cancelMigration、rollbackMigration、decideMigration 无已确认的一一接线 | 逐项保留 unavailable 与原因；setCurrentOrg 可能只是 UI 本地选择，单独界定。不将 agent migration/config 方法硬套 org 场景；不以 fixture 返回成功 |
| 有方法但业务拒绝/GUI 策略限制/模型缺失 | `live-source.ts`；`src/hyprial/daemon/impl/org/network/service.py` | org.delete 后端拒绝 LAST_AUTHORITY_REFUSED；目录 restore 是 **GUI adapter 当前主动拒绝**，daemon `orgfs/api.py::restore(recursive=True)` 与 `orgfs/document/operations/tree.py` 已支持目录；GUI 当前以缺少删除前快照映射为理由拒绝，不证明后端没有目录恢复方法。purgeEntry 不能用清历史冒充清回收站；people/agents/config/routes/stats 详情部分为空 | O 线核对 history/stat_at 与删除前快照可否可靠映射，能确定版本后补目录版本选择/递归确认与测试；无法确定则明确缺的是 GUI 快照映射，不能拿删除后版本盲目 recursive=true。其他拒绝、null 保持，不编造列表 |

组织线无需重写已有 gateway 或复刻页面；本轮关键是公共 client 注入、产品 fixture 退场及真实/Mock 两模式同组件验收。

## 2. 可以并行开展的工作

现有 org/orgfs/link HTTP 已可作为 adapter 重构依据，本线可立即做 client 注入、unavailable/错误展示、目录恢复策略与测试，不等待公共 bootstrap 或 chat 完工。提交 orgSource 工厂/类型/所需 AppShell 行为给公共线；公共线完成入口后补普通产品页面验收。已初始化隔离 home 可提前做 live，不能代替最终空 home 全旅程。

## 3. 文件责任与接口交付

本线拥有 features/org/ 与 OrgPage，沿用原 org/orgfs/link 后端权威，不重写 gateway。capabilities/AppShell 是公共线共享文件，本线提供接入需求与断言；共同负责人 yaoshengyue 不意味着将 org 与公共代码混成一个 PR。

交付 adapter 导出类型、需要的生成方法集合、错误/receipt/phase 依赖、本线方法工厂和必要注册项。新增契约先固定 schema/examples/授权/幂等，再生成 client；不得以页面 DTO 代替 MethodSpec。接口变化写入本线 PR，让 coordinator 更新依赖，不能由消费线私改同名类型。

## 4. 验证与交接

普通产品页面的组织/文件/链接读取、真实写或明确拒绝、邀请/加入/取消、17 项 unavailable；org.delete 固定拒绝、purge 语义、缺失详情模型、目录 restore 的删除前快照/递归确认与错误，跨身份负例。

通用 G1–G8 与命令见总架构。daemon 与 Contract Mock 必须使用同一构建、普通产品页面、adapter/client/URL；Contract Mock 与仅测试替身不算真实 backend 证据。先焦点红→绿，再全量适用检查与 CI；外部能力不可访问写 BLOCKED，未测写 UNKNOWN，skip 不算 PASS。完成后提交固定 SHA 给 codex-daemon-request 审核；REWORK 由本线修复，审核通过由 emergent 合入。

## 5. 可直接分派的 prompt

> 负责人 yaoshengyue，你承接 org 线（O）。以总架构的固定 source SHA 为事实起点，执行从 coordinator 公布的 intg/gui-live-0554 固定 head 建本线短分支，PR base 为该容器。只实现本线范围，不直接推 intg、不发布、不代 merge、不访问日常 home/凭据/daemon。所有后端业务经注入的同一 GeneratedClient → HttpTransport → 同源转发 → HTTP FastAPI → daemon request → daemon 方法；MethodSpec 是契约唯一源。没有方法/契约就明确 unavailable，不能前端 fixture 成功兜底。模式只由宿主 HYPRIAL_GUI_BACKEND_DRIVER=daemon|contract-mock 决定，功能层不配 URL/token/principal。共享文件由公共线装配，生成物由生成器产生。可以在冻结接口上与其他线并行实现并使用仅测试替身；最终 live 验收依赖公共运行时，替身不算 PASS。提交精确 SHA、PR、改动路径、红→绿测试、CI、浏览器证据和未覆盖项给 codex-daemon-request；审核通过后交 emergent 统筹合入，修改 head 后重新复核。
>
> 负责 O。保留 features/org/domain/live-source.ts 与三个 api adapter 的既有业务语义，接公共 client。普通 App 通过共同 capabilities 装配 orgSource；修改 AppShell/OrgPage 去掉隐式 legacy fixture 回退。只在自己的薄 adapter/组件改动，共同装配通过公共 owner 合入。覆盖 list/fetch、创建/邀请/加入、文件与链接、取消/拒绝路径。17 个 unwired 动作逐项保留原因；org.delete 拒绝、purge 语义限制、缺失详情读模型不得伪装成功。目录 restore 后端已有支持，GUI 尚有限制，按本文件能力表 决定补齐递归确认/版本映射并测试，或明确标为 GUI 未接，不能说没有后端。使用相同产品页面验证 daemon 与 Contract Mock；浏览器请求只能出现统一入口，不能出现 reference 专页或 fixture gateway 替代验收。具体文件按本文件能力表。
>
> 并行边界：现有 org/orgfs/link HTTP 已可作为 adapter 重构依据，本线可立即做 client 注入、unavailable/错误展示、目录恢复策略与测试，不等待公共 bootstrap 或 chat 完工。提交 orgSource 工厂/类型/所需 AppShell 行为给公共线；公共线完成入口后补普通产品页面验收。已初始化隔离 home 可提前做 live，不能代替最终空 home 全旅程。文件归属：本线拥有 features/org/ 与 OrgPage，沿用原 org/orgfs/link 后端权威，不重写 gateway。capabilities/AppShell 是公共线共享文件，本线提供接入需求与断言；共同负责人 yaoshengyue 不意味着将 org 与公共代码混成一个 PR。所有修改依本文件缺口表与总架构，不把缺能力改成成功、不新增第二套业务权威。
