# hc 终端后端：交互式 harness 成员改由 hc 承载（设计，待负责人批准）

状态：设计稿，未批准。批准之前不写任何产品代码（裁决 R26）。本文不改变现行契约。

- 基线：GitHub `dev` 提交 `22eb400376`（2026-11-27）。文中所有 `文件:行号` 取自该提交。
  模型连接那串 PR 合入后启动路径会变（裁决 R21），凡涉及启动路径的行号届时需重新核对，文中以「启动路径」标出。
- 决策来源：负责人 2026-11-26、2026-11-27 两天共 26 问的访谈。裁决已定，本文不重新讨论，只把它们落成结构、流程和表格。
- 事实来源：2026-11-26、11-27 在一台 macOS arm64 机器和一台 Linux x86_64 机器上做的三组实验
  （私有 core、固定副本、版本切换；hc 0.1.21 与 0.1.22），hc 0.1.19 的接口描述（只读，未实验），以及对本仓库的只读调查。Windows 没有实测。
  文中说「实验事实」的只指这三组实验测到的；来自接口描述或文档的另行注明。
- 阅读约定：
  - 「裁决 Rn」是负责人的决定，编号沿用访谈记录。
  - 【提议 Pn】是裁决没有规定、由本文补出的细节，批准时可逐条改。
  - 【冲突 Cn】是裁决与现有代码或已知事实对不上的地方，本文不替负责人取舍，汇总在第 12 节。
  - 【未核实】是没有实验或代码证据支撑的假设，汇总在第 12 节。

## 1. 目标、非目标与名词

### 1.1 目标

1. 交互式 harness 成员的终端由 hc（herdr-core，Herdr, Inc. 发布的终端运行时）承载，取代已删除的 tmux 模式和现在的 CLI 前台子进程。
2. 终端由 daemon 创建和持有，CLI 只发请求并 attach。daemon 重启、升级不打断终端里的程序。
3. 交互与无头共用一套启动编排、启动规格和恢复规则，差别只留在「终端本身迫使不同」的地方（裁决 R24）。
4. 旧的交互会话子系统按三个去向拆掉：留给 cell、并入 harness 运行时、删除。

### 1.2 非目标

- GUI 内的终端视图（后续单独立线，裁决 R3）。
- hc 自带的委派能力：模板、子代理、多机器（裁决 R1）。
- 把密钥送进交互终端的任何办法。那是 provider / Magpie 线的事（裁决 R10、R11）。
- 阻塞通知的路由与送达（cell / conversation / adapter 线，裁决 R16）。
- 向非入口交互成员投递（cell 模型线，裁决 R18）。
- Windows 可用性保证，以及 Codex 写死的 `unix://`（裁决 R20）。
- 迁移代码。只写给 agent 的迁移说明和 CHANGELOG（裁决 R17）。

### 1.3 名词

| 名词 | 含义 |
| --- | --- |
| cell | 一个 agent 就是一个 cell：OrgFS 上的一份文档，列出成员、权限和参数。cell 自己没有终端，也没有模式。 |
| 成员（member） | cell 文档里的一个 harness 成员。身份是 `(agent_id, member)`。旧文档里「某 agent 的交互会话」一律读作「交互式 harness 成员」。 |
| 模式 | 成员是交互（interactive）还是无头（headless）。本机期望状态里启动行的一个字段，不进 OrgFS（裁决 R14）。 |
| 成员终端 | hc 里承载一个交互成员的那个终端。一个终端只承载一个成员。Codex 例外地多占一个无人 attach 的终端跑 app-server（裁决 R9）。 |
| 私有 core | hyprial 自己的 hc core：独立的 `HC_SESSION`、socket 和状态目录，只从 hyprial 固定的那份 hc 副本启动，与用户日常用的 core 互不可见（裁决 R2、R8）。 |
| 固定副本 | hyprial 按仓库里的锁文件下载、校验后放在 `<home>/state/` 下的 hc 可执行文件（macOS 是整个签名 `.app`）。 |
| holder | hc 里实际持有终端程序的进程。core 重启时 holder 和程序不动，新 core 接管它们。holder 一直跑它启动时的那个版本。 |
| carrier | harness 一侧替它与 daemon 通信的部件：Claude 的 stdio MCP 通道子进程、Pi 的 attach 扩展、Codex 的 app-server 加 carrier 线程。它负责拉收件箱、上报 turn 边界。 |
| connector | 词汇表已有：把一个 harness 会话接到 daemon 的进程管理加注入层。本文每个 `(harness, 模式)` 一个。 |

## 2. 负责人裁决（按主题）

日期：访谈在 2026-11-26 与 2026-11-27 进行，下表逐条标注。
「理由」一栏是按访谈记录和实验事实归纳的一句话，批准时请一并核对。

### 2.1 范围与归属

| 裁决 | 内容 | 日期 | 理由 |
| --- | --- | --- | --- |
| R1 | hc 完全取代 tmux 承载交互终端，不保留双后端；不用 hc 的委派能力。 | 2026-11-26 | 产品代码里 tmux 已删，只剩存储和线上的残留字段；两套后端等于两套恢复规则；hyprial 已有自己的派活能力。 |
| R2 | 终端放在 hyprial 自己的私有 core，不进用户日常的 core。 | 2026-11-26 | 实验证明私有 core 与用户 core 的进程、socket、状态、可见性完全隔离。 |
| R3 | 所有交互启动都由 hc 承载；删除 CLI 前台 `subprocess.Popen` 路径；迁移 `contract/e2e/` 下 4 个 tmux 驱动；删除 `tmuxSession` 残留。GUI 终端视图另立线。 | 2026-11-26 | 只留一条启动路径，旧字段今天已无读者也无写者。 |
| R4 | daemon 创建终端并拥有私有 core；CLI 只请求和 attach。 | 2026-11-26 | 仓库的既定规则：业务逻辑只在 daemon。 |
| R5 | 无头在同一条线收敛：一个 daemon 启动请求覆盖两种模式；CLI 里的预检、创建、启动、回滚编排搬进 daemon。每个方法的迁移要负责人逐项同意。 | 2026-11-26 | 两种模式的调用路径统一后代码更整洁；启动路径只改一次；业务逻辑只在 daemon。 |
| R6 | 编排、启动规格、分发表共用；每个 `(harness, 模式)` 一个 connector。无头仍是 daemon 的管道子进程，只有交互进 hc。 | 2026-11-26 | 无头行为不变；差别收在 connector 里。 |
| R26 | 流程：设计先经负责人批准；之后按切片在 worktree 实现，自合并进集成分支 `intg/hc-terminal-backend`（本地测试通过、不新增 CI 红）；最后一个 PR 进 `dev`，由负责人批准。 | 2026-11-27 | 设计先于代码；中间态不进 `dev`。 |

### 2.2 hc 副本与私有 core

| 裁决 | 内容 | 日期 | 理由 |
| --- | --- | --- | --- |
| R8 | hyprial 自己获取 hc 副本，用仓库里的锁文件固定版本（每个平台：url、sha256、size）。私有 core 只从这份副本运行，绝不复用用户安装的 hc。 | 2026-11-26 | 用户执行 `hc update` 会换掉他那份的版本；固定副本让行为可复现。 |
| R19 | daemon 在第一次交互使用时把固定副本下载到 `<home>/state/` 下按版本分的目录，对照锁文件校验 sha256 和 size。wheel、源码、app 三种安装共用这一个机制。需要新写下载器（重试、代理、`User-Agent`）。 | 2026-11-27 | wheel 的门禁不允许 `hyprial/_bin/` 下多出文件；运行时下载对三种安装一视同仁。 |
| R12 | daemon 经内核的本机 IPC 入口（W020）在 hc socket 上讲 JSON-RPC。固定副本的二进制只为三件事被执行：启动 core、给人用的 `attach`、把运行中的 core 挪到固定版本。 | 2026-11-26（换版本一项 2026-11-27 补） | 任何 hc 子命令都会顺手启动 core，执行越少越好；裸 RPC 重启选不了新 core 的版本。 |
| R9 | daemon 有序停止（stop、升级重启）时终端和私有 core 继续运行，下一个 daemon 接管；最后一个终端移除后由 daemon 停掉 core。后果：Codex 的 app-server 也作为第二个无人 attach 的终端住进 hc；每个交互 harness 都要有重连路径，Codex 的 carrier 线程要从 CLI 进程搬进 daemon。 | 2026-11-26 | 升级不该打断用户正在用的 TUI。 |
| R22 | 固定版本与运行中的私有 core 版本不同时：daemon 启动时下载固定副本并执行它的 `server restart`（程序不停，已 attach 的查看端掉线一次）。下载不到时，协议代数一致就继续用旧 core，并在状态里显示。旧版本目录在没有该版本的 holder 之后删除。 | 2026-11-27 | 实验：新旧版本互相驱动都可行；重启后谁来启动下一个 core，版本就跟谁。 |

### 2.3 启动、恢复与退出

| 裁决 | 内容 | 日期 | 理由 |
| --- | --- | --- | --- |
| R7 | 两种模式一条恢复规则。终端还活着就接管；行的最后结果是 `running` 而终端没了，就带会话引用在 hc 里重新拉起。接受的代价：重拉的 TUI 可能停在某个提示上，没人 attach。 | 2026-11-26 | 与无头「只重跑上次结果为 running 的行」是同一条规则。 |
| R23 | 终端里的程序自己结束时：退出码 0 等于用户主动退出，按 stop 处理（删行，不重拉）；非 0 或被信号杀死算失败，在现有退避和失败预算下带会话引用重拉；终端丢失则重拉。 | 2026-11-27 | 用户退出不该被当成崩溃拉回来。各 TUI 实际返回什么仍在实验中，见 12.1。 |
| R24 | 需要重启的配置变化（成员摘要漂移、策略变化）与无头同规则：等这一 turn 结束，再带会话引用重启。总则：交互与无头成员遵守同样的规则，除非终端本身迫使不同。 | 2026-11-27 | 少一套规则就少一类只在一种模式下出现的缺陷。 |
| R14 | 成员的模式放在本机期望状态的启动行里。规则：凡是关于终端的都是本机事实，不进 OrgFS。模式必须进入成员配置摘要，否则切换模式不会被看成漂移。 | 2026-11-26 | 终端只与本机有关，不属于跨机共享的 cell 结构数据；同一个 cell 在不同机器上可用不同模式运行。 |
| R21 | 顺序：模型连接那串 PR 先进 `dev`，本线在它之后从 `dev` 起步，启动路径只改一次。若本设计批准时它还没合入，先做不碰启动路径的部分（hc JSON-RPC connector、下载器与锁、私有 core 的启动 / 停止 / 接管）。 | 2026-11-27 | 两条线都改启动路径，叠着改会互相返工。 |

