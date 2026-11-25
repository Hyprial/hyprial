# ROADMAP.md — 长期目标与发版义务

**这个文件的存在理由:这里的每一条都必须在【协调者失联、worker 失联、消息通道不通】的情况下,仍然被找得到、看得懂、跑得起来。**

任何依赖"某个 agent 记得"的目标,不算目标。写在这里,并且写成**任何人拿到就能执行**的形式。

> 失联韧性作为工作原则（为什么关键信息必须落仓库、如何留证据链）定义在
> [AGENTS.md](AGENTS.md)；本文件只承载目标与义务本身——每条自含触发条件、
> 命令摘要、判据、红线和状态；实测记录归 `docs/notes/roadmap/`（WORM），条目内留指针。
> （原名 GOALS.md，2026-10-01 拆分更名。）

---

## G1 · 发版后必须跑一次含 lark-live 的 E2E

**状态:未完成。触发条件已部分满足(`v0.4.2.dev7` 已发)。**

### 为什么(Allen 2026-08-23 亲点)
> "本次两轮升级都没有进行 lark-live 的测试…注意这个任务应该设置为 goal,
> **以免升级到 dev8 后有问题,你联系不上我或者其它 worker**"

⇒ **飞书通道是人和 agent 之间唯一的实时链路。** 如果一次升级把它弄坏了:
- 我(协调者)**发不出告警**;
- 你**下不了指令**;
- 而**验证这条通道的任务本身,不能依赖这条通道来传达** —— 所以它写在这里,不在消息里。

### 什么时候跑
- ✅ `v0.4.2.dev7` 发布后 —— **第 1 轮三条已全部实测通过**(见文末「状态与证据」);
- ⬜ `v0.4.2.dev8` 发布后。
**两次都要跑,不能只跑一次。**
⚠️ **跑 `e2e-macos-credentialed.yml` 闸门 ≠ 跑完 G1** —— 闸门矩阵不含 E2E-011 / E2E-007。

### 怎么跑(照抄即可)
```bash
cd <harness-bridge 检出>
# E2E-004:真实飞书用户 ↔ Python adapter 往返(real_environment_required = True)
uv run --locked --python 3.12 python contract/e2e/run-feishu-roundtrip.py

# E2E-011:Lark App onboarding + 一键授权(real_environment_required = True)
uv run --locked --python 3.12 python contract/e2e/run-lark-app-onboarding.py

# E2E-007:四个 Lark adapter 恢复 online(不需要真实凭据,但同族,一并跑)
uv run --locked --python 3.12 python contract/e2e/run-four-lark-adapters.py
```
或整条 credentialed 闸门:`fj actions dispatch e2e-macos-credentialed.yml <当前 dev tip 或 tag>`。

### 通过判据
- **E2E-004**:用户请求到达预期 actor(OnIt 之前);adapter 回复是**带路由头的原生 reply**;
  **OnIt 只在 reply 被接受后才清除**;
- **E2E-011**:环境凭据短路 onboarding(**零次 accounts 端点请求**);非交互式 onboarding 以结构化 `USER_A…` 拒绝;
- **E2E-007**:迁移 dry-run 与 apply **secret-safe**;四个 adapter 启动后 online;`ps` 报四个;desired state 有记录。

### ⛔ 红线(违反即视为未完成)
- **绝不打印 `app_secret` / `access_token` / `client_secret`**;
- **不碰生产 Lark App 的配置**;
- E2E-004 / E2E-011 需要真实凭据 —— **凭据缺失时不要自行铸造**,记为 BLOCKED 并写明缺什么。

### 如果失败了,而且联系不上人
1. **不要回滚 tag**(tag 已推送 = autoupdate 已全网生效,回滚制造第二个不一致状态);
2. 把现场留下:`contract/e2e` 的 runner 会保留失败态,**别清理**;
3. 在 `docs/notes/roadmap/` 追加实测记录文件（WORM，只追加不改写），写清:版本、跑了哪几个、哪个失败、失败断言、现场路径;并在本条目的「状态与证据」节更新一行状态与指针;
4. **提一个 PR 把这段写进仓库** —— 那是失联时唯一还能送达的通道。

---

## G2 · daemon upgrade 步骤与 mcp 旧 argv 迁移

**状态:实现中（2026-10-20）。**

触发条件：任何包含「删除顶层 `upgrade`/`mcp` 命令、daemon 升级步骤」的版本合并后。

### 为什么
旧 Claude/agent 会话持久化了 `python -m hyprial.cli mcp ...` 的 MCP 配置和
SessionStart/StopFailure hooks。editable/升级切换代码后，活进程内新 spawn 仍用
旧 argv 并 exit 2；旧 launchd/systemd autoupdate 单元调用 `hyprial upgrade run`
也会静默失败。迁移必须在删除命令的同一版本执行。

