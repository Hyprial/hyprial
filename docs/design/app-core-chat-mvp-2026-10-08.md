# Hyprial App Core 最小聊天方案与环境评估

日期：2026-10-08。交付对象：黄佳佳与后续实施 worker。状态：方案及只读评估完成；独立 worker 正在进行残留核查、CLI 对照及 Core 首切片，尚未完成真实聊天验收。

本会话已明确回到官方 Hyprial 主线，采用 tailnet；libp2p 仅作对照。最小目标是 Windows 客户端与 Android App 共用聊天能力，桌面可选安装 Power Pack 管理本地 Agent。iOS 提前准备构建环境，在 Android 闭环之后验收。

## 基线与协作

- 官方源：HyprialOS/harness-bridge，精确基线 `53de6496adaa9000cbdf179cd97fcb71645aa3fa`，本轮 fetch 后的 `origin/dev`。
- 新建集成分支：`intg/gui-app-core-20261008`；工作树 `/home/chloemartin/HyprialOS/.worktrees/gui-app-core-20261008`。已推送远端；未更改原集成分支。
- 用户授权新集成分支覆盖旧 GUI 工作必须从 intg/gui 起步的本次基线限制。旧 `intg/gui@d07ee523d` 保留，不混入新产品架构。
- 任务实现从新集成分支建立独立 worktree，GUI 配套 PR 目标为新集成分支。公共 daemon/identity 先对齐现有 owner，避免重复建设。依最新交接，本次 App PR 交 gui-verifier 验收与合入；不自动安装、重启或发布。
- 最新需求依据：用户指定的本地 `~/HyprialOS-libp2p/docs/林懿伦与黄佳佳的会话 2026年10月8日.md`，尤其 15:58、16:08、16:09 的 Core、adapter、用户态 tailnet 与 Power Pack 裁定。文件位置不改变实施基线。

## 最小范围

1. 一个组织、一个目标节点、一种已配置的 harness；使用隔离测试 Agent 和模型配置。
2. Core 展示连接状态、Agent 列表、会话与文本消息；发送后显示受理、执行中、最终回复或失败。
3. Windows 与 Android 作为同一用户的两个设备，查看并继续同一会话。前台补拉，重启后历史仍在，网络重试不会再次触发相同消息。
4. Windows 可选启用 Power Pack，提供本地 daemon/Agent 状态、启动和停止。关闭整个 Agent 与取消一轮聊天明确区分。
5. Android 安装真正 APK，前台通过用户态 tailnet 访问节点。外装 VPN、宿主机专用地址和 SSH 转发可作诊断，但不计为最终嵌入网络验收。
6. 不做推送、跨 adapter 已读、飞书全量同步、离线发送队列、附件、逐字流式、完整工具卡、Workflow、Studio、商店发布和自动更新。手机回前台补拉；节点离线明确显示不可用。
7. 首版 Power Pack 可管理已准备的 runtime/harness；全自动组件市场与所有依赖的一键安装另列后续，避免把开发环境已有依赖伪装成干净机器安装成功。

## 分层设计

```text
Windows / Android / iOS 的 Tauri 薄壳
                    ↓
App Core：聊天展示、App adapter 客户端、具名 command API 客户端
                    ↓
注入的身份、网络、安全存储与生命周期公共接口
                    ↓
本机连接 或 用户态 tailnet 连接
                    ↓
节点 API 身份校验 → 节点 App adapter → daemon 消息与执行权威 → harness

桌面可选 Power Pack → 本地 runtime/harness 安装配置与进程管理
                     通过能力注册接入；Core 不反向依赖 Power Pack
```

Core 不带 Node/Python/daemon/harness 运行时；Node 可继续作为构建工具或节点侧服务。当前 Node HTTP/SSR product 不能原样放入手机；共享聊天要成为可静态打包的前端，通过受限 API 与节点通信。前端、Rust 壳及 Node 网关均不建立第二套 durable inbox 或业务执行权威。

网络实现应隐藏在窄公共面后。用户态 tailnet 返回的连接不等于系统 VPN 路由，因此不要假定 WebView 的普通 fetch 自动走该连接；由受限原生网络桥消费嵌入库连接，只允许已选择节点及明确的业务请求。不得开放任意 URL、shell、spawn 或文件系统权限。