### 2.4 用户入口

| 裁决 | 内容 | 日期 | 理由 |
| --- | --- | --- | --- |
| R13 | 命令 `hyprial agent tui attach <agent> [--member <m>]`。在真实终端里对交互成员执行 `agent start` 会立即 attach。对已在运行的成员执行 `start` 报错并指向 tui 命令。脱离用 hc 的 `ctrl+\`，不另设 detach 命令。daemon 更换成员的终端时，attach 命令跟到新终端。attach 只能在成员所在的机器上做。 | 2026-11-26（「跟随」一条 2026-11-27） | 入口只有一个；hc 已有脱离键。 |

### 2.5 密钥、策略与阻塞

| 裁决 | 内容 | 日期 | 理由 |
| --- | --- | --- | --- |
| R10 | 交互终端里绝不出现密钥。密钥由 Magpie 持有和投影，属另一条线。后果：交互启动的明文 `secretEnvironment` 路径是删除而不是搬迁；daemon 从不含密钥的环境启动 core；无头保留模型连接线的内存注入。 | 2026-11-26 | 实验：传给终端的环境变量被 hc 明文落盘，core 的启动环境也会落盘。 |
| R11 | 第一版交互启动只支持原生 Auth。Key 类连接（含 Magpie）在 provider / Magpie 线放开之前，交互启动一律拒绝并说明原因。 | 2026-11-26 | 没有密钥通道之前，原生登录是唯一不需要往终端里放密钥的方式。 |
| R15 | cell 文档的工具策略同样作用于交互成员，以启动参数传给 TUI。接受的代价：没有确认握手；TUI 里「允许」的意思是不再询问，不是上限；策略变化会重启 TUI。 | 2026-11-26 | 策略属于成员，不属于模式。 |
| R16 | 被阻塞的交互成员（hc 报 `blocked`：permission / question / auth）成为 daemon 里的成员运行事实，并作为 cell 的一个输入；cell 的连接数据把通知路由到用户原先的通信地址。本线提供状态事实和 cell 的接缝；路由与送达归 cell / conversation / adapter 线。 | 2026-11-26 | harness 上方是 Agent Cell，用户的通信地址对齐在 cell 上，阻塞应由 cell 感知并沿既有通信地址送达。 |

### 2.6 旧子系统与收件箱

| 裁决 | 内容 | 日期 | 理由 |
| --- | --- | --- | --- |
| R17 | 旧交互会话子系统三向拆分。agent 层的部分（准入、origin 托管、路由）留给 cell，键从 actor 改为成员。harness 层的部分（启动行、会话引用、终端地址、turn 上报、carrier 版本检查）改名改键并入现有 harness 运行时；`session.turn.*` 成为成员级接口。只为证明「daemon 没启动的进程归谁」而存在的部分（pid 与出生栅栏、租约心跳、`owner_fence`、`channel_confirmed`、外部 `session.register*` / `unregister`）连同 `interactive_sessions` 表、`SessionActor` 和 `tmuxSession` 一起删除。冻结的 `session.*` 方法逐项交负责人。不写迁移代码。 | 2026-11-27 | 终端由 daemon 启动之后，「证明归属」这一整类机制没有读者了。 |
| R18 | 交互成员继续经 carrier 从 daemon 收件箱拉取工作。cell 准入闸门行为不变，只把键改成成员。 | 2026-11-27 | 投递模型不在本线范围内改。 |

### 2.7 平台与完成标准

| 裁决 | 内容 | 日期 | 理由 |
| --- | --- | --- | --- |
| R20 | Windows：不保证可用，验收里没有 Windows 探针。新代码按跨平台写（不新增 Windows 可移植性基线条目；锁文件带 Windows 目标；处理 Windows 的包布局），现有 GitHub Windows runner 检查保持通过。不在 Windows 上预先拒绝。 | 2026-11-27 | 当前无 Windows 实机可做交互终端验证，先保证代码跨平台与既有 CI 通过，实机问题留待后续测试修复。 |
| R25 | 完成标准见第 10 节。 | 2026-11-27 | — |

## 3. 架构

### 3.1 组件与分层

既定分层（`docs/design/cli-daemon-request-convergence-2026-11-14/architecture.md` §3）：CLI 只做参数、TTY、显式 IPC 调用和渲染；业务逻辑在 daemon；kernel 只留 IPC 传输、协议词汇和纯数据类型。

```
CLI     hyprial agent start / agent tui attach
          │ daemon 请求（本机 IPC）             │ exec 固定副本 attach（仅此一处执行二进制）
          ▼                                    ▼
daemon  启动编排 ──► 分发表 (harness, 模式) ──► 交互 connector ──┐
        （四条启动路径的汇合点）          └──► 无头 connector（不变，管道子进程）
                                                              │ terminal.run / kill / rm
        私有 core 主管 ◄── 固定副本获取（锁、下载、校验、解包）    ▼
          │ 启动 / 接管 / 换版本 / 停止        hc 客户端（JSON-RPC，事件订阅）
          ▼                                    │ kernel connect_local_ipc（W020 入口）
