# 公共线：开发要求与 prompt

负责人：**yaoshengyue**。执行承接：**codex-daemon-request**。
对应工作包：P0a（契约/身份/冷启动）、P0b（统一客户端/宿主）、共享生成与组合装配。
先读 [总架构](00-architecture.md)；合并调度归 [coordinator / emergent](01-coordinator.md)。

## 1. 交付范围与并行边界

公共线交付四线共享的唯一接入能力，不另做一套页面，也不代替各业务线实现全部业务方法。

当前优先交付 [接口 v1](common-interface-v1.md)；实施顺序与验证按用户新授权的
[M1/M2 阶段放行](integration-gates.md)。接口包不修改下述满额 Python 文件；
结构抽取在首次修改对应 Python 运行时前完成，不阻塞类型/注入接口先合入。

- 第一份小交付为接口快照和无行为变化的结构抽取：明确生成类型、注入入口、phase/error/receipt schema 与文件责任，供四线并行实现；运行时未就绪不得标成产品能力。
- P0a/P0b 可按契约、身份隔离、IPC/HTTP、宿主/前端公共客户端、Contract Mock 分小 PR；固定接口后，各 feature 分支无须等待整个 P0 完成再开发。
- 公共线维护共享 `capabilities.tsx`、AppShell/main/App、client/transport、MethodSpec 核心、registry、生成器；业务线分别提交其 adapter 导出、方法工厂与注册需求，统一装配。方法实现仍按 user-setting/chat 等所属线分工。
- 生成物不能手改；按组合后最新 head 重生成 OpenAPI/client/examples。公共线记录接口版本与调用者清单，接口变更通知 coordinator 更新依赖，不让各线暗自修补。
- 公共线代码由非作者独立审查，结论绑定精确 SHA；codex-daemon-request 汇总交 emergent 统筹合并，不自行批准/合并自己的工作。

下述技术机制必须完整实现，不能将“接口快照”当冷启动、安全边界或真实验收完成。

## 2. 首次旅程：登录/注册也进入契约，而非前置手工 CLI

### 2.1 现有冷启动矛盾

现有 `src/hyprial/shell/impl/request_http/server.py` 的 start 要先读取 daemon ping/catalog，并核对 owner/actor。配置 `config.py` 要求 canonical agent principal。现有完整 daemon 初始化又要求 owner。因此不能在空 home 上直接启动现在的 HTTP，然后宣称 GUI 登录自然可用。

当前 GUI auth 经 `gui/server/auth/worker.py` 调 `src/hyprial/shell/impl/desktop_network.py`；login 完成身份/设备阶段，并不等于 daemon 已初始化。setup worker 则直接调用 daemon IPC。两条专桥必须收敛，不能列为统一链路的例外。

还有硬边界：`src/hyprial/shell/impl/login/flow.py` 的 T11 规定 daemon 不导入 shell login、不读取 login refresh credential；`tests/shell/login/device/test_boundaries.py` 实测约束这一点。不能简单把 run_login import 进 daemon，也不能删这条安全测试过关。

### 2.2 选定方案：ownerless BootstrapRuntime → 同进程完整 daemon；HTTP 必须重启

这是共同前置开发，不是现有能力或一个环境变量 hotfix。实施后：

1. 一个 GUI 启动器固定隔离 home/socket/runtime。已有 daemon 只复用/核验，不接管其生命周期；空 home 启动同一 daemon 程序的显式 bootstrap 模式。选定“独立 BootstrapRuntime、同一进程后续构造 DaemonApplication”，不采用两种方案任选。
2. bootstrap phase 先不创建正常业务 Actor、业务 inbox 或业务数据库，不伪造 owner。它提供相同 IPC request registry；静态声明仍是同一全集，运行 catalog 按阶段标记 callable/reason，只允许声明允许 bootstrap caller 的方法。不得维护第二本手写登录方法清单。
3. 宿主与 bootstrap daemon 使用本次启动的本地短期能力进行握手（0600/0700、限本 home/socket、generation、过期、撤销），它不是 user/agent 身份。HTTP 中增加显式 bootstrap config/principal 变体；绝不能给 owner/actor 填占位字符串骗过现有校验。
4. MethodSpec 新增阶段/调用者约束：bootstrap caller 只可调用声明的认证/初始化最小集合。HTTP 与 daemon 两层都执行；不能仅靠隐藏 UI 或 FastAPI allowlist。
5. GUI 通过相同 generated client 发起登录/注册意图。daemon 方法负责 operation 的调度/公开状态，身份域的隔离凭据服务负责 OIDC device flow；daemon 本体不读取/持有 refresh token。身份服务不得反向 import daemon/shell。
6. 保持现有身份来源：服务 issuer 的 device authorization + UserInfo，不把邮箱文本、浏览器传入 owner、JWT 未验证字段当认证成功。GUI 显示 verification URI/user code，认证在提供方页面完成；操作从 GUI 开始，返回 GUI 继续。
7. 注册使用同一契约的明确 intent；若提供方没有已验证的注册能力，返回 `REGISTRATION_UNAVAILABLE`（拟新增），不实现第二个账号数据库、不假称登录等于注册完成。新错误进入对应 MethodSpec.errors、examples 和投影测试；该大写码已符合 `legacy_error_codes.is_declared_error_code` 的 uppercase grammar，不应往“历史小写例外集”追加大写码。
8. 经认证的 owner 事务落到原权威存储；设备失败必须保留“身份已成功、设备尚未就绪”，不能回滚身份或全屏假成功。保留原 CLI 账户切换/锁/确认语义；GUI 首次身份提交使用 2.3 的专门 permit，不调用 CLI 的运行中切换分支。
9. 初始化方法只在认证已成立后推进 daemon 业务初始化；同一进程退出 BootstrapRuntime 后构造完整 DaemonApplication，产生正常 daemon epoch；HTTP 按 2.4 **终止旧进程并启动新进程**，不放宽现有 `_apply_view` 的 owner/digest 漂移拒绝。未完成新握手的旧调用拒绝，不自动重放写请求。declarationDigest 来自同一固定声明全集；不同阶段可有不同 served HTTP digest，变化只能由新 HTTP 进程接受。
10. GUI 继续模型配置、secret grant、管理 agent 启动；这些仍是 daemon request。之后 org、agent、chat 才基于逐项 readiness 开放。
11. GUI/HTTP 从不连接业务数据库；数据库由 daemon 打开和维护。OIDC refresh token、设备身份和本地 HTTP bearer 是三个不同的东西。