嵌入库选型先验证官方现有 tailcat 与 tsnet 依赖能否复用，避免另建身份和组网状态。上游 [tsnet](https://tailscale.com/docs/features/tsnet) 与 [libtailscale](https://github.com/tailscale/libtailscale) 提供嵌入方向，但不证明本仓 Android/iOS binding 已可用。

## 当前能力与缺口

以下路径均对应上述精确官方基线。

| 项目 | 事实 | 处理 |
| --- | --- | --- |
| Tauri 外壳 | desktop/package.json 已是 Tauri；src-tauri 已有窗口及监督代码 | 复用并裁剪移动入口 |
| DSH | runtime/boot.rs 与 manifest.rs 已将其列为 optional harness | DSH_HOME、components/dsh 不作为 GUI 依赖残留机械删除 |
| 当前 core 发行 | runtime/manifest.rs 仍要求 Python、Node、GUI；app.rs 仍启动本地 daemon/GUI host | 与新产品 App Core 语义不同，需明确拆分 |
| GUI 真实后端 | src/gui/transport/index.mjs 只内置 mock-fixtures | 新增真实 adapter 接线 |
| GUI 可移植性 | client/composer.mjs 使用 node:crypto，session 模块使用 Node fs，product 使用 Node HTTP/SSR | 纯前端与节点服务分离，不把 server 模块打进 App |
| 发送与回复 | daemon messaging/inbox_surface/send.py 已有 conversationId、idempotencyKey 与消息路由 | 复用现有执行链 |
| 聊天历史 | messaging/inbox_surface/queries.py 的 message.query 只查 pending inbox/outbox | 增补节点侧持久聊天投影与分页补拉 |
| API 网关 | daemon IPC 已有具名路由；OrgFS HTTP 不是通用 command 网关 | 新增限定操作的认证 facade，不暴露全部 IPC |
| 身份 | 有设备 public record、nodekey/owner 基础；未找到完整 N-A2 接线证据 | 与身份 owner 对齐正式注册、校验、撤销契约；不能省鉴权换进度 |
| 普通聊天取消 | 未见通用契约，workflow.cancel 不是普通 turn cancel | 首版不展示假取消；按能力禁用或延后 |
| 多 adapter | 最新讨论明确设计尚未验收、未实现 | 只做 App 的两个设备补拉，预留统一消息字段，避免承诺飞书同步 |

## 最小接口与数据语义

拟定接口是任务合同提案，不是现存命令名：能力查询、Agent 列表、会话列表、消息发送、消息状态、按游标读历史。只映射当前真实 daemon 公共面；#1126 目标命令文档与实际 CLI 注册树逐项列出差异，完整命令 GUI 化不属于本 MVP。

节点侧统一持久化 conversation ID、message ID、parent message ID、client message ID、来源设备与事件序号。身份来自认证结果，不能信任请求体自报 sender。去重至少按已认证用户、设备、会话及 client message ID 界定；相同键不同载荷拒绝。节点持久化受理成功后才回 ACK；重新补拉只展示既有结果，不重新派发 Agent。

每设备保存独立补拉游标；服务端分页有界。首版不承诺跨 harness 崩溃边界 exactly-once。未知发送结果以原请求 ID 查询或重试，不生成新 ID 盲重发。认证最少证明有权设备成功、无权设备拒绝、撤销后拒绝；N-A2 若尚未就绪，应列为阻塞或由身份 owner 交付明确的测试合同，不私造永久认证方案。

## 实施任务与四天检查点

只读评估已完成。实施先并行开展残留清单、CLI 对照和不触及 desktop 的 Core 首切片；以下为完整闭环的责任划分。

| 责任 | 任务 | 验收 |
| --- | --- | --- |
| Worker A 后端 | App adapter、历史投影、每设备补拉、持久去重、最小认证 API | 真 Agent send/reply，重启与重复请求反例 |
| Worker B Core | 静态共享聊天、真实 transport、纯 Core 启动、Windows Power Pack 接口 | 无本地 runtime 的 Core 可进入远程聊天；装包后本地 Agent 可管理 |
| Worker C 移动网络 | Android 工程、用户态 tailnet binding、APK、模拟器连接；iOS 构建探测 | 不依赖系统 VPN 的真实远程请求及聊天 |
| 主协调 | 协议/owner 对齐、改名基线、依赖集成、环境与测试矩阵、最终证据 | 同一精确 head 的端到端验收与 PR 材料 |

修改外壳前先做单独 desktop→app 纯路径重命名变更，同步 CI、脚本、测试和文档引用；不改 app identifier、持久化目录、组件身份或 manifest 语义。涉及 payload、first-run、更新路径前必须读取本地 desktop-contracts 并按其职责更新契约。改名合入新集成分支后，各 worker 从同一头开始。

| 时间 | 必须看到的结果 |
| --- | --- |
| D1 | 环境能构建；真实 Agent 单次回复；Android 用户态网络访问已认证节点 API；消息重放不重复触发 |
| D2 | Windows Core 真实聊天、节点持久历史、Power Pack 最小管理；Android 共用同一接口 |
| D3 | Android 模拟器安装 APK、远程对话、两设备同会话与独立补拉 |
| D4 | 断线、拒绝未授权、重复发送、App/节点重启、节点离线测试；Windows 包与 Android APK；精确 head 证据 |

四天是有条件冲刺窗口，不是已确认的完工承诺。移动用户态嵌入与身份是 D1 阶段门；失败则四天交付桌面聊天和明确标注的 App 联调版，不能把外置 VPN 当最终方案。按本次新发现的缺口，Windows+Android 完整最小验收建议预留 7–10 个工作日，条件是 3 个 worker 加主协调连续协作、环境与接口 owner 可用。iOS 在共享核心稳定且 Mac/签名就绪后另估 2–4 天；这些是工程估算，不含账户开通或外部审核等待。

## 环境评估

状态只代表本次只读探测，不代表已构建产品。

| 项目 | 实测或用户提供事实 | 结论及准备 |
| --- | --- | --- |
| 本机 Linux | Ubuntu 24.04 / WSL2，16 vCPU，约 7.75 GiB 内存，探测时可用约 3.3 GiB | 后端开发可用；多 Agent 编译加模拟器需控制并发 |
| 存储 | Windows C 盘约 222.8 GiB 可用；WSL 虚拟文件系统显示约 822 GiB | 以实际宿主磁盘余量为准，不能把虚拟容量当额外物理空间 |
| Node | 用户授权后，Linux/WSL asdf 默认已升级至 24.21.0，npm 11.19.0；新 login/interactive shell 验证通过 | 满足官方 GUI >=24；Windows Node 独立核验，不以 WSL 升级代替 |
| Rust / Go / Python | WSL Rust/Cargo 1.97.1、Go 1.26.4、uv 已有 | tailcat go.mod 要求 1.27.1，版本门实测拒绝；Python 新工作树环境未同步锁定依赖，kernel import 缺 pykka |
| Java | 21 已有 | 仍需 Android SDK/NDK 和工程指定版本核对 |
| Android | PATH 与常见位置未发现 adb/emulator/sdkmanager/SDK/NDK；Rust 仅 Linux target | Android 构建及模拟器未就绪 |
| WSL 加速 | /dev/kvm 存在，当前用户无读写权限 | 不能声称模拟器可用；优先核验 Windows 原生模拟器 |
| Linux Tauri | pkg-config、WebKitGTK/GTK 开发依赖缺失 | 本机 Linux GUI 构建未就绪；Linux 可先承载后端 |
| Windows | Windows 10.0.26200，16 逻辑 CPU，约 15.9 GiB 内存，探测时可用约 2.76 GiB | 硬件可用但当前内存紧张；不能同时运行四套重型构建加模拟器 |
| Windows 构建 | MSVC 14.50.35717、WinSDK 10.0.26100.0、Rust/Cargo 1.94.0；隔离 C++/Rust 编译运行均 PASS；WebView2 目录版本 154.0.4258.62 | 完整 Tauri 构建和 WebView 启动仍待验证；Windows Node 22.18.0 仍需升至24 |
| Windows Android | SDK/NDK/模拟器缺失；Java 21.0.7；Hyper-V 已启用但 HypervisorPlatform 禁用 | 工具链与 WHPX/AVD 需准备，可能涉及计划重启；Android Rust targets 尚缺 |
| Mac Studio | SSH 只读检查通过；arm64 macOS 26.5.2，96 GiB RAM；磁盘剩余约 59 GiB；Node 26.4、Rust 1.93.1 | 只有 Command Line Tools，无完整 Xcode/simctl、iOS targets，0 个有效签名身份；尚不能构建 iOS |
| iPhone | 用户确认有设备 | 型号/iOS、TestFlight 安装和签名账号准备 UNKNOWN |
| 测试节点及组网 | 本机有 Tailscale 1.98.10；未启动生产 daemon、未操作真实账号 | 已装客户端不证明 App 用户态网络可用；需隔离节点、身份、模型与 tailnet/DERP 可达性证据 |

建议 Windows 运行 Android 模拟器与 Windows 原生构建，WSL 运行后端与测试；若 Windows 内存不足，改由远程 Mac/专用 runner 承担部分构建。设备组合足够，但目前不能说环境已就绪。Android/iOS 工具要求以 [Tauri 官方 prerequisites](https://v2.tauri.app/start/prerequisites/) 为准。

## libp2p 对照

实验库 `HyprialOS-libp2p@071ce1c` 的 UPSTREAM.json 记录导入基线为 `bccc754d`，仍含旧 Electron/DSH GUI。它已有 PeerID、目录授权、服务连接与中继验证，但不是新 Tauri App 的替代完成品。

其 2026-10-08 公网验收记录证明隔离 TCP 双中继的新流故障切换通过；QUIC 数据面仍有 32 KiB 停滞/超时，未完成验收。这些是历史固定二进制和拓扑的结果，不等于本轮重跑通过，不等于业务消息去重或真实手机网络通过。出处：实验库 docs/libp2p-public-relay-acceptance-20261008.md。

| 维度 | 本次 tailnet | 若改用 libp2p |
| --- | --- | --- |
| 官方路线 | 与 Allen 最新交接一致 | 需要维护实验分支与主线差异 |
| 移动网络 | 需用户态库绑定与 tailcat/身份兼容验证 | 同样需移动库绑定、生命周期和服务拨号 API |
| 身份 | 对齐官方设备身份/N-A2 | PeerID/目录授权还需映射业务用户与请求权限 |
| 聊天 | 需 App adapter、真实 GUI、历史与去重 | 这些工作仍然需要，换网络不能免掉 |
| 网络证据 | 本轮移动链路尚未验证 | 有 TCP 中继证据，QUIC 稳定性仍有缺口 |
| 结论 | 作为本次实施基线 | 保留 NetworkPort 替换点，不在四天冲刺维护第二套实现 |

## 交付验收

- Windows 安装版：Core-only 可以远程聊天；启用 Power Pack 后可管理隔离的本地 Agent。
- Android 模拟器：实际安装 APK，嵌入 tailnet 连接目标节点，不用开发机专用地址替代远程验收。
- 同一会话：桌面发消息、App 看到回复；App 再发消息、桌面补拉看到回复。
- 节点和 App 重启后读取已持久化历史；相同请求重试不重复触发 Agent。
- 未授权请求拒绝；节点离线、过期身份和失败发送有明确状态。
- 所有 PASS 绑定精确提交、包哈希、环境、真实/模拟范围。iPhone 真机、推送、跨 adapter 同步未测则单列，不借 Android PASS 抵扣。

初次方案轮只创建隔离本地工作树与本方案。随后用户授权 mock 预览及默认 Node 升级：已准备 GUI 锁定依赖与 dist、启动 loopback mock，并将 Linux/WSL asdf 默认设为 Node 24.21.0；产品源码未改，未重启生产服务或发布。

用户进一步授权 worker 开展环境评估。第二轮已完成：GUI 架构门 22 文件通过、测试 60/60 通过、静态资源构建通过；Windows 隔离 C++ 和 Rust MSVC 程序编译运行通过；Python 与 Go 版本/依赖阻塞如表；随后按用户提供入口完成 Mac SSH 只读检查，结果见 Mac 报告。GUI 测试通过仅覆盖现有 mock/injected port，不等于真实聊天或移动验证。

详细证据与下一步命令：

- [Windows 与 Android](../evidence/app-core-env-2026-10-08/windows-android.md)：工具版本、原生编译、加速与内存；环境准备估计半天至一天。
- [Core 与 tailnet](../evidence/app-core-env-2026-10-08/core-tailnet.md)：GUI 实测、Python/Go 阻塞、移动 binding 代码缺口。
- [Mac 与 iOS](../evidence/app-core-env-2026-10-08/macos-ios.md)：SSH 只读实测矩阵及剩余准备项。

本轮未安装系统 SDK、改网络或重启服务；Python 探测创建了未同步的开发虚拟环境，不能当作依赖准备完成。Mac 的 Xcode/签名准备，以及团队移动同步说明和 N-A2 当前合同仍需在 D1 前收敛。

## 2026-10-08 正式交接与首轮实施边界

依据 PR #1311（`docs/gui-app-handoff` 分支的 `docs/handoff/gui-app-handoff-2026-10-08.md`），顺序为残留清单、纯路径改名、实际 CLI 对照、分层实施。0.5.4 冻结分支不修改；本分支不构成提前发布。DERP 和推送中继由 infra-ops 实现与运营，App 只定义设备注册、唤醒信号、DERP 地图接口。跨端已读、多设备游标/去重与推送语义为待设计内容，不视作现存能力。

首轮 Core 实现限定为可静态构建的聊天壳和注入连接公共面，无 Node/Python 运行时依赖；默认未连接，演示需显式选择且显示标识。Power Pack 能力由组合根注入，缺失时不显示本地管理入口。端口提案不等于已实现 daemon API。真实网络、设备身份、节点持久历史与端到端验收仍须后续切片。

并行 worker 使用独立 worktree；实际 CC 执行记录保留在本地任务证据目录，不提交服务凭据。系统 SDK 安装、Mac 环境修改、生产服务重启不在本轮已执行操作内。
