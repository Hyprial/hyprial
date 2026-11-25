# 已知问题（Known problems）

开发或修 bug 之前，先查本表（Allen 2026-10-31）。并非所有问题都需要修：已登记且决定为“不修”或“待测”的，不重复投入；新发现但决定暂不修的问题，也登记进来，写明理由。

每条写清：现象、影响范围、规避办法、当前决定（修 / 不修 / 待测 / 修复中）、依据、负责人。状态变化时更新本条，不删除；已修复的条目保留并注明修复版本与 PR。

| ID | 问题 | 决定 |
| --- | --- | --- |
| KP-001 | tailscale.com derphttp 经 HTTP 代理时，被拒区域约 5 秒才失败 | 云电脑已实测未触发；建议不 fork，待负责人确认 |
| KP-002 | Codex 交互 app-server 返回无终止结果 | 待查 |
| KP-003 | dev 全仓 `ruff check .` 存量 508 条 | 不阻塞，0.5.5.4 清理 |
| KP-004 | daemon 批次占用在“插入运行中 turn”后不释放 | 已修复（#2228，0.5.5.3） |
| KP-005 | launchd 管理的安装上 `hyprial daemon restart` 会产生游离 daemon | 待修（#2185 升级重启线） |
| KP-006 | import ratchet 在 Python < 3.10 下读不了源码时只打印 FAIL、不阻断 | 待修 |
| KP-007 | session_persistence_overload 在 xdist 并发下被 fd 卫生门偶发误报 teardown 泄漏 | 已修复（本轮，报告期复核瞬时描述符） |
| KP-008 | `message.send` / `message.send.lookup` 走 registry 派发，网络门 IPC root（router `handle`）看不到它们 | 待修（0.5.5.4，网络门支持声明 handler root） |
| KP-009 | `test_four_sides_cover_a_pure_heartbeat_load` 在本机（Apple Silicon）单独跑常红 | 不修（机器敏感，CI 绿） |
| KP-010 | `test_close_finishes_all_teardown_after_active_ipc_worker_times_out` 本机偶发：sentinel 错误先于 IPC client 超时错误浮出 | 不修（时序竞争，基线同分布） |
| KP-011 | `test_shutdown_fails_loud_when_active_handle_exceeds_bound` 本机偶发 DID NOT RAISE：active client worker 的 0.05s 超时窗与线程退出竞争 | 不修（时序竞争，基线同分布） |
| KP-015 | 本机受限执行环境中 hosted readiness 的 spawn-error 测试在 preexec 阶段失败 | 待测（本轮不修） |
| KP-016 | Claude headless connector 报 running=true 但 pid=null，使严格 ready 谓词无法满足 | 待修（独立缺陷，本轮不修） |
| KP-017 | 合成 Anthropic provider 的 message_start 缺少 model，真实 Claude SDK 读 turn 失败 | 待修（测试 fixture，本轮不修） |
| KP-018 | 受限 macOS 全量验证存在共同基线失败与顺序相关超时 | 待测（本轮不修，整套不得判 PASS） |
| KP-019 | 隔离全量 pytest 的关闭收尾出现长耗时与 teardown 错误，独立单例不复现 | 待测（本单留证，不改日志/测试基础设施） |
| KP-020 | 单独收集 OrgFS HTTP CLI 检查测试时，Typer 根命令未注册 | 待修（测试入口，独立于 journal validity hotfix） |
| KP-021 | post-install profile 调用差异未定位，真实 CI 风险未验 | 独立待立项；CI 恢复后的第一道门 |
| KP-022 | 完整测试组合中子 pytest 长期驻留，外层等待无 timeout | 独立待立项；组合根因未定位 |
| KP-023 | GUI production-copy 校验在公共基线上命中 `fixture`、`演示` 两个禁用标记 | 待修（收尾项；保留门，不按本包回归处理） |
| KP-024 | 受限沙箱/默认长 TMPDIR 下，真实 socket 测试报 bind 或 AF_UNIX 路径错误 | 环境归因（不修；换短 `/tmp` 并在有 socket 权限的宿主运行） |
| KP-025 | GUI 首跑浏览器用例等待不存在的“登录 Hyprial”或“我的账号与连接菜单”按钮并超时 | 待修（共同基线；本单不改首跑页面） |
| KP-026 | safe-r1 开发构建缺少模块索引、preview label 与 data-source notice | 已按 2026-11-09 GUI 等价改造处理：产品内常驻开发标记退役、不再恢复，防回归转由三层门承担（见下方条目） |
| KP-027 | Rust native engine 与现有五平台 wheel 发布、文件名消费契约不兼容 | 待修（0.5.5.4 发布阻断；保持 internal 与正式发布冻结） |
| KP-028 | message thread/history 的两条协议词汇 import 未获 CLI import ratchet 准入 | 已修复（两条精确 protocol exception 与 reviewed digest；import ratchet PASS；全仓 canonical 扫描的另两项存量失败见本条） |
| KP-029 | 本地 IPC 方法扫描器将多 IM 平台 SDK 方法与正则变量误判为 IPC 路由 | 已修复（局部区分外部 API 命名；保留公开契约与真实 IPC 门） |

| KP-030 | provider key 记录在 OrgFS 写入侧没有准入挂接点 | BLOCKED（owner-home 已有；provider 身份映射、存储适配与写入准入未接通） |
| KP-031 | provider key UsageReceipt 在解封时写入，之后才失败的启动仍留 injected 行 | 不修（本版），需两阶段 seam |
| KP-032 | 植入的、签名钥锚不可用的 provider-key meta 让 `user.provider.list` 与旧 provider-write 查找 fail closed | 不修（本版），由 KP-030 根治 |
| KP-033 | provider key 回滚标记只在本地 state 目录存活期间有效 | 不修（本版），跨设备由 KP-030 根治 |
| KP-034 | 组织创建长链浏览器用例在重复创建后的文件页切换超时 | 待排查（保留完整用例 FAIL） |
| KP-035 | 隔离 GUI 焦点验收的 daemon 准入启动失败 | 待排查（功能验收 BLOCKED） |
| KP-036 | 独立方法注册测试额外报告已迁移方法的旧路由 shadow | 已修复（识别前置完整 registry 分发；伪守卫与真实 shadow 仍拒绝） |
| KP-037 | 隔离 checkout 全量 pytest 收集遇到四类既有环境与导入问题 | 待修（CI 整理；不视作 work 退役回归） |
| KP-042 | GUI Dialog 与 Select 组合的 Esc 和 portal 层级不一致 | 本地候选已修，待 PR 验收 |
| KP-043 | GUI Select/Field 丢失可访问名称、错误和已有描述 | 本地候选已修，待 PR 验收 |
| KP-044 | GUI production-copy 缺产物时可能假绿 | 本地候选已修，待 PR 验收 |
| KP-045 | GUI `org.join` 的固定 15 秒预算可能先于真实网络加入完成而中止 | 待修（独立请求预算工作包；不自动重发加入） |
| KP-047 | agent teleport 首版不做对端认证，依赖尚未实现的 Zenoh ACL | 不修（首版，Allen 2026-11-12）；后续由 Zenoh ACL 拦截 |
| KP-048 | OrgFS 定时同步的 Zenoh 查询导致 pytest worker 原生崩溃 | 根因 UNKNOWN，保留 dev 问题编号 |
| KP-052 | GUI 创建页的 provider 范围不等于完整启动组合 | 部分修复（前端顺序与组合限制已调整；正式 Key 装配与真实启动验收仍未完成） |
| KP-058 | provider-key HTTP 方法尚无 Contract Mock 示例（原分支 KP-047） | 待补示例与真实链路验收，不代表方法不存在 |
| KP-059 | GUI 初始化跨进程未知回执恢复缺口（原分支 KP-048） | 已修复（代码已合入 dev：Z-3 #2861，GUI 托管的 initialize 路径随 Z-6b #2933 删除；真实 issuer、真实 Tailcat、macOS 与浏览器全旅程未验证；Z-3：serving 启动对已提交 casdoor owner 补完 promoting/unknown 的 initialize 回执、缺失的设备登记（含上一 owner 遗留的 device.json）、仅有 failed/cancelled initialize 回执的重新 initialize；`accepted` 回执与损坏日志不处理；native 隔离 home 上 kill -9 矩阵 6 行 PASS，受控身份与受控 provisioner；真实身份 issuer 与真实 Tailcat 为 BLOCKED，未验证） |
| KP-060 | 四步引导浏览器验收等待旧按钮颜色超时 | 合并前后同样失败；随引导整理处理 |
| KP-061 | 云工作机并发全量 pytest 中断与基线归因限制 | 待归因（五项 UNKNOWN；已确认 mock 测试适配由 #2780 修复） |
| KP-062 | fs purge plan 无效目标未输出 JSON 错误信封 | 已关闭（2026-11-25 OrgFS 精简实现删除了 `org fs purge plan`，问题随命令消失；与 KP-020 测试入口问题分开） |
| KP-063 | GUI 产品代码仍保留 `/api/reference/*` 默认路径，但已无产品宿主提供 | 已修复（PR 1d、PR 1e 各段、PR 3、PR 1e-4；清单 `pending` 于 2026-11-16 清空，未发版） |
| KP-064 | 已初始化的 home 在没有 daemon 运行时 `hyprial gui` 拒绝启动（`GUI_DAEMON_UNAVAILABLE`），经 GUI 初始化的 daemon 随 GUI 子进程结束 | 已修复（DL-1 #2845：准入前确保 daemon，校验之后启动，对恢复中的 daemon 有界等待；Z-6a #2916 起 ownerless home 同样被确保；Z-6b #2933 起 daemon 不再是 GUI 子进程；含 DL-1 的捆绑运行时在 macOS 上的第二次启动 UNKNOWN） |
| KP-065 | 发布快照内网门在 `ci-native-conformance-routing.md` 的一行 clone 示例上命中两项，`dev` 的 shard 1 与 docs-only PR 的 `checks` 必红 | 修复候选在 #2833（占位写法；161 项焦点测试通过，待合入与 CI 验收） |
| KP-066 | CI 镜像上下文清单未登记 `native-conformance / plan`，`dev` 的 shard 4 必红 | 修复候选在 #2833（补真实登记；123 项焦点测试通过，待合入与 CI 验收） |
| KP-067 | `gate-python / checks` 的 GUI 集成步骤启动真实 daemon 但不构建原生引擎 | 修复候选在 #2836（真实文件清单由 3 failed / 197 passed 变为 200 passed；待授权维护者合入） |
| KP-068 | `gate-gui / gui-unit` 的 `verify:dev-browser-parity` 在 CI 上两入口组织页高度差 1px，本地不复现 | 已关闭（2026-11-20：#2842 后 dev 上未再出现；CI 之外的环境未验证） |
| KP-069 | `pytest-shard (3)` 成功时平均约 28 分钟，约三分之一运行被 runner 的 30 分钟上限取消 | 主修复候选在 #2841（探针独立单 worker 波次，其余恢复双 worker；110+162 项验证通过，待五次 push 验收）；权重刷新另列 BLOCKED |
| KP-070 | 本地 venv 默认落在 Python 3.14 时，两条比较线程名的用例在 native 后端下必红 | 修复候选在 #2837（改用线程 id；3.12/3.14 native 各 27 项通过，其余 35 处已审查） |
| KP-071 | 缺 pi SDK、ssh 工具、TEST-NET 黑洞或带有 GitHub token 变量的环境里，10 条用例以普通失败出现而未报 BLOCKED | 待修（测试前置条件与环境隔离；见 dev 红色基线 L2–L5） |
| KP-072 | owner 迁移的残留检查按子串匹配，随机 agent id 含旧 owner 字面量时迁移中止 | 已关闭（L3 删除 `owner_migration` 及该测试，问题代码不复存在） |
| KP-073 | CI 上 daemon 类子进程在预算内未就绪、xdist worker 被信号终止、worker relay 返回错误帧 | 待修（worker 被杀的主因已定位为 4 GB 内存上限下的内存压力，内核判定与 shard 4 是否转绿 UNKNOWN；其余现象根因 UNKNOWN；见 dev 红色基线 F3–F5） |
| KP-074 | 定时 macOS E2E 两条 lane 失败：E2E-010、E2E-024、E2E-004、E2E-006 | 待修（需 macOS runner；见 dev 红色基线 S） |
| KP-075 | `npm run verify` 链上的 GUI 浏览器旅程脚本与当前界面不一致，链停在 `verify:first-run-browser` | 待修（共享视图脚本可直接修；首跑类脚本待 GUI 决定；见 dev 红色基线 B1、B2） |
| KP-076 | `test:daemon-request-browser` 16 例中 10 例失败 | 不修（owner 2026-11-14 裁定，随旧 Node 宿主退场）；其中 `tailnetInitialized` 一组待核对 |
| KP-077 | external promotion 用例在应答与服务端计数之间存在竞争，`dev` push 间歇变红 | 不修（owner 2026-11-14 裁定）；已随 external promotion 的删除关闭（Z-7 A1，#2963） |
| KP-078 | `native-conformance` 的 PR lane：Linux arm64 单 runner 排队，Darwin arm64 工具链下载超时 | 待修（Darwin）；Linux arm64 只调查（owner 平台决定） |
| KP-079 | `kill -9` 后陈旧的 `state/bootstrap.json` 使下一次 bootstrap 准入失败（“record belongs to another bootstrap instance”） | 已修复（Z-2：已退出或 pid 已被复用的实例的 `bootstrap.json` 与 bootstrap 形态的 `daemon.json` 一并清理；native 引擎、隔离 home 上 kill -9 与复用 pid 标记 → `daemon start` 已实测通过；缺少出生标识的标记、或平台无法读取出生标识的情形仍需手工清理） |
| KP-080 | 安装态 `hyprial gui` 在 Windows 上返回 `GUI_RUNTIME_UNAVAILABLE`（POSIX 文件锁） | 代码已修、待 Windows 真机验收（owner 2026-11-21 解除「Windows 不排期」；锁改 `kernel.ProcessFileLock`，`--stdin-stop` 跨平台） |
| KP-081 | 非默认 home 的 per-user 服务单元以 0644 写入代理变量（可含 userinfo） | 接受（owner 2026-11-15，OD-Z7 选项 a）；发布说明列为已知问题 |
| KP-082 | per-user 服务单元的 `PATH` 以 `~/.local/bin` 开头，非默认 home 的 daemon 按名字查找 `hyprial` 或 harness 时先命中该处的另一个安装 | 待修（登记；launchd 验收 F1，读代码所得；负责人 per-home service line） |
| KP-083 | ownerless home 上显式 `hyprial daemon start` 留下持有锁的 bootstrap daemon，之后 `hyprial gui` 约 24 秒后以 `GUI_START_FAILED` 失败 | 已修复（#2916 Z-6a：`local-gui` 子进程转发到应答 bootstrap 的 daemon，`hyprial gui` 在任何状态都先确保 daemon；#2927 Z-6pre、#2933 Z-6b、#2946 Z-6c、#2963 Z-7 A1 已合入 dev；Z-6b 删除 GUI 托管路径后，下方“已知限制”不再成立，见该条；浏览器侧、真实 OIDC、macOS 与原生壳 UNKNOWN） |
| KP-084 | bootstrap daemon 上 `hyprial daemon restart` 以 `METHOD_NOT_FOUND`（`ps`）失败 | 已修复（Z-4 #2925：状态闸门在 `ps` 被拒时用一次 `ping` 确认 bootstrap 并读取其身份；Z-4b #2944：bootstrap daemon 对 router 自身分支的方法名一律答 `ACCOUNT_REQUIRED`；受监管整段重启只有单元覆盖，真实进程 BLOCKED） |
| KP-085 | `hyprial login` 即使设置 `HYPRIAL_NETWORK_ISOLATED=1` 也会联系配置的身份 issuer（`hyprial init` 自 Z-5 PR 2 起不再登录，不再联系） | 待修（dev 既有；隔离探测时需避开） |
| KP-086 | ownerless 准入的拒绝原因（“previous process has not exited”等）只在 `daemon-launch.log`，`DAEMON_START_FAILED` 的 `recentLog` 为 null | 待修（后续；需要新错误码，涉及 `src/hyprial/error/codes.py`） |
| KP-087 | `kill -9` 之后管理器重启 daemon，`daemon service status` 的 `lastExitCode` 为 null，原因未查 | 待测（登记；launchd 验收 F5；负责人 per-home service line） |
| KP-088 | pytest 对不存在的测试路径收集 0 项，验收时误用旧路径得到 exit 5 而无明确提示 | 待测（登记；launchd 验收 F6；负责人 per-home service line） |
| KP-089 | systemd 上“按请求停止”与“失去监管”无法区分（`systemctl stop` 后单元仍为 loaded），`daemon service status` 仍报 `lost` | 待修（launchd 的 doctor 已区分，见 F2；systemd 需要 `SubState`/`ActiveState` 进入状态；负责人 per-home service line） |
| KP-090 | `daemon stop` 超时消息固定写 “daemon socket closed but teardown did not finish”，而该处并未观察 socket 是否关闭 | 已修（本 PR：消息现在报告超时时实际观察到的 socket 状态；条目保留作记录） |
| KP-091 | serving 启动恢复的设备登记期间若发生 logout，设备记录可能写给已登出的 owner | 待观察，低严重度（Z-3；恢复在登记后重读 owner 并中止 initialize；任何回执都不会为其他 owner 完成；残留的 pending 设备回执在下次启动被日志重整为 unknown） |
| KP-092 | `daemon stop` 的应答无法表达“存在不可归属的单元，监管者可能重启 daemon”（只在 stderr 警告） | 已缓解（所有者 2026-11-17 选方案 B：应答新增加性字段 `supervisorMayRestart`；只读 `ok` 的调用方仍不知情） |
| KP-093 | 没有受支持的办法隔离由管理器启动的 daemon 的环境（只影响测试与验收，原装单元无法带隔离变量） | 待修（登记；launchd 验收 F3；负责人 per-home service line） |
| KP-094 | serving 启动恢复进行设备登记期间，`onboarding.status` 的 `nextActions` 暂列 `device.ensure`，而 serving 拒绝该方法 | 不修（Z-3；瞬态，需要新增状态字段即契约变更；owner：daemon onboarding 维护者）；Z-5 起 daemon 驱动的首次登录在设备登记失败时也会停在这个状态（#2958） |
| KP-095 | 原生壳每次启动换端口，GUI 的 `localStorage`（Squire 身份存储）不跨启动保留 | 不修（OD-4 默认 `--port 0`，待 owner 确认） |
| KP-096 | 原生壳的 WKWebView 数据不随 `HOME` 重定向，测试与冒烟在真实账号 `~/Library` 下留下 `com.hyprial.hyprialos` 目录 | 不修（记录位置与卸载清理；冒烟只列出不删除） |
| KP-100 | 仓库里的 workflow 条件不是 secret 的安全边界：有写权限的人推一个带新 workflow 的分支，就能在 `release-macos-arm64` 上读取任何仓库 secret，或使用 runner 账号能用的签名身份 | 接受（2026-11-20：打包完全离开 CI，两个打包 workflow 删除，只由操作者手动运行 `package-macos-shell.sh`（签名用 `--developer-id`）；身份仍在 runner 所用账号的登录 keychain 里，“CI 不签名”靠没有 workflow 而非隔离；仓库检查只防误开启，真正的控制点在仓库之外；重启后 keychain 锁定如何解锁未决） |
| KP-101 | daemon 在同一进程内每经历一次登出，就多留一个 `shutdown-stall.log` 的打开句柄（每次 1 个文件描述符，线程数不增长） | 待修（登记；影响小，长期运行且频繁登出时累积；负责人 daemon-first 线） |
| KP-102 | GUI 打开时 daemon 被停掉：各情形下子进程与 GUI 的行为 | 子进程一侧已修复（Z-6c，PR #2946，已合入 dev）：第 1 条（CLI `hyprial logout` 使子进程退出）与第 2 条的 `CATALOG_STALE` 建议不再出现，子进程不因 daemon 消失而退出，请求答 `DAEMON_UNAVAILABLE`，daemon 回来后原地恢复；CLI `logout` 改走一次 `auth.logout`（L1，2026-11-21，分支 `refactor/logout-via-auth-logout`，待合入）；页面在 `DAEMON_UNAVAILABLE` 时显示什么、macOS 与原生壳 UNKNOWN |
| KP-103 | GUI 聊天页对 `recordAvailable: false`（daemon 读不到会话记录，只按本机队列作答）没有任何界面提示 | 不修（第一片接受，PR #2936 评审 2026-11-18）；需要时按现有状态约定补一个不惊扰的提示 |
| KP-105 | 没有任何办法取得新的 Lark 绑定证明，且以后也不会有；运行时已不按绑定行事（R1 #2970 入站、R2 #2989 出站），解析器、覆盖与账号表、证明发布与核验、`user bind` 等命令已由 R3 删除 | 已关闭（R3）；GUI 两个面板待 GUI 线删除 |
| KP-106 | 用户 agent 的名字形式判断只看地址文本：任何 `agent:<任意 owner>:<任意节点>:hyprial-user` 都被当作某台机器的用户 agent（从列表隐藏、拒绝派工与管理），不论这条记录是否存在 | 不修（只隐藏和拒绝，不授予任何权限；PR #2941） |
| KP-107 | 不认识用户 agent 的旧版本节点发来的带 role 的消息，在本节点入口不被拦截；只有本节点自己的派发与工作准入拒绝 | 不修（待所有节点升级；PR #2941） |
| KP-108 | owner 切换时，若旧 owner 的用户 agent 已被内部路径销毁、只剩名字账本的冻结行，而 daemon 恰在 owner 改写提交后、重新定键前退出，下次启动无法判定该冻结行属于用户 agent，名字保持冻结，状态为 `name-frozen` | 已关闭（UA4：用户 agent 的 id 不再由 owner 派生，重新定键步骤已删除，不再有这个窗口；NF1：名字账本已删除，被销毁的用户 agent 的名字不再冻结，ensure 直接重建） |
| KP-109 | 大小写不敏感的文件系统（macOS 默认）上，已有普通 agent `Hyprial-User` 时，用户 agent 的 home 与它撞名，创建失败被当作可重试错误，6 次后状态为 `unavailable` | 不修（保留名按惯例精确比较；规避：销毁该普通 agent 后重启 daemon，改名无效；PR #2941） |
| KP-110 | 发送人认证的默认 observe 模式下，本机 IPC 调用方只要指名某个 actor 而不带 `sessionRef`，就能列出该 actor 的待收消息并以它的身份发送；对所有 agent 都如此，并非本 PR 引入；对 owner 的用户 agent 更要紧：它永远不可能持有会话，所以没有围栏的调用方是它收件箱唯一可能的消费者，且它的名字在每台机器上固定可预测（id 自 UA4 起由注册表随机生成，不再可预测） | 待修（须在 owner 的会话被路由到用户 agent 自己的地址之前关闭：enforce 模式或等价收紧；负责人：实现该路由那一片的实现者；PR #2941） |
| KP-111 | GUI 的登录和重新登录已交给 daemon 驱动；浏览器只读取 `auth.operation.get` 回执，登出也只在需要时读取回执 | 已修复（L2 daemon relogin；真实 issuer、真实 Tailcat、macOS 与浏览器全旅程未验证） |
| KP-112 | 同一 owner 的重新登录已由 serving daemon 的 `auth.relogin` 驱动；L3 已删除 `--switch-account`、`--dry-run` 与 `owner_migration` | 已关闭（L2 daemon relogin + L3 单 home 单 uid 与 `settings.homeAccount` 绑定；真实 issuer、真实 Tailcat、macOS 与原生壳未验证） |
| KP-113 | pin 了 adapter 的 agent 发给 `user:<owner>` 的消息不推送到 IM，有 user-proxy 也不推（含它代发的 routine / workflow 报告） | 已关闭（I3，2026-11-23：daemon 按发送方 cell 推到它绑定的聊天，`via: "cell"`） |
| KP-114 | daemon 通知只写 `warn` 日志，`hyprial log` 没有按事件名过滤 | 待做（`--event` 过滤与 GUI 日志页列为后续） |
| KP-115 | 根 `gui/`（共享网页 UI）不在结构门（每目录 7 个直接文件、每文件 1000 行）的扫描范围内，现有文件与目录有超限的 | 接受（owner 2026-11-20：登记为已知问题；是否纳入结构门由 GUI 维护者排期） |
| KP-116 | 旧版 desired-state 里没能迁移的 adapter 固定（`channel_pins`，按名字或名字形态 URI 存）在任何 agent 创建时按名字解析，所以一个已销毁 agent 遗留的未迁移固定会落到之后用同一名字新建的 agent 上 | 已关闭（I6-4 不再按名字解析；F1b 删除 `channel_pins` 存储，不再读写） |
| KP-117 | 带 agent 选择器的 `workflow.list` 也按活 agent 的名字形态 URI 查看（workflow YAML 用它命名节点 owner），所以一个 agent 被销毁、新 agent 用了同一名字后，YAML 曾指向旧 agent 的运行会列在新 agent 下 | 接受（协调者 2026-11-19：只是列出，不是指派；正解是在运行开始时绑定 agent id，本片不做） |
| KP-118 | 授权审计流水（`agent_grant_journal`）按 agent 名字读取，所以用已销毁 agent 的名字新建的 agent，其 `agent.grant ... list --audit` 也显示旧 agent 的流水行（每行带各自的 entity token） | 接受（只是显示；授权本身按化身判定，新 agent 不继承任何授权） |
| KP-119 | CI 上 `hyprial gui` 启动的宿主进程偶发在 60 秒内未就绪，相关真实进程测试失败 | 观察（确认 1 次；原因未知；下次出现时读真实启动记录，不凭猜测调时限） |
| KP-120a | 环境里设置 `HYPRIAL_OWNER` 且 home 已没有登录时，`hyprial logout` 答 `DAEMON_NOT_READY`（serving daemon 没有账号端口），不是幂等的 `not-logged-in` | 待 owner 决定（L1 发现，2026-11-21） |
| KP-120b | `hyprial gui --stdin-stop` 收到 `SIGHUP` 时前端子进程留在孤立会话 | 观察 / 暂不修（用关闭 stdin 停机；PR #3051 实测 F4） |
| KP-121 | 升级前处于登录态、升级后未重新登录就 `hyprial logout` 的老 home，同一账号再登录以 `IDENTITY_CONFLICT`（`reason=home-binding-missing`）被拒 | 接受（不在登出时自动补写绑定；迁移说明与 CHANGELOG 警告，手工补写 `homeAccount` 恢复） |
| KP-122 | cell 成员 reconcile（I6-2）的一次有界漂移重启失败后，该成员在本次 daemon 运行内停在 `FAILED`（`config-digest-mismatch`），旧进程仍在运行；另有几处已知边界 | 暂缓（cell 模型线；改文档回到与运行中进程一致或重启 daemon 可恢复） |
| KP-123 | Windows 上以提升权限的进程运行时，`mkdir(0o700)` 建出的目录没有当前用户 ACE，私有性校验报 `unsafe-acl` | 暂不修（登记；待真机证据；规避：用非提升的 shell 运行；负责人 Windows 可移植性线） |
| KP-124 | Windows 上 `hyprial gui --no-open --json` 启动记录的 `handoff_note` 仍称「private 0600 file」，而 Windows 没有 mode 位，文件私有性靠运行时目录的账号私有访问 | 暂不修（登记；`handoff_note` 是可改写的说明文字，不影响 `handoff_path` 与文件本身；负责人 Windows 可移植性线） |
| KP-125 | 同一 adapter 被两个 agent 的 cell 声明时，写入不拒绝，路由读作“未绑定” | 延后（I6-4 critic P2-3；cm 线） |
| KP-126 | cell 文档里的 adapter 成员不检查 adapter 是否存在 | 延后（I6-4 critic P2-4；cm 线） |
| KP-127 | `agent.cell.get` 的受众包含 `bound-agent`，可读其他 agent 的 cell 文档 | 延后（I6-4 critic P2-5，待 owner 确认受众；cm 线） |
| KP-128 | Lark 入站在 daemon 不可用超过 120 秒重试预算后记为 `forward-retry-exhausted` 死信，需要手工恢复 | 延后（I6-4 critic；cm 线） |
| KP-129 | cell 运行时组合失败期间，本机每个 agent 发给 `user:<owner>` 的消息都以 `USER_DELIVERY_UNAVAILABLE` 被拒绝、不入队；`cache-identity-mismatch` 等原因下一直持续到人工修复 | 接受（I3 契约 §3.2 第 6 行：读不到 cell 不选聊天；按 cell authority 迁移说明手工修复 cache 恢复） |
| KP-130 | 「本机运维者」不是硬边界：agent 自己的 shell 运行 CLI 时也算未验证的运维者，可读写 user agent cell，改变 owner 副本推往哪些聊天 | 接受（I7a critic P2-4；与 I7a 之前普通 agent cell 的 `agent.cell.update` 同等强度；cm 线） |
| KP-131 | 成员 reconcile 重启时新旧配置都起不来，agent 停到下次启动或 `hyprial agent start` | 接受（I6-5 critic V3 P1-1 裁定；cm 线） |
| KP-134 | OrgFS 的节点名与作者是自报的，签名今天不强制 | 接受（owner 2026-11-25） |
| KP-135 | OrgFS 只读成员与被降级成员的写入只在写入节点本地与第一跳节点门被拒 | 接受（owner 2026-11-25） |
| KP-136 | OrgFS 的 blob 拉取通道不识别请求方 | 不修（需改线路协议） |
| KP-137 | 并发写同一受保护记录时，在合并中输掉的伪造更新被接受 | 不改 |
| KP-138 | OrgFS 递归列目录不分页 | 不修 |
| KP-139 | OrgFS 统计“正文未到”的文档时周期性扫描整张节点表 | 暂不改 |
| KP-140 | 带 `--base-version` 的写入在同一行冲突时后写者胜，不报错 | 待负责人决定 |
| KP-141 | 很旧的 `--base-version` 不再能合并，返回 `stale-write` | 接受 |
| KP-142 | 二进制文件没有人读就不会取回，也没有到达事件；`content_pending` 不计二进制 | 接受 |
| KP-143 | OrgFS 删除留下的墓碑随删除次数增长 | 接受 |
| KP-144 | OrgFS 移动操作的时钟是进程内计数 | 不在本次修 |
| KP-145 | OrgFS 被拒绝的远端更新不会自动对账 | 接受 |
| KP-146 | 网络可达性门在同名定义超过 8 个时不连边 | 登记，待门的维护者决定 |
| KP-147 | `orgfs.checkout` 对未知空间报 `checkout-io`，不说明原因 | 暂不改 |
| KP-148 | 加入 OrgFS 空间时，供给方信任加入者自报的作者 | 待加入与身份负责人决定 |
| KP-149 | GUI 回收站列表只显示默认上限内的条目 | 原有行为，登记 |
| KP-150 | 一个对端被分区时，重启的节点连不上其他对端（不属于 OrgFS 改动） | 待查 |
| KP-151 | 侧车停顿约 3 分钟后，转发一直宕到 daemon 重启（不属于 OrgFS 改动） | 待查 |
| KP-152 | `org invite` 返回 `USER_DELIVERY_TIMEOUT` 但成员已经加入（不属于 OrgFS 改动） | 待查 |
| KP-153 | 本机 IPC 请求超过 8 MiB 时直接断开，不回错误（不属于 OrgFS 改动） | 待查 |
| KP-154 | 离开组织的请求在等待 owner 确认时没有日志（不属于 OrgFS 改动） | 待 org 负责人决定 |
| KP-155 | 真实 daemon 上 `org join` 以 `IDENTITY_CONFLICT` 失败（不属于 OrgFS 改动） | dev 既有问题，已报 owner |
| KP-156 | 请求注册表把带数据的已声明错误改写成 `DAEMON_ERROR`（不属于 OrgFS 改动） | dev 既有问题，已报 owner |

## KP-001 tailscale.com derphttp 经 HTTP 代理时被拒区域慢失败

- 现象：sidecar（`sidecar/hyprial-tailcat`）经 HTTP 代理连接 DERP 中继时，代理对某个区域返回非 200 的 CONNECT，`tailscale.com/derp/derphttp` 不会立即失败，而是等满单节点超时（约 5 秒），也不向上层报告“所有区域都被拒”。代理挂住某个节点时，要等到超时。
- 影响：只出现在必须经 HTTP 代理出网、且代理拒绝或挂住部分区域的环境（例如云电脑）。可用区域靠前时只是变慢；被拒区域多或首节点挂住时，单次交换可能超过 30 秒预算，报 `AUTHORITY_UNREACHABLE`。无预算分配的原始基线上，“首节点挂住”和“第 4 个区域才可用”两个用例在 30 秒失败；2026-10-31 真实环境出现过一次 52 秒失败。直连环境不受影响。
- 规避：我方代码内的改进（不依赖 fork）已落在分支 `fix/tailcat-sidecar-http-proxy-nofork`：每个交换的区域预算分配、10 分钟有效的可用区域提示、控制帧换行完整性检查。沙箱 8×20 实测 139/160（`docs/evidence/tailcat-http-proxy-nofork-2026-10-31.md`）：第 4 区域才可用从基线失败变为 20/20（16–26s），提示使后续交换不再重试被拒区域，耗时降到 0.07–10.5 秒（实现方实测；独立复核测得第 2 次 0.08–7.1 秒、均值 2.0 秒，第 3 次 1.1–7.1 秒、均值 1.9 秒），不是亚秒级；残余边界：被拒区域每个仍耗约 5 秒，首节点挂住时健康节点不可达（mixed-node-hung 0/20），预算余量偶发低于 5 秒（实现方 1/20、3.51s；独立复核 2/20、3.80s 与 3.76s）。
- 决定：待测。Allen 2026-10-31 定，先不修改 tailscale.com 基础库，用 nofork 分支在云电脑上实测后再决定是否 fork；若实测出现“首节点挂住”型失败即触发激活候选补丁。候选补丁（每节点代理拨号超时、非 200 CONNECT 立即失败、区域耗尽类型化通知）及其验证保留在 `fix/tailcat-sidecar-http-proxy`@ebb675788，证据 `docs/evidence/tailcat-http-proxy-round3-2026-10-31.md`（该分支依赖临时 fork，不能直接合入）。
- 云电脑实测（2026-11-03，cloud-claude，分支 `hotfix/tailcat-http-proxy-nofork`，证据 `docs/evidence/tailcat-http-proxy-nofork-cloud-2026-11-03.md`）：基线 0.5.5.3.3 的 daemon 剔除 sidecar 的代理变量，sidecar 直连 DERP IP，被沙箱透明拦截后循环重连，`org join` 报 `AUTHORITY_UNREACHABLE`；nofork 构建下 sidecar 全部经代理 CONNECT（0 次直连），邀请兑换成功（`org.join.reconcile ok`，开始后约 3.5 秒），对端 agent 的状态查询能到达本节点；代理无任何 CONNECT 拒绝或挂住记录，**未出现“首节点挂住”型失败，不触发候选补丁**。`org join` 仍在 OrgFS 段失败（`join-orgfs` 超时，随后 `orgfs.sync.failed: no-holder-online`，supplier 为 hq），发生在成员关系成功之后，不属于本条，原因待查。
- 首节点挂住（2026-11-03，#2322 评审阻断项）：消费侧修复，多节点区域里走代理的节点各自成为一个候选、各自分预算，直连节点保持原整区候选（复审要求未设代理时行为不变）；`TestReviewProxyFallback/mixed-node-hung` 由失败变为通过（兑换约 26 秒），仍不 fork tailscale.com。
- 负责人：R&D 负责人（emergent-org-structure-refactor）。

## KP-002 Codex 交互 app-server 返回无终止结果

- 现象：Codex interactive 载体记录 `worker.carrier.error` / warn，错误为 `Codex interactive app-server response ended without a terminal result`，状态为 `FINAL_OBSERVED`（对账后）或 `TURN_STARTED`（待对账）。
- 影响：devusr-dev 的 codex-daemon-request 在 24 小时内出现 33 次（2026-10-31 报告方只读统计）。对应 turn 的结果交付可能需要对账或重试。
- 规避：无；载体会对账。
- 决定：待查。与“已结算批次重放”修复（`fix/codex-carrier-settled-replay`）分开处理，已登记 OrgFS 工作项（group-internal）。
- 负责人：R&D 负责人。

## KP-003 dev 全仓 ruff 存量问题

- 现象：在 dev 上运行 `ruff check .`，报 508 条（56 个文件）：497 条 F811（测试中重复定义 fixture），11 条 F821（`src/hyprial/shell/impl/cli/commands/routine/commands.py`，`spec` 未定义）。
- 影响：不影响产品运行。F821 是误报：外层函数只做了 `spec: RoutineSpec` 的类型声明，内层 `operation` 通过 `nonlocal` 赋值，`render` 在赋值之后才执行。
- 规避：交付时对改动文件运行 ruff；全仓 ruff 的存量问题不算本次回归。
- 决定：不阻塞，在 0.5.5.4 清理（F811 合并重复 fixture；F821 改写法，消除误报）。
- 负责人：R&D 负责人。

## KP-004 daemon 批次占用在“插入运行中 turn”后不释放

- 现象：agent 正在跑 turn 时，又有新消息到达；daemon 的“插入运行中 turn”（steer）被接受后，被插入的消息不会单独产生结果，而 daemon 按 agent 记在内存里的批次占用只在收到结果或投递被拒时才释放。结果该 agent 再也收不到任何投递，`agent top` 显示 backlog，重启 worker 无效。
- 影响：2026-10-31 e2e-verifier 从 06:10Z 起停摆约 7 小时，29 条消息积压。
- 规避：重启 daemon（占用只在内存中，没有 CLI 可以清除）。
- 决定：已修复，#2228 合入 release/v0.5.5.3（含后台投递超限无法溢出的第二个故障）：插入成功时释放占用；worker 重启时清掉该 agent 的占用；对应消息已消费的占用自动丢弃；在 `agent top` 中显示占用。
- 负责人：R&D 负责人。

## KP-005 launchd 管理的安装上 `hyprial daemon restart` 产生游离 daemon

- 现象：生产 daemon 由 launchd unit `com.hyprial.daemon` 管理时，执行 `hyprial daemon restart`，新 daemon 由 CLI 直接拉起（父进程为 1，`supervision: unsupervised`），不在 launchd unit 下。之后 `launchctl bootout` 停不掉它，launchd 再拉起的 daemon 拿不到 socket。
- 影响：2026-10-31 15:00Z 的升级第一次因此自动回滚（新版本没接管，zenoh 判据超时）；系统中短暂同时存在两个 daemon 进程。
- 规避：launchd 管理的安装上，不用 `hyprial daemon restart`。需要重启时用 `launchctl kickstart -k gui/<uid>/com.hyprial.daemon`；已经产生游离 daemon 时，先 `hyprial daemon stop`（停掉持有 socket 的那个），让 launchd 的 daemon 接管，再做后续操作。
- 决定：待修，归入 #2185（升级/重启只经 launchd/systemd）一线，在 slice 4B 的 CLI 路径中处理。
- 负责人：R&D 负责人。

