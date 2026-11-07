# dev-03 daemon 方法映射（只读，当前 e703 基线）

2026-10-28。只读源码定位；未调用 daemon、Lark、授权、业务请求，未读取真实凭据，未运行测试或修改仓库。本表是当前基线的旧方法实现事实及 GUI 接入候选，不是 A05 新契约或网关 ready 声明。用户公告：A05 已在 `intg/cli-command-surface@623e1f95` 整合公共 daemon_request 客户端，GUI HTTP 网关尚未交付；主执行者已核对公告及公共消费基线的固定提交 `a209031c6ba9171558e4126c17c2d8dac5d85ba5`；没有合并该分支，不能把它的契约自动套到当前 `e703c6690`。

源码缩写（均相对仓库）：

- `CHAT` = `src/hyprial/shell/impl/gui/adapter/chat.py`
- `HOST` = `src/hyprial/shell/impl/gui/adapter/host.py`
- `ROUTER` = `src/hyprial/daemon/impl/ipc/router.py`
- `SEND` = `src/hyprial/daemon/impl/application/messaging/inbox_surface/send.py`
- `HISTORY` = `src/hyprial/daemon/impl/application/messaging/inbox_surface/history/__init__.py`
- `QUERY` = `src/hyprial/daemon/impl/application/messaging/inbox_surface/queries.py`
- `STATUS` = `src/hyprial/daemon/impl/application/messaging/delivery/status_queries.py`
- `DIRECTORY` = `src/hyprial/daemon/impl/application/messaging/status/views.py`
- `ADAPTER` = `src/hyprial/daemon/impl/application/actors/agents_admin/adapters.py`
- `CALLER` = `src/hyprial/daemon/impl/application/messaging/visibility.py`
- `OLDCLIENT` = `src/hyprial/shell/impl/cli/commands/common/support.py`

## 方法表

表中 errors 为已定位的代表性校验/结果，不是宣称穷举全部传递错误。所有 GUI 方法须由 work-developer 按 A05 版本确认、共享网关白名单与权限接线后才启用；否则零业务请求。

