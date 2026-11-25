# GUI 完整功能验收开发设计

日期：2026-11-11。目标基线：`origin/dev` @ `3f2914996ca49a3764fb536a1e412d4a616822dd`。现有隔离调试分支为 `feat/gui-docker-debug` @ `97129c031ca84befd99bd67d94a3582f15189b8d`。开始实现和最终验收时均须重新记录实际候选 SHA；旧结果不能外推到新提交。

## 目标和边界

完整交付以 [GUI 统一接线总架构](gui-live-0554/00-architecture.md) 的 G1–G8 和 [M2 放行条件](gui-live-0554/integration-gates.md) 为准。从空的隔离 home 经普通产品 GUI 完成账号入口、身份、设备和初始化，再对组织、agent、chat、设置四线验证真实读、真实写或明确的真实拒绝。验收同时覆盖源码与安装 wheel 的同一产品入口、浏览器会话、daemon 回执、写入断响应恢复、sidecar 网络路径和安全负例。

`make debug-gui` 保留为已初始化合成用户的隔离调试入口，不作为完整验收入口。其当前构建上下文没有 `sidecar/`，实际镜像中的 `verify_tailcat_sidecar()` 返回 `SIDECAR_MISSING`；即使加入二进制，它仍不会证明真实登录、跨节点与安装态。完整验收必须从同一个固定仓库 SHA 构建后端、GUI、sidecar 和 wheel，不能继续把本地 PR 后端与另一个 `dev` GUI 快照拼接后宣称版本一致。

## 交付拆分

1. **可复现候选产物与双节点实验场。** 在 #2624 中增加与原隔离调试入口并存的验收入口。固定单一 SHA、锁文件和构建平台；构建 tailcat，放入包的 `hyprial/_bin/`，在镜像构建时执行 `verify_tailcat_sidecar()` 校验协议版本和引擎提交。用两个全新 home、独立端口及本地测试 relay 验证真实 sidecar 进程、设备/节点连接和清理。无凭据的本地实验只证明协议与进程路径，不冒充外部 tailnet 验收。
2. **M2 首次旅程。** 在现有 MethodSpec → generated client → 同源宿主 → FastAPI → daemon request 链路上补齐空 home 的登录/注册 intent、身份与设备、初始化、状态恢复。每个启用能力必须有真实后端语义和授权；没有能力时保持明确不可用。对照已登记的 KP-025 修复首跑产品入口和浏览器契约，不删除失败断言或改成 fixture 成功。
3. **G1–G8 组合验收。** 在同一候选 SHA 上运行源码、wheel、真实浏览器和 daemon 回执检查；四线各至少覆盖一个真实读及一个写或明确拒绝。写请求断开响应后先查询回执，不自动重发。单独检查身份错配、过期 grant、跨 home、CSRF、未登录写入、错误 envelope 和进程退出清理。实际外部 IdP、provider、远端节点需要独立测试账号和运行环境；未执行时记录 `BLOCKED`，不能用本地合成结果代替。

每一部分单独交付可运行代码和验收证据，再推进下一部分。已有工作包若已在 `dev` 落地，以最新源码与测试为准；不重复实现平行的授权、状态库或前端业务桥。

## 运行结构

- **构建器：** 从单一 checkout 的固定 SHA 生成 tailcat 与 GUI 安装产物，记录输入 SHA、依赖锁、镜像 digest、sidecar `version` 记录。复用现有 `Dockerfile.hyprial-worker` 的 Go 构建和 pin 校验规则，但验收镜像必须包含正式 GUI wheel，而不是 worker thin wheel。
- **测试编排器：** 只管理自己创建的容器、临时 home、relay、端口和浏览器 profile。默认不挂载日常 Hyprial home、daemon socket、Lark 配置或宿主凭据。运行时秘密仅在明确授权的真实账号场景注入，不能进入镜像层、日志、截图或 Git。
- **验收驱动：** 浏览器通过一次性 handoff 登录同一产品 `App`；逐项记录页面动作、HTTP requestId、daemon receipt/业务状态与断言。Contract Mock 用于契约负例和状态组合；真实功能 PASS 必须来自真实 daemon。日志和截图先脱敏，再保存非秘密证据索引。
- **结果清单：** 每项包含候选 SHA、场景、预期、实际、证据路径、`PASS`/`FAIL`/`BLOCKED`/`UNKNOWN` 及原因。只在全部适用硬门 PASS、外部依赖场景完成并经独立审查后，才能写“完整 GUI 功能验收通过”。

## 关键失败行为

- 缺失或不匹配的 sidecar 在构建或启动前失败，显示 `SIDECAR_MISSING`/`SIDECAR_INVALID`；不能静默切到隔离模式。
- 真实 daemon、身份或 catalog 不可用时显示原始稳定错误；不能回退 Contract Mock 或 fixture。
- 写请求结果未知时保留 `dispatch=unknown`，只通过声明的查询或 receipt 方法恢复；不能把超时显示成写入失败后允许盲目重试。
- 外部账号、provider 或 relay 不可达时相应场景为 `BLOCKED`，其他已验证场景仍独立报告；不把 skip 计为 PASS。

## 逐阶段验证

第一阶段先用失败测试证明当前候选镜像找不到 sidecar，再使新验收镜像在构建期通过 `verify_tailcat_sidecar()`；运行两个隔离节点验证版本、连接与销毁。第二阶段对空 home 做红绿测试和真实浏览器首跑，修复 KP-025 的入口契约。第三阶段执行 G1–G8 对应焦点测试、结构/模块边界、Spec/OpenAPI/client 一致性、GUI typecheck/build/浏览器门、适用 Python 套件及 wheel 无源码/Node/npm/Git 启动。每阶段保留精确命令、退出码和未测范围。

## 不在本设计内假定已完成的事项

当前 PR #2624 的手动页面测试只证明隔离 GUI 可访问；它没有 sidecar、真实账号、跨节点或安装态证据。外部 IdP/provider 的测试账号和许可尚未核实。已有 `gui-live-0554` 文档与测试可能继续变化，实施前必须重新核对目标分支与现有负责人产物，避免覆盖并行工作。