hc      私有 core ── holder ── 成员终端（TUI）、Codex app-server 终端
```

【提议 P1】新增一个 daemon 包 `src/hyprial/daemon/impl/terminals/`，放下面前三个组件；connector 放在各 harness 自己的包里。
结构门限是每个模块目录最多 7 个直接文件、每个代码文件最多 1000 行，实现时按此拆子包。

| 组件 | 职责 | 层 | 是否碰启动路径 |
| --- | --- | --- | --- |
| hc 客户端 | 在 hc socket 上讲按行分隔的 JSON-RPC 2.0：`hello`、`server.status`、`server.stop`、`terminal.run / list / get / kill / rm / label`、`events.subscribe`。不实现帧模式，不做 `terminal.attach`。 | daemon；传输经 kernel 的 `connect_local_ipc`（`src/hyprial/kernel/impl/manysys/ipc/__init__.py:121`） | 否 |
| 私有 core 主管 | 拥有私有 core 的全部生命周期：构造不含密钥的环境、启动、探活、接管、版本比对与迁移、最后一个终端移除后停止；维护事件订阅，重启后从快照重建。 | daemon | 否 |
| 固定副本获取 | 读锁文件、下载、校验 sha256 与 size、解包到按版本分的目录、删除旧版本目录（macOS 同时注销 Launch Services 记录）。 | daemon | 否 |
| 交互 connector（claude / pi / codex 各一个） | 把启动规格变成 `terminal.run` 的参数和 carrier 的接线；实现接管、停止、退出分类所需的句柄。 | daemon，`harnesses/<harness>/` | 是 |
| attach 命令 | 向 daemon 要 attach 描述，执行固定副本的 `attach`，返回后再问 daemon 是否要跟到新终端。 | CLI | 否 |

kernel 不新增业务代码。kernel 现有的本机 IPC 入口在 POSIX 上是普通的 AF_UNIX 连接，能直接连 hc 的 socket；在 Windows 上的情况见【冲突 C2】。

### 3.2 私有 core 主管

- **身份。**【提议 P2】`HC_SESSION` 取 `hyprial-` 加 hyprial home 路径 SHA-256 的前 12 位十六进制，同一台机器上的两个 home 得到两个 core；值永远不是 `default`。socket、状态、配置、数据目录都放在 `<home>/state/hc/` 下（布局见 3.3）。
- **环境。** 实验事实：任何 hc 子命令在该 session 没有 core 时都会启动一个（包括 `--version`、`server status`），所以每一次执行二进制都必须带齐私有变量。终端继承 core 的启动环境，且没有按终端取消某个变量的办法。
  因此 core 只从一份白名单环境启动：私有变量（`HC_SESSION`、`HC_SOCKET`、指向 `<home>/state/hc/` 的 `XDG_STATE_HOME` / `XDG_CONFIG_HOME` / `XDG_DATA_HOME`）、`HOME`、`PATH`、语言区域变量，以及 Linux 上真实的 `XDG_RUNTIME_DIR`。其余一律不传，尤其是任何模型密钥和 hyprial 自己的凭据变量（裁决 R10）。
  Linux 上保留真实 `XDG_RUNTIME_DIR` 的原因：换掉它，core 会成为调用者 cgroup 里的直接子进程，daemon 的服务单元一停它就跟着死，裁决 R9 落空。
- **探活。** 判断 core 是否活着只连 socket，不执行二进制。
- **路径长度。** AF_UNIX 路径有平台上限，holder 的 socket 与 `HC_SOCKET` 同目录且名字更长。主管在启动前用 `local_ipc_path_limit()`（`kernel/impl/manysys/ipc/__init__.py:128`）核对，超限就以明确原因拒绝交互启动。
- **配置。** 私有 core 的 `config.toml` 由主管写出，关掉 hc 自己的便签等与 hyprial 无关的功能。
- **停止。** 最后一个成员终端移除后，主管经 RPC `server.stop` 停 core。daemon 有序停止时不停 core，只断开连接。
- **事件。** 主管持有一条 `events.subscribe` 连接。hc 每 15 秒发一次心跳，读超时按心跳的倍数设定，属于有界等待。core 重启时这条流只是结束，没有事件也没有错误行，序号从头开始；主管重连、取新快照、以快照为准重建，不依赖序号连续。

### 3.3 固定副本的获取与锁

- **锁文件。**【提议 P3】放在 `src/hyprial/daemon/impl/terminals/hc-lock.json`，随 wheel 作为包数据分发（运行时要读，不能只留在 `packaging/`）。形状沿用现有的 `packaging/clients/runtime-lock.json`：

  ```
  { "version": "X.Y.Z", "generation": 1,
    "targets": { "<目标>": { "url": "...", "sha256": "<64 位十六进制>", "size": <字节数> } } }
  ```

  目标覆盖 hc 发布的五个：macOS arm64 / x86_64、Linux x86_64 / aarch64、Windows x86_64。升级 hc 版本就是改这个文件的一次提交。
- **目录。**【提议 P4】

  ```
  <home>/state/hc/
    bin/<version>/      固定副本：macOS 是完整的签名 .app；Linux 是单个静态二进制；Windows 是 herdr-core.exe 加 conpty/ 下的文件
    core/               私有 core 的 socket、状态、配置、数据（XDG_* 指向这里）
  ```

  路径辅助函数按 `kernel/impl/primitives/managed_harness.py:28` 的先例放在 kernel（纯路径计算），目录用 home 既有的私有目录加固。
- **下载器。** 仓库里今天没有运行时二进制下载器。新写一个，复用现有的两个出站辅助：代理路由（`daemon/impl/network/proxy_route.py:61`）和 `User-Agent`（`kernel/impl/primitives/outbound.py:29`）。要求：有界重试（实验里同一会话内下载速度相差近十倍，一次 TLS 握手超时）、先校验 size 再校验 sha256、校验通过才解包、解包拒绝路径穿越、先解到临时目录再原子改名。
- **解包。** macOS 归档里有两个不同的 `herdr-core`：裸二进制和 `.app` 里的那个，哈希不同。只取 `.app`，整体保留，不改动任何字节，否则签名身份就变了。现有打包用的解包器会拒绝符号链接，不能直接拿来解 `.app`。
- **何时下载。** 第一次交互使用时（裁决 R19），以及 daemon 启动发现版本不一致时（裁决 R22）。
  【提议 P5】下载在主管自己的工作线程里做，启动请求只等待「副本就绪」这个事实，不在请求处理函数的调用链上直接发起网络访问。
  原因：`scripts/ci/check_network_reachability.py` 会把从 IPC 根到网络边界的新路径判为失败。实现时要实际跑一次这个门；若仍被判为新路径，就是第 7 节的一条基线条目，需负责人批准。
- **删除。** 删除某版本目录时，macOS 上先用 `lsregister -u` 注销那份 `.app`（实验：目录删掉后记录还在）。

### 3.4 分发表与 connector

今天没有分发表。四条启动路径最终都经 harness 运行时的 I/O port 调到 `HarnessLauncher.start`，全仓库只有一个调用点（`harnesses/actor/io_port.py:148`，启动路径）；`_start_process` 是一串按机制名判断的分支，非无头一律抛错「interactive sessions are started by the CLI」（`harnesses/runtime/launcher.py:295-392`，启动路径）。

【提议 P6】本线把这串分支换成一张以 `(harness, 模式)` 为键的表，值是 connector 工厂。无头的五项直接包住现有工厂，行为不变；交互的三项是新 connector。这张表就是裁决 R6 说的分发表，由 11 节的切片 S4 建立。

四条启动路径各自在哪里到达 connector：

| 路径 | 今天的入口 | 到达 connector 的位置 |
| --- | --- | --- |
| 启动请求 | `agent.start`（`request_catalog/agent/start.py:369`）与旧的 `lifecycle.start`（`ipc/router.py:113`） | 生命周期操作 → harness 运行时 → `io_port.py:148` → 分发表 |
| daemon 恢复 | `_restore_harnesses`（`application/restore.py:630`），只重跑结果为 `running` 的行（`application/restore.py:686`） | 同上。交互行先问主管「终端还在吗」，在就接管，不在才走到 connector 的启动 |
| 崩溃重启 | harness 运行时的 reconcile 行为（`harnesses/actor/behaviors/reconcile.py:255` 的退避，`process_events.py:140` 的失败预算） | 同上 |
| cell 成员 reconcile | 计划器（`cells/reconcile/planner.py:316-366`）产出 START / DRAIN_AND_RESTART 等动作，由 `composition/cells/` 下的 effect 执行 | 同上 |

四条路径汇合在同一点，所以交互 connector 只需要接在分发表上，不需要在四处各接一次。

交互 connector 的共同约定：

- 用 `terminal.run` 启动，`keep: false`、无父终端、不发便签。`keep: false` 是实验结论：`keep: true` 的终端在 core 停止后会变成一个登录 shell，原命令不会重跑；重拉是 daemon 的事。
- 传给终端的环境变量只有非密钥项（home、成员身份、会话引用文件的路径、终端类型）。
- 返回给 harness 运行时的句柄代表「一个 hc 终端」而不是一个子进程：停止等于 `terminal.kill` 加 `terminal.rm`；「是否在运行」来自主管的事件流。
- 每个 connector 提供重连：接管一个活着的终端时，carrier 已经在跑，connector 只恢复 daemon 一侧的状态。

三个 harness 的差别：

| harness | 终端里跑什么 | carrier 在哪 | 本线要补的 |
| --- | --- | --- | --- |
| claude | `claude` TUI，带 MCP 配置、hooks 和策略参数 | TUI 拉起的 stdio MCP 通道子进程（已有重连） | 启动参数的组装从 CLI（`shell/impl/cli/commands/agent/runtime/claude.py:102`）搬进 connector；去掉 `--owner-pid` / `--owner-identity`（`claude.py:271-273`） |
| pi | `pi` TUI，加载 attach 扩展 | TUI 进程内的扩展（已有重连） | 启动参数的组装从 CLI（`runtime/sessions.py:67`）搬进 connector |
| codex | 两个终端：一个无人 attach 的 `codex app-server`，一个 `codex --remote` TUI | 今天是 CLI 进程里的线程（`runtime/sessions.py:752`） | carrier 线程搬进 daemon；app-server 的 socket 今天在临时目录里，【提议 P7】改放 `<home>/state/` 下按成员固定的路径，daemon 重启后才找得到；两个终端用同一成员身份、不同角色标签区分，TUI 结束时 app-server 一并停止 |

Codex 今天声明「daemon 重启后不恢复交互 app-server / TUI」（`harnesses/capabilities.py:196-200`），本线完成后这条降级声明改为原生支持。hc 的文档说 Codex 在 hc 里要带 `--no-daemon` 运行，否则命令会跑到 hc 之外，connector 需照此传参【未核实】。

### 3.5 daemon 请求面

【提议 P8】方法名如下，逐项的去留见第 7 节：

- `agent.start`：扩成覆盖两种模式的唯一启动请求。新增参数表达模式、成员、恢复用的会话引用、工作目录等今天只有 `lifecycle.start` 才接受的内容。预检、创建 agent、启动、失败回滚全部在 daemon 内完成。
- `agent.tui.attach`：返回 attach 描述（见 4.2）。只面向本机操作者，不经 HTTP 暴露。
- `agent.member.turn.started` / `ended` / `refresh`：`session.turn.*` 的成员级后继。
- `agent.member.carrier.hello`：carrier 在自己启动或发现 daemon 换代之后调用一次，报告自己的构建版本、协议版本和 turn 覆盖范围；daemon 回答它是否已过期。它取代 `session.refresh` 里仍然有用的那部分，不再承担「声明归属」。

## 4. 生命周期流程

### 4.1 启动

1. CLI 发 `agent.start`（模式、成员、可选的恢复用会话引用）。CLI 不再做预检、创建、回滚；今天这些在 `shell/impl/cli/commands/agent/runtime/start.py:78-553`（启动路径）。
2. daemon 拒绝检查，全部带明确原因：
   - 该成员已在运行：报错并指向 `hyprial agent tui attach`（裁决 R13）。
   - 该成员的旧终端还在收尾：拒绝；旧程序结束之后同样的请求必须被接受（见 5.2 与 10.2 的成对用例）。
   - 交互模式且模型连接是 Key 类：拒绝，说明第一版只支持原生 Auth（裁决 R11）。
   - 路径长度、固定副本不可得且没有可用的旧 core：拒绝。
3. daemon 确保 agent 存在（沿用今天的「先有 agent 再有 connector」），失败时由 daemon 自己回滚。
4. 交互模式：主管确保固定副本就绪、私有 core 在运行（第一次使用时会下载）。
5. 经分发表到 connector。交互 connector 调 `terminal.run`，带成员身份的名字和标签（5.2）。
6. 就绪。【提议 P9】交互成员的「启动成功」定义为终端处于 live 且程序仍在运行；carrier 报到（`agent.member.carrier.hello`）是之后单独的一个事实，显示在状态里。
   原因：TUI 第一次启动可能停在目录信任之类的提示上，要人 attach 之后回答，carrier 才会起来。若等 carrier 报到才算成功，「启动后立即 attach」（裁决 R13）就会互相等待。
   今天 CLI 是前台子进程，一边让用户看到提示一边轮询 `ps`（`runtime/interactive.py:469`、`runtime/sessions.py:316-353`），这段轮询随之删除。
7. 启动成功后写启动行（沿用现有规则：从未成功过的启动不留行）。
8. CLI 若在真实终端里，立即走 4.2 的 attach；否则打印 tui 命令后返回。

无头模式走同一个请求、同一段编排，connector 仍是管道子进程，行为不变。

### 4.2 attach 与 detach

实验事实：`attach` 必须有 TTY；`ctrl+\` 脱离后客户端以 0 退出；core 重启时客户端打印「core 丢失」后也以 0 退出；任何 hc 调用在没有 core 时都会启动一个。

【提议 P10】流程：

1. CLI 调 `agent.tui.attach`，带 agent、成员，以及上一次 attach 过的终端（首次为空）。
2. daemon 回答三种之一：
   - `attach`：给出固定副本的可执行路径、私有变量、终端地址。
   - `pending`：成员正在被重启，新终端还没起来；CLI 在有界时间内再问。
   - `ended`：成员已停止（用户退出、被 stop、失败预算用尽），附原因；CLI 退出。
3. CLI 在 exec 之前做两件事，都是为了不让这次调用意外启动一个带着用户 shell 环境的 core：
   - 经 kernel 入口连一次 socket，连不上就不执行，回到第 1 步。
   - 用与主管相同的白名单环境执行，只额外带上终端类型变量。即使竞态下 core 恰好在这一刻消失，被顺手启动的 core 也不含密钥。
4. `attach` 返回后，CLI 不看退出码，一律回到第 1 步并带上刚才的终端。实测脱离和 core 重启两种情况退出码都是 0，分不出来；终端里的程序结束时 `attach` 返回什么没有测（U15）。
   daemon 若回答「还是同一个终端且仍在运行」，CLI 视为用户脱离，退出。若回答新的终端，CLI 接着 attach，这就是裁决 R13 的「跟随」。
   代价：core 换版本时终端没变，CLI 会把那一次掉线当成脱离而退出，与裁决 R22「查看端掉线一次」一致。

脱离只有 `ctrl+\`，不设命令。attach 只在成员所在机器上可用：`agent.tui.attach` 只面向本机操作者。
已有的 `hyprial agent attach` / `agent detach` 是把身份接入 cell 的另一件事，名字相近，文档和帮助里要写清区别。

### 4.3 daemon 停止与启动（接管）

停止：有序停止不动成员终端和私有 core，只断开主管的连接。
今天的停止流程对所有 harness 调 `stop()`（`application/shutdown.py:801-811`），启动时每个进程的 pid 和出生标记还会登记进孤儿进程托管（`io_port.py:149-154`，`processes/orphan_processes.py:75`），由之后的 daemon 保守回收。
交互成员必须从这两处豁免，否则有序停止会杀掉 TUI，或者下一个 daemon 把它当孤儿回收，见【冲突 C7】。

启动：

1. 若期望状态里没有结果为 `running` 的交互行，且私有 socket 连不上：什么都不做，不执行二进制。
2. 否则连上 core，先做版本比对（4.7），再取快照。
3. 对每个结果为 `running` 的交互行（裁决 R7）：
   - 找到身份匹配且 live 的终端：接管。程序没有被打断，carrier 会在发现 daemon 换代后重新报到并补报 turn 状态。
   - 找到身份匹配但已 finished 的终端：按 4.5 分类，等同于 daemon 当时在场。
   - 找不到：按 4.4 重拉。
4. 【提议 P11】带 hyprial 标签但没有对应启动行的终端：结束并移除。它只会出现在「终端已创建、启动行还没写下」的崩溃窗口里，与「从未成功的启动不留行」是同一条规则。
5. 接管完成后才放开 cell 成员 reconcile（沿用现有的启动闸 `CellReconcileStartBarrier`，`composition/cells/member_rows/rows.py:165`）。

### 4.4 终端丢失

来源：事件流里的 `lost`；终端以 `holder_lost`、`server_stop`、`host_shutdown`、`stopped_by_restart` 结束；或 core 整个没了，重连后的快照里找不到这个终端。

处理：移除旧记录，带启动行里的会话引用在 hc 里重拉（裁决 R7、R23）。接受的代价：重拉出来的 TUI 可能停在提示上，没人 attach；此时它会以 blocked 出现在成员状态里（4.8）。

### 4.5 程序自己结束

今天 harness 运行时对一个进程只观察「在不在运行」（`ProcessFactsObserved` 只有 `running`，`processes/process_owner.py:140`），没有退出码。本线为交互成员增加退出事实：hc 终端记录里的 `exit_code`、`signal`、`reason`。

【提议 P12】分类表（前三行是裁决 R23 原文，后两行是补出的细节）：

| hc 记录 | 判定 | 动作 | 是否消耗失败预算 |
| --- | --- | --- | --- |
| `exited`，退出码 0 | 用户主动退出 | 等同 stop：删启动行、移除终端、不重拉 | — |
| `exited` 非 0，或被信号杀死 | 失败 | 在现有退避和失败预算下带会话引用重拉 | 是 |
| 终端丢失（4.4 列出的原因） | 丢失 | 重拉 | 否：不是程序自己的失败 |
| `killed`，且是 daemon 自己发起的 | stop 或重启的一步 | 按发起它的操作继续 | — |
| `killed`，但不是 daemon 发起的（有人直接在私有 socket 上操作） | 按丢失处理 | 重拉 | 否 |

失败预算与退避用现有的：预算默认 3（`processes/supervisor.py:102-106`，环境变量可改，0 表示永不终止），退避在 `harnesses/actor/behaviors/process_events.py:202-210`。预算用尽后行标为 `failed`，留给人处理，与无头一致。

分类完成后立即移除已结束的终端记录：hc 把传给终端的环境变量明文保存到记录被移除为止。

判定只看两个字段：`signal` 非空或 `exit_code` 非 0 即失败，不依赖 `reason` 的具体取值。原因：`keep: false` 的终端被信号杀死时这三个字段怎样取值没有实测，接口描述里的 `signaled` 只说明了保留型终端（U16）。

「退出码 0 等于用户退出」对 claude、codex、pi 是否成立，正在实验中，见 12.1 的 U1。

### 4.6 配置或策略漂移

今天计划器对运行中的成员有三种结论（`cells/reconcile/planner.py:332-366`）：摘要一致则不动；只有策略摘要变了则 `POLICY_APPLY`（不重启，靠无头 worker 的确认握手）；其余变化则 `DRAIN_AND_RESTART`。

交互成员：

- 配置摘要变化：`DRAIN_AND_RESTART`，与无头相同。等这一 turn 结束（turn 边界来自 carrier 的成员级 turn 上报，hc 不知道模型的 turn），再带会话引用重启。
- 只有策略变化：交互成员没有确认握手，也无法在运行中的 TUI 里改参数，所以同样走 `DRAIN_AND_RESTART`。这是「终端本身迫使不同」的一处（裁决 R15、R24）。
- 模式变化：模式进了摘要（5.1），所以是普通的配置漂移，重启时换到另一种模式的 connector，会话引用保留。
- 【提议 P13】等待期间成员若处于 blocked，就一直等，不强行重启；状态里显示「待重启，等待 turn 结束（阻塞中）」。用户 attach 处理掉提示后 turn 结束，重启随即发生。这是对「等 turn 结束」最保守的读法，是否要加时限请负责人定（12.2）。
- 策略的 harness 覆盖范围不变：今天只有 claude 有策略到启动参数的映射（`cells/reconcile/policy.py:41`），别的 harness 成员声明了策略会被拒绝启动而不是悄悄丢掉策略。交互成员照此办理，见【冲突 C4】。

### 4.7 固定版本变化

实验事实：新版二进制能完整驱动旧版 core，反过来也行；用新版二进制执行 `server restart`，终端的 pid、地址、屏幕、历史都保留，新 core 是新版本，旧 holder 继续跑旧版本；重启之后旧版本的文件可以删。
经裸 RPC `server.restart` 则不行：幸存的 holder 约 3 秒后用它自己的可执行文件把 core 拉回来，版本跟 holder 走，可能是一次降级。所以换版本必须执行固定副本（裁决 R12 的第三项）。

流程（daemon 启动时，裁决 R22）：

1. 连上 core，`hello` 得到它的版本和协议代数；与锁文件比。一致则结束。
2. 不一致：确保固定副本已下载并校验。
3. 用白名单环境执行固定副本的 `server restart`。已 attach 的查看端掉线一次。
4. 重连，再次 `hello` 确认版本已是固定版本；重新订阅事件，从快照重建。
5. 下载不到：协议代数一致就继续用旧 core，成员状态和 daemon 状态里显示「core 版本落后于固定版本」；代数不一致则交互启动一律拒绝并说明原因，已有终端不动。

【提议 P14】版本比对在主管每次（重新）连上 core 时都做，不只在 daemon 启动时。原因：core 意外死亡后由谁、用哪个版本把它拉回来没有实测（12.1），多比一次能把这种情况纠正回来。

旧版本目录的删除见【冲突 C5】：hc 的状态接口列出每个 holder 的 pid、是否与 core 同版本、持有几个终端，但不给出 holder 的版本号。
【提议 P15】规则定为：当所有 holder 都报告与 core 同版本、且 core 已是固定版本时，删除 `bin/` 下除固定版本以外的所有目录（macOS 先注销 Launch Services 记录）。

### 4.8 阻塞状态进入 cell

hc 把程序经 OSC 7501 自报的状态记在终端上：`idle`、`working`、`done`、`blocked`、`error`，blocked 时可带 `permission` / `question` / `auth`。实验里这条链路是用人工发出的状态序列验证的；真实的 claude、pi、codex 在各自的提示上是否发出 `blocked` 没有测（U14）。老版本可能从不上报，此时状态为空，必须当作「未知」而不是「空闲」。

1. 主管从事件流收到 `status` 变化，把它写成该成员的运行事实：状态、种类、起始时间。
2. 【提议 P16】这个事实进入现有的 cell 可观测性归并（`cells/observability/reducer.py:35-57` 已有 `blocked` 状态和 `permission-wait`、`provider-block` 两个原因）：`permission` 映射到已有的 `permission-wait`，`question` 和 `auth` 各加一个原因值。
3. cell 拿到的是「成员 m 自某时刻起阻塞，种类 k」。本线到此为止。把它变成一条通知、按 cell 的连接数据路由到用户原先的通信地址、送达，归 cell / conversation / adapter 线（裁决 R16）。

## 5. 数据

### 5.1 启动行与摘要

- **模式。** 启动行就是 `HarnessLaunchSpec`（`kernel/impl/facts/launch_spec/spec.py:146`）。它已有 `headless` 字段和成员字段，SQLite 的 `harnesses` 表也已有 `headless` 列，主键是 `(harness, name, member)`（`daemon/impl/desired_state/sqlite.py:61-68`），不含模式，所以一个成员只有一行。
  要改的只是解析时的拒绝：今天 `headless` 为假会报「interactive sessions use interactiveSessions」（`kernel/impl/facts/launch_spec/spec.py:256-260`）。不新增列。
- **会话引用。** 用已有的 `session_ref` 字段。Claude 和 Pi 的引用由 daemon 在启动时生成；Codex 的线程 ID 要等 app-server 起来后才知道，由 connector 回写（已有同步会话引用的持久化命令）。
- **成员配置摘要。** 今天的摘要内容是 agent、成员、harness、模型、模型厂商、工作目录、命令、参数、审批、环境引用、策略摘要（`cells/reconcile/effects.py:177-207`），没有模式。
  【提议 P17】只在交互模式下往摘要内容里加一个模式键，无头时不加。这样所有现存无头成员的摘要逐字节不变，升级不会引发一轮成批的排空重启。
- **策略摘要。** 策略摘要的输入里本来就有 `headless`，但解析策略时写死为真（`cells/reconcile/policy.py:106-108`）。改为传成员的实际模式。无头的值不变。
- **活动侧摘要。** 对运行中的进程，今天用「进程确认过的策略摘要」来算活动摘要（`composition/cells/member_rows/rows.py:138-162`）。交互成员没有确认握手（裁决 R15）。
  【提议 P18】connector 启动终端时把这次启动所用的配置摘要写进终端标签；交互成员的活动摘要就读这个标签。接管一个终端后，拿标签与期望摘要比，不一致就是漂移，走 4.6。

### 5.2 终端归哪个成员（接管用）

hc 接口描述给出的事实（除注明外未单独实验）：终端名在 live 终端中唯一，只能是 1 到 32 个小写字母、数字、`-`、`_`、`.`（实验中含大写字母的名字被拒绝）；标签是任意键值对，hc 不解释，可以事后修改；终端地址（hc 分配的短编号）在这个 core 上永久不变；core 有一个只在状态被清空时才变的随机 ID。

agent ID 加成员名放不进 32 个字符，所以【提议 P19】：

- **名字**：固定前缀加 `(agent_id, member, 角色)` 的 SHA-256 截断编码，总长不超过 32。同一成员同一角色永远得到同一个名字。
  按接口描述，hc 不允许两个 live 终端同名；若属实，这就是「旧程序还在收尾时同名启动必须被拒绝」的结构性保证，不靠 daemon 自己记账（issue #2 的要求，见 10.2）。拒绝的具体表现没有实测（U13），实现时先测；不成立则由主管自己按名字查重后再启动。
- **标签**：agent ID、成员名、角色（TUI 或 app-server）、本次启动的随机 ID、本次启动的配置摘要、启动时的 hc 版本。
- **接管条件**：连的是本 home 的私有 socket；终端 live；名字等于按上式算出的名字；agent、成员、角色三个标签与启动行一致。
- **信任边界**：能连上私有 socket 的只有同一个操作系统用户，标签可被他修改。这与状态目录的信任边界相同；标签里不放任何密钥。
- **不落盘**：终端地址、hc 状态、退出事实都是运行事实，放在 harness 运行时的内存里，daemon 启动时从 hc 快照重建，不写进期望状态，更不进 OrgFS（裁决 R14）。hc 自己就是这些事实的持久存储。

### 5.3 成员状态里取代 `tmuxSession` 的内容

今天 `ps` 有一个 `interactiveSessions[]` 数组，每项带 `tmuxSession`（永远为空）和一组 `channel*` 字段（`application/actors/sessions/registry.py:100-165`）。

【提议 P20】该数组删除；成员的运行视图里增加一个只在本机可见的 `terminal` 对象：

```
terminal: {
  state:        "live" | "finished" | "lost" | "absent",
  address:      "<hc 终端地址>",
  status:       "idle" | "working" | "done" | "blocked" | "error" | null,   // null 表示程序从未上报
  statusKind:   "permission" | "question" | "auth" | null,
  statusSinceMs: <毫秒>,
  carrierReady: true | false,
  carrierStale: true | false,          // carrier 的构建版本落后于 daemon
  restartPending: null | "config" | "policy",
  coreVersion:  "X.Y.Z",  pinnedVersion: "X.Y.Z"
}
```

`ps` 是已发布的输出，删数组、加对象都是契约变化，列在第 7 节。`hyprial doctor` 里读 `interactiveSessions` 的通道检查（`shell/impl/cli/commands/doctor/commands.py:572`）改读这个对象。

## 6. 旧交互会话子系统的三向拆分

旧子系统今天的样子：交互会话由 CLI 在前台启动，不受 daemon 监管；harness 一侧的 carrier 再经本机 IPC 回来「登记」，daemon 据此写一行 `InteractiveSession`、绑定 agent、声明路由、让它拉收件箱。每一层都以 actor（agent 的 URI）为键，一个 agent 只能有一个交互会话，没有成员这个维度。

去向三种：**留 cell** 指留在 cell 一侧、键改为成员；**并入** 指改名改键并入 harness 运行时；**删** 指删除。
表中路径的简写：`registry.py`、`routes.py`、`turns.py` 指 `daemon/impl/application/actors/sessions/` 下的文件；`runtime/` 指 `shell/impl/cli/commands/agent/runtime/`；其余 daemon 路径省略了 `src/hyprial/daemon/impl/` 前缀。

| 部件 | 今天的位置 | 去向 | 说明 |
| --- | --- | --- | --- |
| cell 入口准入 `interactive_admission` | `composition/cell_delivery/headless.py:153`，`composition/cell_delivery/port.py:33` | 留 cell | 行为不变，键从 actor 改为 `(agent_id, member)`（裁决 R18）。 |
| origin 托管 | `application/messaging/inbox_surface/origin_custody.py:51-127`，`inbox/contracts/origin.py:31-45` | 留 cell | 认领者从 `(actor, session_ref)` 改为成员加会话引用。 |
| 会话路由 `session:<actor>` 与其协调器 | `network/routing/session_route_coordinator.py:36-95`，`application/actors/sessions/routes.py` | 留 cell | 路由改由 daemon 在成员启动和停止时声明和撤销；登记、续期、心跳、过期这几种触发随外部登记一起消失。 |
| 子进程调用的会话栅栏 | `application/actors/sessions/registry.py:730`、`:801` | 留 cell | TUI 里的模型调用工具或 CLI 时带的会话引用，仍要与该成员当前的会话引用比对；比对的对象从会话表改为启动行。 |
| `WORK_SOURCE_INTERACTIVE` 词汇 | `cells/actor/contracts.py:74` | 留 cell | 不动，归 cell 模型线。 |
| 启动事实：工作目录、命令、harness | `desired_state/documents.py:45` 的各字段 | 并入 | 成为 `headless` 为假的 `HarnessLaunchSpec` 行（5.1）。 |
| `source`、`runtime` 两个字符串字段 | 同上 | 删 | 由 `(harness, 模式)` 推出，不再存。 |
| 会话引用 | 同上 | 并入 | 启动行的 `session_ref`。 |
| 终端地址（原 `tmux_session`） | 同上 | 并入 | harness 运行时里的运行事实，来自 hc（5.2）。 |
| turn 上报 | `application/actors/sessions/turns.py:78`、`:107`、`:207`；`harnesses/carrier/contract.py:46-53`；`request_catalog/session.py:105` | 并入 | 成员级接口（3.5）；账本的键加上成员。hc 不知道模型的 turn，这部分事实仍只能来自 carrier。 |
| 交互 turn 事实到 cell 可观测性 | `turns.py:472` | 并入 | 与无头走同一条可观测性输入。 |
| carrier 版本检查 | `registry.py:100-165`、`:397-418` | 并入 | `agent.member.carrier.hello`。保留的理由：被接管的 TUI 里的 carrier 跨过了 daemon 升级，仍在跑旧构建。 |
| daemon 换代后的 turn 重新对齐 | `session_actor/internals.py` | 并入 | 合进 carrier.hello 与 turn.refresh。 |
| Codex 的 carrier 与 app-server | `harnesses/codex/carrier.py:242`，`harnesses/codex/app_server.py:131`；今天由 CLI 进程持有（`runtime/sessions.py:461-812`） | 并入 | 由 codex 交互 connector 在 daemon 内持有（裁决 R9）。 |
| 三个 harness 的交互启动参数组装 | `runtime/claude.py:102`，`runtime/sessions.py:67`、`:461` | 并入 | 搬进各自的交互 connector。 |
| pid 与出生栅栏 | `registry.py:239`；`session_actor/process_observation.py:14`；`routes.py:709`；`agents_admin/destroys.py:244` | 删 | 只为证明一个不是 daemon 启动的进程归谁、是否还活着。 |
| 通道租约与心跳 | `registry.py:605`；`routes.py:776`、`:840` | 删 | 存活由 hc 的终端状态给出。TUI 还活着但里面的通道子进程卡死，是更窄的另一个问题，见 12.2。 |
| `owner_fence` | `runtime/claude.py:271-273`；`doctor/commands.py:595` | 删 | 不再有发起启动的 CLI 父进程。 |
| `channel_confirmed` 与 CLI 轮询 | `runtime/interactive.py:469`、`:491` | 删 | 就绪由 daemon 直接知道（4.1）。 |
| 外部登记 | `registry.py:197`、`:291`、`:427`、`:632` | 删 | daemon 在启动时自己完成登记。 |
| `interactive_sessions` 表与 `InteractiveSession` | `desired_state/sqlite.py:82`，`desired_state/documents.py:45`，`desired_state/sessions.py` | 删 | 不迁移数据：旧行对应的前台进程不归 daemon 管，升级后由用户重新启动（迁移说明）。 |
| `SessionActor` 及跨域绑定效果 | `session_actor/` 整个包 | 删 | agent 的绑定与释放改由统一的生命周期路径完成，与无头相同。 |
| IPC 调用者归类里对会话表的扫描 | `ipc/router.py:229-256` | 删并替换 | 见【冲突 C3】。 |
| `tmux_session` / `tmuxSession` | `src/hyprial` 下 7 个文件共 21 行（存储、线上字段、投影的直通） | 删 | 今天没有写者，也没有行为上的读者。 |
| CLI 前台启动 | `runtime/claude.py:502`，`runtime/sessions.py:309`、`:671`，`runtime/interactive.py:124` | 删 | 裁决 R3。 |
| 交互启动的明文密钥环境 | CLI：`runtime/interactive.py:413-454`；daemon：`agents_admin/registry_ops.py:726-742` | 删 | 裁决 R10：删除而非搬迁。 |
| `--replace-interactive` | `runtime/start.py:86`；`application/actors/lifecycle_ops.py:726-768` | 删 | 见第 7 节与 12.2：它靠出生栅栏去杀一个外部进程，该栅栏不再存在。 |
| 时间线里的 `session.registered` / `session.unregistered` | `shell/impl/cli/commands/message/observe/trajectory.py:33-34` | 删 | 今天就没有发出者。 |
| 4 个 tmux e2e 驱动 | `contract/e2e/run-cc-tui-worker.py`、`run-codex-interactive-attach-gate.py`、`run-pi-interactive-attach-manual.py`、`run-tui-attach-manual.py` | 迁移 | 改为经 daemon 启动、经 hc 读屏和送键（裁决 R3）。 |

### 6.1 子系统之外读会话表的地方

会话表被删之后，下面这些读者各自改读什么。行号取自基线提交。

| 读者 | 位置 | 今天读什么 | 之后 |
| --- | --- | --- | --- |
| agent 存活绑定的开机播种 | `identity/impl/agents/actor/_core.py:880-897`；`application/actors/agents_registry.py:446-453` | 逐个会话把 agent 绑为交互运行时 | 上面紧邻的循环已经按启动行绑定无头运行时；改为按启动行的模式绑定，一个循环。 |
| 会话表的启动期规整 | `application/actors/agents_registry.py:128-168` | 把旧行的 actor 规整成 URI | 删除。 |
| 存活判定 | `identity/impl/agents/state/liveness.py:315` | 交互绑定的进程是否还活着（靠 pid 探测） | 改问主管：该成员的终端是否 live。 |
| 「已有在位 connector」的提示语 | `identity/impl/agents/state/liveness.py:118` | 交互在位时提示「退出那个会话」 | 改为提示 `agent stop` 或 `agent tui attach`。 |
| home 迁移的预检 | `identity/impl/agents/migration/actor.py:186-197` | 把会话表的行和待处理的跨域效果列为「该 agent 仍有期望行」 | 交互行从启动行读（模式为交互）；跨域效果一项随 `SessionActor` 消失。预检的结论口径不变：有任何期望行就不迁。 |
| teleport 源端准入 | `daemon/impl/teleport/wiring/source_ops.py:78-96` | 同名既在受管 harness 又在交互会话里时拒绝 | 一个成员只有一行，这种双重登记不会再出现；这条拒绝删除，匹配只看启动行。 |
| daemon 恢复摘要 | `daemon/impl/runtime/bridge.py:459-464` | 重启后不在线的交互会话 | 见 7.4。 |
| Claude 通道进程里的唤醒调度 | `daemon/impl/mcp/wake.py:469-471`，`daemon/impl/mcp/channel/session.py:138` | 进程内自建的单行会话表，按 actor 取 | 这是通道子进程自己的内存结构，不是 daemon 的表；随会话类型删除而换成成员身份。 |
| 状态投影与 `ps` | `application/actors/status_projection/`，`application/messaging/status/views.py`，`daemon/impl/status/` | 会话行与心跳 | 改读启动行加终端事实（5.3）。 |
| 装配 | `application/wiring/services.py`、`application/wiring/transport.py`、`composition/domains.py`、`application/messaging/maintenance.py`、`application/actors/stored_agent_uris.py` | 把会话域接进各处 | 随主体改动，逐个文件在实现时处理。 |

运行时标记 `RUNTIME_INTERACTIVE`（`identity/impl/agents/state/liveness.py:57`）保留，含义从「CLI 启动的交互会话」变为「模式为交互的成员」。`agent.create` 的 `runtime` 参数里那个交互标记同理。

## 7. 需负责人逐项批准的条目

一行一项。「今天」一栏的行号取自基线提交。批准本设计不等于批准下表各项；实现到某一项时仍按行单独确认（裁决 R5、R17）。

### 7.1 `session.*` 方法

七个旧方法都冻结在 `scripts/ci/legacy-method-baseline.json:48-54`，并列在 `request_catalog/migration.py:255-261` 的「仅路由器」名单里；`session.turn.refresh` 已迁移并发布。GUI 不调用其中任何一个。删除外部登记对测试面的影响见【冲突 C1】。

| 方法 | 今天 | 提议 | 理由 |
| --- | --- | --- | --- |
| `session.register` | 冻结旧方法；carrier 和 Codex 的 CLI 调用 | 删除 | daemon 自己启动成员，登记在启动时完成。 |
| `session.register_guarded` | 冻结旧方法；Pi 和 Codex 恢复时调用 | 删除 | 「不许顶掉在位者」的检查并进 `agent.start` 的拒绝检查。 |
| `session.unregister` | 冻结旧方法 | 删除 | 成员停止由 daemon 观察到，不靠 carrier 告知。 |
| `session.heartbeat` | 冻结旧方法 | 删除 | 存活来自 hc 的终端状态。 |
| `session.refresh` | 冻结旧方法；carrier 在 daemon 换代后调用 | 删除，由新方法 `agent.member.carrier.hello` 取代 | 归属声明不再需要；版本检查和 turn 覆盖范围仍需要。 |
| `session.turn.started` | 冻结旧方法 | 改名为 `agent.member.turn.started`，进请求目录，参数加成员 | 裁决 R17：成为成员级接口。 |
| `session.turn.ended` | 冻结旧方法 | 改名为 `agent.member.turn.ended`，同上 | 同上。 |
| `session.turn.refresh` | 已迁移（`request_catalog/session.py:105`），在 `contract/daemon-request/openapi.json` 里发布，不经 HTTP 暴露 | 改名为 `agent.member.turn.refresh`，参数加成员，去掉租约令牌 | 同上；这是一次已发布契约的改名。 |

### 7.2 启动与生命周期方法

| 方法 | 今天 | 提议 | 理由 |
| --- | --- | --- | --- |
| `agent.start` | 已声明并经 HTTP 暴露（`request_catalog/agent/start.py:362-369`），只覆盖「全新的无头启动」 | 扩成唯一的启动请求：加模式、成员、恢复用会话引用、工作目录、昵称、参数、推理强度等。【提议 P21】交互模式也接受 HTTP 调用（GUI 可以启动交互成员），attach 仍只能在本机做 | 裁决 R5。已发布契约的扩展：OpenAPI、生成的 GUI 客户端、契约 mock 同步。 |
| `lifecycle.start` | 冻结旧方法（`legacy-method-baseline.json:29`，`ipc/router.py:113`）；CLI 的带参无头启动走它 | 删除，并入 `agent.start` | 一个启动请求。 |
| `lifecycle.resume.preflight` | 冻结旧方法（`legacy-method-baseline.json:28`） | 删除，成为 daemon 内部的一步 | 预检搬进 daemon。 |
| `agent.start.preflight` | 冻结旧方法（`legacy-method-baseline.json:23`） | 删除，成为 daemon 内部的一步 | 同上。 |
| `agent.resume.restore` | 冻结旧方法（`legacy-method-baseline.json:16`）；CLI 在恢复失败时用它回滚 agent 记录 | 删除，回滚由 daemon 自己做 | 补偿写不该由 CLI 发起。 |
| `agent.runtime-launch.acquire` / `custody` / `release` | 三个冻结旧方法（`legacy-method-baseline.json:19-21`，`ipc/router.py:94-96`）；`acquire` 的结果里带明文 `secretEnvironment`；调用者只有 CLI 的交互启动（`runtime/interactive.py:426`、`:461`） | 三个都删除。daemon 内部给无头用的启动托管不动 | 裁决 R10。 |
| `agent.stop` | 已声明 | 契约不变；对交互成员的含义是结束并移除终端、删启动行 | — |
| `agent.tui.attach` | 无 | 新增：只面向本机操作者，不经 HTTP 暴露，不改状态 | 裁决 R13。 |
| `agent.member.carrier.hello` | 无 | 新增：carrier 调用，不经 HTTP 暴露 | 裁决 R17 保留的 carrier 版本检查。 |
| `--replace-interactive` 与参数 `replaceInteractive` | CLI 选项（`runtime/start.py:86`）；daemon 靠出生栅栏结束外部交互进程（`agents_admin/destroys.py:244`） | 【提议 P22】删除。切换模式的做法是先 `agent stop` 再带 `--resume` 以另一种模式启动；`agent stop` 的输出里给出会话引用 | 栅栏被删；裁决 R13 规定对运行中的成员 start 是错误。是否要一条不停顿的切换命令见 12.2。 |
| 新的错误码 | — | 【提议 P23】四个新拒绝各一个码：交互启动遇到 Key 类连接、成员的旧终端仍在收尾、终端宿主不可用（副本下载或校验失败、core 起不来）、路径超限。「成员已在运行」优先复用现有码 | 码表是冻结面，每个新码单独确认。 |

### 7.3 CLI 与状态输出

| 项 | 今天 | 提议 | 理由 |
| --- | --- | --- | --- |
| `hyprial agent tui attach` | 无 | 新命令，进 `contract/cli/surface.json` | 裁决 R13。 |
| `hyprial agent start` | 在清单里（`contract/cli/surface.json:77`） | 去掉 `--replace-interactive`；加 `--member`；行为改为请求加 attach | 裁决 R3、R13。 |
| `ps` 的 `interactiveSessions[]` | 已发布输出 | 删除数组；成员视图加 `terminal` 对象（5.3） | 裁决 R3、R17。 |
| 能力声明 | 交互 codex 的会话生命周期标为降级（`harnesses/capabilities.py:196-200`）；交互 claude 的权限设置标为不支持（`harnesses/capabilities.py:117`） | 前者改为原生；后者改为「经启动参数」 | 裁决 R9、R15。 |

### 7.4 账本、基线、清单

| 文件 | 今天 | 本线的增删 | 理由 |
| --- | --- | --- | --- |
| `scripts/ci/legacy-method-baseline.json` | 冻结的旧方法名 | 删 14 个名字：7.1 的七个，7.2 的七个。不新增 | 这些方法被删除或改名进目录。 |
| `request_catalog/migration.py` | 「仅路由器」名单与已迁移名单 | 同步删去上述名字；`session.turn.refresh`（`request_catalog/migration.py:185`）换成新名字；新增方法进目录 | 与上一行一致。 |
| `contract/daemon-request/openapi.json` 与契约 mock | 已发布 | `agent.start` 的参数与结果扩展；`session.turn.refresh` 改名；新增三个成员级方法和 `agent.tui.attach` | 已发布契约。 |
| `scripts/ci/registry-union.json` | 方法名并集 | 随上面的增删重新生成 | 机械同步。 |
| `scripts/ci/daemon-request-architecture-baseline.json` | 引用 `runtime/claude.py` 19 处、`interactive.py` 14 处、`sessions.py` 35 处、`start.py` 7 处 | 随 CLI 编排搬进 daemon 而删除；不新增 | 这些条目记录的正是 CLI 里的业务逻辑。 |
| `scripts/ci/import-ratchet-baseline.json`、`import-ratchet-exceptions.json` | 基线引用上述四个文件共 48 处，例外 5 处 | 只减不增 | CLI 不再导入 daemon 的启动内部件。 |
| `scripts/ci/unbounded-waits-baseline.json` | `sessions/routes.py` 与 `mcp/channel/session.py` 各 1 处 | 前者随删除消失；后者重新评估。事件流读取用有界超时，不新增条目 | — |
| `scripts/ci/windows-portability-baseline.json` | 12 个文件，都不在启动路径上 | 不新增（裁决 R20） | — |
| `scripts/ci/network-reachability-baseline.json` | 已审过的 IPC 根到网络边界的路径 | 目标是不新增（3.3 的 P5）；若门仍判定下载器为新路径，则新增一行 | 下载是真实的新出站访问，是否算作请求可达需实现时确认。 |
| `tests/shell/cli/installation/restart/post_install/post_install_surface.json` | 列有 `agent.runtime` 下各模块、`desired_state.sessions`、`mcp.channel.*` | 重新生成；闭包里的增减逐个说明 | 自升级尾段允许执行哪些模块是受控面。 |
| `scripts/ci/structural-baseline.json` | — | 不新增：新包遵守 7 文件、1000 行的上限 | — |
| wheel 门禁 | `hyprial/_bin/`、`hyprial/_core/` 下不许多出文件 | 不变：hc 不进 wheel，锁文件是普通包数据 | 裁决 R19。 |
| `scripts/ci/windows/` 下的测试清单 | Windows 一级测试清单、待入清单与拒收清单 | 新增的测试是否进一级清单逐个决定；进不了的按该清单现有规则登记，不为它们改规则 | 裁决 R20：现有 Windows 检查保持通过。 |
| `contract/e2e/scenarios.json` 及场景文档 | E2E-008（`contract/e2e/scenarios.json:238`）等场景以 tmux 和「交互会话登记」描述步骤；`contract/e2e/08-cc-tui-worker.md`、`codex-interactive-attach-manual-review.md` 同 | 随驱动迁移改写：步骤改为经 daemon 启动、经 hc 观察 | 裁决 R3。 |
| 提到 `session.*` 的契约文档 | `contract/daemon-lifecycle/interactive-turn-refresh.md`、`contract/daemon-lifecycle/run-state.md`、`contract/wire/mcp-channel-stale.md`、`contract/identity/agent-addressing.md` | 按新的成员级方法改写；其中只描述已删除机制（租约、外部登记）的段落删除 | 契约文档与方法同步。 |
| daemon 恢复摘要里的 `missing_interactive` | 列出重启后不在线的交互会话（`daemon/impl/runtime/bridge.py:459-464`） | 改为列出未能接管、已重拉或重拉失败的交互成员；字段改名 | 会话表不存在后原含义消失。 |
| `GLOSSARY.md` | 无相关词条 | 【提议 P24】实现时加：成员终端、私有 core、固定副本、carrier；改写 connector 一条 | 词汇表是公开用词的依据。 |
| 迁移说明与 `CHANGELOG.md` | — | 【提议 P25】新增 `docs/guides/migration-interactive-sessions-retired.md`（沿用 `docs/guides/migration-*.md` 的命名），CHANGELOG 的 Removed / Changed 各记一笔 | 裁决 R17：不写迁移代码，只写说明。 |

### 7.5 kernel 的本机 IPC 入口（W020）

| 项 | 今天 | 提议 | 理由 |
| --- | --- | --- | --- |
| `connect_local_ipc` 作为 hc socket 的传输 | 规则 W020 禁止 kernel 之外的原始本机 IPC，且不允许有基线条目（`scripts/ci/check_windows_portability.py:190`）。入口至今只连 hyprial 自己的端点 | hc 客户端经这个入口连 hc 的 socket。POSIX 上不改 kernel 代码，不新增任何基线条目 | 裁决 R12。这是该入口第一次连第三方端点，所以单列一行。Windows 见【冲突 C2】。 |
| kernel 里的路径辅助 | `managed_harness.py` 有同类先例 | 新增 `<home>/state/hc/` 的路径计算（纯函数） | 分层：路径词汇在 kernel，逻辑在 daemon。 |

## 8. 安全

- **终端里没有密钥。** 交互启动不再从 daemon 取明文密钥环境（第 6 节）；第一版只允许原生 Auth（裁决 R11）。传给 `terminal.run` 的环境变量只有非密钥项。会话引用文件的路径在环境里，文件本身是私有权限，内容是会话引用，不是密钥。
- **core 的环境里没有密钥。** core 只从白名单环境启动（3.2）。实验事实：`server restart` 启动的新 core 沿用旧 core 保存的环境，所以第一次启动干净，之后一直干净。CLI 执行 attach 也用同一份白名单（4.2）。
- **明文文件。** 以下文件由 hc 写出，hyprial 改不了它们的内容，只能保证里面没有密钥：
  - 状态目录里的终端记录：每个终端的 `terminal.run` 环境变量明文保存到记录被移除为止，即使终端已结束。对策：只传非密钥项；结束的终端在分类后立即移除（4.5）。
  - Linux：core 停止后，运行时目录下留有 core 启动环境的明文文件，以及一个已退出的 systemd 单元。对策：白名单环境。【提议 P26】主管在停掉 core 之后清理它自己那个 core 留下的运行时目录；清理不到的记入状态而不是忽略。
- **下载的信任锚。** 发布站没有签名，也没有版本索引；哈希只出现在它自己的清单里，可信程度等同于送达它的 HTTPS 通道。所以唯一的信任锚是仓库里锁文件的 sha256 与 size：改版本就是一次经过评审的提交。下载器不读发布站的清单，不跟随「最新版本」，校验不过就不解包、不执行。
  旧版本今天都还能下载，但发布方没有书面的保留承诺：某个被固定的版本一旦下架，新机器就无法首次启用交互，直到锁文件前移。
- **私有 socket。** 在 hyprial home 下的私有目录里，只有同一操作系统用户能连。能连上的人可以直接操作终端和改标签；这与能写状态目录的人能做的事相当，不构成新的边界。用户自己的 hc 只要带上那两个私有变量也能驱动这个 core，文档里要说明不要这么做。
- **调用者归类。** 见【冲突 C3】：删掉出生栅栏之后，「这个本机 IPC 对端是 agent 还是操作者」的一条判据没了，需要替代。
- **macOS 隐私授权。** 隐私提示的归属是 hc 的签名身份而不是 hyprial：提示框上写的是上游应用的名字，授权在用户自己安装的那份与 hyprial 这份之间共享。hyprial 无法拥有自己的隐私身份。

## 9. 平台

| 平台 | 状态 | 要点 |
| --- | --- | --- |
| macOS | 验收平台 | 保留完整的签名 `.app`，运行里面的二进制；每份副本会多出一条 Launch Services 记录，删除时注销；隐私提示显示上游应用名。core 以一次性 launchd 任务脱离调用者。 |
| Linux | 验收平台 | 一个静态二进制。保留真实 `XDG_RUNTIME_DIR`，core 才会进自己的 systemd 临时单元、不随 daemon 的单元被杀。`/proc` 专属的语义只在 Linux 上测。 |
| Windows | 不保证可用，不做验收（裁决 R20） | 锁文件带 Windows 目标；解包处理 `herdr-core.exe` 加 `conpty/` 的布局；新代码不新增可移植性基线条目；现有的 GitHub Windows 检查（workflow `windows-smoke`：构建 wheel、一级测试波次、x64 与 ARM 冒烟）保持通过；不预先拒绝。Codex 的 `unix://` 不在本线修。hc 客户端在 Windows 上的传输见【冲突 C2】。 |

