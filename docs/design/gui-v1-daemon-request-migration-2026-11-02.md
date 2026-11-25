# GUI v1：daemon request 改造的分工与口径（Allen 2026-11-02）

> 适用于 `intg/gui-v1` 四条业务线（`gui/docs/business-lines/README.zh-CN.md`）。开工包 README 第 2 节第 3 步
> “先做改造”按本文执行。本文暂放在 `docs/design/`：只改 `gui/docs/**` 的 PR 目前在 CI 拿不到测试计划
> （CI_ROUTE 缺口，已记入工作项 M-emergent-org-structure-refactor-0051），缺口修好后并入开工包 README。

Allen 2026-11-02 裁定：改造由**各业务线自己改本线用到的方法**，不集中排队（四条线状态表里
“需先做 daemon request 改造”约 46 行，集中实现会成为 GUI 瓶颈）。

- **谁改**：本线负责人在自己的工作分支里改，和用到它的组件一起提交。
- **跨线共用的方法**（一个方法被多条线使用）：由最先需要的那条线改，其他线复用。
  动手前在本线文档状态表该行写「改造：本线」，复用方写「复用：<slug>」；看到别的线已登记就不要再改。
- **统一接入**：GUI 组件 → 功能接口 → 生成 client → daemon request HTTP 入口 → 方法注册表 → 业务实现。
  MethodSpec 定义参数、结果、错误与调用语义，MethodRegistry 绑定真实业务实现；业务请求统一走
  `POST /daemon/request/<method>`，不另建业务 REST 路由或直接读写 daemon 数据库。
- **功能分类**：除共享/独占外，区分 daemon 方法与启动安装、离线维护、纯显示等本地能力。
  已有 daemon 实现但缺 MethodSpec/绑定的，补齐后接入；本地能力使用对应的宿主接口。
  缺少业务实现或公开接口时，在状态表登记负责人、使用方及缺口，不注册空 handler。
- **评审**：方法改动由 `codex-daemon-request`（daemon request 分层改造线）独立评审；它不排队实现（2026-11-02 已确认接下通用口径与评审）。
  它暂时联系不上时，按本文口径自查，并在 PR 正文写明“口径评审待补”。
  合入前须完成独立方法评审；GUI 组件和用户流程另行评审。
- **共享生成物不手工解冲突**：`contract/daemon-request/openapi.json` 与 `gui/src/client/generated/*`
  在合并或 rebase 后一律重新生成（`uv run python scripts/verify/dump_daemon_openapi.py --write`，
  再 `npm --prefix gui run client:reference`），提交生成结果。
  生成后执行 `npm --prefix gui run client:reference:check`。接口模拟使用 MethodSpec examples；
  界面场景可自备测试数据，但不据此另定义请求或响应结构。
- **照抄示例**：已迁移的 `agent.list`（`codex-daemon-request` 指定）：参考它的 MethodSpec、真实 handler 绑定与契约测试；
  每个方法仍按自身语义验收。写方法另看 `request_catalog/agent/provider.py` 的 `agent.provider.default`（`mutates=true`）。

已定口径（2026-11-01 对 daemon request 线的答复，改造时照此执行）：

1. 超时参数显式校验，不静默截断：以 `message.status` 为例，省略取默认 2 s，合法范围 0 < t ≤ 10，
   其他值报参数错误。
2. 不接受“显式 null 等于省略”：`onBehalfOf: null` 报参数错误。
3. 日志/轨迹类读方法：保留显式离线模式；按调用身份限定可见范围；游标分页，默认 200 条、上限 1000；
   轨迹不完整时显式标记 `partial`，不当作完整结果。
4. 发送方认证从 observe 改为 enforce 另立一批统一做，**不在单个方法迁移里暗改**认证行为。
5. 上述默认值和边界仅适用于所指方法；其他方法明确类型、必填、默认值、范围及额外字段策略。
   方法的等待参数与 HTTP 总请求预算分别校验。
6. 声明公开错误 code/data，核对经过注册表、IPC 和 HTTP 后仍保留原定语义；结果结构覆盖真实
   生产方返回的合法字段，包括来源身份与代理信息。
7. GUI 宿主提供可信调用身份和访问令牌；浏览器传入的业务目标、代理等参数不能证明调用身份。
8. 按真实行为声明 mutates、retry、确认和幂等；消费读取、游标推进等也可能有副作用。
   写操作不自动重试；发送后断线或超时可能返回 dispatch=unknown，应显示结果未知。
9. HTTP 准入决定方法能否通过网页调用，检查真实绑定、调用身份、调用模式、确认和重试配置；
   当前支持 local-operator（本机操作者）、unary（单次请求/响应）、confirmation=none，
   以及只读 retry=safe / 写入 retry=never。超出范围登记缺口，不削弱声明来通过检查。
   运行时另查就绪状态。状态表区分已声明 / 已注册 / 已准入 HTTP / 可调用；生成 client 中出现
   方法名不代表可通过 HTTP 调用，其中也包含仅用于进程间通信（IPC）的方法。

其余通用口径（错误码命名、写方法幂等、身份字段）由 `codex-daemon-request` 补充到本文；补充前遇到
拿不准的，按既有已迁方法的做法，并在 PR 正文写出你的选择。

## 每批完成后的 Swagger / IPC 自检

使用测试身份和数据，隔离 HOME/XDG、状态、socket、端口、缓存与临时目录；HTTP 配置中的 backend
socket 指向测试 daemon，不使用日常 daemon 或生产凭据。身份、准入和就绪场景可自动化验证；
真实外部账号登录、扫码或授权需要人工时单独记录。

HTTP 入口提供开发 Swagger，需本地 Swagger UI 5.17.14 的 `swagger-ui.css` 和 `swagger-ui-bundle.js`：

```sh
HYPRIAL_REQUEST_HTTP_DEV_SWAGGER=1 \
HYPRIAL_REQUEST_HTTP_SWAGGER_ASSETS=/absolute/path/to/swagger-ui-dist \
uv run python -m hyprial.shell.impl.request_http serve \
  --config /absolute/path/to/isolated-http.json
```

配置项见 [HTTP 入口配置](../../src/hyprial/shell/impl/request_http/config.py)。访问 `/docs`，用入口生成的
受保护凭据文件中的临时 token 加载 `/openapi.json` 并 Try it out；token 仅保留在页面内存，不存入
URL、浏览器存储、截图或代码。

逐方法核对正常返回、缺参数/null/边界、错误 code/data、越权、未就绪、副作用和来源字段。
仅支持 IPC 的方法通过真实 IPC 验证，不为 Swagger 开放 HTTP。难以手工复现的断线等场景用自动化
覆盖；Swagger 自查不代替回归测试和独立评审。未实测项明确记录原因。

## 批次验证与提交记录

开发中跑受影响测试，模块批次完成后统一检查与独立评审，全仓回归在集成批次最终 head 执行。
公共基础改动按影响扩大验证；静态门与目标分支 CI 仍须满足。同 SHA 的有效证据可复用。
PR 附方法清单、使用方、固定 SHA、生成检查、测试结果及未测项。失败记录命令、环境和证据，
匹配基线复现后才能标“与本批无关”；已确认问题记入 [known-problems](../reference/known-problems.md) 或既有任务。