| 页面用途 /候选底层方法 | 当前 params → result / errors | 读写、可信身份与权限 | 超时/重试、确认/预览/幂等 | 实现与验收状态 / 准确源码证据 |
|---|---|---|---|---|
| 聊天发送：桥 `sessions.send` → daemon `message.send` | 桥 `{sessionId, request:{text,idempotencyKey}}`。桥从绑定产生 daemon `{from,to:[...],message,summary,conversationId,idempotencyKey,sessionRef?}`。daemon 返回 `{ok,actingPrincipal,operationId,conversationId,deliveries,messageId?}`；桥仅全体可证实受理时返回 `{status:'accepted',commandId,conversationId,messageId?,deduplicated?}`。校验 invalid-argument/identity-rejected/unknown-session；daemon-rejected 包装底层 code/data；部分拒绝 send-rejected 含 acceptedTargets；未知投递/畸形结果/传输失败为 uncertain。 | 写。sender、会话绑定和可选 fence 由可信 composition root 提供，浏览器不能传 from/actor/principal/token 等。daemon `_authenticated_message_caller` 验证主体；session caller 另执行 send-to/channel 权限。 | idempotencyKey 原样透传并成为 operationId；agent 投递 ID 按 operation+target+index 由 daemon 生成。无已确认 GUI 预览/确认协议。桥没有自动写重试；未知只保留原 key/请求，按本任务先核对，禁止擅自新写。受理≠送达/回复。 | 旧 daemon 和桥实现存在，GUI HTTP/共享控制器缺失。注册 `ROUTER:605`；桥构造 `CHAT:535`、`:545`；参数校验 `:783`；回执投影 `:822`；daemon 主体/权限 `SEND:73`、`:114`，operationId `:173`，回执 `:563`，幂等 agent ID `:404`。测试源码 `tests/shell/gui/adapter/test_app_chat_adapter.py:134`、`:174`、`:292` 存在，本次未运行。 |
| 聊天历史：桥 `sessions.history` → `message.history` | daemon `{from,conversationId,limit:1..200,cursor?,sessionRef?}`；旧桥默认50，仅 v1 cursor/limit。daemon v1 `{ok:true,conversationId,entries,nextCursor}`；桥 `{events:[{eventId,kind:'user'|'assistant',text,at}],nextCursor}`。daemon 另有 mode tail/older/after，旧桥未暴露。invalid-argument（字段、limit、opaque cursor/会话/身份不匹配）；无认证 IPC 调用拒绝；桥不同会话/乱序/重复/畸形答案 uncertain。 | 只读 durable conversation，参与者取认证主体，不能由浏览器扩大。带 session/operator/adapter 身份；新的历史读取不继承 observe 模式的无认证兼容窗口。 | 只读重试可由统一控制器按截止时间/预算做；cursor 原样，不能重铸或跨参与者/会话复用。无写确认/幂等键；读取失败保留既有历史并标陈旧。 | 注册 `ROUTER:541`；桥 `CHAT:640`、`:667`、`:769`；daemon `HISTORY:66`、`:77`、`:123`、`:132`。模式/字段许可 `HISTORY:33`；旧桥只传cursor/limit `HOST:347`。测试源码 `tests/daemon/delivery/message/test_message_history.py:385`、`:422`、`:562`，未运行。 |
| 发送结果/收据查询：daemon `message.status` | `{from/actor,sessionRef?/onBehalfOf?,messageId?}`（可信根注入身份）；返回 `{ok:true,from,records,diagnostics,messageId?,state?,holdExpiresAtMs?}`。有 ID 时无最终记录返回 pending 或 unknown；records 与 mesh diagnostics 不是 completeness 保证。errors: 身份/fence、无效 messageId 等传递错误；mesh不全可能是正常结果里的诊断，不是确定写失败。 | 只读持久 verdict，按认证 sender 查询；指定已收到消息另可读本地主体的 recipient 记录。底层不是通用 requestId 查询。 | 可重新读原 messageId；空/unknown 不能证明原写未发生。当前方法不按 operationId/idempotencyKey 过滤；若发送响应丢失且无权威 messageId，缺失新 GUI 对账映射，不能客户端推导消息ID或把 requestId 当 messageId。无写确认。 | 注册 `ROUTER:609`；`QUERY:531` 转交；主体/参数 `STATUS:126`、`:155`；结果/非终态 `STATUS:296`、`:338`。旧 HOST 无对应桥操作（HOST:323–361 列表），需新统一契约。 |
| 进度：桥 `sessions.events` → daemon `progress.list` | daemon `{from,deliveryId?,sinceSeq?,sessionRef/onBehalfOf?}`；旧桥只传 fixed from 与正 sinceSeq，没传配置 sessionRef。daemon `{ok:true,events,daemonEpoch}`；桥按 conversationId 过滤 → `{entries:[{id,seq,state,at,summary}],terminal:null|{terminal,eventId,at}}`。invalid-argument（非负游标）、认证/fence错误、畸形 event uncertain。 | 只读认证主体 consumer keys 的 progress，不是消息正文历史，也不是任意请求的受理查询。 | seq 为单个 delivery stream 内游标，不是全局序号；0 在旧桥意为从头。跨投递/会话不能把一个最高seq作为统一完整性保证。只读重试预算由网关统一决定；不据进度缺失重发写。 | 注册 `ROUTER:603`；daemon `QUERY:487`；桥 `CHAT:557`、`:585`、`:593`。需确认旧桥未携带sessionRef在新认证路径是否可用，不能直接复制为新接口。 |
| Agent/节点目录：桥 `targets.list` → `hosts` + `targets` | 桥无params；daemon `hosts:{}`、`targets:{kind:'agent'}`。daemon hosts含 nodeId/status/observedAtMs/ageMs/stale/lastError；targets含 targetKind/targetUri/actor/status/deliverable/hosted?/hostedBy?。桥检查后各取最多200项返回 `{nodes,targets,counts,truncated}`，只保留部分目录字段。invalid-argument（kind）；畸形/重复/不deliverable/非agent行 uncertain。 | 只读目录。targets 对 hosted caller 应用 visibility grants；旧桥 params 不携带 caller selector，不能把它的无选择器投影当浏览器权限白名单。Agent“可投递/在线”不代表 GUI/model ready。 | 可重读；status陈旧/unknown不可变造online。无写确认/幂等键。新网关须确认目录权限、上限和陈旧字段是否保留。 | 注册 `ROUTER:519`、`:521`；daemon `DIRECTORY:754`、`:844`、`:856`；桥 `CHAT:431`。Agent身份供dev-02统一消费，dev-03不建第二目录真值。 |
| 会话绑定：桥 `sessions.create/list`（没有同名 daemon RPC） | create `{targetUri}`，先读 daemon `targets(kind=agent)`，可信根按固定sender+target生成/复用sessionId/conversationId，返回 `{sessionId,targetUri,status,created}`；list是已配置/ensure绑定，无daemon调用。invalid-argument/identity-rejected/unknown-target。 | 不是新 durable会话权威；create改变桥实例绑定，目录读由daemon负责。浏览器不能指定sender/session/conversation。 | 相同绑定复用；这是旧桥行为，不能擅自认定新GUI沿用uuid5算法。无新GUI预览/确认定义。 | `CHAT:460`、`:493`、`:516`、`:612`；`HOST:325`、`:330`。需集成人决定正式会话来源与接入，不由dev-03另建全局state。 |
| 渠道查询：daemon `adapter.list/status/pins` | list `{}`→`{ok,adapters:[...payload,agent]}`；status `{name}`→`{ok,adapter:payload}`；pins `{}`→`{ok,pins:{adapter:agentURI}}`。ADAPTER_NOT_FOUND、INVALID_ARGUMENT。 | 只读本机gateway运行状态/绑定。旧注册点和handler没有新的GUI身份/权限白名单；新网关必须映射可信操作员/授权范围。运行状态≠App scope授权、用户OAuth授权或GUI四步完成。 | 只读重试预算、陈旧投影需新共享标准；无写确认。单靠status/pins不能认定授权成功。 | 注册 `ROUTER:649`、`:651`、`:661`；`ADAPTER:58`、`:69`、`:200`。旧桥HOST没有这些操作。 |
| “切换渠道”候选之一：daemon `adapter.pin/unpin` | pin `{name,actor}`→`{ok,adapter,actor,previous,changed,pins}`；unpin `{name}`→`{ok,adapter,previous,changed,pins}`。ADAPTER_NOT_FOUND、AGENT_NOT_FOUND、typed PinConflictError.code、INVALID_ARGUMENT。 | 写持久channel→Agent一对一绑定；目标必须本机已存在Agent。含义是“此adapter收到的所有DM转给此Agent”（ADAPTER:237），不是已确认的GUI切换渠道含义或授权接口。旧handler无新GUI操作权限证明。 | 无requestId/idempotencyKey/preview/confirmation-token/CAS字段。重复同pin可changed=false，但这不是覆盖并发变更安全/未知写重试合同。需确认受影响旧Agent/渠道、预览/确认、冲突与对账策略，超时先读pins不自动重写。 | 注册 `ROUTER:657`、`:659`；实现 `ADAPTER:141`、`:181`；本机限制 `:234`。`tests/daemon/application/adapters/test_daemon_adapter_pin.py` 存在未执行。须业务owner确认“切换”确指pin；否则仍缺正式方法。 |
| 适配器启停：`adapter.start/stop/reload`（辅助，非授权/切换替代） | start/stop `{name}`，reload `{}`；返回 changed/adapter 或 added/updated/removed/removedRunning。ADAPTER_NOT_FOUND、ADAPTER_START_TIMEOUT/ADAPTER_START_FAILED、ADAPTER_RELOAD_FAILED 等。 | 写运行状态/重载配置，不授予Lark权限、不完成Agent渠道选择。GUI是否需要这些方法尚未确认，不扩大dev-03范围。 | 无新GUI预览/确认/幂等字段；start有daemon生命周期deadline，但不是HTTP截止时间定义。未知先读status。 | 注册 `ROUTER:647`、`:653`、`:655`；`ADAPTER:34`、`:79`、`:112`。 |
| 飞书授权：当前**未定位到可直接复用的授权 daemon RPC** | 旧CLI的 App scopes authorize (`src/hyprial/shell/impl/cli/commands/adapter/authorize.py:137`) 直接调用 configured_scope_client/apply_scopes 或交互流程，可能返回 authorizationUrl/manualApprovalRequired；user-auth check/complete (`.../adapter/lark_auth.py:110`、`:186`) 直接调用 reauth.check/complete，属于不同授权对象。不能命名为已有 `adapter.authorize` RPC。 | 授权、凭据与平台外部副作用；App tenant scopes、lark-cli用户凭据、Agent绑定是不同能力。GUI身份/授权范围、redirect/state校验、token保管均须正式网关契约，浏览器不持久化凭据。 | 需新的发起/待用户/查询/取消/过期结果、超时/重试/幂等、用户确认与安全预览定义；不能把CLI stdout、URL生成或点击标记为授权完成。 | `ROUTER:647–662`完整adapter分派无授权RPC，末端未知方法METHOD_NOT_FOUND `:689`；旧HOST已支持列表也无Lark授权。CLI依据 `authorize.py:186`、`:229`，`lark_auth.py:159`、`:207`。源码存在不等于GUI ready。 |

