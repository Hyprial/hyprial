# Changelog

All notable changes to hyprial (Harness Bridge) are documented here.

格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)（版本倒序、
每节带日期、Added / Changed / Fixed / Removed 分类）；版本号遵循 [SemVer](https://semver.org/lang/zh-CN/)。

> 2026-10-01 重排：0.4.1–0.4.6 各节由 tag 区间提交记录重建（标题级摘要），
> 早期"按主题归档"的原始记录原样保留在对应版本节内并注明出处。
> 单版本详细发布说明归档于 [docs/notes/changelog/](docs/notes/changelog/)
> （如 [CHANGELOG-v0.4.2.dev7.md](docs/notes/changelog/CHANGELOG-v0.4.2.dev7.md)）。

## [0.5.5.4] - 2026-11-03

### Fixed
- ci(release)：发布链不再因不影响分发的原因判红（v0.5.5.3.4 收尾，runs 51606/51622/51727）。
  wheel-release 的 registry 发布改用 `uv publish --check-url`，字节相同的同名文件幂等跳过
  （stable floor 让 nightly==stable SHA 时不再 409），sha256 不同仍响亮失败；workflow
  artifact 上传失败只在 registry 发布 success 时降级为 warning；Go/npm/uv 缓存与 CPython
  3.12 镜像持久化，构建步 30 分钟上限、job 55 分钟；github-publish 等待 registry 的时长按
  job 剩余预算计算，上游缺件时明确 NOT EVALUABLE 而非被 runner 强杀。
- orgfs：修复 replica 挂接窗口永久吞信封的缺陷。`OrgFsMesh.attach_replica` 先 prime 后赋值，
  prime 期间到达并已落 store 的信封被 `_store_replica_envelope` 丢弃（`replica_store` 仍为 None），
  而赋值后 serving 又优先读 replica——窗口内只落在 store 的文档对 clone/catch-up 永久不可见
  （`test_all_members_offline_replica_serves_new_member_clone_and_catchup` 反复红、clone 读
  `unknown-doc`）。现在 attach 先以 NOT-READY 态赋值，prime 后用一次幂等 prime 回扫补齐窗口内
  store 增量，并排空窗口内有界镜像队列，然后才置 ready；未 ready 期间 serving 保持 store 路径，
  镜像写入只入队不上 replica actor——broadcaster put 不再被 prime 串行阻塞（实测窗口内镜像 put
  ~0.002ms；简单换序方案实测会阻塞 ~10s）。prime 失败回滚到挂接前状态，由下次 serve 的
  reuse 分支增量检查重新接管。19 文档空间实测挂接窗口 ~52ms；replica_mesh 测试连续 50 次 +
  全文件 3 次 `-n 8` 零失败。证据：`docs/notes/replica-attach-window-2026-11-03.md`。

## [0.5.5.3.4] - 2026-11-07

> 补丁版：从 dev 切出并合入 v0.5.5.3.3（补回 0.5.5.3.2/0.5.5.3.3 的发布工具链修复），再经集成分支合入
> daemon runtime、identity/network、CI/tooling、docs、单 daemon 锁（filelock）与 Windows 兼容门禁，用于清理积压 PR。
> 版本号 0.5.5.3.1–0.5.5.3.3 已发布，本版为 0.5.5.3.4。**升级与兼容须知**（公网镜像改黑名单、并发 login 语义、
> 降级需重新 login、已装 service 需 `hyprial doctor --fix` 等）见单版本说明
> [CHANGELOG-v0.5.5.3.4.md](docs/notes/changelog/CHANGELOG-v0.5.5.3.4.md)。

<!-- intg/daemon-runtime-fixes-0.5.5.3.4 -->
### Fixed（daemon runtime）
- daemon：pending 的 invite redemption 无法续跑时不再中止 daemon 启动，改为 degraded 启动并记录
  `tailnet/redemption-degraded.json`，按退避重试；`doctor`/`ps` 可见该状态（#2319）。
- membership：#2319 的降级重试与 dev 上 #2484（不重放过期 invite）合并后的语义：pending redemption 若被 authority
  永久拒绝（`INVITE_EXPIRED`/`INVITE_INVALID`/`INVITE_CONSUMED`/`INVITE_VERSION_UNSUPPORTED`），daemon 仍降级启动，
  但之后的启动和后台重试都不再重放该 invite；degraded 记录标 `retryable: false`，doctor 提示用新 invite
  `hyprial org join -`。用新 invite 加入或兑换完成（含启动时完成）会清除该 tailnet 的记录；多 tailnet 时 stale 记录
  不覆盖其他 tailnet 的记录；不可重试的记录不再让每个维护 tick 读盘。临时性失败照旧按退避重试。
- readiness：harness supervisor 缺席时聚合不再抛错，`unknown` 判定照实记录（#2346 / #2090）。
- pac：PAC 展开时按完整 principal 解析 `created_by`，agent 创建的父图不再因短名截断而失败（#2389）。
- actor：effect 失败保留经脱敏、截断的安全错误细节（`EffectCompleted.detail`）（#2392）。
- routine：图在两次轮询之间 completed 后仍被算作「进行中」，导致 `hyprial routine set` 长期返回
  `ROUTINE_BUSY`（pause 后永不结算）。现在 `routine set` 在替换前只读探查在飞图，已结算
  （`done`/`escalated`）的轮次会清除 inFlight 并记入 outcomes；仍在跑或探查不了的轮次照旧拒绝（#2398）。
- daemon：agent 的投影缓存在完整记录的运行时更新中可能把 `harness_args` 回写成旧值（双向）；
  launch 与 resume 快照现在读取持久化的意图，`start` 不再丢失或复活已存储的启动参数，非法的 stored
  mode 以 `INVALID_ARGUMENT` 拒绝（#2482）。
- windows：`request_http` 模块顶层 `import fcntl` 导致 Windows 上 ImportError；server.lock 改走 kernel
  `ProcessFileLock`。init 在没有受支持 per-user supervisor 的平台（Windows）上，默认 service 注册报告为
  `{ok: true, skipped: true, reason: "service-platform-unsupported"}`，不再是 `service-install-failed`；
  显式 `--service` 在动 home 之前以 `SERVICE_PLATFORM_UNSUPPORTED` 报错（Task Scheduler 推迟）。
- service：已安装 launchd/systemd per-user service 时，`daemon start`、login/init 拉起 daemon 改为请求
  supervisor（`launchctl bootstrap|kickstart` / `systemctl --user start`），`daemon stop` 与 login 切号
  前的停服改为 `launchctl bootout` / `systemctl --user stop`，不再出现 CLI spawn 与 `KeepAlive` /
  `Restart=always` 两个 launcher 抢锁、或 IPC 停服后被立即拉起。未安装 service 时行为不变。
  非 supervisor 管理的 daemon 遇到健康 holder 只做有界等待后退出（不再无限等待成常驻第二进程）。
- upgrade：从 ≤0.5.5.3.3 升级时，旧解释器的升级尾段（`hyprial upgrade` / autoupdate 子进程，未装
  service 时走 `restart_daemon`）仍持 identity flock 并经 `pass_fds` + `HYPRIAL_IDENTITY_TRANSACTION_FD`
  把 fd 交给新装的 `daemon run`。新 daemon 若另开 open file description 去 acquire，会被自己继承的 fd
  挡住，等满锁预算后以 `IDENTITY_TRANSACTION_BUSY` 退出、daemon 一直停着。现在 `daemon run` 保留一个
  只服务这一个版本的兼容分支（`IdentityTransactionLock.adopt_legacy`，0.5.5.4 删除）：pop 该变量，POSIX
  上校验 fd 的 `(st_dev, st_ino)` 与锁文件一致、并用 kernel `lock_exclusive(fd, blocking=False)` 确认它
  就是持锁方后接管，ready 时关闭；其余情况忽略该变量照常 acquire。从 ≤0.5.5.3.3 升级必须经过这条路径。
- daemon：`daemon run` 等待 `daemon.lock`（含首次有界等待与 wait-while-healthy）期间不再一直持有 identity
  事务锁：每次等待前释放、等待后重新 acquire 并重新校验 expected generation（锁顺序仍为 identity →
  `daemon.lock`），login 不会在这段时间里一直报 `IDENTITY_TRANSACTION_BUSY`。拿到锁后 `chmod` 失败时
  立即释放，不再把锁泄漏在本进程里。
- service：macOS 上 `daemon stop` / login 切号走 `launchctl bootout` 会卸载 LaunchAgent，之后
  `hyprial daemon restart` 和 upgrade 的 supervisor restart 曾报 `SERVICE_SUPERVISION_LOST`
  （installed but not loaded）。现在已安装、可归属、未加载且没有 serving daemon 的 unit 按 supervisor
  start 处理（与 `daemon start` 同一个 `bootstrap`）；仍有 daemon 在 supervisor 外运行时照旧报 lost。
  login 在 supervisor 停服之后、identity commit 之前失败（plan-stopped 拒绝、commit 失败、Ctrl-C）时，
  会再请求 supervisor 启动旧 daemon（结果记在 `data.daemon.restart`），`nextStep` 也写明
  `hyprial daemon start`。
- service：supervisor 停服的 SIGTERM→SIGKILL 宽限期提到 120s（launchd 模板新增 `ExitTimeOut=120`，
  systemd `TimeoutStopSec` 45→120），不低于 daemon 自己的 teardown 预算加 exit backstop
  （`TEARDOWN_BUDGETED_SECONDS`=92s）加余量；CLI 等待 supervisor 停服命令的上限随之调到 150s。
  **已安装的 unit 需要重新安装才能生效**：`hyprial doctor --fix`（或 `hyprial daemon service install`）；
  在此之前 doctor 报 template 不匹配，归属判断（attributed）不受影响。
- service：login 切号的 supervisor 停服在 probe 没拿到 pid 时，先报 `DAEMON_STOP_IDENTITY_UNKNOWN`
  再决定是否停服（不再出现 daemon 已停、login 却失败）；旧进程的 process identity 在发停服命令之前读取，
  PID 复用 fence 恢复有效。
- login：`refresh_access_token` 写回轮换后的 refresh token 时改为在 identity 锁内 compare-and-swap：
  重新读 `secrets/login.json`，只有 `refreshToken` 与 `identityGeneration` 都没变才写，否则丢弃这次
  rotation 并报 `CREDENTIAL_WRITE_CONFLICT`。此前 refresh 与 login 交错时会把新账号的 refresh token
  覆盖成旧的、把 generation 回退，导致新 daemon 被误判 superseded、下一次 login 复用同一个 generation。
  desktop onboarding 的 `run_login` 也通过 `before_commit` 在同一把锁内 commit。
- login：identity generation 跨 logout 保持单调（`HYPRIAL_HOME/.identity-generation` 记录高水位，
  每次 commit 前先推进），同号碰撞不再掩盖插队；只有磁盘上是更大的 generation 时才报
  `IDENTITY_SUPERSEDED`，login.json 被删除或改写成其他值时报 `CREDENTIAL_CHANGED`。
- windows：`hyprial doctor --fix` / upgrade steps 在原生 Windows 上因 `os.open(目录, O_RDONLY)` 抛
  `PermissionError [Errno 13]`（#1902 复现，`upgrade/runner.py` 与 `upgrade/steps/mcp_entry.py` 的目录
  fsync）。新增 kernel 唯一 seam `fsync_directory(path)`：按能力判断，目录打不开（`PermissionError`）就跳过
  目录 flush，`fsync` 本身的错误与目录不存在照常抛出；不写 `os.name` 分支，不移植 #1902 的
  `directory_sync.py`。0.5.5.3.4 只迁移这两处，其余目录 fsync 由 W007 基线收敛。
- windows：daemon 已 ready 后 `init` / `daemon start` 仍报失败——launch 结束时 `finally` 里删除
  per-launch capture alias（`.daemon-launch.<nonce>.log`），而 daemon 子进程正把它当 stdout 持有，
  Windows 不能删除仍打开的文件（WinError 32，#1902/#2035 复现）。现在删除改为 best-effort
  （`_discard_launch_capture`，删除失败不再让成功的 launch 变成错误），残留 alias 由下一次 launch 在
  `daemon-launch.lock` 范围内 sweep（不加锁的 capture helper 不 sweep，避免误删并发 launcher 的
  alias）。平台无关代码，没有 `os.name` 分支（采用 #2035 d7d73d89b + a7d5345a9，合并为一个改动）。
- windows：`uv tool install` / venv 环境里 `daemon start` / `init` 一直等到超时——launcher 的 readiness fence
  比较 `daemon.json` 的 `pid` 与 `Popen.pid`，而 Windows 上 venv 的 `python.exe` 是 redirector，真正的
  daemon 是另一个进程，两者永远不等。现在每次 launch 生成 `uuid4` nonce，通过 `HYPRIAL_DAEMON_LAUNCH_ID`
  交给子进程，daemon 写进 `daemon.json` 的 `launchId`，launcher 按 nonce 比对。三平台都用 nonce，没有
  `os.name` 分支（采用 #2035 0407fbd1e 但去掉其 `if os.name == "nt"`）；`daemon run` 一启动就 pop 该变量，
  launcher 也不转发继承来的值，后代进程（worker、harness、autoupdate 子进程）不会继承。常量放在 kernel
  daemon-launch contract，`set_launch_id`/`take_launch_id` 与 identity generation 的 launch env 助手放在一起
  （`shell/impl/login/generation.py`，不新增 CLI→backend import）。

### Changed（daemon runtime）
- cli：`route:<adapter>:<route>` 被用作 `escalate_to`/`report_to` 时，拒绝信息改为指向受支持的用法
  `hyprial message send --to route:<adapter>:<route>` 和创建命令 `hyprial adapter route add <adapter> <route>=<native-chat-id>`。
  新增显式探针 `hyprial message probe-delivery-paths`，向 agent/user/route 三族各发一条带 `hyprial-probe` 标记的消息，
  结果写入 `state/delivery-paths-probe.json`；`doctor` 新增只读检查 `delivery-paths`：从未跑过探针、或未配置 route 时
  为 warn（不影响 doctor 退出码），探针超过 24h 为 warn，已探测且未送达为 fail（#2092）。
- routine：默认 `agent-home-setup` 初始化流程先为 agent 绑定其所 pin adapter 的 owner 私聊 route；
  无候选会话时跳过并提醒 owner 先私聊 App，随后再完成 agent home 与默认 routine 绑定（#2332）。
- lock：identity 事务锁与 `daemon.lock` 改由新增依赖 `filelock==4.2.9` 实现，统一封装在 kernel
  `ProcessFileLock`（`fallback_to_soft=False`、`preserve_lock_file=True`、`thread_local=False`、
  `is_singleton=False`；`probe()` 在本进程持锁时返回 `self-owned`），架构测试禁止在别处直接构造
  `filelock`，并禁止模块顶层导入 POSIX-only 模块。`pywin32==312` 声明为 win32 直接依赖。
- login：删除 CLI→daemon 的锁 fd 交接（`HYPRIAL_IDENTITY_TRANSACTION_FD`、`pass_fds`、
  `acquire_or_adopt`）。CLI 在锁内 commit identity，并在 `secrets/login.json` 同一次原子写入里记录单调
  递增的 `identityGeneration`，放锁后带 `HYPRIAL_EXPECTED_IDENTITY_GENERATION` 启动 daemon；daemon
  自己先拿 identity 锁再读 identity，立即 pop 该变量，generation 不符时以退出码 76
  （`IDENTITY_TRANSACTION_STALE`）退出且不构造 runtime；supervisor 拉起的 daemon 从不带该变量。
  并发 login 语义由「后来者 BUSY」改为「后写入者生效」：被抢先的 login 报告 `IDENTITY_SUPERSEDED`。
- login（T11 例外，需 owner 知情）：daemon 现在会打开 `secrets/login.json`，但只读取整数
  `identityGeneration` 做启动校验，从不读取 refresh token；identity 来源仍是
  `HYPRIAL_OWNER > settings.owner`。
- 降级：0.5.5.3.4 写入的 `secrets/login.json` 多了 `identityGeneration` 字段，≤0.5.5.3.3 的
  `read_login_credential` 会以 `CREDENTIAL_UNPARSEABLE` 拒绝（org invite、shortlink、desktop status、
  login session 失败）。**不支持在不重新登录的情况下降级**：装回旧版后运行 `hyprial login`（或手动删掉该字段）。
- daemon：只有 `HYPRIAL_SERVICE_MANAGED=1` 的 daemon 会在健康 holder 后面无限等待 `daemon.lock`；
  launchd/systemd 模板已设置。docker `restart: always`、k8s 等外部进程管理器以 `hyprial daemon run`
  为 entrypoint 时也需要设置 `HYPRIAL_SERVICE_MANAGED=1`，否则 daemon 在有界等待后退出，进入外部
  supervisor 的重启循环。
- CI（Windows 兼容原则）：不再逐个打 Windows 补丁、也不专门为 Windows 写代码路径，而是一开始就用跨平台库，
  并在 CI 里双向拦截 anti-pattern。新增纯 stdlib AST 闸门 `scripts/ci/check_windows_portability.py`
  （tests.yml structural-limits 步骤 + PR base 对比步骤 + 契约测试
  `tests/tooling/architecture/platform/test_windows_portability_gate.py`）。AST 规则 W001–W015、W019：
  POSIX-only 模块 import（含 `importlib.import_module`/`__import__`）、`os` 的 POSIX-only 属性（`getuid`/
  `killpg`/`wait`/`O_NOFOLLOW`…，含 `import os as o`、`from os import fork`、无默认值的
  `getattr(os, "fork")`；`getattr(os, "O_X", 0)` 合规）、POSIX-only signal 与 `loop.add_signal_handler`、
  `subprocess` 的 `pass_fds`/`preexec_fn`、`start_new_session`/`process_group`/`user`/`umask`（只有 `None`/
  `False` 算关闭，`process_group=0` 计数；`from subprocess import Popen as P`、`**kwargs` 字典、
  `functools.partial` 同样识别）、`AF_UNIX`/unix asyncio API、目录 `os.open`+`fsync`（按 flag 与 fd 数据流
  判定，普通文件读不算）、`dir_fd=`、`os.kill(pid, 0)`、`os.kill(pid, SIG*)`、POSIX 路径字面量、
  `chmod(0o600…)`、运行时 symlink、`os.pipe()`/`select.poll|epoll|kqueue`/`add_reader`，W015：平台 seam
  （`kernel/impl/platform/**`、`owned_process.py`）之外任何方向的平台分支（`os.name`/`sys.platform`/
  `platform.system()`，含 `.lower()`、`startswith`、`in`、`match`、局部变量），W019：seam 之外的
  Windows-only API（`msvcrt`/`winreg`/`_winapi`/`win32*`/`pywintypes`、`subprocess.CREATE_*`/
  `DETACHED_PROCESS`/`STARTUPINFO`、`ctypes.windll`/`WinDLL`、`os.startfile`…）；W015/W019 有守卫也计数。
  POSIX-only 命中只有在守卫检查同一能力时才不计：同一模块与属性的 `hasattr`/`getattr(..., None)`/
  `os.supports_*`、极性正确的平台判断（`if os.name == "posix": return` 之后的 POSIX 调用照样计数）、与
  命中类型匹配且不裸 re-raise 的 `except ImportError|AttributeError|NotImplementedError`。W016 import smoke
  （`test_windows_import_smoke.py`：隔离解释器中屏蔽 POSIX-only 模块与属性、`os.name="nt"`，扣除普通解释器下
  同样失败的模块）。现存 349 处 AST 命中 + 1 个 W016 根因记入 `scripts/ci/windows-portability-baseline.json`：
  `(path, rule) -> count` map（183 个 key，每个 key 有 reason 与 removal 目标版本），超出计数或新 key 为红、
  已修复未下调（stale）为红；`--base-ref` 对比 PR base，新 key 或计数上调为红（总数不变的"一出一进"也拦
  住），`--prune-baseline` 只降不增，测试另有只能往下改的总数上限。W017（子进程可能仍打开的文件不能
  `unlink`/`os.replace`）、W018（用 launch nonce 而非 `Popen.pid` 识别进程）写进 PR 模板 checklist。替代
  lock 工作里较窄的 `test_windows_antipattern_ratchet.py` + `windows-antipattern-baseline.json`（规则是其超集）。

<!-- intg/identity-network-fixes-0.5.5.3.4 -->
### Changed（identity / network）
- identity：agent 的 git 提交作者不再回落到 owner 的个人身份，默认签为
  `<actor> <agent-<name>@agents.hyprial.com>`；旧 home 下的 legacy tool profile
  （如 `tools/agent-home-p2-<owner>-v1`）升级时改名为 `*.migrated`。owner 身份经原生 git 读取（#2378）。
- sidecar（Tailcat）：`proxyProtocol: "v2"` 的 exposure 只接受私有 Unix socket（绝对、clean 的 `unix:` 路径，
  父目录为当前用户所有、`0700`、非 symlink、无 setuid/setgid/sticky）；servicehost 与协议校验两处同时执行。
  **升级注意**：已部署的 host config 若用 `v2` 加 TCP target，启动时会被拒（#2139）。

### Fixed（identity / network）
- dl（edge worker）：白名单命中但 R2 对象缺失时返回 `503`（`retry-after: 1`）而非 `404`，
  并收紧双向 bucket 校验；worker 需另行 `wrangler deploy` 才生效（#2057）。

<!-- intg/ci-tooling-0.5.5.3.4 -->
### Changed（CI / tooling）
- ci：去掉组件源分支规则（gui/sidecar 改动不再要求来自 `intg/<name>*`），dev → release 的同步 PR
  免检 release changelog 小节（#2324）。
- 测试：单测一律在临时 HOME / XDG 目录下运行，不能再向真实的 launchd/systemd 域安装 unit；
  `HYPRIAL_HOME` 不是 `$HOME/.hyprial` 时，service install/uninstall/restart/reconcile 拒绝执行，
  并报 `service-home-nondefault`。**升级注意**：使用非默认 `HYPRIAL_HOME` 且已装过服务的用户，
  不能再经 supervisor 重启 daemon（#2205）。

### Fixed（CI / tooling）
- ci：release changelog 门禁关闭两处 fail-open：未版本化的 release 分支，以及只有空小节或只有删除行的情况，
  不再被当作有条目（#2129，取 517d8f7a8、4265a8301）。

### Changed（发布）
- 发布：公网快照从白名单改为黑名单（Allen 2026-11-03 裁定），除 `SNAPSHOT_DENYLIST` 列出的路径外全部同步到
  GitHub 镜像（约 1560 → 4359 个文件）。扫描闸加固：内网标记豁免改为按行（`path :: rule :: line-sha256-16 :: reason`），
  新增不可豁免的 private-marker（真实 tailnet/节点/设备名单不进快照）、staff-email、mdns-device-name、lark-app-id 规则，
  同时扫描文件路径；`agents.hyprial.com` 与 `code.hyprial.com` 视为公开域名。黑名单条目必须命中实际路径，
  GUI 子树 `gui/{docs,vendor,tests,evidence}/` 继续不公开；GUI wheel 仍由 0.5.5.3.2 的溯源戳在快照内构建（#2330）。

### Added（CI / tooling）
- e2e：把 #1855 的 tailcat 入网覆盖按 v6 重建为 E2E-029（control 侧邀请拒绝 + inbound business-stream 撤销）与
  E2E-030（redemption 回复丢失后 durable 恢复），走真实 v6 transport 并接入 isolated gate；仅测试与文档，
  无产品语义变更。sidecar `go test ./...` 因 E2E-030 每次多约 70 秒（`-short` 下跳过）（#2142）。
- ci：只读的 `scripts/ci/cancel-queue-plan.py`，生成待取消 run 的清单，交给有权限的人用 API 执行（#2153）。

<!-- intg/docs-0.5.5.3.4 -->
### Added（docs）
- 文档：合入一批设计草稿存档，**均未实施**，不代表本版交付对应功能：PAC 准入门（#1742）、
  消息结算从模型移到 harness（#1898）、agent move 设计基线与 R4.3 方案（#2006、#2004）、
  agent comms 收集与 daemon 查询重构（#2005）、业务生命周期与接口映射（#2222）、
  cell/thread/conversation 模型与 cloud agent attach（取自 #2313，目标 0.5.5.4）。
- 文档：OrgFS 照搬 Yjs 默认语义的状态模型设计（仅保留当前 CRDT 状态，删除 journal/历史/快照，一次性 journal 折叠迁移、N-1 只读兼容窗口），Allen 已在文中定案但**未实施**、目标版本待定；已发布的 0.5.5.3.x 仍是 v2 journal 存储，无布局不兼容（#2104）。

<!-- intg/ci-e2e-hotfixes-0.5.5.3.4 -->
### Fixed（CI / E2E）
- ci：删除 `desktop-release.yml`。它依赖的 `app/` 工具链已随 d9b953805 删除，触发后只能失败；同时把
  `cancel-on-pr-close.yml` 登记进 runner 镜像清单，修掉 dev 上 `test_real_tree_denominator_and_family_witnesses`
  的红（#2524）。
- e2e：`hosted-agent-e2e.yml` 改名为 `mock-model-harness-e2e.yml`（context 变为
  `mock-model-harness-e2e / hosted`），并修复 10-30 起的持续红：loopback runner 改为继承 job 的 proxy，
  只让 loopback 地址绕过 proxy；`agent start` 之前先把 Claude Agent SDK（80 MiB）预热进 agent 自己的
  uv cache，不再在 daemon 60s/70s 的启动 deadline 内冷下载；失败时诊断里带上 worker 日志；AT01 登记
  8135feacd 新增的两个 additive 列（#2527）。
- e2e：AT04 hosted-upgrade 标记为 KNOWN-FAIL（只认 `MEMBERSHIP_NOT_INITIALIZED` 这一个签名，PASS 反而判红）。
  产品问题是隔离 / 单节点 daemon 上 `org create` 依赖 Tailnet sidecar，在 #2539 跟踪（#2527）。
- ci：`org remove --device`（490e111f2）补齐 `contract/cli/surface.json` 快照，错误码改用
  `ipc_errors.INVALID_ARGUMENT` 常量（值不变），清掉 pytest shard 的两个红（#2527）。

## [0.5.5.3.3] - 2026-11-03

> 补丁版：wheel-release 自 10-30 起全红（stable 41379/44421、nightly 41262/44423、internal 44411，
> 仅 41381 nightly 与 40843 dev 成功）：workflow 把构建副本的 VERSION 戳成 `<base>+g<short sha>`，
> GUI 构建钩子的 clean-source 检查把这一 tracked 修改拒收
> （`GuiWheelError: the source tree has tracked modifications`）。tag 不可改，按 0.5.5.2.x
> 先例发补丁版；公网 release 资产仍停在 stable 0.5.5.2+g10ed948 / nightly 0.5.5.2.3
> （0.5.5.3.2 已 tag 且源码已发布，github-publish 44420 成功）。

### Fixed
- 发布：`gui_build_hook` 的 clean-source 检查（git checkout 路径）容忍且仅容忍一个 tracked
  修改——`VERSION`，且内容必须恰为 `git show HEAD:VERSION` 的提交值加 `+g` 与 HEAD 完整
  SHA 的前缀（≥7 位十六进制，接受 git 产出的任意缩写长度）；任何其他 tracked 修改、任何
  其他 VERSION 内容仍然拒收；`gui/` 未跟踪文件规则与快照溯源戳路径原样保留。
- 发布：clean-source 检查加固（独立评审 5 轮）：VERSION 戳逐字节比对（只认 `printf '%s\n'` 写出的形态、小写十六进制）；VERSION 的权限或类型变化不算戳；暂存区只允许同权限的 VERSION 内容变化；带 skip-worktree / assume-unchanged 标记的条目一律拒收；所有 git 探针固定使用 `core.fsmonitor=false`、`core.fileMode=true`、`core.untrackedCache=false`、关闭外部 diff，且不继承 `GIT_*` 环境变量。威胁模型写在代码里：防的是意外留下的脏状态，以及会把它藏起来的 index 和配置状态，不防能改写构建机 git 配置或 filter 的一方。

## [0.5.5.3.2] - 2026-11-03

> 补丁版：v0.5.5.3（run 44089）与 v0.5.5.3.1（run 44202）都打了 tag 但都未发到 GitHub。
> 0.5.5.3.1 死在安装性门：0.5.5.3 把 GUI 移到顶层 `gui/`，但发布快照白名单没有 `gui/`，
> 快照内 `uv build --wheel` 报 `GuiWheelError: GUI project is missing: …/gui/package.json`。
> tag 不可改，按 0.5.5.2.x 先例发补丁版；本版只动发布工具链与 GUI 构建钩子，产品行为不变。

### Fixed
- 发布：快照白名单加 `gui/`（wheel GUI 构建输入），排除项按 Allen 2026-09-16 对旧 `src/gui` 的裁定
  重新锚定到新路径——`gui/{docs,vendor,tests}/` 外加 `gui/evidence/`（WORM 材料）；
  fail-closed 的 `excluded_entries` 复查同步覆盖新条目。
- 发布：快照写入 GUI 溯源戳 `gui/gui-source.json`（schema `hyprial.gui-snapshot/v1`，写入方
  `scripts/ci/github_publish_policy/guistamp.py`）：钉定提交的完整 40 位 SHA、发布目标 remote
  （公网镜像，绝不信源构建者的内网 origin）与逐文件 SHA-256；戳在扫描闸之前落入快照，
  发布步的纯净重抽逐字节复现，purity 门保持精确。
- 构建：`gui_build_hook._stage_gui` 在无 `.git` 的快照树接受该溯源戳并逐字节复核，
  替代的只是原本依赖 git 的检查（tracked inputs / clean source / rev-parse / remote.origin.url）；
  lockfile 要求、全新 `npm ci`、symlink 拒收、dist 检查与整个 git checkout 路径原样保留，
  checkout 里出现的戳文件仍按未跟踪文件拒收（git 路径零放宽）。

## [0.5.5.3.1] - 2026-11-02

> 补丁版：v0.5.5.3 tag 已打（9eebfbaed7），但公网发布门（github-publish run 44089）的 gitleaks 把
> Agent 请求示例里的合成凭据 base64 字面量判成 generic-api-key，0.5.5.3 未能发到 GitHub。tag 不可改，
> 按 0.5.5.2.x 先例发补丁版。

### Fixed
- 发布：`request_catalog/agent/examples.py` 的合成凭据 base64 改为由明文合成值运行时计算，取值逐字节不变（方法样例与 OpenAPI 快照不变），公网发布扫描不再误报；不加豁免、不把运行时模块排除出快照。

## [0.5.5.3] - 2026-11-02

> 范围说明（Allen 2026-11-02）：#2214（durable work validation evidence / `org work rebaseline`）与
> #2244/#2245（org 成员模型第一波、org 邀请登录确认）**均不在本版**，整体移至 0.5.5.4；本 train
> 按 5 个 PR 重建（#2216 #2229 #2213 #2242 #2243，基线含 #2255，零 GUI 路径变更）。
> 本 train 对 openapi 的唯一新声明是 `session.turn.refresh`（`x-hyprial-methods` 11 → 12），
> GUI 参考客户端再生成仍不经本 train：紧随的 intg/gui* PR #2261（`intg/gui-0553-client-regen-r4`）
> 它已基于本 train head 重生成，把客户端从 11 追平到 12（逐文件 before → after 见 train 证据 note）。

### Added
- GUI：intg/gui-init 并入 0.5.5.3（Allen 2026-10-31：“gui-init 进 0.5.5.3”）。包含：预览与正式共用的单套 GUI；Agent provider 参考样例（四个组件：AgentSelector / ProviderDefaultView / AgentGrantList / AgentProviderSection），经 MethodSpec 声明的五个 Agent 方法、HTTP 入口（H-01）与生成的 TypeScript client（H-02）；登录 / 登出写操作在本版不开放 HTTP（H-03 写准入下一轮集成）。浏览器验收为显式 opt-in 入口（gui_acceptance），不进普通测试通道。
- 观测：Codex 整终端回合边界（含手动在 TUI 里输入的回合）进入 cell 运行状态。连接断开后不再声称持续覆盖，重连并重新订阅后才恢复；启动时接管已在进行的回合失败不会拖垮观测进程；回合边界在写入成功后才推进去重状态，失败的接管优先于之后的实时边界补发。已知缺口：产品 `--tmux` 启动路径暂无边界来源（见 docs/notes/cell-e2b-launch-scope-2026-10-31.md）。
- 观测：Claude/Pi 原生回合刷新 `session.turn.refresh` 改为 MethodSpec，带已确认的 daemonEpoch；观测断档只有在更新且被接受的观测之后才清除。已退役的会话代次和 daemon 重启前的注册一律拒收（`stale_daemon_epoch` 等类型化原因），重复注册、注销、统计清零都不会抹掉防护状态；每次断档恢复只做一次单飞刷新。已知缺口：旧的回合开始/结束上报协议尚未带代次与序号（跨载体迁移留待 0.5.5.4）。
- 文档：新增 `docs/reference/known-problems.md`，AGENTS.md 要求开发前先查；首批登记 4 条（Allen 2026-10-31）。
- CI: 目标分支为 `release/**` 的 PR 现在必须带对应版本的 changelog 小节，且该小节在本 PR 中被改动，
  否则 `release-changelog` 检查变红（Allen 2026-10-29）。独立 workflow 实现，不依赖 `tests.yml`
  的触发分支与 uv 安装；promotion PR（`release/**` → `dev`/`main`）不适用，并在输出里说明。

### Changed
- CI：`dev` 只接受 `release/*` 与 `hotfix/*` 的 PR，其他来源在路由阶段拒绝；GUI/sidecar 的集成线路门（须来自 `intg/gui*` / `intg/sidecar*`）改在进入 `release/*` 时检查，晋升 PR 仍跑被晋升组件的测试套件（Allen 2026-11-02）。
- 流程：0.5.5.3 起功能分支一律先合入 `release/v0.5.5.3` 集成分支，`dev` 只接受 `release/*` 与 `hotfix/*`
  的合并（Allen 2026-10-31）。本次把 `dev`（`df9d50502`）同步进本分支作为集成起点。

### Fixed
- daemon: 修复 #2242 后 interactive Codex 回复不再到达 daemon 的回归：carrier `thread/resume` 加线程后 app-server 把 MCP 工具同意请求（`mcpServer/elicitation/request`）也投递给 daemon 连接，transport 的 PR1 `-32601` 自动拒绝被 codex 记为"用户拒绝"，`harness_reply` 从不执行；现在同意面请求一律不回应、留给用户 TUI 结算，其余 server request 保持原拒绝，whole-session 观测不受影响。
- orgfs：不可解析的 pending 信封触发反熵活锁。`_membership_at` 对每个 pending 行的
  token 都全量重放 meta 历史，且每个 owner 候选都重建整个 trial Doc（二次方）；
  失败结果不被记忆，反熵 tick 每 ~2 分钟重算一遍，daemon 维护线程被占死
  （`orgfs.anti_entropy` elapsedMs 60s-122s，全部 `org` IPC 超时）。现在单个 token
  只线性重放一次（trial 走 scratch Doc，仅被接受的更新落到权威 probe Doc；推进过
  scratch 的失败用合并基线回滚），不可解析 token 以内存 memo（meta 历史头
  = count+max rowid）记住，meta 有新提交才重评；drain 增加 5s 可注入时间预算，
  超预算停止并在下个 tick 继续（限频告警 `orgfs pending drain deferred by budget`）。
  准入决策不变：可解析行的受理结果与原实现一致（差分等价测试），不可解析行仍按
  原规则老化。实测（601 commits × 50 pending，隔离 HOME）：首个 tick
  apply_update 从 1,496,350 降到 1,203（~0.05s），第二个 tick 为 0。
  证据：`docs/notes/orgfs-pending-replay-fix-2026-11-02.md`。
- CI: release/v0.5.5.3 基线转绿——登记 #2228 新增的 `daemon/impl/runtime/batch_claims.py` 原生模块；cell 运行态契约里的 `providerBlocked` / `provider-block` 按精确标识加入 provider 词白名单（dev 上由 #2203 统一改名，随晋升一起处理）。
- daemon: 修复已接受的 steer 不释放 managed batch claim 导致该 agent 后续消息全部被阻塞的事故
  （2026-10-31 e2e-verifier 29 条 pending）。accepted steer 现在会结算被 steer 的 claim
  并盖 fetch 戳（不重发已注入的消息）；connector 停止/代际变更、成员全部被消费、以及
  10 分钟无进展租约都会在 reconcile 中丢弃陈旧 claim（`dispatch.batch_claim.*` 日志）。
  `agent top` 暴露 `claimedBy` / `claimAgeMs`。
- daemon: 修复 headless worker 的 inbox batch 超过 inline 限额时 `SpillUnavailable`
  使 dispatch 永久停摆的问题：runtime dispatch 现在传入 agent 私有 state root 以落盘
  spill；无法落盘时回退为逐条投递并记 `harness.batch.spill_fallback`，不再让整个
  tick 失败。
- 飞书 adapter：daemon 停机或断线期间发来的消息不再晚到或乱序。重连后按每个会话上次持久观测到的位置补拉（有上限，超出时发 backfill.truncated 事件），补拉的消息按平台时间排在同一会话的新消息之前；待重投请求按时间分页并保证每轮有进展，持续失败的请求转入死信，不再卡住整个会话。
- 飞书 adapter：live 消息触发的 inline 补拉不再在串行 inbound effect lane 上直接调用历史接口 —— 一
  次挂起的历史拉取曾让所有会话的 live 注入无限期停摆（waiter 30s 放弃但 lane 仍被占用）。现在历史
  拉取走独立线程，lane 至多等待 inline 预算（25s，与调度路径 30s 同量级，SDK 客户端另加显式逐请求
  HTTP 超时）。超时后该会话暂停注入而不是乱序放行：live 消息转为 `backfill-unavailable` 死信并拒绝
  ack（平台重投托底），观测水位与 epoch 标记均不推进；detached finisher 把最终返回的批次连同被暂挂
  的消息一起经 `ordered` 在 lane 上按序补放（该会话恢复顺序，其余会话不受影响）；无法完成时记
  `lark.backfill.async-deferred`，由重投与下次调度扫收回。
- 飞书 adapter：空闲健康扫描的补拉窗口现在锚定到每会话最后一次干净补拉的位置
  （`chat_reconciliations` 表），不再随后续 live 消息推进的观测水位前移；此前一个早于水位且超出
  30 分钟回看窗口的同 epoch 缺口会被永久丢失。
- CI: 同步 dev 的 #2219/#2220 进 0.5.5.3 集成分支——CI 门禁子进程转发不含凭据的出口代理并设 `UV_HTTP_TIMEOUT`，pr-hygiene 在首个 `uv run` 前把 uv 缓存指向共享 CI 缓存；此前所有 PR 的 pr-hygiene/unit 因直连 PyPI 超时而红。
- org 加入：成员操作按操作分别设超时预算（兑换 65 s、单段 35 s、本地 10 s、整个加入共享 105 s、IPC join/execute 110 s、CLI 120 s），各层取同一张预算表，不再被 10 s 的 IPC 默认值或 90 s 的服务端上限提前截断；超时报错带 operation/leg/budgetSeconds（及 sidecar 的 bytesReceived），长时间加入在 stderr 显示阶段进度；OrgFS 生命周期等待改为有界（120 s 兜底并记事件）。修复只能走 HTTP 代理的机器上 `org join` 报 MEMBERSHIP_SIDECAR_TIMEOUT 的 Python 侧原因。
- Codex 交互载体：恢复、入队及原生注入前核对 daemon inbox，清扫已全部结算的 FETCHED 批次；
  混合批次由 daemon 重放游标，仅展示未结算消息，覆盖 FIFO 等待期间的外部结算。
  TURN_STARTED 连续 900 秒无可观察进展后转为 STATUS_UNKNOWN 并释放本地队列；原生通知或 turn 内容变化会续期，
  不中断原生 turn、不代替用户 ack/final。合成事实被 actor 过载且 durable 事实中继满员丢弃时，
  截止对账按次重推（runtime 侧 expected_stage/attempt 护栏使重复成空操作），
  结算路径的一次性 pending 标记也只在中继受理后落定，被丢弃的 kept/settled/status-unknown 事实在压力缓解后重试，
  不再把泵永久卡在 FINAL_OBSERVED。新增恢复、过滤、清扫及对账日志。
- CI: `scripts/ci/pytest_impact.py` 现在把 `tests.yml` 声明的 6 个 `HYPRIAL_*` 测试控制变量
  （含 `HYPRIAL_BIN`，`contract/cli-blackbox/run.py` 靠它选择可执行文件）显式传给 pytest 子进程；
  此前它们被不可信代码白名单拦掉，sandbox 屏蔽与等待预算在 `pytest_impact` 入口下失效。
- cli: CLI 冷启动不再拖入 daemon 运行时模块——`daemon/service` 的两个 USER_BUS 原因常量改为比较处
  懒加载，`import hyprial.cli` 的 daemon 模块计数回到 0（#2204 同源修复）。
- login: 设备流提示中的注册链接改为跟随已解析的 issuer 域名推导，自建 auth 域不再硬编码指向
  `auth.hyprial.com`。
- daemon: 会话观测的身份裁剪改用内核 `parse_agent_uri` 原语（不再 `startswith("agent:")`）；系统通知
  ack 改用注册的 `AMBIGUOUS_TARGET` 错误码常量比较；turn authority 的 whole-session 来源表移入
  harness capability 表（消除 `claude-channel` 字面量）。

## [Unreleased]

自 v0.4.6 起的 dev 工作（含深模块仓库重构，见 [docs/design/refactor-plan-deep-modules-2026-10-01.md](docs/design/refactor-plan-deep-modules-2026-10-01.md)）；
条目随下次发版归档。

### Changed
- daemon 状态面改为只读投影：`ps`、`hosts` 与健康状态不再触发路由清理或网络探测；
  `refresh:true` 仅排队并合并后台刷新，受理字段位于 `refresh` 对象，投影报告操作结算。
  host 行新增 `observedAtMs`、`ageMs`、`stale` 与 `lastError`，证据过期时报告
  `unknown`；本版本没有 tailnet 状态生产者并显式报告 `unsupported`，不伪造在线或刷新结果。
- cli: `hyprial agent start claude -- --allowedTools/--disallowedTools …` 现在会拒绝两类"被接受但不按字面生效"的权限规则
  （此前静默接受）：① `Write`/`MultiEdit`/`NotebookEdit`/`Glob` 的路径规则（Claude Code 只对 `Edit(...)`/`Read(...)` 做文件路径匹配，
  这些规则永不匹配；`Edit` 覆盖前三者，`Read` 覆盖 `Glob`，只写工具名则匹配该工具的全部使用）；② 单斜杠的绝对路径规则
  （CLI 参数里 `Edit(/x/**)` 锚在工作目录，绝对路径需写 `Edit(//x/**)`）——仅当根下存在 `/x` 且工作目录下不存在 `x` 时拒绝，
  两处都存在或工作目录未知时只告警。已存入 agent 记录、由 restore/wake 回放的旧参数不经此检查。判定依赖宿主文件系统。
  升级后原先能启动的命令若带上述规则会被拒，按提示改写即可（这些规则本来就没有生效）。

### Fixed
- sidecar（Tailcat）在必须经 HTTP 代理出网的环境（如云电脑）连不上 DERP：daemon 不再从 sidecar
  环境剔除 `HTTP(S)_PROXY`/`NO_PROXY`（仍剔除 `ALL_PROXY` 与 `TS_*`），DERP map 与 DERP 连接
  按代理以主机名发 CONNECT，不回落直连。组织加入（invite-redeem）
  在未修改的 `tailscale.com` 依赖上按区域共享 30 秒预算（为健康握手保留至多 10 秒），
  并在同一引擎内缓存 10 分钟有效的健康区域提示（按 authority 公钥 + DERP map 摘要
  限定，至多 128 条，不持久化），后续交换优先尝试已知健康区域，不再重复被拒区域。
  控制帧必须收到换行才算完整：超时/EOF 截断的响应返回 `AUTHORITY_UNREACHABLE`
  并附可选 `bytesReceived` 字节计数（0..1048577，仅 `stage=control` 且 cause 为
  `deadline-exceeded`/`transport-failed`），不再误报 `BAD_REQUEST`；完整但畸形的帧
  仍返回 `BAD_REQUEST`。已知边界（KP-001，见 `docs/reference/known-problems.md`）：
  被拒或挂住的候选仍会耗尽自己的份额。多节点区域里走代理的节点各自成为一个候选，
  首节点挂住时同区域的健康节点仍可在 30 秒内接上（`mixed-node-hung` 约 26 秒）；
  直连节点（无有效代理或命中 `NO_PROXY`）保持原来的整区候选，未设代理时行为不变。候选 tailscale
  补丁（`sidecar/hyprial-tailcat/patches/`）本版本不包含、不激活。
- orgfs：受保护目录文档 id 随真实用户名、设备名变长（`group-internal` 的邀请 id 达到 253 字符），
  超过本地文件名上限，导致 `hyprial org invite` 失败。本地文档文件与 replica 文档目录
  改为按 id 的哈希命名（`docs/h-<hash>.loro`，replica 下为 `h-<hash>/`），id 本身写在文件头
  或 `.docid` 里。id 编码与线上协议不变。

### Upgrade notes
- ⚠️ **组织里的每一台设备都升级到 0.5.3 之后，才能创建邀请或其他长 id 文档。**
  0.5.2 的节点仍会拒收超过 240 字符的受保护文档 id。由于提交序号按（writer, doc）递增，
  被拒的那一条之后，该 writer 的所有树提交都会在这个节点上停住（`seq > next_seq` 被挂起），
  整个空间不再收敛，影响的不止那一条邀请。
- ⚠️ 升级后首次打开 orgfs 时，会把旧的 `docs/<id>.loro` 文件和 replica 文档目录一次性迁移到
  哈希命名，迁移不可逆。`hyprial upgrade` 不会备份状态：**升级前请自行备份 `state/orgfs/`；
  回滚到 0.5.2 时，必须先恢复这份备份。** 没有恢复就回滚的话，0.5.2 会在打开 store 时
  以 StoreError（invalid document id）明确报错，而不是把文档当作不存在。

### Removed
- 退役 tsnet 边车（`sidecar/hyprial-tsnet`）及其发布/打包/CI 引用（sidecar.yml、
  tsnet-windows.yml、E2E-022）；网络连接改用 Tailcat，新边车
  `sidecar/hyprial-tailcat` 编译后随 wheel 分发（`hyprial/_bin/hyprial-tailcat`），
  组织网络使用 `hyprial org create/invite/join`。

## [0.5.5.2.3] - 2026-10-31

替代未能发全的 0.5.5.2.2：修好让它卡住的两处发布流水线守卫。0.5.5.2.2 的代码已推到 GitHub，但 stable/nightly 轨道没有推进，R2 和 registry 都没有它的 wheel。产品代码与 0.5.5.2.2 相同。

### Fixed
- 发布后的公网安装校验遇到传输失败（如 TLS 断连）时先退避重试；仍失败时报告"无法核实"（`unverifiable(fetch_failed)`），不再误报"tag 不存在"。真的 404 仍判为不存在。
- 重新发布一个已存在的不可变 `v*` tag 时，如果它就是目标 main，且内容树与本次快照相同，视为"已发布"并继续推进轨道；内容不同仍然拒绝。此前一次网络抖动就会让该版本永远无法补完发布。

### Upgrade notes
- 从 0.5.5.2 或更早版本升级时，旧版本还不认五段式 tag，需要显式指定：`hyprial daemon upgrade --tag v0.5.5.2.3`。

## [0.5.5.2.2] - 2026-10-30

替代未能发出的 0.5.5.2.1：在它的基础上修好发布链路。0.5.5.2.1 的 tag 已存在，但 wheel 没有构建出来，GitHub、R2 和 registry 都没有它的产物。产品代码与 0.5.5.2.1 相同。

### Fixed
- 依赖改从官方 PyPI 解析，删除清华镜像（#2171）。CI 代理到 `mirrors.tuna.tsinghua.edu.cn` 时好时坏，0.5.5.2.1 构建时解析 `hatchling` 超时，wheel 没有产出。两个 lock 文件只换下载地址，包的版本和哈希不变。
- 所有 upload/download-artifact 步骤清空代理变量，直连 runner 的 artifact 服务（#2152）。`upload-artifact@v3` 不认 `NO_PROXY`，走代理后连接被断开，上传失败。

### Upgrade notes
- 从 0.5.5.2 或更早版本升级时，旧版本仍不认识五段式 tag，需要显式指定：`hyprial daemon upgrade --tag v0.5.5.2.2`。

## [0.5.5.2.1] - 2026-10-30

0.5.5.2 的单项 hotfix（Allen 2026-10-30 指示）：让默认升级能选中 hotfix 版本 tag。

### Fixed
- `hyprial daemon upgrade` 与自动升级定时器不带 `--tag` 时只认三段式 `vX.Y.Z` tag，
  忽略 hotfix tag（四段的 `v0.5.5.1`、`v0.5.5.2`，五段的 `v0.5.5.2.1`）。已装 0.5.5.1.1 的
  机器因此把"最新"解析为 `v0.5.5`，被防降级拒绝，升级静默不执行。现接受三段及以上（#2167）。
- main-release 的 VERSION 校验放宽到五段，hotfix 的 hotfix（如本版本 0.5.5.2.1）可以正常打 tag。

### Upgrade notes
- 从 0.5.5.2 或更早升级时，本机旧版本仍带这个 bug，默认 upgrade 选不到本版本；
  请显式指定：`hyprial daemon upgrade --tag v0.5.5.2.1`。之后的升级不再需要 `--tag`。

## [0.5.5.2] - 2026-10-30

0.5.5.1.1 之后的修复版：针对 hq 生产 daemon 主循环过载（OrgFS 大 journal、投递批次、
Codex 状态轮询、PAC 时钟）与 launchd 重启循环，另含 Claude 默认凭据与 org 邀请短链。
只收入经独立评审 ACCEPT 的修复（Allen 2026-10-29）。

### Added
- `hyprial org invite` 在登录态下自动把长邀请链接换成 `https://hyprial.ai/j` 短链：
  人类输出以短链为主、长链在下方，`--json` 增加 `short`；短链不可用/超时（3s 总预算）/未登录时
  仍只出长链，`short:null`，stderr 给出 `SHORT_LINK_*` 原因码，退出码不变。短链同样是凭据，
  保留「私发，勿贴群」。见 [contract/wire/org-invite-short-link.md](contract/wire/org-invite-short-link.md)（#2058）。
- Claude 默认凭据：`hyprial agent provider login claude --setup-token --actor <owner> --default`
  从非 TTY stdin 读入 `claude setup-token` 生成的长期 token，只存于 owner 的 SQLite；
  此后新建的 Claude agent（CLI、PAC、lifecycle 同一规则）在创建事务中获得可见、可撤销的
  `default-claude` grant。新增 `hyprial agent secret ls`，`agent secret grant` 新增 `--owner-agent`，
  `agent provider default [--clear]` 覆盖 PAC。见
  [docs/design/claude-default-credential-2026-10-28.md](docs/design/claude-default-credential-2026-10-28.md)
  与 [docs/guides/agent-secrets.md](docs/guides/agent-secrets.md)（#2042）。
- `scripts/orgfs_compact_journal.py`：`--dry-run` 只读报告各文档可回收的行数/字节与协议风险；
  `--apply` 须在 daemon 停止时执行，先备份整个空间目录，创建各文档完整 snapshot、发布
  `snapshotPoints`、删除已被覆盖的广播历史并校验物化 hash，任一步失败按备份恢复；必须显式传
  `--accept-lagging-peers`，确认接受离线老节点追不上、被误拒成员需重新邀请（#2069）。

### Changed
- OrgFS 启动只重放 checkpoint 之后的 journal：落盘 `.loro` 时记录每个文档的
  (rowid, state vector)，启动时状态匹配则跳过已物化的历史，checkpoint 缺失/过期/不可能时
  回退全量重放。历史不删除，同步协议不变（#2069）。
- OrgFS 写入的回滚快照只导出本次操作涉及的内容文档，单次写入不再随空间大小线性导出
  （合成基准 N=5000：153.8 → 51.8 ms，峰值内存 86.2 → 3.9 MB）（#2055）。
- 投递：每个 worker 独占一条 effect lane，一个卡住的 worker 不再占满共享线程、饿死其它 worker；
  批次只在 worker 接受后才标记 fetched，批次每个成员都经 HarnessRuntimeActor 预留和 fence，
  跨 worker 移除/重启与迟到的 claim 不会重复执行已结算的消息（#2076、#2088）。
- 投递：每个 agent 的单轮 dispatch 有上限，超时的 agent 本轮跳过、记一条
  `harness.dispatch.agent_timeout`，消息留在 pending 下轮重试；workflow 节点的 turn 拒绝 steer
  （`WORKFLOW_TURN_NOT_STEERABLE`），期间到达的消息排队到节点结束后再执行，不再打断节点（#2076）。
- PAC：一次 tick 的时钟任务批量执行（约 28 ms/graph → 1.6 ms），单个 graph 的失败只回滚该 graph
  并报 `PAC_CLOCK_TICK_FAILED`，其余 graph 继续；只有存储不可用连续 3 次才整体停止（#2082）。
- org：`create/invite/join/execute/leave/remove/delete` 的管理 IPC 只向 forwarding-reconcile
  cadence 提交一次刷新，不在请求线程内同步执行 reconcile（#2074，并入 0.5.5.1.1 的 #2061 机制）。
- 发布规则集中到 [docs/guides/release-rules.md](docs/guides/release-rules.md)，自 0.5.5.2 起生效：
  internal 只在 dev 单元测试全绿后移动；dev→main 推送更新 stable 并按 `VERSION` 打 tag；
  所有公开轨道必须附带 wheel 并镜像到 R2，缺失即失败（#2041）。

### Fixed
- daemon / launchd：按 plist 实际运行的命令（严格白名单，含 `bash -lc "ulimit -n N; exec <hyprial> daemon run"`
  包装）识别 launchd 监管的 daemon，不再只认 `com.hyprial.hyprial.daemon` 标签；
  `daemon restart` / `upgrade` 对识别到的标签执行 `launchctl kickstart -k`，`status.unit` 报告真实标签，
  不再另起一个无监管的 daemon（#2084，经 #2085 进入发布分支）。
- daemon：`daemon run` 拿不到锁且持有者健康应答时阻塞等待，不再退出交给 KeepAlive 反复重启
  （生产曾约 2900 次）；等待进度写 stderr，`--json` 下为 `{"event":"daemon.lock.waiting","holderPid":N}`；
  无人应答时仍快速失败（#2084）。
- Codex：carrier 的 `message.status` 轮询每条消息最多 8+1 次（生产曾 30 分钟 2478 次），尝试计数
  先写后查且只增不减；耗尽后以 `worker.carrier.settlement.status_unknown` 结算并留墓碑，
  跨 native session 生效（#2081）。
- PAC：daemon 重启后，仍处于 `requested` 的已受理请求会重新拉起 worker，不再被判为终态丢失（#1779，#2082）。
- `hyprial agent start` 在 harness 启动后被其自身前置条件拒绝（如 Pi P2 信任）时不再以 rc=0 报成功：
  返回 `HARNESS_START_FAILED`，`--json` 输出 `ok:false` 且退出码非 0；观察窗内既未就绪也未报错时
  输出 `readiness: unconfirmed`，不冒充已就绪（#2048）。
- 长期驻留的 Claude MCP channel 进程若由旧 hyprial 构建启动，注册应答会标记
  `MCP_CHANNEL_STALE`（含重启指引），`ps` 暴露 `channelBuildStale`，doctor 告警；
  不再让旧工具 schema 以 `SUMMARY_REQUIRED` 的形式静默失败（#2048）。
- Claude：PAC/工作流启动或启动时创建的 Claude agent 不再继承 daemon 宿主的 ambient 登录；
  既无显式 auth 也无自有私密 native 根时，在 spawn 前以永久错误 `CLAUDE_CREDENTIAL_MISSING`
  拒绝（PAC 节点 `reasonRef: pac:launch_failed:CLAUDE_CREDENTIAL_MISSING`）。P1 Claude 使用自有
  `CLAUDE_CONFIG_DIR` 与会话目录；操作员创建的常驻 PTY 不受影响，来源记为 `ambient`（#2042）。
- session：路由 I/O 不再持有全局投影锁与 Agent actor，慢路由不再阻塞其它 agent 的心跳；
  接管期间的过期确认与过期路由声明会被撤回（#2056）。`binding_revision=0` 按未托管处理，
  迁移后的会话可正确刷新（#2054）。
- orgfs：压缩备份改到 `state/orgfs/compaction-backups/`，不再落进 `spaces/` 被 daemon 当作第二个
  空间；启动对 `spaces/` 下非 UUID 目录报 `ORGFS_INVALID_SPACE_DIRECTORY` 并点名路径；
  `--apply` 结束前用正常 store-open 路径重开压缩结果，失败自动恢复备份（#2069、#2089）。
- org：`hyprial org network` 的 directory 面板只读 OrgFS 的 pending 表，不再为了取
  parked commits 去全量扫描并逐条解码 commit 历史；journal 很大的空间上不再超时、同事互相可见
  （合成 30 万条 commit：解码次数 300,001 → 0，<1s）（#2102）。
- CI / 测试：artifact 上传限时 3 分钟并在 summary 中可见（#2070）；`pull_request` 上下文不再在非 PR
  事件中展开（#2073）；NO_PROXY、已退役 GUI fixture、release 目标的 PR CI 与若干测试清单/计数同步
  （#2029、#2033、#2088、#2089、#2106 等）。

### Upgrade notes
- ⚠️ 依赖宿主 Claude 登录运行的 PAC/工作流 Claude worker 升级后会以 `CLAUDE_CREDENTIAL_MISSING`
  启动失败。升级前先在可信终端运行 `claude setup-token`，再执行
  `hyprial agent provider login claude --setup-token --actor <owner> --default`；已存在的 agent
  不会自动补发 grant，需用 `agent secret grant ... --source agent-shared --owner-agent <owner>` 单独授权。
- 升级后重启长期驻留的 Claude 会话，否则其 MCP channel 会报 `MCP_CHANNEL_STALE`。
- `orgfs_compact_journal.py --apply` 产生的 baseline snapshot **本版本仍不能被落后节点安装**：
  baseline snapshot 同步扩展只有设计草案
  （[docs/design/orgfs-baseline-snapshot-2026-10-29.md](docs/design/orgfs-baseline-snapshot-2026-10-29.md)），
  未在 0.5.5.2 实现。执行 `--apply` 等于接受落后节点停在 `snapshot-barrier`、相关成员需重新邀请。

### Known issues
- 发布门禁中有 8 个测试在本版本之前已经失败（本版本未新增失败），计划在 0.5.5.3 修复或移除。
- `#2076` 的批次 fence 只覆盖受管 daemon 投递路径；Codex 交互式 carrier 仍依赖自身的
  per-generation fence，二者是否等价在 0.5.5.3 跟进。
- checkpoint 重放只缩短启动；OrgFS 的树/节点/历史遍历每次写入仍为 O(N)，在 journal 很大的空间上
  `hyprial fs space status` 等读取仍可能较慢。
- 直连 authority 的成员表映射实测约 10.5s，略超请求路径 10s 预算（0.5.5.1.1 已知，未在本版本解决）。

## [0.5.5.1.1] - 2026-10-28

0.5.5.1 的生产热修（Allen 2026-10-28 授权 fix-forward）：升级到 0.5.5.1 后 daemon 因 tailnet 成员一次性迁移进入重启循环（每次约 100 s 满 CPU，1h49m 内 271 次失败启动，产品面不可见）。

### Fixed
- 迁移不再因不该迁移的空间失败：`directory/org.json` 为 `deleted` 的 org、已离开的 org、本机 owner 已不是成员的空间、零设备行的探针 org 一律跳过，各记一条 `membership.migration.skipped {spaceId, org, reason}`，不写回执，下次启动再评估。
- authority 选择改为 tailcat 层本地事实：有 settings pin 就用 pin；否则唯一 owner 行；多 owner 行时仅当 `forwarding.tailnet.authorityDefault:"self"`（新设置，默认关闭）才选本机；否则 `MIGRATION_AUTHORITY_MISSING`，消息列出候选 deviceId（不含地址/密钥）和修复命令。
- `membership-init` 前按边车规则做行卫生：owner/deviceId 非空、两把 `nodekey:` 可解析且不同、地址非空；与其它 deviceId 共享任一密钥的行只保留 `updatedAt` 最新一条（损坏的 `updatedAt` 视为最旧且不再拒绝整行），丢弃项写入回执 `diagnostics.dropped/droppedDeviceIds`；回执 `params` 即实际发送的请求，重命名主机残留旧行不再触发 `MEMBER_CONFLICT` 重放循环。
- 边车错误码可见：`daemon.start.failed` 新增 `code`；回执记录 `lastError:{code,at}`，成功后清除。消息文本仍不含密钥。
- 快失败不发热：迁移先用本地文件（设备记录、settings、回执、成员存储文件、OrgFS 行）做出决定，再启动边车；拒绝时启动阶段几秒内结束，不经边车握手。
- 重启循环退避：连续失败计数持久化到 `state/daemon-start-failures.json`（0600，`count/firstAt/lastAt/lastCode/lastPhase`），`daemon.start.failed` 带 `attempt`/`backoffMs`（5s·2^(n-1)，上限 60s）；`HYPRIAL_SERVICE_MANAGED=1` 的受管 daemon 自第 2 次失败起退避后再退出（SIGTERM 可打断），`daemon.ready` 清零。launchd `ThrottleInterval` 5s 叠加其上。
- org：`org.invite`（以及 create/join/execute/leave/remove/delete）在写入落盘后立即回复，邀请后的 forwarding reconcile（`membership-status` + slot 重绑，经串行 `forwarding-sidecar-io`）改为提交到 daemon 自有的 `forwarding-reconcile` cadence 异步执行，并以 `org.<op>.reconcile` 事件（ok / code / durationMs / org / user）记录结果；回复载荷不变。
- forwarding：来自 IPC 请求路径的边车 membership 调用（`invite-create`、`invite-redeem`、CLI 读取的 `membership-status`）等待上限从 35s 收紧到 10s；超时以稳定错误码 `MEMBERSHIP_SIDECAR_TIMEOUT` 返回（不再是 handler 挂起后的无码 DAEMON_ERROR），边车自身的错误码原样到达 `--json` 的 `code`。35s 仅保留给启动/后台工作；Go 边车不改。

### Added
- `hyprial org pin-authority <spaceId> --device <deviceId> [--control-port 7448] [--yes] [--json]`：离线为尚未迁移的 tailnet 写 authority pin（daemon 停或不停都可），从本地目录行解析 owner/密钥/地址，只写 `settings.json`；成员表已存在时拒绝 `MEMBERSHIP_ALREADY_INITIALIZED`（改用 `org repin`），已有不同 pin 时拒绝 `AUTHORITY_PIN_MISMATCH`。
- `hyprial org migration-status [--json]`：离线列出每个 org space 的 pin、回执状态、accepted/rejected/dropped 行、skipReason、边车表是否存在、最后边车错误码。
- `hyprial doctor` 新增 `daemon-start-failures` 行（有计数文件时出现；<3 warn，≥3 fail，附修复命令）。
- `contract/e2e/run-tailcat-membership-hardening.py`：真实边车下复现生产形状（多设备 owner + 已删除 org + 探针 org）的两节点升级场景。

### 操作手册（升级 0.5.5 → 0.5.5.1.1 时 daemon 起不来）
1. `hyprial doctor`：看 `daemon-start-failures` 的 `lastCode`；或读 `logs/daemon.jsonl` 最后一条 `daemon.start.failed` 的 `code`。
2. `hyprial org migration-status --json`：找 `pinned:false` 且无 `skipReason` 的 org space，核对 `rows.accepted` 中的 owner 设备候选与 `rows.dropped`。
3. 在**每个**节点执行 `hyprial org pin-authority <spaceId> --device <ownerDeviceId> --yes`（pin 同一台 owner 设备）。
   成员节点也必须 pin：authority 是本机事实，成员节点看到的 owner 行只是目录数据，即使只有一行也不会自动选用（第 2 轮评审）。
4. 先启动被 pin 的 authority 设备的 daemon，再启动其它节点；用 `hyprial org migration-status` 确认 `receipt.state:"complete"`，`daemon-start-failures` 行消失。
. 0.5.5.1 留下的 `pending` 回执不会被原样重放：每次启动按当前本地文件重算（跳过规则、authority、行卫生）并改写回执，旧请求保留在 `diagnostics.supersededParams`；重算结果为跳过的空间，回执改为 `state:"skipped"`（保留文件，`skipReason`/`skippedAt`），`org migration-status` 可见。只有 `complete` 回执是权威的。

### Upgrade notes
- 若此前因超时重试过 `org invite`，多出的 pending 邀请文档仍留在 org 空间的 `directory/invites/` 下（按 token 摘要命名），到 `expiresAt` 自动失效；本修复不会自动删除它们。

## [0.5.5.1] - 2026-10-28

0.5.5 的性能与修复补丁版，完整说明见 [CHANGELOG-v0.5.5.1.md](docs/notes/changelog/CHANGELOG-v0.5.5.1.md)。

- daemon 空闲开销大幅下降：inbox 只在真实变化时发布，PAC `WorkflowTick` 只处理有变化或到期的 workflow，macOS 进程探测按 PID 缓存（#1891）。
- `hyprial upgrade` / `daemon restart` 只在见到新 daemon（新 pid、新 epoch、已安装版本）后才报成功，启动等待改为 120s 硬上限（#1913）。
- Pi / Codex harness 启动修复，并以真实 start→hello→reply 回环验证；新增 `agent start --effort`（#1911）。
- 飞书 adapter 增删后自动重载 daemon；合并转发消息中的附件可下载（#1910、#1906）。
- 升级注意：重启后短时间内重型查询可能返回 `DAEMON_RESTORING`，稍候重试即可。
- 凭据：agent secret 授权统一以 SQLite 为唯一权威，旧 `secrets/` 文件在启动时按来源一次性迁移，`agent secret set` 后立即对 worker 生效（#2012）。
- 飞书 adapter 进入隔离后按慢速退避自动重试（最长 10 分钟一次），不再永久停止；`adapter status/list` 新增 `nextRetryAt`、`retryAttempt`（#2013）。
- Tailcat：打破准入与复制之间的循环依赖。成员关系由边车持有，启动顺序固定为 tailnet → Zenoh → OrgFS，删除成员需二次确认；新增离线修复命令 `hyprial org repin`（#2021）。
- 同步 dev：旧 GUI 已退役，由新的 GUI 重建线取代（#2009）。
- 升级注意：Tailcat 边车协议升级（stdio v6），组织内所有设备需在同一维护窗口升级；升级前发出的邀请失效，需重新邀请。
- 已知问题：在投递前钩子配置尚未加载完的时刻，消息可能停留在收件箱中不再投递（自 0.5.5 起即存在）。修复随下一个 internal 构建发布。

## [0.4.6] - 2026-09-16

公网发布轨首发 + GUI 源码合仓 + 安装/更新链路加固（119 PR / 152 非合并提交）。

### Added
- GUI 源码合仓 `src/gui/`（独立 Node 工程），发布包确定性构建 + 逐文件 hash 校验安装；
  安装与升级改走公网发布仓（`Hyprial/hyprial` 的 `internal` 移动轨 tag）。
- `autoupdate` 改为默认关闭，由 `settings.json` 的 `autoUpgrade` 开关控制。
- contract-impact 门支持 deleted-side-only extraction（内部端点取数）。

### Fixed
- installers（P2a 系列）：悬空 symlink 报 `broken_links`、manifest+回执原子写、
  扫描跟随符号链接、拒绝带 userinfo 的 URL、打破 INSTALL_SOURCE_MISSING 死循环。
- login：显式请求 join 失败时，首访路径同样以 exit 1 结束。
- PAC：schema-8 就地改写 legacy owner（cursor-neutral）；managed-worker 标记（M2）；
  契约 additive-optional（M3）。
- GUI 发布快照清除全部内网标识（含 CI 守卫与 src/gui/{docs,vendor} 排除）。
- daemon：hosts/org.fetch 先按 host 身份过滤、共享一份 worker-status 快照；
  host 类 survivor 的 liveness 复查逐字节保真。
- CI：按依赖 scope 裁剪 GUI 集成检查、dev promotion 独立认证、后端 lint 限定组件。

## [0.4.5] - 2026-09-13

仓库改名 hyprial 收束 + daemon 就绪三阶段（U0）+ forwarding/插件/工作流加固（236 PR / 566 非合并提交）。

### Added
- workflow：暴露 sender 维度的 node 观测。
- daemon 就绪三阶段集成（PR #332）：ping/accept 提前、ps 快照、事件化、retrying/failed 状态、
  白名单对账、僵尸行清理；U0c：重启补偿被打断的 saga，回执不跨 generation。

### Changed
- 退役的 `H2B_*` 环境变量改为当场拒绝；改名收束（`bin/h2b-tsnet` → `bin/hyprial-tsnet`、
  bin 仓引用指向 hyprial-tsnet-bin、pytest 插件名 hyprial-ci-profile 等）。
- 旧 home 迁移收窄为仅改名路径。
- cli：用户错误只打一行；内部错误保留完整栈。

### Fixed
- forwarding：teardown 分类、启动失败上浮、显式 listener 要求、SIGTERM drain。
- lark：阻塞式启动失败时释放 worker 资源。
- migration：边车改名失败给出独立 reason（不再借用 rename-failed）。

### 专题记录（原始归档）

### Desired-state start semantics (U0b, PR #332 fix set)

- `h2b start` writes its desired-state row only AFTER the process is
  actually up: **a daemon restart no longer re-runs starts that never
  succeeded** (no trace is left for them to be read from), and a daemon
  crash anywhere inside the start/retry window leaves no ghost row.
- A connector that ran before but failed to come back is persisted as
  `failed` — displayed, never auto-retried — until an explicit start
  succeeds. Restore re-runs only rows whose last result was `running`.
- A daemon restart starts each declared connector through exactly one
  owner: the restore batch (U0BRACE). The construction-time create-saga
  resubmission — which used the same selection predicate as restore and
  therefore put two submitters on every non-Lark running row — is gone;
  a restart can no longer produce two processes for one connector, and
  restart-time `harness.ensure` evidence in the journal is no longer
  refreshed (the journal's historical effects remain permanent and keep
  backing the migration's veteran rescue).
- Pre-U0b databases are migrated in one shot: `harnesses.status` is
  backfilled to `running`; rows whose only receipt evidence is an
  incomplete harness receipt (a start frozen mid-flight by a crash,
  never confirmed) are removed — no trace, per the same semantics.

## [0.4.3] - 2026-08-28

（40 PR / 239 非合并提交）

### Added
- e2e：Lark stand-in（真实 SDK client + stand-in websocket），入站闭环可测。
- lark：单一出处决定本进程与哪个 Lark host 通信。
- start：从 `H2B_HOME`（而非启动目录）加载 MCP server 与 skill。

### Fixed
- daemon：restore 不再阻塞 readiness、不再为从未联系的 harness 报失败、
  quarantine 由失败计数推导且可过期、恢复此前无人恢复的声明 harness。
- workflow：run 结束必须终止（报告发不出也终止）、report 重试有界。
- tmux：pane 生命周期跨信号保留、detached 启动清理由 pane 负责、cleanup grace 证据化。
- mcp：wake 与客户端退出竞争时上报而非杀掉 channel。
- lark：actor 不停则不关闭其 effect store。
- plugins：MCPREV 能力与生命周期阻塞项。

### Removed

### Actor big-bang cleanup

- Retire the unused IRC `channel join/part/list` CLI and daemon IPC stubs.
- Remove the unconnected Squire feature prototype, future-only room mirror
  protocol, duplicate Squire Typer registration, and unreachable
  `NOT_IMPLEMENTED` CLI rendering branch.

## [0.4.2] - 2026-08-24

开发线快照 dev1–dev7（2026-08-16 → 08-23）+ 正式 0.4.2（08-24）。
注：本节"开发线记录"为当期原始专题归档（逐条含背景），其中 08-24/25 的条目与 0.4.3 窗口交叠。

### 快照摘要
- **0.4.2.dev7（08-23）**：PAC 声明式编排、跨机 transfer、进度事件全链路、TUI 三家接入落地；
  发版闸门从"连红且查不出原因"修成"绿且失败自说明"（详见 [docs/notes/changelog/CHANGELOG-v0.4.2.dev7.md](docs/notes/changelog/CHANGELOG-v0.4.2.dev7.md)）。
- **dev4–dev6（08-18/19）**：self-drive routine 纯机制 + E2E-016 端到端；
  inbox outbox 死信三缺陷（C1/C2/C3）修复；workflow retry 四护栏。
- **dev1–dev3（08-16/17）**：org 签名上下文（phase 1）、lark agent 目录与出站文件投递、
  告警终态即时反馈、connector 日志统一、升级后自动重启 daemon、DSH per-worker MCP 注入、
  DeepSeek Harness API adapter。

### 专题记录（原始归档）

### Harness provider routing

- Add public `h2b start <harness> --provider <vendor> --model <id>` options,
  persisted separately from the historical harness-valued `provider` key.
- Route `deepseek/deepseek-v4-flash` through Pi native flags, DSH
  `session.selectModel`, Claude's Anthropic-compatible environment, and
  Codex's Responses custom-provider config without storing a key in argv or
  desired state.
- Move real Pi/Claude/Codex/DSH gates onto the shared DeepSeek model so
  ChatGPT or Claude subscription quotas no longer decide release readiness.

### Development (0.4.2)

Chore (IRC codec removed, `channel:lark:` outbound retired — naming/protocol
cleanup line, PAC b8d2b52b6358 / NAMB efeeaf0a01ba / NAMT3 664a7d314947,
2026-08-24/25): the `src/h2b/proto` IRC-line codec (`irctokens`-based) is
deleted along with the `irctokens` dependency — it never had a production
caller; the real wire format is the Zenoh delivery-frame JSON envelope
(`docs/reference/proto.md`). `h2b.contracts.wire`'s frozen wire-golden contract suite no
longer round-trips fixtures through IRC lines (constructs `InboxMessage`
directly from the JSON fixture instead); three cases that existed only to pin
IRC-only guards with no production equivalent (a ChannelGateway target guard
and a hop-limit guard) are retired, 30 → 27 cases. Separately, `channel:lark:`
addressing (the Lark reply-bridge sender identity) is superseded by
`adapter:lark:` for every new mint — inbound parsing keeps accepting both
spellings permanently (existing `desired-state`/inbox/log records are never
migrated or rewritten), a CI guard now fails any new `channel:lark:` literal
outside the one legacy-spelling parse point. `claude-channel` (the
interactive-session wake mechanism) is unaffected — it was never IRC, kept
its name per Allen's ruling. See `docs/reference/naming-audit-channel-adapter-route.md`
and `docs/reference/proto.md` §5/§6 for the full naming audit and evolution rules this
line of work produced.

Version scheme rebuild (task 25, Allen 2026-08-20): the declared version is
now a manually bumped `X.Y.Z` — development `.devN` suffixes are retired.
Built wheels carry the PEP 440 local segment `X.Y.Z+g<short7>` naming their
exact source commit, stamped at build time by `scripts/build/build-wheel.sh`;
`pyproject.toml` itself always holds the plain `X.Y.Z`.  This is the
versioning half of the three-track upgrade channel rebuild
(`dev`/`nightly`/`stable` movable tag refs); track resolution in
`h2b upgrade` and the promotion automation land in follow-up changes.

Feat (IPC error-code registry, proto.md §3.7 / §9.4): every proto-facing
error code now lives as a named constant in `h2b/contracts/ipc_errors.py`
(49 codes: the daemon-minted ones with their fallbacks, the client-side
IPC transport family, and the cross-boundary codes the #283 review
surfaced — `H2B_HOME_IN_USE` and the `TARGET_SQUIRE_*` pair), and every
minting or branching site imports the registry instead of spelling the
string — the `SESSION_SUPERSEDED_CODE` precedent generalized, as the
protocol document prescribes.  Values are frozen so old peers stay
recognised; client-side-only display codes and other purely internal
exception spaces deliberately stay out (membership line documented in the
module and §3.7).  A frozen-membership test plus an AST guard
(`tests/test_ipc_error_code_registry.py`) turn red if a registered code is
ever written as a literal under `src/h2b` again.

Feat (turn timeouts retired, #277 ruling — supersedes the cap entries
below): no timeout ever kills a turn any more.  The wall-clock deadlines
are removed from managed codex, interactive codex (whose 900s cap killed
live TUI turns — half of #94) and dsh; liveness will move to
connector-side steer probing.  The quiet watch now REPORTS instead of
killing: after `idleTimeoutSeconds` (env `H2B_TURN_IDLE_TIMEOUT_SECONDS`,
default 900s, 0 = off) without a new correlated app-server notification
the worker logs `worker.turn.stalled` (re-fired per further threshold of
silence) and sends a `turn-stalled` progress observation; the first new
correlated activity logs/sends `turn-resumed` and clears it.  `h2b top`
replaces the cap-based "timeout"/age-based codex "stuck" verdicts with a
"stalled" state driven by those events.  `turnTimeoutSeconds` and
`H2B_TURN_TIMEOUT_SECONDS` still parse (existing desired-state files and
`--turn-timeout` callers must not crash) but enforce nothing.  Graceful
interrupt (explicit stop/steer), real-cause failure labels, replay
backoff and the abandoned terminal state are kept unchanged.
⚠️ KNOWN CONSEQUENCE (issue #276, now routine rather than edge): with no
cap, any turn longer than the 30-minute inbox hold TTL loses its reply —
the inbox row is pruned mid-turn and the late completion is dropped at
`_complete_harness_results`.  The work itself persists (rollout + worker
log) but the requester never hears back.  Until #276 lands (dispatch-time
TTL exemption for in-flight deliveries), raise `H2B_HOLD_TTL_MS` on
machines running long turns.

Fix (turn wall-clock cap, issue #270): the managed turn cap is now
operator-configurable — `turnTimeoutSeconds` on the launch spec, or the
`H2B_TURN_TIMEOUT_SECONDS` environment variable — and the managed headless
default rises from 900s (codex) / 600s (dsh) to 3600s; interactive sessions
keep the 900s cap because a human is waiting there.  A capped codex turn
now sends `turn/interrupt` and waits a short grace window so the app-server
closes the turn out and persists the rollout, instead of tearing the
process down mid-turn and discarding all completed work.  Failure labels
carry the real mechanism ("exceeded its Ns wall-clock cap" with elapsed
time) — the pump logs `str(exc)` rather than the bare exception class name
(the issue #81 fix point), so seven cap kills can never again all read as
"ConnectionError".  The streaming replay loop gains exponential backoff
(base doubles per failure, capped) and a per-delivery attempt budget;
exhaustion logs `worker.turn.abandoned` and surfaces a terminal FAILED
result instead of replaying the same delivery every 0.25s forever
(the observed 15-minute failure loop).  All four behaviours are
mutation-gated in `tests/test_turn_timeout_cap.py`.
Known window opened by the raised default (issue #276): the 3600s cap now
exceeds the 1800s inbox hold TTL (`DEFAULT_HOLD_TTL_MS`,
`H2B_HOLD_TTL_MS`), so a turn legitimately running 31-60 minutes can have
its inbox row pruned mid-turn — the late completion then hits
`original is None` in `_complete_harness_results` and the reply is never
sent (the work itself persists in the rollout and worker log, but the
requester never hears back).  The pre-existing 900s cap could not cross
the 30-minute TTL, so this combination is new; until #276 lands
(preferred fix: exempt in-flight deliveries from TTL at dispatch), either
keep per-spec caps at or below the TTL or raise `H2B_HOLD_TTL_MS`
alongside the cap.

Feat (activity-aware turn timeout, phase 1 — codex): a producer-local idle
lease (`idleTimeoutSeconds` on the launch spec, `H2B_TURN_IDLE_TIMEOUT_SECONDS`
env, managed codex default 900s) interrupts a turn only after that long
without any NEW app-server notification correlated to it, observed inside
the harness client — before the route-C progress channel's drop-oldest
queue and coalescing, whose silence therefore never kills anything.  Level
state (thread/read answering "inProgress") never renews the lease.  The
absolute cap is kept as pure resource insurance and both timeouts are now
disable-able with 0.  Failure labels distinguish `[hard_cap]` from
`[idle_timeout]`, each with budget and elapsed time.  Phase 2
(silent-command heartbeats for Claude/pi/DSH) is a documented follow-up.

Fix (lark adapter send observability): outbound sends now record the full
Feishu API answer — `code`, `msg`, and the native message id — as a
`lark.send` event on the adapter log, on both the success and the failure
path, so a specific `om_` can later be matched to exactly what the platform
returned (today's silent route-send loss was undiagnosable because
`send.received` carried only the h2b id and the worker/daemon dropped the
reply-bridge error string).  `_require_success` hardens the unparseable-code
case: a response whose `code` did not parse now raises the typed
`LarkApiError` (with `code`/`msg` preserved, `None` kept as JSON null)
instead of dying with a `TypeError` inside the error path.  The worker-side
`reject_delivery` no-op (`del error`) now logs the rejection reason, and the
daemon control socket logs `lark.reply_bridge.rejected` with the reason keyed
by delivery id, so a refused `harness_reply` is never silent.  The daemon's
route-send `send.received` also carries `nativeMessageId`.  Regression tests
mock a platform response that returns a message id with `code != 0` and
assert the send fails (mutation-checked: reverting to optimistic return goes
red); send-outcome logging is asserted for success and rejection, and no
secret or message body reaches any new log field.

Feat (pi interactive attach, PR2: launcher + capability unlock): `h2b start
pi --name <n>` now launches a real pi TUI attached to the Harness Network.
The launcher follows the same five phases as the Claude interactive one
(validate + agent.create, foreground spawn, attach-registration wait,
foreground wait, teardown): it owns the pi session identity (`--session-id`
and friends are rejected on the command line), injects the carrier identity
(`H2B_WORKER_ACTOR` / `H2B_WORKER_SESSION_REF` / daemon socket roots) into
the TUI environment, loads `pi_harness_attach.ts` with `--extension`, and
appends the Harness operating instructions to the system prompt.  The
registration wait is attended-aware: pi's project-trust prompt can stall
the extension before it ever registers (research finding E8), so the
deadline is 180s and the failure message names the trust prompt.  The
`--session-id` charset translation (#192) moves into
`h2b/harnesses/pi_session.py` and is now shared by the RPC boundary, the
interactive pty connector (which previously passed raw refs — the same
crash-on-colon class #192 fixed), and the interactive launcher.  The
capability table flips (`pi`, interactive): INTERACTIVE_ATTACH, WAKE_PUSH,
PROACTIVE_SEND, TOOL_INJECTION and SESSION_LIFECYCLE are native via the new
`extension` mechanism, making an attached pi TUI coordinator-capable.

Feat (pi interactive attach, PR1: carrier + daemon protocol): an interactive
pi TUI can now attach to the Harness Network.  The new
`pi_harness_attach.ts` extension is the in-process session carrier: it
registers the TUI session on `session_start` (source `pi-extension`, runtime
`pi_interactive`), polls the daemon-owned durable inbox with observation
reads (never `fetched`), performs the explicit `fetched=true` consumption
read only at the injection boundary, and injects one batched user message
via `pi.sendUserMessage(text, {deliverAs: "followUp"})` — always with
`deliverAs`, so the idle/streaming race is designed out rather than checked
away (a bare call while streaming is rejected and lost, and the `input`
event fires even for rejected injections, so neither is a delivery proof).
Completion is `message.reply` (with the settled run's final assistant text)
for request intents and `message.ack` otherwise — acceptance is never
delivery; `MESSAGE_REPLY_UNAVAILABLE` (the model replied through its own
harness_reply tool) is benign.  Fence semantics match the Claude Channel
carrier: SESSION_SUPERSEDED goes quiet, STALE_SESSION re-registers,
connection failures back off (1s base, 5s cap), `session_shutdown`
unregisters.  The daemon-side session protocol is generalized from
`claude-channel`-only to the closed carrier set
`SESSION_CARRIER_SOURCES` (register's generation contact proof, refresh,
heartbeat; the liveness lease stays claude-channel-only, other carriers
renew through fenced contact), and the shared pi daemon IPC wire is
extracted into `pi_daemon_ipc.ts` (the headless harness-bridge behavior is
unchanged).  The version-guard probes from the attach research are checked
in under `contract/workers/pi-attach-probe/` for re-runs on pi upgrades.

#96 minimal (A+B): the SENDER_UNRESOLVED refusal now also states the
consequence (an unregistered identity cannot receive replies — fact only,
#67), and `message.send` flags `replyPathUnavailable: true` when the
sender is legal but provably cannot be replied to: `user:<owner>` without
a complete squire profile (delivery would fail TARGET_SQUIRE_UNCONFIGURED)
or `channel:lark:<adapter>` with no such adapter configured here
(MESSAGE_REPLY_UNAVAILABLE).  The CLI prints the fact on stderr for
non-JSON sends.  Best-effort by design (#60): unsure shapes are never
warned and nothing is ever blocked by the warning path.


Address parsing is now single-source like address construction:
`identity.parse_agent_uri` / `parse_channel_uri` / `short_actor_name` are
the only deconstructors, `normalize_agent_recipient` moved into the
identity module, and every bare `split(":")`/`rsplit`/`startswith` parse
in `src/` is migrated (24 sites) or whitelisted with its grammar owner
noted (route:/tcp endpoint/IRC combo/process markers).  A guard test
(`test_address_parsing_guard.py`) fails CI on any new bare parse outside
the identity module, mutation-proven.  Two validations tightened as a
side effect: profile `_agent`/`_channel` now require exactly four
non-empty segments (five-segment shapes previously slipped through).


Registered agents without a connector (personas) now get daemon-backed
network presence: `agent.create` and the startup sweep declare the actor
liveliness token and hold an inbox endpoint for every registered URI, so
the cross-node twin of "self-node URIs are locally deliverable" holds —
a remote sender sees the target and the row lands in this node's durable
inbox; destroy withdraws the token, and the daemon's death takes every
promise with it.  `ps.pendingMessages` is now node-wide (all unconsumed
rows, each carrying `recipient`): post canonical-identity, traffic is
agent-addressed, and the node-scoped read reported an empty node while
rows waited under agent URIs — a blind spot that made a working
cross-node persona delivery read as a delivery failure in E2E-001.

Sender identity is now resolve-or-reject (Allen (b), superseding the
short-lived mint): an unfenced sender that does not resolve against the
local registry (agents / harness specs / interactive sessions, never
presence) is refused with `SENDER_UNRESOLVED` — facts only, nothing
queued, nothing minted.  A canonical-shaped sender must additionally name
THIS owner+node and resolve to a registered identity, closing the
foreign-URI impersonation shape (`--from agent:<other-node>:x`).
`message.reply` authors go through the same gate; reads never reject —
status/pending/ack resolve-or-verbatim and union the other eras' keys
(verbatim bare, and the mint era's local four-segment spelling, composed
through the one constructor as a read probe).  URI construction is now
unique to `identity.canonical_agent_uri`: the two real escape sites
(squire identity, org-fetch error display) route through it or display
verbatim, the two namespace-prose splices were reworded, and a CI guard
test fails on any new `f"agent:` splice in `src/` (mutation-proven).
E2E runners no longer send as bare node ids: each scenario registers a
driver persona (or full canonical URI for cross-node reads), node
presence assertions moved from `targets` to `hosts`, and
upgrade-continuity's node-to-node self-send (already rejected by the
target gate) now round-trips through a registered persona.

Fix (E2E-013 regression): the sender-canonicalizing boundary minted bare
CLI names into `agent:<owner>:<this-node>:<name>` identities, but
`LocalFirstDeliveryTransport` only delivered to *registered* actors, so
replies to minted identities were treated as remote traffic and parked in
the outbox forever — the mint/delivery layers disagreed about what is
local, and only the end-to-end gate could catch it (application-level
fixtures bypass the local-first transport entirely).  A canonical URI
whose machine segment is this node is now a local delivery object by
construction — registration governs wake/liveliness, never
deliverability; the durable inbox accepts the row and its reader collects
it later — in both `deliver` and `is_online`.  The consumer side
canonicalizes a bare `--from` the same way and unions the verbatim
spelling for pre-boundary rows (pending.list / ack / reply lookup), and
the `TARGET_UNRESOLVED` reply refusal is removed: a self-node URI is
drainable now, so the refusal had become a false rejection.  Outbox rows
parked by the regression drain through the normal retry pump once the fix
is in.

`h2b targets` no longer calls every online identity an `agent` — and no
longer lists nodes at all.  Presence carries both canonical connector URIs
and every node's bare `node_id` self-token (each daemon announces
`node_id` under the actor liveliness keyspace); stamping both `agent`
advertised nodes as connectors, and a send to one normalized back to the
bare node id, landed in the node inbox with a signed receipt, and was
never consumed — a delivered-and-receipted silent hole (Allen's 主机/
user/adapter/connector confusion, severity: serious).  Allen's rulings:
a target is a delivery promise, so **nodes never appear in targets** (node
visibility moved to the new **`h2b hosts`** view); **sends to a node
address are rejected outright** (`TARGET_IS_NODE`, facts-only error, no
backwards-compat shim for old scripts); the announcing side stays exactly
as it is (zero wire change — the bare node_id liveliness token is
load-bearing for `deliver_notice` system-notice gating, recorded here for
whoever migrates the keyspace later).  One shared classifier
(`identity.classify_target_identity`) names Allen's four object types —
`agent` (canonical four-segment connector), `host` (bare node ids and the
`agent:<node>` display shape), `user`, `channel_route`
(`route:<adapter>:<route>`) — plus an explicitly test-covered `unknown`
for shapes no rule claims: downgrade, never mislabel.  The same gate
rejects formless send targets (`UNSUPPORTED_TARGET`), rejects replies
addressed to node-shaped senders (the original message stays pending),
and stops `_ensure_interactive_route` from advertising a pre-canonical
bare session actor into the agent liveliness keyspace (the inbox endpoint
stays open, so in-flight messages are never dropped).
`_undeliverable_outbox_recipient` is deliberately NOT tightened: bare-name
rows already queued — including pre-rename node addresses owned by the
pending-migration card — keep their durable-queue semantics.  The MCP/pi
`harness_targets` kind filter drops `channel_route` (those rows were
deliberately removed with the IRC channel-pin branch; accepting the kind
advertised a target type that could never appear).  Default CLI output is
now a plain-text table in the `h2b top` style instead of a Python-dict
repr, and `--json` is pretty-printed; the cli-blackbox contract pins
targets as agents-only plus the new hosts schema.  `user:`/
`channel_route` rows join targets only after their delivery paths are
verified against the intended product shape (message lands in the chat,
sender attributed) — a target is a promise, so unverified kinds are not
listed.

Full-link sender identity honesty (Allen A+B, same card): the daemon send
boundary now canonicalizes every unfenced sender before anything reaches
the wire — a bare alias becomes the registered agent's four-segment URI,
an unresolved bare name mints the local `agent:<owner>:<node>:<name>` —
reusing the one targeting pipeline (`normalize_agent_recipient` +
`_resolve_agent_alias`), never a second resolver.  Sender resolution is
deliberately local-only (`include_presence=False`): presence spans remote
actors, and resolving a local sender to a remote identity would author an
impersonating message.  `message.status` reads key by the same
canonicalized sender (local reads union the pre-canonicalization verbatim
spelling so durable pre-upgrade records stay visible; the mesh leg stays
canonical-only).  With no legitimate path left that can author a
bare-sender message, the inbox ingress rejects one outright
(`SENDER_NOT_CANONICAL`): no commit — so no receipt is ever signed — and
a deterministic system notice (dedup-safe under retry storms) tells the
origin node why, via the transport-stamped origin or, for pre-stamp peers,
their bare sender node id.  Every committed inbox row now carries the
transport-layer `origin_node` stamp (additive frame key, NULL-tolerant
column migration), so the physical source survives even a client that
bypasses canonicalization — the forensic gap the `onboarding-check`
incident exposed.

`route:<adapter>:<route>` posts now carry sender attribution
(`转述自 <full-actor>：\n\n<text>`) — Allen's pick for the Lark
bot-identity platform constraint, the same marker the `user:<owner>` DM
path renders, text-only injection (file/image posts untouched).  With both
legs of the targets promise true for every kind (deliverable, and the
receiver can tell who sent it), `targets` now lists all three deliverable
kinds: `agent`, `user` (owners with a completed squire profile), and
`channel_route` (configured outbound routes from channels.json); `--kind`
and the MCP/pi `harness_targets` filter accept exactly those three.

Remove the org-context signing ritual entirely (Allen: it is a plain local
Markdown document).  The envelope was self-contained --
`ed25519-v1:<public-key>:<signature>` with the key embedded in the document,
so any node could re-sign anything -- and the signing side was never wired
(no production caller, no `h2b org sign`).  The actual adoption gate is and
remains `h2b org import <file>` with owner confirmation; the mesh only ever
stages candidates into pending.  `meta.signature` is now tolerated-and-
ignored on parse (existing documents keep working, no migration) and never
written.  The signing module, its tests, and the key-file conventions are
deleted; this retires the flaky-tamper test fixed in #178 -- that fix's
purpose was served, the removal is not an escape from it.

`h2b top` now shows subscription quota for claude, codex, and kimi. A
daemon-owned background thread refreshes each source every two minutes from
the CLIs' own credential files (read-only, re-read each cycle, never
refreshed or written); `top.snapshot` only reads the in-memory cache, so the
command's latency is unchanged. Every source line carries its fetch age and
a STALE marker; any failure degrades to `n/a` with a readable reason
(missing / expired / rejected / network / invalid) without affecting the
rest. Quota windows are normalized to used/limit quantities (kimi returns
string ratios whose limit only happens to be 100 today); claude's
model-scoped weekly limit -- invisible in the official CLI -- is rendered
with its scope, severity, and active flag. kimi credentials prefer pi's own
self-refreshing set (`my-pi-setup/agent/auth.json`, fresh exactly when kimi
workers run) over the kimi CLI's 900-second file, and expiry timestamps are
normalized across seconds/milliseconds before comparison. Network-attempted
failures back off exponentially up to 30 minutes (the claude endpoint was
observed answering http 429 under repeated polling); local credential
checks retry every cycle so a pi-side refresh is picked up promptly.
`H2B_USAGE_DISABLE=1` skips collection entirely. Also fixed: `h2b top`
crashed on daemons with zero connectors (column-width `max` over a bare
int).

Add `h2b top`: a per-agent liveness view that answers "is this agent actually
working" instead of "is the process alive". The daemon now exposes each
managed harness child's pid (`h2b ps --json` connectors, like adapters always
did) and a new `top.snapshot` IPC method that joins the live process table
with generation-split worker-log turn statistics (turn counts and durations
never mix a restarted process's history into the current one) and per-actor
durable queue depth joined on the full canonical URI. Each actor is classified
as stopped / stuck (unclosed turn past its budget) / spin (fast failed turns)
/ timeout (turns hugging the harness turn cap) / backlog (queued but idle) /
idle / ok; stranded inbox keys owned by other nodes are reported separately.
The CLI renders one table and samples CPU/RSS with a single `ps` sweep,
aggregating per-actor over ppid subtrees (process-group aggregation is wrong
for both pi and codex workers); instantaneous CPU% is hidden unless `--wide`.

Correct release identity drift across project metadata, the lockfile, runtime
`__version__`, CLI output, and MCP server information. `pyproject.toml` is now
the only handwritten version source; CI rejects a stale lock before any uv
command can silently update it.

Preserve unfenced CLI node-mailbox actors during registration normalization,
so messages queued before an upgrade can still be acknowledged afterward.
The running-upgrade E2E fixture now follows canonical session identities and
remains compatible with the frozen pre-liveness Channel interface.

## [0.4.1.dev1] - 2026-08-16

（69 PR / 134 非合并提交）

### Added
- lark：identities 表（平台 id ↔ 显示名 ↔ h2b owner，带独立 schema 版本）、
  媒体获取封装（stand-in 带 ref、转发子消息带 sender、`adapter media get`）、
  `adapters.sqlite3` 真实状态库（替代 JSON-in-sqlite）。
- squire：幂等 setup。
- upgrade：简化为 tag-only upgrade。

### Fixed
- lark：回复桥经 adapter workers、worker 路由编程错误上浮、运行中 worker 即时读取 pin。
- codex：Darwin 僵尸进程组识别。
- daemon：子进程退出后 worker liveness 收敛、IPC 关机可移植。
- agents：start 拒绝连接已在运行的 agent（AGENT_ALREADY_RUNNING，#90 尾部移植）。

## [0.4.0]

First stable Python release, consolidating the `0.4.0.dev0` baseline,
`0.4.0.dev1` harness-capability work, and the release-candidate reliability
fixes developed since those snapshots.

### Reliability and recovery

- Lark inbound health now distinguishes a running worker process from a live
  websocket. Transport activity, business events, bounded REST probes, and
  reconnect history reconciliation feed explicit `healthy`, `checking`, and
  `stale` states; stale workers exit for daemon-owned replacement.
- Reconnect reconciliation is single-flight, deadline-bound, newest-first,
  idempotent by native message ID, and fail-closed when a known-chat history
  sweep is partial or fails. Terminal rebuild state cannot be overwritten by
  late callbacks, and health telemetry is dispatched asynchronously so a
  blocked observer cannot delay worker replacement.
- Inbound failures retain bounded dead-letter context and can be recovered by
  native message ID without sending duplicate routing guidance. Durable
  Harness custody, reply routing, reaction cleanup ordering, and daemon
  idempotency are covered across component boundaries.
- Linux orphan-channel cleanup, temporary-path handling, and hermetic release
  runners were tightened so isolated gates do not depend on ambient Harness
  state.

### Harness and compatibility

- The Python API uses the correct `harness` terminology and publishes a
  capability table for cc, pi, and codex. Existing on-disk and daemon wire keys
  remain compatible: schema version 1 and the historical `provider` keys are
  still read and written, so no state migration is required.
- Lark status fields are additive. `h2b ps --json`,
  `h2b adapter status NAME --json`, and Doctor now disclose
  `knownChatsRecovery=supported`,
  `unknownFirstChatRecovery=unsupported`, and
  `chatEnumeration=not-implemented` without changing persisted state.
- Package, CLI fallback, MCP HTTP server, and Claude Channel server versions
  now consistently advertise stable `0.4.0`.

### Deployment order

Deploy in this order: **roll the new binary, remove old orphan Channel
processes, then restart the coordinator Channel**. Reversing the last two
steps can allow an old process to reclaim an actor before the new fencing
behavior is active. This release preparation does not deploy or restart any
production process.

### Known limitations and release evidence

- Recovery sweeps only chats already known from local correlations or dead
  letters. Chat enumeration is not implemented, so the first message from a
  previously unknown chat cannot be promised recoverable. The platform also
  does not expose a reliable contract for enumerating every unknown bot DM.
  Group history additionally depends on the app's group-message scope. A
  healthy Lark status therefore describes stream liveness and complete
  **known-chat** reconciliation, not universal message recovery.
- The official pinned Lark SDK still emits Python 3.12 deprecation warnings
  around protobuf timestamps and event-loop discovery. The repository avoids
  the SDK's unstarted-cache coroutine warning in tests without globally
  suppressing warnings or depending on its private cron task.
- Automated isolated unit, contract, install/upgrade, four-adapter, and
  running-upgrade gates are part of release preparation. `[REAL ENV]` rows
  requiring dedicated Lark/provider accounts remain separately attended and
  must not be inferred green from the isolated matrix.

## [0.4.0.dev1]

Pure internal rename + capability declarations; **wire/on-disk fully
compatible and rollback-safe** (no daemon restart required; new CLI mixes
with old daemons in either direction).

- Rename "provider" → "harness" across Python symbols (package
  `h2b.providers` → `h2b.harnesses`; `HarnessLaunchSpec.harness`,
  `DesiredState.harnesses`, `HarnessLauncher`, `HarnessDelivery`/
  `HarnessResult`/`HarnessResultStatus`, `Managed`/`Streaming`/
  `PtyHarnessProcess`, `HarnessStartError`, `HarnessOperation`,
  `HarnessSupervisor`). On-disk JSON keys, daemon IPC keys, and CLI `--json`
  output keys are unchanged; desired-state reads accept a `"harness"` key
  (preferred) with `"provider"` as fallback, and still write `"provider"`
  at `schemaVersion=1`, so a downgrade reads everything this build writes.
  squire's `provider` (model vendor) terminology is intentionally untouched.
- New `h2b.harnesses.capabilities`: the capability enum, support levels,
  and tonight's honest (harness, mode) declaration table as the single
  source of truth; launcher dispatch and the interactive-attach predicate
  are table-driven.
- **The one intentional behavior change**: `h2b start pi|codex` without
  `--headless` now fails fast locally with `UNSUPPORTED_CAPABILITY`
  (naming `interactive_attach`) instead of being rejected by the daemon's
  desired-state validation (`INVALID_ARGUMENT`).
- Internal observability: the daemon reconcile log field
  `providerRestarts` is now `harnessRestarts`.
- G1 byte-exact golden test guards the interactive cc (channel) launch
  path; a grep lint test locks the remaining whitelisted `provider` hits.

## [0.4.0.dev0]

(Baseline: daemon, transport/inbox, lark adapter, squire, MCP channel.)
