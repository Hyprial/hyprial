# user-settings — 用户设置与设备

业务线：首次使用、Hyprial 账号、设备初始化、本机状态/升级/诊断/日志。
原负责人：黄佳佳；当前集成分支 `intg/gui-agent-journey`。

当前读写能力、方法缺口和验收边界只维护在 [方法对账](../../../docs/design/gui-v0.1-method-plan-2026-11-13.md)。
执行顺序见 [TODO](../../../docs/design/gui-v0.1-lifecycle-todo.md)，术语见 [GLOSSARY](../../../GLOSSARY.md)。
本文件只定义业务范围和组件入口，不另存方法状态表。

## 页面与组件

- `features/onboarding/`：首次使用共享页面；历史四步场景作为归档输入保留，不是当前真实流程定义。
- `pages/SettingsPage.tsx`、`features/account/`：用户设置只呈现账号信息、登录与本机退出。
- `pages/DevicePage.tsx`、`features/settings/`：设备初始化及真实状态读取，未登录不展示旧账号内容。
- 模型凭据与平台接入在 `pages/ConnectionsPage.tsx`（Agent 设置），不是用户设置的第二套入口。
- 用户/平台身份目录存在后端能力，但不作为账号设置中的通用管理表；平台身份组合查询归平台接入。

## fixtures

当前旅程入口是 logged-out-init / logged-in-init，后者是已登录且初始化完毕的空 Agent 起点。
历史首次设置、组织等输入在归档场景中保留。使用合成身份/文件，不含真实凭据；
fixture 登录、Contract Mock 与真实 issuer 授权分开验收。