## 10. 测试计划与验收

### 10.1 测试分层

| 层 | 内容 | 在哪跑 |
| --- | --- | --- |
| 单元测试 | 针对一个进程内的 hc socket 协议假实现：握手、终端的启动 / 结束 / 移除、事件流（含流中断与序号重置）、标签、状态变化、版本号。覆盖主管、connector、退出分类、接管、版本迁移的决策。下载器对本地假服务器测重试、size 与 sha256 不符、路径穿越。 | 合并门内 |
| 场景测试 | 用真实的固定副本、真实的 claude / pi / codex，在一台 Linux 和一台 macOS 机器上跑第 10.3 节的清单。 | 门外，作为验收证据 |
| 既有门禁 | 结构、导入棘轮、请求架构、可移植性、契约、自升级尾段。 | 合并门内，含 GitHub Windows runner |

假实现必须与真实 hc 的行为对得上的几处：流在重启时无声结束；任何子命令都会启动 core；`keep: false` 的终端在 `server stop` 后以 `server_stop` 结束（以上三处来自实验）；同名 live 终端被拒绝（来自接口描述，U13）。

关于「合并门」：基线提交上只有一个 workflow（`.github/workflows/windows-smoke.yml`），最小合并门还在一个未合入的 PR 里（#78）。本文说「合并门内」，在该门合入后指由它运行；在此之前指每片自合并进集成分支前在本地跑的同一组测试。

