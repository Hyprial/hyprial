# GUI 启动方式收拢为唯一路径

- 日期：2026-11-14
- 目标分支：`dev`
- 调研基线：`cecde87f6e51fd023b9e89a273f268e3b91f69aa`
- 状态：方向已由 owner（Allen）2026-11-14 确认，进入实施。第 2 节是读代码得到的现状，
  未实际运行任何一条路径。2026-11-19：第 5 节的 PR 序列中 #2822 步骤 1–4 已合入 `dev`，仅 `hyprial gui` 帮助文字待 owner 决定，各行的 PR 号见第 5 节末尾的“实施状态”；第 2 节描述的 11 条路径是实施前的现状，当前的启动方式见 `docs/guides/gui-dev-mode.md`。

## 1. 目标

GUI 只有一种启动方式：同一个 host、同一套 session、同一个 backend 接线。
安装 wheel、源码 checkout、原生壳之间不再有产品行为差异。

`dev` 不是另一条启动路径，而是在正常启动之上打开两个开关。

## 2. 现状：11 条路径

```
hyprial gui ──┬─ ① 安装 wheel               Python 静态服务，校验 _gui/release.json，业务 501
(normal)      ├─ ② --gui-home/HYPRIAL_GUI_HOME 同一静态服务，指向任意构建目录，业务 501
              └─ ③ 源码 checkout            npm run build + vite preview + request-http

hyprial gui dev ┬─ ④ --gui-home/HYPRIAL_DEV_GUI_HOME 指定 worktree，Vite HMR + request-http
                ├─ ⑤ 源码 checkout          自动建 .worktrees/gui-dev-auto 稀疏 worktree
                └─ ⑥ 安装 wheel             受管 bare cache + 精确 sourceCommit worktree

CLI 之外 ──┬─ ⑦ npm run dev + HYPRIAL_GUI_DEV_PREVIEW=1   fixture 数据 + 浮动开发工具层
           ├─ ⑧ build:dist-parity + serve:dist-parity     仅测试用的 parity 产物
           ├─ ⑨ HYPRIAL_GUI_REFERENCE_CONFIG / npm run stub reference HTTP 插件
           ├─ ⑩ clients/native: npm run dev               gui/dist 拷入 src-tauri/ui，tauri dev
           └─ ⑪ make debug-gui / accept-gui               Docker 内 daemon + ③ 或 ④
```

### 2.1 三份 host

backend 本身只有一份：`src/hyprial/shell/impl/request_http/`（FastAPI + uvicorn）。
分叉发生在浏览器与它之间的 same-origin session host（launch grant、HttpOnly
cookie、CSRF、转发 `/daemon/*`）：

| 实现 | 位置 | 使用者 |
|---|---|---|
| Node（Vite 插件） | `gui/server/daemon-request/` | ③④⑤⑥ |
| Rust | `clients/native/src-tauri/src/runtime/gui_host/` | ⑩ |
| Python 静态服务（无 session，无转发） | `src/hyprial/shell/impl/gui/install/server.py` | ①② |

### 2.2 已确认的不一致

- `hyprial gui dev` 打不开开发组件。浮动工具层只在编译期 token
  `__HYPRIAL_DEV_PREVIEW__` 为真时挂载（`gui/src/main.tsx`），而 CLI 把
  `HYPRIAL_GUI_DEV_PREVIEW` 列为已移除配置并返回 `GUI_CONFIG_CONFLICT`
  （`shell/impl/gui/backend/config.py`）。开发组件只能绕开 CLI 走 ⑦，且绑定 fixture。
- wheel 与源码不是同一个产品。① 设置 driver 返回
  `GUI_INSTALLED_BACKEND_UNSUPPORTED`；③ 没有 driver 不能启动。
- `--gui-home` 在 normal 下是“构建产物目录”，在 dev 下是“Git worktree”；
  在源码 checkout 里对 normal 使用它会落到无 backend 的 ②。
- 原生壳安装包里 GUI 字节出现两次：`build-shared-ui.cjs` 拷入壳的
  `ui/shared`，以及 wheel 自带的 `hyprial/_gui`。两者可以不同 SHA。
  这一条依据打包脚本推断，未解包实测，记为 UNKNOWN。

## 3. 目标形态

```
                     ┌──────────── Python host（唯一）─────────────┐
浏览器 / webview ───▶│ /daemon/*  → session → request-http → daemon │
                     │ 其余路径   → 资源提供者                      │
                     └───────────────┬──────────────────────────────┘
                        默认：wheel 内 _gui（已构建）
                        --live-source：Vite dev server（只出资源与 HMR）
```