建议冻结的新方法面（均待实施；命名/完整 schema 在 P0 契约 PR 固定）：

| 拟新增方法 | 调用阶段/身份 | 责任 |
|---|---|---|
| `onboarding.status` | bootstrap 或已认证本地会话 | 只返回公共状态、operation 引用、下一步能力，不返凭据 |
| `auth.login.begin` | 本地 bootstrap 会话；已认证同 owner 可续认证 | 接受 login/register intent、幂等 operationKey；返回公开授权指引 |
| `auth.operation.get` / `auth.operation.cancel` | 仅该启动会话/认证 owner 拥有的 operation | 查询/取消；不能枚举其他账号或其他 home |
| `onboarding.device.ensure` | 身份已完成、可信 owner 已绑定 | 调 daemon 已有 tailcat/设备服务完成设备阶段，保留部分成功；身份执行器不反向调用 daemon |
| `onboarding.initialize` | 身份已完成；幂等、受 home 锁保护 | 初始化同一 daemon 的业务阶段；返回接受/完成/unknown 及可查询引用 |

**既有 home 切换账号不纳入第一次补齐的 GUI 快捷操作**：本批 GUI 对运行中切账号明确拒绝；既有 CLI 的预览、确认、停止证明、迁移与恢复流程不变，不能把它误写成 CLI 永远只能拒绝。后续 GUI 若支持切账号，必须完整契约化该流程。已认证 home 可直接进入 ready，无须每次强制网络登录。

鉴于 T11，身份服务需从 shell 拆出可复用公共端口及隔离执行器，不能仅移动目录后让 daemon 间接读凭据。profile/owner 存储依赖也要下沉到正确公共域；保持原文件格式、事务锁和 CLI 行为。凭据执行器使用固定启动协议、只接受宿主固定的 home/profile 和受限动作，只回公开状态；daemon 通过 identity 公共 port 调用，不加载含凭据 I/O 的 worker 实现。此项必须由契约/身份 reviewer 审查后先落地，四条 GUI 线不各自实现。

还必须保留 `src/hyprial/identity/impl/identity_transaction.py` 的真实锁/描述符传递验证，以及 `src/hyprial/shell/impl/cli/commands/login/flow.py`、`commands/init/init.py` 的运行实例隔离、身份提交与恢复语义。不能以“空 home 分支已跑通”替换这些测试。bootstrap operation 的持久回执属于 daemon onboarding 域，不包含 token，也不复制身份权威；进程重启后只能据身份公共状态核验完成或返回 unknown/expired，不能重放登录/初始化写操作。

### 2.3 BootstrapRuntime 的锁、进程与状态转换（固定方案）

入口改动在 `src/hyprial/shell/impl/cli/commands/daemon/commands.py` 的 `daemon_run`：在现有 `require_initialized_hyprial_home()` **之前**识别显式 bootstrap 启动参数，委托新增 `src/hyprial/shell/impl/daemon_wiring/onboarding/{__init__.py,launch.py}`。普通 daemon_run 路径保持原顺序；不得用普通路径传假 owner。新 helper 只做生命周期装配，业务交 daemon onboarding 公共面。

| 阶段 | 实例/所有权 | 请求和状态行为 |
|---|---|---|
| B0 admission | 先验证 home 内不存在已认证 owner/不可解释身份残留；按现有顺序取 `IdentityTransactionLock` → `DaemonStateOwnershipFence`，并正面核对旧 PID/出生身份已退出。两锁全程只有一个控制者 | owner 存在则走普通启动或显式恢复，不进入“首次注册”；旧实例存活/无法证明死亡一律拒绝。不得删除旧记录来过门 |
| B1 awaiting-auth | BootstrapRuntime 持两锁；不构造 DaemonApplication/IdentityResolver/业务存储，不写普通 `.active_daemon`/daemon.json。写独立 0600 bootstrap 运行记录（PID、出生身份、epoch、socket、phase；无秘密） | 绑定同一选定 IPC socket；使用共用 IPC framing/MethodSpec registry，运行阶段 admission 拒绝全部业务方法；受限 catalog/ping 的 backend 用 discriminated bootstrap 结构，owner 明确为 null，正常 backend 结构仍要求非空 owner |
| B2 authenticating | identity worker 是固定可执行模块的自有子进程，仅继承身份事务锁的 dup descriptor；bootstrap 留原 descriptor，worker 用 acquire_or_adopt 验 inode/真实占锁，不再次 acquire，也不显式 LOCK_UN | 首次登录使用“已验证 bootstrap commit permit”，不是调用 `_run_login_cli_flow`。permit 仅由本实例在两锁下、old identity 空且记录/出生身份一致时签发到私有管道。worker 在锁内再次比对原身份快照，UserInfo 验证后才提交。不会触发旧业务 daemon 的冲突/停止分支 |
| B3 authenticated | worker 结束并关闭 descriptor，bootstrap 独立读取已提交身份公共字段核对 owner/issuer；仍持两锁；设备动作由 daemon `ensure_device_key` 完成 | login credential 先提交而 owner 后提交的原有部分失败语义保持；设备失败可单独重试。身份部分提交不可被 cleanup 回滚为“从未登录” |
| B4 promoting | `onboarding.initialize` 先记 durable accepted receipt，再停止接受新写；等待已接收 worker/IPC handler 有界 drain，撤销 bootstrap capability。主线程退出 bootstrap accept loop、关闭其 socket listener（不释放两锁） | 响应丢失只能 unknown；不能重复 initialize。取消只在不可逆 promotion 前接受，越界返回已接受/不可取消。退出 bootstrap 不代表完成初始化 |
| B5 constructing | 同一主线程构造完整 DaemonApplication，owner 从已验证 settings 解析；将原 fence.detach()/transaction.detach() 交现有 `runtime.run(...)`，不重新抢同一文件锁 | 才开始 owner migration、业务数据库、正常 heartbeat/daemon.json 和正常 epoch。constructor/run 失败按既有 abort/清理退出，身份提交保留，receipt 标失败/unknown；不退回“未认证”或自动重放 |
| B6 serving/ready | 按现有 `application/startup.py` 在可接 ping/shutdown 的 serving 边界释放 identity transaction；state fence 到 daemon 正常退出才释放 | 恢复期按每方法 readiness 拒绝；HTTP 新握手后读取 initialization receipt/status。serving 不等于全部 connector ready，不能提前宣称聊天可用 |