## KP-006 import ratchet 在 Python < 3.10 下打印 FAIL 但不阻断

- 现象：用系统 `python3`（macOS 自带 3.9.6）手动运行 `scripts/ci/check_pr_hygiene.py ... merge-tree` 时，import ratchet 解析到用了 `match` 语句的源码（例如 `actor.py:243`）就报 `unreadable-backend-source`，输出 `FAIL import ratchet`，但整个命令的结果仍是 `status=green`。用 3.12 及以上运行时正常。
- 影响：本地手动复跑门禁时，结论可能被误读；一个“总是 FAIL 又从不阻断”的检查，会让人习惯性忽略 FAIL。CI 用 uv 管理的 3.12，不受影响。
- 规避：一律用 `uv run --no-project --python 3.12 python scripts/ci/check_pr_hygiene.py ...`，不要用裸 `python3`（AGENTS.md 也禁止裸跑）。
- 决定：待修。ratchet 遇到读不了的源码时应 fail-closed；解释器低于 3.10 时直接拒绝运行，并自报环境（例如 `requires python>=3.10, got 3.9.6`），不能报 `invalid syntax`，否则会被读成代码坏了。
- 负责人：R&D 负责人。依据：hq-adjutant 2026-11-01 在 #2231@7cd60fc2e 上的复跑读数。

## KP-007 session_persistence_overload 的 fd 卫生门在并发下偶发误报

- 现象：全量分片（`-n 2 --dist=loadfile`）里，`tests/daemon/sessions/actors/test_session_persistence_overload.py::test_deferred_completion_survives_generation_and_cannot_restore_old_route` 的 teardown 偶发报 `AssertionError: test leaked N file descriptor(s): fd=… (unresolvable)`（N=1–3，fd 号每次不同），两个参数化在同一秒内先后报；同 head 重跑或单跑该文件均绿。已记录两次：2026-10-16（`docs/notes/resident-pi-provider-credentials-rework2-2026-10-16.md`，“并发干扰，不计作产品失败”）与 2026-11-01 release/v0.5.5.3 shard 2（run 33456，job 105293，4418 passed，仅此 2 个 ERROR；同分支前一跑 33451 shard 2 全绿）。
- 影响：只影响 CI 分片的稳定性；泄漏计数器把“列出与归因之间已关闭的短命 fd”计为 unresolvable 泄漏，过载测试（真实 daemon + socket 池）放大了窗口。不指向产品 fd 泄漏（两次均 unresolvable，且同 head 重跑即绿）。
- 规避：分片红时先看是否为本条（overload 测试 + unresolvable + 同 head 重跑绿）；重跑该分片/重新触发同 head 全量即可。
- 决定：已修复。根因是 fd 卫生门的报告窗口：候选 fd 在快照后、报告前自行关闭（异步 teardown 收尾），被计为泄漏且无法归因。`tests/conftest.py` 现在在报告期用 fstat 复核，仍打开的才算泄漏，已消失的从计数剔除（消息尾注明数量）；真实泄漏（仍打开）照旧红。若再现可归因（非 unresolvable）泄漏，按真实泄漏处理，不适用本条。
- 负责人：R&D 负责人。依据：run 42756 shard 2 同测试全绿；docs-only diff（`95333f15e6` → `732096c109`）前后一红一绿，`6b5d4b085d` 再次同签名。

## KP-008 registry 派发的方法在网络门的 IPC root 之外

- 现象：`message.send.lookup` 和 `message.send` 走 `METHOD_REGISTRY.invoke`，网络门（`scripts/ci/check_network_reachability.py`）只从 `router.handle` 函数体解析调用边，看不到注册 handler 的子树。移除旧 send 分支后，旧 root 的八条静态可达指纹不再出现，其中一条是 Discord heartbeat 的同名保守别名。
- 影响：lookup 当前只读本地 SQLite；send 仍可能触发网络发送。旧 root 指纹消失不表示 send 无网络效应，而是扫描器的注册派发盲区。
- 规避：测试分别钉住真实 registry 中 send 和 lookup 的 handler，以及 handler 到各自 `_ipc_message_send` / `_ipc_message_send_lookup` 的调用边；另实测 Discord heartbeat 到其 transport 的发送。修改 send 或 lookup 子树的网络效应时须人工审查。
- 决定：待修。网络门需把声明的 handler 也作为 root（或采用等价规则），届时恢复 root 级可见性断言。
- 负责人：R&D 负责人（emergent-org-structure-refactor）。

## KP-009 heartbeat CPU 覆盖率测试在本机单跑常红

- 现象：`tests/daemon/ipc/cost/test_ipc_cost_counters.py::test_four_sides_cover_a_pure_heartbeat_load` 断言 40 次 heartbeat 窗口内 `instrumentedCpuMs / processCpuDelta >= 0.70`。在本地 Apple Silicon（macOS，CPython 3.12）单跑时比值落在 0.68–0.74，常红（2026-11-03 实测：基线 `origin/release/v0.5.5.4` 五跑 4 红 1 绿，带 agent-git-identity 工作树五跑同样 4 红 1 绿）。窗口总 CPU 仅约 12–22 ms，未插桩的后台/GC CPU 就能把比值压到线下。
- 影响：只影响本机手动复跑；CI（uv 管理的 3.12 runner）照常绿。不指向产品回归——基线与改动树同分布。
- 规避：本机不以此单测结论为准；以 CI 全量结果为准，或多次重跑取稳定结论。
- 决定：不修。比值门是为 CI 机器校准的；本地机器差异不是产品问题，不为它放宽或改写断言。若 CI 也开始变红，再按真回归处理。
- 负责人：R&D 负责人。依据：agent-git-identity 分支调查（2026-11-03），基线/改动树同分布红绿。

## KP-010 shutdown 遥测测试断言的错误次序在本机偶发颠倒

- 现象：`tests/daemon/application/transport/resilience/shutdown/test_shutdown.py::test_close_finishes_all_teardown_after_active_ipc_worker_times_out` 期望 `_close()` 抛出的首错误是「IPC client workers did not stop」（client worker 超时先登记），但本机偶发改为先抛 adapter cleanup sentinel（2026-11-03 实测：单跑循环 10 次第 4 次红；在仅含前置 commit 的基线树同样复现，与 agent-git-identity 改动无关）。两个 teardown 错误的登记/浮出次序依赖线程调度时序。
- 影响：只影响本机手动复跑的偶发单红；断言的两个错误都会发生，只是次序不定。CI 未观测到变红。
- 规避：重跑或以 CI 为准；不改断言。
- 决定：不修。错误聚合顺序的确定性化属于 shutdown 模块自身的改进，不属于本批；若 CI 出现同型失败再升级。
- 负责人：R&D 负责人。依据：agent-git-identity 分支验证记录（2026-11-03）。

## KP-011 shutdown loud-fail 测试本机偶发不抛错

- 现象：`tests/daemon/application/transport/resilience/shutdown/test_shutdown.py::test_shutdown_fails_loud_when_active_handle_exceeds_bound` 期望 `_close_ipc_clients()` 在被阻塞的 active IPC client worker 超过 0.05s 宽限后抛 `RuntimeError("IPC client workers did not stop")`，本机偶发 `DID NOT RAISE`（review 聚焦套件 2026-11-03 报 1 红；2026-11-04 修复头单跑 16 次 4 红、基线 `f98c63e248` 单跑 10 次 2 红，分布重叠）。被阻塞 worker 的释放与 `_IPC_CLIENT_SHUTDOWN_TIMEOUT=0.05` 窗口、join 时序竞争。
- 影响：只影响本机手动复跑的偶发单红；agent-git-identity 修复分支与基线同分布，非该改动引入。CI 未观测到变红。
- 规避：重跑或以 CI 为准；不改断言。
- 决定：不修（同 KP-010 的时序竞争结论）；0.05s/2s 测试窗口的确定性化属 shutdown/ipc 模块自身改进，不属本批。基线对照数字见 `docs/notes/agent-git-identity-fixround1-redfirst-2026-11-04.md`。
- 负责人：R&D 负责人。依据：review GI 证据登记（18066078db）+ fix round 1 复跑对照（2026-11-04）。

## KP-015 受限执行环境中的 hosted readiness spawn-error 测试

- 现象：`tests/integration/hosted/readiness/test_hosted_readiness_timing.py::test_spawn_error_is_recorded_and_reraised` 期望缺失解释器抛出 `OSError`，但本机 macOS 受限执行环境先抛出 `subprocess.SubprocessError: Exception occurred in preexec_fn.`；启动器的 preexec 是 `lambda: os.nice(10)`。冻结基线 `4ffe3b789887ab46c66276be8385d501f75661d0` 的独立工作树与本轮候选均复现同一异常，涉及文件没有修改。
- 影响：本轮相邻 readiness 套件结果为 1 failed / 153 passed，不能报整套 PASS；尚未验证普通宿主或 Linux CI 是否复现，不据此推断产品启动失败。
- 规避：在允许该 preexec 操作的执行环境复验；当前只单列此失败与独立通过的焦点测试，不跳过、不放宽异常断言。
- 决定：待测，本轮不修。当前任务仅修复 loopback 的就绪预算；preexec 失败是否由沙箱限制导致仍未脱离受限环境验证。
- 依据：[就绪预算实现证据](../notes/e2e-headless-ready-budget-2026-11-05/report.md)，包含候选与冻结基线的复现命令、异常与结果。
- 负责人：hyprial-cli-developer（本轮派单方，后续环境复验）。

## KP-016 Claude headless connector 的 pid 缺值

- 现象：冻结基线 `4ffe3b789887ab46c66276be8385d501f75661d0` 上，真实 Claude headless 在全新隔离 daemon/home 和本地合成 provider 下启动后，IPC 的 connector 为 `running:true, pid:null`，对应 agent 为 online。2026-11-05 09:11:45.569 UTC 发起启动，约 216 秒后的状态仍如此；worker 日志报告子进程 started 与 sdk-ready waiting，但没有 SDK ready 记录。
- 影响：`run-real-harness-loopback.py::_wait_headless_ready` 要求 running connector 具有 int pid，因此这一状态无法通过 ready 判定。其他依赖该 pid 的观测也缺少依据。它是状态字段缺值的独立问题，不能用加长预算或去掉断言来宣布修复。
- 规避：保留严格谓词并报告就绪测量 UNMEASURED；尚无已验证的运行规避。不推断所有机器或所有 harness 都复现。
- 决定：待修，独立于本轮 E2E 就绪预算修复；由派单方消息 `4a7861e6-9105-530c-8437-85196d573508` 分配 KP-016，禁止本轮顺手修复或放宽 ready 断言。
- 依据：[就绪预算实现证据](../notes/e2e-headless-ready-budget-2026-11-05/report.md)，冻结基线隔离采样与实际 IPC 字段记录。
- 负责人：hyprial-cli-developer（已接收并分配独立缺陷编号）。

## KP-017 合成 Anthropic 消息缺少 model

- 现象：`contract/e2e/_fake_provider.py:438` 的 `_anthropic_chunks` 发送不含 `model` 的 `message_start.message`。在本轮自建临时 HOME、固定 `claude-agent-sdk==0.2.125`、其内置 Claude Code 2.1.217 和 loopback provider 下，SDK ready 后的首轮 query 报 `MessageParseError("Missing required field in assistant message: 'model'")`；worker 返回 `isError:true` 并以 1 退出。
- 影响：消费该合成 Anthropic 流的真实 Claude 路径无法以此证明 turn 或会话连续性；这不是生产 provider 故障的证据。完整 loopback runner 本轮未执行，不能据此宣称其所有场景均失败。
- 规避：本轮独立会话探针使用自有 loopback provider 补足 `model`、`usage` 和终止帧，保留真实 SDK、CLI 与原生 transcript；该探针验证通过，不替代完整 E2E-006。
- 决定：待修。本轮仅实现 Claude 原生会话的持久化与严格恢复，不修改共享 provider fixture 或放宽解析断言。
- 依据：[本轮实现与验证报告](../notes/claude-session-resume-2026-11-05/report.md)，保留原始失败、诊断、独立探针源码与成功输出。
- 负责人：hyprial-cli-developer（本轮派单方，后续 fixture 修复协调）。

## KP-018 受限 macOS 全量验证的基线失败与超时

- 现象：在冻结基线 `89de5d013fbcd28e0521ff25dd7589c5ae36703f` 与 Claude resume 候选上回放同一 78 个失败/错误 ID，基线 41 failed / 37 passed / 1 teardown error；修正本轮九项新增失败后的候选为 42 failed / 36 passed / 1 teardown error。额外的 Lark authorization cleanup 用例单独重复后两侧均报 `authorization worker cleanup is unconfirmed`。
- 影响：候选完整运行在收集 18500 项后按时限中断，留下部分 66 failed / 18315 passed / 39 skipped / 7 xfailed / 2 xpassed / 14 errors；不是完整全量结论。共同失败还包含 Codex 子进程提前退出、ps 与 /var/tmp 权限拒绝、hosted preexec 失败、CLI org remove --device 快照漂移、membership removal 的 IPC 字符串常量、CI/build context 清单等。会话注册超时的顺序/负载原因未定。
- 规避：只在自建 home、短 pytest 根和 loopback NO_PROXY 中回放；保留原始失败与每项 skipped/error。需要允许对应本机操作的执行环境继续验证；沙箱外复测未获批准，不跳过、不放松断言来宣告通过。
- 决定：待测。本轮只修复 Claude native session 持久化/恢复及其引入的两类测试问题；不将共同基线失败统称环境问题，不顺手修 unrelated 功能。最终代码的完整全量仍为 NOT_RUN。
- 依据：[Claude resume 完整报告](../notes/claude-session-resume-2026-11-05/report.md) 与其中的基线回放、逐项 JUnit 投影和未报告范围。
- 负责人：hyprial-cli-developer（本轮派单方，后续全量验证与问题拆分协调）。

## KP-019 隔离全量 pytest 关闭收尾异常

> 编号说明：交付报告（`docs/notes/harness-args-start-2026-11-05/`，哈希已冻结）中记为 KP-018；合入 0.5.5.3.4 时与已有 KP-018 撞号，改为 KP-019。

- 现象：本单在独立 HOME/TMPDIR、去除 worker 身份与代理变量的环境运行固定源码 `f6b9dd357c49ed8cb1ec73f525803d4f6842caea` 的全量 `tests/ -n 4`，进度到 99% 后仍长时间收尾，并出现 teardown E；测试目录内日志停在 daemon 关闭资源阶段。此前受继承环境污染的全量失败栈还包含 `daemon log writer did not drain`。本单没有把两者断言为同一根因。
- 影响：该次全量执行不能给出完整 gate 结论；不建立生产 daemon 故障或 stored harness_args 新回归的结论。
- 规避：暂无验证过的全量规避。独立运行同名 ACL 入口用例在冻结基线与候选源码上各通过一次，不能以单例绿替代全量，也不能宣称全仓基线等价。
- 决定：待测；本单只修参数持久意图与启动快照，不修改日志关闭逻辑，不延长断言预算或增加 skip。源码随后因 CLI import ratchet 修正而更新，该全量被停止并报告 INCOMPLETE。
- 依据：[参数启动报告](../notes/harness-args-start-2026-11-05/report-v2.md)，其中保留执行环境、独立对照与中断事实；具体根因 UNKNOWN。
- 负责人：hyprial-cli-developer（交付协调者，后续独立排查）。

## KP-020 单独收集 OrgFS HTTP CLI 检查测试时根命令未注册

- 现象：`uv run pytest tests/daemon/orgfs/server/http/request_contracts/test_request_contracts.py::test_fs_web_check_cli` 报 `RuntimeError: Could not get a command for this Typer instance`；全 OrgFS 目录收集同样失败。测试直接把 `shell.impl.cli.commands.common.root.app` 交给 `CliRunner`；入口注册差异是待确认的原因，不将其推断为产品 CLI 故障。
- 影响：本机 Python 3.14.2 / Typer 0.27.2 的独立焦点门；未修改 dev `db184708b45ad90efa9b557314f87e1c2d099fca` 和 journal validity 候选均复现同一异常。真实入口的隔离 CLI 契约 14/14 通过；本条不能推出全量套件必然失败或通过。
- 规避：报告该独立门 FAIL 并保留基线对照；使用正式 CLI 入口的契约结果单独列出，不用其冒充该测试通过。
- 决定：待修，独立于 `hotfix/orgfs-validity-from-journal-v2`；不为此改动产品命令注册或放宽断言。
- 补充案例（2026-11-14）：`tests/shell/cli/orgfs/test_space_purge_cli.py` 单独收集时三项同样因未装配 Typer 根入口失败；固定父 `837eaad9d` 与候选 `7484087aa` 均复现。与会装配真实 CLI 入口的 `test_cli_read.py` 合并收集为 16 PASS。此项为同类测试入口问题，不证明产品 purge 功能通过；产品无效输入 JSON 输出缺陷另见 KP-062。
- 依据：`docs/notes/orgfs-validity-from-journal-v2-2026-11-05.md`；候选全 OrgFS 读数 `1 failed, 763 passed, 3 xfailed`，基线单测 `1 failed`，显式 loopback NO_PROXY 下仍复现。
- 负责人：R&D 负责人（CLI 测试维护）。


## KP-021 post-install profile 调用差异：CI 风险未验

- 现象：精确断言 `tests/shell/cli/installation/restart/post_install/test_post_install_fence.py::test_every_profiled_function_rejects_a_new_lazy_import_under_m2[hyprial.daemon.impl.autoupdate.alert:record_alert_outcome-child-exited]` 在候选默认调用两次出现原有 90 秒 `subprocess.TimeoutExpired`；未改 dev 自有环境单跑 1 passed/3.78s，base 源码配 candidate 项目环境 1 passed/4.04s，candidate 源码配 base 项目环境 1 passed/4.18s。两环境包名/版本集合无差分。
- 影响与边界：候选自有环境显式 `PYTHONPATH=.../dev-tree/src` 得 1 passed/3.60s；经 CI 同一 `pytest_subprocess_environment`、无 PYTHONPATH 的本机调用也得 1 passed/3.76s。因此未设 PYTHONPATH 不是充分触发条件，根因仍 UNKNOWN；不能归为已证既有红或已证本 PR 回归。真实 Linux/Python 3.12 CI 未验，本机为 macOS/Python 3.14.2。
- 规避：保持 CI 风险未验，不把本地通过、单跑通过或字节一致当放行证据。生产调用环境未读取、结论 UNKNOWN。
- 决定：独立待立项；不在 journal validity hotfix 中改产品、断言或测试阈值。CI 恢复后的第一道门是在 PR 最终 head、真实 Linux/Python 3.12、shard 同一安全环境和 python -m pytest 形态下先跑此精确断言。合并前须取得该真实 CI 读数，或由合并权人显式接受风险；不预测其必然失败。
- 依据：`docs/notes/orgfs-validity-from-journal-v2-2026-11-05.md`；独立本地证据仓库 `/private/tmp/pac-orgfs-validity/verification-evidence` 的 profile 观察与实际命令回执。实际 CI action 401 阻断来自调度方报告，不冒充本机读取过关联 job 日志。
- 负责人：R&D 负责人（升级/profile 与 CI 验证线，待立项指派）。

## KP-022 完整测试组合中的无上界子进程等待

- 现象：`tests/tooling/contracts/test_contract_python_resolver.py::test_key_mutations_are_killed[regular-file-fence]` 的外层 `subprocess.run`（887–899 行）没有 timeout，等候复制目录内的子 pytest。默认全量的 case 目录建立于 2026-11-04 18:31:13 UTC；子进程 PID 30622 的原生出生时刻为 18:31:15.886187 UTC，后续仍驻留其 mutant 目录。进程链为 uv 9808 → pytest controller 10272 → worker 11801 → 子 pytest 30622，证据包含 PPID 与 cwd 快照。
- 影响：完整 tests/ 调用可能长期无法获得终态与完整计数；三种调用必须分列，不能相加为通过率。无 timeout 的代码在未改 dev 已存在，但这不证明组合阻塞与本 PR 无交互。
- 对照：同一精确 case 经 CI 同一安全环境、单 worker 单跑，在未改 dev 得 1 passed/17.79s，在候选得 1 passed/21.83s；两次外层 120s 诊断预算均未触发，未改任何测试阈值。单跑通过不排除组合条件下挂起，根因 UNKNOWN。
- 规避：调度方批准以固定请求 deadline 减 10 分钟（2026-11-04 20:56:27.275 UTC）为本轮停跑线。未获得完整终态时保留 INCOMPLETE/BLOCKED 与缺失信息；不以中途百分比推导实测测试数，不设全量 PASS 或可合并标记。
- 决定：独立待立项。调度方另报告上午 31% 停滞/Pykka dump 与 log writer 未 drain 两起，建议按无上界等待的共同形状合并调查；这两起不是本机复现，也没有证实共同根因或第三方归因。
- 依据：`docs/notes/orgfs-validity-from-journal-v2-2026-11-05.md`、`verification-evidence/observations/process-tree-current.txt`、原生 PID 出生记录与两侧独立诊断回执。
- 负责人：R&D 负责人（测试 runner/进程退出线，待立项指派）。
## KP-023 GUI production-copy 基线命中 `fixture`、`演示`

> 编号说明：release/v0.5.5.4 线上原记为 KP-009（`docs/notes/gui-m1-source-entry-20261105/`、`docs/notes/gui-model-key-composition-20261105/` 等证据中仍写 KP-009）；合入 0.5.5.3.4 时与已发布的 KP-009 撞号，改为 KP-020（per Allen 2026-11-07，合并保留两线条目）；2026-11-07 intg←dev 同步时 dev 已占用 KP-020/021/022，再改为 KP-023。

- 现象：`npm run verify:production-copy` 在构建成功后报 `Error: Production copy contains forbidden markers: fixture, 演示`。
- 影响：只影响 GUI production-copy 收尾门；不影响本包已初始化的源码入口、request-http v1、同源 Node 宿主或实际 Chromium 验收。拦截器本身仍有效，不能为通过而删除或放宽。
- 规避：本包按已知基线记录并把该门保留为红；修复 production 构建中的 fixture/演示文案后重新跑整条命令。
- 决定（2026-11-09 更新）：已修复（集成 PR #2612）。协调者此前在 `a09eee736b10` 与 `eca622e20042` 独立复现；本包在合并目标 `ffc989bbb7c8099089fd8b0ad98c507e6f72074b` 上再次确认，证据见 `docs/notes/gui-m1-source-entry-20261105/03-evidence.md`。`verify:production-copy` 现为 GUI dev/product 等价改造三层门中的产物门（[gui-dev-product-parity-2026-11-09](../design/gui-dev-product-parity-2026-11-09.md)）；集成 HEAD `5975e9e2c9c5b361af90257cf3536b8002bc99d2` 上该门实测 0 forbidden markers，完整 `npm --prefix gui run verify` 同时通过，本条闭合。
- 负责人：GUI/前端负责人（收尾时与 R&D 负责人复核）。

## KP-024 受限沙箱/默认长 TMPDIR 下真实 socket 测试不可执行

> 编号说明：release/v0.5.5.4 线上原记为 KP-010（`docs/notes/gui-settings-read-registry-20261105/`、`docs/notes/native-resident-inventory-split-20261105.md` 等证据中仍写 KP-010）；合入 0.5.5.3.4 时与已发布的 KP-010 撞号，改为 KP-021（per Allen 2026-11-07，合并保留两线条目）；2026-11-07 intg←dev 同步时 dev 已占用 KP-020/021/022，再改为 KP-024。

- 现象：在 Codex 的 workspace-write 沙箱中，任何 `bind()`（TCP 或 AF_UNIX）返回 `PermissionError: [Errno 1] Operation not permitted`；`tests/daemon/identity` 因此出现 23 failed / 1 error，`tests/integration/request_http/identity/test_identity_reads_production.py` 也 BLOCKED。另一份不受该沙箱限制、但使用 macOS 默认长 `TMPDIR` 的全量日志里，`test_message_status_cli_entries_round_trip_over_an_isolated_unix_socket` 报 `OSError: AF_UNIX path too long`，随后 fd 卫生门把未关闭的 SQLite fd 记为该测试的 teardown 泄漏。
- 影响：只影响本地验证可执行性，不指向产品逻辑；同一批非 socket 测试与真实 `DaemonApplication.handle` 测试均通过。长路径问题已有独立记录（`docs/notes/gui-http-constraints-20261104/verification.md`、`docs/notes/gui-round2-review-focus-2026-11-01.md`）。
- 规避：焦点测试设置 `TMPDIR=/tmp`；需要 socket 的用例在具备 loopback/AF_UNIX 权限的宿主运行，并给 socket 根使用短路径。沙箱内用 `--noconftest` 运行不依赖 fixture/socket 的焦点文件，结果中明确标注未跑项。
- 决定：环境归因，不修产品；不把 `PermissionError` 或 `AF_UNIX path too long` 记为本次回归，也不以 skip 冒充 PASS。
- 负责人：R&D 负责人。依据：2026-11-05 settings reads 交接运行的 `/tmp/gui-read-registry-isolated-full-pytest.log` 与本轮 reads 焦点复跑。

## KP-025 GUI 首跑浏览器用例等待不存在的按钮

- 现象：`npm run test:browser` 的 375/760/800/900/1440px 五个用例都在 `completeFirstRun` 等待 `getByRole('button', { name: '登录 Hyprial', exact: true })`，30 秒后超时；`verify:first-run-browser` 的生产场景也在账号流程等待 `我的账号与连接菜单` 超时。
- 影响：开发预览 chrome 与首跑生产浏览器门为红，不能据此判定其余 GUI 浏览器场景或生产功能失败；157 个 Vitest 文件（1661 项）与真实 daemon loopback 可独立通过。
- 规避：首跑流程与按钮契约对齐前，保留这些门为红并单独报告；不通过删除用例或放宽等待来放行。
- 决定：待修（共同基线，本单不改首跑页面）。2026-11-08 在候选树与未修改的 `release/v0.5.5.4` @ `ff02fc5ca00c29e18b11d832876f6de334b947ec` 独立 worktree 上执行相同命令，开发预览均得到 0 passed / 5 failed；生产首跑均在相同账号菜单 locator 超时。
- 复测（2026-11-11，`feat/gui-full-acceptance` 候选工作区）：重新构建 `gui/dist` 后，`npm --prefix gui run test:browser` 的五个开发视口和 `verify:first-run-browser` 的开发/生产各四个视口均通过。`verify:first-run-browser` 的开发契约改为只排斥业务请求，允许浮动开发工具按需加载的同源静态资源。此复测不能代替真实账号/M2 验收；正式闭合仍须最终候选复跑。
- 负责人：GUI/前端负责人。

## KP-026 safe-r1 开发构建缺少开发标记

- 现象：`npm run verify:safe-r1-build` 报 development build 缺少 `开发模块索引` toolbar、`dev-01` 至 `dev-06`、preview label 和 data-source notice。
- 影响：safe-r1 构建门在候选与基线均为红，不能据此判定生产构建失败；普通 `npm run build` 与 production-copy 可独立通过。
- 规避：保留该构建门为红，单独报告；不为放行删除开发标记断言。
- 决定（2026-11-09 更新；原决定“待修（共同基线）”已被 GUI dev/product 等价改造取代）：等价改造（目标见 [gui-dev-product-parity-2026-11-09](../design/gui-dev-product-parity-2026-11-09.md)，集成 PR #2612）决定 development build 不再包含产品内常驻开发标记（`开发模块索引` 工具栏、`dev-01`–`dev-06`、preview label、data-source notice 及改变产品布局的 `.dev-preview-frame`）；这些常驻组件退役，开发期附加 UI 收进产品 `#root` 之外的独立浮动工具层（默认收起、点击展开、Escape/外部点击关闭），不进入正式构建，production 不挂载开发工具 root。因此不再以“恢复这些标记”为修复方向，不得为对齐本条旧断言而恢复常驻开发组件；`verify:safe-r1-build` 已改写为检查独立浮动工具、数据来源说明与模块索引，并检查正式构建不含开发内容；`verify:safe-r1-browser` 同步改为展开/关闭浮动面板后验证，两个契约在集成 HEAD `5975e9e2c9c5b361af90257cf3536b8002bc99d2` 均实测通过，本条闭合。构建防回归由三层门承担：静态门 `verify:dev-product-parity`、浏览器门 `verify:dev-browser-parity`、产物门 `verify:production-copy`（正式 bundle 不含开发工具与 fixture 文案的既有方向由产物门继续保障），完整 `npm --prefix gui run verify` 同时通过。
- 历史事实（保留）：2026-11-08 在候选树与未修改的 `release/v0.5.5.4` @ `ff02fc5ca00c29e18b11d832876f6de334b947ec` 上复现相同缺项。
- 负责人：GUI/前端负责人（等价改造集成后复核三层门与 safe-r1 门的替换形态）。

## KP-027 native engine 的五平台 wheel 发布尚未接通

- 现象：#2514 的正常 wheel 构建要求目标 OS/架构等于宿主，且携带 `cp312-abi3` actor 扩展；现有 `wheel-release.yml` 却在单个 `release-ci` 作业中循环五个平台。wheel 校验器、R2 清单读取、GitHub 附件计划及 POSIX/Windows 安装器仍硬编码 `py3-none`，即使编译完成也不能消费新文件名。Linux native 还必须证明 glibc ≤ 2.17，不能以普通 Bookworm 容器代替该 ABI 验证。
- 影响：合入 actor 的 0.5.5.4 候选尚不具备完整的构建、分发和安装链路；这是已确认的实现不兼容，不只是缺一份 CI 绿。2026-11-08 的只读 job 日志仅证实近期有 Linux x86_64 和 macOS arm64 执行环境；另外三平台容量与所有 runner 当前在线情况仍 UNKNOWN。runner 清单 API 因缺少组织 owner 权限返回 403，不能据此断言 runner 不存在。
- 规避：保持 `INTERNAL_PUBLICATION_ENABLED="false"` 与 `FORMAL_PUBLICATION_ENABLED="false"`，本轮只准备候选，不移动轨道或发布新版本。不得使用 `HYPRIAL_BUILD_SKIP_NATIVE=1`、提高 glibc 下限或伪写 `py3-none` 标签绕过发布门；这两个提交内的 hold 不等于旧 ref 上全部发布 workflow 已被管理员禁用。
- 决定：待修，作为 0.5.5.4 发布阻断项单独完成五平台构建与兼容迁移；不阻止已授权的 dev 集成。安装器有独立 dev 自动发布路径，本轮不修改；后续必须同时兼容既有轨道的旧 wheel，不能先部署仅认识新 ABI 的安装器。
- 依据：[native wheel 发布准备与验收计划](../design/native-wheel-publication-0554.md)，记录固定审计源码、真实 run/job、API 权限边界、全部消费路径和解除冻结条件。五平台候选实测为 BLOCKED，未执行，不记 PASS。
- 负责人：R&D 负责人协调 CI/发布维护者；组织 owner 提供 runner 清单与容量确认，分发/安装维护者完成 ABI 消费契约迁移。

2026-11-09 消费兼容修复候选已准备：上述三类校验/发布消费者与两个安装脚本接受严格的
旧 `py3-none` 或新 `cp312-abi3`，发布集合不得混合 ABI；native actor 的存在/普通文件/
架构与 WHEEL 标签同步校验，旧标签隐藏 native actor 被拒绝。安装仍要求唯一目标匹配、
摘要及版本回读，新 ABI 请求 CPython ≥3.12。该候选不修改 workflow 或冻结开关；五平台
构建、真实 ABI/RECORD/GLIBC 与安装验证仍未完成，本问题继续保持发布阻断。

## KP-028 message thread/history 协议错误词汇与 import ratchet 尚未对齐

