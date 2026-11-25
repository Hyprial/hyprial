# SAFE-R1 测试说明

结论：本组测试把已确认的 5 项界面改动、开发模块标识和正式构建隔离写成 7 个检查点。生产源码未修改。

## 检查点

| 编号 | 优先级 | 来源 | 检查内容 | 自动化入口 |
| --- | --- | --- | --- | --- |
| TC-001 | P0 | 用户反馈 1 | 页面背景为纯白，不含蓝紫渐变。 | `safe-r1-layout-contract.test.ts` |
| TC-002 | P0 | 用户反馈 5 | 1440 和 760 都固定应用外框，普通页只在内容区滚动。 | `safe-r1-layout-contract.test.ts` |
| TC-003 | P0 | 用户反馈 4 | 主导航不显示“设置”；账号菜单按设置、连接、登出排列（没有帮助能力，不渲染帮助项）。 | `safe-r1-ui.acceptance.test.tsx` |
| TC-004 | P0 | 用户反馈 3 | 产品外框内不显示演示、样例、fixture 或占位服务。 | `safe-r1-ui.acceptance.test.tsx` |
| TC-005 | P0 | 用户反馈 2 | 聊天页直接占用主导航右侧工作区，不保留卡片边距、边框或圆角。 | `safe-r1-ui.acceptance.test.tsx`、`safe-r1-layout-contract.test.ts` |
| TC-006 | P0 | 用户反馈 6 | development 构建和公开组件包含 dev-01 至 dev-06 与开发模块索引（只列模块编号与名称，不显示任何不可用说明）；禁用开关后不渲染。 | `safe-r1-dev-modules.acceptance.test.tsx`、`safe-r1-build-contract.mjs` |
| TC-007 | P0 | 安全 R1 门禁 | production 构建即使设置开发开关，也不含 dev-XX、fixture、演示、样例或占位服务。 | `safe-r1-build-contract.mjs` |

## 执行方式

1. 运行界面与布局测试：`npm exec -- vitest run tests/safe-r1-ui.acceptance.test.tsx tests/safe-r1-layout-contract.test.ts tests/safe-r1-dev-modules.acceptance.test.tsx`。
2. 运行双构建检查：`node tests/safe-r1-build-contract.mjs`。
3. 集成完成后运行完整验证：`npm run verify`。

## 集成要求

- 新测试在功能尚未写入的基线上必须失败，且失败原因必须对应缺失的界面行为。
- 双构建检查使用临时目录，不覆盖仓库的 `dist`。
- “暂不可用”只表示功能尚未接通。界面不得显示成功，也不得发送真实业务请求。