补充生命周期约束：bootstrap 等待有明确空闲/授权 deadline；OIDC interval/expires_in 按提供方控制，不能为保活无限延长。SIGTERM/SIGINT 先取消/回收自有 worker、标记非终态 receipt 为 interrupted/unknown、撤销 capability、关闭自己的 listener/记录，再关闭锁。超过 drain 上限不做 promotion，报告失败并退出；只清匹配本 PID/出生身份/epoch 的记录。宿主恢复页面可查询状态或让用户明确重试，不能后台重放写请求。

GUI 启动与显式 bootstrap 入口均在 B0 前拒绝非空 `HYPRIAL_OWNER`（包括恰与 settings 相同的值），报告明确的启动配置错误；不静默删变量后继续。该限制只针对本产品旅程，普通 CLI 的受管 override 能力不变。B3/B5 再检查传入完整 daemon 的环境无 override、settings owner/issuer 与已验证结果一致。保留原 login D7 `OWNER_OVERRIDE_CONFLICT` 测试，补空 home、相同/不同 override、直接绕 GUI 启动 bootstrap 的负例；不允许 `resolve_node_owner` 的环境优先级换掉刚认证的 owner。

进程崩溃后：锁由 OS 释放，但记录与 operation receipt 保留。下一次启动先证明旧 generation 已死亡，再由同一 home 的新 trusted 启动控制者检查身份公共状态与 receipt；已提交 owner 进入普通启动，未提交则明确 auth expired/unknown。receipt 内部核验与向浏览器开放查询是两件事；浏览器恢复授权按 2.4.1，operationId/operationKey 本身不是凭证。

既有 home 的同 owner 重认证：由正常 daemon 的 onboarding operation 调隔离身份 worker；它取得 identity transaction 并比对“现有 owner/issuer 与认证结果相同”，不停止 daemon、不改变 epoch、不迁移库。不同 owner/issuer 返回现有身份冲突语义，GUI 不进入 switch-account。现有 CLI switch-account 继续使用其完整 orchestration。首次 bootstrap permit 绝不能用于已有业务 daemon，也不能用字符串 `phase=bootstrap` 冒充。

### 2.4 HTTP 交接：关闭旧入口、重启、重新建立传输会话

不改 `_apply_view` 对 owner/version/digest 变化的拒绝规则。共同宿主收到自有 bootstrap 控制通道的 promotion 状态后，执行以下固定序列（这只是生命周期控制，不代跑业务方法）：

1. 将 GUI 连接 phase 置为 transitioning，拒绝新的业务提交；已经发出的写仍保留 unknown/receipt。
2. 有界 drain 并停止旧 request-http，自有 PID 退出确认后撤销旧 bearer、清理其 generation 对应 server.json。旧服务因先看到变化而返回 CATALOG_STALE/MISMATCH 属预期拒绝，不能捕获后用旧 token 继续。
3. 等正常 daemon ping/catalog 的 owner、node、version、epoch 一致，取经核验的 node owner/nodeId，使用公共 `canonical_agent_uri(owner, nodeId, 'squire')` 构造 `principal.actor`（复用旧 `shell/impl/gui/live/runtime.py::_personal_squire_uri` 的派生规则，不复用其业务 bridge）。owner 来自已验证 settings 与 daemon 的一致读数，不来自浏览器；原有 `HYPRIAL_GUI_PRINCIPAL` 若显式提供必须等于相同 owner 的 canonical user URI。配置 actor 的 owner/node 必须与 catalog、ping/ps 一致，启动全新 request-http；不原地改 bootstrap 配置。这个 actor 是本地 operator 的受限调用上下文，不证明 squire 已创建/运行；需要实际 agent 的方法仍检查存在性/readiness，U1 再创建它，不能伪造 actor 数据库记录。
4. 从新 0600 server.json 读取新 capability，仅在宿主内使用；浏览器收到新连接 generation，旧业务 cookie/session 与 CSRF 全失效；控制 cookie/view proof 按 2.4.1 保留，用于 HTTP 重启续接。经同源传输握手得到新业务会话，重建公共 client/catalog pin。真实 upstream token 永不返回浏览器。
5. 新会话可恢复查询本 home/同 owner/同启动控制会话的 onboarding receipt（并校验权限），不重新提交 login/initialize；其他会话/账号拿到 operationId 仍拒绝。

