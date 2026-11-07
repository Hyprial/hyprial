# 设计：agent 可迁移运输单元

- 日期：2026-09-25；状态：设计提案，不授权实施，不含产品代码。
- 代码基线：`dev@df1beae86c99244cc967bab38637c7769c028d01`；本分支起点 `c58b54887810c6171250ac952f8ca397f7d6b8ce`。
- 上游：`docs/design/design-agent-home-dirs.md`、`docs/design/design-transfer-container.md`、#742/#761（P2）、#793（托管 guest MVP）、#815（迁移入口，基线时未合）。

## 0. 裁决与设计目标

Allen 依次说：

> 请优先从可迁移的角度去思考我们需要的隔离环境，我们实际上更接近container的原意：用来运输的集装箱/托盘

> 另外，sandbox的问题还要考虑，如果agent运行在sandbox中，其中的harness再启动sandbox是否会受到影响？

> 实际上我更倾向外层（我们的封装）只负责打包、运输和映射，安全仍有原来的harness负责，不要混淆或者重复harness的工作

**第三句是已裁决的分工**：外层只打包、运输、映射与恢复；命令安全、沙箱和审批归各 harness。外层不施加 Seatbelt/bubblewrap 沙箱，也不以 Docker 的默认隔离替代 harness 的安全承诺。候选技术先按能否完整运送 §2 的箱子评估。任何落地环境必须保留 harness 自己的沙箱；拆箱时的沙箱冒烟失败就拒绝启动，不悄悄改成无沙箱；不在已测矩阵内只发 warning（§4）。

**推荐**：定义版本化、内容寻址的运输单元；在 macOS、Linux、WSL2 上以本机目录恢复为通用路径，Docker 只是可选落地点，同样适用 §4 的两层沙箱保证。凭据仅用收件方密钥密封传输，在目标机受控展开；明文不进入可复制的箱体、镜像、argv 或环境变量。现有 P2 与 transfer 一代提供材料和传输通道，但尚未实现本设计。

## 1. 当前状态与真实边界

### 1.1 P2 agent home

`home.py:31-33` 建立 `H/agents/<actor>/{config,secrets,state,workspace}`。P2 仅在显式 C 且 harness 为 `claude|codex|pi` 时激活；容器启动被显式拒绝（`agents/runtime.py:240-251`）。`agent create --config` 可选（`cli.py:6249-6253`）。`AgentRuntimeContext.public_projection()` 返回路径、环境和认证元数据（`runtime.py:207-226`）；**没有 `runtime_summary()`，也没有容器边界描述或安全环境白名单**。

当前有效 C 可以在 home 之外：`agents/config.py:472-496` 只检查冲突/重叠；有效 cwd 也可在外部（`agents/registry.py:1388-1417`、`cli.py:6248`）。所以“搬 home 就搬完所有权威数据”尚不成立。**提议的新规**：新建 agent 的 C 和默认 workspace 应落入 home；存量外部 C/cwd 在打包前逐项分类，快照进箱并改写映射，或声明可验证的外部依赖并在目标机物化。缺失、无法快照、路径别名/符号链接逃逸、未获授权的外部依赖一律停止迁移；不猜测目标路径。原路径在提交后仍留作回滚源，直到完成清理裁决。

生产宿主“285 个 agent、0 个显式 C”只是 hq-adjutant 的**未重核**旧读数，本文与独立审查均未读生产 registry。代码仅证明激活条件，不能推出当前生产激活数为零。M1 的只读清点须重新给出时间、registry 版本、按 harness/C/cwd 分类的计数。

### 1.2 transfer 一代、二代与入口

一代以身份无关镜像、`docker save`、临时凭据 staging→per-worker volume、tmpfs HOME、固定 owner 身份做冷迁（`transfer/container.py:1-29`；`docs/design/design-transfer-container.md:13-18` 的 D-A～D-D）。现有 `docker_run_argv` 固定 `HOME=/home/hyprial`，只挂 legacy session/credential volume、cwd 和 daemon state，并按受限键传环境（`transfer/container.py:225-265,298-325`）；它**不能原样承接 P2**。旧设计的“去掉凭据挂载后 401/403”仍是真实供应商身份下的未来验收，不是当前测试已证明的结果（`docs/design/design-transfer-container.md:218`）。

