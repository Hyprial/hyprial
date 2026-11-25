# C 集成容器与首批范围

> 本文记录 2026-11-01/02 的历史分工与验证状态。2026-11-08 起集成与发布遵循
> [dev-first 发布规则](../../guides/release-rules.md)，下文 release-first 路线不再是操作要求。
> 本次仅集成 #2268 的单路由检查器，不将冻结的 C00 清单或旧模块状态冒充当前验收。

C 将已有 CLI 使用的 daemon 业务接入 MethodSpec 与真实绑定，使后续实现能复用声明、验证与文档生成路径。命令入口和业务方法是多对多关系，231 个可执行入口不等于 231 个待新增方法。

当前路由：各片 PR → `intg/daemon-request` → `release/v0.5.5.4`。旧到 dev 的路线退役。release 基线为 `6c38f71f9bf5bc5b10230b4c0351d4d1aa043eba`，容器已普通 merge-forward，旧提交不重写。所有实现片和容器均由独立 reviewer 审阅，e2e-verifier 审合；owner 不自行合入 release。

首批：

- C00：231 个现有入口的完整、可核验命令映射。固定源坐标、真实 callback、现有方法/本地业务/豁免/依赖分别有证据；首版有错误已退回，不以数量校验替代内容正确。
- C01：已有 `message.status` 的参数/结果/错误声明与真实绑定，覆盖 `message status`、`message delivery status`，并核对 `message trajectory` 依赖。复用既有权限、会话及状态查询，不新增状态权威；HTTP 暂不暴露。

Org/lifecycle 领域权限、状态机与恢复由 Allen 定稿；move 业务属于 B。M 已交付五方法与 H HTTP 集成维持各自来源，不重复实现。C 首批完成不等于 Org/lifecycle 完成，也不等于整个命令面迁移完成。

容器保持 WIP：最终固定 intg head 必须包含已接受的 C00/C01，完成焦点/隔离真实 IPC、结构/模块边界/Ruff、必要全量验证和基线归因，并有独立评审。FAIL/BLOCKED/UNKNOWN 如实保留，不把源分支验证冒充集成验证。当前尚无实现片合入，无产品验收结论。

## 2026-11-02 执行更新

后续分工、C01/C02交接、未认领方法并行批次和批次验证节奏以 [MethodSpec并行安排](methodspec-parallel-2026-11-02.md) 及 [覆盖台账](methodspec-coverage-2026-11-02.json) 为准。首批历史范围保留；本次不启动 CLI 迁移，也不把 GUI 业务线分工重复集中实现。