- 现象：`check_request_contracts.py` 的 importRatchet 子门拒绝 `src/hyprial/shell/impl/cli/commands/message/threads/history.py` 与 `reader.py` 各自新增的 `from hyprial.kernel import ipc_errors`；每个文件各有 `base-import-edge-growth` 和 `new-cli-backend-import`，共四项违规。`legacyMethods` 子门可独立通过，但汇总门仍为 FAIL。
- 影响：这是当前集成基线的架构准入债务，不是 #2268 单路由检查器引入的问题，也不是已证明的运行时故障。冻结基线 `cb99e39e87844cc197ea3da4f9a9baf01e1f7214` 在独立 worktree 的实跑与 #2268 集成树得到完全相同的四项违规；`a9f06ff14a1da4bf2505ede4d1baf102a1bb7278` 两处代码仍相同。不能把焦点测试通过报告为整个 request-contract 门通过。
- 依据：`fb0e53fa2aedbb868518345af1811ce4bf28a174` 为修复 canonical IPC error literal 门引入两条 import；history 使用 `INVALID_ARGUMENT`，reader 使用 `TARGET_NOT_FOUND`、`AMBIGUOUS_TARGET`，后续 `3178f32d4` 还用 `TransientDaemonError` 保持 `agent.resolve` 失败时的旧别名回退。基线实测见 [#2268 集成证据](../notes/dev-first-single-route-2026-11-08/README.md) 与同目录 `baseline-import-ratchet.json`。
- 修复：保留公开错误常量、typed transient 捕获和既有回退语义，按既有 `protocol` exception 机制仅为这两个精确路径登记一条 `ipc_errors` 词汇依赖，并同步 reviewed digest 与 `reader.py` 文件头；没有改回字符串、隐藏 re-export、扩大 import baseline 或增加业务 helper/request path。
- 验证：独立 `check_import_ratchet.py` PASS，import/IPC/CLI 相关 48 项焦点测试 PASS，全仓 Ruff PASS。`test_no_literal_proto_code_outside_the_registry` 仍报告 `process_owner.py` 的 `ACTOR_RESTARTED_UNDELIVERED` 与 `authorization/ports.py` 的 `PAYLOAD_INVALID` 两处字面量；未修改 dev `ad907cf85d418401fa60357f0b5bf658f6a4b96a` 也复现同一失败，因此不得把本项修复写成全仓 canonical 扫描 PASS。
- 决定：本项 import-ratchet 债务已修复并闭合；上述两个非本项的存量字面量保持真实 FAIL，留待各自修复，不在本条中豁免或扩大范围。发布候选仍须记录该扫描的实际结果。

## KP-029 本地 IPC 方法门误判多 IM 平台方法命名空间

- 现象：#2063 多 IM 与 #2268 单路由检查器合并后，`test_repository_router_baseline_matches` 报三个 `unregistered-router-method`（`chat.postMessage`、`files.upload`、`files.uploadV2`）及一个 Telegram transport 的 `unresolved-method-table`。固定组合源码为 `8ccc36f4bf525b5288413ffd15bb99e0bbeb7f4f`。
- 原因与边界：`check_method_registration.py` 扫描整个 `daemon.impl` 中名为 `method` 的比较，以及名字含 `METHOD` 的模块赋值。Slack 的这些值来自 outbound `OperationNode`/HTTP transport，并非 daemon IPC 名称；Telegram 的 `_METHOD` 是 `re.compile(...)` 生成的 API 方法名称校验器，并非 IPC 表。不能因此把这些 SDK 名字登记到本地 IPC baseline，也不能按整个平台目录或任意方法名关闭检测。
- 影响：当前组合的 method-registration/相关 request-contract 门为 FAIL；这是扫描命名空间与新模块组合的失配，不证明 SDK 功能坏了，也不代表所有真实 IPC 路由已经验收。独立 #2268 焦点通过的旧组合结论不能沿用到加入多 IM 后的候选。
- 规避：本轮按“先合代码、后整理 CI”的已批准边界保留明确 FAIL；不放松真实 IPC shadow 检测、猜测重建 baseline 或无依据豁免整个 adapter 子树。Slack transport 的 `method` 同时属于 Protocol/可能的关键字参数，不能仅为消除扫描结果直接改签名；发送给平台的 wire `method` 字段也必须保留。
- 决定：已修复。Slack/Telegram 是外部 API 命名空间，本地比较变量改为 `api_method`，Telegram 正则改名为 `_API_PATTERN`，不属于本地 IPC dispatch。保留公开 `method` 参数和请求中的 `method` wire 字段，不修改 baseline、checker 或真实 IPC shadow 检测。方法注册测试包含外部 API 不被登记和真实 router 同名字面量仍被识别的负例；`check_request_contracts.py` 与 Slack adapter 焦点测试另行实测。
- 依据：[#2268 与多 IM 组合复现](../notes/method-sdk-boundary-2026-11-08/README.md)，记录固定源码、执行命令和原始失败；同批另有的 actor registry 展开格式失败不归入本条。
- 负责人：CI/请求契约维护者与多 IM adapter 维护者；发布负责人在最终候选重新跑组合门并记录真实结果。

> #2195 原分支 KP-009 至 KP-012 在 dev-first 合并时重编号为 KP-030 至 KP-033；历史证据保留原编号。

## KP-030 provider key 记录在 OrgFS 写入侧没有准入挂接点

- 2026-11-14 清理后复核 `ccca186d5`：U2 仍只有本地验证能力；`user_key/ipc.compose` 的 signer 默认 disabled，`user_key/publication.publish` 拒绝，登录的 user-key 阶段不调用 issuer。已合入 #2195 的说明及 `contract/identity/user-key.md` 同样明确未交付生产发行/撤销/发布。T10 须先核对这组依赖，再完成 store/准入装配，不能以本地自签或手动打开 gate 作为修复。原生 Auth 文件导入不依赖该共享 Key 服务，可独立接线。

- 2026-11-13 GUI 复核补充：当前 `provider_keys/context.py` 默认 store resolver 为 UNAVAILABLE_RESOLVER，owner key port 也未装配；新 home 默认 legacy gate，登录/初始化没有自动补齐此组组件。真实 generated client→HTTP→daemon 的 `user.provider.login` 返回 `PROVIDER_STORE_UNAVAILABLE`。HTTP MethodSpec 存在不意味着 GUI 共享 Key 保存/授权可用；仅手动打开 gate 不解决正式装配。证据：[启动组合核查](https://code.hyprial.com/HyprialOS/hyprial/src/commit/51f7efd1898251e5ac04ca331a9514c8cca2e51d/docs/notes/gui-harness-credential-matrix-2026-11-13/README.md)。

- 现象：冻结契约 §4.5 要求对 `directory/provider-keys/**`、`directory/provider-grants/**` 的写入做准入（主人设备、owner 签名、seq 单调、deleted 终态、信封摘要一致）。OrgFS admission 只能看到 `(doc_id, author, update)`，看不到路径；provider 路径要么需要确定性受保护 doc id，要么需要 S3 owner-home 的 `codec.verify`，原 PR 基线尚无这些空间。当前 dev 已有 S3 owner-home，但 provider resolver、owner 标识映射和 provider-record 写入准入仍未接通；不能把空间存在等同于信任边界已就绪。
- 影响：任何对该空间有写权限的成员都能写入或删除 provider 记录。读取侧逐条验签、本地写前 transition 校验和持久回滚标记（`daemon/impl/provider_keys/ops/marks.py`）会让这类写入 fail closed（拒绝启动或报错），但无法阻止写入本身；也就是说可以造成拒绝服务，不能伪造授权。
- 规避：功能门 `providerKeys.authority` 默认 `legacy`，生产存储解析器默认不可用（`PROVIDER_STORE_UNAVAILABLE`）。在空间和准入落地之前，不打开 `owner-keys`。
- 决定：BLOCKED，完成同一 S3 空间的 resolver/身份映射后在 `daemon/impl/orgfs/storage/store/admission_support/` 增加 provider 准入 mixin（见 #2195 review 1407、1427）。
- 负责人：provider key 后端线（#2195）。

## KP-031 provider key UsageReceipt 在解封时写入

- 现象：`launch_secrets` 在所有槽位都校验通过后，一次性写入 `injected` 回执；如果在其后的旧 grant 解析或子进程环境构建阶段失败，启动被拒，但回执仍记为 `injected`，`lastUsedAt` 也会更新。
- 影响：只影响本地用量回执的精确性，不涉及明文，也不改变启动结果。
- 规避：无。
- 决定：本版不修。要做到精确，需要把 `ProviderKeyService.launch_secrets` 改成“解封”和“提交/拒绝回执”两阶段，这会改动冻结的 seam（`daemon/impl/provider_keys/api.py`），留待后续版本。
- 负责人：provider key 后端线（#2195）。

## KP-032 植入的、锚不可用的 provider-key meta 让 list 与旧查找 fail closed

- 现象：`directory/provider-keys/<keyId>/meta.json` 如果是被植入的、签名钥在 U2 锚里查不到（unavailable）的记录，`user.provider.list` 和旧 `agent.secret.provider-write` 的 legacyRef 查找会报 `PROVIDER_STORE_UNAVAILABLE`。grant 记录的同类情况已改为逐条跳过并告警。
- 影响：拒绝服务，不会泄漏或伪造。契约规定 `invalid[].code` 只能是 `PROVIDER_KEY_SIGNATURE_INVALID` 或 `PROVIDER_KEY_NOT_FOUND`，§9.5.3 又不允许把 unavailable 报成篡改，因此保持 fail closed。
- 规避：由主人删除该植入文件。
- 决定：本版不修，由 KP-030 的写入侧准入根治。
- 负责人：provider key 后端线（#2195）。

## KP-033 provider key 回滚标记只在本地 state 目录存活期间有效

- 现象：持久回滚标记 `state/provider-keys/high-water-*.json` 记录每条记录已见过的 `(seq, digest)`，能拒绝重启后被回放的旧记录和被删除的记录。但 state 目录被清空或换新设备后，标记从零开始。
- 影响：在没有写入侧准入的前提下，新 state 目录上第一次读取到的被回放旧记录会被接受。跨设备的回滚防护依赖 §4.5 准入。
- 规避：功能门默认 `legacy`；本阶段只支持主人自己的单设备。
- 决定：本版不修，由 KP-030 根治。
- 负责人：provider key 后端线（#2195）。
## KP-034 组织创建长链浏览器用例在重复创建后的文件页切换超时

- 现象：2026-11-08 在 dev `e8d38384372e06a14601090fece5833beeb92b58` 与 #2523 完整合并树上，修正 GUI 对真实隔离 `org.create` 的 nullable 发布状态消费后，`actual CLI org.create reaches the shared client for both drivers without a fake success` 的 daemon 用例在 `runOrgCreateBrowser` 等待“文件分组 → 回收站”20 秒后超时。该步骤位于文件写入/删除/恢复、文件夹恢复和重复创建流程之后。
- 影响：该完整长链为 FAIL，不能宣称完整组织/文件操作已通过浏览器验收。此次响应记录确认真实隔离 daemon 返回 `tailnetInitialized=false`、`published=null`、`devicePublished=null`，GUI 已显示本地创建成功，并真实读取 `orgfs.members`；这些局部观察不替代整条用例。
- 规避：分别运行聚焦本地创建、成员投影和幂等重复创建的真实 daemon 用例，保留长链失败；不删断言、不放宽超时、不把 nullable 标志伪造成 true。测试使用独立 home/state/socket、合成 owner 和本地 membership fixture，不接生产凭据或真实 mesh。
- 决定：待排查。当前 locator 与组件的 aria-label 相符，根因 UNKNOWN；没有做未修改 dev 的同条件回放，不能判定为已证基线故障或本 PR 回归。本轮成员集成不扩展到文件页长链状态调查。
- 依据：[#2523 集成记录](../notes/dev-first-org-members-2026-11-08/README.md) 与同目录 `daemon-create-browser.log`，记录执行命令、失败栈和焦点验证边界。
- 负责人：GUI/组织文件流程维护者；CI 整理阶段在最终集成源码重放并定位重复创建后的选中组织/页签状态。

## KP-035 隔离 GUI 焦点验收的 daemon 准入启动失败

- 现象：#2523 集成期间，新建 `isolated daemon local organization creation preserves nullable publication and lists its member` 焦点用例共运行三次：首轮 dev 页面等待 networkidle 超时；改用 DOM 加载及明确连接状态后，第二轮在浏览器启动前得到 `GUI_DAEMON_UNAVAILABLE`；第三轮增加有界的真实 `ping` phase 为 serving/reconciled、无 restorePending 且 catalog 同 epoch 等待后，GUI 自身准入仍返回同一错误。
- 影响：最终焦点运行 FAIL 于启动，成员/重复创建的完整浏览器行为验证 BLOCKED。先前另一完整长链曾成功启动并真实创建本地组织、读取成员（随后因 KP-034 失败）；它不能证明新焦点的完整断言已通过。没有证实共同基线故障、环境归因或本 PR 回归。
- 规避：目前没有已验证的稳定规避。保留启动失败，不提高产品准入 timeout、不替换 daemon 响应、不关闭隔离；每轮仅清理本用例的 GUI/daemon 子进程和临时目录。
- 决定：待排查，本轮不扩展到 daemon/GUI 准入产品修改。真实服务就绪后的 CLI 准入为何失败仍为 UNKNOWN，不能把 socket 存在、单次 ping/catalog 成功或单测通过等同于 GUI 入口验收。
- 依据：[#2523 集成记录](../notes/dev-first-org-members-2026-11-08/README.md) 中三次启动日志；首次日志的短期私有 handoff grant 已脱敏。
- 负责人：GUI launcher/daemon 就绪链维护者；后续在最终候选独立复现，并保留 daemon 日志与 admission 错误原因。

## KP-036 独立方法注册测试额外报告已迁移方法的旧路由 shadow

- 现象：`tests/tooling/architecture/ipc/test_method_registration.py::MethodRegistrationTests::test_repository_router_baseline_matches` 的独立检查在固定 dev `88df3a586383cc74c837505f982bedaaf76e62cd` 报 146 项，在 #2214 普通 OrgFS work 候选报 134 项。归一化新增项为空，减少项恰为本次明确退休的 12 个 `work.*` 方法。除 KP-029 的平台 SDK 项外，该入口还把已迁移方法报告为 `registered-legacy-method-shadow`。
- 影响：此 pytest 门仍 FAIL；不能将运行时焦点通过等同于整个方法注册门通过。该入口的结果与 `check_request_contracts.py` 汇总检查不同，后者必须独立运行和报告。
- 规避：保留失败与精确基线对照，不跳过测试、不扩大 legacy baseline、不豁免整个 router。当前只按已批准的接口退休缩减 work 条目。
- 决定：已修复。检查器只识别同一函数内前置的、无 `else`、完整返回当前 `method` 给 `METHOD_REGISTRY.invoke` 的分支；此后的拒绝分支不可达已注册名称，不再误报 shadow。条件分发、转发其他 method 和不完整守卫仍拒绝。未修改 baseline 或产品路由；真实 router 字面量、shadow 和伪守卫测试保持有效，方法注册与 Slack/Telegram 共 71 项焦点测试及完整 request-contract 均 PASS。
- 依据：`docs/notes/work-orgfs-2026-11-08/` 中的方法门对照与后端验证记录；完整原始检查保存在本次任务审计目录。
- 负责人：CI/请求契约维护者。

## KP-037 隔离 checkout 全量 pytest 收集遇到四类既有环境与导入问题

- 现象：#2214 固定候选 `ac76d4ba24e425e8664c4a2215d79eb1ee109c58` 按 `uv --with libcst==1.9.0` 对 `tests/` 执行 collect-only，收集 26,133 项后因 4 个错误退出：Zenoh 测试在 import 时要求本 checkout 的 `.venv/bin/hyprial`；跨版本测试默认旧解释器指向不可访问的 `/root` 路径；forwarding/membership 与 ipc/contracts/membership 两个 `test_migration.py` 在默认导入模式同名；link 集成测试从未导出 `_ipc` 的 fixture 模块导入它。
- 影响：该环境不能完成默认全套收集，因此本轮不宣称全量 pytest 通过。没有错误指向删除的 work 模块或方法。
- 规避：使用显式焦点路径验证改动，保留全量收集 FAIL；工具测试必须按 AGENTS 使用 LibCST overlay，避免把缺依赖的额外 10 项错误当成产品回归。不扩大忽略清单、不修改生产路径。
- 决定：留待 CI 整理阶段分别修复环境入口与两处导入问题。相关 29 文件与精确 dev 基线对照确认根因代码/包结构已存在；唯一改动是测试函数内删除 work 家族期望，不涉及导入时行为。
- 依据：[收集复核](../notes/work-orgfs-2026-11-08/collection-review.md)，包含命令、真实日志位置与基线比较范围。
- 负责人：CI/测试基础设施维护者。

## KP-038 当前 dev 的网络可达性门存在一项新路径和一项失效 baseline

- 现象：`check_network_reachability.py` 在 dev `284dcf72942b1b1b2495c9bbddc1e646cfc386a3` 报 `newViolationCount=1`、`staleBaselineCount=1`。新 fingerprint 为 `ccf5d6a7fa919e13b652ed6b7096cbbc28cb454c65bbe4a5e2194ff3b4e9584c`：IPC send 经 agent-bot、Discord HeartbeatSupervisor 到 ForwardingSidecarController._send；失效 fingerprint 为 `eaa63db54b374230e4815187a1bccaff24e814452ca502a36a6d1afa4aed7fb1`。
- 影响：`gate-python / cli-help-inventory` 及其汇总门为 FAIL。#2586 的首次远端 run 52259 / page 41886 与未修改 dev 的独立本地运行给出相同路径和 fingerprint；该 PR 没有修改产品源码、此检查器或 baseline，不能归因于新的 dev 来源规则。
- 规避：保留真实失败和对照；不扩大 baseline、关闭门或把声明重跑的轻量绿当作完整验证。此静态路径是否代表真实同步网络调用仍需分析，不据此宣称运行时故障已被证实。
- 复核：`provider.py` 将 `HeartbeatSender` 绑定到 `DiscordTransport.send` 的 gateway op1 heartbeat，不是 `ForwardingSidecarController`；扫描器对 `self._send()` 的保守同名回退形成静态别名，并非已证实的 Tailcat 调用。
- 决定：已复核并闭合。移除已无调用路径的 `RemoteWorkflow.send` 旧项，仅登记上述精确 fingerprint 和完整静态路径，baseline 总数保持 43，不改变运行时网络边界。`check_network_reachability.py` 实测 `newViolationCount=0`、`staleBaselineCount=0`，相关 7 项测试 PASS。
- 依据：[远端 checkout 与 baseline 对照](../notes/ci-dev-first-routing-2026-11-08/remote-followup/README.md)。
- 负责人：CI/daemon 网络边界维护者。

## KP-039 显式转发目标的旧进程原位升级兼容尚未验收

- 现象：真实 v0.5.1 升级器安装新包后仍在旧 Python 进程中启动新 daemon。精确旧 prime 与新文件组合证实旧 kernel/identity 公共面缓存会遇到新 lazy 模块；默认无 target 路径先报 FileLockBusy，随后提前加载 forwarding policy/tailcat 又报新预算与设备绑定符号缺失。本 WIP 复用现有可传递锁并把未使用配置依赖移回实际分支，默认旧启动器已在控制中走到真实 FD 接续。
- 影响与边界：该控制只证明精确旧启动器到 spawn 边界及当前子进程采用锁，不等于完整 E2E-010、安装后消息连续性或所有部署配置通过。显式设置 HYPRIAL_FORWARDING_INBOUND_TARGET 时仍需加载并校验完整 forwarding/identity 依赖；旧缓存缺新符号的该分支仍会响亮失败，真实显式目标部署的完整升级尚未执行。不能把无 target 控制或现代配置测试的绿沿用到它。
- 规避：尚无经过实测的全配置升级规避；不删除用户转发配置、不增加宽泛旧接口别名或绕过 Unix target、代际与 inode/FD 校验。本轮维持发布准备 WIP 和现有 publication holds，不把聚焦绿当作升级验收。
- 决定：待修/待测。发布前增加包含真实显式转发目标、旧解释器缓存和完整重启/数据连续性的固定候选验证，明确配置迁移与恢复安排；默认路径修复不承担未经验证的兼容承诺。
- 依据：[真实旧启动器、红绿与交接证据](../notes/e2e-upgrade-legacy-tail-2026-11-08/README.md)，[固定 41e3 macOS 失败矩阵](../notes/e2e-macos-41e3-2026-11-08/README.md)，[E2E WIP #2580](https://code.hyprial.com/HyprialOS/hyprial/pulls/2580)。旧版基线和既有升级断言保持，非空 target 冷导入失败控制也保持。
- 负责人：升级与 daemon forwarding 维护者；发布负责人在解除冻结前确认对应实测和升级安排。

## KP-040 测试 DERP override 的 hostname 与 IPv6 不能建立拨号地址

- 现象：Tailcat `regionFromOverride` 只在 Host 是 IPv4 literal 时填 IPv4，其他情况填 none，IPv6 同样始终为 none。实际依赖的 DERP client 将双 none 解释为禁用全部拨号，而不是 DNS 解析；这不是生产默认 DERPMap 的普遍结论。
- 实测：同源真实 Go/Unix/正常组织生命周期，仅把私有 relay Host 从127.0.0.1换为localhost后，join从4步骤PASS变成DAEMON_ERROR，再换回IPv4恢复4步骤PASS。IPv6 literal具有相同静态分支，但没有单独的真实IPv6验收。939 Docker使用容器名时也失败，原现场底层同因仍UNKNOWN，不能拿受控对照代替它。
- 影响与规避：自定义/测试 override 的hostname路线不可作为正常已验收配置；本轮001 fixture用Docker inspect核验本次owned internal bridge/container/networkID/label/IPAM中的RFC1918 IPv4，没有放宽产品或绕过membership。下一候选的完整跨容器7阶段仍待实测。
- 决定：Go override的一般DNS/IPv6支持留待网络维护者单独修复和验证，不在fixture修复中扩大产品范围；保留明确失败与实际支持边界。
- 依据：[IP/hostname真实对照与边界](../notes/e2e001-relay-ipv4-2026-11-08/README.md)，[939 Mac矩阵](../notes/e2e-macos-939-2026-11-08/README.md)。
- 负责人：Tailcat/测试网络维护者。

## KP-041 org.join 的错误数据与声明契约失配遮住下层诊断

- 现象：真实hostname负控中的正常org.join返回DAEMON_ERROR，公开错误为 org.join handler attached undeclared error data；下层异常未以合法声明结构交给调用方。939 Docker原helper又只打印code，缺少error/steps，不能据其DAEMON_ERROR推断原现场具体下层原因。
- 影响：正常成功路径与错误路径必须分开验收；上层通用错误会阻碍组织加入故障定位。当前未证明数据丢失，也不据此关闭声明错误门。原939四个owned日志在只读诊断中成功读取但未选出可输出错误，底层现场仍UNKNOWN。
- 规避：001 helper仅白名单输出有限code/error与step状态，并移除完整invite、解码token、私有地址及opaque bearer；不回显原stdout/stderr或恢复未知字段。它增加诊断，不修复handler生产端的错误数据失配。
- 决定：待复核/待修。应按真实声明整理org.join错误生产端及安全服务器诊断，保持fail closed；不放宽全局schema、不吞异常、不把fixture正常通过当作此错误契约已修。
- 依据：[hostname负控及脱敏红绿](../notes/e2e001-relay-ipv4-2026-11-08/README.md)，[939矩阵](../notes/e2e-macos-939-2026-11-08/README.md)。
- 负责人：Org membership与请求契约维护者。

## KP-042 GUI Dialog 与 Select 的 Esc 和层级组合

- 现象：#2579 的隔离探针中，首次 Esc 同时关闭 Select 和父 Dialog；Select portal 的 z-index 低于 Dialog backdrop。
- 影响：可能提前关闭表单或遮住选项；尚未逐一确认生产页面的消费者。
- 规避：本地候选让 Dialog 尊重子弹层已处理的 Esc，并将 Select portal 放在 Dialog 层之上。
- 决定：本地候选已通过 5 个组合契约及 Chromium 交互探针；仍待 PR 和远端 CI 验收。
- 依据：[GUI 测试交接](../design/gui-testing-architecture-handoff-2026-11-08.md)；本地验证记录见 [实施计划](../design/gui-ci-followup-2026-11-09.md)。
- 负责人：GUI/primitives 维护者。

## KP-043 GUI Select/Field 可访问属性透传

- 现象：Select 的可访问名称和 Field 注入的错误元数据未到达实际 trigger，Field 覆盖调用方已有描述。
- 影响：辅助技术可能缺少名称、错误状态和描述；消费者范围待逐项核实。
- 规避：本地候选转发 Select 属性并合并 Field 描述 ID。
- 决定：本地候选 13 个定向契约通过，待完整回归与 PR 验收。
- 依据：[GUI 测试交接](../design/gui-testing-architecture-handoff-2026-11-08.md)；[实施计划](../design/gui-ci-followup-2026-11-09.md)。
- 负责人：GUI/primitives 维护者。

## KP-044 GUI production-copy 在缺产物时假绿

- 现象：既有测试读不到 dist/assets 就直接返回；GUI CI 在 build 前运行测试，build 后没有在 gui-unit 中执行产物校验。
- 影响：绿的测试不能证明新产物完整或不含开发内容。
- 规避：本地候选强制检查目录、入口、JS、资源引用和禁用内容；`npm test` 的 `pretest` 先构建产物供测试读取，GUI job 的现有 build 步骤随后重复执行产物校验。
- 决定：13 个产物契约与真实构建的本地校验通过；远端 CI 尚未运行。文字扫描不能独立证明完整模块依赖图隔离。
- 依据：[GUI 测试交接](../design/gui-testing-architecture-handoff-2026-11-08.md)；[实施计划](../design/gui-ci-followup-2026-11-09.md)。
- 负责人：GUI 测试/CI 维护者。

## KP-045 GUI org.join 的固定请求预算先于真实网络加入完成

- 现象：2026-11-10 在两个隔离 daemon、loopback DERP 和两个正式 GUI 的验收中，四轮组织加入有两轮超过浏览器默认 15 秒预算；一轮实测约 20 秒，浏览器中止请求并显示“结果未知”，daemon 随后仍完成加入。另两轮在预算内成功，其中一轮约 14.5 秒。`gui/src/features/org/impl/api/org-request-api.ts` 的 `join` 未覆盖 `gui/src/client/transport.ts` 的默认预算。
- 影响：慢速真实网络加入可能已成功，但用户看到结果未知；不能把一次 14.5 秒成功写成任意网络时延均通过，也不能把超时直接当作未加入。
- 规避：保持写入不自动重发；先通过 `org.list` 和成员视图核对本次加入的权威状态，再决定后续操作。本容器保留实际超时记录，不删除场景、不伪造成功。
- 决定：待修，由独立 daemon request/GUI 请求预算工作包设计足够的加入预算或异步回执；不借本容器顺带修改通用 transport。
- 依据：[设置页收敛真实验收记录](../design/gui-settings-user-org-convergence-2026-11-09.md#实现与真实验收记录2026-11-10)。
- 负责人：GUI 组织页与 daemon request 维护者。

## KP-046 Local HTTP fixture failures reproduced on the B6 base

- Symptom: On 2026-11-11, `test_fake_lark_events.py::test_the_selfcheck_can_fail` and both `test_link_reuse.py::test_existing_link_examples_cross_real_http` cases failed in the isolated B6 checkout. HTTP response bodies were empty, JSON decoding failed, and the stand-in selfcheck did not emit its expected terminal marker.
- Impact: These three tests cannot provide passing fixture evidence in this environment. This is not evidence of a production Lark or daemon failure.
- Workaround: Retain failures and compare with the base using the same interpreter/dependencies. Do not skip tests or inspect production credentials to obtain a green result.
- Decision: Defer diagnosis outside the B6 ownership migration. Candidate: 3 failed; verified base `87ad6b35`: the same 3 failed. Root cause remains UNKNOWN; do not attribute other full-suite failures to this control.
- Evidence: [B6 migration report](../evidence/b6-engine-holders-2026-11-11/report.md#full-suite-limitations-and-baseline-attribution).
- Owner: test-infrastructure maintainers; follow-up coordinated by agent-emergent-org-structure-refactor.

## KP-047 agent teleport 首版不做对端认证

- 现象：按 2026-11-12 的决定，teleport 的 `agent-teleport` Zenoh 入口不验证对端身份。Zenoh ACL 未实现（`zenohAcl: not-implemented-0.5.5.1`），key 与 payload 中的 node/host/owner 均为自报值。登记时 teleport 尚未实现，本条在入口合入 `dev` 后生效。
- 影响：mesh 内任一 peer 可以在目标 daemon 落地并启动其提供的 agent home；源 daemon 无法证明 prepare 返回的凭据加密公钥来自真实目标，冒充目标的 peer 可以收到 home 与随迁凭据。
- 规避：只在成员可信的 mesh 中使用 teleport。prepare 的 owner 比对只防误操作，不能当作访问控制。
- 决定：不修（首版）。Allen 2026-11-12 决定先实现传输，后续通过 Zenoh ACL 拦截启动信息；keyspace 已按目标节点与操作类型分段，便于 ACL 过滤。
- 依据：[teleport handoff §6](../design/agent-teleport-handoff.md)。tailcat device 密钥不能签名，Zenoh 链路不携带可信对端身份。
- 负责人：Allen（决定）；teleport 与 zenoh-acl 维护者（后续）。

## KP-048 pytest worker 在 OrgFS 定时同步的 Zenoh 查询处原生崩溃

- 现象：2026-11-12 至 11-13，用 CI 的分片命令（Python 3.12，`-n 2 --dist=loadfile --max-worker-restart=0`）跑同一份 417 个测试文件，`dev` `34273d9b` 4 轮中 1 轮、teleport 分支 8 轮中 5 轮出现 xdist worker 崩溃（`node down: Not properly terminated`）。留下栈的 3 次相同：`orgfs/network/supplier_health.py::_run_scheduled_sync` → `orgfs/network/sync.py::_sync_query` → `transport/zenoh/transport.py::query`，另一原生线程 `Segmentation fault`（无 Python 帧）。其余几次进程直接消失、没有栈。崩溃时正在运行的用例集中在 `tests/daemon/orgfs/mesh/test_orgfs_device_row_owner.py`、`tests/daemon/orgfs/purge/test_orgfs_purge_twonode.py` 与紧随其后的 `tests/daemon/provider_keys/service/flows/test_ipc_e2e.py`。`dev` 的 CI 运行 55811 也在同一 OrgFS 文件上出现过 worker 崩溃。
- 影响：所在分片整片判红，被打断的用例记为失败；与被打断用例本身无关。CI 不允许空提交重跑，只能等下一次真实提交。
- 规避：无可靠规避。只跑 `tests/daemon/orgfs` 或只重放崩溃前最后 12 个文件都不复现；需要完整分片才触发。不要为了变绿而跳过或隔离这些用例。
- 决定：不在 teleport 工作内修。根因 UNKNOWN：未确认是 Zenoh 原生层在 session 关闭与查询并发时的缺陷，还是 OrgFS 定时同步在测试 teardown 后仍持有 session。teleport 分支上的发生率看起来更高，但样本小且两边文件集合差 11 个文件，不能据此归因；“去掉 teleport target 关闭时提前注销 query handler”的实验未消除崩溃（3 轮中 1 轮），已撤回。
- 依据：#2721 的 PR 正文与 CI 记录；本地对照运行的命令与轮次见该 PR。
- 负责人：OrgFS 网络同步与 transport 维护者；teleport 协调者负责在相关 PR 中引用本条。

## KP-058 provider-key HTTP 方法尚无 Contract Mock 示例

2026-11-14 rebase：原集成分支编号为 KP-047；dev 已将该编号用于 teleport，故本条改为 KP-058，问题语义与状态不变。

- 现象：2026-11-12 的 `intg/gui-agent-journey` 在 dev `c9b2f33d` 上恢复 provider-key HTTP 暴露后，共有 109 个 HTTP 方法；其中 `user.provider.login/list/logout/rotate` 与 `agent.provider.allow/revoke/status` 没有 `x-hyprial-examples`，Contract Mock 明确返回 `METHOD_NOT_FOUND`。
- 影响：这 7 个方法不能通过现有 Contract Mock 验收；它们已有 MethodSpec 与 HTTP binding，不能将此现象写成 daemon 方法缺失，也不能把纯前端合成 Key 成功当作后端 PASS。
- 规避：前端旅程使用明确选择的隔离 preview；现有后端语义通过隔离焦点测试验证。Mock 契约测试精确断言 7 个未覆盖方法及错误，继续校验其余 102 个示例，不扩大忽略范围。
- 决定：待补示例与验收。用户要求先梳理前端旅程，再统一补后端/契约，本轮只恢复原 HTTP 暴露，不新增示例或业务方法。
- 依据：[集成记录](../design/gui-v0.1-method-plan-2026-11-13.md)、[用户旅程台账](../design/gui-v0.1-method-plan-2026-11-13.md)、`tests/daemon/contract_mock/test_mock_contract.py`。
- 负责人：GUI Agent 与 daemon request 维护者，后续按用户确认的旅程范围补齐。

## KP-059 GUI host 尚未开放完整初始化切换

2026-11-14 同步 dev：本分支历史编号 KP-048 与 dev 的 OrgFS/Zenoh 崩溃问题重号，现改为 KP-059；历史证据中的原编号保留。

- 现象：最新 dev `c917a2ba2d9a6a013ba21eccd079ce54142dc563` 的 `GuiRuntimeHost` 在 bootstrap 允许 `onboarding.device.ensure`，但 `onboarding.initialize` 仍 disabled；serving 禁用这两个 bootstrap 操作。ensure 成功只说明 deviceReady，不能说明 initialized。
- 影响：从空 home 通过 GUI 登录后，不能把“一键初始化并进入正常服务”视为已交付；单纯补按钮或放开 MethodSpec 不能代替启动 owner 的 quiesce/锁交接/generation migration/daemon ready/完成回执。
- 规避：前端预览明确使用隔离 mock；正式 GUI 检查整段方法可用性后才发写请求，只有 authoritative initialized + serving 才显示完成。不要自动改写初始化标记。
- 决定更新（2026-11-12）：Owner 已明确授权真实接线。当前集成工作树补充 foreground lifecycle owner、HTTP 排空、连续锁移交、ready 回执和 GUI generation 重开；隔离真实 HTTP/daemon/Tailcat 循环已通过，身份提交为受控端口。外部账号注册/登录与人工全旅程仍 UNKNOWN，未将此项关闭。
- 现场补充（2026-11-13，`33ec5390e`）：4181 隔离演示环境使用 macOS 长临时目录，IPC socket 路径 110 字节；人工初始化在 ipc-server 阶段报 `AF_UNIX path too long`，backend 退出后 GUI supervisor 关闭页面服务。已将同一隔离环境迁到短路径（socket 48 字节），保留账号、设备与数据，真实 daemon ping 和 GUI health 恢复。后续普通 daemon 重启仍暴露恢复缺口：`onboarding.status` 返回 authenticated=true/deviceReady=true/initialized=false/phase=serving/nextActions=[initialize]，但 serving 不开放 initialize；旧 initialize 回执为 unknown。不能把重启/端口可访问视为初始化完成，不能改写回执。当前以用户显式退出本机登录→重新登录→初始化重新进入引导；本次人工循环待验证。后续需定义跨进程失败恢复和页面提示，不能直接放开 serving initialize 或自动重放写请求。证据见 [4181 恢复记录](https://code.hyprial.com/HyprialOS/hyprial/src/commit/51f7efd1898251e5ac04ca331a9514c8cca2e51d/docs/notes/gui-demo-4181-recovery-2026-11-13/README.md)。
- 依据：`src/hyprial/daemon/impl/application/onboarding/gui_host.py`、`service.py`、`runtime.py`；[统一旅程对账](../design/gui-v0.1-method-plan-2026-11-13.md)。最新 dev 39 个 host/gateway 测试 PASS 只证明已声明的边界。
- 指针（2026-11-15）：跨进程未知回执的自动恢复在 [daemon 先行设计](../design/daemon-first-init-login-decoupling-2026-11-15.md) 的 Z-3 中安排（serving 启动完成当前 owner 的非终态 initialize 回执，待 owner 决定 OD-Z4）；GUI 子进程内嵌 daemon 的问题见 KP-064。本条状态不变。
- Z-3 实现（2026-11-16，owner 决定：serving 启动自动补完，不改契约）：`lifecycle/recovery.py` 在 serving 就绪后于独立线程中完成当前 owner（identityMode 为 casdoor）的 promoting/unknown initialize 回执；owner 已提交但设备未登记或 initialize 从未开始时，daemon 内部完成设备登记与 initialize。`device.ensure` 与 `initialize` 仍只对 bootstrap 外部调用者开放，`request_catalog`、OpenAPI 未改动。任何异常只记日志、不改状态、不阻塞 serving 与 ready；失败后 `onboarding.status` 仍为 `initialized:false`/`deviceReady:false`，下次 serving 启动重试（同进程内不重试）。另一 owner 的回执与设备记录不被触碰；`accepted` 回执被跳过（状态机不允许 accepted→completed，日志重整已将死写者的 accepted 转为 unknown）。
- Z-3 实测（2026-11-16，native 引擎、隔离 home、`tests/integration/daemon_recovery`，受控身份与受控 provisioner，三次连续运行均 4 passed）：promoting 被杀→重启 initialized、回执 completed：PASS；authenticated 之后 initialize 之前被杀→重启自动补完（device ready、initialize completed），外部调用 `onboarding.device.ensure`/`onboarding.initialize` 仍返回 `CALLER_NOT_AUTHORIZED`：PASS；provisioner 失败→状态如实、下次启动重试成功：PASS；kill 后 owner 被改写→回执未被触碰：PASS。移除恢复启动后前三行红。真实 issuer 与真实 Tailcat sidecar 未接触：BLOCKED（需公网 issuer 与已构建 sidecar；该路径不得在测试中伪造）；损坏的操作日志无法在真实进程中单独驱动（serving 装配本身先打不开日志而拒绝启动），仅有单元覆盖。
- Z-3 复核后实测（2026-11-16，Python 3.12，`tests/integration/daemon_recovery` 三次连续均 6 passed；与 `daemon_lifecycle` 在不回收子进程的父进程下同跑 30 passed）：新增“账号切换后 kill→重启为新 owner 自动补完，上一 owner 回执逐字段不变”与“failed initialize 回执→重启后重新 initialize、旧回执不变”两行 PASS，owner 被改写一行改为等待恢复的 `recovery-finished` 记录而非固定 sleep，且现在为新 owner 自动补完。移除修复后这三行红。真实身份 issuer 与真实 Tailcat 仍为 BLOCKED。
- 残余（见 KP-091）：恢复在 provisioning 期间若发生 logout，设备记录可能被写入已登出的 owner（低严重度，见该条）。
- Z-3 复核修正（2026-11-16，三项验证发现）：(F1) `auth.logout` 从不删除 `device.json`，账号切换后该记录属于上一 owner；恢复不再跳过，而是与 bootstrap `device.ensure` 一致，经同一 `TailcatDeviceProvisioner` 为当前 owner 重新登记（沿用旧 device id、重写记录），随后 initialize；其他 owner 的回执仍不被触碰，provisioning 后重读 owner、变化即中止。上文“另一 owner 的……设备记录不被触碰”对设备记录一项由此作废。(F2) 仅有 failed/cancelled 的 initialize 回执（例如 `run_daemon` 在应用工厂抛错时写 failed）现在会以新 operation key 重新 initialize，旧回执不改动。(F3) 恢复只在当前 owner 于恢复 surface 上的 failed/unknown 设备回执少于 8 条时才写失败回执；超过后仍每次启动尝试 provisioning、失败只记日志、成功才补写 begin+ready，日志不再随重启循环无界增长，上限与记录不删改。
- 负责人：daemon onboarding/GUI 启动装配维护者；GUI 集成维护者负责门槛与状态投影。
- 状态变化（2026-11-19）：已修复，代码在 dev。本条的现象（GUI host 的 bootstrap 允许 `onboarding.device.ensure` 而 `onboarding.initialize` 仍 disabled）属于已删除的 GUI 托管 bootstrap：Z-6b（#2933）删除了它，`hyprial gui` 的 GUI 子进程不再 promotion、不取锁，登录与 initialize 在 daemon 进程内完成，同一进程从 bootstrap 进入 serving（Z-2 #2858）；跨进程的 promoting/unknown initialize 回执由 Z-3（#2861）在 serving 启动时补完。未验证：真实身份 issuer 与真实 Tailcat sidecar（BLOCKED，需公网 issuer 与已构建 sidecar），macOS 与原生壳（UNKNOWN），浏览器全旅程（UNKNOWN）。残余见 KP-091 与 KP-094。
- 本次恢复结果（2026-11-13T02:33:37Z）：短路径环境经用户操作后，真实 HTTP status 返回 authenticated=true/deviceReady=true/initialized=true/phase=serving/nextActions=[]，当前 initialize 回执 completed；auth.logout 为 available。4181 当前初始化完成，恢复状态 PASS；这不关闭跨进程 unknown 回执的自动恢复缺口，也不代表新账号注册、退出后重新登录等全部场景本轮重测完成。脱敏响应见 [历史证据](https://code.hyprial.com/HyprialOS/hyprial/src/commit/51f7efd1898251e5ac04ca331a9514c8cca2e51d/docs/notes/gui-demo-4181-recovery-2026-11-13/ready-observation.json)。

## KP-049 GUI 自动升级只有状态查询，暂无配置入口

- 现象：`autoupdate.status` 已接入设备页；每日计划是本机时间 03:17/15:17 的调度时刻，是否执行仍由 `autoUpgrade` 控制。GUI 未提供对应设置入口。
- 影响：用户可以查看开关和计划，不能在 GUI 改自动升级开关；计划存在不代表自动升级已开启。
- 规避：保持只读，不从状态读取触发升级。现有 CLI `hyprial config set autoUpgrade true/false` 是配置能力，本轮没有执行。
- 决定：按用户 2026-11-12 要求记录，后续确认升级旅程再做 GUI 设置，不顺带加入本轮登录/初始化开发。
- 依据：`src/hyprial/daemon/impl/autoupdate/__init__.py`、`gui/src/features/settings/impl/UpgradeStatusSection.tsx`、统一旅程对账。
- 负责人：GUI 设备页及 daemon 自动升级维护者。


## KP-050 macOS 上 Playwright 缓存脚本的 Linux 测试失败

- 现象：2026-11-12 的 GUI `npm test` 中 `playwright-cache.test.mjs` 有 12 项失败，全部停在 `dpkg --print-architecture failed`；当前环境为 macOS。
- 影响：本机不能把 GUI 全套记为 PASS；不影响使用已安装 Chrome 运行独立浏览器验收。
- 规避：保留失败结果、不加 skip；GUI 产品相关测试和 Chrome 验收分别报告。
- 决定：本轮登录/设备任务不修改 Linux CI provisioning。以 HEAD `3a223760a0a585ca0b0a46a89b34ecfdb4eca3da` 的原始测试、脚本、lockfile、workflow 在同一 macOS/dependency 下复跑，同样 12 FAIL / 4 PASS / 1 SKIP。未把首次不完整基线副本的 3 个缺文件错误计入该归因。
- 依据：[登录前置证据](../notes/gui-login-access-2026-11-12/README.md)。
- 负责人：GUI CI/测试基础设施维护者。

## KP-051 adapter 旧 CLI 测试直接构造空 Typer 入口

- 现象：2026-11-13 Agent 配置验证中，`tests/daemon/application/adapters/test_daemon_adapter_pin.py`
  的四个 `test_cli_*` 报 `RuntimeError: Could not get a command for this Typer instance`。
- 影响：相关 Python 焦点集合 504 PASS / 4 FAIL，不能报告集合全绿；同轮独立 daemon+HTTP 的
  adapter.list/pin/unpin 往返通过。没有证据表明 HTTP 绑定受此测试入口问题影响。
- 决定：本轮不改后端或历史 CLI 测试；后续迁移测试到当前 CLI 装配入口核对。本轮后端及该测试文件均未修改。
- 规避：单独报告真实 HTTP 结果与四项失败，不用删测试或 skip 掩盖；日志 `/tmp/gui-agent-config-backend-005.log`，
  汇总见 [历史证据](https://code.hyprial.com/HyprialOS/hyprial/src/commit/51f7efd1898251e5ac04ca331a9514c8cca2e51d/docs/notes/gui-agent-config-2026-11-13/verification-001/README.md)。
- 负责人：GUI lifecycle 开发线（codex-gui-orgs-review）记录，CLI 测试维护时处理。

## KP-052 GUI 创建页的 provider 范围不等于完整启动组合

- 原始现象：`ContactsPage.tsx::startCreatedAgent` 对 Claude/Codex 提交空 model，Hyprial provider 却要求 vendor/model；Codex 默认 file 凭据模式要求 agent-owned auth.json，共享 Key 环境授权不会安装该文件；Pi 文件导入只验文件集合、不转换 Claude/Codex 格式。
- 影响：创建记录成功或兼容 provider 可选，不能保证凭据可用、启动就绪和真实回复。共享 Key 正式装配另受 KP-030 阻断。隔离启动探针还观察到 Claude 泛化 HARNESS_START_FAILED、Codex 不兼容 provider 请求 15 秒超时；后者根因及最终启动结果 UNKNOWN，不归因于厂商认证。
- 规避：当前真实 HTTP 已验证创建记录和各自原生文件安装；完整运行尚不作为可用结论。预览可继续整理，禁止合成成功回执掩盖正式失败；启动结果未知时不重发写操作。
- 进展：已取消创建时自动启动；配置后显式保存并启动；Hyprial/Codex DeepSeek 参数预检、自定义入口禁用、Codex 共享 OpenAI Key 排除、Key store 不可用时阻止写入均已加入。2026-11-14 原生 Auth 清单已通过 `agent.provider.auth.list` 接线并独立审核。Pi 新增显式 cwd/approve 后，真实 HTTP 与 Chrome 启动通过，身份/模型服务受控；基础对话因隔离用户无回复路由仍未通过，厂商认证未知。不合成 Auth 版本关联启动状态。
- 决定：保持部分修复；正式 Key 装配、真实模型响应和失败恢复仍需独立验收，不走环境变量或 CLI 旁路。前端及 mock 通过不关闭后端缺口。

## KP-053 GUI legacy live 测试偶发等待查询按钮超时

- 现象：2026-11-13 全量 GUI 首轮中，`tests/live/live-pages.test.tsx` 的 unknown-result lookup 用例在默认等待时间内未找到“查询原消息结果”；同树单独复跑及随后全量复跑均通过。
- 影响：首轮为 2092 PASS / 13 FAIL / 2 SKIP；随后全量 2093 PASS / 12 FAIL / 2 SKIP，剩余 12 项均为 KP-050。首次失败保留，不记作 PASS。
- 规避/决定：暂不改 legacy controller 或放宽断言；再次出现时采集发送按钮可用性、请求记录和控制器时间线。根因 UNKNOWN，不能据此认定真实聊天失败或已修复。
- 依据：[历史验证结果](https://code.hyprial.com/HyprialOS/hyprial/src/commit/51f7efd1898251e5ac04ca331a9514c8cca2e51d/docs/notes/gui-agent-detail-polish-2026-11-13/RESULT.json)。
- 负责人：GUI lifecycle 开发线。
- 依据：[启动组合核查](https://code.hyprial.com/HyprialOS/hyprial/src/commit/51f7efd1898251e5ac04ca331a9514c8cca2e51d/docs/notes/gui-harness-credential-matrix-2026-11-13/README.md)，基线 7af55eb9；HTTP 探针失败、成功子集和本地 SDK 格式测试分别保留。
- 负责人：GUI lifecycle 开发线（codex-gui-orgs-review）；provider-key 装配与信任边界由后端线共同核对。

## KP-054 GUI 历史浏览器总门与当前起点不一致

- 现象：2026-11-13 清理前 `51f7efd18` 与清理后运行 `dev-product-parity-browser.mjs` 的 760px 产品矩阵，均等待默认 Squire 输入、创建 Agent、组织子导航和旧“连接”菜单超时；当前默认起点已收敛为登录/初始化空状态，菜单为“Agent 设置”。总门仍按旧场景断言。
- 影响：该历史总门不能作为当前旅程通过的证据；本轮总门 1 PASS / 11 FAIL，后续部分用例另有服务响应失败，未把其根因归入全部选择器问题。单矩阵前后对照确认上述四处超时既存。
- 规避：按 logged-in-init 显式入口独立验证共享组件；本轮 Chrome 的 development/dist-parity × 375/1440 四次凭据清单、创建、Auth 移除与空状态检查通过，无业务网络请求。
- 决定：用户明确暂不处理四步引导，本轮保留引导实现及旧验收脚本，不删失败断言、不改变产品流程；后续同步场景和验收契约再关闭。真实后端未由这组浏览器结果验收。
- 依据：清理前后同命令对照及本 PR 验证结果；原始临时日志不入库。
- 负责人：GUI lifecycle 开发线。

2026-11-14 验证更新：在 `f280850` 产品代码上复跑 760px product matrix，复现上述四处超时。
本轮只对齐测试：业务操作明确使用已登录、有 Agent 的归档输入；菜单使用“Agent 设置”；
归档选择验证新窗口打开且不替换当前旅程；开发工具检查先等待异步挂载完成。
未修改四步引导实现，也未删除引导断言。修改后的真实 Chrome 双入口浏览器门 12/12 PASS，
含 375/760/1440 三个宽度的产品操作与四步引导；远端 GUI job 是否通过仍独立核对。
日志 `/tmp/gui-ci-current-parity-browser.log`（修改前）、`/tmp/gui-ci-parity-sync-green.log`（修改后）。

## KP-055 历史 linked-journey 引导等待偶发超时

- 现象：2026-11-13 全量 GUI 2101 PASS / 13 FAIL / 2 SKIP 中，除 KP-050 的 12 项外，J6 在首次设置辅助流程等待“初始化当前设备”超时；8 文件焦点中 J5 改为等待“下一步”超时。
- 影响：本轮全量不记 PASS；未证明真实账号或设备流程失败，也未证明不存在时序问题。
- 规避：清理前后分别单 worker 运行 linked-journey + dev-tools-floating，均 31/31 PASS；保留初次失败记录和范围，不用重跑结果抹去失败。
- 决定：根因 UNKNOWN；与四步引导一起后续处理，本轮不放宽超时或改引导逻辑。
- 依据：本 PR 的完整与焦点测试汇总；临时日志不入库。
- 负责人：GUI lifecycle 开发线。

## KP-056 macOS 原生壳的共享登录入口未注入系统浏览器能力

- 现象：用户报告安装包点击登录及“打开登录页”均无浏览器。核对含 #2742 的 dev `46a29ba48`：
  共享页面使用 `window.open`/`target=_blank`，原生主窗口 `on_new_window` 返回 Deny，
  `createConnectedGuiCapabilities` 未提供已有的 `openAuthorization` 能力。
- 影响：HTTP 授权操作即使已受理，原生用户也可能无法打开授权网站；不是 Casdoor 注册结果的证据。
  本次为源码路径核对和用户现场描述，尚未独立重现安装包。
- 规避：本轮先用隔离 Web launcher 完成真实账号验收；不放开任意 WebView 新窗口或绕过可信 URL 边界。
- 决定：本轮不顺带修改打包实现。打包线接入受限的系统浏览器打开能力，GUI 线复用共享页面已有端口；
  自动打开和手动重试均复测。仅安装 opener plugin 不等于页面已调用或获得权限。
- 依据：`clients/native/src-tauri/src/windows.rs`、`power_pack/mod.rs`、主窗口 capability，
  `gui/src/app/capabilities.tsx`、`features/account/impl/useAccountSettings.ts`、
  `features/onboarding/impl/AccountProgressPanel.tsx`。
- 负责人：原生打包线；GUI lifecycle 线配合能力注入和共享页面回归。
- PR 6c（2026-11-16）：上文“依据”所列文件属于已删除的旧壳。新壳（#2840）没有 IPC 与 capability：
  `on_new_window` 把其他源的 `http(s)` 地址交给系统浏览器，其余（含 `about:blank`）拒绝，
  共享页面因此走链接回退。6c 的自动冒烟不能点击，登录点击是否打开系统浏览器**未实测，仍为 UNKNOWN**；
  需要有屏幕的会话里人工点击一次。

## KP-057 dev 46a29ba48 的既有验证失败阻断 GUI 集成 CI 判定

- 现象：2026-11-14，在干净 worktree、Python 3.12、短临时路径复跑 GUI 分支上一轮失败所在文件，
  dev `46a29ba48a23054408106816cc6e4d967e7e2391` 为 360 PASS / 38 FAIL / 5 SKIP。
  失败覆盖 CLI inventory/post-install、原生转发/清单、凭据导入边界、Claude 上下文窗口、
  协议错误码常量、发布快照检查及 macOS 上的 Linux uv 下载测试。
  这是具体失败集合在 dev 的复现，不代表每项根因已确定，也不能推广到候选的全部失败。
- 影响：全量测试不能记 PASS。daemon request 架构门在 dev 与 GUI 候选 `f280850` 都报告相同的
  7 项原生 setup/checkpoint/process 发现；相关源文件在候选中无差异。
  独立的 daemon-lifecycle 契约 runner 也在两树同样因缺少 `actor_host` 失败。
- 规避：固定 SHA、清洁环境、独立工作树对照精确 nodeid；本轮新增回归先修，再分别报告继承失败、
  环境限制与未归属项。门中的 base-growth 比较例外清单，不能据其名称认定当前分支新增。
- 决定：不在 GUI 清理中改写原生/CLI 断言或扩大架构 baseline；所属模块需核对后修复。
  远端 checks / cli-help-inventory 失败日志尚未取得，本地复现不能直接作为远端根因。
- 依据：本地 `/tmp/gui-ci-py312-base-attribution.log`、`/tmp/gui-ci-base-daemon-gate.log`、
  `/tmp/gui-ci-final-daemon.log`、`/tmp/gui-contract-base-lifecycle.log`；进度与后续结果维护在
  `docs/design/gui-v0.1-method-plan-2026-11-13.md`，原始证据不入库。
- 负责人：GUI lifecycle 线维护归属和候选回归证据；原生、CLI、CI 与发布各模块修复尚待协调。

2026-11-14 归属补充：候选 `f280850` 全量 27212 PASS / 101 FAIL / 4 ERROR；该轮工作树缺少
本地 venv/GUI 依赖且使用路径别名，存在验证环境问题。补齐后对失败所在文件和未收集模块复跑
742 PASS / 35 FAIL / 6 SKIP，35 个失败 nodeid 均已在上述固定 dev 失败集合中；额外 66 项与
收集错误消除。候选未因此获得全量 PASS。远端 `754e29c60` GUI push/PR 均通过，Python checks
及 CLI inventory 仍失败；具体远端日志仍未取得。原始复跑记录为 `/tmp/gui-ci-prepared-failures.log`。

## KP-060 四步引导浏览器验收等待旧按钮颜色超时

- 现象：指定系统Chrome后，`dev01-onboarding-browser.test.mjs` 在 `assertLayout` 等待主按钮颜色 `rgb(26, 22, 32)` 超时。
- 影响：7d204415b全量GUI为2142 PASS/13 FAIL/1 SKIP，其中12项是KP-050，此项额外失败；不记GUI全绿。
- 依据：相同命令在合并前e808f9a和合并后7d204415b均失败，选择器和断言相同；两头GUI产品无差异。日志 `/tmp/gui-e808-fourstep-browser.log`、`/tmp/gui-dev-sync-fourstep-browser.log`。
- 决定：按用户明确要求暂缓四步引导；后续核对共享token与视觉基线，不删除断言或放宽超时。
- 规避：当前登录/初始化→Pi创建/启动/Chat导航另以真实隔离HTTP/浏览器验证；不以该主线PASS覆盖引导失败。
- 负责人：GUI lifecycle开发线。

## KP-061 云工作机并发全量 pytest 的中断与基线归因限制

- 现象：2026-11-14，在当前 4 核 / 16 GiB 云工作机上，daemon request 的四个独立候选全量 `pytest tests/` 诊断运行并发且均在完成前中断。固定候选 `60a37624e`（service）记录 3926 PASS / 9 FAIL / 1 ERROR / 5 SKIP；`a30e98556`（identity）3988 PASS / 4 FAIL / 5 SKIP；`d5d27cad0`（network）1931 PASS / 3 FAIL / 1 ERROR / 3 SKIP；`c1c9812ab`（routine）896 PASS / 5 FAIL / 1 ERROR / 3 SKIP。这些是中断时的部分计数，均非完整套件结论。Service/identity 中的 `tests/daemon/contract_mock/error_data/test_catalog.py` 参数化失败和 `test_mock_contract.py::test_the_real_registered_method_projection_loads_and_serves_success` 与新增声明未同步 mock 测试有关；共用测试修复已由 #2780 合入，基线与修复候选各 555 PASS，独立 service+routine 组合 562 PASS、identity 组合 560 PASS。network/routine 的历史全量在该段之前中断，不能将焦点通过写成那些全量通过。
- 固定基线对照：在未改产品的 `2a494edccdee0135b0a4dbb6fd7f7cf755b803f0` 独立 checkout，使用已安装锁定依赖，仅对四轮中其余去重的 11 个失败 nodeid 做一次 `uv run --frozen pytest -q`，得到 5 PASS / 6 FAIL / 1 teardown ERROR（88.70 秒）。同一 nodeid 且同类症状的六项如下；它们在此控制中不能归因于上述新增 MethodSpec：
  - `tests/daemon/adapters/qq/test_token.py::test_token_stop_cancels_and_joins_background_task`：后台任务未全部完成，继发 `store.sqlite`/wal/shm 三个 fd 泄漏 teardown ERROR。
  - `tests/daemon/application/supervision/test_daemon_exit_backstop.py::test_rearm_supersedes_old_timer_and_new_timer_still_fires`：8 秒子进程 TimeoutExpired。
  - `tests/daemon/channels/adapters/test_review_e1c.py::test_pi_unsupported_cache_expires_on_new_daemon_epoch`：测试明确报 `Pi protocol rig unavailable; report BLOCKED, not PASS`。
  - `tests/daemon/application/composition/application/test_daemon_application_test_cleanup.py::test_assertion_failure_still_closes_owned_daemon_application`：30 秒子进程 TimeoutExpired。
  - `tests/daemon/adapters/lark/identity/authorization/test_process.py::test_host_crash_stops_a_worker_blocked_in_sdk_io[bound]` 与 `[disabled-control]`：8 秒内未观察到 stand-in 的 hung 事件。
- 待归因：其余五项在该次基线焦点控制 PASS，但曾在候选并发全量中 FAIL：`tests/daemon/attach/test_attach_errors.py::test_attach_refuses_invalid_arguments[params5]`、`tests/daemon/adapters/lark/identity/scopes/throttle/test_throttle.py::test_throttle_claim_is_atomic_across_processes`（候选另有 teardown ERROR）、`tests/daemon/application/composition/runtime/test_daemon_runtime.py::test_supervisor_status_reports_the_process_owned_dsh_endpoint`、`tests/daemon/adapters/lark/actors/actor_domain/shutdown/test_shutdown.py::test_shutdown_reports_blocked_process_stop_until_custody_releases`、`tests/daemon/adapters/lark/identity/authorization/test_process.py::test_real_hung_sdk_is_cancelled_and_reaped`。一次基线绿既不证明这些是候选回归，也不证明它们在重负载下属于基线；归因 UNKNOWN。
- 另项环境证据：service 关联测试 `tests/integration/service_connect/test_service_connect_integration.py::test_real_ipc_socket_routes_service_request_to_manager` 在候选与同依赖的固定基线均因创建 `/private` 得到 `PermissionError: [Errno 13] Permission denied`；本 Linux 容器无该 macOS 路径。该单测是 FAIL，其他 `/tmp` 隔离 socket 焦点通过不能替代它。`contract/daemon-lifecycle` 的 `ValueError: actor_host is required` 已在 KP-057 记录并于 network 候选与无相关产品改动的对照树复现，不在本条冒充新问题。
- 影响：四个候选均不能报告全量 pytest PASS；历史 mock 测试失配已由 #2780 修复并取得焦点结果；其余五项归因仍为 UNKNOWN，需以固定候选/基线的同条件结果判定。中断、skip 或焦点通过不能抵销实际 FAIL/ERROR。
- 规避：继续用隔离 home/socket/cache 和固定 SHA；已由 #2780 修复并实测相关 mock 断言；针对未归因 nodeid 在相同资源负载下对照 base/head，不并发启动新的整套 full 以争夺 4 核 / 16 GiB。保留 `/private` 和外部 Pi rig 的环境拒绝，不以未说明的路径替换或 skip 冒充原测试通过；独立跨平台测试修复须保留原失败证据与相同隔离断言。发布前按适用门重新取得完整结果或明确 BLOCKED。
- 决定：本轮登记验证限制，根因按项 UNKNOWN；不扩大 baseline、不改已存在测试断言以获得绿。QQ/SDK/进程时序与 runner 环境由相应维护者后续排查；新增 MethodSpec/mock 测试失配已由 #2780 修复；修复后完整候选全量仍须实测。
- 依据：`/workspace/work/w1-service-full-60a376.log`、`/workspace/work/w2-full-fixed-a30e985.log`、`/workspace/work/w2-network-full-pytest.log`、`/workspace/work/w1-routine-full-c1c981.log`、`/workspace/work/acceptance-base-remaining-failures.log`、`/workspace/work/w1-service-base-private.log`、`/workspace/work/w2-network-isolated-contract.log`、`/workspace/work/w2-network-contract-baseline.log`。登记基线 `25835754bab1994d1559fd97b9537ec86b21e7ef` 中最大编号为 KP-060；上述日志是当时工作区证据，跨环境不保证保留，本条列明固定 SHA、nodeid 和实际结果供复现。
- 负责人：Allen（协调）；Codex 实施线（mock 修复与固定候选复验），各测试基础设施/领域维护者（对应基线项）。

## KP-062 fs purge plan 无效目标未输出 JSON 错误信封

- 现象：隔离执行 `uv run python -m hyprial.cli org fs purge plan space-1 not-json --json`，以及把位置参数 `not-json` 换为 `'[1]'`，均退出 1、stdout 为空，stderr 有对应 `CliError` 信息和 Python traceback，而不是单个 JSON 错误对象。`targets_json` 是位置参数，不是 `--targets`。使用独立临时 HOME、HYPRIAL_HOME、HARNESS_STATE_DIR 并设置 `HYPRIAL_NETWORK_ISOLATED=1`（不证明 OS 网络隔离）；未访问真实 daemon 或凭据。
- 影响：无效输入违反 CLI 的 `--json` stdout 错误信封约束，并向调用方显示 traceback。此探测没有验收有效 purge 行为；不能以此推断该功能整体可用或不可用。
- 规避：对无效输入没有经过验证的规避；调用方可先提供 JSON 对象数组，但是否满足全部业务约束仍由产品校验。
- 决定：待修，由 CLI 输出 / OrgFS 命令维护者单独处理；词汇登记切片不改产品行为。不得用放宽输出断言或伪造成功来关闭。
- 依据：产品源码未变的父 `837eaad9d` 与仅调整词汇台账的候选 `7484087aa`，两输入均得到 `exit 1, stdout_empty True, stderr_message True, stderr_traceback True`。原始隔离探针为当时工作区 `/workspace/work/inventory-scratch/fs-probe.py`；独立 Typer 测试入口的三项失败另归 KP-020，不将两种不同现象合并。
- 负责人：CLI 输出 / OrgFS 命令维护者（待具体指派）。
- 状态更新（2026-11-25）：已关闭。OrgFS 改为只保存当前状态的精简实现后，全网内容清除连同 `org fs purge plan`、`org fs purge status`、`org fs purge unban` 一起删除，本条描述的命令已不存在；保留的 `org fs purge run` 不接受目标列表参数。见 `docs/guides/migration-orgfs-minimal.md`。
## KP-063 GUI 产品代码仍保留 `/api/reference/*` 默认路径，但已无产品宿主提供

2026-11-14 rebase：本条在 PR 1b 原分支编号为 KP-049；#2727 合入后 dev 已将该编号用于自动升级配置入口，故改为 KP-063。GUI 启动方式收拢设计第 5 节中的“KP-049”指本条。

- 现象：`createHttpAgentProviderApi`（`gui/src/features/agent-provider/impl/api/index.ts`）、`createHttpDiagnosticApi`（`gui/src/features/settings/impl/diagnostic/api.ts`）默认指向 `/api/reference/live`；`gui/src/dev/mock/agent-provider.ts` 指向 `/api/reference/mock-<scenario>`。PR 1b 删除 reference 启动方式后，这些路径只在 `gui/tests/reference/gui-host.mjs` 的测试宿主中有应答。（`createHttpHostsApi` 所在的 `hostsApi.ts` 已随设置与账号区的无引用代码一并删除。）
- 影响：`HYPRIAL_GUI_DEV_PREVIEW=1 npm run dev` 的 Agent provider 场景请求得不到 Contract Mock 应答（删除前同样需要手工配置 `HYPRIAL_GUI_REFERENCE_CONFIG`）；正式入口经 daemon request，不经过这些默认路径。
- 规避：浏览器验收经 `gui/tests/reference/gui-host.mjs` 启动；不要把这些默认路径当作可用后端。
- 决定：已修复（2026-11-16 关闭，见本条末尾）。历史：部分已修复（PR 1d、PR 1e 第一段，2026-11-14，未发版）。余下各项由 PR 1e 后续各段删除，存量逐条登记在 `scripts/ci/gui-request-paths-ledger.json` 的 `pending`，该清单只减不增（对基线分支比较）；清空时关闭本条。
- 依据：GUI 启动方式收拢设计（2026-11-14）第 4.2、5、6 节；PR 1b 正文。
- 负责人：GUI 启动收拢各 PR 负责人。
- 已修复（PR 1d）：删除 `createHttpHostsApi`、`createHttpDiagnosticApi`、`createHttpAgentsApi`；`createHttpAgentProviderApi` 不再有默认路径（调用方必须显式传入）。`/api/gui/v1/live` 与 `/__gui/auth/*` 的客户端及宿主已由 #2727 删除。
- 已修复（PR 1e 第一段）：删除 `HttpStubOperations` 与 `/api/gui/v1/not-wired` 路径常量（占位端口先改为进程内拒绝，随后整体删除，见下一条）；删除无调用方的 `createHttpAdapterStatusApi`、`createHttpMessageReadApi` 与 `runtime/backend-endpoint.ts`；会话状态轮询并入 `app/backend/browser-session.ts`。`gui/tests/unserved-host-paths.test.mjs` 由门禁 `scripts/ci/check_gui_request_paths.mjs` 取代：`gui/src` 中传输模块之外不得出现网络原语、`HttpTransport` 构造或请求路径，`gui/tests`、`gui/scripts` 与 Vite 配置不得另起数据宿主，浏览器测试不得新增 `route.fulfill`。
- 已修复（PR 1e 验证后修订）：删除无调用方的占位端口：`adapters/stub.ts`（`UnavailableStubOperations`、`StubOperations`）、`adapters/useStubAction.ts`，以及 `App`、`FixtureApp`、`AppShell` 与各页面上从未被读取的 `stub` / `stubClient` 属性链；测试中的 `RecordingStub` 与只验证该端口的用例一并删除。
- 已修复（PR 1e 验证后修订）：门禁的扫描器配置固定为数据文件并与基线分支逐项比较（只许扩大、不许缩小，其他改动需脚本内 `REVIEWED_SCANNER_SHA256`），首次落地摘要仅在基线没有清单时可用；扫描范围扩到括号/解构/`Reflect.get` 形式的请求全局、HTTP 客户端库及 `gui/package.json`、任意位置的三个旧前缀、`gui/src` 内的 `dist`/`node_modules`、内联 html 脚本，以及测试宿主的 `node:http`/`net` 各种导入形式、服务端库、带插件的 `vite.createServer`、别名 `middlewares.use` 与括号 `fulfill`。
- 已修复（功能区合并，2026-11-15，未发版）：删除无读取方的 `GuiCapabilities.agentProvider` 与 `larkBinding`，以及只为前者供数的 `createHttpAgentProviderApi`、`createAgentProviderApi` 和开发 mock `createAgentProviderMock`（`/api/reference/mock-<scenario>` 随之消失）；`gui/tests/live/browser.mjs` 已随连接区删除。清单 `pending` 由 25 行减为 19 行。
- 已修复（PR 1e-2，2026-11-15，未发版）：Contract Mock 只剩一份 JavaScript 实现 `gui/contract-mock`（Node 进程与浏览器内两种形态）；Python mock 服务（`server.py`、`service.py`、`__main__.py`）与 `gui/tests/reference/mock-host.py` 删除，`hyprial gui` 的 contract-mock driver 改为启动 `node gui/contract-mock/serve.ts`；例子只经生成表 `gui/src/dev/contract-mock/contract.generated.json`（`client:reference:check` 逐字节检查）供给，`examples.generated.json` 删除；新门禁 `scripts/ci/check_gui_mock_source.mjs` 守住单一来源。`/api/reference/*` 的测试宿主代理不在本段范围，仍待 PR 1e-4。
- 待办（PR 1e-2 评审后，2026-11-15）：`scripts/ci/check_gui_request_paths.mjs` 的 `REVIEWED_SCANNER_SHA256` 在本 PR 内为非 null，仅用于放行新增的受信宿主根；豁免已收窄为单文件 `gui/contract-mock/serve.ts`（`sanctionedHostRoots`）。该 pin 只对本 PR 有效，下一个改动该扫描器配置或脚本的 PR 必须把它重置为 `null`（自检要求非 null 的 pin 必须等于当前扫描器摘要）。PR 1e-3 只改了该脚本的种子摘要与头部说明，没有重置该 pin，重置留给负责人决定。
- 已修复（PR 3，2026-11-16，未发版）：Node host `gui/server/daemon-request/` 删除（浏览器会话与 `/daemon/*` 只由 Python 会话 host 提供）；`sanctionedHostRoots` 随之只剩 `gui/contract-mock/serve.ts`（该键只许缩小，故无需 review pin），`REVIEWED_SCANNER_SHA256` 重置为 `null`，上一条待办完成。`gui/server` 仍留在 `hostRoots`，回来的服务目录是无豁免的违规。浏览器脚本 `first-run-browser-contract.mjs` 与 `contract-mock-gui.mjs` 经 `gui/tests/support/python-host.ts` 启动 Python host。
- 更正（PR 3）：下文“`host.test.ts` 的 `listen`……随 Node 宿主一起删除”不再成立：`host.test.ts` 现在是 Python host 的黑盒会话契约套件，其假上游 `listen` 随之保留，仍登记为 `pending`（清单只减不增，不能改登记为 `allowlist`）。
- 已修复（PR 1e-3，2026-11-15，未发版）：浏览器脚本改走 `gui/tests/support/contract-mock-gui.mjs`（`startContractMock` + `openContractMockView`，正式入口加会话宿主，脚本只拿到 origin / `launchUrl()` / `close()`）：`agents-second-browser.test.mjs` 与 `org-request-browser.mjs` 迁移；`first-run`、`safe-r1`、`j2`、`j10`、`dev01-onboarding` 本就不用 mock，改为直接导入 `gui/scripts/serve.mjs`；删除基线即失败且不在任何工作流的 `dev01-v17-visual-compare.mjs` 与五个无人打开的 reference 入口页。清单 `pending` 由 19 行减为 15 行。
- 历史（PR 1e-3 时点的余项，已由下一条取代）：测试宿主自身的 `/api/reference/*` 代理（`gui/tests/reference/reference-http-plugin.ts`、`gui-host.mjs`、`mock-host.mjs`）、两个测试入口（`org-entry`、`org-agent-entry`），`gui/tests/daemon-request-host/host.test.ts` 的 `listen`（返回预置 daemon 应答的假上游，随 Node 宿主一起删除，故登记为 pending 而非 allowlist），以及 `org-lifecycle-browser.mjs`、`org-rework-browser.mjs` 中的 `route.fulfill` 伪造应答。这两个脚本在 `npm run verify` 链中且基线即失败（reference 入口停在“账号状态未确认”，从不发出 org.list），迁移需要 daemon 先提供对应例子：加入成功后 org.list 变为新组织（需要状态）、步骤不合法的 join 结果、两个组织与按组织的 org.network、org.network 首次失败后恢复、org.repair 的人工/部分发布结果、link.add/link.drop 后 link.list 变化、被目录拒绝的 org.create；这些都不是现有已声明用例能产生的状态。
- 已修复（PR 1e-4，2026-11-16，未发版）：删除测试宿主自身的 `/api/reference/*` 代理与全部私有测试数据通道：`gui/tests/reference/reference-http-plugin.ts`、`proxy.test.ts`、`gui-host.mjs`、`mock-host.mjs`，以及 `gui/scripts/serve.mjs` 的 `startGuiServer({ plugins })` 选项与对应的头部条目；该启动器因此不再有带插件的 `vite.createServer`，`serve.mjs` 的 allowlist 行随之删除。入口页 `org-entry`、`org-agent-entry`（`.html` 与 `.tsx`）与两个浏览器脚本 `org-lifecycle-browser.mjs`、`org-rework-browser.mjs`（`route.fulfill` 伪造应答，基线即失败）及其 `package.json` 脚本与 `verify` 链条目一并删除；它们覆盖的八类场景保留为 Stage F 的验收清单，见 [1e-4-stage-f-acceptance.md](../design/gui-single-launch-plans-2026-11-15/1e-4-stage-f-acceptance.md)。`first-run-product-entry` 仍由 j10 使用，它只用 fixture、不经任何 HTTP，予以保留。`HYPRIAL_GUI_REFERENCE_CONFIG` 在 `serve.mjs`、`serve.test.mjs` 与 `gui-dev-mode.md` 中的记账一并删除（Python 会话宿主 `shell/impl/gui/backend/` 对该变量的拒绝仍保留，那是产品代码，本 PR 不动）。清单 `pending` 由 15 行减为 1 行。
- 历史（PR 1e-4 合入时的余项，已由下一条关闭）：`scripts/ci/gui-request-paths-ledger.json` 的 `pending` 只剩 `gui/tests/daemon-request-host/host.test.ts` 的 `host-http-server createServer@listen`。它是 Python host 黑盒契约套件的假上游（返回预置 daemon 应答，供超大/畸形/断连等用例使用），不是 `/api/reference/*` 通道，也无法用 Contract Mock 替代这些故障注入。门禁规则下它既不能改登记为 `allowlist`（两个清单对基线只减不增），也不能靠扩大 `sanctionedHostRoots` 取得豁免（那需要改扫描器配置并设置 `REVIEWED_SCANNER_SHA256`，是 owner 的决定，本 PR 未做）。待 owner 在“放宽门禁规则 / 把假上游迁到受信宿主 / 保持 pending”之间决定后清空并关闭本条。
- 已关闭（2026-11-16，owner 决定，未发版）：`gui/tests/daemon-request-host/host.test.ts` 加入门禁扫描器的 `sanctionedHostRoots`，其 `pending` 行移除，清单 `pending` 为空。理由：该文件是 Python session host 的黑盒一致性套件，唯一的本机监听是用来产生超大、畸形与中途断开应答的假上游，不提供页面，浏览器不会访问它。`REVIEWED_SCANNER_SHA256` 按脚本自身规则固定为本次配置的摘要，只对这一次改动有效；下一次改动扫描器配置或脚本时必须重置为 `null`。产品 Python 中两处 fail-closed 拒绝清单仍保留 `HYPRIAL_GUI_REFERENCE_CONFIG` 字面量，用于拒绝已删除的启动方式，不属于本条。

## KP-064 已初始化的 home 在没有 daemon 运行时 `hyprial gui` 拒绝启动，经 GUI 初始化的 daemon 随 GUI 子进程结束

- 现象：从无 owner 的 home 经 GUI 登录并初始化后，daemon 就是 `request_http` 子进程本身（`daemon.json` 的 pid 等于子进程 pid，没有写任何服务单元）；子进程退出后 `daemon.sock` 与 `daemon.json` 消失。之后在同一个 home 上运行 `hyprial gui`，准入返回 `GUI_DAEMON_UNAVAILABLE`（“the selected daemon is not reachable and ready”），exit 1，因为 `hyprial gui` 不启动 daemon。隔离 home 上用真实 `hyprial gui --no-open --json` 实测复现。
- 影响：只经 GUI 初始化、没有终端的用户，第一次会话之后的每一次启动都被拒绝；关闭 GUI 窗口会停止消息投递，因为 daemon 随窗口结束。终端路径不受影响：`init`、`login`、`daemon start` 用同一个启动原语把 daemon 作为 detached 进程（或经已安装的服务单元）启动，实测能比启动者与 GUI 子进程活得久。
- 规避：先在终端运行 `hyprial daemon start`（home 已有 owner）再运行 `hyprial gui`；不要依赖经 GUI 初始化时的嵌入式 daemon 持续运行。
- 决定：修复候选在 #2845。owner 2026-11-15 决定 daemon 成为唯一常驻后端、`init` 与 `login` 解耦、关闭窗口不停 daemon；DL-1 在 `hyprial gui` 准入前对已初始化的 home 复用现有启动原语确保 daemon，再按 Z-1…Z-9 实施；进程内 promotion 之后的 hand-over（DL-2）不做。
- 验证（2026-11-15）：隔离 GUI 准入测试 7 项通过，现有 CLI GUI 启动测试 2 项通过，结构限制检查通过；真实 daemon 子进程回归与 CI/合入验收由 #2845 继续记录。
- 接手后（2026-11-15，GUI 线）：钩子移入启动器，driver 解析与启动器一致（安装态未设置即 `daemon`；`contract-mock`、空值与无效值不启动），所有廉价拒绝之后才启动；启动原语的就绪不含 restore，准入对恢复中的 daemon 有界等待；真实 `hyprial gui --no-open --json` 在 driver 未设置与 `daemon` 两种形态下启动 daemon，第二次复用同一 pid/epoch，GUI 退出后 daemon 仍在。结果见 #2845。
- Z-6a（2026-11-17）：`hyprial gui` 的确保钩子现在对 ownerless home 同样运行（准入按 daemon 的 ping 阶段决定 bootstrap 或 serving）；子进程转发到 bootstrap daemon，关闭 GUI 不再停 daemon。见 KP-083。
- 原生壳（PR 6c，2026-11-16）：计划 6c-6 要求登记“served home 但没有 daemon”，本条即该问题，不另立编号。DL-1 已在 dev（`commands.py` 的 `ensure_daemon`），壳仍只运行 `hyprial gui`，在状态页显示 CLI 的错误文本；壳不启动 daemon。含 DL-1 的捆绑运行时在 macOS 上的第二次启动**未实测，UNKNOWN**（Mac 复验清单）。
- 依据：[daemon 先行设计](../design/daemon-first-init-login-decoupling-2026-11-15.md) 第 2.3、2.4 节（事实 G3–G6）与第 6 节 PR 序列。真实 OIDC 登录、launchd、macOS 未验证，见该文第 8 节，不记 PASS。
- 负责人：daemon 生命周期与 GUI 启动装配维护者；CLI 收敛区内的部分与 CLI 收敛开发者协调。
- 状态变化（2026-11-19）：已修复，代码在 dev。#2845（DL-1）与 #2916（Z-6a）已合入：`hyprial gui` 在 home 的每种状态下都先确保 daemon，关闭 GUI 不停 daemon。#2933（Z-6b）已合入：GUI 子进程不再托管 daemon，上文“daemon 就是 `request_http` 子进程本身”的情形不再存在。“规避”一节不再需要。含 DL-1 的捆绑运行时在 macOS 上的第二次启动仍 UNKNOWN（Mac 复验清单）。

## KP-065 发布快照内网门在 native conformance 路由文档上命中

- 现象：`tests/tooling/release/publish/github/test_snapshot.py::test_intranet_gate_on_a_real_repo_snapshot_reports_only_pending_hits` 报 `docs/reference/ci-native-conformance-routing.md:124` 的 `intranet-git-host` 与 `staff-email` 两项命中，`PENDING_HITS` 为空集，断言失败。
- 影响：自 #2821 合入（`b55e09b14`，2026-11-15）起，`dev` 每次完整 push 的 `pytest-shard (1)` 必红；所有 docs-only PR 的 `gate-python / checks` 必红；GitHub 发布快照门同样会拒绝该行。
- 规避：无。不要把命中项加入 `PENDING_HITS` 或 allowlist。
- 决定：修复候选已进入 #2833，clone 地址已改为占位符，未增加豁免；待合入与真实 CI 验收。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) R1：CI job 155711 的引用行，本地 4 次运行 4 次失败；修复后的最终组合提交上发布快照套件 **161 passed**。
- 负责人：CI / 发布维护者。

## KP-066 CI 镜像上下文清单未登记 `native-conformance / plan`

- 现象：`tests/tooling/ci/acquisition/test_ci_dependency_caches.py::test_real_tree_denominator_and_family_witnesses` 报 `assert not ['unaccounted current image contexts']`；用同一扫描器求差集，唯一未登记项是 `native-conformance.yml` 的 `plan` job（`runner-image release-ci`）。
- 影响：自 #2821 合入起，`dev` 每次完整 push 的 `pytest-shard (4)` 必红。
- 规避：无。
- 决定：修复候选已进入 #2833，在 `tests/support/ci_download/release_images.py` 的 `INTRODUCED_NATIVE_CONFORMANCE_IMAGE_CONTEXTS` 补登记真实 job；扫描器和断言未改，待合入与真实 CI 验收。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) R2：CI job 155714，本地 4 次运行 4 次失败；修复后的最终组合提交上镜像上下文套件 **123 passed**。
- 负责人：CI / 发布维护者。

## KP-067 `gate-python / checks` 的 GUI 集成步骤没有原生引擎

- 现象：变更范围为“未选中 unit、选中 gui_integration”时，`checks` 的步骤 “Run Python GUI integration for audited GUI changes” 中 `test_cli_adapter_add.py` 1 例与 `test_cli_init.py` 2 例以 `ActorBackendUnavailableError`（native 扩展不可导入）失败。
- 影响：`9932b2b63`（#2826）与 `086070982`（#2831）两次 push 的 `checks` 均红，`unit` 随之红；同范围的 PR 同样受阻。GUI 单启动线 PR 2b 只给 `gate-gui.yml` 加构建步骤，不覆盖本项。
- 规避：无。
- 决定：修复候选在独立 PR #2836，选择复用 `pytest-shard` 的原生引擎构建三步，不改用 `reference-abi`；workflow 变更由授权维护者合入。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) R3：CI job 155390、155863。候选在无引擎时按原文件清单复现 **3 failed, 197 passed**，构建后 **200 passed**；真实 Forgejo 结果以 #2836 为准。
- 负责人：CI 维护者。

## KP-068 `gui-unit` 的开发入口与正式入口在 CI 上相差 1px

- 现象：`gui/tests/dev-product-parity-browser.mjs` 的 “archived tools open the same mock input separately and preserve the active journey” 报 `scenario j5=m11 org page: layout.page differs — dev=[216,32,1080,452] production=[216,32,1080,453]`。
- 影响：`f1a4caf86` 起 `dev` 每次执行 GUI 步骤的 `gate-gui / gui-unit` 都红（4 次 push，引用行相同）；此前被真实 daemon 回环用例的失败遮住。
- 规避：无。本地 `verify:dev-browser-parity` 1 次通过，不复现，不能以本地通过代替 CI 结论。
- 决定：待修，根因 UNKNOWN。回归落在 `2e7c4e395` 与 `c4044b033` 之间，其间只有 #2825 改动 GUI 与 `gate-gui.yml`。不得加容差或删除断言。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) R4：CI job 155706、155747、155859、155932。
- 负责人：GUI 维护者。
- 2026-11-20 状态变化：关闭（owner 同意）。#2842（等待字体加载后再量几何）合入后，dev 的 152 次 push 里 `gate-gui / gui-unit` 没有再出现这一差异；其中约 51 次 push 改动了 `gui/` 并实际跑完 `verify:dev-browser-parity`，全部通过，16 次 `gui-unit` 失败逐条查过日志，都不是这一项（依赖下载失败 11 次、runner 超时 3 次、另两条无关测试各 1 次）。断言仍是逐项精确比较，没有加容差。11-15 一位开发者在自己机器上的 dev `2ab5822` 读到 451/452，此后没有新的报告；组织页在 11-16 重做过（`28fff91815`、`5bfcc7b933`、`d2588fac42`），那次与最初的 452/453 读数都是对旧页面量的。CI 之外的机器、字体与渲染环境没有验证；再出现时在本条下追加，不新开条目。

## KP-069 `pytest-shard (3)` 触发 runner 的 30 分钟上限

- 现象：runner 以 “exceeds the maximum run time” 取消 `pytest-shard (3)`，取消前进度 99%。2026-11-15 的 30 次 push 运行中 11 次被取消；成功时平均 28.1 分钟，其余 shard 为 15 至 20 分钟。
- 影响：被取消的运行使 `unit` 变红，且该 shard 的用例结果缺失。
- 规避：无；重跑不改变耗时分布。
- 决定：主修复候选在独立 PR #2841。含探针的 shard 拆为两波，只让探针波使用 `-n 1`，其余文件保持 `-n 2`；不改 timeout、matrix、lane 或测试集合。后续五次 dev push 的耗时用于验收。权重刷新仍是独立 BLOCKED 测量项；若未来刷新把探针移入 catch-all shard，须先扩展同样的波次约束。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) R5、1.1 与 2.2 节统计；真实两波执行为探针 **8 passed**、普通文件 **2 passed**，合入最新 dev 后 `tests/tooling/pytest/` **110 passed**、Node **162 passed**；[dev CI 稳定化设计](../design/dev-ci-stability-2026-11-14.md) 工作包 A3。
- 负责人：CI 维护者。

## KP-070 Python 3.14 本地环境下比较线程名的用例失败

- 现象：`uv sync` 在没有固定版本的机器上会选中 CPython 3.14；此时 `test_state_persistence_authority.py::test_desired_and_journal_calls_share_one_owner_thread` 与 `test_launch_fencing.py::test_agent_home_lifecycle_io_does_not_hold_mailbox_or_registry_lock` 在 native 后端下各 5 次运行 5 次失败（线程名为不同的 `Dummy-N`）。同机 3.12.3 下通过；3.14 下 `reference-abi` 后端通过；CI（3.12.13）为绿。
- 影响：本地全量验证出现与改动无关的失败。其余 35 处 in-scope `current_thread()` 用法已逐一审查，没有发现另一个“同一外部线程多次进入后比较名字集合”的断言，无需批量替换。
- 规避：未合入修复前，本地可使用 `uv sync --python 3.12 --extra test` 与 `uv run --python 3.12 …`。
- 决定：修复候选在独立 PR #2837，两条断言改用 `threading.get_ident()`；不增加 `.python-version`，以免用版本固定回避 3.13+ 兼容问题。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) L1 与 1.1；最终组合分支在 Python 3.12.16 与 3.14.4 的 native 后端上各 **27 passed**，其余相关文件也已直接复验。Python 3.13.x 本身未单独实测（UNKNOWN）。
- 负责人：daemon / identity 测试维护者。

## KP-071 环境前置条件缺失时用例以普通失败出现

- 现象：缺 `pi` SDK 时 `test_review_e1c.py::test_pi_unsupported_cache_expires_on_new_daemon_epoch` 失败（原同列的 `test_squire_prepare_http.py` 两例已于 2026-11-21 随 Squire 设置面删除（S2b）一并移除）；缺 `ssh-agent` 时 `test_agent_git_ssh_capability_e2e.py` 三例失败；到 TEST-NET 地址的连接被立即拒绝时 `test_peer_reachability.py::test_tcp_probe_reports_timeout_for_test_net_blackhole` 失败；环境带有 `GH_TOKEN` / `GITHUB_TOKEN` 时 `test_wheel_assets.py::test_missing_github_token_is_not_evaluable_not_failure` 失败。
- 影响：在这类机器上的全量 pytest 固定多出这些失败；除第一例外，失败信息不说明缺少什么。CI 具备这些条件，为绿。
- 规避：在具备条件的环境运行；`test_wheel_assets.py` 可在去掉两个 token 变量后运行（实测通过）。
- 决定：待修。各用例先检测前置条件并在失败信息中写明 BLOCKED 与缺少的条件；`test_wheel_assets.py` 的子进程环境须清理继承的 token。不使用 skip 或 xfail。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) L2–L5。与 KP-024、KP-037 是不同的环境问题。
- 负责人：测试基础设施维护者与各用例所属域。

## KP-072 owner 迁移的残留检查按子串匹配

- 现象：`test_mcp_actor_by_name.py::test_owner_migration_relocated_session_refreshes_and_reads_as_same_session` 间歇以 `OwnerMigrationAborted`（“value(s) still contain the old owner”）失败并连带 fd 泄漏 teardown ERROR。用例的旧 owner 是 `op`；失败样例的随机 agent id 均含 `op` 子串。
- 影响：CI push 1 次（`f1a4caf86` 的 shard 2）；本地 10 次运行 2 次失败。推断真实的短 owner 名也可能因随机 id 而无法迁移，未在真实数据上验证。
- 规避：重新生成 id 后可能通过；不是可靠规避。
- 决定：待修。测试先改用不会出现在 base32 字母表中的 owner 字面量；产品是否把残留判定收紧到地址段由 daemon 维护者决定，不为通过而放宽重写。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) F6；`src/hyprial/daemon/impl/owner_migration/planning.py` 的 `LIKE` 查询与 `old in rewritten` 判定。
- 负责人：daemon owner migration 维护者。
- 状态更新（L3，2026-11-23）：已关闭。L3 删除了整个 `src/hyprial/daemon/impl/owner_migration/`（含 `planning.py` 的 `LIKE` 查询与子串残留判定）和 `test_owner_migration_relocated_session_refreshes_and_reads_as_same_session`；代码中不再有任何 owner 自动改写，换账号改为离线处理（`docs/guides/migration-account-binding.md`）。

## KP-073 CI 上 daemon 类子进程在预算内未就绪

- 现象：`test_profile_two_homes.py::test_t11_two_homes_two_isolated_daemons` 30 秒内 `daemon.sock` 未出现（#2821 之后 7 次完整 push 中 4 次）；request-http 集成用例报 “request-http did not publish server.json” 且子进程日志为空（`2c1befe8e` 两个 shard 各 1 次）；xdist worker 被信号 9 或 11 终止（当天 5 次，5 个不同用例）；`test_worker_relay.py` 两条用例以 `KeyError: 'result'` 失败（2 次）。
- 影响：`dev` 的 shard 2、shard 4 间歇变红。本地在负载下各 3 次运行均通过，不复现。
- 规避：无。
- 决定：待修，根因 UNKNOWN，各现象是否同因未知。先补取证（超时即输出子进程输出与线程栈、断言打印完整应答帧），再修根因；不以加长预算代替。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) F3–F5。信号 11 的崩溃与 KP-048 的关系未知。
- 负责人：daemon 生命周期与测试基础设施维护者。
- 更新（2026-11-19，PR #2960，上文保留）：只针对“xdist worker 被信号 9 终止”这一条现象，已建立的事实如下。
  - 签名：`gate-python / pytest-shard (4)` 在 `dev` 上几乎每个提交都红，xdist worker 在 shard 接近结束时被 SIGKILL（`CI_WORKER_EXIT` 的 `returncode` 为 -9、`signal` 为 9），controller 进程存活；pytest 把它记在 `tests/daemon/teleport/legacy_removal/test_source_tree.py::test_no_legacy_module_remains_under_src` 名下。产品与测试代码里没有任何一处会杀 worker，所以这是内存压力，不是该用例的逻辑。
  - 内存上限：CI runner 容器的内存上限是 4 GB（`e1d3c408d4` 的提交说明，2026-11-14，当时用它解释含两个真实 daemon 的 teleport 集成模块使 shard 超限）；shard 的 RAM 盘临时目录在运行中持续增长，它与 worker 常驻内存在同一个 cgroup 里计费。
  - 一个明确的放大因素：`tests/daemon/teleport/legacy_removal/support.py` 用 `functools.cache` 缓存全部约 1800 个 `src/hyprial` 文件的 `ast` 树，在 worker 的整个生命周期里不释放。同一进程实测比不缓存多约 570 MB（该 helper 的全部公共函数跑完后 worker 峰值约 895 MB，空闲约 300 MB）。
  - 本 PR 的改动：该 helper 改为每个文件只解析一次、只保留 imports、legacy 原因和函数名，语法树随即丢弃；需要任意节点的两个全仓扫描在检查内按需解析、不缓存。检查的文件范围、断言与失败信息（名称、路径、行号）不变，全仓 helper 输出在改动前后逐字相同。实测：同一进程跑完该 helper 的全部公共函数，峰值 RSS 由约 588 MB 降到约 39 MB；`pytest -n 2` 跑 `tests/daemon/teleport/legacy_removal` 时进程树中最大的单个进程峰值由 885 MB 降到 335 MB。另外 CI 日志现在留下内存证据：`ci-step.sh` 在每个步骤开始与结束各打一行 `CI_MEMORY`（cgroup 的 `memory.max`、`memory.current`、`memory.peak`、`oom_kill` 计数，结束行带 `oom_kill_delta`；缺失或格式不对的值写 `-`，不影响步骤），`CI_WORKER_EXIT` 增加 `oom_kill` 与 `oom_kill_since_start` 两个字段（读不到为 `null`）。
  - 仍是 UNKNOWN：内核是否确实是 OOM killer 杀了该 worker（要等下一次 worker 被杀的运行里看 `oom_kill` 计数）；本改动后 shard 4 是否转绿（尚未观察到任何运行）；信号 11、daemon 未就绪与 relay 错误帧三类现象不在此更新范围内，仍按上文处理。
  - 本 PR 未采取、仍可选的做法：把 pytest 的 `tmp_path_retention_policy` 设为 `"failed"`，或把 basetemp 移出 RAM 盘，以限制 RAM 盘增长；像 teleport 那样把启动真实进程的集成目录隔离出共享 shard（见 `e1d3c408d4`）。是否采用待上述证据出现后再定。

## KP-074 定时 macOS E2E 两条 lane 失败

- 现象：2026-11-15 定时运行各 1 次。`e2e-macos-isolated`：E2E-010 报 `cannot import name 'harden_private_path' from 'hyprial.kernel'`，E2E-024 报 “RED stale modes: swallow-close-delay-10-0”，其余 16 项 PASS。`e2e-macos-credentialed`：E2E-006 报 “No such command 'start'”，E2E-004 报 “timed out waiting for evidence: harness.request.accepted”。
- 影响：两条定时 lane 为红；E2E-006 与 E2E-004 的后续断言为 BLOCKED，不是 PASS。
- 规避：无。
- 决定：待修。E2E-006 的脚本 `contract/e2e/run-resident-providers.py` 仍调用已不存在的 `start` 子命令，可独立更新；其余三项需 macOS runner 复现，根因 UNKNOWN。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) 3.S：CI job 153167、153752。只有 1 个样本，未复现。
- 负责人：升级线、转发线、E2E 脚本维护者（待具体指派）。

## KP-075 `npm run verify` 链上的浏览器旅程脚本与当前界面不一致

- 现象：`verify:j3/j5/j6/j7-browser`、`verify:linked-browser` 等待 `.chat-workspace` 或 `.org-workspace` 超时；`verify:j2-browser`、`verify:j10-browser`、`verify:safe-r1-browser` 等待 `[data-mock-scenario="j1"]` 超时；`verify:first-run-browser` 在开发模块索引计数处断言失败。
- 影响：`npm --prefix gui run verify` 停在 `verify:first-run-browser`，其后的脚本不会被执行；这些脚本不在任何 workflow 中，失败无人看见。
- 规避：单独运行仍通过的脚本（`test:browser`、`verify:dev-browser-parity`、`verify:j4-browser`、`verify:agents-second-browser`）。
- 决定：待修。共享视图脚本的原因是场景串缺 `j1=account-ready`，临时补上后 j3、j5 实测通过，可直接修；首跑类四个脚本断言已退役的常驻开发标记（KP-025、KP-026），按浮动开发工具重写还是退役由 GUI 负责人决定。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) B1、B2。`org-lifecycle`、`org-rework` 两个脚本归 KP-063，已由 PR 1e-4 删除（场景清单见 KP-063）。
- 负责人：GUI 测试维护者。

## KP-076 `test:daemon-request-browser` 16 例中 10 例失败

- 现象：`gui/tests/daemon-request-host/browser-acceptance.mjs` 实测 1 次，6 通过、10 失败（824 秒）。失败分为六组：聊天入口无数据源时隐藏、组织空状态文案消失、Agent 表单目录路径无“职责”字段、“飞书渠道绑定”区块已删除、聊天提交未知结果提示位置变化，以及真实 daemon 的 `org.create` 返回 `tailnetInitialized: true`。
- 影响：该脚本不在任何 workflow 中；旧 Node 宿主路径的浏览器验收没有有效结论。
- 规避：无。
- 决定：不修（owner 2026-11-14 裁定，随 GUI 单一启动改造与旧 Node 宿主一起退场）；不加 skip，不恢复旧宿主。`tailnetInitialized` 一组原因 UNKNOWN，建议 daemon 组织 / 网络维护者做一次定点核对。
- 状态更新（PR 3，2026-11-16）：Node 宿主已删除，该脚本现在经 Python 会话 host 运行（`hyprial gui` / `hyprial gui dev` 的真实命令）。实测 1 次：16 例中 6 通过、10 失败，通过与失败的用例集合和各自的首个失败位置（`chat-layout` 不可见、组织创建断言、`tailnetInitialized` 断言等）与删除前一致；没有新增失败。决定不变：仍不修，也不加 skip。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) B4 的逐例表；[dev CI 稳定化设计](../design/dev-ci-stability-2026-11-14.md) 工作包 C2。
- 负责人：GUI 单启动线；`tailnetInitialized` 核对归 daemon 组织 / 网络维护者。

## KP-077 external promotion 用例的应答与计数竞争

- 现象：`tests/shell/request_http/launch/test_external_promotion.py::test_initialize_reply_flushes_then_republishes_a_new_generation` 在读到 HTTP 应答后立即断言 `host.post_reply_takes == 1`，间歇得到 0。
- 影响：2026-11-15 的 `dev` push 中 5 次使 shard 变红；本地 10 次运行 1 次失败。
- 规避：无。
- 决定：不修（owner 2026-11-14 裁定）。该用例仍在 `dev` 上并持续制造红色，建议向 owner 复核；复核前不改。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) F2；[dev CI 稳定化设计](../design/dev-ci-stability-2026-11-14.md) 的最新裁定段落。
- 负责人：request_http 宿主维护者（待 owner 复核）。
- 状态变化（Z-7 A1，#2963）：external promotion 随 `serve_external`、`nativeHost` 配置分支与 `tests/shell/request_http/launch/test_external_promotion.py` 一并删除，该竞争用例不再存在，问题随删除关闭；本条保留为历史。

## KP-078 `native-conformance` PR lane 的排队与下载超时

- 现象：`native-linux-arm64` 由单个 runner 串行执行，2026-11-15 从 00:41 到 19:05 首尾相接，单次 40 至 65 分钟（22 次成功、4 次取消）；`native-darwin-arm64` 在 PR 上 9 次失败，抽查的 2 次均为 Rust 工具链清单下载超时。
- 影响：PR 的 `native-summary` 长时间 pending 或为红；#2821 按变更范围路由后队列仍在。push 运行不含该 workflow。
- 规避：无。
- 决定：Darwin arm64 待修（设计稿已有下载策略候选）；Linux arm64 只调查、不修、不删，等 owner 的平台决定。
- 依据：[dev 红色基线 2026-11-15](dev-red-baseline-2026-11-15.md) 2.4 节：CI job 155554、155614；其余 7 次 Darwin 失败未读。
- 负责人：CI 维护者；平台取舍归 owner。

## KP-079 bootstrap host 被 `kill -9` 后，陈旧的 `bootstrap.json` 使下一次准入失败

- 现象：ownerless 的 bootstrap host（GUI 托管的 `GuiRuntimeHost`，以及 Z-1 的 `BootstrapWireHost`）被 `kill -9` 后，`state/bootstrap.json` 留在原地；下一次 `BootstrapRuntime.admit` 以 “record belongs to another bootstrap instance” 失败，而两把锁此时已释放。Z-1 独立复核实测，GUI 路径同样如此。（Z-6b 删除 GUI 托管路径之后，只有 `BootstrapWireHost` 还会留下该记录。）
- 影响：手工删除该文件之前，该 home 无法再进入 bootstrap；在服务管理器（`KeepAlive`、`Restart=`）下表现为无法自愈的重启循环。
- 规避：确认没有 daemon 在运行后，删除该 home 的 `state/bootstrap.json`。
- 决定：已修复（Z-2，两步）。第一步：准入确认 `bootstrap.json` 中的进程已退出（出生标识逐位一致）后删除陈旧记录，再重新获取准入。第二步（评审发现的回归）：为让 `daemon start` 认出 bootstrap，Z-2 新增了带 `phase: "bootstrap"` 的 `state/daemon.json`，它没有心跳，被杀后 `previous.py` 拒绝它（`previous daemon birth identity cannot be verified`），home 卡死；现在该标记记录自己的 `birthIdentity`，仅当操作系统明确报告该出生标识已不存在（且与 `bootstrap.json` 一致）时才与 `bootstrap.json` 一并删除。存活进程（出生标识一致）、缺少出生标识的标记、平台读不到出生标识的情形仍然 fail closed；serving daemon 的 `daemon.json`（无 `phase`）与 `.active_daemon` 心跳的校验没有放宽。第三步（Z-2 修复轮 3）：pid 被复用（出生标识与该 pid 上的存活进程不同，常见于重启后）视为已记录的 bootstrap 进程已退出，在准入锁内回收两份记录，不接管也不向任何进程发信号；此前该情形以 “PID has been reused; refusing takeover” 拒绝，在服务管理器下同样是重启循环。仍需手工清理的情形：标记缺少 `birthIdentity`，或平台无法读取出生标识；这两种情形仍 fail closed，需确认无 daemon 后手工删除 `state/daemon.json` 与 `state/bootstrap.json`。其余情形均自动回收。
- 实测（native 引擎，隔离短路径 home，`HYPRIAL_OWNER` 未设置）：`daemon start` 后 `kill -9` 再 `daemon start` 退出码 0、`phase: bootstrap`、新 pid；经 `daemon run` 在 `awaiting-auth` 与 `authenticating` 阶段 `kill -9` 后重启均进入 bootstrap，随后 SIGTERM 退出码 0 且文件清理干净。回退 `previous.py` 的修复，上述三条集成测试与九条单元测试变红。真实 OIDC 登录与真实 Tailcat sidecar 未参与（用受控 identity 端口）。
- 依据：[daemon 先行设计](../design/daemon-first-init-login-decoupling-2026-11-15.md) 6.3 节第 1 条；Z-1 独立复核记录。
- 负责人：daemon 先行方案 Z-2 的实现者。

## KP-080 安装态 `hyprial gui` 在 Windows 上不可用

- 现象：wheel 的 `hyprial gui` 现在与源码入口一样经 admission、`request_http` 与 Python 会话 host 启动，运行时目录用 `fcntl.flock` 与 `os.getuid`（`backend/platform_support.py`）。没有它们的平台在启动前得到 `GUI_RUNTIME_UNAVAILABLE`。此前 wheel 走无后端的静态服务，不需要这些能力，Windows 上可以打开占位页。
- 影响：只有 Windows 的 wheel；Linux 与 macOS 不受影响。Windows 原有的测试、门禁与 CI 目标保持不变，本条不删除任何一项。
- 规避：无；在 Linux 或 macOS 上使用 `hyprial gui`。
- 决定：不修。owner 2026-11-15 裁定当前只构建与验证 Linux 与 macOS（macOS arm64、Linux x64）；Windows 只登记不排期，不新增 Windows 代码或测试。
- 依据：[GUI 单入口计划 host-2-3](../design/gui-single-launch-plans-2026-11-15/host-2-3.md) 的 OD-4 与 2026-11-15 owner 决定段落；`tests/shell/gui/installed/test_installed_admission.py` 钉住无 `fcntl`、无 `getuid` 时的拒绝。
- 负责人：GUI 启动维护者；PR 6 之前把运行时锁改为 `kernel.ProcessFileLock`，之后再复核是否收录 Windows。
- 状态更新（2026-11-21，W1C）：owner 2026-11-21 解除「Windows 不排期」，并裁定业务代码不得「判断平台后直接 raise 拒绝 Windows」。
  `backend/platform_support.py` 及其 preflight 已删除：运行时单实例锁（`supervisor.lock`）改用 `kernel.ProcessFileLock`（POSIX flock / Windows LockFileEx，一次非阻塞尝试，第二个前台 GUI 仍得 `GUI_RUNTIME_BUSY`）；
  仅当 `<state_dir>/gui` 确实位于已验证的 Home 边界之内（且中间无逃逸出 Home 的 symlink/reparse）时只查形状（真实目录、非 symlink/reparse），不再重复 `st_uid`/`0o077`；
  凡 `home` 为空（如 Contract Mock）、显式 `HARNESS_STATE_DIR` 位于 Home 之外的外置状态根、以及没有 state 目录的临时目录兜底根，一律保留硬门（新建即 `harden_private_path`，状态根/兜底根与运行时子目录都过形状校验与 `private_tree_violation`，Windows 上为 DACL）；
  `--stdin-stop` 不再拒绝无 `signal.pthread_kill` 的平台，改经 `kernel.manysys`（`StdinStopWatcher` 与 `deliver_main_termination`：POSIX `pthread_kill`，无 `pthread_kill` 时 `_thread.interrupt_main(SIGTERM)`）投递同一清理控制流。
  `GUI_RUNTIME_UNAVAILABLE` 仍是已发布错误码，继续由运行时目录/日志/锁的真实失败产生，只是不再因平台产生。
  测试改为钉住「无 `fcntl`/`getuid`/`pthread_kill` 时不拒绝」（`tests/shell/gui/backend/test_source_platform_import.py`、`tests/shell/gui/installed/test_installed_admission.py`、`tests/shell/gui/startup/`）。
  Windows 真机结果：UNKNOWN，按 `scripts/verify/test-wheel-on-windows.ps1` 的 GUI 段验收（启动记录、`--stdin-stop` 退出且无残留子进程、第二实例被锁拒绝）；真机通过后再把本条改为已修复。

## KP-081 非默认 home 的 per-user 服务单元以 0644 写入代理变量（可含 userinfo）

- 现象：Z-8b 把 HTTP_PROXY / HTTPS_PROXY / NO_PROXY（含 URL userinfo）渲染进每-home
  的 launchd plist / systemd unit；单元文件仍按既有 `_atomic_write` 以 0644 写入。
- 影响：同机其他用户可读到代理凭据（当代理 URL 含 userinfo 时）。仅涉及配置了带凭据
  代理的非默认 home；默认 home 的固定单元名与既有模板行为不变。
- 规避：代理凭据使用仅本机可读的配置，或避免在代理 URL 中嵌入 userinfo。
- 决定：接受（owner 2026-11-15，OD-Z7 选项 a）：照样渲染并保持 0644，不做过滤，
  不改 0600。
- 依据：PR #2830 Z-8 实施与 owner 决策记录（2026-11-15）。
- 观察（launchd 验收 2026-11-16）：该次验收渲染出的代理值不含 userinfo，所以只验证了渲染形态，没有验证含凭据时的暴露面；本条的决定不变。
- 负责人：daemon 生命周期维护者；发布说明列为已知问题。

## KP-082 per-user 服务单元的 `PATH` 以 `~/.local/bin` 开头

- 现象：`ServiceConfig.path_env` 以 `~/.local/bin` 开头（为了让 uv tool、pipx、codex shim 的 harness 可执行文件可被找到），非默认 home 的 daemon 经 `PATH` 按名字查找 `hyprial` 或 harness 时，先命中该处的安装，而不一定是本单元所属的安装。
- 影响：同机存在多个安装时，daemon 派生的子进程可能运行另一个安装的 `hyprial`；单元自身的程序路径不受影响（单元用绝对路径，见 D1/D3）。
- 规避：非默认 home 的子进程不依赖按名字查找 `hyprial`；需要时在单元环境显式指定。
- 决定：待修，未在 D1–D3 修复批次内处理；读代码所得，未实测。
- 依据：launchd 验收报告 F1（PR #2830，2026-11-16）。
- 负责人：per-home service line。

## KP-083 ownerless home 上显式 `daemon start` 之后 `hyprial gui` 启动失败

- 现象：本 PR 之后，ownerless home 上显式 `hyprial daemon start`（或 `daemon run`）会留下一个持有该 home 两把锁的 bootstrap daemon；随后在同一 home 上 `hyprial gui` 约 24 秒后以 `GUI_START_FAILED`（`request-http exited before publishing its private capability`）失败。dev 上是 `daemon start` 本身失败，GUI 反而可用。
- 影响：只有显式 `daemon start` / `daemon run` 能走到这条路径（`init`、`login` 与 GUI 自身的启动不会创建 bootstrap daemon）；仅限 ownerless home。
- 规避：`hyprial gui` 之前先 `hyprial daemon stop`。
- 决定：待修，由 Z-6a（GUI 准入与 bootstrap daemon 的衔接）处理；本 PR 不触碰 `shell/impl/gui` 与 `request_http`。
- 依据：Z-2 第三轮独立复核的真实运行；[daemon 先行设计](../design/daemon-first-init-login-decoupling-2026-11-15.md) 6.3 节 GUI 准入一行。
- 负责人：daemon 先行方案 Z-6a 的实现者。
- 修复候选（Z-6a，2026-11-17，分支 `feat/z6a-gui-child-forwards-bootstrap`，#2916，待 CI/合入验收）：`GuiRuntimeHost` 先探测 home 的 daemon socket，daemon 以 bootstrap 应答时转发（不取锁、不写 `bootstrap.json`、不开日志、不 promotion），`serve_gui` 观察到 daemon 阶段或 epoch 变化时换端口与 bearer 重新发布，logout 之后只要所附 daemon 进程还活着就不接管锁；`hyprial gui` 在 ownerless home 上同样先确保 daemon，并按 daemon 的 ping 阶段准入。回归测试：`tests/integration/gui_daemon_first`（真实进程、native 引擎：子进程附着、HTTP 登录与 initialize 在同一 daemon pid 内完成、logout 回到 bootstrap 后重新附着、真实 `hyprial gui` 在 ownerless home 上启动 daemon 并在 GUI 结束后保留）在 dev 源码上红、本分支绿。已知限制：从未成功 ping 过的 daemon 没有记录 pid，之后的重开可能在 daemon 关闭与重开锁的间隙里托管；转发前只做一次 ping 校验，不是原子栅栏。未实测（UNKNOWN）：浏览器侧、真实 OIDC、macOS 与原生壳（Mac 复验清单见 Z-6a 计划）；owned home 上启动的 daemon 在 logout 后仍退出（计划 Q2），修复候选见下一条 Z-6pre。
- 修复候选（Z-6pre，2026-11-17，分支 `feat/z6pre-owned-start-survives-logout`，#2927，待 CI/合入验收）：owned home 上的 `daemon run` 以 `exit_unless_logout` 启动应用；纯 logout 停止后在同一进程进入同一条生命周期循环（不重复检查期望 identity generation，logout 后 login 记录已不存在）并回到 bootstrap，之后的 login 由同一 PID 服务。owner 在 logout 后仍存在的 home（`HYPRIAL_OWNER`、部分移除）不能被 bootstrap 准入，维持原行为：进程以 0 退出。其余停止原因不变。回归测试：`tests/shell/cli/daemon/lifecycle/test_owned_start_logout.py`、`tests/integration/daemon_lifecycle/test_owner_homes.py`、`tests/integration/gui_daemon_first/test_owned_logout_reattach.py`（在 dev 源码上红、本分支绿；另含不同账号二次登录同一 PID 的测试）。未实测（UNKNOWN）：macOS、service unit 下的 `daemon service status`、真实 OIDC。
- 修复候选（Z-6b，2026-11-17，分支 `refactor/z6b-delete-gui-hosted-bootstrap`，待 CI/合入验收）：删除 GUI 托管 bootstrap。`local-gui` 子进程在任何阶段都不取 `daemon.lock` 与 identity 事务锁、不写 `state/bootstrap.json`、不开 onboarding 日志、不构造 `DaemonApplication`，也不再 promotion；无 daemon 应答的 ownerless home 上启动即以既有的 `BACKEND_UNAVAILABLE`（退出码 3）拒绝，重开阶段等不到 daemon 同样如此（见 KP-102）。因此 Z-6a 条目里的已知限制（从未成功 ping 过的 daemon 没有记录 pid，之后的重开可能在 daemon 关闭与重开锁的间隙里托管）不再存在：没有任何路径托管，`daemon_alive()` 不是 `True` 时只会在预算耗尽后拒绝。启动器的准入对 ownerless home 无钩子时也先探测 daemon，无应答为 `GUI_DAEMON_UNAVAILABLE`。仍保留：转发前只做一次 ping 校验，不是原子栅栏；`rotated_config`、`new_host` 与 `serve_external`、`nativeHost`、`bootstrap/response.py` 留给 Z-6c/Z-7。回归测试：`tests/integration/request_http/test_lifecycle.py`（真实子进程，ownerless 无 daemon：退出 3、无 `server.json`、锁未被取）、`tests/integration/gui_daemon_first/test_logout_reattach.py`（bootstrap 阶段 logout 后重新发布）及转发、启动器准入、架构测试。同一 PR 还修了被托管回退掩盖的 daemon 缺陷：bootstrap 阶段（登录后、initialize 前）的 logout 会让 daemon 以状态 1 退出（`previous process has not exited`，同进程重开 bootstrap 前没有删除自己的 `daemon.json` bootstrap 标记），回归测试 `tests/integration/daemon_lifecycle/test_same_pid_lifecycle.py::test_a_logout_before_initialize_keeps_the_process_and_serves_the_next_login`（在 dev 源码上红、本分支绿）。修复候选未经 CI，浏览器侧、真实 OIDC、macOS 与原生壳未实测（UNKNOWN）。
- 限制变化（Z-6c，2026-11-18，#2946，修复候选）：`rotated_config` 的换端口与每个阶段一个监听器的做法已删除，initialize 与 logout 在同一子进程、同一 endpoint 上原地换主体（新 bearer 与 generation，旧 bearer 401）；“转发前只做一次 ping 校验，不是原子栅栏”仍成立，换主体本身由一个不可变绑定的单次替换保证不跨主体分发。
- 限制变化（Z-7 A1，#2963，修复候选）：Z-6b 条目里留给 Z-7 的 `serve_external`、`nativeHost` 与 `bootstrap/response.py` 已删除，同一 PR 还删除了 `request_http bootstrap`（无主 home 上的 HTTP 登录进程）、`BootstrapHttpHost` 与终端登录循环 `launch_bootstrap_daemon`；`daemon run --bootstrap` 选项仍被接受、不再产生效果（删除该选项另行处理）。
- 状态变化（2026-11-19）：已修复，代码在 dev。#2916（Z-6a）、#2927（Z-6pre）、#2933（Z-6b）、#2946（Z-6c）与 #2963（Z-7 A1）均已合入，上文各“修复候选”行中的“待 CI/合入验收”不再适用。仍保留的限制：转发前只做一次 ping 校验，不是原子栅栏。未验证（UNKNOWN）：浏览器侧、真实 OIDC、macOS 与原生壳。
- 2026-11-19 更新（#2822 步骤 1–4，#2985、#2995、#2996 已合入）：daemon driver 下 `request_http` 由会话 host 在自己的进程内提供，`hyprial gui` 只起一个 Python 子进程，不再有独立的 `local-gui` 子进程（`python -m hyprial.shell.impl.request_http serve` 作为独立入口仍在，仍接受 `local-gui` 配置，是否保留待 owner 决定）。上文各行里“子进程转发”“子进程附着”所描述的行为由该 host 进程承担；原地换主体的规则不变：新主体有新 bearer 与新 generation，旧 bearer 被拒，只是 bearer 在进程内由 watcher 交给 host，不再经过独立 endpoint，也不再写 `server.json`。启动记录的 `backend_pid` 为 `null`、`backend_endpoint` 为空串。上面的旧文字保留，描述 #2822 之前的行为。
- 2026-11-20 状态变化（owner 批准）：`python -m hyprial.shell.impl.request_http serve` 现在拒绝 `local-gui` 配置，以既有的 `CONFIG_INVALID` 退出码 2 退出，只服务 `local-operator`；前台的 GUI serve（`serve_gui`）已删除，`local-gui` 主体只由会话 host 在自己的进程内提供。上面 2026-11-19 更新里“仍接受 `local-gui` 配置，是否保留待 owner 决定”已不再成立。同批删除了 request_http 配置的 `transport` 块，配置文件里残留的 `transport` 键现在按未知键拒绝（`CONFIG_INVALID`，退出码 2）。

## KP-084 bootstrap daemon 上 `hyprial daemon restart` 失败

- 现象：bootstrap 阶段的 daemon 不服务 `ps`，`hyprial daemon restart` 以 `METHOD_NOT_FOUND`（`ps`）失败。
- 影响：仅 ownerless home 上正在运行的 bootstrap daemon。
- 规避：`hyprial daemon stop` 之后 `hyprial daemon start`。
- 决定：待修，由 Z-4 处理。
- 依据：Z-2 第三轮独立复核的真实运行。
- 负责人：daemon 先行方案 Z-4 的实现者。
- 修复候选（Z-4，2026-11-17，分支 `feat/z4-cli-ensure-daemon`，#2925，待 CI/合入验收）：`daemon restart` 的状态闸门在 `ps` 以 `ACCOUNT_REQUIRED`、`METHOD_NOT_FOUND` 或 `CALLER_NOT_AUTHORIZED` 被拒时，用一次 `ping` 确认 daemon 处于 bootstrap，并以 `{daemon, agents: [], connectors: []}` 继续（没有可被打断的 turn）；结果的 `before`/`after` 对 bootstrap daemon 增加 `phase: bootstrap`。回归测试：`tests/shell/cli/ensure/test_restart_bootstrap.py`、`tests/integration/cli_ensure/test_bootstrap_restart.py`（在 dev 源码上红）。未实测（BLOCKED）：此容器里 `daemon restart` 的停止/启动段在隔离 home 上以 `SERVICE_SUPERVISION_LOST`（`service-account-home-mismatch`）结束，serving daemon 在未修改的 dev 上同样如此（KP-093 一类的隔离限制），所以受监管的整段重启只由单元测试覆盖，未在真实进程上验证。
- 修复候选（Z-4b，2026-11-17，分支 `fix/z4b-account-required-router-only`，#2944，待 CI/合入验收）：bootstrap daemon 对 serving router 自身分支应答的所有方法名（含 `ps`）返回 `ACCOUNT_REQUIRED`，不再是 `METHOD_NOT_FOUND`；`daemon status`、`daemon restart`、`doctor` 本来就接受两种码。回归测试：`tests/daemon/onboarding/wire`、`tests/daemon/ipc/router_only`、`tests/integration/daemon_lifecycle/test_ownerless_cli.py`。
- 状态变化（2026-11-19）：已修复，代码在 dev。Z-4（#2925）与 Z-4b（#2944）均已合入：`daemon restart` 的状态闸门对 bootstrap daemon 用一次 `ping` 确认后继续，bootstrap daemon 对 router 自身分支的方法名一律答 `ACCOUNT_REQUIRED`。上文“待修”“修复候选”各行描述的是合入前的状态。受监管整段重启只有单元测试覆盖，真实进程上的验证仍为 BLOCKED。

## KP-085 `login` 在网络隔离设置下仍联系身份 issuer

- 现象：`hyprial login` 即使设置 `HYPRIAL_NETWORK_ISOLATED=1`，也会向配置的（公共）身份 issuer 请求 device code。Z-5 PR 2 之前 `hyprial init` 在没有 owner 的 home 上也会（它内部运行登录）；现在 `init` 不再登录，只启动 bootstrap daemon，不联系 issuer（#2967）。没有账号的 home 上的首次登录现在由 daemon 联系 issuer，不再是 CLI 进程；`HYPRIAL_NETWORK_ISOLATED` 对它仍不构成约束。
- 影响：dev 上既有行为，不是本 PR 引入；在隔离环境里探测 ownerless home 的人会无意触达公共 issuer（之前有两个 agent 因此误触）。
- 规避：在没有 owner 的 home 上不要运行 `hyprial login`，除非 issuer 是受控的（测试 harness 的 `child_entry.py` 或 fake OIDC）。
- 决定：待修（dev 既有，独立于 Z-2）。
- 依据：Z-2 第三轮独立复核；`HYPRIAL_NETWORK_ISOLATED` 只约束 daemon 网络，不约束登录 CLI。
- 负责人：identity / login 维护者。

## KP-086 ownerless 准入的拒绝原因没有进入 `DAEMON_START_FAILED`

- 现象：ownerless 准入的每一种 fail-closed 拒绝（“previous process has not exited”、“PID has been reused”等）都让 `daemon start` 返回 `DAEMON_START_FAILED "daemon exited during startup with status 1"` 且 `recentLog: null`；真实原因只在 `state/daemon-launch.log` 中以 `BOOTSTRAP_LIFECYCLE_FAILED` 的自由文本出现。
- 影响：诊断需要读日志；行为与退出码正确。
- 规避：读 `state/daemon-launch.log`。
- 决定：待修（后续）。`recentLog` 由 `daemon_start.py` 的 `_daemon_launch_log_summary` 有意恒置 null（任意 daemon 输出从不进入 CLI JSON）；它只透传白名单错误码与有类型的字段。让原因可见需要为这些拒绝定义新的稳定错误码并登记到 `src/hyprial/error/codes.py`（并由 `kernel` 的 `ipc_errors.py` 转出），再加入该白名单，这两处超出本轮可改范围。另有一条不需要 `ipc_errors.py` 的路径：在 `common/daemon_start.py` 的 `_SAFE_DAEMON_STARTUP_EVENTS` 中增加一个只增不改的命名拒绝事件，由 daemon 写入 `daemon-launch.log`、CLI 透传；它是可行的，本轮有意推迟而不是被阻塞，未实现。两条路径都会改变 `DAEMON_START_FAILED` 数据的发布形状（Contract-Impact）。
- 依据：Z-2 第三轮独立复核；`daemon_start.py` 中 `_daemon_launch_log_summary`。
- 负责人：daemon 先行方案 Z-5 之前的 CLI 维护者。

## KP-087 `kill -9` 之后 `lastExitCode` 为 null

- 现象：管理器因 `kill -9` 重启 daemon 后，`daemon service status` 的 `lastExitCode` 为 null。
- 影响：状态里缺少上一次退出的信息；不影响监管判定。
- 规避：无。
- 决定：待测，原因未查（launchd 的 `last exit code` 行在被信号杀死时的形态未知）。
- 依据：launchd 验收报告 F5（PR #2830，2026-11-16）。
- 负责人：per-home service line。

## KP-088 pytest 对不存在的测试路径收集 0 项

- 现象：给 pytest 一个不存在的路径时验收得到收集 0 项、exit 5；交接任务给的路径 `tests/daemon/composition/service` 实际为 `tests/daemon/application/composition/service`。
- 影响：路径写错时只有 exit 5，没有“路径不存在”的明确提示，容易被当成“通过”。
- 规避：运行后核对收集到的数量；本批验证命令使用现有路径。
- 决定：待测；未调查 pytest 配置（`-q` 或 addopts 是否吞掉 “file or directory not found”）。
- 依据：launchd 验收报告 F6（PR #2830，2026-11-16）。
- 负责人：per-home service line。

## KP-089 systemd 上“按请求停止”与“失去监管”无法区分

- 现象：launchd 上 `daemon stop` 即 bootout，单元“已安装、未加载、无 daemon 服务”，doctor 现在把它报为 warn（`stopped-on-request`）。systemd 上 `systemctl stop` 之后单元仍是 loaded，状态里没有 `ActiveState/SubState`，与崩溃循环无法区分，仍按 `lost` 报失败。`daemon service status` 的 `supervision` 在两个平台上仍为 `lost`（契约的三值不变）。
- 影响：systemd 上一次有意的停止在 doctor 中仍显示红。
- 规避：`hyprial daemon start` 之后恢复。
- 决定：待修；需要把 systemd 的 `ActiveState/SubState` 放进 `ServiceStatus`。
- 依据：launchd 验收报告 F2（PR #2830，2026-11-16）；`tests/shell/cli/lifecycle/service/test_program_and_install.py`。
- 负责人：per-home service line。

## KP-090 `daemon stop` 超时消息在未观察 socket 的情况下声称 socket 已关闭

- 现象：`common/daemon_stop.py` 的 `DAEMON_STOP_TIMEOUT` 消息固定为 “daemon socket closed but teardown did not finish within …”。launchd 验收中，已装但未加载的单元旁边有 detached daemon 时，`daemon stop` 既不 bootout 也不发 IPC shutdown，102 秒后以该消息失败，而 socket 从未关闭。
- 影响：消息误导（已修）。
- 规避：不再需要。
- 决定：已修（PR #2830 修复轮，2026-11-16）：D2 路由修复消除了未发 shutdown 的路径，消息本身改为 “daemon teardown did not finish within Ns (<detail>; the control socket is gone | is still present | could not be checked)”，在超时时观察 socket；`daemon_stop.py` 属 `daemon_stop.py` 允许范围。
- 依据：launchd 验收报告 D2（PR #2830，2026-11-16）；`tests/shell/cli/lifecycle/service/test_stop_timeout_message.py`。
- 负责人：per-home service line。

## KP-091 serving 启动恢复的设备登记与 logout 竞态

- 现象：Z-3 的 serving 启动恢复在独立线程中登记设备（可能涉及网络）。若此时用户 logout，provisioner 仍可能把设备记录写给原 owner；恢复在登记返回后重读 owner，发现已变化即中止、不写 initialize 回执，但不删除这条设备记录。
- 影响：登出后 home 里可能残留旧 owner 的设备记录；其他 owner 登录时 `deviceReady` 因 owner 不符保持 false，恢复遇到他人设备记录会整体跳过。未观测到数据损坏，也没有真实 issuer/Tailcat 下的复现，竞态本身 UNKNOWN。
- 规避：logout 后重新登录前不依赖该设备记录；必要时手工清理设备记录。
- 决定：待观察，不在 Z-3 内扩大范围；需要 identity 的 logout 与 provisioning 共享事务锁才能根治。
- 依据：`src/hyprial/daemon/impl/application/onboarding/lifecycle/recovery.py` 文件头决策块。
- 负责人：daemon onboarding 维护者。
- 更新（2026-11-16，验证复核，上文保留）：竞态本身严重度低。恢复在 provisioning 后重读 owner，owner 已变则不推进任何回执，故不存在为其他 owner 完成的回执；被中断时遗留的 pending 设备回执在下次启动由日志重整为 unknown。provisioning 是本地 `genkey` 子进程（15 秒上限），不访问网络，窗口很短。上文“恢复遇到他人设备记录会整体跳过”已不成立：现在恢复与 bootstrap `device.ensure` 一样为当前 owner 重新登记旧记录。残留的仅是已登出后可能多出一份旧 owner 的设备记录，登录新 owner 时会被改写。

## KP-092 `daemon stop` 的应答无法表达“存在不可归属的单元，监管者可能重启 daemon”

- 现象：本 home 已安装或已加载的单元若不可归属（外来、不安全、或另一安装的解释器），`installed_supervisor()` 返回 None，`daemon stop` 只做 IPC shutdown 并返回 `ok: true`、`teardownCompleted: true`；KeepAlive/`Restart=always` 会在数秒后重启 daemon（launchd 验收 N1 中 `runs` 1→2）。
- 影响：应答的 `ok`/`teardownCompleted` 对“服务已停止”给出错误印象。N1 之前同一安装的另一种解释器拼写被误判为外来，现已修复；真正外来的单元仍会出现这种情况。
- 规避：stderr 仍输出 `warning: ... not attributable ...`；用 `hyprial daemon service status --json` 查看 `attributed`/`ownershipReason`。
- 决定：方案 B（所有者，2026-11-17）：保持 `ok: true` 与 `teardownCompleted`（语义不变：daemon 已完成拆除），对“已加载且不可归属”的单元新增加性字段 `supervisorMayRestart: true` 与 `unattributedSupervisor`（`platform`/`unit`/`attributed: false`/`ownershipReason`，字段名同 `daemon service status --json`，不含单元路径）。无此类单元时两个键都不出现（与 `survivingPid` 的可选字段做法一致）。读法：`teardownCompleted: true` 只说明这次停止完成了拆除，`supervisorMayRestart: true` 才说明管理器可能随后重启 daemon。契约见 `contract/daemon-lifecycle/service-supervision.md` 的 Stop routing。
- 状态：已缓解。剩余：只读 `ok` 的调用方仍不会被告知；仅“已安装未加载”或“已加载但无单元文件”的情形仍只有 stderr 警告或无信号，不出字段。
- 依据：launchd 验收报告 N1（PR #2830，2026-11-16）；`tests/shell/cli/lifecycle/service/test_same_installation_stop.py`。
- 负责人：per-home service line。

## KP-093 没有受支持的办法隔离管理器启动的 daemon 的环境

- 现象：原装单元以真实账号 `HOME` 启动 daemon，不带任何隔离变量；usage 采集会读取 `$HOME` 下各 harness 的凭据文件，home 级没有开关可关，只有环境变量 `HYPRIAL_USAGE_DISABLE`。
- 影响：只影响测试与验收：在共享机上无法用原装单元启动一个不触碰真实凭据的 daemon。验收只能经放在 `PATH` 最前的 launcher，因而“原装 console script 经 launchd 运行”未实测。
- 规避：验收用 launcher 固定设置隔离变量；单元测试用 fake。
- 决定：待修，未在本批次处理。
- 依据：launchd 验收报告 F3（PR #2830，2026-11-16）。
- 负责人：per-home service line。

## KP-094 恢复期间 status 暂列 device.ensure

- 现象：serving 启动恢复正在登记设备时，`onboarding.status` 返回 `deviceReady:false`、`nextActions:["device.ensure"]`，而 serving 阶段拒绝 `device.ensure`（`CALLER_NOT_AUTHORIZED`）。恢复完成后 `deviceReady:true`、`initialized:true`、`nextActions:[]`。
- 影响：短暂窗口内提示的下一步不可执行；provisioning 失败时（日志只在恢复面记录至多 8 条失败回执）该提示持续到下次成功启动，此时也是 serving 拒绝的状态。
- 规避：等待恢复完成或重启 daemon 触发下一次尝试；以 `deviceReady`/`initialized` 而非 `nextActions` 判断是否完成。
- 决定：Z-3 不修。要让 status 表达“恢复进行中”需要新增状态字段，属于契约变更（`request_catalog`、OpenAPI），不在本任务的协调区之外改动。
- 依据：Z-3 独立验证发现 F4；`lifecycle/recovery.py` 文件头决策块。
- 负责人：daemon onboarding 维护者。
- Z-5 更新（#2958）：daemon 驱动的首次登录（`auth.login.begin` 带 `until: serving`）在设备登记失败时仍然成功：登录结果的 `completion.device.ready` 为 `false`、`retry` 为 `serving-start`，初始化回执停在 `promoting`，同一进程进入 serving，此时 `onboarding.status` 就是本条描述的状态（`deviceReady:false`、`initialized:false`、`nextActions:["device.ensure"]`，serving 拒绝该方法）。恢复在 promote 之后的启动里也会运行（以前只在普通启动里运行），每次 serving 启动仍只尝试一次，所以这次登录所导向的启动里，登录结果之后几秒就有一次尝试，只有它也失败时才停在该状态直到下次 serving 启动；设备登记成功后 `initialized` 才变 `true`。GUI 在这个状态下的实际显示未运行（UNKNOWN）。决定不变：不修，未新增状态字段。

## KP-095 原生壳每次启动换端口，GUI 的 `localStorage` 不跨启动保留

- 现象：原生壳以 `hyprial gui --port 0` 启动，每次得到新的回环端口，即新的源；GUI 唯一写入 `localStorage` 的值（Squire 身份存储，`gui/src/app/capabilities.tsx`）按源保存，所以每次启动都从空开始。
- 影响：只影响原生壳；浏览器里固定端口的 `hyprial gui` 不受影响。会话本身在 host 内存中，本来就不跨启动。
- 规避：无。
- 决定：不修，按计划 OD-4 的默认值（`--port 0`）；固定端口回退方案的取舍待 owner 确认。
- 依据：`docs/design/gui-single-launch-plans-2026-11-15/shell-6.md` F7、OD-4；由代码推得，PR 6c 未单独测量。
- 负责人：原生壳维护者与 GUI 线。

## KP-096 原生壳的 WKWebView 数据不随 `HOME` 重定向

- 现象：即使覆盖 `HOME`（以及 `HYPRIAL_HOME`），WKWebView 仍把数据写到真实账号的 `~/Library/WebKit/com.hyprial.hyprialos`、`~/Library/Caches/com.hyprial.hyprialos`、`~/Library/HTTPStorages/com.hyprial.hyprialos.binarycookies`；而壳自己的 `launcher.log` 随 `HOME` 写到 `<HOME>/Library/Logs/com.hyprial.hyprialos`。#2840 与 PR 6c 的隔离运行实测。
- 影响：在开发机或共享 runner 上跑冒烟，会在该账号下留下（或改写）这些条目；与 hyprial home 无关，不涉及凭据。冒烟与验证器自己的隔离 home 另有一个 daemon：daemon-first 之后 `hyprial gui` 在任何状态下都会确保该 home 的 daemon 且关闭 GUI 不停它（owner 模型，不是缺陷），所以验证器与冒烟把本次运行 home 的那一个 daemon（home 自己的 `daemon.json`/`bootstrap.json` 记录的 pid，且占用隔离根）视为预期，用 `hyprial daemon stop` 停止并确认退出；停不下来时 `kill_carriers` 只是后备，且记为优雅停止失败（2026-11-18，Mac 实测 verify_staged 6/6 次因此失败后的修复；真实 Mac 复测 UNKNOWN）。
- 规避：冒烟之后列出这些目录，只删除本次运行新建的条目；不要在有真实 HyprialOS 安装的账号上删除整个目录。
- 决定：不修。壳不配置 webview 数据目录；位置与卸载清理写在 `packaging/clients/README.md` 的 App data and uninstall 一节；打包脚本 `scripts/build/package-macos-shell.sh`（2026-11-20 起只手动运行，没有打包 workflow）只列出不删除。
- 依据：#2840 合入评论第 3 项；PR 6c 证据。
- 负责人：原生打包线。

## KP-100 仓库里的 workflow 条件不是签名凭据的安全边界

- 现象：Forgejo 运行的是被推送或被派发的那个 ref 上的 workflow 文件，workflow 里的触发条件与 `if:` 只约束这一份文件。有写权限的人推一个新分支，带一份改过的或全新的 workflow（`runs-on: [release-macos-arm64]`），就能在发布 Mac 上运行任意代码并读取任何仓库 secret；`secrets` 是仓库级的，只对 fork 来的 `pull_request` 置空。Forgejo 文档（admin/actions/security.md "Preventing Arbitrary Workflow Execution"）明说分支保护不是 workflow 的安全边界。
- 实例读数（2026-11-17，只读 API，Forgejo 16.0.5）：dev、main 的分支保护设了 `protected_file_patterns: .forgejo/workflows/**`，但 `apply_to_admins: false`；没有 tag 保护；`release-macos-arm64` 是实例级 runner（owner、repo 都为 0）；`workflow_dispatch` 在本仓库用于过未受保护的分支。
- 影响：如果把 Developer ID 证书或公证凭据配成本仓库的 secret，或让 CI job 能用 runner 账号 keychain 里的签名身份，就等于交给每一个有写权限的人和实例上每一个能用该 runner 标签的仓库。
- 现状：没有任何 workflow 签名或公证，Forgejo 上没有配置签名 secret；`tauri.conf.json` 只做 ad-hoc 签名。`tests/tooling/release/shell/signing/test_no_signing_in_ci.py` 防止 workflow 重新出现签名凭据名、`notarytool`/`stapler`、keychain 导入或非 ad-hoc 的 `codesign --sign`，但它只是本仓库文件的回归检查，同样不是安全边界。打包脚本的 tauri-cli 默认装到新的临时目录并在退出时删除（CI 调用时是 `RUNNER_TEMP` 下的每次运行目录），不再复用持久的 `~/.cache` 工具目录；2026-11-17 起候选 workflow 只由 `workflow_dispatch` 手动触发，是该脚本的薄调用，打包期间由开发者本地运行脚本。
- 决定：签名不进 CI。Developer ID 证书留在打包用的 Mac 上；签名与公证推迟到另行规划的工作。如果将来要自动签名，必须先在 Forgejo 与 runner 一侧解决（例如签名 runner 只注册给一个只有 owner 有写权限的独立仓库，或使用不跑 CI 的独立 macOS 账号），仓库里的 workflow 条件不是安全边界。runner 账号是否持有签名身份的只读核对由 owner 另行处理。
- 安装签名身份（owner 2026-11-18）：owner 决定把 Developer ID Application 身份装进打包 Mac 的登录 keychain，并接受由此带来的后果——该 Mac 上的 CI runner 与它是同一个 macOS 账号，keychain 解锁期间，任何能用该 runner 的仓库、任何被推送分支上的 workflow 都能用它签名。安装由 owner 手动运行引导脚本 `./scripts/build/import-app-dev-id.sh`（它只驱动 `scripts/build/setup-macos-signing-identity.sh` 的子命令；`packaging/clients/README.md` “Installing the signing identity”）；代价更小的缓解是用 `--keychain` 放进不在搜索列表里、只在签名时解锁的单独 keychain。签名与公证本身仍推迟，workflow 仍不签名。
- 依据：PR #2889 评论 37622、37765；PR #2899 评论 37804、37806、37808、37809；Forgejo 文档 admin/actions/security.md、user/actions/reference.md、admin/actions/registration.md。
- 负责人：owner（Forgejo 设置与 runner 注册）；原生打包线（workflow）。
- 决定反转（2026-11-19，owner；Developer ID 签名里程碑 2）：上文“签名不进 CI”作废。owner 选择在 runner 上签名：runner 账号的登录 keychain 里已装有一个有效的 Developer ID Application 身份和 notarytool 凭据 profile，探测 run 证明 `release-macos-arm64` 的 job 能非交互地签名（hardened runtime + timestamp）并查询公证历史；runner 跑在已登录的 Aqua 会话里，keychain 已解锁。理由：签名与公证由同一台打包 Mac 上的同一份脚本完成，产物可以由 CI 手动产出并留证据，不再依赖有人在终端里手动运行。签名只有一个入口：`scripts/build/package-macos-shell.sh --developer-id`（显式身份 `--identity <40 位 SHA-1>` 或 `--discover-identity`、显式 `--notary-profile`、必须 `--source-sha`），它调用 `packaging/clients/sign_bundle.py`，所有 `codesign --sign`、`notarytool`、`stapler`、`spctl` 调用都只在这个文件及其 `packaging/clients/signing/` 下的 `modes.py`、`notary.py` 里；只有一个 workflow `shell-macos-signed.yml` 传这个开关，只有 `workflow_dispatch` 触发，不读任何 Apple 相关 secret（Forgejo 上仍未配置），`shell-macos-candidate.yml` 仍是 ad-hoc；不带开关时脚本的阶段、产物与文件名与之前完全相同；`tauri.conf.json` 仍为 `signingIdentity "-"`、`hardenedRuntime false`。失败即失败（fail closed）：任何签名、公证或检查失败都让整次运行失败，没有退回 ad-hoc 的路径，失败的签名运行不收集任何 dmg，只留 `NOT-SIGNED.txt`、`BUILD-INFO.txt` 与证据（`packaging/clients/README.md` “Developer ID signing”）。
- 仓库检查拦不住这件事：手动触发的 workflow 使用被触发提交上的 workflow 文件（其内联步骤也来自它），而之后执行的脚本来自输入 `source_sha` 的检出；能推分支的人可以同时改这两者；真正的控制点在仓库之外——Forgejo 上谁能推分支、谁能触发 workflow、哪些 job 跑在这台 Mac 的同一账号下，以及 keychain 允许哪些程序使用私钥（见下方 2026-11-19 PR #2980 评审记录）。`tests/tooling/release/shell/signing/test_signing_only_by_switch.py`（原 `test_no_signing_in_ci.py`）只防止签名被**意外**打开（开关只在一个手动 workflow 里出现、其他 workflow 仍不签名、不引用 Apple secret、不改 keychain、打包脚本自己不调用 Apple 工具），不防故意滥用；`test_identity_setup_policy.py` 不变，安装身份的脚本仍不得出现在任何 workflow 里。
- 重启：登录 keychain 只在已登录的桌面（图形界面）会话里是解锁的。Mac 重启后若没有人在图形界面登录，登录 keychain 是锁着的；这种情况**未测试**（没有为此重启过）。此时 workflow 的行为必须是**失败，而不是挂起等待**：`sign_preflight` 在长构建之前用该身份给一个临时文件试签名（`codesign --timestamp`，上限 120 秒，之后删除），锁着的 keychain 让它失败，若 `codesign` 等待对话框则 120 秒上限到期，同样失败；`sign_bundle.py` 的每个子进程都有超时、stdin 为 `/dev/null`，不可能回答任何提示，并在第一次真正签名前再试签一次（长构建期间钥匙串重新上锁时约 2 分钟内失败）；job 上限 180 分钟。整次运行失败，不产出任何 dmg。以上“失败而非挂起”由设计与假 keychain 测试保证（推断），真实重启场景未实测。之后如何解锁（谁、在什么会话里、是否允许无人值守解锁）是未决问题，本次不解决，也不在 workflow 或脚本里解锁 keychain。
- 源 remote 与分发（owner 2026-11-19 决定）：签名 workflow 把 `HYPRIAL_BUILD_SOURCE_REMOTE` 固定设为 `PUBLIC_SOURCE_REMOTE`（`resources.py` 的常量，构建钩子唯一接受的值），不是 workflow 输入。全部代码将镜像到 GitHub，仓库很快从内部服务器迁到 GitHub。迁移完成之前，签名包**仅供内部验证，不得对外分发**：包里记录的提交在 GitHub 上可能不存在，安装后的 `hyprial gui dev` 会取不到源码。迁移之后的规则：对外分发的包必须用 GitHub 上已经存在的提交构建。`BUILD-INFO.txt` 写明“仅供内部验证”（或 wheel 没有记录公开 remote 时写“不可分发”），并说明对外分发要求记录的提交存在于公开 remote、构建本身无法核对；构建不会联系 GitHub。
- 外部依赖：每次签名运行依赖 Apple 的公证服务与时间戳服务（每次两次提交：app 的 zip 与 dmg）。实测一次早期提交等待了 62 分钟，此后测得的每一次都在 2–4 分钟；`sign_bundle.py` 默认等待 30 分钟，签名 workflow 传 20 分钟（时间预算见下方 PR #2980 评审记录），job 上限 180 分钟。等待超时不会重新提交，失败信息里给出提交 id。服务不可用时签名运行失败，不降级。
- PR #2980 评审记录（2026-11-19，GUI 线设计评审 38554 与 #2965 复评 38551）：
  - 哪些代码在钥匙串解锁时运行（更正上文“能推分支的人”的说法）：签名 workflow 的文件与内联步骤来自被触发的提交（`GITHUB_SHA`，派发时该 ref 的顶端）；之后执行的每个文件来自 `source_sha`。检出步骤先以深度 1 抓取被触发的提交，`source_sha` 不是它时再加深到 20 代，在运行检出里的任何文件之前，要求 `source_sha` 是该提交本身或其 20 代以内的祖先（`git merge-base --is-ancestor`，抓不到即拒绝），否则失败。因此被执行的代码处在某个由有写权限的人推送、由能派发的人选中的 ref 的历史里；只存在于 fork 的 PR ref 或别的分支上的提交会被拒绝。它挡不住有写权限的人把任意提交（包括第三方的）推进一个分支再派发。`shell-macos-candidate.yml` 与 `kp027-native-candidate.yml` 的 `source_sha` 仍可是任意可抓取的提交，且跑在同一台 Mac 的同一账号下（本 PR 未改，是否加同样的检查由 owner 定）。
  - 风险范围（读 workflow 文件得出）：落在这台 Mac 上的 job——`native-conformance.yml` 的 `native-darwin-arm64`（`release-macos-arm64`；`pull_request` 指向 dev、main、`intg/**`、`release/**`、`refactor/reconstruct-modules`，以及 push 到 dev：PR 的代码在这里运行）；`kp027-native-candidate.yml` 的 `native-darwin-arm64`（`release-macos-arm64`；push 到 `kp027/**`，以及手动派发）；`e2e-macos-credentialed.yml` 的 `controlled-real-env`、`e2e-macos-isolated.yml` 的 `isolated-macos`（`macos-host`；push `v*` tag、定时、手动）；`e2e-provider-calibration.yml` 的 `calibrate`（`macos-host`；push `v*` tag 且命中路径、手动）；`shell-macos-candidate.yml` 与 `shell-macos-signed.yml` 的 `package`（`release-macos-arm64`；只手动）。实测（planner，按 runner 列表与进程名）：`release-macos-arm64` 与 `macos-host` 是同一台机器、同一个 macOS 账号下的两个 runner 进程，共用登录钥匙串；所以以上每个 job 在钥匙串解锁期间都能使用签名身份与公证凭据。
  - Forgejo（owner 决定，2026-11-19）：实例只在内部使用，只有内部账号能开 PR 或推送，owner 据此接受目前的暴露；fork PR 的审批设置未查看（不是一个声称的值）。
  - 仓库公开之前必须满足的条件（本次不解决）：开发迁移计划（PR #2849，`docs/design/github-development-migration-2026-11-15.md`）的 D3 等决定没有提到 Developer ID 签名、登录钥匙串里的身份或公证 profile。今天的做法（身份放在同时为 PR 跑 CI job 的账号的登录钥匙串里，例如 `release-macos-arm64` 上的 `native-darwin-arm64`）只在每个能推送或开 PR 的账号都是内部账号时可接受；仓库公开后，一个由 PR 触发、跑在这台 Mac 上的 job 会让未经审查的外部代码使用签名身份与公证凭据，仓库里的检查防不住。公开之前必须二选一（不在此选定方案）：要么持有身份的机器 / 账号上不运行任何由 fork 或 PR 触发的 job（专用签名 runner 或专用 macOS 账号、只跑可信 ref、手动审批），要么身份离开 CI 账号。
  - 时间与取消：Forgejo runner 的 `runner.timeout` 与实例的 job 超时默认都是 3 小时，这台 runner 实际配置的值未知（问 owner）。因此 job 上限 180 分钟（job 准备 5、检出 15、打包 140、上传 15、尾部 5），公证等待每次 20 分钟（两次完整提交各按 20 分钟等待加 10 分钟上传上限、2 分钟等待余量、2 分钟取日志计算；run 65142（深度 100）与 65221（深度 1）的检出都在 5 分钟上限处被杀，当时主机负载 220–385，同一提交从主机经 ssh 以深度 1 抓取用了 46 秒、118 MiB；测试按文件里的数字核对总和）；等待更久则失败并记下提交 id。实测（2026-11-19 本机）：409 MB 的 app 上传超过 5 分钟，Apple 的 history 在第 1 秒就列出了该提交，而不带 `--wait` 的 `notarytool submit --output-format json` 在这 300 秒里什么也没打印（该提交因此用掉、id 未进证据，是改为轮询 history 的原因）。runner 取消或超时时对步骤的整个进程组发 SIGKILL（runner v13.0.0 源码 `act/common/cmdgroup.go`；版本号取自本仓库 job 日志的首行），任何处理函数都不会运行：`sign_bundle.py` 在每一步之后原子地重写证据 JSON；提交改为不带 `--wait` 的 `submit`，上传期间每 15 秒查一次 `notarytool history`，Apple 一列出新提交就记下 id 并写盘，然后 `wait`；打包脚本在输出目录一建立就写一份“未完成”的 `NOT-SIGNED.txt`，只在全部通过时删去。并发组改为固定的 `shell-macos-signed`，不与 candidate 共用（candidate 的 `cancel-in-progress: true` 可能在公证等待中取消签名运行）。
  - 冒烟落在最终产物上：`--verify-only --extract-to` 只读挂载最终 dmg，用 `ditto` 拷出 app，加上浏览器下载形式的 `com.apple.quarantine`，校验后卸载；两个冒烟都对这份拷贝运行；被挂载 dmg 的 SHA-256 必须等于收集到输出目录的那份（连同 manifest 与 SHA256SUMS，都要等于 `--verify-only` 核对过的值）。冒烟直接执行二进制，不经 LaunchServices：没有 app translocation，也没有首次打开的对话框，所以这不等于经 Finder 的首次启动（仍由 owner 验证）。
  - 静态库（`ar`、`*.a`）：保持拒绝（签名前失败）。理由：静态库运行时不加载，发出的 app 不需要它；codesign 无法签它里面的目标文件；Apple 公证服务是否反对 `.a` 里未签名的目标文件**未实测**（实测的 bundle 里没有）。若将来出现：实测公证是否原样接受（接受则放行并记录），或在 staging 时去掉该文件；两者都未实现，没有剪掉它们的决定。归档扫描也在不带开关的 `verify_bundle` 阶段运行，依赖一变普通构建就会失败。
- 决定再次反转（2026-11-20，owner）：打包完全离开 CI。上文 2026-11-19 的“只经手动 workflow `shell-macos-signed.yml` 签名”作废，ad-hoc 的 `shell-macos-candidate.yml` 也一并删除；没有任何 workflow 打包、签名或公证。macOS 包（ad-hoc 或 Developer ID）只由操作者在打包 Mac 上手动运行 `scripts/build/package-macos-shell.sh`（签名用 `--developer-id`，脚本与 `packaging/clients/sign_bundle.py`、`packaging/clients/signing/` 行为不变），步骤写在 `packaging/clients/README.md` “Package by hand”。这推翻了 GUI 线此前要求的“单独的签名 workflow”。
  - “CI 不签名”靠的是没有 workflow，而不是隔离：签名身份与公证 profile 仍在登录 keychain 里，runner 进程用的也是这个账号；keychain 解锁期间，这台 Mac 上任何 job（包括由 PR 触发的 `native-conformance.yml` 的 `native-darwin-arm64`）在技术上仍能使用它们。`tests/tooling/release/shell/signing/test_signing_only_by_switch.py` 改为断言没有 workflow 调用 `package-macos-shell.sh` 或 `sign_bundle.py`、传 `--developer-id`、签名或公证；它只是本仓库文件的回归检查，不是安全边界。
  - 源提交的选择改为操作者流程：已删除的签名 workflow 里的祖先检查（`source_sha` 必须是被派发提交或其祖先）不再存在，脚本只要求 `--source-sha` 等于 `HEAD` 且工作区干净，不检查它是否在主线上。操作者负责选择主线上的提交并在构建前自行核对（README 给出 `git merge-base --is-ancestor` 命令）；评审意见 A（任意 `source_sha`）由此转为流程，而非检查。
  - 源 remote：脚本仍不设置 `HYPRIAL_BUILD_SOURCE_REMOTE`；手动运行时由操作者把它设为 `PUBLIC_SOURCE_REMOTE` 的值（README 的命令里写明），否则 `BUILD-INFO.txt` 写“不可分发”。
  - 分发规则不变：仓库迁到 GitHub 之前打出的包只供内部验证；迁移之后，对外分发的包必须用 GitHub 上已存在的提交构建，构建本身不联网核对。
  - 仓库公开之前的条件不变（开发迁移计划 PR #2849）：身份所在账号上不得运行由 fork 或 PR 触发的 job，或者身份离开这个账号；删除打包 workflow 不改变这一点。重启后 keychain 如何解锁仍未决（手动运行时由操作者在已登录会话里处理）。
  - 公证等待（2026-11-20，PR #2980 复评第 4 条）：上文每次 20 分钟的等待是按已删除签名 workflow 的 job 时间预算算的，手动运行没有这个预算；README 的命令不再传 `--notary-timeout-minutes`，用 `sign_bundle.py` 的默认 30 分钟（实测出现过 62 分钟的等待，需要时用该选项加长）。超时的运行在 `NOT-SIGNED.txt` 与 `sign-bundle.json` 里留下提交 id：不重新提交，稍后用 `notarytool info` 查看结果。
  - 钥匙串与 job token（owner 裁定 2026-11-20，GUI 线在 PR #2980 评论中转记）：签名身份与公证 profile 不必放入单独的钥匙串，维持现状，风险按本条已登记的条件接受（仓库公开之前仍须按上文处理）；“Mac job 只读 job token”的检查不扩展到 `macos-host`，`test_signing_only_by_switch.py` 文件头写明的缺口保留。

## KP-101 同一进程内每次登出多留一个 `shutdown-stall.log` 句柄

- 现象：daemon 在同一进程里从 serving 回到 bootstrap（登出）时，`daemon/impl/processes/shutdown_stall.py` 的 `register_stall_signal` 为卡死转储再打开一次 `<state>/shutdown-stall.log` 并交给 `faulthandler.register`，之前那次打开的句柄不关闭。实测（2026-11-17，Linux，读 `/proc/<pid>/fd`）：bootstrap 阶段的打开文件描述符数随登出次数为 16、17、18；六轮为 16 到 21。线程数每轮都是 6。
- 范围：ownerless 启动的同进程循环（Z-2）已有此行为；Z-6pre（#2927）让 owned 启动的 daemon 在登出后也留在同一进程，因此也会走到。未在未改动的 `dev` 检出上单独复测 ownerless 路径。
- 影响：每次登出泄漏 1 个文件描述符。常规使用下登出很少，离进程的描述符上限很远；长期运行且反复登出的进程会缓慢累积。不影响正确性，不泄漏数据。
- 规避：重启 daemon 即释放。
- 决定：登记，不在 Z-6pre 中修。修法须保持 `faulthandler` 信号处理的异步信号安全（该文件的决策记录说明了为何句柄要长期持有），应在重新注册前注销并关闭上一次的句柄，或让句柄在进程内只打开一次；需要带描述符计数的多轮回归测试。
- 依据：Z-6pre 独立验证与实施者的 `/proc` 读数（PR #2927 正文）。
- 负责人：daemon-first 线。

## KP-102 GUI 打开时 daemon 被停掉：各情形下子进程与 GUI 的行为

- 现象（按情形，来自源码 `bootstrap/gui.py` 的 `serve_gui`/`reopen` 与 `gui_host.py`，其中第 2 条另有真实进程测试）：`serve_gui` 只在 `daemon_changed()` 为真（静默的 daemon 不算变化）或 serving 时 owner 消失时离开监听循环。
  1. 终端执行 `hyprial logout`（进程内停掉 daemon、再移除 owner，不经 `auth.logout`）：子进程看到 owner 消失，进入重开，在 `shutdownDrainSeconds`（默认 5 秒）内等不到 daemon，以 `BACKEND_UNAVAILABLE`（退出码 3，stderr 一行 JSON）退出，启动器随即以 `GUI_BACKEND_EXITED` 结束 GUI。Z-6b 之前子进程会自己托管 bootstrap，GUI 回到登录页。
  2. `hyprial daemon stop`（owner 仍在）：子进程**不退出**，GUI 保持打开。业务请求被拒：真实进程测试 `tests/integration/gui_daemon_first/test_child_attaches.py::test_a_daemon_stop_leaves_a_serving_child_open_and_it_recovers_when_a_daemon_returns` 读到 HTTP 错误 `CATALOG_STALE`（`dispatch: not-dispatched`，消息 “daemon contract changed; restart the request-http process”），不是 `DAEMON_UNAVAILABLE`。经 HTTP 的 `ping` 以及 `agent.list`、`onboarding.status` 同样读到 503 `CATALOG_STALE`（2026-11-17 独立验证的真实进程探针，未入库）：宿主内部的 `_serving_ping` 会回落到本地的 “serving” 投影，但本地回落目录的摘要与启动时不同，请求在 `_apply_view` 被拒，用户看不到这个回落。错误消息里的 “restart the request-http process” 在这里是错误的建议：daemon 回来后子进程自行恢复，不需要重启。
  3. daemon 崩溃：与第 2 条相同（子进程不看 pid，只看 ping 与 owner）；只有崩溃发生在重开窗口内才像第 1 条那样以 `BACKEND_UNAVAILABLE` 结束。转发 bootstrap 的子进程同样不退出，daemon 静默不算变化；它的请求读到的是 `DAEMON_UNAVAILABLE`，不是 `CATALOG_STALE`（同一次真实进程探针，未入库）。
  4. 之后又有 daemon 回来：同一 owner 的 serving daemon 回来后，同一个子进程与同一个 bearer 的请求恢复成功（上述测试在 daemon 重新 serving 后读到 `daemon.status` 成功，子进程没有重启）；bootstrap 转发子进程在新 daemon 的 epoch 与旧的不同时会重新发布（`daemon_changed`），该路径由 `test_gui_forwarding_loop.py` 的假宿主测试覆盖；独立验证的真实进程探针见到它成立（同一个子进程、新 token、`phase: bootstrap`），但仓库里没有固定它的真实进程测试。
- 既有行为：第 2、3、4 条在 Z-6b 之前就是这样（第 2、4 条的真实进程测试在 Z-6b 之前的源码 `432ffb5653` 上同样通过；其余为源码对照）：循环的离开条件、serving 的 ping 回落与逐请求转发都没有改动。Z-6b 改变的只有第 1 条（以及重开窗口内 daemon 消失）：原先托管，现在以 `BACKEND_UNAVAILABLE` 结束。
- 影响：第 1 条只在 GUI 打开期间用 CLI 登出时出现；GUI 内的登出走 daemon 的 `auth.logout`，daemon 在同一进程回到 bootstrap（Z-2、Z-6pre），不受影响。第 2 条 GUI 停留在不可用的页面，直到 daemon 回来；页面在 `CATALOG_STALE` 之后显示什么未在浏览器中验证（UNKNOWN）。daemon 仍活着但没在预算内回来曾是 `GUI_LOGOUT_DRAIN_TIMEOUT`（Z-6c 修复候选已删除该错误码，见下方修复候选）。
- 规避：第 1 条再次运行 `hyprial gui`（启动器会先确保 daemon）；第 2 条 `hyprial daemon start`（或重新运行 `hyprial gui`）。要在 GUI 打开时登出，用 GUI 内的登出。
- 决定：登记；修复是让 CLI `logout` 经 `auth.logout` 由 daemon 自己完成（daemon 先行方案 §4.3，目前不在任何 PR 行里），尚未排期。Z-6b 按其计划 Q2 的推荐答案接受第 1 条的行为；第 2 条的“停着但请求失败”是既有行为，不在本条修。
- 依据：[daemon 先行方案](../design/gui-single-launch-plans-2026-11-15/daemon-first.md) §4.3 与 Z-6b 一行（Z-6b 计划 Q2）；`src/hyprial/shell/impl/cli/commands/login/commands.py` 的进程内 logout；`tests/shell/request_http/bootstrap/test_gui_forwarding_loop.py::test_a_child_whose_daemon_is_gone_stays_until_stopped_and_never_hosts`。macOS 真实运行未验证（UNKNOWN）。
- 负责人：daemon-first 线。
- 修复候选：CLI `logout` 改走 `auth.logout`（尚无 PR）；第 2 条的 `CATALOG_STALE` 消息文案应另行更正（尚无 PR）。
- 修复候选（Z-6c，2026-11-18，分支 `feat/z6c-rebind-in-place`，PR #2946，未经 CI）：第 1 条在子进程一侧不再出现（真实进程测试里 CLI logout 之后子进程保持打开，daemon 以 bootstrap 回来后原地换主体；但 CLI `logout` 自身要在 `DAEMON_STOP_SURVIVOR` 修复之后才能完成，见下一条）：`reopen` 等 daemon 不设期限，`GUI_LOGOUT_DRAIN_TIMEOUT` 与重开期限处的 `BACKEND_UNAVAILABLE` 退出已删除，终端 `hyprial logout` 之后子进程保持打开、请求答 503 `DAEMON_UNAVAILABLE`，daemon 以 bootstrap 回来后原地换主体（同一 endpoint 与进程，新 bearer 与 generation）；真实进程测试 `tests/integration/gui_principal_change/test_daemon_absent.py`。第 2、3 条：serving 宿主不再回落到本地投影，daemon 静默时答 `DAEMON_UNAVAILABLE`，`CATALOG_STALE` 只剩真正的契约漂移，其消息不再建议重启进程；第 4 条由同一文件里的真实进程测试固定（同一 daemon 的 serving 重启沿用 bearer，bootstrap 转发子进程在新 epoch 下换 bearer 且 endpoint 不变）。CLI `logout` 仍不经 `auth.logout`。页面在 `DAEMON_UNAVAILABLE` 时显示什么、macOS 与原生壳均未实测（UNKNOWN）。
- 同一修复候选里的另一处缺陷（2026-11-18 验证中发现）：`hyprial gui` 以 `start_new_session` 启动 daemon，而启动它的长寿命进程（启动器）从不 wait 这个子进程，所以 GUI 打开期间 `hyprial logout --yes` 等到 daemon 退出后，CLI 的 `_stopped_process` 仍把这个已退出、未被回收的僵尸进程当成存活，最终以 `DAEMON_STOP_SURVIVOR` 失败（真实启动器路径上复现：`tests/integration/gui_principal_change/test_launcher_reaps_daemon.py`）。修复候选（Z-6c）在源头：`common/daemon_start.py` 在创建 Popen 的地方起一个 daemon 线程阻塞在 `Popen.wait()`，daemon 退出即被回收；线程不发信号、不停 daemon（“关闭 GUI 不停 daemon”不变）、不阻止启动器退出，短寿命 CLI 父进程照旧退出后由 init 回收，与平台无关。单元测试 `tests/shell/cli/daemon/lifecycle/test_spawner_reaps.py`。未修复的盲区：`_stopped_process` 对“由某个**其他**长寿命父进程启动、已退出但未被回收”的 daemon 在所有平台上仍判为存活（Linux 之外内核的 `process_exited_unreaped` 也不可用）。启动器之外的 harness 测试里的 daemon 由测试 harness 启动，harness 在线程里回收它。
- 2026-11-19 更新（#2822，宿主进程内直接服务 daemon 驱动）：daemon 驱动不再有 request-http 子进程，第 1 条里的 `BACKEND_UNAVAILABLE` 子进程退出与启动器的 `GUI_BACKEND_EXITED` 不再适用于该驱动；daemon 的主体监视（watcher）丢失会使 GUI 宿主以退出码 3 结束，启动器以同一状态退出，并在 stderr 解释一次（`gui-host-exited`，含已脱敏的宿主日志尾部），stdout 不增加行。上面的旧文字保留，描述 #2822 之前的行为。

- 状态变化（2026-11-19）：#2946（Z-6c）已合入 dev。上文“现象”一节描述的是 Z-6b 之后、Z-6c 之前的行为，现状如下：第 1 条（终端 `hyprial logout`）子进程保持打开，请求答 503 `DAEMON_UNAVAILABLE`，没有 `GUI_LOGOUT_DRAIN_TIMEOUT`，重开阶段不再有等待期限（`bootstrap/gui.py` 文件头：`reopen` 等 daemon 不设期限）；第 2、3 条答 `DAEMON_UNAVAILABLE`，`CATALOG_STALE` 只剩真正的契约漂移；第 4 条 daemon 回来后同一子进程恢复，同一 daemon 的 serving 重启沿用 bearer，bootstrap 转发子进程在新 epoch 下换 bearer 且 endpoint 不变；启动器启动的 daemon 在退出后由启动器进程回收（`common/daemon_start.py`）。仍未做：CLI `logout` 经 `auth.logout` 由 daemon 自己完成；`_stopped_process` 对由其他长寿命父进程启动、已退出未回收的 daemon 仍判为存活。未验证（UNKNOWN）：页面在 `DAEMON_UNAVAILABLE` 时的显示、macOS 与原生壳。
- 2026-11-19 更新（#2822 步骤 1–4，#2985、#2995、#2996 已合入）：daemon driver 下不再有独立的 `request_http` 子进程，上述“子进程”的行为（保持打开、答 `DAEMON_UNAVAILABLE`、原地恢复）由会话 host 进程承担，GUI 进程在 daemon 消失期间同样保持打开、请求答 503 `DAEMON_UNAVAILABLE`；上面各条里“子进程”“endpoint”的说法描述 #2822 之前的行为，不改写。
- 2026-11-20 状态变化：`serve_gui` 已删除，`python -m hyprial.shell.impl.request_http serve` 拒绝 `local-gui` 配置（`CONFIG_INVALID`，退出码 2），上文“现象”里的 `serve_gui`/`reopen` 描述的前台 GUI serve 不再存在，该行为只由会话 host 承担。“依据”里引用的测试 `test_a_child_whose_daemon_is_gone_stays_until_stopped_and_never_hosts` 已改名为 `test_a_host_whose_daemon_is_gone_stays_until_stopped_and_never_hosts`（`tests/shell/request_http/bootstrap/test_gui_forwarding_loop.py`）。
- 2026-11-21 状态变化（L1，分支 `refactor/logout-via-auth-logout`，待 CI/合入）：终端 `hyprial logout` 不再在进程内停 daemon、取身份锁或删凭据，只向 daemon 发一次 `auth.logout`（没有 daemon 时先启动），与 GUI 内的登出走同一路径：serving daemon 删除登录产物后在同一进程回到 bootstrap，GUI 原地换主体。上文“决定”与“修复候选”里“CLI `logout` 改走 `auth.logout`（尚无 PR）”即此改动；第 1 条只在登出后仍有 owner 解析（`HYPRIAL_OWNER`）、daemon 因而退出时出现，行为同第 2 条。真实进程测试：`tests/integration/daemon_lifecycle/test_same_pid_lifecycle.py::test_the_cli_logout_is_one_daemon_request_and_the_process_returns_to_bootstrap`（dev 源码上以 `DAEMON_STOP_SURVIVOR` 失败，本分支通过）。浏览器、macOS 与原生壳未实测（UNKNOWN）。同一分支随后（cm-l1-fix）改写了两条仍假定 CLI logout 让 daemon 退出的真实进程测试：`gui_principal_change/test_daemon_absent.py::test_a_cli_logout_with_the_gui_open_leaves_the_host_running` 改为验证 GUI 打开时 CLI 登出后同一 daemon 进程回到 bootstrap、宿主（同 pid 与出生标识）原地换成 `anonymous-bootstrap` 主体、旧请求头答 401，并在旧的重开期限之后仍在；`daemon stop` 造成的缺席与恢复仍由同文件前两条固定。`test_launcher_reaps_daemon.py` 改名为 `test_a_cli_logout_keeps_the_launcher_daemon_and_its_stop_is_reaped`：CLI 登出后 daemon 仍是启动器的子进程、同 pid 回到 bootstrap，随后的 `hyprial daemon stop` 退出由启动器回收、不留僵尸（`daemon stop` 用 `require_exit=False`，不再出现 `DAEMON_STOP_SURVIVOR`，进程仍在时报告 `survivingPid`）。同时修正 `daemon.stopRequested` 只认 ping phase `serving`、对已完成首轮 reconcile 的 serving daemon（phase `reconciled`）误报 `false` 的缺陷。
- 2026-11-20 macOS 实测（ad-hoc 包，dev `4297ab4e49`，结果贴在 #2980 评论）：单一宿主进程下停掉 daemon，GUI 不退出，用 CLI 启动的宿主经 HTTP 读到 503 `DAEMON_UNAVAILABLE`，daemon 回来后原地可用；没有再读到 `CATALOG_STALE`，上文“现象”第 2 条的 `CATALOG_STALE` 是改造前的行为。未测：app 页面内此时显示什么（BLOCKED，未能在 app 内读到业务错误）、Finder 首次打开（owner 定现阶段不探查）。

## KP-103 GUI 聊天页对 `recordAvailable: false` 没有界面提示

- 现象：`conversations.list` 与 `conversations.history` 的应答带 `recordAvailable: false` 时（OrgFS 未就绪或读取失败、没有 recorder 组合），daemon 只按本机投递队列作答。GUI 聊天页（`gui/src/features/chat-channels/impl/connected-chat.ts`）照常显示这份历史，不显示任何标记。
- 影响：用户看不出这次看到的历史只来自本机队列：别的设备记录、而本机队列没有的消息不会出现，已从本机队列清走的消息也不会出现。页面本身可用，不报错、不阻塞。
- 规避：无界面规避；需要时在 daemon 侧确认会话记录是否可读。
- 决定：不修（第一片接受）。以后按现有状态约定加一个不惊扰的提示；新增文案须过不可用文案账本规则（账本只减不增）。
- 依据：PR #2936 的 GUI 线评审（2026-11-18，“`recordAvailable: false` 没有界面提示：第一片可以接受，请登记”）；交接记录 `gui/docs/business-lines/chat-channels-conversation-document-handoff.zh-CN.md` 第 7.3 节。
- 负责人：GUI 线与产品。

## KP-105 没有取得新绑定证明的办法，也不会再有；绑定代码已删除

- 现象：`hyprial login` 与 `hyprial org join` 不再发起第二次设备确认，因此不再取得 `settings.identityBinding`（绑定证明）；原计划的显式确认步骤（`hyprial user bindings confirm`，即“PR B”）已于 2026-11-18 取消，以后也不会有。owner 的模型是：hyprial 用户不绑定 IM 身份，与 IM 相关的是 adapter。R1（PR #2970）让入站发送人只呈现 adapter 给出的平台事实，登录不再写 `settings.identityClaims`；R2（PR #2989）删除了按 owner 的 open_id 私信的两条出站路径与 `state/users.json` 的 `ownerOpenId`；R3 删除了其余部分：`IdentityResolver`、本地覆盖库（`state/identity.sqlite3`）与用户库的账号表 API（`state/users.sqlite3` `user_accounts`）、证明的发布与核验（OrgFS `directory/people/<slug>/binding.json` 的发布、撤回、加入后发布与发布时间门，`state/verified-org-bindings.json`、`state/identity-binding-suppression.json`）、方法 `identity.resolve`、`identity.override.set|clear`、`identity.bindings.list|audit`、`identity.binding.publish|withdraw`、CLI `hyprial user bind|unbind|whois|bindings …` 与 `user list --source`，以及 `adapter.identities.list` 的 `hyprialOwner`。只有名字的人员登记（`hyprial user add|show|list`、`identity.users.add|list`）与 `identity.whoami` 保留。
- 影响：新版本不读、不写上面列出的文件与字段；它们可以手工删除（迁移说明第 9 节）。旧版本的组织成员仍按已发布的绑定行识别该用户的飞书账号；新版本没有撤回命令。GUI 的「配置绑定」「查询绑定」与身份绑定列调用已删除的方法，R3 合入之前须由 GUI 线删除（清单见 R3 的 PR）。
- 规避：想撤回已发布的绑定行，须在**升级之前**、在旧版本上运行 `hyprial user bindings withdraw --all`；见 `docs/guides/migration-im-push-by-agent-binding.md` 第 10 节。
- 决定：已关闭（R3）。GUI 两个面板的删除是 GUI 线的后续工作。
- 依据：owner 2026-11-18、2026-11-19 的裁定；计划 [login-without-adapter-binding](../design/login-without-adapter-binding-2026-11-18.md)；tier 2 设计 PR #2950；PR #2970、#2989 与 R3。
- 负责人：会话线（R3，已完成）；GUI 线（两个面板）。

## KP-106 用户 agent 的名字形式判断只看地址文本

- 现象：`user_agent.naming.is_user_agent_address` 对名字形式的地址只比较最后一段是否为保留名 `hyprial-user`，所以任何 `agent:<任意 owner>:<任意节点>:hyprial-user` 都被认作某台机器上的用户 agent，不论这条记录是否存在。自 UA4 起它只认名字形式：id 形式的地址按普通 agent 地址处理，本机的用户 agent 由注册表标记识别。
- 影响：匹配上的地址只会被隐藏（listings、targets）、被拒绝派工（`DISPATCH_ROLE_MISMATCH`）和管理（`USER_AGENT_IMMUTABLE`），不授予任何权限；对端可以借此让一个名为 `hyprial-user` 的地址被拒绝，但做不了别的。本机上普通 agent 不能取这个名字（所有公开创建与改名入口都拒绝）。
- 规避：无需规避。
- 决定：不修（UA1c 评审决定只登记）。
- 依据：PR #2941；`src/hyprial/daemon/impl/user_agent/naming.py` 文件头决策块。
- 负责人：用户 agent（UA）线。

## KP-107 旧版本节点发来的带 role 的消息在入口不被拦截

- 现象：不认识用户 agent 的旧版本对端可以向某台机器的用户 agent 发送带 role 的消息；本节点的入口不按用户 agent 拦截，只有本节点自己的派发（`dispatch_gate`）与工作准入（routine、workflow、远端借用）拒绝。
- 影响：混合版本期间，旧节点发出的带 role 消息可能进入用户 agent 的收件队列；用户 agent 没有 harness，不会执行它。
- 规避：升级所有节点。
- 决定：不修（UA1c 评审决定只登记）。
- 依据：PR #2941；`src/hyprial/daemon/impl/user_agent/naming.py` 文件头决策块。
- 负责人：用户 agent（UA）线。

## KP-108 owner 切换与重新定键之间退出时，旧用户 agent 的冻结名字本 PR 不认领（已关闭）

- 现象：owner 切换在一次启动中先提交 owner 改写（`apply_plan`），再单独提交用户 agent 的重新定键。现在每次启动都会重跑幂等的重新定键，所以活着的用户 agent 记录与其 home 回执镜像在下一次启动时会被修好。但如果旧 owner 的用户 agent 记录此前已被内部路径销毁、只在名字账本里留下按旧派生 id 冻结的一行，而 daemon 恰好死在两次提交之间，下一次启动已看不到旧 owner（账本行的 owner 列也已被改写），无法区分这一行属于旧用户 agent 还是曾用过这个名字的普通 agent。
- 影响：该机器上名字 `hyprial-user` 保持冻结，ensure 回答 `name-frozen`，`ps` 的 `userAgent` 块与 `hyprial doctor` 显示该状态；这台机器上没有用户 agent。
- 规避：本 PR 内无安全的自动规避；需要人工确认后清理账本行。
- 决定：本 PR 未修（需要记录被销毁 + owner 切换 + 恰在窗口内崩溃三者同时发生）。这个窗口可以关掉，两种做法任选其一：(1) 把账本行的重新定键放进 owner 改写的 plan（`build_plan`/`apply_plan`），与 owner 改写同一次提交；(2) 在 `apply_plan` 之前把旧 owner 持久化，下一次启动据此认出旧派生 id 的账本行。
- 依据：PR #2941 评审第 2 项与第二轮复核；`src/hyprial/daemon/impl/owner_migration/user_agent.py` 文件头决策块（UA4 已删除该模块）。
- 关闭（UA4，2026-11-19 owner 决定：每台机器上的用户 agent 是独立 agent）：用户 agent 的 id 由注册表像其他 agent 一样随机生成，不再由 owner 派生；owner 切换只由通用 owner 改写移动地址里的 owner 段，id 不变，重新定键步骤（`owner_migration/user_agent.py`）已删除，所以两次提交之间的窗口不再存在。NF1（2026-11-19 owner 决定：不再冻结已销毁 agent 的名字）删除了名字账本与 `name-frozen` 状态：用户 agent 若被内部路径以正向销毁删除（所有公开入口都拒绝销毁它），下一次 ensure 以新 id 重建它。
- 负责人：用户 agent（UA）线。

## KP-109 大小写不敏感的文件系统上 `Hyprial-User` 与用户 agent 撞 home

- 现象：保留名检查按仓库惯例精确比较（`bootstrap/names.py` 与注册表的保留内部名都是精确比较），所以 `Hyprial-User` 可以作为普通 agent 名创建。在大小写不敏感的文件系统（macOS 默认）上，两者的 home 目录同名：用户 agent 已存在时，创建 `Hyprial-User` 被 home 预留拒绝（`AGENT_ERROR`，`name-collision`）；反过来，普通 agent `Hyprial-User` 先存在时，用户 agent 的创建以同样的错误失败，ensure 把它当作可重试的 `deferred`，6 次后状态为 `unavailable`。大小写敏感的文件系统上两者互不影响。
- 影响：极少见；只有用户自己取了这个仅大小写不同的名字才会出现。
- 规避：销毁该普通 agent（`hyprial agent destroy Hyprial-User`），然后重启 daemon；改名无效，`agent.rename` 只改显示名，actor 键与 home 目录不变。
- 决定：不修。
- 依据：PR #2941 评审（不阻塞项“保留名区分大小写”）；2026-11-18 在 macOS（APFS，大小写不敏感）上对两个方向各做一次实测：用户 agent 先在时创建 `Hyprial-User` 得到 `AGENT_ERROR`（home `name-collision`）；`Hyprial-User` 先在时 ensure 得到 `deferred`（同一错误），销毁它之后 ensure 得到 `created`。
- 负责人：用户 agent（UA）线。

## KP-110 observe 模式下无 `sessionRef` 的本机调用方可消费用户 agent 的收件箱并以它发送

- 现象：发送人认证的默认 observe 模式下，本机 IPC 调用方指名某个 actor 而不带 `sessionRef`，就能列出该 actor 的待收消息，并以该 actor 的身份发送。这对每个 agent 都成立，并非本 PR 引入。`fetched=true` 仍然需要会话。enforce 模式下以 `SENDER_NOT_AUTHENTICATED` 拒绝。
- 影响：对 owner 的用户 agent 更要紧：它没有 harness，永远不可能持有会话，所以没有围栏的调用方是它收件箱唯一可能的消费者；它的名字（`hyprial-user`）在每台机器上固定且可预测。自 UA4 起它的 id 由注册表随机生成，不再可预测（此前由 owner 派生、每台机器相同）；名字仍可预测，所以暴露面不变。当前限制暴露面的事实：本分支不把任何消息路由到用户 agent（发送路径未改动），所以它的收件箱目前不会收到 owner 的会话消息。
- 规避：启用发送人认证的 enforce 模式。
- 决定：待修。在 owner 的会话被路由到用户 agent 自己的地址之前必须关闭（enforce 模式或等价收紧）。
- 依据：PR #2941 评审（owner 2026-11-18 批准登记）。
- 负责人：实现把 owner 的会话路由到用户 agent 的那一片的实现者。

## KP-111 GUI 以只读回执观察 daemon 登录

- 现象：当前 GUI 在 `auth.login.begin` 或 `auth.relogin` 中声明 `until: "serving"`，daemon 负责发行方轮询、身份提交、设备恢复和 initialize。浏览器端 `connected-account-actions.ts` 只按回执读取 `auth.operation.get`，必要时读取 `auth.logout` 的结果，不提交设备或 initialize 步骤。
- 影响：当前 daemon 驱动操作在 GUI 关闭后仍可完成，页面只会失去实时展示并在重新打开后读取 durable receipt。
- 规避：无。若页面在提交后关闭，重新打开 GUI 并读取账号状态或操作回执。
- 决定：已修复。L2 将登录与重新登录的完整 serving 流程收回 daemon，浏览器保留只读回执观察。真实 issuer、真实 Tailcat、macOS 与浏览器全旅程仍为 UNKNOWN。
- 依据：当前 head `29f6830e51dae304b7cab7d2798cb1154628fc4f` 的 `connected-account-actions.ts`、`ServingReloginDriver` 与 `tests/daemon/onboarding/relogin/`；历史定位保留旧 head `564337f810`。
- 负责人：daemon onboarding 与 GUI account 维护者。

## KP-112 同一 owner 的重新登录由 daemon 驱动（L3 已关闭 `--switch-account` 与 `owner_migration`）

- 现象：L2 将同一 owner 的重新登录收口为 serving daemon 的 `auth.relogin`（`until: "serving"`）；L3 进一步确立「单 `HYPRIAL_HOME` 永久绑定单 uid（`settings.homeAccount`）」，删除 `--switch-account`、`--dry-run` 与 `owner_migration/`（含 `migrate_owner_if_needed`、CLI 进程内登录管线与三个 `OWNER_MIGRATION_*` 错误码）。
- 影响：同一 `(issuer, userId)` 下即便上游 `preferred_username` 改名，也保留绑定 `owner` 并仅更新 `settings.username` 与 GUI 显示名；不同 uid 且存在遗留业务数据时在写入前以 `IDENTITY_CONFLICT` 拒绝，换账号需使用独立 `HYPRIAL_HOME` 或按 `docs/guides/migration-account-binding.md` 离线清理。
- 规避：无需 `--switch-account` / `--dry-run`。同 uid 复登直接运行 `hyprial login`；跨账号隔离使用独立 `HYPRIAL_HOME`。
- 决定：已关闭。L2 收回 daemon `auth.relogin`，L3 落盘单 home 绑定与清理 closure。真实 issuer、真实 Tailcat、macOS 与原生壳全旅程仍为 UNKNOWN/BLOCKED。
- 依据：`src/hyprial/identity/impl/onboarding/home_binding.py`、`src/hyprial/identity/impl/onboarding/credentials/flow.py`、`docs/guides/migration-account-binding.md` 与 `tests/identity/onboarding/home_binding/`、`tests/integration/home_binding/`。
- 负责人：登录 / 身份维护者与 daemon onboarding 维护者。

## KP-113 pin 了 adapter 的 agent 发给用户的消息不推送到 IM

- 状态（2026-11-23，I3）：已关闭。发送方的 cell 有 adapter 角色成员时，daemon 把发给 `user:<owner>` 的副本推到该 cell 写明的聊天（发出成员在保留标签 `user` 上的连接；cell 里没有任何 `user` 连接时推到每个 adapter 成员所在网关的 `defaultRoute`），结果 `pushed: true`、`via: "cell"`；routine / workflow 报告同样推送。这份副本是尽力而为：发送时内联尝试一次，不因推送失败让已入队的发送失败，也不重试（owner A-7）。测试改为 `tests/daemon/conversations/user_target/push/test_daemon_notices.py::test_a_report_from_an_adapter_bound_agent_is_pushed_to_its_chat` 与 `test_cell_push.py`。I7b-2（2026-11-24）已删除 user-proxy，下文「规避」中的 user-proxy 交接不再存在。下文是登记时的原记录。

- 现象：本机 agent 发给 `user:<owner>` 的消息入队并记入对话；只有发送方没有 pin 任何 adapter、且本机恰好有一个 user-proxy 时，才另交 user-proxy 推一份到 IM。pin 了 adapter 的 agent 一律不推：结果为 `pushed: false`、`pushCode: TARGET_UNRESOLVED`，日志 `send.user_push` 带 `pushSkipped: sender-pinned`；有 user-proxy 也一样。由这类 agent 代发的 routine / workflow 报告同样不推。
- 影响：依赖在飞书里收到这类 agent 主动消息的用户，只能在 GUI、CLI 的对话里看到。
- 规避：在 GUI 或 CLI 里看；或从该 agent 的 cell 去掉 adapter 成员（`hyprial agent cell apply`；I6-4 起 `adapter unpin` 已退役，绑定只写在 cell 文档里）。
- 决定：接受（owner 2026-11-19 两次确认：「先删除并接受空档」「维持原决定，接受这个更大的空档」）。「推到 agent 绑定的聊天」由 cell 模型的切片（#2712）实现，届时结束。
- 依据：PR #2989；测试 `test_a_report_from_an_adapter_pinned_agent_lands_in_the_accepted_gap`；迁移说明 `docs/guides/migration-im-push-by-agent-binding.md` 第 3 节。
- 负责人：cell 模型线（#2712）。

## KP-114 daemon 通知只写日志，`hyprial log` 没有按事件名过滤

- 现象：daemon 不再向用户发送任何东西（owner 2026-11-19）。配额与收件箱看门狗、运维告警、OrgFS 通知、agent 被阻塞、模型提供方需要重新登录、其他 owner 告警，以及没有本机 agent 可代发的报告，都只写一条 `warn` 级、component `daemon` 的日志（事件名 `quota_watchdog.notice`、`inbox_watchdog.notice`、`daemon.operator_alarm.notice`、`orgfs.owner_notice`、`agent.blocked.notice`、`provider_auth.notice`、`owner.notice`、`workflow.user_report.unsent`），带全文 `text`。升级失败告警与重启前通知已删除。`hyprial log` 只能按 component、name、level、actor、conversation、时间与 correlation id 过滤，没有 `--event`；GUI 日志页（`log.page`）的事件 allowlist 里也没有这些事件名（`request_catalog/maintenance/logs.py`）。
- 影响：不主动查日志就不会知道这些通知；查时要在 JSON 输出里按 `event` 字段自己筛。
- 规避：`hyprial log --component daemon --level warn --since <RFC 3339 时间> --json`，再按 `event` 筛。
- 决定：待做（`hyprial log --event` 过滤与 GUI 日志页的 allowlist 条目列为后续；IMAP 等日志转发渠道 owner 说「现在不做」）。
- 依据：owner 2026-11-19 的两条裁定；PR #2989；迁移说明第 5、6 节。
- 负责人：daemon 维护者（日志查询）。

## KP-115 根 `gui/` 不在结构门的扫描范围内

- 现象：`scripts/ci/check_structural_limits.py` 只扫描产品源码根 `src/hyprial/`、`tests/` 以及活动 `scripts/`、`contract/`、`sidecar/`；根 `gui/` 不是它的产品根（Z-7 A3 移除已不存在的旧 GUI 根时在文件头写明）。按同一口径手工统计（2026-11-20，不含 `node_modules` 与构建产物）：超过 1000 行的代码文件 4 个（生成的 `gui/src/client/generated/types.ts`、`gui/tests/daemon-request-host/browser-acceptance.mjs`、`gui/src/styles/app.css`、`gui/src/styles/primitives.css`）；126 个含代码目录中直接文件数超过 7 的有 23 个（最多的是 `gui/tests`、`gui/tests/modules/dev04`、`gui/src/features/onboarding/impl`、`gui/src/dev`、`gui/tests/support`）。
- 影响：AGENTS.md「每个含代码目录最多 7 个直接文件、每个代码文件最多 1000 行」对 `gui/` 没有 CI 约束，新增超限不会被拦截；GUI 模块边界（跨边界只导入各自 `index.mjs`）由 GUI 自己的检查负责，不受本条影响。
- 规避：评审 `gui/` 改动时人工看新增文件的行数与目录文件数；不新增超限。
- 决定：接受（owner 2026-11-20：登记为已知问题）。本条不改结构门，也不为现有超限登记例外；把 `gui/` 纳入结构门（含生成文件与样式文件的口径）需另行裁定。
- 依据：`scripts/ci/check_structural_limits.py` 文件头；上述手工统计。
- 负责人：GUI 维护者。

## KP-116 未迁移的旧版 adapter 固定按名字落到同名新 agent 上

- 状态（2026-11-23，I6-4）：已消失。adapter 绑定只写在 agent 的 cell 文档里，创建 agent 不再按名字迁移旧版 `channel_pins`，暂存项保留但不读作路由，也不绑定到同名新 agent；`adapter.pin|pins|unpin` 已退役。下文是登记时的原记录。测试改为 `test_kp116_legacy_channel_pin_never_binds_a_new_same_name_agent`。

- 现象：旧版 desired-state 的 `channel_pins` 表（`channel` → `agent`，值是保存时的拼写：裸名字或名字形态 URI）在 adapter 未配置或目标 agent 不存在时保留不迁移（`desired_state/documents.py`、`application/actors/sessions/registry.py` 的 `_migrate_legacy_channel_pins`）。每次创建 agent 都会重跑这次迁移（`agents_admin/registry_ops.py` 创建路径），它用 `agents.get(value)` 按名字解析目标，并把 adapter 固定到此时持有该名字的 agent。agent 销毁不清理这张表。
- 影响：NF1 删除名字账本后，已销毁 agent 的名字可以再用。若某个旧版固定在原 agent 存在期间一直没能迁移（例如当时 adapter 未配置），原 agent 被销毁、之后又用同一名字新建 agent，这个固定会落到新 agent 上。只影响升级前留下、至今未迁移的旧数据；当前的 `adapter.pin` 写注册表的 `pins` 表（按 actor 键，随 agent 删除级联），不会产生这种行。
- 规避：新建同名 agent 前用 `hyprial adapter unpin <adapter>` 去掉未迁移的旧固定（`adapter.unpin` 同时清理旧版暂存项）。
- 决定：不修（协调者 2026-11-19）：旧版 `channel_pins` 数据与其迁移由另一条线删除，那时本条随之消失。当前行为由 `tests/daemon/application/restore/test_daemon_desired_state.py::test_kp116_legacy_channel_pin_resolves_by_name_to_a_new_same_name_agent` 固定。
- 依据：NF1 按名字继承检查（`desired_state/sqlite.py` 的 `channel_pins` 表；`_migrate_legacy_channel_pins` 按名字解析）。
- 负责人：删除旧版 `channel_pins` 的那条线。
- 状态更新（F1b，2026-11-23）：已关闭。I6-4 起 adapter 绑定只在 agent cell 文档里，创建 agent 不再解析旧固定；F1b 删除了 `channel_pins` 的全部读写（`DesiredState.channel_pins`、`set_channel_pin`/`remove_channel_pin`/`remove_matching_channel_pins`、`SetChannelPin`/`RemoveChannelPin`/`RemoveMigratedChannelPins` 命令、SQLite 建表与读写）。旧文档里的 `channelPins` 键与升级后数据库里遗留的 `channel_pins` 表行不再读取，也不自动删除；该表不要手工删除，回滚前提见 `docs/guides/cell-authority-migration.md` 第 7 节。上文提到的测试已替换为 `test_kp116_legacy_channel_pin_never_binds_a_new_same_name_agent`。adapter removal 日志仍保留恒为空的 `legacyPin` 字段，随日志 schema 升级在后续切片删除。

## KP-117 `workflow.list` 把旧同名 agent 的运行列在新 agent 下

- 现象：workflow YAML 用名字形态 URI（`agent:<owner>:<node>:<name>`）命名节点 owner。带 agent 选择器的 `workflow.list` 除了 agent 的 id 地址，也按活 agent 的名字形态 URI 查看（`request_catalog/agent/relations.py`）。NF1 之后已销毁 agent 的名字可以再用，所以旧 agent 的运行在同名新 agent 的 `workflow.list` 里可见。routine 绑定按同样的名字映射比较（`_routine_binding_key`），公开的销毁会删除绑定在该 agent 上的 routine。
- 影响：只是列出，不是指派：PAC 节点的授权按入口令牌核对，新 agent 不会被授权处理旧运行的节点。
- 规避：无需规避；按运行 id 查看时以运行自身记录为准。
- 决定：接受（协调者 2026-11-19）。正解是在运行开始时把名字形态 owner 解析并绑定到 agent id，本片不做。当前行为由 `tests/daemon/ipc/contracts/agent/selector_fence/test_reused_name_workflow_listing.py::test_kp117_workflow_list_lists_an_earlier_same_name_agents_run` 固定。
- 依据：NF1 报告。
- 负责人：PAC / workflow 维护者。

## KP-118 授权审计流水按名字读取，同名新 agent 看到旧 agent 的流水

- 现象：`agent_grant_journal` 按 agent 名字（actor）存取，读取不要求 agent 存在（运维可以查看已销毁 agent 的历史）。NF1 之后已销毁 agent 的名字可以再用，所以同名新 agent 的审计视图里先是旧 agent 的流水行，再是自己的。
- 影响：只是显示。每行带各自的 entity token；生效的授权按 `agent_capability_grants` 与当前化身的 entity token 联结判定，新 agent 不继承任何授权。
- 规避：按行里的 `entityToken` 区分化身。
- 决定：接受（NF1，2026-11-19）；若要分开，需把流水改为按 agent id 读取，本片不做。
- 依据：`identity/impl/agents/registry/_records.py` 的 `grant_journal`；`tests/daemon/configuration/capabilities/test_agent_capability_grants.py::test_ledger_revision_revoke_restart_destroy`（重新邀请同名 agent 后断言旧流水在前）。
- 负责人：身份（identity）维护者。

## KP-119 CI 上 GUI 宿主进程偶发 60 秒内未就绪

- 现象：`tests/integration/gui_daemon_first` 里以真实进程启动 `hyprial gui` 的测试，在 CI 上偶发等不到宿主就绪（60 秒期限）。确认 1 次：#2995 合入 dev 那次运行里的 `test_gui_command_ownerless`。本地没有专门复跑过。
- 影响：只是 CI 偶发红；没有证据表明用户环境里启动变慢。
- 规避：重跑该任务。
- 决定：观察，不改时限。原因未知，那次失败的启动记录没有被读过。下次出现时先读那次运行的真实启动记录与宿主日志（`HostGui.logs()` 自 #3020 起按启动记录里的 `handoff_path` 取日志），确定卡在哪一步再处理；不以延长超时作为修复。
- 依据：#2995 合入后的 dev CI 运行；dev CI 分片自 2026-11-19 起另有 worker 被内存超限杀掉，两者是否相关未验证。
- 负责人：GUI 维护者（GUI 启动线已于 2026-11-21 结束，交接见 `docs/design/ops-upgrade-smoke-macos-plan-2026-11-20.md` 与看板）。

## KP-120a `HYPRIAL_OWNER` 设置时，已登出的 home 上 `hyprial logout` 答 `DAEMON_NOT_READY`

- 现象：`hyprial logout` 只发一次 `auth.logout`（L1）。调用环境里设置了 `HYPRIAL_OWNER`、home 里已没有登录（无 `secrets/login.json`、settings 无 Casdoor owner）时，命令启动或找到的 daemon 以环境里的 owner 进入 serving，但 `_assemble_onboarding` 只在公开身份快照有 owner 时装配账号端口（`src/hyprial/daemon/impl/application/onboarding/__init__.py`），于是 `auth.logout` 以 `DAEMON_NOT_READY`（"onboarding services are not assembled"）拒绝，命令退出 1。同一设置下第一次登出（还有登录时）正常完成，daemon 随后退出而不回到 bootstrap。
- 影响：只出现在以环境变量固定 owner 的 home（测试、e2e、黑盒契约夹具）上重复登出；没有 `HYPRIAL_OWNER` 的 home 第二次登出走 bootstrap 的空 home 幂等路径，返回 `not-logged-in`。dev 上的旧 CLI 在进程内判定、此情形返回 `not-logged-in`，所以这是 L1 的行为变化。
- 规避：登出时不设置 `HYPRIAL_OWNER`（`contract/cli-blackbox/run.py` 的登出检查即如此）；或在已无登录时不再登出。
- 决定：待 owner 决定。可选做法：serving daemon 在没有账号端口时也对空 home 的 `auth.logout` 答完成回执，或保持现状只登记；L1 不擅自扩大到 serving 装配逻辑。
- 依据：L1 实测（隔离 home 手工复现：有 `HYPRIAL_OWNER` 时第二次登出 `DAEMON_NOT_READY`，无时 `not-logged-in`）；`contract/cli-blackbox/run.sh --isolated-only` 的登出检查在带 `HYPRIAL_OWNER` 的夹具上复现。
- 负责人：登录线（L1 实现者报协调者，待 owner 裁定）。

## KP-120b `hyprial gui --stdin-stop` 收到 `SIGHUP` 时前端子进程留在孤立会话

- 现象：在真实 macOS 27.0.1 ARM64 独立 home 实操中（PR #3051 实测发现 F4），当 `hyprial gui --no-open --json --port 0 --stdin-stop` 进程收到 `SIGHUP` 被终止时，其拉起的前端/宿主子进程未随父进程一同退出，而是留在自己新建的进程会话中继续监听原 loopback 端口并持有该 home 的 GUI 运行时锁；相比之下，通过关闭 `--stdin-stop` 标准输入停机可以干净清理子进程并释放运行时锁。
- 影响：仅影响以 `SIGHUP`（例如终端挂断或信号误杀）终止 `hyprial gui --stdin-stop` 的场景；残留子进程会继续占用原监听端口与 `<home>/state/gui` 运行时锁，导致同一 home 后续启动 `hyprial gui` 或 `dev_upgrade_smoke.py apply --gui` 在启动前探测阶段即报 `GUI_RUNTIME_BUSY`。`dev_upgrade_smoke.py` 正常路径通过关闭标准输入停机（失败超时时对进程组发 `SIGTERM`/`SIGKILL`），不触发该缺陷。Linux 同路径尚未实测（UNKNOWN）。
- 规避：始终通过关闭 stdin 停止 `hyprial gui --stdin-stop`，或显式向其进程组/前端子进程发送 `SIGTERM`/`SIGKILL` 清理后再启动新 GUI；避免直接向其发送 `SIGHUP`。
- 决定：暂不修（观察登记）。在后续 GUI 启动/信号清理工作切片统一处理，实测覆盖 macOS 与 Linux 下的 `SIGHUP` 子进程清理。
- 依据：PR #3051 comment `39184`（2026-11-21 真实 macOS 27.0.1 ARM64 实操 F4）；证据见 `../notes/ops-upgrade-smoke-macos-evidence-2026-11-22.md`。
- 负责人：GUI 维护者 / Mac 打包线（待后续排期认领）。

## KP-121 升级后未重新登录就登出的老 home，同一账号再登录被拒

- 现象：L3 之前创建、升级时仍处于登录态的 home 没有 `settings.homeAccount`。新版本只在登录提交时写入 `homeAccount`；`hyprial logout` 删除 `owner`、`userId`、`identityIssuer` 而不补写绑定。若升级后第一次操作是登出，之后以同一账号 `hyprial login`，提交前的检查看到「未绑定 + 目录里有计入的业务数据（如 `state/agents.sqlite3`）」，以 `IDENTITY_CONFLICT`（`data.reason = "home-binding-missing"`，带 `loginUserId` 与 `issuer`）零写入拒绝。
- 影响：所有升级前处于登录态的老 home，在新版本上重新登录过一次之前都处于此风险；用户须手工编辑 `settings.json` 才能回到原账号。升级后先在登录态重新登录一次的 home 不受影响（登录态下绑定从现有 `identityIssuer`/`userId`/`owner` 读出，重新登录即写入 `homeAccount`）。
- 规避：升级后先以同一账号执行一次 `hyprial login`，确认 `settings.json` 出现 `homeAccount` 后再登出。已经被拒的：`hyprial daemon stop` 并确认停止，核对 `state/identity/user-keys/*/key.json` 的 `casdoorSub` 等于错误 `data.loginUserId` 后，在 `settings.json` 补写 `homeAccount`（`issuer`、`userId` 取自错误 `data`，`owner` 取自 `key.json`），再 `hyprial login`。步骤见 `docs/guides/migration-account-binding.md` 第 2 节结论与第 5 节。
- 决定：接受，不修。协调者 2026-11-23 选定选项 2：不在登出时从现有登录字段自动写入 `homeAccount`，遵守「不写迁移代码、不做自动改写」原则；以迁移说明、CHANGELOG 警告与本条登记代替。
- 依据：L3 critic 报告 P1-2（隔离 home 探针：登录 → 去掉 `homeAccount` → 放入 `state/agents.sqlite3` → `remove_logout_artifacts` → 同 uid 登录，得 `HOME_ACCOUNT_CONFLICT`、`reason=home-binding-missing`）；`src/hyprial/identity/impl/onboarding/credentials/flow.py` 未绑定分支；`identity/impl/onboarding/logout.py`。
- 负责人：登录线（L3）。

## KP-122 cell 成员 reconcile 一次有界漂移重启后停在 FAILED，及其余边界

- 现象：只涉及在 `cell.members` 里声明的成员（自定义模板或手改的 cell 文档）。
  - 有界重启之后卡住：reconcile 对运行规格与文档不一致的成员只做一次停止再启动。如果停止没有生效，或者有别的启动方在停止和再次 ensure 之间按旧行拉起了进程，成员就记为 `FAILED`（`config-digest-mismatch`），旧进程继续运行。之后再改文档（换一个 model，或者再声明一次）都只是接管活进程，然后再次失败，不会再停。快照如实显示 `FAILED`、cell `DEGRADED`，但 reconcile actor 之外没有代码读 `can_admit_work`，这个 agent 仍会收到工作。
  - 失败后退役 cell：处于上述 `FAILED`、标记为未运行的槽位，退役时直接弹出，不发停止，进程继续运行。
  - 先停后起：漂移重启在确认新规格能启动之前就停掉了正在工作的 agent。如果启动失败，成员处于 `FAILED` 并停机，desired-state 行仍是 `running`，要等下一个 revision 或者 daemon 重启才会回来。
  - 回写的 model 不会自动撤回：先声明 `model: opus` 并到达 READY，再声明 `{}`，成员仍跑 `opus`，行里原来的 `sonnet` 已经被覆盖。
  - 文档的 `kind` 与本地行的 harness 不同：本地行仍按原 kind 存储（行以 harness kind 为键），所以每次 daemon 恢复都会先启动原 kind，reconcile 随即停掉它、启动文档的 kind，每次开机替换一次。新 kind 启动时不带原行的 `session_ref`。
  - 存储的参数里有裸位置参数（如 `('positional',)`）时，参数合并不是幂等的。活进程摘要永远不等于期望摘要，于是重启一次后进入 `FAILED`。受管 Claude 启动本来就拒绝这类参数；codex、pi 是否接受未知。
  - 上一轮 critic 记录、本片未处理的其余边界：恢复策略留在停机状态、但声明为 `running` 的行，会在 barrier 打开时启动；daemon 自己写 cell 也算一个新的 revision；reconcile 之外停掉的成员，快照里仍显示 READY；barrier 打开时的 tell 顺序；接管一个处于 `starting` 的 harness 会占住一个 effect 线程；`env_ref`/`policy_digest` 取自 intent；`_stop_member` 回退到 URI；被持有的行在后来的 revision 里释放后，daemon 重启又会被持有。
- 影响：只有声明了 `cell.members` 的 cell 受影响；内置模板与未声明成员的 cell 仍由 daemon 恢复负责，不受影响。卡住时快照如实显示 `FAILED`，但实际运行的进程与文档不一致，且仍然接收工作。
- 规避：把文档改回与运行中进程一致的规格（成员回到 READY、清除重启标记，之后的修改恢复正常），或者重启 daemon（标记只在内存中，每次开机允许一次重启）；退役 cell 之前，先手动停掉该成员的 harness。
- 决定：暂缓（defer）。I6-2 只修了 critic V5 的 P1-A（kind 不一致导致一个成员同时跑两个 harness）和 P2-3（漂移重启中的重新启动没有写入 journal）。是否在新 revision 改变期望摘要时清除重启标记、退役时是否对 `FAILED` 槽位补发停止、是否改成先起后停，留给 cell 模型线后续切片（I6-4 起）决定。
- 依据：I6-2 critic V5 报告的 P2-1、P2-2、P2-4、P2-5、P2-6、P2-7（基于 `_World` 夹具的 14 个探针）；`src/hyprial/daemon/impl/composition/cells/reconcile.py` 文件头的决策块。
- 状态更新（I6-5 合入 I6-2，2026-11-24，integration critic P2-5）：
  - 范围扩大：上文「只有声明了 `cell.members` 的 cell 受影响、内置模板不受影响」已不成立。I6-5 的 registrar 把 `managed-worker` 模板规则写进每个 claude agent 自己成员的声明，所以**每个 claude agent** 都在本条范围内。
  - kind 不一致分两种：agent 自己的成员（以 agent URI 命名、即 `hyprial agent start` 启动的那个 harness）拒绝（`OWN_MEMBER_HARNESS_MISMATCH`），什么都不停、什么都不启动，cell 为 `degraded`，直到文档改回或用 `hyprial agent start --harness` 切换；上文「每次开机替换一次」只适用于非自己的声明成员。
  - 被持有的行（coordinator 裁定 P2-1）：持有只看行状态。任何文档修订（包括 registrar 自己写的模板种子与 owner 的修改）都不会再启动 `failed` 行，只有显式的 `hyprial agent start` 能放行（确认后的生命周期把行改回 `running` 并触发 reconcile）。上文「被持有的行在后来的 revision 里释放后，daemon 重启又会被持有」已不再发生。非 agent 自己的声明成员没有能放行它的命令（`agent.start` / `lifecycle.start` 没有 `member` 参数），生产中是否存在这类成员行未知。reconcile 仍记为运行中的成员在行变成 `failed` 后（harness 已耗尽重启预算、进程已不在）会收到一次普通的停止；非自己成员的这次停止会删掉这一行（按阅读，未探针）。
  - 漂移重启的上限（coordinator 裁定 P2-2）：agent 自己的成员按**期望摘要**各重启一次：重启后仍不一致时记下该期望摘要，之后不改变它的修订都不再重启，成员保持 `FAILED`（`config-digest-mismatch`）；改变配置的修订再得到一次重启。非自己成员保持 I6-2 的更严格规则（每个成员一次，直到某次 ensure 达到期望摘要；配置变化本来就走计划内的停止再启动）。
  - 新的进入方式（integration critic P2-3）：非自己成员的新启动未确认策略时，成员为 `FAILED`，新起的进程不停、重启标记不清，之后的修订接管它又失败。生产可达性未知，大概率没有。
  - 已确认的策略被报为「无策略」（P2-4）：一个按带规则的文档启动、之后被不带规则的文档首次观察到的接管进程，成员的 `policy_digest` 显示为空。claude 成员会被 registrar 重新写入种子，窗口在非种子成员或 registrar 推迟时。
  - 非自己成员的新启动失败时（包括「无策略」），已发布的策略条目不撤回（P2-7，按阅读）；agent 自己的成员会释放。
- 负责人：cell 模型线（cm）。

## KP-123 Windows 提升权限进程下 `mkdir(0o700)` 目录被判 `unsafe-acl`

- 现象：CPython 3.12.4+（gh-118486）在 Windows 上对 `os.mkdir(path, 0o700)` 写入受保护的 DACL `D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)`。提升权限的进程（管理员 PowerShell、内置 Administrator、关闭 UAC 的管理员账户）新建对象的默认 owner 是 `BUILTIN\Administrators`，不是用户本人。这样建出的目录没有当前用户 ACE，`OWNER RIGHTS` 又只在 owner 等于当前用户时才计为当前用户可读，`private_metadata_violation` 返回 `unsafe-acl`。被 `secure_private_directory(create=True)` 建出并 harden 的 home 与 state 不受影响；之后用普通 `mkdir(mode=0o700)` 建、随后被校验的目录受影响，例如 `src/hyprial/daemon/impl/onboarding/first_run_attempt.py:791`（`FIRST_RUN_STORE_UNAVAILABLE`）与 `src/hyprial/identity/impl/user_key/store.py:61`（`USER_KEY_QUARANTINED`）。这些目录下新建的文件继承同一 DACL，owner 同样是 Administrators。
- 影响：只有 Windows、Python 3.12.4 及以后、且进程处于提升状态。bootstrap 在 admission 通过后的下一个私有目录上失败。此前在提升状态下运行失败而留下的 home，其 `state\` 已存在，launcher 不改写，仍被拒。非提升进程不受影响；POSIX 不受影响。
- 规避：用非提升的 shell 运行 hyprial。真机验收须记录 `whoami /groups` 的输出，确认 `BUILTIN\Administrators` 不是 enabled 状态。已被提升进程建坏的 home 换一个新目录重新 `hyprial init`。
- 决定：暂不修（PR `fix/windows-bootstrap-private-acl` 只登记）。待真机证据确认提升状态下的实际 owner 与失败点后再定方案；候选是把这些 `mkdir` 改为创建后 harden，或在 owner 为 Administrators/SYSTEM 且当前令牌已启用该组时把对应 ACE 计为可读。
- 依据：`src/hyprial/kernel/impl/manysys/fs/private_access.py` 文件头决策块与 `_windows_violation`；`tests/kernel/platform/test_private_access.py::test_owner_rights_does_not_count_as_current_user_read_for_an_admin_owner` 与 `tests/daemon/onboarding/lifecycle/test_windows_state_dacl.py::test_elevated_launch_is_admitted_but_a_plain_private_mkdir_stays_unsafe` 钉住当前结果（模拟的 Windows 安全描述符）；CPython `Modules/posixmodule.c` 在 v3.12.4 起的 `mode == 0700` 分支。提升进程的默认 owner 为 Administrators 是 Windows 平台行为，真机 UNKNOWN。
- 负责人：Windows 可移植性线。

## KP-124 Windows 上启动记录的 `handoff_note` 仍称「private 0600 file」

- 现象：`hyprial gui --no-open --json` 的启动记录里，`handoff_note` 在所有平台上都是同一句「Open the absolute URL in this private 0600 file. The host removes it after the launch grant is consumed or expires; Ctrl-C also removes it.」（`src/hyprial/shell/impl/gui/install/report.py:99-100`），人读输出的 `open` 行也打印它。Windows 没有 mode 位，交接文件的私有性靠运行时目录的账号私有访问（DACL），不是 0600。契约 `contract/cli/gui-startup-record.v1.json` 的 `consumerChecks` 已按平台分别说明，但其 Windows 形态示例的 `handoff_note` 与 POSIX 示例保持同一句缩写，照 host 的现状写「0600」。
- 影响：只是说明文字不准确。`handoff_path`、交接文件的内容与访问控制、launcher 的解析都不受影响；`handoff_note` 不在 `startupRecord.required` 内，consumer 不应解析它。在 Windows 上读启动记录或契约示例的人会看到与实际机制不符的「0600」说法。
- 规避：Windows 上以契约 `consumerChecks` 与 `contract/cli/README.md` 的 `--no-open` 条目为准，忽略 `handoff_note` 里的「0600」。
- 决定：暂不修（登记）。修改 host 文案属于 `src/` 改动，不在放宽 `handoff_path` pattern 的契约 PR 范围内；`handoff_note` 按 `contract/cli/README.md` 可以改写而不升版本，后续在 Windows 可移植性线改为不含 mode 的平台中性说法，并同步契约示例。
- 依据：`fix/windows-handoff-path-contract` 的 critic 审查 S2（2026-11-23）；`src/hyprial/shell/impl/gui/install/report.py:99-100`；Windows 真机上的实际输出未实测（UNKNOWN）。
- 负责人：Windows 可移植性线。

## KP-125 同一 adapter 被两个 agent 的 cell 声明时，写入不拒绝，路由读作“未绑定”

- 现象：adapter 绑定写在 agent cell 文档的 `role: "adapter"` 成员里（I6-4）。`agent.cell.update` 写入时不检查别的 agent 的 cell 是否已声明同一 adapter。路由索引发现两份声明时按冲突 fail-closed（`src/hyprial/daemon/impl/cells/adapter_routing/index.py` 的 `status="conflict"`），但对外只表现为未绑定：`adapter.list` 的 `agent` 为 `null`，入站发送者收到“没有绑定的 agent”。
- 影响：一次误写就会让原来已绑定 agent 的入站流量停下，且没有明确提示冲突。写入只允许本机运维者（local-operator），路由不会把消息投给错误的 agent。
- 规避：改绑前先用 `hyprial adapter list` 确认该 adapter 当前绑定的 agent，并从旧 agent 的 cell 去掉该成员再写新 agent；`adapter.list` 出现 `agent: null` 而 cell 里明明声明了该 adapter 时，检查是否有第二个 agent 也声明了它。
- 决定：延后。下一片在 `adapter.list` 或 `agent.cell.update` 里公开冲突状态（索引已计算 `status="conflict"`），属于小的契约增补。
- 依据：I6-4 V2 critic（P2-3）；`cells/adapter_routing/index.py` 冲突判定。
- 负责人：cell model（cm）线。

## KP-126 cell 文档里的 adapter 成员不检查 adapter 是否存在

- 现象：`agent.cell.update` 接受 `cell.members.<ref>.adapter` 指向任意名字，不核对本机是否配置了该 adapter。已退役的 `adapter.pin` 会拒绝未知 adapter。
- 影响：拼错名字时不绑定任何东西也不报错；之后用旧名字重新添加 adapter，会重新接到仍列着它的那个 agent 上。
- 规避：写入后用 `hyprial adapter list` 核对该 adapter 的 `agent`；删除 adapter 时同时从 agent cell 去掉对应成员。
- 决定：延后。后续切片在写入时校验 adapter 存在（或给出警告）。
- 依据：I6-4 V2 critic（P2-4）。
- 负责人：cell model（cm）线。

## KP-127 `agent.cell.get` 的受众包含 `bound-agent`，可读其他 agent 的 cell 文档

- 现象：`agent.cell.get` 的受众是 `local-operator` 与 `bound-agent`（`src/hyprial/daemon/impl/request_catalog/agent/cell/specs.py`）；`agent.cell.update` 只有 `local-operator`。任何已绑定的 agent 都能读取其他 agent 的 cell：成员、adapter 名字与权限列表。不含凭据。
- 影响：同一 owner 下 agent 之间可见彼此的组织结构；写入没有放宽。
- 规避：无。
- 决定：延后，待 owner 确认 `agent.cell.get` 的预期受众；没有找到写明受众的设计文本。
- 依据：I6-4 V2 critic（P2-5）。
- 负责人：cell model（cm）线（owner 裁定受众）。

## KP-128 Lark 入站在 daemon 不可用超过 120 秒重试预算后记为 `forward-retry-exhausted` 死信，需要手工恢复

- 现象：I6-4 让 daemon 的 cell 运行时不可用时 `adapter.list` 返回 `DAEMON_NOT_READY`。Lark worker 对它按瞬时错误重试，有界：`_transient_retry_budget` 默认 120 秒，指数退避（`src/hyprial/daemon/impl/adapters/lark/inbound/pipeline.py`）。超过预算后该入站消息记为 `forward-retry-exhausted`，正文保留，发送者收到注明 `DAEMON_NOT_READY` 的“未送达”回复。`src/` 里没有代码自动调用 `recover_message`（`inbound/reconcile.py`）。
- 影响：短于 120 秒的不可用不会产生死信；更长的不可用会留下需要手工恢复的消息。与现有 `DAEMON_RESTORING` 的行为一致，消息不会记为 `no-target`。
- 规避：daemon 恢复后对 `forward-retry-exhausted` 的消息手工执行恢复。
- 决定：延后。自动恢复与预算调整由后续切片处理。
- 依据：I6-4 V2 critic（Lark 注意事项）。
- 负责人：cell model（cm）线。

## KP-129 cell 运行时组合失败期间，本机 agent 发给 owner 的消息被拒绝

- 现象：cell 运行时组合失败后（日志 `cell.runtime_unavailable`，`app._cell_runtime_failed_at` 已设），`recording_available` 为假，发给 `user:<owner>` 的消息不再入队，只走推送（`src/hyprial/daemon/impl/user_agent/local_target.py`）。推送路径上读不到发送方的 cell，本机每个 agent（绑定 adapter 的与未绑定的都一样）都落在契约 i7-0 v2 §3.2 第 6 行：`message.send` 以 `USER_DELIVERY_UNAVAILABLE` 失败，`send.user_push` 带 `pushSkipped: "cell-unavailable"`，不转交 user-proxy（`send_targets/user.py` 的 `_hand_off_unqueued`）。不是本机 agent 的发送者（operator 等）仍转交 user-proxy。I7a（2026-11-24）起 user-proxy 交接已删除：operator 等发送者改由 user agent cell 推送，组合失败时同样读不到 user agent cell，也以 `USER_DELIVERY_UNAVAILABLE` 失败（`pushSkipped: "cell-unavailable"`）。
- 影响：持续到组合成功为止；daemon 每 `COMPOSE_RETRY_S`（30 秒）重试一次，瞬时原因（如 OrgFS 一时不可用）消失后的下一次重试即自动恢复。`cache-identity-mismatch`、持续的 `cache-unreadable` 与 `authority-uid-missing` 需要人工修复，在那之前本机所有 agent 发给 owner 的消息一直失败。消息没有入队，也不进对话记录；发送方收到错误，可用同一个 key 重发。workflow 报告把该错误码视为可重试并保留（`delivery/sends.py` 的 `_REPORT_RETRYABLE`），重试上限未核实（UNKNOWN）。
- 规避：按 `docs/guides/migration-cell-authority-uid.md` 第 4 节处理日志中的 `reason`（移走冲突的 `state/cells.sqlite3` 三文件，或补齐 uid 后重启 daemon）；`docs/guides/migration-im-push-by-agent-binding.md` 第 3 节第 7 条写明了操作员视角的现象与恢复。
- 决定：接受。读不到 cell 时不猜测绑定、不选聊天、不退回 user-proxy，是契约 §3.2 第 6 行与 §3.4 的明确规定，失败是闭合且有日志的。它收窄了设计 §10.0 关于 owner 消息照常入队的承诺，需告知 owner。
- 依据：I3 critic（cm-coord3，P2-1 / Q3）。
- 负责人：cell model（cm）线。

## KP-130 「本机运维者」不是硬边界：agent 自己的 shell 可读写 user agent cell

- 现象：I7a 起 `agent.cell.get` / `agent.cell.update` 在允许清单内接受 user agent cell，受众是本机运维者（`src/hyprial/daemon/impl/application/actors/cell_admin/service.py` 的 `_local_operator`）。运维者判定由 `_identity_binding_operator`（`src/hyprial/daemon/impl/ipc/router.py`）给出，其 docstring 已写明：agent 自己的 shell 运行 CLI 时算未验证的运维者。daemon 能识别的调用方（worker origin、`actor`/`sessionRef`、已登记的交互 peer）会被拒绝，识别不了的进程不会。
- 影响：这样的进程可以读 user agent cell，并改写其 adapter 成员与 `user` 连接，从而改变 owner 副本推往哪些聊天（只能是本机已配置的 adapter）。与 I7a 之前对任何普通 agent cell 执行 `agent.cell.update` 的强度相同；同一进程本来也能直接读磁盘上的 `channels.json`。
- 规避：无；不要把「只有本机运维者」读成硬边界。
- 决定：接受（继承的限制，I7a 只扩大了它的作用面）。
- 依据：I7a critic（cm-coord3，P2-4）。
- 负责人：cell model（cm）线。

## KP-131 成员 reconcile 重启时新旧配置都起不来，agent 停到下次启动或 `hyprial agent start`

- 现象：reconcile 为策略或 `model` 变化重启 agent 自己的成员（或首次接管未确认策略的运行中 harness）时，先停 harness，再以新配置启动；新配置失败后以失败前运行的配置重拉。两次启动都失败（例如同一个原因导致的进程起不来）时，agent 不在运行，成员为 `failed`，cell 为 `degraded`。
- 影响：该 agent 停到下一次 daemon 启动、下一个改变其配置的文档修订，或操作者执行 `hyprial agent start`。重启时 desired-state 行不删除（`keep_intent` 停止，`lifecycle_ops._ipc_down`），所以启动恢复会按该行重新拉起；daemon 在停与启之间被杀同样如此（`tests/daemon/application/actors/harness_args_lifecycle/test_restart_keeps_intent.py`）。例外：重启的停止本身报错、随后两次启动也失败时，停止经 `fail_harness_removal` 结算，行仍被删除（I6-5 critic V4 P2-1）。两次启动失败后到恢复之前，行状态为 `running` 而没有进程，只读行状态会误以为 agent 在运行；成员（`failed`）与 cell（`degraded`）的状态是准确的。
- 规避：`hyprial agent start <agent>`。
- 决定：接受。保留启动意图后状态可见、可自愈；同一次重启内不再自动重试。
- 依据：I6-5 critic V3（P1-1 裁定：保留 desired-state 行后可作为已知问题）。

## KP-132 留有 user-proxy 启动记录的 home 升级后 daemon 起不来，原因只在启动日志里

- 现象：I7b 删除了 `user-proxy` harness。升级前没有销毁 user-proxy 的 home，desired state（`state/lifecycle-operations.sqlite3` 的 `harnesses` 表，或未导入的 `state/desired-state.json.v1`）里仍有 `harness = 'user-proxy'` 的一行；新版本读 desired state 时拒绝它，daemon 启动即退出。终端只显示 `DAEMON_START_FAILED`（`daemon exited during startup with status 1`，`recentLog` 为 `null`），原因在 `state/daemon-launch.log` 最后一行（`providers[N] has an invalid harness; expected codex, claude, pi, jev, or agy`），只给序号不给 agent 名。
- 影响：手工修复之前，需要 daemon 的所有命令都失败；`hyprial daemon status` 不受影响。修复后 `lifecycle_resources` 表留下一行 `harness:user-proxy:<proxy 名>`（active），实测无害（两次停启、`agent list`、`agent destroy` 都正常）。
- 规避：升级前按 `docs/guides/migration-im-push-by-agent-binding.md` 第 14 节销毁 user-proxy；已升级的按同节「手工修复」删掉 `harnesses` 表里那一行再启动 daemon。
- 决定：接受（A-4，Allen 批准：不写兼容或迁移代码，迁移指南是唯一的安全网）。`daemon start` 不把启动日志的原因带到终端是 I7b 之前就有的行为（`daemon_start.py` 的 `recentLog`），另行处理。
- 依据：I7b critic（cm-coord3，P1-1 / P2-1 / P2-3），隔离 home 实测。
- 负责人：cell model（cm）线。

## KP-133 `agent tiers probe` 在 CLI 进程里跑探测，绕过 daemon 请求（两行受审例外）

- 现象：S3c 起 `hyprial agent tiers probe` 直接导入 `hyprial.daemon:probe_capabilities`，在 CLI 进程内运行 harness 探测并写 `state/runtime-capabilities.json`，不发任何 daemon 请求。daemon-request 架构门因此在 `src/hyprial/shell/impl/cli/commands/agent/runtime/tiers.py` 报 `backend-import` 与 `command-without-request` 各一行，登记在 `scripts/ci/check_daemon_request_architecture.py` 的 `REVIEWED_CLI_EXCEPTIONS`（各以 1 为上限，内容由测试固定）。
- 影响：运行能力缓存有两个写者进程（daemon 的 provider-auth 标记与 CLI 的显式探测），只靠文件锁协调；先读后写不在锁内，并发时 CLI 的探测可能覆盖 daemon 刚写的账号标记。没有可用的 daemon 方法替代：契约里没有运行探测或读写运行能力的方法。
- 规避：避免在 daemon 处理 provider-auth 故障/恢复的同时运行 `agent tiers probe`；被覆盖时重跑一次 probe。
- 决定：暂留为债。退出条件：新增 daemon 方法 `agent.tiers.probe`（探测可能持续 `timeout × 组合数` 秒，需流式或作业形态；记录视图由读方法提供），daemon 成为缓存唯一写者，届时删除这两行例外及 `dispatch.py` 余下的后端行。新增方法是新的已发布面，需 owner 批准。
- 依据：S3c critic（cm-coord3，P1-1 / P2-4）。
- 负责人：cell model（cm）线。

**OrgFS 精简实现的已知限制（2026-11-25 起）**

下面 KP-134 至 KP-149 是 OrgFS 改为“只保存当前状态”的精简实现之后仍然存在、并且决定暂不修的问题。术语：**空间**是一个 OrgFS 文件空间；**节点门**是每个节点在与对端交换某空间的数据之前做的检查（对端在线，且对端节点所属的用户是该空间的当前成员）；**准入**是接收方在应用一个远端更新之前，比较应用前后的结果状态、拒绝违反规则的更新；**blob** 是二进制文件内容。设计见 `docs/design/orgfs-minimal-design-2026-11-25.md`，迁移见 `docs/guides/migration-orgfs-minimal.md`。

## KP-134 OrgFS 的节点名与作者是自报的，签名今天不强制

- 现象：传输层不提供已认证的对端身份。节点门、准入与写进文档和目录行的作者字段，用的都是对端自己报出的节点名与用户名。仓库里有签名原语，记录格式也预留了签名字段，但生产上注入的校验实现恒返回“不可用”，没有任何记录被要求带有效签名。
- 影响：这些检查防的是如实报告身份的节点，不防伪造身份的节点。一个仍是成员的节点可以把写入署成另一个成员的名字。旧实现同样如此，不是回归。
- 规避：不要把 OrgFS 的作者字段当作身份证明或审批证据；需要可验证的审批时用显式的 PAC 审批节点。
- 决定：接受（owner 2026-11-25）。签名强制等用户密钥签发上线后另行接入，不需要改磁盘格式与同步协议。
- 依据：设计 §0.1–§0.3、§3.1、§3.8。
- 负责人：OrgFS 维护者。

## KP-135 只读成员与被降级成员的写入只在写入节点本地与第一跳节点门被拒

- 现象：只读成员与非成员的写入，在写入节点本地被拒，并在第一跳的节点门被拒；节点门只看对端节点所属的用户是否是成员，不看读写模式。接收节点不依据内容里的作者再查当前成员资格与模式。因此：被降为只读的成员，在它的节点得知降级之前写的内容会被接受；某个写入一旦被任何一个成员节点合并，之后随该节点转发，不再被拦。被移除的成员在分区期间的新写入会被节点门拒绝。
- 影响：读写模式不是跨节点强制执行的边界。旧实现同样接受降级之前的写入，不是回归。
- 规避：需要硬隔离的内容放进只有可信成员的空间。降级或移除成员后，检查该成员在变化前后写入的内容。
- 决定：接受（owner 2026-11-25：成员资格的变化在各节点得知之后才生效）。
- 依据：设计 §3.1、§3.5；旧实现上的同场景对照测试。
- 负责人：OrgFS 维护者。

## KP-136 blob 拉取通道不识别请求方

- 现象：二进制内容按内容摘要拉取。应答方无法核对发出拉取请求的是谁，不能据此判断请求方是否是该空间的成员。
- 影响：知道某个摘要、并能连到持有该内容的节点的一方，可能取到这份二进制内容（推断，未实测）。新旧实现相同。
- 规避：无。
- 决定：不修（改它需要改线路协议）。
- 依据：节点门查证（计划 `docs/design/orgfs-minimal-plan-2026-11-24.md` §9 节点门一行）。
- 负责人：OrgFS 维护者。

## KP-137 并发写同一受保护记录时，在合并中输掉的伪造更新被接受

- 现象：准入比较的是合并之后的结果状态。两个写者并发修改同一条受保护记录（例如设备记录）时，如果伪造的一方在 CRDT 合并中输掉、结果状态不变，这次更新被接受、不计入 `rejected_updates`。输掉的那部分结构留在目录文档里，不改变任何可见的行。
- 影响：可见数据不受影响；目录文档里多留一点不可见的结构。
- 规避：无需处理。
- 决定：不改。
- 依据：设计 §9.2“准入规则的补充”末段。
- 负责人：OrgFS 维护者。

## KP-138 递归列目录不分页

- 现象：一次递归列出整个空间的目录树没有分页。实测根目录递归列出 25 100 项约 174 ms，耗时与返回项数成正比。
- 影响：极大的空间里，一次递归列出的结果与耗时随空间大小线性增长。CLI 的 `org fs ls` 只列一层，不受影响。
- 规避：按层列出。
- 决定：不修（测量后决定不加分页）。
- 依据：写入成本调查中的只读测量（计划 §9 写入成本一行）。
- 负责人：OrgFS 维护者。

## KP-139 统计“正文未到”的文档时周期性扫描整张节点表

- 现象：OrgFS 维护“本地还没拿到正文的文本文档”的集合（`org fs space status` 的 `content_pending` 由它得出）时，每核对 128 篇文档扫描一次整张节点表。2.5 万篇文档的空间合计约 0.8 秒。
- 影响：核对一个大空间的积压时有一段与空间大小成正比的额外 CPU 开销。
- 规避：无需处理。
- 决定：暂不改，空间规模明显更大时再看。
- 依据：同 KP-138 的测量。
- 负责人：OrgFS 维护者。

## KP-140 带 `--base-version` 的写入在同一行冲突时后写者胜，不报错

- 现象：`org fs write --base-version <版本>`（以某个旧版本为基准做三方合并的写入）按行合并：两边在基准上修改了不同的行时都保留；修改了同一行时取后写者的这一行，先写者对这一行的修改被丢弃，不报错。同一行里两处互不重叠的字符修改也算冲突。
- 影响：并发编辑同一行时，一方的修改会无声地消失。
- 规避：不能接受丢失修改的场合用 `--expect-version`（版本不符时返回 `stale-write`，调用方重读后再写）。
- 决定：待负责人决定是否改为对重叠行返回 `stale-write`。
- 依据：设计 §9.2“真实节点第二轮之后的裁定”第 2 行；真实多节点验证中发现的旧算法文本损坏已改为按行合并。
- 负责人：OrgFS 维护者。

## KP-141 很旧的 `--base-version` 不再能合并，返回 `stale-write`

- 现象：OrgFS 不再保存历史，三方合并所需的基准正文只来自一个进程内缓存：保存本进程交给过调用方的正文，最多 256 条、合计 16 MiB，最久未用者先淘汰。基准不在缓存里时（被淘汰、daemon 重启之后，或基准版本是在别的节点上读到的）返回 `stale-write`。最后一种情形是从“只缓存本进程交出的正文”推断的，未单独实测。
- 影响：以前很旧的基准版本也能合并，现在不行。
- 规避：收到 `stale-write` 后重读，再基于新版本重写。
- 决定：接受。
- 依据：设计 §9 表格第 1 行。
- 负责人：OrgFS 维护者。

## KP-142 二进制文件没有人读就不会取回，也没有到达事件；`content_pending` 不计二进制

- 现象：远端写入的二进制文件按需取回：只有本机有人读取或导出它时才去拉。取回入库之后，为引用它的每个节点发一次 watch 的 `modified` 事件；没有人读就不会取回，因此也没有事件。`org fs space status` 的 `content_pending` 只统计文本文档，不统计二进制文件，所以 `content_pending` 为 0、`synced` 为 `true` 并不表示二进制文件都已在本地。
- 影响：订阅方不能靠 watch 得知远端二进制何时可读；断网前若要保证二进制在本地，需要先读一遍。
- 规避：需要本地副本时显式 `org fs read` 或 `org fs export`。
- 决定：接受（不做后台补齐）。
- 依据：设计 §9.2“真实节点第二轮之后的裁定”第 3 行；真实多节点验证。
- 负责人：OrgFS 维护者。

## KP-143 删除留下的墓碑随删除次数增长

- 现象：文档开启垃圾回收，被覆盖与被删除内容的正文在下一次合并后不再占用磁盘，但每个被删除的键仍留下约 15 字节的墓碑；被多次覆盖过的行删除后留下更多（实测覆盖过 20 次的行约 137 字节）。
- 影响：长期频繁删除的空间，状态文件会缓慢增长。
- 规避：无需处理。
- 决定：接受（量级小）。
- 依据：设计 §6；规模测试。
- 负责人：OrgFS 维护者。

## KP-144 移动操作的时钟是进程内计数

- 现象：决定并发移动先后的时钟值 `ts` 是进程内的计数，不是跨重启、跨节点一致的时钟。这是旧实现就有的问题，精简实现沿用。
- 影响：并发移动同一节点时，哪一次生效可能与真实时间顺序不一致（推断，未验证）。
- 规避：避免在多个节点上同时移动同一节点。
- 决定：不在本次修。
- 依据：设计 §9 表格 R10 行。
- 负责人：OrgFS 维护者。

## KP-145 被拒绝的远端更新不会自动对账

- 现象：准入拒绝的远端更新不应用、不落盘，计入该空间的 `rejected_updates` 并记日志 `orgfs.update.rejected`。同样的内容对端每次同步都会再提供，本节点只在有界的记忆里静默丢弃。被拒绝的内容使该文档在本节点上无法与该对端收敛，没有自动修复。
- 影响：`rejected_updates` 大于 0 的空间可能有文档在不同节点上长期不一致。
- 规避：用 `org fs space status <空间> --json` 查看 `rejected_updates`，按日志里的 `docId`、`peer` 找到来源，在对端修正或永久删除（`org fs rm --permanent`）该文档。
- 决定：接受（没有逐提交历史的代价）。
- 依据：设计 §3.7。
- 负责人：OrgFS 维护者。

## KP-146 网络可达性门在同名定义超过 8 个时不连边

- 现象：`scripts/ci/check_network_reachability.py` 按方法短名把调用连到定义；某个名字的定义超过 8 个时，门不为它连边。类层次里重复的方法名（例如 mixin 与子类各定义一次）会让门静默地漏掉经过它的路径，表现只是基线条目变成“陈旧”，不会报新违规。
- 影响：门的覆盖面可能悄悄变窄。OrgFS 切换时出现过一次：两套实现并存让若干名字越过 8，基线中 20 条变为陈旧，合并重复定义后恢复。
- 规避：门报陈旧条目时，先查是否有方法名的定义数刚越过 8，不要直接删条目。
- 决定：登记，待门的维护者决定是否改为报警。
- 依据：OrgFS 切换时对可达性门的查证（设计 §9.2 末段）。
- 负责人：CI / daemon 网络边界维护者。

## KP-147 `orgfs.checkout` 对未知空间报 `checkout-io`，不说明原因

- 现象：checkout 的权威层把所有失败包装成 `checkout-io`。旧格式空间现在单独透传 `ORGFS_LAYOUT_UNSUPPORTED`，但空间不存在等其他查找错误仍报 `checkout-io`，没有能区分原因的错误码。
- 影响：调用方分不清“空间不存在”和真实的读写故障。
- 规避：先 `org fs space list` 确认空间存在。
- 决定：暂不改（改了会变更已发布的请求例子 `orgfs.checkout:missing-space`），待 OrgFS 负责人决定。
- 依据：公共面收窄时的查证。
- 负责人：OrgFS 维护者。

## KP-148 加入空间时，供给方信任加入者自报的作者

- 现象：加入（join）一个空间时，供给方根据加入者自己报出的作者判断它是否已被邀请。今天经组织邀请加入时，冒名会被组织成员授权的邀请兑换规则挡住：兑换绑定的是会话所属设备的 owner，而不是 daemon 自报的作者。测试 `tests/daemon/org/test_org_join_identity.py::test_another_identity_declaring_itself_the_invitee_is_refused` 标为非严格 xfail，实际通过（XPASS），原因就在这里。
- 影响：去掉兑换规则的对照实验里，自称为受邀者的另一台机器经 OrgFS 完成了加入。直接经 OrgFS 加入而绕过组织邀请的路径上，这个缺口仍在。
- 规避：成员只经组织邀请加入。
- 决定：待加入与身份负责人决定：是否拆分该测试，一条钉住兑换规则，另一条以严格 xfail 钉住 OrgFS 的缺口。
- 依据：加入身份测试的对照实验（去掉兑换规则后该测试变为 “DID NOT RAISE”）。
- 负责人：org 加入与身份维护者。

## KP-149 GUI 回收站列表只显示默认上限内的条目

- 现象：GUI 读取回收站时不传条数上限，使用 daemon 的默认上限（旧注释写的是 100 条）。
- 影响：条目多的空间里，一部分回收站条目不出现在 GUI 列表中，也就无法从 GUI 恢复。
- 规避：用 CLI `hyprial org fs trash list <空间> --limit <N>` 列出，再 `hyprial org fs trash restore <空间> <节点>`。
- 决定：原有行为，登记；是否分页由 GUI 维护者决定。
- 依据：GUI 回收站改接时的查证。
- 负责人：GUI 组织页维护者。

**多节点验证中发现、不属于 OrgFS 改动的问题**

下面 KP-150 至 KP-156 是 OrgFS 精简实现的真实多节点验证中观察到、但原因在 OrgFS 之外的问题。**它们不是这次 OrgFS 改动引入的**，也不随这次改动修复；已报 owner。

## KP-150 一个对端被分区时，重启的节点连不上其他对端

- 现象：三节点 A、B、C，A 是 owner。停 C；A 被分区；再启动 C。C 在 60 秒（第二次试验 180 秒）内都连不上 B，读取 B 已有的文件一直失败；A 恢复后约 1 秒 C 就拿到了内容。同时观察到：全员在线时 C 也不主动拨任何对端，B 与 C 之间的连接只靠 B 一侧拨号。
- 影响：owner 不在时，重启的成员节点可能与其余成员失联。
- 规避：重启成员节点时保证 owner 在线。
- 决定：待查（传输层的对端发现；推断：重启的节点只经 owner 得知其他对端）。
- 依据：真实三节点验证第二轮。
- 负责人：传输与 tailnet 维护者。

## KP-151 侧车停顿约 3 分钟后，转发一直宕到 daemon 重启

- 现象：把一个节点的转发侧车（sidecar）冻结约 3 分钟后，daemon 先报转发应答超时、`forwarding.failed status-unreachable`，随后两次重启侧车都以 `start_failed MEMBERSHIP_STORE_INVALID` 失败，接着报 “sidecar relaunch budget exhausted; forwarding stays down until the daemon restarts”。之后该节点的转发状态为 failed，直到 daemon 重启。冻结不超过 20 秒时没有这个现象。
- 影响：侧车长时间卡住一次之后，节点长期失联。
- 规避：出现后重启 daemon。
- 决定：待查（侧车内部原因没有查）。
- 依据：真实三节点验证第二轮。
- 负责人：转发侧车维护者。

## KP-152 `org invite` 返回 `USER_DELIVERY_TIMEOUT` 但成员已经加入

- 现象：邀请成员时命令返回 `ok:false`、`USER_DELIVERY_TIMEOUT`，而成员实际上已经加入。
- 影响：调用方会把成功误判为失败，可能重复邀请。
- 规避：收到这个错误后先查成员表再决定是否重试。
- 决定：待查。
- 依据：真实三节点验证第二轮。
- 负责人：org 成员与用户投递维护者。

## KP-153 本机 IPC 请求超过 8 MiB 时直接断开，不回错误

- 现象：发给 daemon 的本机 IPC 请求超过 8 MiB 时，连接被直接关闭，客户端只看到 BrokenPipe，没有错误应答。
- 影响：调用方不知道失败原因，例如用 `org fs write` 写入很大的文本时。
- 规避：大文件用 `org fs import`（二进制，上限 64 MiB，经本地文件路径传递）。
- 决定：待查。
- 依据：真实三节点验证第二轮。
- 负责人：daemon IPC 维护者。

## KP-154 离开组织的请求在等待 owner 确认时没有日志

- 现象：成员离开组织后，owner 一侧把该请求记为“待确认”（自 2026-10-27 起离开需要 owner 确认才撤销成员资格），处理循环遇到未确认的请求直接跳过，不写任何日志。owner 上看不到任何 `org.leaves.*` 事件，成员资格也不撤销。
- 影响：看起来像是离开请求丢失了；验证者曾误判为 OrgFS 缺陷。
- 规避：owner 确认离开请求。
- 决定：待 org 负责人决定是否补一条“待确认”日志（需要避免每 30 秒重复打印）。
- 依据：真实三节点验证第二轮与随后的代码查证。
- 负责人：org 维护者。

## KP-155 真实 daemon 上 `org join` 以 `IDENTITY_CONFLICT` 失败

- 现象：`org join` 要求已验证的 OIDC 身份声明，而 dev 上没有代码给真实 daemon 提供它，所以真实 daemon 上的加入必然以 `IDENTITY_CONFLICT` 失败。多节点验证只能经测试钩子注入身份后进行。
- 影响：dev 上无法在真实 daemon 之间完成组织加入。
- 规避：无（测试中经钩子注入身份）。
- 决定：dev 上既有的问题，已报 owner，不在 OrgFS 改动里修。
- 依据：真实三节点验证第一轮与读码。
- 负责人：org 加入与身份维护者。

## KP-156 请求注册表把带数据的已声明错误改写成 `DAEMON_ERROR`

- 现象：处理函数抛出的错误码已在方法里声明、但带的数据与声明的 schema 不符（例如数值字段以字符串到达）时，注册表把整个错误改写为 `DAEMON_ERROR`（“error data violates its declared schema”），原始错误码与数据不进日志。
- 影响：调用方看不到真实的失败原因。与 KP-041 是同类现象。
- 规避：查 daemon 日志里处理函数一侧的事件。
- 决定：dev 上既有的问题，已报 owner，不在 OrgFS 改动里修。
- 依据：真实三节点验证第一轮的最小复现。
- 负责人：daemon 请求契约维护者。
