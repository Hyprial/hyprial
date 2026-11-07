# 独立 Hyprial GUI：深模块重构 checkpoint（D1）

## 目标与授权

本轮目标是从 D0 提取原型形成清晰的深模块与公共接口，隐藏实现细节并保留已验证行为，为后续真实后端开发准备。不是旧 GUI 全量功能复刻、浏览器功能补齐或发行切换。

用户已要求设置 goal 并使用 Luna 子代理并行实施。基线 4382f94689adcc1c4ca1cacc3961ad2af734f544；D0 来源 c3fa0178c5fb6ce3d043e384e163c2089627a12a，交付文档为未提交文件。D0、原型来源、主工作区、Python 线、旧 src/gui/desktop 均只读。本轮仅新增 src/gui-next 与本设计/本线新 evidence，不 commit/push/合入/删除存量。

## 信息隐藏与依赖

`host` 是组合/认证边界，通过注入的 authenticator 获取可信身份，而不是自行实现 HTTP/认证服务；只消费三个模块的公共面。`session` 隐藏 mock/transport 数据映射、命令幂等、游标与状态；不持有真实业务 durable owner。`studio` 隐藏文档 schema/修订/权限、存储锁/原子写与工具定义。`client` 隐藏 React 片段装配、composer pending intent 与 Cordis/slots/插件生命周期。会话与 Studio 模块互不导入，client 业务调用只依赖注入端口，不导入 host/session/studio 的 impl。纯展示基础模块 `presentation` 统一样式契约、校验、contrast 与 token 映射，供 Studio/Client 经公共面单向消费，避免复制两套实现。所有 impl 只能通过同模块 index.mjs 访问。

公开 API 选择少量稳定工厂/意图方法，不能把每个内部 helper 变成 facade export；不强制把多个小文件机械包上 facade。拆分依据是隐藏的复杂职责而非文件行数。

## 公共面协议（子代理共享，变更需协调者统一）

### studio/index.mjs（Studio 代理所有）

导出 `createStudio(options)` 与 `registerStudioTools(registry, studio)`。
options 使用显式 `persistence: 'memory'|'fs'`（默认 memory）、`root`（fs 必需）、可选 memory seed/now；具体命名可先与协调者核实。
studio 返回 `{ invoke(request, trustedContext), close() }`；request 为现有 `{operation,...args}`。context 必须显式包含 source='user'|'agent'、非空 principalId，agent 还必须有 sessionRef（映射内部旧 sessionId，不改旧 fixture identity）。不接受缺/空 context，不从请求体生成权限。
registerStudioTools 使用 registry.register(definition)->disposer，definition 保留 h2b_gui_* 名称、parameters/description/output，execute(args, trustedContext) 直接传受信执行上下文而非 DSH exec；返回一个整体 disposer。registry 本身由 host 注入，不由 Studio 建第二 runtime。

### session/index.mjs 与 host/index.mjs（Host/session 代理所有）

`createMockSessionAdapter({fixtures?,...})` 返回 D0 会话端口，但所有 operation 以可信 context 为首参数：
- sessions.list(context)
- sessions.history(context, sessionId, options)
- sessions.events(context, sessionId, since)
- sessions.send(context, sessionId, args)
- sessions.cancel(context, sessionId, commandId)
- capabilities.get(context)
context shape 同上。principal/sessionRef(or incarnation)/目标session/operation 隔离去重，语义载荷冲突拒绝。不要宣称进程重启 exactly-once。history cursor opaque 且不跨目标会话复用。fixtures 导入显式而不是读取用户状态。

`createHost({sessionAdapter, studio, resolveContext, ...})` 返回 `{request(input, credentials), close()}`。
input 为 `{service:'sessions'|'capabilities'|'studio', operation, sessionId?, args?}`。request 先经注入的 resolveContext(credentials) 获取可信 context，再分派；拒绝客户端 source/principalId/trustedUser/context 等覆盖。不要默认 localhost/undefined=context user。没有本轮真正 HTTP 网关需求；薄内存边界足以为之后 Node transport 提供端口，避免扩充产品功能。

### client/index.mjs（Client/Cordis 代理所有）

公开 `createComposer({adapter, sessionRef})`，保持 D0 composer getter/setDraft/submit/cancel 行为，但 adapter 是已经绑定可信身份的 UI 端口（sessions.send(id,args)/cancel(id,commandId)），不是业务 owner。
待确认 intent 固定目标+文本+key；ACK 丢失后编辑新草稿不能被旧意图 ACK 成功；显式 retry 或有清晰文档的 submit 重试旧快照均可，不扩充功能。
静态模块化保留一组 D0 已运行的真正提取 React 呈现（例如 Workspace + 会话/工具卡预览）。导出最小 render factory/组件，不 runtime new Function/eval，不只是携带但未执行原件。
`createUiRuntime({trustedPlugins,...})` 隐藏 Cordis/context、slots registry 与装卸；返回小的 mount(id)/unmount(id)/dispose() 公共面，可提供 slot 读模型供 React呈现。插件由受信组合根提供固定 registry，浏览器/agent 不提供代码/路径/URL。新默认插件用箭头 apply/framework effect；卸载清理要测试。不要求完整浏览器功能/所有旧 slots 已等价。