两个正交开关：

| 开关 | 含义 |
|---|---|
| `--with-dev-tools` | 运行时挂载 `#root` 之外的独立开发工具层，并由 host 提供开发 Swagger；两者随 wheel 发布，默认不加载、不提供 |
| `--live-source` | 页面字节来自可编辑源码的 Vite dev server，用于同态修改 GUI |

`hyprial gui dev` 等价于 `hyprial gui --with-dev-tools --live-source`。

源码定位只有一条规则：`--gui-home` 显式指定；否则源码 checkout 用自身的
`gui/`；否则安装态用清单精确 `sourceCommit` 的受管 worktree。

## 4. 原生壳：删除后按最简封装重建

owner 2026-11-14 决定：

- 原则：client 只是最简封装。client 启动与“用户安装 wheel 后用浏览器打开
  GUI”保持几乎完全一致。
- 现有原生壳（`clients/native`）与打包线（`packaging/clients`）整体删除，
  连同 mobile 桥与 Python 侧 mobile 配对适配。打包侧已停止开发。
- 接受 `dev` 上一段时间没有桌面包。
- 本轮不处理 mobile，也不做 core / extensions 划分；待唯一 host 落地、
  session 契约成文后再划分。

这取代 `docs/design/packaging-host-split-20261113.md` 中由打包宿主线持有
GUI host、launch grant 与独立 setup 页的安排，也取代本文早先版本的“方案 A”。

重建后的最小壳只做浏览器和 wheel 做不到的事：

| 保留 | 不再由壳实现 |
|---|---|
| 窗口、托盘、单实例 | session host（改由 Python host 提供） |
| 用安装包内的 wheel 与 uv 建环境，以及建环境期间的一个启动页 | GUI 字节拷贝（改用 wheel 内 `_gui`） |
| 拉起并监督 `hyprial gui --no-open --json --port 0 --stdin-stop`，webview 打开其返回的 URL | onboarding、account 页面（改走共享 GUI） |
| deep link、自动更新、签名 | 本地 Agent 管理、`app_core_call` 中继 |

### 4.1 对后续划分保留的三条约束

1. 共享 GUI 不直接依赖 Tauri；数据请求只经 `gui/src/platform/transport` 出入。
2. session 契约在 PR 2 之前成文，不假设 GUI 与 backend 同 SHA。
3. 壳不取得业务身份、凭据或 upstream bearer。

### 4.2 GUI 侧的规则

owner 2026-11-14 决定：

1. GUI 的所有 API 路径都走 daemon request。`gui/src` 中其他客户端路径
   （`/api/reference/*`、`/__gui/auth/*`、`/api/gui/v1/not-wired` 等）全部删除，
   除非有特殊理由（例如 Tauri 的强制要求）并逐条登记。
2. 页面不用静态文案说明“功能不可用 / 未接线 / 未实现”。功能不存在时不渲染
   对应控件；操作被拒绝时在操作当下通过正规 notification 说明。
   开发工具层在真实 backend 上不渲染 fixture 专用工具，也不显示原因文案。

3. mock 数据只有一个来源、一份实现。数据源是 daemon 方法声明导出的
   `contract/daemon-request/openapi.json`（`x-hyprial-examples`）。Contract Mock
   改为一个 JS 模块：浏览器内直接注入供纯前端开发使用，Node 进程形态对 host
   提供 HTTP 供走真实 session 的测试使用。Python 版 `contract_mock` 的服务与
   选用例部分随之删除，示例数据校验与敏感值扫描保留为校验工具。预设与
   “请求成功后切换预设”的迁移表是数据，不在 mock 中写业务逻辑。
4. 手写的进程内 fixture（`gui/src/dev/fixtures/**`、`*-scenarios.ts`）在 PR 5
   之后按业务旅程逐批换成 Contract Mock 预设；此前由“只减不增”的清单约束。
5. CI 门防止回归：请求路径门、测试宿主无私有数据路径门、单一 mock 来源门。

Contract Mock 只有 HTTP 形态，只被 GUI 侧使用。CLI 经本地 IPC 调用同一套
daemon request 方法，不使用 Contract Mock。CLI 尚未完全收敛到 daemon request
之后（`scripts/ci/import-ratchet-baseline.json` 与 `legacy-method-baseline.json`
记录存量，只减不增）；该收敛不在本改造范围内。