### 执行（照抄）
```bash
# 1. 新版代码生效后执行统一升级步骤（不再由 daemon start 自动执行）
hyprial daemon upgrade
# 若 daemon 未运行或只需重跑步骤：
hyprial doctor --fix
# 2. 立即 relaunch 全部 agent 会话（旧 hooks/MCP 重连才会换新 argv）
# 3. 直到 legacy 文件与活进程计数都为 0
hyprial doctor --json
```
Step1 = `mcp-entry-v1`（精确旧前缀改写 + 活进程只读扫描）；Step2 =
`autoupdate-timer-v1`（精确 autoupdate label；绝不碰 `com.hyprial.daemon`）。
步骤 marker 在 `<state>/upgrade-steps/`，失败不阻启动，doctor/`daemon upgrade
status` 显示 pending，可重试。

### 红线
- 不保留任何旧命令别名；不自动 kill 活进程；
- Step2 只按 exact label 移除 autoupdate timer，不得匹配 daemon 单元；
- 部署批次需 Allen 终端授权；升级后未 relaunch 的会话视为未完成迁移。

---

## G3 · plain daemon stop survivor observation

**状态：计划在 0.5.6 处理。**

`hyprial daemon stop` 只等待 teardown receipt；若旧进程仍在退出，命令把 PID
作为 `survivingPid` 事实返回，不等待完整的 process-exit budget。0.5.5 不改变这项
已测量的操作员语义，因为普通调用方约 20 秒的预算不足以覆盖 teardown 加退出等待。

已知缺口：bare operator stop 返回后，目前没有组件读取 `survivingPid` 并继续观察或告警。
0.5.6 的后续工作必须在不延长 operator stop 临界路径的前提下，消费该字段并对真正的
长期 survivor 形成有界观察与可见告警。restart、upgrade 及任何即将启动 replacement
的路径必须继续使用 `require_exit=True`，不得借此放宽。

验收至少覆盖：operator stop 在 teardown 后立即返回 survivor fact；延迟退出不误报；
长期 survivor 可被观察并告警；restart/upgrade 仍要求 exit proof。焦点回归：

```bash
HYPRIAL_NETWORK_ISOLATED=1 uv run --python 3.12 pytest -q \
  tests/daemon/application/supervision/test_daemon_restart_lock.py \
  tests/shell/cli/lifecycle/test_cli_restart.py \
  tests/shell/cli/installation/restart/test_failure_markers.py
```

---

## G4 · dev CI 稳定性闭环与残余问题归属

