# CLI → daemon request 设计阶段独立验收

**结论：PASS（仅设计与静态盘点）。** 产品行为、真实 daemon、TTY、升级与 E2E：**UNKNOWN**；本轮没有产品迁移。

基线为 `533ef27ca42e53230224d55e58ba074f93d0eb15`，目标 Forgejo `HyprialOS/hyprial` 的 `dev`。交付前 `dev` 更新到 `b15393e3933b6eda6fbeffe503761876a77e6b0b` 的说明与最初盘点基线分开陈述；#2727 合入后明确要求重新盘点。

- `imports.json` 与固定 baseline 按 `(path,module,symbol,count)` 精确一对一：417 条唯一边、470 次出现，a=173、b=192、c=52，69 个文件；`imports.md` 417 个 ID 各一次。四域 daemon/kernel/identity/biz 分别 223/129/53/12。901 条可核对的引用文本与源码行一致。
- `methods.json` 与 legacy baseline 精确一对一：175 个方法、7 个前缀；87 个已同名真实声明、88 个待声明现有操作、0 个已证可删。registry 共 138，另 51 项只在 registry；`methods.md` 逐项各一次。静态同名不冒充参数、权限、错误、副作用等价验收。
- `exceptions.json` 与既有 20 项 manifest 一致且不与 417 条 baseline 重叠；52 个 c 候选与 import 清单完全同集，其中 15 个标准客户端职责仍待精确登记、37 个 owner 待审。I417 已按多消费者公共装配项改为 owner 待审；新候选未写入 CI 许可，离线/无锁旧条目没有被洗成已证明安全。
- 每命令组 E/F 文件数与固定源码相符；成本区间明确为估算，逐组列 JSON/文本、退出码、错误码风险与新方法判定。先处理低成本 b 词汇，生命周期、初始化和升级后置；共享文件串行集成。未改禁改目录，当前工作树只有新 `docs/design` 文件。
- 固定基线实跑 PASS：结构门、模块边界门、import ratchet、request-contract 汇总、独立方法注册、OpenAPI 快照、daemon-request 架构门。日志位于 `/workspace/work/acceptance-*.log`；方法注册首次无项目依赖运行因缺 `pykka` 未形成结论，依赖就绪后带 `--base-ref` 重跑 PASS。

验收边界：这些门证明当前源码/台账和文档清单一致，不证明未来任一迁移 PR 或产品运行通过。owner 对 37 个本地例外及潜在行为变更的具体决定仍是第二阶段入口；不得扩 baseline、搬业务至 kernel/helper 或绕过门禁。