### 4.3 Tauri 社区实践对照

| 来源 | 结论 |
|---|---|
| Tauri 官方 localhost 插件文档 | 本地 HTTP 服务“带来相当大的安全风险”，拿不准就用默认 custom protocol |
| Tauri 官方 capabilities 文档 | IPC 默认只对随应用打包的代码开放；远程来源须在 capability 的 `remote.urls` 显式授权 |
| 社区 Python sidecar 模板 | 静态 UI 走 `frontendDist`，sidecar 用随机 loopback 端口、每次启动的 token 与 Host 校验 |
| `tauri-plugin-python` | 把解释器嵌入壳进程并经 Tauri IPC 调用；浏览器无法复用该通道，不采用 |

webview 加载 loopback HTTP origin 偏离官方默认推荐。被删除的 Rust host
自 F10（2026-11-10）起已是同样做法，因此 origin 类型不变。最小壳的验收条件：

1. host 只绑定 `127.0.0.1`，端口由系统分配。
2. launch grant 一次性，沿用 `hyprial gui` 的私有 handoff；壳与浏览器走同一机制。
3. host 校验 Host 与 Origin。
4. 不给该 loopback origin 授予 Tauri IPC。
5. 主窗口导航守卫只认这一个 origin。
6. Tauri 版本不低于修复 CVE-2026-42184 的 2.11.1。

