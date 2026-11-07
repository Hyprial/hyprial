# GUI 入口与公共能力收拢

本批是 dev-01 UI/应用组装调整，不定义新的 daemon/HTTP 权威契约，不修改 src/hyprial、原生壳或打包。最终业务目标 intg/gui-init，公共组装由 work-developer 统一集成；当前增量随 #2068 叠加至 #2065。

## 目录及责任

- `gui/src/app/capabilities.tsx`：应用能力唯一组装点。Web auth/setup共享一份platform transport；native/test显式注入能力。不提供自动demo回退。
- `gui/src/platform/transport/`：同源会话、nonce、有限重试。网络失败不自动重放写操作；仅服务端确认执行前拒绝的过期会话可重试一次。
- `gui/src/platform/auth/`、`setup/`：已有本地host能力客户端，保留各领域响应解码、错误及流取消。后续正式后端接口由后端owner提供，这些客户端只是替换入口。
- `gui/src/features/onboarding/`：原features/dev01/onboarding；视图与useFirstRunController分离。恢复、登录、设备和Pi准备由controller编排，页面负责输入、布局与焦点。
- `gui/src/features/account/`：设置页账号状态、登录窗口与取消。
- `gui/src/features/model/`：模型视图、Key保存与状态恢复；设置页保存不创建/启动Agent。
- `gui/src/components/forms/`：原dev01/forms的共享表单。
- `gui/src/pages/`：正式页面组装；Settings与FirstRun消费同一个capabilities provider。
- `gui/server/`：受限本地host；不允许各业务线另建任意shell调用通道。本批不改其操作协议。

旧features/dev01源码目录已移除，引用和测试随迁移更新；历史证据保留原路径，不覆盖过去报告。CSS类dev01-v17-*本批作为已有视觉选择器保留，不再表示代码owner；不保留重复实现目录。

## 三处跨线交接

1. dev02的Agent能力提供稳定targetId/displayName/lifecycle，dev01编排Squire初始化而不创建第二套注册/生命周期权威。现有setup接口尚未提供已确认目标ID时，不根据显示名或固定agent-squire构造真实身份。
2. dev01向dev03交接`{targetId,draft}`。共享reducer新增open-chat-draft：只接受已存在目标、保留历史、聚焦输入、不自动发送；不同的现存草稿不被覆盖。目标尚未可解析时须显示未接入。预览的模拟消息仍独立且标注未发送。
3. 首次完成必须是实际请求消息ID对应的真实Agent回复。`features/onboarding/handoff.ts`规定前端交接类型及confirmsFirstReply：排除fixture/preview、ACK、用户消息、其他Agent/会话/旧请求。此函数不是认证边界，只有受信聊天adapter负责映射真实观测；未接入前不标完成。

dev03尚未挂载真实收发，不在本批伪造接线；dev04组织加入仍非本机首次使用的前提。

## V17与产品交接

沿用已批准V17布局和基础组件，首次设置局部样式统一使用ui-*语义颜色；专属品牌尺寸、进度条和卡片布局仍局部作用域，不全局改其他团队页面。瑞华后续原型应吸收：设置页独立账号/设备/模型入口，首次设置复用同一状态；Key单一“保存并继续”；controller及目录变化不要求产品重画页面。当前Key更换受既有授权保护，未实现完整轮换，不暗示保存等于模型验证。

## 研发与验证

日常预览使用hyprial gui dev --gui-home <任务工作树>/gui；正式入口为根页面，专用步骤预览为/src/features/onboarding/preview.html，auth=local使用相同Web能力组装；无参数预览是显式隔离demo。

测试覆盖共享session/错误重试、恢复不重复登录/启动、Key不写浏览器存储、页面统一组装、草稿保留和回复关联。auth-browser.mjs使用隔离合成host及合成Key，覆盖预览到正式设置/首次设置共享状态；不证明真实授权或模型回复。真实聊天与Windows/macOS原生客户端验收另列，不以本批UI测试替代。

### 本轮实际验证（2026-10-29）

- `npm --prefix gui run typecheck`、`npm --prefix gui run build`：PASS。
- `node gui/tests/auth-browser.mjs`：PASS，增加正式根页面→设置→首次设置的状态复用验证；合成host，非真实账号/模型验收。
- `node gui/tests/dev01-onboarding-browser.mjs`：375/760/1440 PASS，另带截图目录重跑PASS；无网络业务请求、无浏览器存储写入。
- 全 `npm --prefix gui test`：208 PASS / 1 FAIL。剩余 `dev01-onboarding-browser.test.mjs` 在Vitest中等待账号标题可见超时；同一runner独立运行通过，原因尚未确定，不称全绿、不豁免为已知环境问题。交独立worker复验，不能以独立通过掩盖全套红项。
- Python结构门、模块边界门与git diff whitespace：PASS；本批未改Python产品代码，不宣称Python全量测试通过。