二代 #793 是赶一天进度交付的 **MVP**：无自有 host 的 guest 直接由 host 托管。org-improver 2026-09-25 指出四项推迟且未再向 Allen 呈报：跨机迁移、容器隔离、take-away/清理、可见性过滤；该线目前暂停。`agents/grants.py:1` 说记录 grant 不执行授权；`cli.py:6336` 的准确文字是 “Recording does not enforce runtime permissions”，意思一致，字面不同。`isolation: directory|container` 是 ledger 词表，不是执行证明（`agents/grants.py:36-38`）。#815 提供 preflight/execute/rollback 入口；不能把入口存在等同于可运送单元已建成。

| 二代延期项 | 本设计关系 | 仍需交付的验收 |
|---|---|---|
| 跨机迁移 | §2 箱体、§5 目标机恢复覆盖数据路径 | 两机真实 turn、原 owner 身份、失败回滚 |
| 容器隔离 | Docker 可作为解包目标；不承担 harness 安全 | 每个 harness 在该落地方式上的兼容矩阵与拆箱冒烟（§4）；Pi/Kimi 缺口见 §4 |
| take-away 与清理 | 箱体移交、epoch fencing、收据和撤销步骤覆盖协议 | guest 离开后源/目标活跃实例、临时卷、密封包的清理证明 |
| 可见性过滤 | **未被箱体设计涵盖** | 单独的 registry/查询授权设计与测试；本线仍暂停 |

## 2. 运输单元：装什么、如何封箱

定义 `agent-transport/v1`：一个规范化 `manifest.json`、在源端私有 staging 中按相对路径排列的 `payload.tar.zst`、传输时的 `payload.envelope`、单独的 `credentials.envelope`、签名/哈希收据。**整个 payload 也加密**，因为会话和工作区可能含敏感文本。manifest 载明 schema、actor/owner、源与目标 machine、registry incarnation/epoch、harness 与版本需求、C 来源及 revision、workspace 来源及映射、P2 root 映射、会话标识、平台/架构、外部依赖、文件列表及大小/SHA-256、工具链声明、两个密文包的哈希和收件方 key ID；不载 bearer 值或密钥。tar 路径必须相对、规范化、无向外链接、无设备文件；包内只有 agent 的配置快照、workspace 快照、会话与工具状态，不复制宿主 HOME、Keychain、进程内存或平台二进制。签名绑定 manifest 与两个密文包哈希；目标先验证签名、epoch、owner、密文哈希与目标未占用，解密后再验证文件哈希、大小上限和路径，才允许原子展开。明文 staging 只在受控私有目录短暂存在，清理须有收据。格式是**提案**，不是现有实现或已决定的加密协议。（2026-10-11 补注：具体签名/密钥协议**已定稿并落地** —— 实现为 v1 原型（`signature.py` 仍自述 prototype、契约仍是 C2 draft），此处"定稿"指**方案已定**，⛔ 不是"格式已冻结"；且它不是由 Q8 裁决的 —— Q8 裁的是运输格式，本 Q 表未列签名项。落地形态：签名密钥 = 导出机自身 Ed25519 key（⛔ 非 tailcat 设备身份，密钥文件不进 bundle）；保管 = 目标机 `<home>/secrets/transfer/trusted-source-keys.json`（0700/0600）；轮换 = `transfer-trust-source-retire` 的记录式退休（不删行）加 store 的 `retirementPolicy`；验证方 = 目标机自己的离线信任源（缺 store 即空 store，⛔ 非通配）。详见 `contract/daemon-lifecycle/p2-source-custody.md`「Attributing a received bundle」，随 AT06 #399 的 #1369 落地。）