### 10.2 从 issue #2 带过来的成对用例

issue #2 描述的是已删除的 tmux 闸门，但它的验收要求原样保留，两条必须同时成立：

- 旧程序还在收尾时，用同一成员再次启动：必须被拒绝。
- 旧程序结束之后，同样的启动：必须被允许。

只测前一条分不出「修好了」和「把门焊死了」。

### 10.3 验收清单（裁决 R25）

在真实的 macOS 与 Linux 机器上，用固定副本演示：

1. claude、pi、codex 以原生 Auth 交互启动并 attach。
2. 脱离后重新 attach，屏幕和对话都在。
3. daemon 停止再启动，接管终端，程序未被打断。
4. 终端丢失后，带会话引用重拉。
5. 退出码 0 不重拉；非 0 重拉。
6. 改固定版本后，core 换到新版本，程序仍活着。
7. 卡在权限提示上的 TUI，在 daemon 里显示为成员的 blocked 事实。

另外：

- 旧路径已消失：第 6 节标「删」的各项、CLI 前台启动、tmux e2e 驱动。
- 无头行为不变。
- GitHub 检查全绿，含 Windows runner。Windows runner 通过只说明代码能在 GitHub 托管的 Windows 上构建和跑既有测试，不等于交互终端在 Windows 真机上可用。