来源：
[localhost 插件](https://v2.tauri.app/plugin/localhost/)、
[capabilities](https://v2.tauri.app/security/capabilities/)、
[sidecar](https://v2.tauri.app/develop/sidecar/)、
[Tauri 2.11.1 发布说明](https://v2.tauri.app/release/tauri/v2.11.1/)、
[tauri-plugin-python](https://docs.rs/crate/tauri-plugin-python/0.3.9/source/README.md)。

### 4.4 Rust actor 引擎与打包平台

owner 2026-11-14 决定：

- session host 不用 Rust 实现。`crates/` 中的 actor 引擎经 PyO3 嵌入 Python
  进程，Python 入口保留，唯一 host 仍是 Python host。
- 1c 只删除原生壳与打包线；`crates/`、`conformance/` 及引擎的构建与 CI 不在删除范围。
- 引擎使 wheel 成为平台相关（KP-027）。最小壳的安装包须携带对应平台的 wheel，
  因此 PR 6 依赖该平台能产出 wheel。
- 打包目标平台：macOS arm64、Windows x86-64、Linux x86-64。三者暂时无法全部
  成立时，只保证 macOS arm64 一个平台可打包。

## 5. PR 序列

先删后加。删除类 PR 直接合入 `dev`，其他开发者随即基于收窄后的 `dev` 开发。

| PR | 内容 | 依赖 |
|---|---|---|
| 0 | 本文档 | 无 |
| 1a | 删 CLI 侧模式：源码 checkout 的自动 worktree、normal 模式的 `--gui-home` / `HYPRIAL_GUI_HOME` | 无 |
| 1b | 删 `gui/` 侧模式：reference HTTP 插件、`npm run stub`、`gui/server/auth`、`gui/server/setup` | 无 |
| 1c | 删旧原生壳与打包线：`clients/native`、`packaging/clients`、mobile 桥、Python 侧 mobile 配对适配 | 无 |
| 1d | 删 `gui/server/live/` 及其配套；删产品代码中指向 `/api/reference/*` 的默认 API 路径（KP-049） | 1b |
| 1e | 分四步：1e-1 删除剩余的非 daemon request 客户端路径并加请求路径门；1e-2 JS 版 Contract Mock（浏览器内与 Node 两种形态）并删除 Python 版服务；1e-3 浏览器测试迁到新 mock；1e-4 删除 reference 代理并关闭 KP-049 | #2727、1d、4a |
| 1f | 删除“不可用”类静态说明文案，改为不渲染或操作时 notification | 4a |
| 2 | session 契约成文；Python host 接入 session 与 `/daemon/*`，wheel 接通真实 backend | 1a |
| 3 | 源码 `hyprial gui` 改用 Python host，删除 Node host | 2 |
| 4a | 前端：开发工具层与 fixture 组合解耦，改为可运行时加载 | 无 |
| 4b | `--with-dev-tools`；工具层随 wheel 发布；开关打开时唯一 host 提供开发 Swagger（`/docs`），Swagger UI 静态资源随 wheel 发布，工具层提供入口；`verify:production-copy` 改为“不含 mock、fixture URL 与场景数据” | 3、4a |
| 5 | `--live-source`；`hyprial gui dev` 等价于 `hyprial gui --with-dev-tools --live-source` | 4b |
| 6 | 最小原生壳（至少 macOS arm64 可打包） | 2、目标平台的 wheel（KP-027） |
| 7 | Docker 入口、文档、skill、`contract/` 收尾 | 5、6 |

```
第 1 波   1a ─┐   1b    1c    4a
第 2 波       └→ 2
第 3 波          ├→ 3 ─┐      6
第 4 波               └→ 4b → 5
第 5 波                        └────→ 7
```

1a、1b、1c 的目录互不重叠，但都会改 CI 清单与快照，须逐个合入，后合者先 rebase。

合入顺序（owner 2026-11-14）：#2727（`intg/gui-agent-journey`）先于 GUI 侧的
1b、1d、4a、1e、1f 合入，这些 PR 随后 rebase 到它之上；1a 与 1c 与其重叠很小，
可先行合入。

合并授权（owner 2026-11-14）：不引入新的 CI 缺陷即可直接合入 `dev`。
判定方法是把 PR 的失败检查集合与其 base 提交在 `dev` 上的失败集合比较；
`dev` 既有的失败不算新缺陷，但须在 PR 正文列出。

实施状态（2026-11-19）：下表按上面的 PR 行列出已合入 `dev` 的 PR。

| PR | 状态 |
|---|---|
| 0 | 已合入 #2755 |
| 1a | 已合入 #2760 |
| 1b | 已合入 #2756 |
| 1c | 已合入 #2762；其后在 `clients/native/` 与 `packaging/clients/` 重建了最小壳（见 6 行） |
| 1d | 已合入 #2801 |
| 1e | 1e-1 #2806、1e-2 #2828、1e-3 #2831、1e-4 #2868 已合入 |
| 1f | 已合入（#2802、#2809、#2814、#2820、#2825） |
| 2 | 2a #2826、2b #2851、2c #2852 已合入 |
| 3 | 已合入 #2863 |
| 4a | 已合入 #2758 |
| 4b | 4b-H #2892、4b-G #2891、4b-S2 #2905 已合入；4b-C（`--with-dev-tools`）随 #2895 合入 |
| 5 | 已合入 #2895 |
| 6 | 6a #2871、6b #2840、6c #2889 已合入（macOS arm64 包；原生壳运行 `hyprial gui --no-open --json --port 0 --stdin-stop`） |
| 7 | Docker 入口随 Z-7 A3 #2977 收尾；文档、skill 与 `contract/README.md` 的收尾是 Z-9（本节所属的文档 PR，见 7b 与 daemon 先行文的第 6 节）；`contract/README.md` 没有 `native-host/` 行，无需改动 |

daemon 先行的 PR 序列（7b）另见该文第 6 节的实施状态。

## 6. 第 1 波删除范围

owner 已确认全部删除。

| PR | 删除 | 保留 |
|---|---|---|
| 1a | `shell/impl/gui/worktree/` 中为源码 checkout 建 `.worktrees/gui-dev-auto` 的路径；normal 模式的显式构建目录路径 | 安装态精确 `sourceCommit` 的受管 worktree（`install/dev_source.py`）及其复用的 worktree 函数；dev 模式的 `--gui-home` / `HYPRIAL_DEV_GUI_HOME` |
| 1b | `gui/server/reference/`、`gui/scripts/stub-backend.mjs`、`gui/server/auth/`、`gui/server/setup/` 及其测试、skill 与引用方 | `gui/server/daemon-request/`（PR 3 删除）、parity 测试产物 |
| 1c | `clients/native/`、`packaging/clients/`、Python 侧 mobile 配对适配、相关 CI 门与文档入口 | 无 |

## 7. 开放问题

1. `--live-source` 的拓扑：Vite 在前并把 `/daemon/*` 代理给 Python host，
   或 Python host 在前并反向代理 Vite 的 HTTP 与 HMR WebSocket。
   已决定并实现（PR 3，2026-11-16，OD-3 默认）：Python host 在前、Vite 在后；
   `hyprial gui dev` 已按此拓扑运行（host `--live-upstream`，Vite 不持有 backend 变量，
   WebSocket 须带同源 Origin）。PR 5 只映射 flag。
2. core / extensions 的当前定义。`core-power-pack-architecture-2026-10-19.md`
   把 daemon 启动划给 Power Pack，与 owner 口述（core 含通信与 daemon 启动）
   不一致；划分启动前须先统一口径。
3. `gui/docs/native-host-session-contract.md` 被引用但在基线上不存在；
   session 契约已落成 `contract/gui-session/`（README 规范 + `gui-session.v1.json` 语料，PR 2a）。

## 7a. 待办：session host 并入 request_http（owner 2026-11-15）

PR 2 把浏览器 session 层做成独立的 Python 进程（`hyprial.shell.impl.gui.host`），
位置等同于被删除的 Node host：浏览器连它，它持 bearer 把 `/daemon/*` 转发给
`request_http`（或 Contract Mock）。wheel 内因此有两个 Python 进程，用户只看到一个地址。

owner 同意按此方案先行，并要求在 CLI → daemon request 收敛完成后删除多余的进程，
即把 session 层并入 `request_http`，只留一个 Python host 进程。

- 触发条件：CLI 命令已全部经 daemon request，`request_http` 不再有并行改造。
- 当时不并入的原因，也是并入前必须先解决的三件事：
  1. `request_http` 在 bootstrap home 初始化或登出时重开监听（新端口、新 bearer、
     新 generation，`bootstrap/gui.py`）。浏览器 origin 不能随之变化。
  2. Contract Mock driver 下没有 `request_http`；session 层必须能把 mock 当上游，
     或 mock 改为挂在 `request_http` 之后。
  3. `request_http` 拒绝 query string 与未知路径并逐响应关闭连接，
     与面向浏览器的静态资源服务相反；其包已到 7 文件、单文件近 1000 行的结构上限。
- 不变量：`contract/gui-session/` 的 session 契约与两端共用的 host 一致性测试不变；
  并入是内部重构，GUI 不应能分辨前后差别。
- 跟踪：WIP PR `refactor/gui-fold-session-host-into-request-http`（#2822）。
- 状态（2026-11-19）：前置问题 1（principal 变化时重开监听）已由 Z-6c（#2946）解决：principal 在同一进程、
  同一监听与端口上原地换绑。#2822 的步骤 1–3 已合入（#2985：`request_http` 的分派可以不带监听器运行，
  principal 监视器成为函数；#2995：daemon driver 下会话 host 在自己的进程内提供 `request_http`，`hyprial gui` 只起一个
  Python 子进程、一个 loopback 监听，启动记录的 `backend_pid` 为 `null`、`backend_endpoint` 为空串；Contract Mock driver
  仍另起一个 Node 子进程）。步骤 4（#2996）删除了启动器里旧的 `request_http` 子进程代码、authority 文件写入与 `server.lock`；
  契约说明随本文档 PR 更新，`hyprial gui` 的帮助文字待 owner 决定。文件式 authority 读取与 `HYPRIAL_GUI_AUTHORITY_PATH` 已于 2026-11-20 删除
  （owner 批准；host 的 authority 只来自进程内交接或启动时固定的配置）。

## 7b. daemon 生命周期：daemon 先行（owner 2026-11-15）

后端 daemon 由谁启动、`init` 与 `login` 如何解耦，另见
[daemon 先行：init 与 login 解耦](daemon-first-init-login-decoupling-2026-11-15.md)。
它与本文并行，对本文 PR 序列的影响只有三点：

- PR 2c、3 不变：session host 继续转发给 `request_http`，不直接对 daemon socket 说话。
- PR 6（原生壳）至少依赖该文的 DL-1（`hyprial gui` 在准入前对已初始化的 home 确保 daemon），
  否则经 GUI 初始化的用户之后每次启动都是 `GUI_DAEMON_UNAVAILABLE`（KP-064）。
- 上一节的 PR #2822 只有在该文的 Z-6c（principal 变化时不换端口与 bearer）合入后才解除阻塞。

## 7c. 工作计划归档（2026-11-15）

PR 2 至 6 与 daemon 先行的详细工作计划、调查记录与复现材料见
[`gui-single-launch-plans-2026-11-15/`](gui-single-launch-plans-2026-11-15/README.md)；
与本文及上一节所指文档冲突时，以这两份文档为准。

## 8. 未测范围

本文所有现状描述来自阅读基线源码，没有启动任何 GUI、daemon、浏览器或原生壳。
11 条路径的可用性与各 CI 门当前读数均为 UNKNOWN。调研时 `dev`
（`cecde87f`）自身的 `gate-python` 存在失败的 pytest shard。