测试必须在每个间隙注入旧 token、旧 CSRF、旧 generation、并发查询/写、HTTP 启动失败、正常 daemon 构造失败。旧凭据全拒绝、无重复提交、无遗留 listener 才可 PASS。Node 与 wheel 宿主执行同协议。

### 2.4.1 回执归属与浏览器续接：控制证明不等于业务凭据

固定区分三个身份：`hostControlSessionId`（一次可信 GUI 控制器启动）、`viewSessionId`（被授权的浏览器视图）、`transportGeneration`（可撤销的 HTTP/业务传输代次）。ID 都是公开选择器，不能单凭 ID 授权。下面是拟新增协议，当前 server.json/cookie 不具备这些能力。

- 宿主初次通过一次性本机 launch grant 建立视图：精确 Origin/Host、一次性随机值交换，发 HttpOnly/SameSite=Strict 控制 cookie，并把独立 view proof 只交该视图保存于 sessionStorage。launch grant 不进入查询参数/访问日志，可用 URL fragment 后立即清除；只用 cookie 不能区分同源其他 tab。浏览器的 launch grant/control cookie/view proof 仅用于宿主控制握手，不发给 FastAPI/daemon，不是 OIDC token；它们不同于下一条 supervisor 的 control proof。业务 cookie/CSRF 与它们名称、用途、路由分离。
- daemon 仅从 2.6 私有控制通道注册的 HTTP capability 得到 `{homeBinding, hostControlSessionId, viewSessionId, ownerOrNull}`。operation receipt 持久记录此归属、controller PID/出生身份、控制恢复 proof 的摘要及生成时 epoch；认证提交后经身份公共状态核验，owner 只允许从 null 绑定到该验证 owner 一次。supervisor control proof 由 CSPRNG 生成，raw proof 常驻 supervisor 内存，仅经 2.6 私有控制通道交 daemon 临时核对；daemon 仅持久化摘要，raw 不落盘、不进 server.json/浏览器/日志，核对完释放临时引用。daemon 重启后必须摘要匹配且原 controller PID/出生身份仍存活，才允许复用 hostControlSessionId，否则只能新会话。仅报相同 ID 不能续接。`onboarding.status` 只能返回此上下文可见的 operation；`auth.operation.get/cancel` 同时核上下文、归属、阶段与权限，不从业务 params 采信 caller/view/owner。即使同一 owner 的另一启动会话知道 operationId，也不自动获得旧授权指引/取消权。
- HTTP 重启而宿主仍存活时，宿主校验原控制 cookie + view proof，原子撤销旧业务 cookie/CSRF/upstream capability，再由私有控制通道登记相同 host/view、当前 daemon epoch 的新能力。新视图的业务 session 只能关联这个新能力。daemon 允许新 epoch 查询原 receipt 的条件是上述同归属且 owner 未变；旧 epoch capability 一律拒绝，不能以“恢复”为由延长旧权限。
- 每个浏览器视图的 upstream bearer 在宿主/HTTP 内映射到独立的 daemon capability，不能把 viewId 作为未经验证的 header 透传。新的 0600 server.json 写明 controller PID/出生身份、HTTP PID/出生身份、hostControlSessionId、transportGeneration、backend epoch 与协议版本；view grant/capability 通过私有控制管道交付，不公开在浏览器运行描述中。读文件不能代替核验存活进程与握手。
- **本轮不支持宿主死亡后的浏览器授权自动续接**：控制证明只在该存活宿主内有效，退出/崩溃即失效。新宿主按 B0/正常启动规则核验身份及 daemon 内部 receipt，显示已核实的初始化状态或 unknown/expired，但不给新视图返回旧登录授权指引/取消权限，不自动重放 login/initialize。daemon/HTTP 子进程重启且宿主未死亡，可用原存活控制会话恢复；若 daemon 自身重启，必须用新私有通道与旧 receipt 的同归属记录重新登记，不能恢复已死宿主。显式跨宿主恢复能力留后续契约，不是假定已实现。
- 负例：其他 tab 没有本视图 proof、同 cookie 不同 view、其他 owner/home、已死宿主、旧业务 cookie/CSRF/epoch、伪造参数、HTTP PID 复用全部拒绝；正例：同视图刷新、HTTP 重启后只查询已接受回执、不重复写。浏览器复制同源 sessionStorage/全套凭证不构成可辨识的“另一人”，不声称 tab 隔离能抵抗同源脚本或凭证复制；需显式新视图授权时先撤销旧 view grant。

### 2.5 身份服务的依赖拆解与 T11 新边界

不整体搬运 shell/login；下面逐项决定原依赖去向。身份执行器只运行首次身份/同 owner 续认证，不运行 owner migration preview，也不读业务 SQLite。