## 当前基线缺失/需确认的新正式契约

1. **A05公共客户端未进入所读e703源码公共面**：`shell/interface.py`、`kernel/interface.py`未发现公开 `daemon_request` 导出；当前HOST仍从 CLI `commands.common.support` 导入 `_daemon_request` (`HOST:195`)，不是用户公告A05新公共接口。不要跨层消费daemon impl，也不要dev-03自行替换此共享HOST；集成人提供A05适用公共入口、签名、版本与落入GUI集成基线的提交。
2. **HTTP网关未交付**（用户公告）；当前gui仅 `HttpStubOperations` POST `/api/gui/v1/not-wired` (`gui/src/adapters/stub.ts:9`、`:100`)。HOST是旧newline JSON socket bridge，不能当作现有浏览器HTTP API；它的旧AuthGate授权矩阵不是新HTTP身份约束。GUI state/actions/selector接线、可信principal与权限映射、params/result/error envelope、业务白名单仍需共享交付。
3. **超时和重试是旧客户端事实，不是新GUI策略**：`OLDCLIENT:325`默认IPC roundtrip 15秒（可配，范围0<秒<=300，`:273`）；只对DaemonRestoringError在restore预算内重试 (`:354`)，transport transient不在此循环自动重试。`_daemon_request_once` 每次创建 frame.id (`:371`)，它不是业务idempotencyKey/operationId/messageId；新网关必须明确各ID关联和写超时outcome unknown规则。
4. **未知发送对账缺口**：现有message.status按权威messageId查，旧AppChatAdapter无sessions.status/按原requestId查受理结果的入口；丢失send ACK时如何从原opaque request/idempotencyKey获得权威messageId尚无已验证GUI合同。不得填入推测字段或客户端按旧UUID算法造ID。旧adapter errors文档允许原key retry或lookup (`src/hyprial/shell/impl/gui/adapter/errors.py:41`)，但当前用户任务要求unknown先核对，不授权生产UI直接重发。
5. **Lark四步正式契约缺口**：chooseAgent身份来自dev-02；auth到底是App scopes、用户OAuth还是已有App选择需明确；switch是adapter.pin还是其他模型需owner确认；result需分别证明授权与渠道绑定权威状态。新网关要给params/result/errors、可信身份权限、读写、每步截止时间/未知结果、重试/幂等、预览确认及实现版本。不可把adapter在线当授权，或把pin成功当完整四步成功。
6. **验收仍UNKNOWN**：以上均静态定位，无新网关demo request三项证据（页面预期请求、可识别响应、正确状态回写）。待work-developer确认方法表并统一接线后，由隔离测试覆盖正常/失败/unknown/权限拒绝/幂等/部分受理/陈旧读取/授权过期和并发切换；正式链路未通过前GUI send/auth/switch业务请求数保持0。