## 11. 切片（提议）

【提议 P27】整节都是提议。顺序按依赖排；每片在自己的 worktree 实现，自合并进 `intg/hc-terminal-backend`。
「碰启动路径」的切片要等模型连接那串 PR 进 `dev` 之后再开工（裁决 R21，何谓「进了」见【冲突 C6】）；S1 到 S3 不碰，可以先做。

| 片 | 成果（功能、架构或体验上的变化） | 依赖 | 碰启动路径 |
| --- | --- | --- | --- |
| S1 固定副本 | daemon 能按仓库锁文件把指定版本的 hc 取到本机并验明正身；换版本只需改一个文件。此时还没有任何用户可见变化。 | 无 | 否 |
| S2 hc 客户端 | daemon 能不执行任何二进制、只经 kernel 入口与一个 hc core 对话并跟随它的事件；同时有了供门内测试用的协议假实现。 | 无 | 否 |
| S3 私有 core 主管 | hyprial 拥有一个与用户 core 隔离、环境里没有密钥的私有 core：能启动、跨 daemon 重启接管、换到固定版本、在空了之后停掉；状态里看得到它的版本。 | S1、S2 | 否 |
| S4 统一启动 | 启动成为一个 daemon 请求：CLI 里的预检、创建、回滚搬进 daemon；启动行和摘要认识模式；分发表建立。用户看到的无头启动行为不变，但 CLI 不再含业务逻辑。 | 模型连接合入 | 是 |
| S5 Claude 交互 | 第一个可用的交互成员：`agent start` 在 hc 里拉起 Claude TUI 并立即 attach，`agent tui attach` 可重连；daemon 重启接管；终端丢失、程序退出、配置和策略漂移按规则处理。成员级 turn 接口与 carrier.hello 随本片引入。 | S3、S4 | 是 |
| S6 阻塞事实 | 没人 attach 的 TUI 卡在提示上时，成员状态和 cell 都能看到「阻塞、何种、自何时」。 | S5 | 否 |
| S7 Pi 交互 | Pi 成员获得与 S5 相同的全部行为。 | S5 | 是 |
| S8 Codex 交互 | Codex 成员获得同样的行为，并且第一次能跨 daemon 重启存活：app-server 住进 hc，carrier 住进 daemon。 | S5 | 是 |
| S9 拆除旧子系统 | 旧的交互会话子系统消失：准入、origin 托管、路由改以成员为键；会话表、`SessionActor`、外部登记、心跳、栅栏、`tmuxSession`、CLI 前台路径、明文密钥环境全部删除；迁移说明与 CHANGELOG 写好。 | S5、S7、S8 | 是 |
| S10 驱动迁移与验收 | 4 个 tmux e2e 驱动改用 hc；两台真机上的场景测试跑完第 10.3 节清单并留下证据；最终 PR 可以提交。 | S9 | 否 |