## 最小安全与行为修复

D0 主模型 probe 已证明：Studio 无 context 默认 user、幂等 key 跨会话碰撞、同 key 不同载荷被旧 ACK 覆盖、composer 丢 ACK 后改草稿被错误确认。承接后 first-fail→修复→回归；这些直接影响公共面正确性，必须修。

源码可耐久化：全部必要源码/fixtures/lock/NOTICE/provenance 放新 repo 路径；不能只交 /tmp 哈希。bootstrap 源目录指定、清单不自引用/包含可变报告。只做具体修正，不扩建通用自动迁移工具。

## 结构收拢结果

| 模块 | 小公共面 | 隐藏的复杂职责 |
| --- | --- | --- |
| Studio | createStudio / registerStudioTools | schema、修订、权限、发布与恢复、工具定义、fs 锁与原子写 |
| Session | createMockSessionAdapter → sessions / capabilities 端口 | fixture 映射、游标、命令幂等与取消；后续 transport 替换点 |
| Client | createComposer / createWorkspaceRenderer / createUiRuntime | React 注册与布局、pending intent、Cordis fiber 和槽位清理 |
| Host | createHost → request / close | 受信 context 解析、端口分派与组合生命周期 |
| Presentation | 样式契约及六个纯函数 | palette、contrast、继承、语义 token 与兼容 alias；Studio/Client 唯一共同样式权威 |

依赖为 `Studio → Presentation ← Client`；Session 独立；Host 的业务端口和认证器通过注入接入。实现文件跨模块导入、反向 foundation 依赖和公共 export 扩张均有负面测试。没有为了目录整齐重新分发业务 owner，也没有引入新的泛化迁移框架。

35 项单测与集成测试 PASS，13 个运行文件的有界依赖检查 PASS。干净临时目录经离线 npm ci 安装 12 个锁定依赖后，完整 verify 再次 PASS。Node v26.7.0 / npm 11.19.0 为实测环境；Node 24 单独兼容性未测。详细记录见本线 evidence/verification.json 与 summary.md。

## 分工与验证

Luna Studio：studio/**、tests/studio*.test.mjs、本线 evidence/lanes/studio.md。
Luna Host/session：session/**、host/**、tests/session*.test.mjs、tests/host*.test.mjs、本线 evidence/lanes/host.md。
Luna Client：client/**、plugins/**、tests/client*.test.mjs、tests/runtime*.test.mjs、本线 evidence/lanes/client.md。
主模型：package/lock、shared入口、integration/architecture gates、scripts、provenance/NOTICE、最终设计/证据及收拢。

各代理完整迁移/测试/修复；共享目录不得互写。所有源码编辑用 apply_patch，测试临时 root/socket/profile/cache；仅 mock/合成运行，不读取真实配置或访问 daemon/model/Lark。根 Python 环境、旧 GUI、shared CI/contract 一律不改。

验收：模块公共面与依赖方向检查、完整架构单测、保留 D0 相关行为的集成 smoke、具体反例、fs 临时存储基本原子写/修订/恢复、Cordis生命周期、构建/静态呈现、新目录重建、scope/integrity证据。真实浏览器/全功能/真实后端/发行保持 UNKNOWN；不把这些宣称完成，也不把它们强行扩为本轮目标。

## 后续阶段

Python 重构和本 GUI checkpoint 并行。接下来先审阅两个产品树的真实公共面，再在干净联合 checkout 做真实 Hyprial 端口/受信 agent 工具接线；发行/desktop/数据连续性/回滚另行验收后才能考虑删除旧代码。

D2 的最小开发任务是实现一个消费 Python 重构后公共面的真实 adapter，与当前 mock 保持同一端口；另由认证组合根生成 user/agent context 并将 Studio registry 接入真实执行边界。SessionActor 的 lifecycle/list/runtime 投影不等于正文/tool history 权威，接入前必须分别定位提供者。不要在 Node 增建第二套 durable session/turn/inbox owner。

IR-1/2/3/9/10 移到 D2/D3 联合打包与切换；IR-4 待 Python CLI 完成后复验；IR-5 是 D2 会话/正文/工具执行 context 接口；IR-6 既有 PAC Python 消费者仍由 Python 集成所有者处理；IR-7 Desktop 待 D3；IR-8 新目录 CI 分类在联合集成树审阅，不在本树直接改共享 CI。旧 G2a+IR-1/G2b 保持 SUSPENDED/SUPERSEDED，并未执行。以上是后续计划，不是本次真实后端或发布授权。