## 已核对的 A05 公共消费入口

来源为固定提交 a209031c6 的 `docs/reference/daemon-request-consumer-baseline.md` 与 `contract/daemon-request/README.md`，通过内部 SSH Git 对象读取，未安装或调用该分支的产品。

- `hyprial.kernel.daemon_request(method, params=None, *, socket_path, timeout=None, restore_wait=None, clock=None, uuid4=None)`：唯一共享 IPC 客户端，只对副作用前 `DAEMON_RESTORING` 拒绝进行恢复等待。
- `daemon_request_once(...)`：一次连接/请求/响应；写失败不能自动重放。可信宿主显式选择 socket；浏览器不传可信 principal。
- `MethodSpec`、`PreviewSpec`、`IdempotencySpec`、catalogue/OpenAPI 为声明能力，不代表聊天或授权已经实现通用预览、幂等和 HTTP 服务。
- 已声明的 link 四方法不能替代本组聊天、历史、授权或渠道绑定契约。接口字段/错误变化需相应领域 owner 同步 contract，GUI 只消费正式公共入口。

所有真实 GUI 验收仍未完成；上表没有任何行被标为“已有且通过 GUI 真实验收”。当前分类是聊天等旧方法“已有但契约/暴露待补”，飞书授权和产品渠道切换“所需正式接口未确认/缺失待 owner 核实”。