集成分支在 S5 到 S8 之间新旧两条路径并存，这是集成分支内部的中间态；进 `dev` 的只有最后一个 PR，届时只有一条路径（裁决 R1）。

## 12. 未核实的假设与待决事项

### 12.1 未核实的假设

| 编号 | 假设 | 依赖它的地方 | 现状 |
| --- | --- | --- | --- |
| U1 | claude、codex、pi 在每一种正常退出方式下都返回 0，在崩溃或被杀时返回非 0 或死于信号。 | 裁决 R23，4.5 | 实验进行中，尚无结论。若某个 TUI 的正常退出返回非 0，它会在用户退出后被拉回来；反之崩溃会被当成用户退出。 |
| U2 | Claude TUI 接受允许 / 禁止工具的启动参数，且「允许」意为不再询问、「禁止」确实禁用。 | 裁决 R15，4.6 | 凭记忆，没有对照当前文档或实测。 |
| U3 | macOS 上用户已给上游应用的隐私授权，会沿用到 hyprial 路径下的那份副本。 | 第 8、9 节 | 静态证据支持（两份的签名要求与代码哈希逐字节相同，要求里不含路径），没有做会弹框的动态测试。 |
| U4 | 以上所有行为在 Windows 上成立。 | 第 9 节 | 完全未测；hc 在 Windows 上用什么本机传输也没有确认。 |
| U5 | `hc update --from` 只作用于指定的副本和 session。 | 未使用 | 未测，本设计不用它，换版本只用 `server restart`。 |
| U6 | core 意外死亡（崩溃、被强杀）后，holder 的行为与计划内重启后相同。 | 4.4、4.7 的 P14 | 只测过计划内重启。 |
| U7 | hc 的 API 在补丁版本之间保持兼容。 | 整个 hc 客户端 | 没有书面的兼容承诺。实测 0.1.21 到 0.1.22 只增不减，但有一个错误码的数值变了。固定版本加门外场景测试是对策。 |
| U8 | 由 launchd 或 systemd 托管的 daemon 停止时，私有 core 和终端不会随它一起被服务管理器结束。 | 裁决 R9，4.3 | 实验里 core 都是从 shell 启动的。Linux 上已确认保留真实运行时目录时 core 进自己的单元；由服务托管的 daemon 这个具体场景没有测。 |
| U9 | 被固定的 hc 版本在需要的时间内一直可下载。 | 3.3，第 8 节 | 今天所有版本都在，无书面承诺。 |
| U10 | Codex 在 hc 里需要以不启用其自身守护进程的方式运行；app-server 与 TUI 分住两个终端可以正常配合，app-server 的 socket 可以放在固定路径。 | 3.4 | 来自 hc 的文档和现有代码，未在 hc 里实测。 |
| U11 | 每个 harness 的 carrier 在被接管后都能自己发现 daemon 换代并重新报到。 | 4.3 | Claude 和 Pi 今天有这条路径；Codex 的 carrier 今天不做整会话的 turn 刷新，要补。 |
| U12 | 多个不同版本的 holder 同时幸存时由哪一个把 core 拉回来。 | 4.7 | 实验里新的那个赢过一次，没有分离出规则。 |
| U13 | hc 拒绝启动第二个同名的 live 终端，且拒绝是一个可识别的错误。 | 5.2，10.2 | 只有接口描述里的一句话，未实测。 |
| U14 | 真实的 claude、pi、codex 停在权限、提问、登录提示上时会上报 `blocked` 及其种类。 | 裁决 R16，4.8，验收第 7 项 | 实验用的是人工发出的状态序列。claude 与 pi 在一个在用的 core 上见过 `working` / `done`；codex 的支持只来自其二进制里的字符串。 |
| U15 | 终端里的程序结束时，已 attach 的客户端以什么方式、什么退出码返回。 | 4.2 | 未测。设计不依赖它：CLI 不看退出码。 |
| U16 | `keep: false` 的终端被信号杀死时，hc 记录里 `exit_code`、`signal`、`reason` 的取值。 | 4.5 | 未测；实验里只见过自然退出的退出码和 `server_stop`。 |
| U17 | 固定版本低于运行中的 core（hyprial 回滚到旧版本）时，换版本同样可行。 | 4.7 | 降级重启在 Linux 上测过一次，终端保留；macOS 未测。旧版 core 读取新版保存的、含 live 终端的状态未测。 |