| 原符号与来源 | 固定归属/改法 |
|---|---|
| flow/session 的 read_settings_identity、write_settings_identity、write_settings_owner，当前在 `daemon/impl/configuration/identity.py` | 将身份记录的纯读写/校验实现抽到 **新增** `identity/impl/onboarding/identity_record.py`，原 daemon 公共函数改薄委托，文件格式/部分提交语义不变。身份 worker 与 daemon 读公共字段共用一个存储实现；此模块不读 OIDC credential |
| NetworkProfile、SECRETS_DIRNAME，当前在 `daemon/impl/configuration/network_profile.py` | 不将整个 daemon profile 服务下沉；身份 port 定义只读 AuthProfile DTO（issuer/clientId 等必需字段），shell/daemon 可信装配从现有 profile 转换。credential 路径常量归身份 credential store；身份 worker 不 import NetworkProfile |
| oidc.py 的 url_opener（daemon 网络代理选择） | 定义身份侧 HTTP I/O port；shell/daemon 装配只给可信代理/CA 配置数据，worker 内创建其 stdlib opener；不能传 daemon 对象/任意可执行代码。原 CLI 的代理/issuer origin/TLS 语义必须回归测试 |
| binding.py 的 NetworkProfile 与 session.py 的身份读取 | 分别消费同 AuthProfile DTO 与身份 store；binding assertion 处理留执行器，只回认证后的公开结果 |
| flow.py 中 live_daemon_pid 与 before_commit 回调 | 业务实例存活/切换判断留 daemon/shell；worker 只接受私有管道送入的窄 commit permit + 已占锁 descriptor，并核 snapshot。没有 permit 的旧 CLI 走原 orchestration，不允许 worker 自己判“daemon 不存在” |
| preview.py 的 build_plan/MIGRATION_DATABASES/MIGRATION_TEXT_FILES/MigrationPlan、SQLite backup | 全部留原 shell preview + daemon owner_migration 公共权威；禁止移入 identity 或复制算法。未来 GUI 切账号另声明 daemon preview/execute 契约；本批 GUI 明确不可用 |
| ensure_device_key/locate_tailcat_sidecar | 留在 `daemon/impl/network/tailcat/{keys.py,binary.py}`；onboarding.device.ensure 的 daemon handler 调它。identity 只保留现有 DeviceRecord/store 类型；不反向 import daemon |

拟新增凭据实现放 `identity/impl/onboarding/credentials/{__init__.py,flow.py,oidc.py,binding.py,session.py,credential_store.py}`；credential_store 只在凭据执行器加载，与上面的公开 identity_record 职责分开，不复制存储权威。父目录 ports/service 是**无凭据的子进程代理**，worker 是独立入口。daemon 只能 import ports/proxy，worker/credentials 只在子解释器加载。固定 stdio/socketpair 帧 allowlist：begin、poll-public、cancel、close；只包含验证 URI/user code、公开 owner/issuer、状态/receipt；不得输出 access/refresh/device code、binding bearer 或 credential 文件内容。用户可见 user code 与私密 device code 必须区分。

T11 新测试不能只改扫描路径：扩展 `tests/shell/login/device/test_boundaries.py`，对 daemon→identity proxy 的传递 import 图设拒绝；在 daemon 进程拦截 credential 文件读取并放置 canary，证明真实运行时也不读取；断言 worker 的秘密不进入协议/异常/stdout/stderr/receipt，且公共 proxy 不加载 worker/credentials 模块。更新 `tests/integration/native_harnesses/test_native_resident_tripwires.py` 的 NATIVE_ORIGINS：逐个登记新增 daemon onboarding/catalog/protocol 文件为对应原生 owner，禁止通配豁免/以 relocation 隐藏 resident 调用。身份 worker 的允许 credential I/O 是被测试的独立边界，不等于放宽 daemon T11。

### 2.6 新版 IPC、明确进程树与握手（不塞业务 params）

固定源码 `daemon/impl/ipc/server.py:579-601` 会忽略 v1 帧中的未知顶层字段；包 VERSION 又相同，所以“给 v1 添 transportAuth”不足以 fail closed。选定 **GUI 授权通道使用 wire version=2**，普通既有 IPC 继续 version=1。P0 同步改 `kernel/impl/ipc/client.py::daemon_request_once`、IPC framing/admission/router 与 wire 契约；不是只在 HTTP 加 bool。

**协议判定固定如下：**

- 新版控制握手必须返回 `protocol='hyprial-gui-ipc'`、`protocolVersion=2`、`ipcFrameVersion=2`、`authKinds`、`phase`、`daemonPid`、`daemonBirth`、`epoch`、`homeBinding`、`socketBinding`、`controllerChallenge`、静态 declarationDigest。控制通道与启动实例按下述时序绑定；相同包版本或相同方法列表不能代替这些字段。
- GUI HTTP 只接受字段完整、类型正确、challenge/子进程身份/绑定全部吻合、kind 支持当前阶段的握手；缺失/旧版本/未知版本/绑定不符均在监听业务 HTTP 前启动拒绝。禁止尝试 v1 写请求探测或失败后降级 v1。
- v2 帧强制 `transportAuth={kind, capabilityId, secret, epoch, requestNonce}`；kind 为 bootstrap 阶段的 `local-bootstrap` 或 ready 阶段的 `local-gui`。它不是 MethodSpec.params，不进日志/data/receipt。v2 响应必须标 version=2；第一条请求只能是共用 registry 的只读 catalog/状态核验，校验其 phase/epoch/digest 后才允许业务请求。v1+transportAuth 在新服务明确拒绝；不能声称旧服务也会拒绝这种混合帧。
- **旧 daemon 负例必须直接使用 764cf168 的解析器**：v2 请求在任何 handler 调用前得到 VERSION_MISMATCH；握手缺字段时 HTTP 不监听、不发送业务请求。另测 B0 发现存活旧 daemon 时不 spawn bootstrap、无身份写入。B0 是第一道门，显式握手和 v2 是第二道门，不以“版本字符串相同”绕过任一道。
- daemon 用 CSPRNG 签发能力，仅交已登记 HTTP 子进程；保存 constant-time 可验证摘要、home/socket/epoch、controller/view 归属、PID/出生身份、过期/撤销与 nonce 状态。IPC 核 OS peer UID/PID/出生身份，再核能力和阶段，生成不能被 params 覆盖的 CallerContext。平台无法核 peer 时 BLOCKED；不降级仅凭 bearer。
- ready runtime 拒绝全部 local-bootstrap 能力，即使 secret 正确；local-gui 同时通过现有正常 principal/方法授权及新增控制会话绑定，不替代它们。旧 v1 正常 CLI 语义不扩大；不能借 v1 调用受 GUI receipt 控制会话限制的操作。nonce 去重不替代 operationKey 幂等，写超时仍 unknown。

**固定进程归属与启动顺序：**

