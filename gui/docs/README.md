# GUI 文档索引

新读者先看 [术语表](../GLOSSARY.md)、[当前架构](architecture.md) 和
[架构决策记录](architecture-decisions.md)。这三份文档描述当前代码边界及其依据；
具体接口与可用状态以目标提交的源码、契约和验证结果为准。

| 需要了解 | 文档 |
| --- | --- |
| 页面与组件规则 | [设计与研发规范](ui-development-standard.zh-CN.md)、[组件目录](component-catalog.zh-CN.md) |
| 预览和正式如何共享页面 | [共享入口映射](shared-ui-entry.zh-CN.md)、[单视图组合](single-view-composition.zh-CN.md) |
| GUI 启动与开发预览 | [开发模式指南](dev-mode-user-guide.zh-CN.md) |
| HTTP 客户端和生成器 | [请求客户端说明](http-request-client.md) |
| 业务模块公共面与依赖方向 | [模块边界检查](module-boundaries.md) |
| 各业务线的页面范围 | [业务线索引](business-lines/README.zh-CN.md)、[模块说明](modules/dev-01.md) |
| 主题系统与替换方式 | [主题系统](theming.zh-CN.md) |
| 视觉原则提示性检查 | [Refactoring UI 提示性检查](refactoring-ui-advisory.zh-CN.md) |
| UI 改动交接 | [交接模板](ui-change-handoff-template.zh-CN.md) |

历史计划、交接记录和截图保留各自时间点的结论。遇到冲突时，先核对
[仓库规范](../../AGENTS.md)、[根术语表](../../GLOSSARY.md)、当前源码及对应测试；
再更新本目录的现状说明，不把计划文字直接改写成已实现能力。
