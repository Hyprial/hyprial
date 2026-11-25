# PR-A 视觉验收：真实不可达目标（返修 1 记录）

基座 `c32b005ff3041b6716c47162ee82518fc3412b1e`，契约宿主 `tests/reference/entry.html?driver=contract-mock`。
全部结论来自真实页面点击与 DOM 计数，未使用注入 probe。

| 目标 | 真实入口步骤 | 结果 |
| --- | --- | --- |
| P3 `.notif-panel` / `.toast` | 设置 → 连接 → 登录服务商 → 粘贴 API Key → 保存并校验（失败路径）；以及 Agent 页各失败动作 | 面板与 toast 均不渲染（0），当前数据不产生通知 → **不可达** |
| P4b `.model-service-row` | 连接页点击「添加模型服务」 | 按钮点击 5s 超时，行卡不渲染（0）→ **不可达** |
| X1 开始页三卡片 | 开始页直接访问 | `.start-page` 无 recent/board/recommendation 卡片（0），当前契约数据无对应区块 → **不可达** |
| X2 `.org-machines-row` / `.org-graph__node` | 组织 → 机器 tab、组织 → 拓扑 tab | 两个 tab 可切换，但行/节点均为 0，当前契约数据无机器与拓扑条目 → **不可达** |

已真实渲染并测量：P1（设置页 Button 与 `.ui-field` 输入）、P2（聊天页 `.chat-workspace`）、P4a（Agent 页 `.agent-form` 与删除确认 `.agent-dialog`）、P4b（连接页 `.provider-login` 与其中的 API Key 输入）。

上述 4 个不可达目标需要产品侧提供可达数据或入口后再验收，本 PR-A 不以 probe 或 CSS 文本替代真实渲染。

## 返修 2：逐目标代码路径核查（2026-11-06）

| 目标 | 组件与挂载情况 | 缺口性质 |
| --- | --- | --- |
| X1 `.recent-card` / `.board-card` / `.recommendation-card` | 三类卡片只在 `gui/src/pages/start/StartPageView.tsx` 渲染；该组件**没有被任何页面导入**（`grep StartPageView src/` 只命中它自身与 `tests/start-page-view.test.tsx`）。当前 start 页是 `gui/src/pages/StartPage.tsx`，只渲染 `recommendation-card` 一类 | **代码路径缺口**：`.recent-card` / `.board-card` 无任何生产页面可达，需产品决定是否接线 `StartPageView`。2026-11-15 已决：组件、其测试与 `.recent-card` / `.board-card` 的 start-page 样式一并删除 |
| P4b `.model-service-row` | 由 `gui/src/features/connections/ModelServiceSection.tsx:119` 渲染；Connection 页可用，但列表为空 | 数据缺口：需要契约 fixture 返回至少一条 model service |
| X2 `.org-machines-row` / `.org-graph__node` | 分别由 `gui/src/features/network/impl/OrgMachines.tsx:35`、`gui/src/features/org/impl/sections/TopologyViews.tsx:52` 渲染；组织页 机器/拓扑 tab 可切换但列表为空，数据来自 `machineModel` / `topologyModel` props | 数据缺口：需要 fixture 提供 machines/nodes |
| P3 `.notif-panel` / `.toast` | `gui/src/components/NotificationCenter.tsx:53` 由 Rail 常驻渲染；面板需点头部铃铛打开，toast/center 项来自 reducer 的 `notices` / `centerNotices` | 待核：面板可能仅靠点击铃铛即可见；toast 需真实失败动作产生 notice |
