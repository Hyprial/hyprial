# 重构后的开发边界

本记录描述 `refactor/reconstruct-modules` 的实际工作区结构，不代表已经提交、发布或完成 GUI 功能替代。原方案的分析数字与草案仍保留，当前执行证据见 [最终架构集成交付](../evidence/refactor-main-final-2026-10-03/FINAL.md)；原联合交付是其历史 checkpoint。

## Python

```text
src/hyprial/
├── kernel/       基础类型、契约、平台事实、进程与 actor 基础能力
├── identity/     agent/user 身份、home、认证与声明词汇
├── daemon/       durable 消息、会话、路由、通信、PAC 与运行生命周期
├── biz/          usage、quota、routine、workflow 等附加业务
└── shell/        CLI 装配、安装、登录及 GUI 生命周期
```

每域只有 `interface.py` 和包入口承担公共导出，实现与内部子系统在 `impl/`。跨域调用公共面，不直接导入另一域的实现；上层可以依赖下层，顺序为 kernel、identity、daemon、biz、shell。测试对内部观察点的引用不意味着那些点成为产品公共 API。

`hyprial.cli:main` 与 `python -m hyprial.cli` 保留为薄入口，根 `gui_build_hook.py` 也是打包入口而非第二份 GUI 实现。旧 Python 包路径不保留整套兼容 shim。CLI 子树由 shell 按语义装配；DaemonApplication 的方法按职责拥有唯一实现者与显式装配点，不以编号碎片或复制方法绕过文件大小限制。

daemon 是 durable inbox、会话与恢复的唯一权威；identity 不通过反向 import 借用 daemon 的策略。需要运行能力时注入明确端口，由上层 composition root 装配。PAC 在 `daemon/impl/pac/`，agent 身份在 `identity/impl/agents/`，harness 运行接入在 `daemon/impl/harnesses/`。

## 独立 GUI

`src/gui` 已接管为独立产品，旧 DSH 插件实现已从工作区删除，可从 Git 历史恢复：

| 模块 | 所有权 |
| --- | --- |
| presentation | 纯样式、布局与校验基础 |
| studio | GUI 文档、修订、存储及编辑工具 |
| session | 中立会话与命令交互，不新建业务会话权威 |
| transport | 显式 backend driver 与传输映射 |
| host | 可信身份注入、端口组合与工具宿主 |
| client | 呈现、composer 与可信插件生命周期 |
| product | HTTP 服务、资产与启动配置的 composition root |

跨模块只消费对应 `index.mjs`。Cordis 用于可信本地插件生命周期，不重新引入 DSH agent/runtime。Studio 的快速声明式自定义能力保留；真实 backend、浏览器 E2E、完整旧功能等价与插件沙箱不能从 mock/unit/build 结果推断。

启动必须显式提供 transport 配置与可信 principal，缺失时拒绝启动；浏览器请求不能自报可信身份。desktop 只包装同一 GUI 产品，不拥有第二份 session/inbox/turn。Tauri v2 增量现已进入本工作区；原联合交付中的 DEFERRED 仍是其历史状态，当前集成与新增验收见 [Tauri 主集成收据](../evidence/tauri-v2-integration-2026-10-03/README.md)。Windows、真实账户/网络和 release provenance 不因代码集成而自动通过。

## 测试与并行开发

`tests` 已按 kernel/identity/daemon/biz/shell、integration、tooling、support 和 fixtures
组织，测试职责归属以真实被测权威为准，不按旧文件名或 import 多数票机械分类。
路径与收拢证据见 [测试布局方案](test-layout-refactor-2026-10-03.md) 和
`docs/evidence/test-layout-refactor-2026-10-03/final-path-map.json`；更早 map 保留为历史 lineage。
50 个 >1000 行测试/辅助文件已按职责拆分；原 9988 用例在 owner/path 与主题映射下精确保留，
新增结构门 11 和 Tauri 集成 7 单独计账。当前 collector 10006、0 error，见
[收尾进度与证据](../evidence/refactor-main-final-2026-10-03/PROGRESS.md)。焦点原失败不改写为 PASS；
活动契约 runner 的职责拆分已收拢，默认门现已覆盖产品、tests 和活动工具：2581 代码文件、776 模块，最大 1000 行/7 直接文件，零违规；真实 E2E 与完整功能验收仍独立记账。

Tailcat 可以基于核验的未提交重构快照，在独立 worktree 完成网络替换，无需等待重构 commit。
主集成保持产品网络/身份/OrgFS/登录相关路径不再搬迁；共享 CI、GUI/Tauri 外壳和根依赖由主集成
持有，Tailcat 交相对冻结快照的独立增量。允许隔离开发不等于允许生产切换、发布或共享基础设施清理。

## 持续检查

```sh
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run ruff check .
npm --prefix src/gui ci --ignore-scripts
npm --prefix src/gui run check
npm --prefix src/gui test
npm --prefix src/gui run build
```

早期 CI 已加入结构与依赖硬门，GUI/desktop-only 变更也经过结构检查。结构门默认检查三个产品源码根、`tests`（包括 helper 与 Go fixture）及存在时的 Tauri Rust 源：含代码目录最多 7 个直属普通文件（包括同目录文档/数据），单代码文件最多 1000 行。第三方与生成目录仅按明确边界排除，不添加逐文件豁免。

活动脚本、契约 runner 与 sidecar 同时接受代码行数门；独立命令集合不是产品模块，因此不以命令个数判断模块嵌套。精确历史 `scripts/archived` 与 WORM 不改写，也不将活动代码挪入历史区避开限制。

行为改动先跑焦点测试，再做全量验证；测试使用隔离 home/socket/端口。现有失败、未授权真实环境、desktop 延期及未完成的发布 provenance 分开报告，不用放宽断言、skip/xfail 或伪造 stamp 声称通过。
