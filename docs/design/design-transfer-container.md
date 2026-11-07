# 设计:hyprial transfer 容器封装 —— 凭据随迁的容器化冷迁移(段 1)

- 日期:2026-08-24(段 1);2026-08-24 定案更新(段 2 开工)
- 状态:**段 1 已过目,四决策点全部定案(见 §0),段 2 实现开工**;本文为定案后的设计定稿
- 输入:Allen 的设计意图 —— P0 冷迁移不迁凭据与其意图不符;容器封装要做到 worker 装在 docker 容器里、**携带原 owner 凭据、落地后仍以原 owner 身份和额度运行**;首个真实用例 = devusr(`agent:amyws:devusr-dev:*`)的 worker 推到 node-hq-01
- 上游:`notes/design-hyprial-transfer.md`(P0 设计,§5 把容器化排在 P3——Allen 决定提前);`src/hyprial/transfer/`(P0 实现,#227);`hq/notes/pac-transfer-two-machine.yaml`(双机验收口径)
- 勘误:任务书所引 **#45 在仓内是防火墙文档 PR**,与身份无关;"身份现算"问题实体 = owner/machine 段从 daemon 环境现铸(`identity.py:84-97` `resolve_node_owner`、`application.py:498` node_id),devusr→hq 按现状会把 owner 段静默从 `amyws` 改成 `bobadmin`。本文按此实体展开

---

## 0. 决策定案(2026-08-24,两条 Allen 亲裁、两条委托按提议定)

| # | 决策点 | 定案 | 影响章节 |
|---|---|---|---|
| D-A | "容器停止后凭据不残留宿主"的解释 | **解释 A(委托定)**:存活期凭据留 docker volume(daemon 重启可重拉容器);退役(down/destroy/再迁出)即 `docker volume rm`。字面解释 B 不采用 | §2.4 |
| D-B | 备选方案(凭据束+隔离 HOME) | **已裁弃(Allen 亲裁)**:「容器化后没必要保留 macOS 宿主」—— 放弃 macOS 降级路径,目标环境**全容器化**;§6 保留为决策痕迹,不再作为候选 | §6 |
| D-C | 镜像分发 | **`docker save` 二进制链(Allen 亲裁)**:save 出 tar → 走现有 transfer 传输管道(scp,transport-only 纪律不变)→ 目标机 `docker load`;不做 build-on-target,不建 registry | §1.2 |
| D-D | 迁移后 URI | **owner 钉住、machine 随物理节点(委托定)**;迁移报告显式列新旧 URI;workflow/PAC 寻址用逻辑名(存短名不存派生 URI 的既有规约),不受影响 | §3 |

---

## 1. 容器边界

### 1.1 里面装什么 / 外面留什么

**镜像里(身份无关、无密、可复用)**:base OS + node + python/uv + 三家 harness CLI(钉版本)+ hyprial wheel(给 claude worker 的 MCP `agent-channel` stdio server 用,`worker_channel.py` 的 `mcp_server` 在容器内拉起)。**镜像不含凭据、不含 worktree、不含 session 文件、不含任何 owner 信息** —— 同一镜像服务所有 owner。

**每 worker 运行时注入(daemon 在目标机 `docker run -i --rm` 拉起)**:

| 注入物 | 方式 | 说明 |
|---|---|---|
| worktree | rw bind,挂到与目标 cwd **同绝对路径** | rsync 先于容器启动完成(P0 ship 原样复用) |
| session 文件 | rw bind,落容器 HOME 内 harness 期望路径 | P0 定位器/落位规则原样复用(`session_files.py`) |
| 凭据束 | **ro** bind,来自 per-worker docker volume | §2 |
| daemon state dir | rw bind,与宿主**同绝对路径** | 注册回总线的通道,见 §1.3 |
| env | `docker run -e` 显式白名单 | `HYPRIAL_WORKER_*`、`HYPRIAL_HOME`、`HARNESS_STATE_DIR`、**HTTP(S)_PROXY**(PAC 教训:env 白名单剥掉代理与 USER 已咬三次;容器 env 天然是白名单,代理必须显式携带) |

**容器外(宿主)**:hyprial daemon、zenoh mesh、inbox、registry、desired-state 全部留在宿主,不在容器内跑第二个 daemon。容器是 **launcher 层的进程包装**:connector 原 command 被包成 `docker run … <原 command>`,stdio/unix-socket 通道不变,三家 connector 与 supervisor/strict-resume 逻辑零侵入。重启策略归 daemon supervisor,**不用** `docker --restart`(防与 daemon reconcile 双拉)。

### 1.2 镜像分发(D-C 定案:docker save 二进制链)

源机 `docker save` 出 tar → 走 transfer 现有传输管道(scp,transport-only 纪律不变)→ 目标机 `docker load`。**不做 build-on-target,不建 registry**。uv 冷缓存首跑 ~95MB 的 P0 教训(docker 验证报告 #3)由镜像构建期预热吸收(构建时以同一 `UV_CACHE_DIR` 预拉 claude-agent-sdk),不再吃 strict-timeout。镜像 tag = `hyprial-worker:<hyprial 版本>`(版本号 `+` 转 `-`),与仓内代码同源构建(`scripts/build/docker/` 下 Dockerfile + 构建脚本)。

#### 大镜像与目标缓存（ce6f5e64）

quiesce 前用目标已解析的 Docker 路径检查**相同 image 引用/tag**（默认当前版本或 `--image`）。
确认存在则跳过本次 `docker save` 与 `image.tar` 上传，但凭据、session、staging finalize 和
strict receive 仍照常；dry-run/结果的 `imageTransferSkipped` 明示该分支。
检查未命中时仍走原 save/scp/load 链；SSH 探测超时在停源之前报错。此检查不是镜像 digest
一致性证明，且不会取消源 Docker、源镜像等既有前置。

每个 scp 文件的预算为 `max(控制面 timeout, 60s + 文件字节数 / 1MiB/s)`，独立于默认 30s
控制面预算。测试方更正实际 tar 为 544MB、40.4s（旧 30s 的约 1.35 倍），不是 2.3GB/142s
的实测；新预算约 579s（若 MB 指 MiB 则 604s），而非固定改 60s。`ps` 仍是 30s、receive
仍是 300s、save 仍是 1800s。任意网络停滞仍可能超出有限预算并走原回滚；没有实测吞吐或迁移。

#### 源机与目标机的执行前置（2026-09-07，fe535026 / #417）

- **源机也必须有可用的 Docker**（CLI 可执行、能连接 daemon），并已在源机用
  `scripts/build/docker/build-worker-image.sh` 构建 `hyprial-worker:<当前 hyprial 版本>`（`+` 转 `-`）；
  指定 `--image` 时检查该镜像。只在目标机安装 Docker 不够。
- 容器化 claude 默认携带凭据时，源登录 HOME 下的 `~/.claude/.credentials.json`
  必须已是文件。macOS 主凭据通常在 Keychain，导出必须由操作者显式授权
  （**operator-authorized**）完成；preflight 只检查元数据，不读取内容、不自动导出。
  该缺项与两端 Docker / 源镜像缺项一起列入 `data.missing`，不是遇到 Docker 缺项就跳过。
- **源会话文件（91fc57d3）**：两种模式下，已有 `sessionRef` 必须能由原 harness 定位器找到
  可迁文件。即使工具、Docker、镜像或凭据缺失也继续核查，统一列入 `TRANSFER_PREREQUISITE`
  / `data.missing`，不再因其他前置先失败而漏报。缺文件时给出 harness/ref/路径及提示：
  若源 worker 从未跑过，**先跑一个 turn**，产生对应文件后重试；已有历史则检查/恢复 transcript。
  多候选、坏头或目录冒充文件也拒绝，但不误称「从未跑 turn」。定位不改算法，仍在停源前只读执行；
  没有 `sessionRef` 时保留原来的无 session 步骤路径，未新增静默冷迁或伪造会话。
- 构建脚本将 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`（含小写变量）的
  `127.0.0.1`、`localhost`（另含 `[::1]`）代理主机改写为 `host.docker.internal`，
  保留协议、端口、userinfo 与路径；`NO_PROXY` / `no_proxy` 绕过列表保持原样。
  Docker Desktop 提供该主机名；其他/远程 builder 需自行配置可达的宿主映射或取消代理变量，
  此改写不保证代理监听范围、builder 网络或 CDN 可用，也不调整 Docker 凭据存储。
- `UV_PYTHON_INSTALL_MIRROR` / `UV_INDEX_URL` 是可选 build args：不设空默认值，
  两个 uv 安装 RUN 还会 unset 显式传入的空值，避免镜像固定的 uv 0.8.15 把空串解析为 URL。
  非空值原样保留；venv/pip 失败时保留退出码，并提示核查这两个变量名（不回显 URL 值）。
- 目标机必须有 Docker；探测先用非交互 SSH 的 PATH，再检查
  `/usr/local/bin/docker`、`/opt/homebrew/bin/docker`。目标镜像无需预装，仍由源机 save/scp。
- 按 #417，**两种模式均检查目标非交互 SSH PATH 中的 `uv`、`claude`、`pi`**；
  裸机 codex 另需 `codex`。登录 shell 或 `--remote-hyprial` 前缀中的 PATH 不能替代此检查。
- 完成身份 fencing / admission 后、quiesce 之前，preflight 收集上述**所有**缺项或不可核项，
  一次返回 `TRANSFER_PREREQUISITE`，人读文本与 JSON `data.missing` 均列明源/目标。
  源 Docker 缺失或不可达时，镜像标为「缺失或无法检查」，不伪称已经证实镜像不存在。
  dry-run 同样检查；有缺项时不 stop、不 save、不上传。
- 裸机模式不要求两端 Docker、源镜像或上述 claude 凭据；显式 `--no-credentials` 仍跳过凭据要求。
  其他 harness 的凭据束保留后续只读验证及独立错误码；claude 凭据与源 session 定位缺项已并入
  `TRANSFER_PREREQUISITE` 聚合，但元数据通过不等于真实认证或额度已验证。

#### 目标选择与节点 fencing（#417，2026-09-07 B-窄版裁定）

`--to [user@]<nodeId>` 的主机部分必须**精确匹配本 home 的 `hyprial host list` 条目**。
不自行解析 SSH alias、DNS 短名或另查 tailnet；名称不同请使用列表中的完整 nodeId。
SSH 后先读远端 `hyprial daemon status --json` 的 `daemon.nodeId`，与选中条目比较；不一致返回
`TRANSFER_TARGET_MISMATCH`，错误同时列 `hostsNodeId` / `remoteNodeId` 实值，禁止迁移。

**hosts 的真实字段仅为 nodeId/status，没有 owner**。节点匹配后才走既有 P0 规则：
两节点不能同名；裸机远端 owner 必须等于本地 plan 的 owner，仍用
`TRANSFER_OWNER_MISMATCH`；容器模式仍以源 owner 为 pinnedOwner，允许 daemon owner 不同。
不从 agent URI 推断 daemon owner，也不把远端自报 owner 当作 hosts 的 owner。

生产 `--remote-hyprial` 默认值固定为 `hyprial`；非默认字符串仅在源命令有显式、非空、
且不指向默认 `~/.hyprial` 的 `HYPRIAL_HOME` 覆盖时允许，用于隔离演练。
此开关只检查源 home 选择，不推导远端配置，也不证明任意远端命令本身已隔离；
操作者须在演练命令中选择正确远端 HOME。即使覆盖命令，发现列表与节点 fencing 也不能绕过。

#### 跨登录用户的 cwd（7b9da710）

不带 `--cwd` 时，停源前以源进程 UID 对应的 OS 用户名和目标 SSH `id -un` 实值比较。
不同或无法确认时返回 `TRANSFER_CWD_REQUIRED`，给出用户名事实并要求显式 `--cwd`；
不能从源 `/Users/amyws/...` 默认写到另一登录用户的目标家目录。
这不是 hyprial owner 比较；也不从 `daemon.socket` 推 HOME、不猜 SSH 配置用户或进行 HOME 替换。
裸目标名同样通过 `id -un` 核用户；显式 `--cwd` 原样使用，不再额外核登录用户。
同名用户保留 same-path 默认，但用户名相同不保证不同 OS 的家目录布局或路径权限相同，
这类目标也应显式给 `--cwd`（例如 `/Users/bobadmin/hyprial-work/xfer-probe` 或 Linux `/home/...`）。

#### 执行确认（#417.2，B-窄版）

通过节点/owner fencing 后，执行任何 precheck、quiesce 或传输之前，输出：

- `will run <remote_hyprial 实值> on <SSH destination>`（生产默认值就是 `hyprial`，不冒充绝对可执行路径）；
- 远端 `ps` 自报的 `daemon.socket` **原始绝对路径**（state 可在 home 之外，不反推 HYPRIAL_HOME）；
- 将要迁移的源 actor URI。

未带 `--yes` 则返回非零 `TRANSFER_CONFIRMATION_REQUIRED`，说明补确认的方式；
**`--dry-run` 同样要求 `--yes`**，确认后只验证、不变更。缺失/非法 socket 事实也不能执行。
`--json` 的执行前事实/进度写 stderr，stdout 仍是一个 JSON 对象；确认拒绝时 `data`
包含事实，确认后的结果用 `remote` 包含相同事实。没有新增远端 hyprial 探测子命令，
没有推导远端 hyprial 绝对路径或 HYPRIAL_HOME，也未改造前述基础设施 preflight。

### 1.3 容器经什么通道注册回总线

worker 从不自行注册 —— 身份由 daemon 铸造和声明(presence/inbox 键都在 daemon 侧)。worker↔daemon 的三条现行通道在同机容器下全部保活:

- **pi**:`pi --mode rpc` stdio JSON-RPC → `docker run -i` 透传;bridge 扩展经 `HYPRIAL_HOME`/`HARNESS_STATE_DIR` 找 `<state>/daemon.sock`(`pi_harness_bridge.ts:16`)→ state dir 同路径挂载即通;
- **codex**:app-server `--listen unix://<socket>`(`codex.py:553`)→ socket 在 state dir 下,同路径挂载即通;
- **claude**:SDK worker stdio + 容器内拉起 `python -m hyprial.daemon.impl.mcp.entry agent-channel`(镜像内 hyprial wheel 的用途),经挂载的 state dir 到达 daemon sock。

无需任何宿主机网络端口暴露;容器出向网络(NAT)仅供 harness API 调用。

## 2. 🔴 凭据安全

### 2.1 凭据位置(2026-08-24 按当前版本实证,未照抄组织记忆;只核实路径与权限,未读内容)

| harness | 版本 | 凭据文件 | 备注 |
|---|---|---|---|
| claude | 2.1.241 | `~/.claude/.credentials.json`(0600) | ⚠️ **macOS 上主存储是 login Keychain**(实证本机存在 `Claude Code-credentials` 条目,文件同时在);Linux 容器内只认文件 |
| codex | 0.147.0 | `~/.codex/auth.json`(0600),另见 `.credentials.json` | 两文件同现,束内都带,段 2 钉各自角色;`config.toml` 若引 env_key 类 API key 则超出文件束范围(§2.5) |
| pi | 0.83.0 | `~/.pi/agent/auth.json`(0600) | `models-store.json` 非密但随迁 |

### 2.2 凭据怎么进容器:volume 挂载,禁烙镜像

- ⛔ **build 时烙进镜像 = 错误**(镜像层可被 `docker history`/save 读出,且镜像被设计成身份无关)。CI 加镜像历史扫描守卫。
- ⛔ argv/env 注入(`docker inspect` 明文可见)。
- ✅ 流水线:源机导出(§2.3)→ scp 到目标 staging(`<state>/xfer-cred/<actor-hash>/`,目录 0700、文件 0600,scp 保权限;**transport only 纪律不变**,ssh.py 不加任何凭据参数)→ 短生命周期 helper 容器把束灌进 **per-worker docker named volume** → **staging 立即 shred** → worker 容器只读挂载该 volume 到 harness 期望路径。
- 容器内以**非 root、uid=daemon 用户**运行;volume 内文件 0600。

### 2.3 macOS 源特例(claude)

claude 在 macOS 的主凭据在 login Keychain,文件可能是副本或不存在。源侧导出必须是**源机操作者显式授权的一步**(如 `security find-generic-password -w` 触发本机授权),导出成 0600 临时文件即传即 shred,**不允许脚本静默读 Keychain**。devusr 用例的源机就是 macOS,此步写进段 2 演练脚本并人工确认。

### 2.4 落地后权限与"不残留宿主"(D-A 定案:解释 A)

- staging:receive 完成(strict resume 通过)即 shred,残留窗口 = 传输分钟级。
- volume:**worker 收养期内存活** —— daemon 重启要重拉容器,凭据必须在。内容在 docker 内部存储(`/var/lib/docker`,root-only,非常规路径),宿主非 root 进程与宿主 harness 够不着。容器内运行时副本落在 **tmpfs $HOME**(`--tmpfs /home/hyprial`,`--rm` 随容器灭),从不写宿主路径 —— "容器停止后凭据不残留宿主"在文件系统层面成立,volume 是收养期的权威副本。
- **退役即清**:`hyprial agent stop`/destroy/再迁出 ⇒ 按 label 清容器 + `docker volume rm` + 删 worker home;再迁出时束从现 volume 经 helper 容器导出走同一管道。
- 字面解释 B(停止即清)经呈报后**不采用**(与 daemon 重启复活冲突,D-A 定案记录)。

### 2.5 env 类凭据

owner 若用 `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` 等 env 凭据(而非 OAuth 文件),只能 `docker run -e` 注入,`docker inspect` 可见 —— 降级标注。devusr 用例优先确认其为文件型 OAuth;env 型在段 2 再议(候选:daemon 侧 env 注入代理,不进 inspect,但复杂度上移)。

## 3. 身份:owner 钉住,machine 诚实

- 现状:URI 四段全部由 daemon 环境现铸(owner=`HYPRIAL_OWNER`||login;machine=`HYPRIAL_NODE_ID`||hostname)。devusr→hq 按现状 = `agent:bobadmin:<hq-node>:X` —— **owner 段静默改名,额度归属与身份叙述全错**。
- 提议:`HarnessLaunchSpec` 增加 `pinnedOwner`(容器模式必填),receive 时从源 plan 钉入;daemon 铸 URI 时 `pinnedOwner` 优先于 `resolve_node_owner()`。machine 段保持**目标 node_id**(presence 诚实:worker 物理在 hq)。迁移后 `agent:amyws:devusr-dev:X` → `agent:amyws:<hq-node>:X`,新旧 URI 在迁移报告显式列出 —— machine 段变更是 P0 既定语义,显式即非静默(D-D)。
- P0 的 `TRANSFER_OWNER_MISMATCH`(两 daemon env owner 必须一致,`orchestrator.py`)在容器模式下**改写**:不再要求 daemon env 一致,改为 pinnedOwner 写入并生效,否则拒绝。裸机 P0 的闸不动。
- resolve-or-reject、别名收敛、HandoverNotice 语义等 L3 其余部分原样继承 P0。

## 4. 额度归属的验证方法

1. **provenance 探针**:receive 前后各在容器内跑一次 harness 身份查询(auth status 类只读命令,具体命令与输出字段按 §2.1 版本在段 2 钉死),指纹(账号/plan 标识)与源机同探针结果比对,不一致 = receive 失败。
2. **变异判据(进验收,硬性)**:摘除凭据卷重启容器 ⇒ 首个 API 调用必须 401/403 **响亮失败**;并断言无静默 fallback —— 容器内无宿主 HOME 挂载、无 API key env、macOS Keychain 物理够不着容器。**容器隔离正是该判据可证的前提**(对照 §6 备选方案)。
3. **额度佐证**:一个真实 turn 后按组织记忆 harness-quota-sources 口径查 owner 侧额度消耗(⚠️ 该记忆条目在本机未检索到,段 2 向 Allen/devusr 确认口径)。
4. PAC 既有口径延续:源侧四条 + 目标侧五点 + 本文新增凭据三条(探针一致/变异判据/额度归属),假绿教训(只查 sessionRef)同样适用于凭据 —— 只查"进程活着"不算数。

## 5. 与 P0 的关系:并列模式

`hyprial agent move <name> --to <host> --containerized`。P0 裸机路径**不动、不退役**(无 docker 目标机仍是合法落点)。复用:plan/quiesce/session 定位器/SSH 控制面/strict resume/回滚提交点。差异四块:前置探测(两端 docker+源机镜像,两种模式均核目标 uv/claude/pi 非交互 PATH)、ship 多凭据束管道与镜像 tar 链(D-C)、receive 的 spec 多 `containerized`+`pinnedOwner`(+`containerImage`)字段、launcher 包装层。**Q2 裁定变更记录**:P0 设计"凭据永不随迁"仅保留给裸机模式;容器模式按 Allen 决定携带凭据。实现落点(段 2):`src/hyprial/transfer/container.py`(镜像/volume/挂载/argv 包装/入口契约)、`HarnessLaunchSpec` 三字段、daemon receive 的 volume 准备与退役清理钩子、三家 client 的容器包装钩、`scripts/build/docker/Dockerfile.hyprial-worker` + 构建脚本、CI 镜像守卫。容器化 worker 的回滚语义:**回滚恢复的是源机 quiesce 前的原 spec**(裸机迁出则回滚回裸机;容器迁出则带既有 volume 回滚),不把容器化字段反向烙进源机。

## 6. 备选方案:凭据束 + 隔离 HOME(不用 docker)—— **已裁弃**

> **D-B 定案(2026-08-24,Allen 亲裁):「容器化后没必要保留 macOS 宿主」** —— 放弃 macOS 降级路径,目标环境**全容器化**。本节保留为决策痕迹,不再作为候选方案;若未来出现无 docker 目标机需求,需重新立项评估,不得默认回退到此方案。

worker 以 `HOME=<隔离目录>`(含 `.claude/.credentials.json`、`.codex/auth.json`、`.pi/agent/auth.json` + session + worktree)在目标机裸拉起。

- ~~优点~~:目标机零 docker 依赖(macOS 目标可行);无镜像构建/分发;实现面最小。
- **裁弃依据(当时的缺点分析,现成为定案理由)**:①隔离弱 —— 同 uid 进程可读宿主全部,凭据外溢面大;②🔴 **变异判据在 macOS 上不可证**:claude 走 login Keychain,HOME 重定向管不住,摘除束后 worker 会**静默 fallback 到宿主 Keychain 凭据** —— 恰是本设计要防的事故形态(用了宿主的额度还显示成功);③环境一致性仍靠 P0 的 PATH 探测,版本漂移问题原样保留;④uv 冷缓存等目标机陷阱原样保留。

## 7. 开放问题与段 2 前置

- 段 2 演练:与 devusr 协调,他机器出一个 claude worker(带 marker 会话)→ hq;走 PAC + §4 三条;macOS Keychain 导出步(§2.3)由他本机显式执行。
- 镜像内三家 CLI 版本钉在哪个基线(跟随 P0 验证矩阵:claude 2.1.235+/pi 0.84.2/codex 0.148)。
- docker 组成员 ≈ 宿主 root:目标机信任模型一句话写进 runbook(段 2)。
- interactive/TUI 会话、dsh、lark adapter 不在范围(继承 P0 边界)。

## 8. 段 2 实现补记(2026-08-25,实现 PR #284)

与本文设计稿的两处偏差,实现时发现并记录:

1. **claude SDK 运行时从"预热 uv 缓存"改为"持久 SDK venv"**。§1.2 原案是构建期预热 `uv run --isolated` 的缓存;实测(本机构建)发现运行时 `uv run` 仍按 index 解析,缓存命中依赖构建/运行两侧 index 一致,脆弱且每次启动都要网络可达。改为镜像内 `/opt/hyprial/sdk-venv` 持久 venv 预装 `claude-agent-sdk==0.2.125`,容器内 claude worker 命令 = `sdk-venv/bin/python /opt/hyprial/_agent_sdk_worker.py` —— 容器内隔离是天然的,`uv run --isolated` 的收益在容器里不存在,运行时零网络。
2. **镜像体积实测**:2.29GB(node 22 + python 3.12 + 三家 CLI 钉版 + SDK venv),`docker save` tar **552MB** —— D-C 的 save/传输/load 链在真实镜像上可行(未触发任务书的体积呈报线)。构建期网络逃生口:`UV_PYTHON_INSTALL_MIRROR`/`UV_INDEX_URL` build args(github/pypi 不可达网络下实证有效;默认不变)。

实现落点与 §5 预告一致:`src/hyprial/transfer/container.py`、spec 三字段、daemon receive 准备 + down/undo/complete 三处退役钩、三家 client 包装钩、`scripts/build/docker/` 三件套。补两处 §5 未列的细节:①容器不定名,打 `hyprial.worker=<harness>:<name>` label,退役按 label 清(防 SIGKILL 孤儿容器与旧容器双活);②docker 组成员 ≈ 宿主 root 的信任模型:迁移的 worker 在目标机以 daemon uid 跑在容器里,能读写挂载的 state dir 与 worktree —— 与裸机 worker 等价,不多不少。

验证:单测 29 项新增(含 Dockerfile 禁 COPY 凭据守卫、argv 无宿主 HOME/无 API key 不变量),全量 2042 过;本机真实 docker 冒烟(假凭据隔离路径):staging→volume→entrypoint 拷贝(0600/降权 exec)→prune 全链绿。API 级变异判据(摘卷必须 401/403)留真实演练,argv 层不变量已有测试钉住。