```text
Python GUI supervisor（shell/impl/gui/backend/lifecycle.py；唯一生命周期 owner）
  ├─ daemon bootstrap → 同 PID 完整 daemon（仅启动自有实例时）
  │    └─ identity credential worker（由 daemon 的 identity proxy spawn）
  ├─ request-http child（仅 supervisor spawn，启动时等待私有配置）
  └─ 页面/同源 proxy（源码为 Node；wheel 为 Python，无业务执行器）
```

源码 npm start 的薄 wrapper 进入同一 Python supervisor，等待/转发退出；不自行另起 daemon/HTTP。Node 仅页面/proxy，接 supervisor 的私有控制管道；wheel 用同协议。复用既有 ready daemon 时它不成为自有子进程，退出 GUI 不停它。

1. supervisor 创建 hostControlSessionId、内存 control proof、challenge 和 socketpair（close-on-exec，精确 pass_fds）；spawn 自有 daemon 时仅给它预期端，记录 OS 返回 PID/出生身份。daemon 取 B0 两锁后回上述 hello；supervisor 同时验证文件/socket 所有者、实际 peer、出生身份和 challenge。首次注册存 proof 摘要，恢复旧 session 按 2.4.1 核摘要与仍存活的原 controller；不可仅用新启动器自报旧 ID。CLI stdout/随机 server.json 不可代替此控制通道。
2. 复用新版既有 ready daemon 时，通过其 **新增本地控制 socket** 建立控制会话：固定在已核验 home 的 0700 runtime，拒绝符号链接/所有者不符，双向核 peer UID/PID/出生身份与活实例记录，challenge-response 绑定本连接；只接受同 OS 用户的可信本机启动器（沿用本地 operator 信任边界，不声称抵抗已控制同一 OS 账号的代码）。未升级 daemon 没有该协议则明确不可用。控制帧只允许 hello/register-http/revoke-http/close，不提供登录/设置/chat 业务；所有业务仍走 MethodSpec。
3. supervisor spawn HTTP child，先由其私有 socketpair 回 challenge 与 PID/出生身份；HTTP 尚不监听。supervisor 将实际观测的 HTTP PID/出生身份、child challenge、host/view 注册信息送 daemon 控制通道。daemon 独立核 OS 进程身份与 controller 记录，签发一次性注册回执和 capability，经 supervisor 私有管道交该 HTTP；不是“HTTP 是 daemon 的 OS 子进程”。daemon 同时核后续 IPC 的真实 peer 正是该已登记 HTTP，兄弟进程持相同 secret 也拒绝。
4. HTTP 完成 v2 只读 catalog 核验后，才绑定 loopback、生成自己的 upstream bearer 并写 0600 server.json；supervisor 从子进程控制回执核 pid/birth/epoch/endpoint，装配页面 proxy。新增 view grant 经同控制链分配独立 bearer→daemon capability 映射，不共享一份可伪造 viewId 的业务凭据。注册超时/子进程早退立即撤销，不留下可消费能力。
5. promotion/HTTP 重启先 revoke 旧注册、确认旧 PID/birth 退出，再重复 3–4，保留存活 host/view ID、更新 epoch/generation/全部能力。同进程 promotion 保留独立的 controller 通道/内存身份，仅撤销业务能力；控制状态通过显式移交进入 full daemon，不随 bootstrap listener 销毁。daemon 重启必须重建控制连接、核旧 daemon 死亡、重登记并验证 control proof；不得信任旧 pipe/file。宿主退出关闭控制连接，daemon 撤销该 controller 所有 grant；只停自有 children。借用 daemon 的活进程不被停止。

实现落点：daemon **新增** `impl/onboarding/control.py` 管控制会话/进程登记（不是身份业务入口），`admission.py` 管能力；共用 IPC `protocol/{framing.py,caller.py}` 验 v2。host **新增** `shell/impl/gui/backend/control.py` 管上述 socketpair/注册，`lifecycle.py` 是唯一 spawn owner。正常 daemon 通过公共装配挂同控制服务，不能只给 bootstrap 接上。新增 request_http/bootstrap/handshake.py 消费注册并严格核协议；名字不表示 ready 阶段另建第二实现。

负例必须涵盖错误 PID/出生身份、PID 重用、兄弟进程、伪造 controllerChallenge、重复注册、非法 control 帧、socket/link 替换、跨 home/socket、过期/撤销、nonce 重放、伪造 params actor/view、直接 socket 调用及 promotion 后复用；另验证未授权旧 v1 receipt 查询拒绝，正常 CLI 不回归。

静态声明保持纯函数，不读 home/socket/凭据；阶段/audience 和控制会话归属约束同步 MethodSpec 校验、catalog/OpenAPI 投影与测试。新增协议文档固定为 `contract/wire/daemon-bootstrap-auth.md` 与 `contract/wire/daemon-bootstrap-auth.schema.json`，覆盖 v2/control/server.json/浏览器传输握手；更新 `contract/daemon-request/README.md`、OpenAPI、HTTP envelope 必要阶段/错误及跨实现 golden。控制握手不是后端业务旁路，不能往这里添加 login/init 的业务动作。

### 2.7 Contract Mock 首次旅程：后端 fixture 状态机，不是前端 mock

当前 `contract/daemon-request/contract_mock/service.py` 主要按 method/scenario 取单次样例，不能据此宣称完整 onboarding journey 已支持。新增 `contract/daemon-request/contract_mock/journeys/{__init__.py,onboarding.py,cases.json}`（后端测试服务）并扩展 service 的显式 journey 选择；模式仍只有一个 driver，journey 名是测试场景，不是第二套数据源开关。