封箱顺序：在 turn 边界 quiesce 并冻结该 incarnation 的写者 → 清点 home 加外部 C/cwd → 对外部内容做受授权的稳定快照 → provider 凭据刷新/修复完成并刷盘 → 将 credential 文件单独用目标机器公钥做认证加密 → 生成内容哈希/签名 → 搬运。源目录和原注册仍可回滚；目标只能在校验、映射、§4 的矩阵比对与沙箱冒烟，以及真实 resume 验收之后，才 CAS 提交为活跃。其中矩阵比对不命中只发 warning、不算失败；校验、映射、沙箱冒烟或 resume 验收任一失败，都恢复源 incarnation，不让两个端同时写同一会话/凭据。删除 staging 和临时卷必须有收据；销毁源密封数据需独立生命周期决策。

**凭据处理**：P2 的 `native_root`、XDG 配置、tool profile、workspace 或 transcript 都可能含敏感值；封箱前按 provider 清单识别认证叶文件，列入独立 credential envelope，未知认证来源则 fail closed。其余 payload 也加密；目标私有目录/临时卷只在验签且确认收件方身份后展开。凭据不能从源 Keychain、host HOME、宿主 API key 环境或旧 volume 静默回退。认证失败类型、401/403 和刷新能力须在真实帐号下单独验收；密封包和目录权限只证明保管条件，不证明供应商认证成功。`runtime.py:262` 仅定义路径；目录所有权/0700 检查在 `runtime.py:400+`，私有文件 0600 创建在 `runtime.py:425+`，Claude 凭据文件 0600 检查在 `harnesses/claude_runtime.py:137-151`。

## 3. 先看搬运能力，再看落地点

| 技术 | 能否装运 §2 箱体 | macOS / Linux / WSL2 恢复 | 本设计位置 |
|---|---|---|---|
| Docker/OCI 镜像 | 镜像可装工具；`docker save` 可搬镜像；箱体仍须单独校验/挂载 | macOS 经 Linux VM，Linux 原生，Windows 仅 WSL2 路径 | 可选解包目标；适用 §4 的矩阵与冒烟 |
| rr | trace 可 `rr pack` 搬去重放；不承载新输入、活进程续跑或完整 agent 箱体 | Linux；macOS 不可直接运行，WSL2 未验 | 调试工具，不作运输基础 |
| CRIU | 可检查点 Linux 进程；依赖内核/资源兼容，不能替代配置/凭据/工作区清单 | Linux，Mac/WSL2 仅 Linux guest 且须实测 | 温迁研究项，不在冷迁主线 |
| Nix | `nix copy` 搬目标平台的工具依赖 closure，不搬会话/工作区/凭据 | Darwin/Linux 各自构建，WSL2 走 Linux；跨平台需重建 | 可选工具链声明/镜像构建，不作箱体 |
| Seatbelt / bubblewrap | 不提供持久箱体或跨机恢复 | 各为平台内命令沙箱 | **拒绝用作外层边界**；由 harness 自己使用 |
| 规范化包 + 本机目录映射 | 完整承载文件、身份与环境声明；密封凭据另包 | 三种目标均可按路径/平台重投影 | **推荐的通用运输单元** |

### rr 的精确用途