### 12.2 待负责人决定

1. **切换一个运行中成员的模式用什么命令。** 裁决 R13 规定对运行中的成员 start 是错误，裁决 R14 要求模式切换被看成漂移，`--replace-interactive` 所依赖的栅栏被裁决 R17 删除。P22 提议先 stop 再带会话引用 start。是否需要一条不停顿的切换命令？
2. **排空等待遇到 blocked。** P13 提议一直等。是否加时限、到时强行重启？
3. **TUI 活着但通道子进程卡死。** 心跳删除后，这种情况不再有信号。是否需要一个比进程存活更窄的 carrier 存活信号，还是接受到用户发现为止？
4. **Linux 上 hc 停止后的残留。** P26 提议主管清理自己的运行时目录。残留的已退出 systemd 单元是否也要清？
5. **用户可见的「停掉所有终端」。** 今天停掉所有 agent 就会让最后一个终端消失、core 随之停止。是否还要一个单独的入口？
6. 下面 12.3 的各项冲突。

### 12.3 裁决与现状的冲突

本文没有替负责人取舍，下列各项按裁决原样写进了前面各节，并在此说明对不上的地方。

| 编号 | 裁决 | 对不上的地方 | 本文的处理 |
| --- | --- | --- | --- |
| C1 | R3、R17 | 裁决 R3 点名迁移 4 个 tmux 驱动。但 `contract/e2e/` 下另有 12 个脚本直接调用 `session.register` 或 `session.heartbeat`，把自己伪装成一个在线的交互 carrier（跨节点、群聊、升级连续性等场景）；`tests/` 下有 91 个测试文件出现这两个方法名，一部分是旧子系统自己的测试，随它删除，其余把它当夹具用。裁决 R17 删除外部登记后它们全部失效，而它们不在 R3 的范围里。 | 未纳入切片。【提议 P28】这些脚本改为经 daemon 启动一个无头的桩成员，不为测试保留登记方法；工作量单列一片，排在 S9 之前。需负责人确认范围。 |
| C2 | R12、R20 | kernel 的本机 IPC 入口在 Windows 上走 hyprial 自己的命名管道后端，按 hyprial 的端点文件格式连接（`kernel/impl/manysys/ipc/windows.py:46-51`）。hc 在 Windows 上用什么传输没有确认。所以按 R12 写出的 hc 客户端在 Windows 上没有能用的传输；而 W020 不允许基线条目，不能绕过入口。 | POSIX 上按 R12 实现。Windows 上交互启动会在连接 core 时失败并给出原因，符合 R20「不保证可用、不预先拒绝」，但「新代码按跨平台写」在这一处做不到。要做到需先确认 hc 的 Windows 传输，再扩 kernel 入口。 |
| C3 | R17 | 本机 IPC 调用者被归为 agent 还是操作者，其中一条判据是「对端 pid 与出生标记匹配某个登记过进程的交互会话」（`ipc/router.py:229-289`，今天实际只有 codex 的 app-server 能匹配）。R17 删除 pid 与出生栅栏和会话表，这条判据随之消失，带来的是权限判定的变化而不只是清理。 | 【提议 P29】改为与主管内存里成员终端的进程号比对（hc 的终端记录里有）。这比今天少一项：今天还比对出生标记以排除 pid 复用，hc 的记录里没有出生标记；可由 daemon 在拿到进程号时自己读一次出生标记补上。需负责人确认，因为这是身份绑定路径。 |
| C4 | R15 | 策略到启动参数的映射今天只有 claude（`cells/reconcile/policy.py:41`）；别的 harness 成员声明了策略会被拒绝启动。能力声明里交互的 claude、pi、codex 都把权限设置标为不支持（`harnesses/capabilities.py:117`、`:168`、`:210`）。所以「策略同样作用于交互成员」今天只可能对 claude 成立，且依赖 U2。 | claude 交互成员按 R15 实现。pi、codex 的交互成员若声明了策略，与它们的无头成员一样被拒绝启动（与 R24 的「同规则」一致）。若负责人的意思是三个 harness 都要生效，那是新增映射，不在现有代码里。 |
| C5 | R22 | 「旧版本目录在没有该版本的 holder 之后删除」需要知道每个 holder 的版本。hc 的状态接口对 holder 只给 pid、实例、是否与 core 同版本、终端数，没有版本号。 | P15：以「所有 holder 都与 core 同版本且 core 已是固定版本」为删除条件。比裁决的字面更保守：有多个旧版本并存时会晚删，不会错删。 |
| C6 | R21 | 模型连接那串 PR 在基线提交时一个都没有合入。按 provider / Magpie 线当天的设计稿（PR #97），其中 #84、#85、#86 不会按原样合入，而是由该线另开的 PR 取代。「这串 PR 先进 `dev`」因此有两种读法：前六个合入即可，或者要等取代它们的新 PR。后者很可能同样改启动路径（该线要让启动不再下发密钥）。 | 按 R21 的后备方案安排：S1 到 S3 先做。S4 何时可以开工，取决于负责人对「这串 PR」范围的确认。 |
| C7 | R9 | 今天 daemon 停止时对所有 harness 调停止（`application/shutdown.py:801-811`）；每个启动的进程都登记进孤儿进程托管，供之后的 daemon 回收（`harnesses/actor/io_port.py:149-154`）。交互成员若不从这两处豁免，有序停止会结束 TUI，或下一个 daemon 把它当孤儿收走。 | 不是做不到，而是 R9 能否成立取决于这两处改对；已写进 4.3，并作为 S5 的必测项。连同 U8 一起，是 R9 风险最集中的地方。 |

### 12.4 相关的未关闭 issue

| issue | 内容 | 是否属于本线 |
| --- | --- | --- |
| #2 | tmux 会话名闸门认的是 tmux 的账本而不是进程，同名重启有窗口。 | 它指向的代码已删除。它的成对验收要求由本线承接（5.2、10.2）；本线完成后可关闭。 |
| #23 | 交互 Codex 冷启动失败：交互路径组装启动输入的方式与无头不一致，校验器只认无头的那一种。修复方向是两种模式共用一个启动输入构造器。 | 部分属于。「共用启动输入构造」正是裁决 R6 的共用启动规格，由 S8 承接。其中自定义厂商的两种失败在第一版不可达（裁决 R11 只允许原生 Auth）。原生目录清单漏掉的锁文件、配置漂移、厂商字段不更新这几项属于 provider 与原生 home，不属于本线。提到 tmux 的那一条随旧代码消失。 |
| #44 | 向交互会话推送消息的路径没有日志：看不到「通知已发给会话」「会话已确认」；另有一个错误事件缺少对象字段。 | 不属于。推送路径的事件归 cell / conversation 的投递线。本线只保证新增的成员运行事实（终端状态、阻塞、退出分类、重拉）在日志里带 `(agent_id, member)`，不再只有 actor。 |
