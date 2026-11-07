# dev-03 正式聊天和飞书本机接入

代码位于 `feat/gui-dev-03`。依赖 `feat/gui-dev03-chat-backend` 的消息对账和
`feat/gui-dev03-channel-backend` 的条件绑定、App 授权公共接口。PR 统一面向
`intg/gui-init`，先集成两项后端，再验收 GUI；不要单独安装旧版 Python 包并宣称完整接通。

## 启动

在包含上述三个改动的独立 worktree 运行 `uv sync --extra test`，GUI 使用 Node24，
运行 `npm --prefix gui ci --ignore-scripts`。使用既有已初始化的本机 home 和实际 owner，
显式设置以下环境；示例值必须替换，不能原样当作真实身份：

```sh
export HYPRIAL_GUI_BACKEND_DRIVER=daemon
export HYPRIAL_GUI_PYTHON=/absolute/worktree/.venv/bin/python
export HYPRIAL_HOME=/absolute/initialized-home
export HYPRIAL_GUI_PRINCIPAL=user:your-owner
npm --prefix gui run dev -- --host 127.0.0.1 --port 4173
```

若 daemon state 位于 home 之外，还需设置真实 `HARNESS_STATE_DIR`。不要同时设置
`HYPRIAL_BACKEND_ENDPOINT`。GUI 不会自动启动/升级 daemon，也不会创建飞书 App。
默认不设置 daemon driver 时仍使用开发占位。生产构建后启动时会重新查询实际模式，
不会仅凭构建时变量决定是否连接真实服务。

## 逐项验收

1. 选择真实 Agent，发送带唯一标记的文字。确认权威历史显示原消息和真实 Agent 回复；
   刷新页面后仍各出现一次。发送接受回执不能替代真实回复。
2. 在 daemon 已接受后断开浏览器响应。界面保留未知状态和原请求，禁止重复发送；
   刷新/重启宿主后只查询原请求，确认后解除锁定。不能以“发送前断网”替代此用例。
3. 明确拒绝时保留草稿；用户修改后重新发送才建立新请求。未知操作不能用新 key 重发。
4. 飞书页面选择已有配置，发起 App 权限授权，打开返回链接并由用户完成审批。
   分别验证拒绝、取消、过期、授权后重查权限。此功能不包含用户 OAuth。
5. 查看旧/新绑定并确认切换。确认前由另一客户端改变绑定，应提示冲突而非覆盖。
   超时后先查询绑定；只有权限、绑定和在线三者均满足时显示已连接。

自动验证：`npm --prefix gui run verify`；
`node --test gui/server/live/*.test.mjs`；
`node gui/tests/live/browser.mjs`（明确标注的 HTTP 替身）；
`uv run pytest tests/shell/gui/live tests/daemon/delivery/message/test_send_lookup.py
tests/shell/gui/adapter/lookup tests/daemon/adapters/lark/identity/authorization -q`。
浏览器可通过 `HYPRIAL_BROWSER_EXECUTABLE` 指定已安装 Chromium，不硬编码 macOS 路径。

消息权威始终在 daemon。宿主只保存未确认 UI 请求并在浏览器确认接收证据后清除文字；
不会建立第二份聊天记录，也不会在后台重发。真实飞书审批需用户操作，未执行的场景应记为
UNKNOWN/BLOCKED，不能把自动化替身结果当作生产验收。