- `cases.json` 的每次输出引用对应 MethodSpec examples 的 case 名；只允许声明过的 operationId/operationKey/generation 绑定替换，替换后再次做 schema 和 sensitive 校验。不为缺失 example 生成任意 payload。
- 状态按独立 mock 运行会话隔离：unauthenticated → pending → authenticated → device-ready → initialized。begin 按 operationKey 幂等；同 key 不新建 operation；get 在配置的有限轮次后选择已声明的成功/错误样例，重复查询返回同 terminal 结果；cancel 不回滚已提交身份。
- 明确场景：success、registration-unavailable、authorization-denied、expired、credential-committed-owner-failed、device-failed、initialize-response-lost、wrong-order。名称/输出映射随 P0 的 examples 冻结；未定义组合一律 MOCK_SCENARIO_MISSING，不能悄悄退回单方法 success。
- 身份前业务写、device 先于 auth、initialize 先于必要前置都必须返回契约规定的拒绝。initialize-response-lost 先记录接受再模拟丢响应，后续查询同 operation 恢复；不得再次初始化。
- Mock auth 不联系 IdP，公开指引使用 synthetic 值并显式标示模拟；状态转移由 mock scenario 驱动。其浏览器通过只证明流程与契约，真实授权必须单独实测。
- 暂存状态只在独立 mock 进程/测试目录，重启默认重置；测试选择恢复 scenario 时显式加载 synthetic snapshot，不能碰真实 home。四线共用此 upstream，不能各加页面内状态机代替。

## 3. 公共接线的精确代码位置与所有权

下表路径相对仓库根。新增文件为建议的固定任务边界；作者只能在对应公共面协商后调整，不得私建第五套调用层。

| 层/owner | 已有路径 | 应改/新增路径与职责 |
|---|---|---|
| GUI 公共装配（公共线） | `gui/src/app/capabilities.tsx`、`gui/src/main.tsx`、`gui/src/App.tsx`、`gui/src/vite-env.d.ts` | **新增** `gui/src/app/backend/index.ts`、`connection.ts`、`context.tsx`：唯一运行时连接、模式、generation、client factory；capabilities 只统一装配四线 adapter。main/App 不再自动转 fixture |
| 前端通用客户端（公共线） | `gui/src/client/{index.ts,transport.ts,types.ts,errors.ts}`；`gui/src/client/generated/{index.ts,types.ts}` | 保留 HttpTransport 一次尝试与错误分类；支持同源会话授权、catalog/phase 变化；业务只调用生成方法。generated 文件必须通过生成器修改 |
| 同源宿主（公共线） | `gui/server/reference/index.ts`、`gui/vite.config.ts`、`gui/scripts/serve.mjs` | **新增** `gui/server/daemon-request/{index.ts,proxy.ts,session.ts,config.ts}`，抽取 reference 的安全转发能力用于产品；统一 `/daemon/request/*`、catalog/readiness 透传，不含 feature operation switch |
| FastAPI/IPC（daemon-request owner） | `src/hyprial/shell/impl/request_http/{__main__.py,app.py,server.py,config.py,catalog.py,errors.py}` | 加阶段握手和有限 bootstrap principal；目录已有结构预算，新增实现放 **新子包** `request_http/bootstrap/{__init__.py,principal.py,handshake.py}`，不要在原目录塞第八个文件 |
| 方法声明（daemon-request owner） | `src/hyprial/kernel/impl/contracts/daemon/method_spec.py`、`src/hyprial/daemon/impl/request_catalog/registry.py` | 扩展阶段/身份声明及投影校验；**新增子包** `request_catalog/onboarding/{__init__.py,specs.py,examples.py}`。所有合法 bootstrap 方法仍来自同一 registry |
| bootstrap 执行（daemon-request/身份 owner） | `src/hyprial/daemon/impl/application/__init__.py`、`src/hyprial/daemon/impl/ipc/{router.py,server.py}`、`src/hyprial/daemon/interface.py` | **新增** `src/hyprial/daemon/impl/onboarding/{__init__.py,runtime.py,operations.py,admission.py,control.py}`：BootstrapRuntime、receipt、可信能力、控制登记与状态交接；提取共用 framing/admission 到 **新子包** `src/hyprial/daemon/impl/ipc/protocol/{__init__.py,framing.py,caller.py}`，不另建登录 HTTP 业务服务器。满额文件先按 3.1 拆分 |
| 身份凭据服务（身份/daemon-request 共同前置） | `src/hyprial/shell/impl/login/{flow.py,oidc.py,session.py,binding.py,preview.py}`、CLI login/init、`src/hyprial/daemon/impl/configuration/identity.py` | **新增** `src/hyprial/identity/impl/onboarding/{__init__.py,ports.py,service.py,worker.py,identity_record.py}`；凭据实现路径见 2.5 的 credentials/credential_store.py，禁止混用公开身份记录与秘密存储。公共出口通过 `identity/interface.py` 与 `identity/__init__.py` 的仓库机制更新。凭据只在身份执行器内；CLI 成为同公共服务的薄调用者 |
| 统一产品启动（公共线） | `src/hyprial/shell/impl/gui/{launcher.py,runtime.py,termination.py,apps.py,project.py}`；`gui/scripts/serve.mjs` | **新增** `src/hyprial/shell/impl/gui/backend/{__init__.py,lifecycle.py,config.py,control.py}`：唯一 Python supervisor、子进程所有权、受限控制协议、ready/退出；launcher 近行数上限，不能继续堆业务 |
| wheel 入口（同上） | `src/hyprial/shell/impl/gui/install/{server.py,launch.py,resources.py,report.py}` | 安装态同样接上述公共宿主协议与 FastAPI，上游由 Python 管理；保留无需 Node/npm/Git。不能只修源码 npm start |
| 契约与生成物（公共线） | `contract/daemon-request/openapi.json`、`scripts/verify/dump_daemon_openapi.py`、`gui/scripts/generate-reference-client.mjs`、`gui/src/features/agent-provider/api/examples.generated.json` | 从 Spec 重生成，examples 随方法变更；冲突由公共线按 coordinator 固定的组合 head 重新生成，不人工挑一份旧 JSON |
| Contract Mock（公共线） | `contract/daemon-request/contract_mock/{server.py,service.py,contract.py,envelope.py,sensitive.py}` | 使用同一公开请求契约/错误；bootstrap/认证场景使用 synthetic 身份/回执，不访问真实凭据；必要状态依赖由显式 scenario 驱动，不能让每个页面自行 mock |

