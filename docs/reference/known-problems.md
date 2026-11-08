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
| KP-008 | `message.send.lookup` 走 registry 派发，网络门 IPC root（router `handle`）看不到它 | 待修（0.5.5.4，网络门支持声明 handler root） |
| KP-009 | `test_four_sides_cover_a_pure_heartbeat_load` 在本机（Apple Silicon）单独跑常红 | 不修（机器敏感，CI 绿） |
| KP-010 | `test_close_finishes_all_teardown_after_active_ipc_worker_times_out` 本机偶发：sentinel 错误先于 IPC client 超时错误浮出 | 不修（时序竞争，基线同分布） |
| KP-011 | `test_shutdown_fails_loud_when_active_handle_exceeds_bound` 本机偶发 DID NOT RAISE：active client worker 的 0.05s 超时窗与线程退出竞争 | 不修（时序竞争，基线同分布） |
| KP-015 | 本机受限执行环境中 hosted readiness 的 spawn-error 测试在 preexec 阶段失败 | 待测（本轮不修） |
| KP-016 | Claude headless connector 报 running=true 但 pid=null，使严格 ready 谓词无法满足 | 待修（独立缺陷，本轮不修） |
| KP-017 | 合成 Anthropic provider 的 message_start 缺少 model，真实 Claude SDK 读 turn 失败 | 待修（测试 fixture，本轮不修） |
| KP-018 | 受限 macOS 全量验证存在共同基线失败与顺序相关超时 | 待测（本轮不修，整套不得判 PASS） |
| KP-019 | 隔离全量 pytest 的关闭收尾出现长耗时与 teardown 错误，独立单例不复现 | 待测（本单留证，不改日志/测试基础设施） |

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

## KP-008 `message.send.lookup` 在网络门的 IPC root 之外

- 现象：#2246 把 `message.send.lookup` 声明为 MethodSpec 后，派发走 registry（`handle` 前段的 `METHOD_REGISTRY.invoke`），legacy 链不再有可见分支；网络门（`scripts/ci/check_network_reachability.py`）只从 `router.handle` 的函数体解析调用边，看不到该方法的 handler。`tests/tooling/architecture/network/test_network_reachability.py::test_send_and_lookup_remain_visible_to_network_gate` 在 #2246 自身分支上即为红。
- 影响：当前 `_ipc_message_send_lookup` 只读本地 SQLite（`self._inbox.lookup_send`），无网络边界，无实际盲区。若未来该方法子树引入网络效应，网络门将无法从 IPC root 看到它。
- 规避：GUI 第一轮进 release 时（#2254）把该测试改为钉住真实派发链（registry 绑定 handler → `app._ipc_message_send_lookup`），`message.send` 的可见性断言不变；网络效应若要进入 lookup 子树，必须先修本条。0.5.5.3 train（`infra-ops/intg-rel0553-train-r4`）把 `request_catalog` 拆出 `daemon/` 子包后，被钉住的 handler 键为 `hyprial.daemon.impl.request_catalog.daemon.message_lookup.message_send_lookup`；0.5.5.4 修网络门 root 规则时一并回看该键。
- 决定：待修。0.5.5.4 让网络门把声明的 handler 也作为 root（或等价的 root 规则扩展），届时恢复 root 级可见性断言。
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