**状态：执行中，尚未验收（2026-11-14）。** 用户已批准本任务方案并要求持续推进；
[#2727](https://code.hyprial.com/HyprialOS/hyprial/pulls/2727) 已进入 dev，当前整合基线为
`2a494edccdee0135b0a4dbb6fd7f7cf755b803f0`。旧基线的红绿证据保留其原 SHA，
不能改写为新基线实测。设计见 [WIP #2764](https://code.hyprial.com/HyprialOS/hyprial/pulls/2764)
与 [CI 稳定化设计](docs/design/dev-ci-stability-2026-11-14.md)；
原始现场与后续追加记录见 [本轮证据](docs/notes/dev-ci-stability-2026-11-14/README.md)。

### 触发与范围

本轮 dev 的 worker crash、daemon 启动无进展、分片 max-time、settlement/help
teardown、freshness/Git 环境依赖、isolated contracts、原生引擎及 3.15t 已触发本目标。
任一未归属失败、缺少完整 CI 终态或残余 KP 缺少决定/owner，目标继续执行；
协调者失联时，接手者从本条、设计、known-problems 和最新 Forgejo job 接续，不重新猜测旧现场。

### 执行步骤与命令摘要

1. 获取最新 dev 完整 SHA；重读 AGENTS/GLOSSARY/known-problems，核对关联 PR 的合入状态。
   每类故障独立短分支和小 PR，目标 dev；保留 base/head、runner/OS/arch/Python、命令、
   同 SHA 样本数、失败数、job URL与原始日志。测试名不是 crash 根因，缺 signal/OOM/栈时写 UNKNOWN。
2. 在隔离 HOME/socket/port/cache 中先复现，再修原因。产品行为变化先红后绿；测试修复
   保留原业务不变量，靠明确完成事件、持久化边界和资源所有权消除竞态。修改产品/测试结构先运行：

   ```bash
   uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
   uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
   ```

   任何代码变更先跑精确焦点；`src/hyprial/` 变化按 AGENTS 追加：

   ```bash
   uv run --with libcst==1.9.0 pytest tests/
   uv run ruff check .
   ```

   C1/跨实现契约修复必须跑完所有段，不能以前序失败遮住后续结果：

   ```bash
   ./contract/run-all.sh --isolated-only
   ```

3. Sol 独立检查最终 diff/精确 head、关键红绿、负例和禁区；Astra协调跨模块权威边界与
   共享 fixture 的唯一 owner；Terra完成小 PR施工。相关失败必须修复或明确 BLOCKED，
   不把本地通过冒充真实平台 CI。macOS arm64引擎门继续修；Linux arm64只调查、不修、不删，
   待Allen平台决定。既有Linux arm64三连PASS与其他SHA取消/等待分别保留。
4. 合入后固定最终 dev 版本及测试/workflow/依赖/runner配置，在 Forgejo Actions 对该冻结ref
   显式触发 `gate-python.yml` 的 `workflow_dispatch`，核对plan为full、全部checks/unit及四个pytest
   shards有终态；同时执行适用的GUI/契约/原生引擎门并保存清单。按当前
   `native-conformance.yml`核对reference-abi，以及各原生平台三次CONFORMANCE PASS；
   3.15t按既有非阻断探针单列结果。重复完整周期五次，任何代码/测试/workflow/依赖/runner配置变化
   后重新建立可比窗口，不用retry覆盖失败，不以branch protection的最小contexts代替全矩阵。
5. 如五次全绿不能取得，在known-problems逐项写现象、影响、已验证规避（没有则写无）、
   决定、证据、负责人，明确FAIL/BLOCKED/UNKNOWN与缺失次数；证据只追加，不删除历史失败。
   新编号先核对最新dev，避免重号。完成后更新本条状态，保留证据和PR链。

### 验收判据

- 同一可比最终版本五次完整 CI 全绿，含每次run/job、SHA、矩阵、终态与时长；pending/cancelled/
  skipped不能填PASS，3.15t既有非阻断结果仍须披露；**或**所有残余KP都具备上述六项且owner/决定明确。
- 本地焦点、单分片、一次native三连或强行合入均不等于五次完整CI通过。
- 残余KP不自动关闭required checks，不意味着产品已稳定；完成报告明确采用哪条验收分支。

### 红线与本轮合入授权

- 不新增skip、xfail、自动retry，不放宽业务断言，不无证调高预算，不修改branch protection；
  证据支持的下载/runner/启动预算设计可独立提出，workflow仍由授权维护者合入。
- 不修改`gui/`、`src/hyprial/shell/impl/gui/`、`clients/`、`packaging/`。external promotion、
  GUI signals、daemon-request-browser、reference代理相关两入口按用户决定随GUI单一启动改造移除，
  本轮不修、不加skip；dev-browser-parity只核#2758已修。crates/conformance引擎门不随旧壳删除。
- 不读改清理生产daemon、凭据、Lark或用户配置；只能结束本例自有进程。真实环境缺失写BLOCKED。
- 用户2026-11-14新增授权：**本任务已由Sol独立验收的小PR，若仅被无关失败阻断，允许主协调者
  强行合入dev。** 这不是跳过相关失败的授权，也不适用于未经本任务验收的其他PR。每次须记录
  精确base/head、被判无关的job与对照证据、局部验收、授权依据、实际merge结果；不修改分支保护，
  合入不涂绿失败。API/权限拒绝仍如实BLOCKED，不能伪报已合入。

### 负责人和当前证据

主协调者负责Forgejo小PR、合入审计与最终闭环；Astra负责架构/协同，Sol负责独立验收，Terra负责施工。
平台决定由Allen负责；具体残余项由known-problems中的模块/runner责任role承接。
已取得旧基线若干局部候选红绿与独立验收，真实macOS官方依赖获取/三连conformance仍BLOCKED，
新基线五次完整CI窗口尚未建立。本目标继续执行，不能宣告完成。

---

## 维护规则

- **每次发版必须过一遍本文件**(发版是天然的触发点,不设"定期维护" —— 定期维护的东西一定会烂);
- 一条目标完成后**不要删**:改成 `✅ 已完成(日期)` 并保留一行状态;实测记录归 `docs/notes/roadmap/`（WORM 证据区）,条目内只留状态与证据指针,证据链不断;
- 新增目标必须回答:**"如果所有人都联系不上,拿到这个文件的人能不能执行?"** 答不上就不算写完。

---

## G1 · 状态与证据

✅ **G1 第 1 轮（`v0.4.2.dev7`，2026-08-23）完成**：E2E-004 PASS 16/16（`e2e-macos-credentialed.yml` #505）、
E2E-011 PASS 16/16、E2E-007 PASS 6/6×3（后两者 `e2e-macos-isolated.yml` #497 + 本机隔离复核）。
完整实测记录（含 BLOCKED 误判史、flake 特征、现场路径）：[docs/notes/roadmap/g1-execution-log-2026-08-23.md](docs/notes/roadmap/g1-execution-log-2026-08-23.md)。

⬜ **G1 第 2 轮（dev8 / 下一次发版）未开始** —— 触发条件是下一次发版,不是日期。

> ⚠️ 给下一个执行的人 —— **两条闸门,别混为一谈**:
> - **`e2e-macos-isolated.yml`**(免凭据,tag 触发的发版准入)跑 **E2E-007 / E2E-011**,还有 001/002/003/005/010;
> - **`e2e-macos-credentialed.yml`**(要真凭据)跑 **E2E-004** / 006 / 008 / 012 / 013 / 014。
>
> ⇒ **G1 三条分布在两条闸门上。** 想确认 G1,两条闸门在**同一个 tag 上**都要绿;
> 手动补跑时**先确认隔离**(`HYPRIAL_HOME` / `HARNESS_STATE_DIR` / 端口 / accounts stub),
> 否则 E2E-007 会把四个 Lark adapter 起到生产 daemon 上。