[O'Callahan 等，*Engineering Record And Replay For Deployability*，arXiv:1705.05937](https://arxiv.org/pdf/1705.05937) §2.3.2 说录制时多数 syscall 正常执行，因此在 rr 录制中运行 `rm -rf` 仍会真实删除可访问的宿主文件；§2.1 说重放时不执行文件系统操作。§2.3.4 明确假定 tracee 不是恶意的，还描述与沙箱交互的开口。论文报告重要低并行工作负载录制/重放放慢低于 2 倍，且高度并行工作负载可能放慢很多；这是论文的工作负载结果，不是本仓实测。[rr 项目](https://rr-project.org/)与[系统要求](https://github.com/rr-debugger/rr#system-requirements)说明 Linux/硬件计数器前提；VM 计数器支持随配置而变。此前“Apple Virtualization.framework 通常没有可用 PMU”未经验证，本设计不据此下结论。rr trace 是调试记录，不是可重新接受新工作的 agent。

CRIU 的 [checkpoint/restore](https://criu.org/Checkpoint/Restore)、[TCP 条件](https://www.criu.org/TCP_connection)及 [Docker checkpoint 的 experimental 状态](https://docs.docker.com/reference/cli/docker/checkpoint/)均已在线复核；三 OS 冷迁仍需 §2 的文件与身份协议。Nix 的 [flake](https://nix.dev/concepts/flakes.html)、[copy](https://nixos.org/manual/nix/stable/command-ref/new-cli/nix3-copy.html)、[平台安装](https://nix.dev/install-nix)、[dockerTools](https://nixos.org/manual/nixpkgs/stable/#sec-pkgs-dockerTools)已在线复核；本机未发现 `nix` 的先前只读探针不等于所有安装路径不存在，构建和 agent turn 均未运行。

## 4. 嵌套沙箱：目标机启动闸

macOS：Codex [文档](https://learn.chatgpt.com/docs/agent-approvals-security)说明用 Seatbelt；Claude Code [文档](https://code.claude.com/docs/en/sandboxing)说明 sandboxed Bash 在 macOS 用 Seatbelt。hq-adjutant 在 Darwin 25.5 的外层 allow-default Seatbelt 中再启动带 `/tmp/nest-deny` 写拒绝的内层 profile，得到 `sandbox_apply: Operation not permitted`，内层命令未执行；内层也为 allow-default 的对照 rc=0。**本 worker 重跑**同一嵌套命令得到 rc=71、同一错误，但连 allow-default 对照也 rc=71；当前托管沙箱已阻止创建外层 Seatbelt，故这个重跑不能独立验证两种内层的差异。外部重跑请求被自动审批拒绝（§8）。设计据 hq-adjutant 的对照探针拒绝外层 Seatbelt，并在正式实施前把裸机/嵌套对照纳入可复现测试。

Linux/WSL2：Codex [安全文档](https://learn.chatgpt.com/docs/agent-approvals-security)指出 Docker 内若 namespace、setuid bwrap 或 seccomp 操作被禁，其沙箱可能不能工作；[codex-rs Linux sandbox README](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/README.md)说明 bwrap 用户命名空间失败会警告，WSL1 不支持。Docker [默认 seccomp 文档](https://docs.docker.com/engine/security/seccomp/)将一般 namespace clone 列为受限，并明确 `CLONE_NEWUSER` 例外；[实际默认 profile](https://github.com/moby/moby/blob/master/vendor/github.com/moby/profiles/seccomp/default.json)还按 capability/参数约束 `clone`、`unshare`，并让非特权 `clone3` 返回 ENOSYS。故不能笼统说“Docker 默认一律阻止用户命名空间”，也不能因此保证 bwrap 可用。bubblewrap [手册](https://github.com/containers/bubblewrap/blob/main/bwrap.xml)允许嵌套 user namespace，但 `--disable-userns` 会阻止更深层创建；[上游嵌套讨论](https://github.com/containers/bubblewrap/issues/164)也表明其依赖内核与外层配置。所以常用的镜像与宿主组合应当进入 §4 的发布前矩阵实测；未测的组合按 §4 只发 warning，不拒绝。

**沙箱保证分两层：发布前的兼容矩阵，拆箱时的轻量核对**（Allen 2026-09-26：「不在矩阵内warning即可，harness的更新速度太快，矩阵几乎不可能跟上，但行为不会总是发生剧烈变化，有问题了再说」）。

1. **发布前（CI）兼容矩阵**：按 harness 版本 × 系统（macOS / Linux / WSL2）× 落地方式（本机目录 / Docker / VM）建矩阵，**走真实 worker 执行路径**（例如 codex 走 `app-server`，沙箱与审批策略经线程字段传入），使用 CI 专用测试凭据跑真实回合，证明 workspace 外的写入被拒、workspace 内的写入成功。矩阵要记录被测的 harness 版本与配置指纹，作为拆箱时比对的依据。矩阵跟不上 harness 的更新是预期内的事，不追求全覆盖。
2. **拆箱时轻量核对**：
   - **比对矩阵**：目标机上的 harness 版本、系统、落地方式和配置指纹是否落在已测矩阵内。**不在矩阵内只发 warning**，写明差在哪一项，照常启动，不拒绝。
   - **沙箱冒烟**：用 harness 自己的沙箱执行入口（如 `codex sandbox`），不调模型、不用凭据，花几秒确认两件事：这台机器上沙箱确实生效，工作区外的写入被拒；工作区内的写入成功。**冒烟明确失败就拒绝启动**，因为这是已经观察到的问题，不是"未知"；报错写明失败的步骤和错误类别。
   - **残余风险（明说）**：冒烟只证明 harness 的独立沙箱入口在这台机器上生效，**不证明实际 worker 的执行路径**（如 codex `app-server` 的线程、回合与 resume 策略）同样执行了沙箱和审批。对矩阵外的目标，可能出现冒烟通过、而 worker 路径没有执行沙箱策略的情况，准入后越界写入可能成功。这是按 Allen 的裁定（矩阵外只 warning）接受的风险，warning 文案里要写明"此组合未经真实 worker 路径验证"；发现问题后，把该组合补进矩阵。
3. **不变的约束**：运行时任何形式的沙箱关闭（`danger-full-access`、unsandboxed retry 等）都属于禁止的隐式降级。只检查 bwrap/Seatbelt 可执行文件是否存在或运行 `true` 不算冒烟。审批请求仍按 harness 既有的策略工作。macOS 原生落地同样要做这两步。

**harness 缺口**：本仓 `src/hyprial/harnesses` 中 Codex 有显式 `--sandbox`/approval 传递（`codex.py:1399-1480`），Claude 通过 SDK 调 Claude Code；其沙箱可用性按上文的矩阵与冒烟判断。Pi 适配器 `pi.py`/`pi_loader.py` 没有可证的内建 OS 沙箱或每命令审批；Pi [上游安全文档](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/security.md)说明默认以启动用户权限运行，缺少内建沙箱。**Kimi 在本仓是 Pi 的 `kimi-coding` provider**（`dispatch/matrix.py:66`），不是 `harnesses/kimi.py` 独立 harness，因此继承 Pi 缺口。Allen 需裁决这类 harness 在本分工下的准入/暂缓；本文不以外层沙箱填空。自动迁移/新建默认启用不得越过该闸。

## 5. 解包、路径与凭据映射

目标先在私有 staging 展开并验证 §2 收据，建立目标 home 和外部 workspace 的映射，再由**目标端**解析 `AgentRuntimeContext`、生成 provider 投影和 tool profile；签名的 manifest 只存逻辑相对根与原路径对应关系，不把源 host 的绝对 `environment()` 字符串当容器变量。`public_projection()` 用来诊断/对照，不授新权限。所有路径须经过 realpath、所有权、模式、无重叠/逃逸检查。macOS/Linux 本机以目标绝对路径重新生成环境；WSL2 在 Linux 文件系统内恢复，Windows 宿主路径需要显式映射，禁止默默把 `C:\\...` 当 POSIX 路径。不同 OS 的工具二进制、Nix closure、SSH agent socket 不从源机照搬；按目标平台重新安装/授权。会话如含绝对 cwd，必须由 harness 级 locator/严格 resume 证实可映射，或保留经验证的同路径；否则停止迁移。

- macOS：在目标机器私有 H 下恢复，按 Darwin 平台重建 tool profile/native projection，逐个复核 Keychain 之外的认证来源；做 §4 的矩阵比对与沙箱冒烟后 resume。
- Linux：在目标 H 下恢复；若选 Docker，再按下表 bind 到固定容器路径，做 §4 的矩阵比对与沙箱冒烟后 resume。
- WSL2：在发行版的 Linux 文件系统内恢复 H 和 workspace；按 WSL2/Linux harness 与 socket 路径重建，拒绝 WSL1，做 §4 的矩阵比对与沙箱冒烟后 resume。

容器方案提议固定目标前缀 `/hyprial/agent`，目录结构保留相对 home 布局。以下均来自 `AgentRuntimeContext.roots`（`agents/runtime.py:259-340`），不是从 cwd/HOME/actor URI 推算：

| 逻辑内容/源 root | 容器目标与访问方式 | 环境/用途 |
|---|---|---|
| `config_source`，含外部 C 的快照 | `/hyprial/agent/config/source` ro | 目标重建投影的输入 |
| `projection_root` | `/hyprial/agent/state/config/<entity>/<rev>/native/<harness>` ro | 投影收据验证；native adapter 消费 |
| `native_root` 非凭据运行状态 | `/hyprial/agent/secrets/native/<harness>` rw | `CLAUDE_CONFIG_DIR` / `CODEX_HOME` / `PI_CODING_AGENT_DIR`；见下文凭据叶挂载 |
| `session_root` | `/hyprial/agent/state/sessions/<harness>` 或 `/hyprial/agent/state/pi` rw | strict resume 与 transcript |
| `tool_home` | `/hyprial/agent/state/home` rw | `HOME`；不传源 host HOME |
| `xdg_config_home` | `/hyprial/agent/secrets/tools/xdg` rw | `XDG_CONFIG_HOME` |
| `xdg_cache_home` | `/hyprial/agent/state/xdg/cache` rw | `XDG_CACHE_HOME` |
| `xdg_data_home` | `/hyprial/agent/state/xdg/data` rw | `XDG_DATA_HOME` |
| `xdg_state_home` | `/hyprial/agent/state/xdg/state` rw | `XDG_STATE_HOME` |
| `tool_profile_root` | `/hyprial/agent/state/config/<entity>/<rev>/tools/<profile>` rw/受验证 | `GIT_CONFIG_GLOBAL`、`GIT_SSH_COMMAND` 中的配置路径需重写；SSH socket/identity 单独目标授权 |
| `workspace` 或显式外部 cwd 快照 | `/hyprial/workspace` rw | `-w`、harness cwd；外部依赖必须声明/物化 |

此表是**拟议映射**，非现有 `docker_run_argv`。目标先建立不含 credential 叶的 native 运行状态视图；它可以由 home 中的非凭据文件重建，但**不得直接把仍含 credential 叶的源 native_root 整根 bind 到容器**。对 `context.environment()` 逐键翻译：`HOME`、四个 `XDG_*`、三选一的 provider root、`GIT_CONFIG_GLOBAL` 与 `GIT_SSH_COMMAND` 内的配置文件路径按表替换为容器绝对路径；`SSH_AUTH_SOCK`、identity/known-hosts 路径仅在目标重新授权并建立映射后传入；姓名、邮箱、profile ID 等非路径选择器沿用；未知路径或敏感键拒绝。实现必须替换 legacy `session_mount`、`cred_volume` 及固定 `HOME=/home/hyprial` 的假设（`transfer/container.py:225-265,298-325`）；不能一边 tmpfs 覆盖 `/home/hyprial` 一边假设 P2 的 HOME 位于那里。新 wrapper 只能把目标端生成的已知路径/非秘密选择器经受限环境传入；现有 `CONTAINER_ENV_PREFIXES` 不允许全部 P2 键，需显式扩展后逐项测试。代理环境只按现有白名单传**名字**，API key/bearer 值不得经 `-e`、argv 或镜像输入；目标 host HOME/Keychain 不挂载。每个 harness 必须证明 native loader、会话链接、profile、cwd、HOME/XDG 与源无关，并通过 §4 的拆箱沙箱冒烟，才可宣称 C1 落地。

`native_root` **不能整根 ro**：Claude 会替换投影链接并建立 `projects` 链接（`harnesses/claude_runtime.py:73-94`）；Codex 会写投影文件并建立 `sessions` 链接（`harnesses/codex.py:356-371`）；Pi auth repair 写 `native_root/auth.json`（`provider_auth/coordinator.py:285-289`）。提议由目标 provider adapter 独占写其 native 运行状态，session 写者独占 session root；credential 叶文件（至少 Claude `.credentials.json`、Codex `auth.json`/兼容凭据、Pi `auth.json`，以目标版本清单为准）在容器运行时由 per-worker 临时 volume 可写挂入，凭据以目标 home 的 `secrets/native` 为**停机/运输保管源**，两者不能同时充当活跃权威。启动前从目标私有密文展开到临时卷；运行中 provider 刷新/repair 写卷；quiesce 时在 epoch 锁下停止写者、把刷新结果原子同步回私有 home、校验模式/摘要，再封入下一箱。同步失败拒绝迁出、退役或回滚。回滚必须先 fence 目标写者、回灌最新已确认凭据版本，再启用源；不可用旧密文覆盖已刷新的 token。D-A 的临时卷在 worker 退役时清除，不能因 P2 home 存在就直接退役。若实现无法把 credential 叶与 native 状态分离，C2 保持阻塞，不允许把整根 rw 映射后声称“摘 credential 挂载”可证明无回退。

**负控制验收**：真实帐号的隔离 fixture 上，先证明挂载凭据可成功认证；再仅移除 credential 叶挂载，保持其它配置/会话/网络完全相同，确认无 host HOME、Keychain、API-key 环境、别处 token 副本，供应商调用失败（预期 401/403 或明确的本地缺凭据错误，记录实际结果）；恢复挂载再成功。此为未来 M3 验收，尚未运行。provider refresh/repair 与 rollback 必须另做正负对照。

## 6. 新建与存量迁移

拟议：`agent create` 默认产生最小显式 C，使支持的 harness 激活 P2；外部 C/cwd 必须走 §1.1 的归箱规则。默认落地为本机映射，容器是目标机可选解包位置，选择时须过 §4 闸；不把 `host-invite` 的 grant 当安全执行。Pi/Kimi 在 Allen 裁决前不自动进入要求 harness 沙箱的可迁移默认路径。现有无 C agent、residents/DSH 保持逐项清点与分类（#815 的 `migrate preflight`），不能假设统一迁移。

历史 285/0 计数未重核。建议先做 3 个非驻留试点，按 harness 分波；每批做 #815 的 `migrate preflight`（清点与分类）、一个真实 turn、严格 resume、回滚演练和批间观察。Allen 于 2026-09-29 裁定允许常驻 agent 迁移；批次选择见 Q6，任何一批失败即停线。迁移和解包目标变更不在同批叠加。

常驻 agent 的迁移顺序是：`hyprial routine pause <name>` → `hyprial agent stop` → `hyprial agent home migrate preflight` → `hyprial agent home migrate execute` → 启动 → 一个真实 turn → `hyprial routine resume <name>`；rollback 仅将 execute 替换为 rollback，顺序相同。

## 7. 阶段与 Allen 决策

| 阶段 | 工作 | 验收与回滚 |
|---|---|---|
| M0 | 裁定箱体格式、C/cwd 外部依赖、Pi 准入 | 文档决议；未裁不改默认 |
| M1 | #815 入口与只读清点 | 隔离 HOME 上 preflight/execute/rollback；重新测量存量数 |
| M2 | 封箱/验签/三 OS 本机恢复 | 跨机冷迁、恶意路径拒绝、原 owner/epoch fencing、失败回源 |
| M3 | P2 环境映射、可选 Docker 解包、C2 双向凭据同步 | 为每个获准 harness 建立 §4 的 CI 兼容矩阵（真实 worker 路径 + 测试凭据）、实现拆箱冒烟、做 §5 凭据负控制；Pi/Kimi 待 Q9；失败保持本机路径 |
| M4 | 逐批存量迁移和 guest take-away | §6 批次/观察、每批回滚；可见性过滤仍另立项 |
| M-N | 可选 Nix 工具链探针 | 非生产 Mac/Linux 各构建，目标平台 closure，凭据不入 store；失败撤销 opt-in |
| M-R | 可选 rr/CRIU 研究 | Linux 调试或温迁独立探针，不影响 M2/M3 |

**已决定**：Allen 第三句规定外层只负责打包、运输、映射，安全由 harness 的沙箱/审批承担；不再提供“外层 Seatbelt/bwrap”选项。第二句的嵌套问题由 §4 的两层保证处理（发布前矩阵 + 拆箱冒烟）；矩阵外的目标只有冒烟这一层，§4 写明了其残余风险。第一句的“集装箱/托盘”由 §2 的箱体定义实现。

| # | 待决问题 | 可执行选项 | 推荐 |
|---|---|---|---|
| Q1 | rr 用途 | A. Linux 调试工具 / B. 另立 rr 续跑研究（需证明可接受新输入）/ C. 不采用 | A |
| Q2 | 新建 agent 默认显式 C？ | A. 是，外部依赖必须归箱 / B. 仅 opt-in | A，但 Pi/Kimi 按 Q9 闸住 |
| Q3 | 旧 D-B“全容器化、不留 Mac 宿主”如何与本裁决衔接？ | A. 修订为容器可选落地点 / B. 单独规划迁走 Mac 宿主 | A |
| Q4 | residents | A. G-R 前冻结 / B. 逐个纳入试点 | A |
| Q5 | 运行中凭据 volume | A. 保留 D-A 临时卷，停机/迁移 home 保管，epoch 同步（§5）/ B. 直接 rw 挂 home（须另证 credential 叶撤除和回退阻断） | A；不提前退役 volume |
| Q6 | 存量节奏 | A. ≤20/批，批间 ≥24 小时 / B. ≤40/批，批间 ≥12 小时 / C. ≤5/批，批间 ≥72 小时 | A；先 3 个试点 |
| Q7 | Nix | A. 非生产探针后 opt-in / B. 不引入 / C. 全宿主默认（需安装与跨平台验收） | A |
| Q8 | 运输格式与定义 | A. §2 的版本化 manifest + 内容寻址的加密 tar.zst payload + 单独收件方密封凭据 / B. OCI artifact（仍需相同 manifest/凭据协议）/ C. 只传目录（缺少独立收据，须另补） | A；先做跨平台 fixture |
| Q9 | 无自有沙箱的 Pi/Kimi | A. 暂缓其自动迁移/启动，待 harness 提供可证安全能力或 Allen 给出单独准入政策 / B. 仅允许明确指定的受信本机使用，不计入自动可迁移承诺 | A；不加外层沙箱 |

## 8. 证据边界

- **代码**：以上 `file:line` 以 `dev@df1beae8` 及 #820 独立审查为基线；#815 入口来自当时未合分支 `feat/agent-migrate-entry@9567d81c`。箱体、映射、凭据双向同步、兼容矩阵与沙箱冒烟均为**提案/未实现**。
- **生产**：285/0 只是一条未重核 hq-adjutant 读数；没有访问生产 `~/.hyprial`、生产 daemon 或真实凭据。真实认证 401/403、两机恢复、Docker 内 harness 沙箱、WSL2、Nix 构建、rr/CRIU 执行均 **NOT_RUN**。
- **在线资料**：#820 独立审查已在线核对 [rr](https://rr-project.org/)、[CRIU](https://criu.org/Checkpoint/Restore)、[Nix](https://nix.dev/install-nix) 和 [Docker Mac VM](https://docs.docker.com/desktop/troubleshoot-and-support/faqs/macfaqs/)；本轮进一步核对 rr 论文、Docker seccomp、Codex/Claude/Pi/bubblewrap 一手资料（见 §3–§4 链接）。这些证明文档化能力/限制，不证明本仓验收通过。Apple VM PMU 推断保留为未知。
- **Seatbelt 探针**：hq-adjutant 提供的 Darwin 25.5 外层 allow-default/内层 deny 对照为外部证据；本轮在已受限沙箱内两条都失败，不能独立复核差异。要求的外部重跑遭自动审批拒绝。正式验收需由可运行该对照的授权环境重复。