Node 源码宿主与 Python wheel 宿主可有两种转发实现，但只实现同一个无业务语义的宿主协议，用同一组协议验收用例测试。浏览器业务路径、请求 envelope、FastAPI 路由和 daemon 方法只有一套。

前端公共面的落地约定：`gui/src/app/backend/connection.ts` 唯一创建 `HttpTransport` 与现有 `createGeneratedClient`（导出类型为 `GeneratedClient`）；`context.tsx` 只注入该实例和连接状态；feature adapter 接受所需方法的 `Pick<GeneratedClient, ...>`，不再自行 new transport 或创建另一份 client。`capabilities.tsx` 只装配已有 UI capability，不做网络请求。`gui/src/client/types.ts` 的 token 必填配置需增加区分明确的同源会话模式，保留直接 HTTP 使用者的 bearer 模式，不能将真实 bearer 返回浏览器，也不能用空 token 绕过校验。宿主负责把同源会话换成其持有的 upstream capability。

安全实现必须包括：loopback/明确 Origin/Host、会话绑定、CSRF、无浏览器自报可信身份、真实 bearer 仅宿主持有、catalog digest/请求 ID/dispatch 透传、跨 generation 拒绝。现有 reference 路由主要只转发 POST，不能直接复制后遗漏 catalog/ready 与必要 header。本地会话建立是传输握手，不是旁路登录业务 API。

### 3.1 已测结构预算：必须先做无行为变化的抽取

以下为固定 source SHA 的物理行/直接受控文件数，不是估计：

| 位置 | 当前预算 | 固定处理 |
|---|---|---|
| `daemon/interface.py`、`daemon/impl/application/__init__.py`（均在 src/hyprial 下） | 各 1000 行，零余量 | 不追加一行再靠压行补救。P0 先按已有公共导出机制抽取 export 映射到新子包；Application 的构造分段抽入 **新子包** `application/initialization/{__init__.py,identity.py,resources.py}`，保持正常路径语义，明确腾出新增 public port 所需余量 |
| `shell/impl/gui/launcher.py`、`shell/impl/request_http/app.py` | 997、979 行 | launcher 将生命周期委托新 gui/backend；app 将纯 admission/route 构造抽入 request_http 新子包；不得在原文件堆实现 |
| `daemon/impl/ipc/router.py`、`kernel/impl/contracts/daemon/method_spec.py` | 893、890 行 | 新协议 admission 与阶段验证各进独立子包，原入口只薄委托；先检查原目录文件预算 |
| `daemon/impl/request_catalog/message` | 6 个直接文件 | send 放 **新增子包** `message/send/{__init__.py,specs.py,examples.py}`；targets/progress 所需声明用 **新增子包** `message/directory/{__init__.py,specs.py,examples.py}`，不在 message 根加 send_specs/query_specs 两文件造成 8 个 |
| `shell/impl/request_http`、`shell/impl/daemon_wiring`、`daemon/impl/application` | 各 7 个直接文件 | 只在新子包内加实现；原目录不能再加直接代码/文档文件 |

P0a 第一份交付必须带抽取前后 public exports/行为等价、完整目录计数与行数报告、结构门/模块边界门结果。新增导出若需要改生成脚本/manifest，必须改其事实源，不能手工压缩生成文件避门。各 feature PR 也独立检查，最终组合再次检查。

## 4. 开发 prompt

> 你承接公共线，负责人 yaoshengyue，执行者 codex-daemon-request。读取总架构与本文件，在 coordinator 固定的 intg/gui-live-0554 基线上建立短分支，所有 PR base 指向该容器。先以小 PR 固定接口与结构抽取，提供 GeneratedClient 注入、错误/phase/receipt schema 和注册约定，让 agent/org/user-setting/chat 同时开发，不等待全部公共运行时完成。按本文件完整实现 ownerless BootstrapRuntime→同进程正常 daemon、身份事务/锁交接、T11 凭据执行器隔离、GUI IPC v2/旧 parser 拒绝、HTTP 重启与控制/业务会话隔离、唯一 Python supervisor、同源传输和唯一 driver。维护共享文件与生成器，接收各线 adapter/方法注册，不另写 chat/settings 业务 bridge。真实与 Contract Mock 共用同一页面/client/path；Mock journey 只能由后端契约样例驱动。保留正常 CLI、旧业务错误和 unknown/幂等语义。逐项提供结构预算、红→绿焦点测试、协议负例、真实浏览器/wheel 证据；不能用单测或模拟登录替代真实认证。只使用隔离 home/socket/端口。代码提交精确 SHA 后交非作者独立审查，汇总结论与 CI 给 emergent 合入；不自审自合、不发布、不接管其他线实现。

## 5. 放行清单

接口快照可先交付；完整 P0 的放行必须覆盖本文件全部状态/失败/恢复/权限负例，以及总架构 G1–G8 中对应公共能力。结构抽取先通过 exports/行为等价、7 文件/1000 行和模块边界检查。bootstrap principal 不等于 owner；业务 daemon、IPC registry 和 MethodSpec 权威只有一套。任何未实测的真实服务/认证平台明确记录 BLOCKED 或 UNKNOWN。
