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
⚠️ **跑 `e2e-controlled.yml` 闸门 ≠ 跑完 G1** —— 闸门矩阵不含 E2E-011 / E2E-007。

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
或整条 credentialed 闸门:`fj actions dispatch e2e-controlled.yml <当前 dev tip 或 tag>`。

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

## 维护规则

- **每次发版必须过一遍本文件**(发版是天然的触发点,不设"定期维护" —— 定期维护的东西一定会烂);
- 一条目标完成后**不要删**:改成 `✅ 已完成(日期)` 并保留一行状态;实测记录归 `docs/notes/roadmap/`（WORM 证据区）,条目内只留状态与证据指针,证据链不断;
- 新增目标必须回答:**"如果所有人都联系不上,拿到这个文件的人能不能执行?"** 答不上就不算写完。

---

## G1 · 状态与证据

✅ **G1 第 1 轮（`v0.4.2.dev7`，2026-08-23）完成**：E2E-004 PASS 16/16（`e2e-controlled.yml` #505）、
E2E-011 PASS 16/16、E2E-007 PASS 6/6×3（后两者 `e2e.yml` #497 + 本机隔离复核）。
完整实测记录（含 BLOCKED 误判史、flake 特征、现场路径）：[docs/notes/roadmap/g1-execution-log-2026-08-23.md](docs/notes/roadmap/g1-execution-log-2026-08-23.md)。

⬜ **G1 第 2 轮（dev8 / 下一次发版）未开始** —— 触发条件是下一次发版,不是日期。

> ⚠️ 给下一个执行的人 —— **两条闸门,别混为一谈**:
> - **`e2e.yml`**(免凭据,tag 触发的发版准入)跑 **E2E-007 / E2E-011**,还有 001/002/003/005/010;
> - **`e2e-controlled.yml`**(要真凭据)跑 **E2E-004** / 006 / 008 / 012 / 013 / 014。
>
> ⇒ **G1 三条分布在两条闸门上。** 想确认 G1,两条闸门在**同一个 tag 上**都要绿;
> 手动补跑时**先确认隔离**(`HYPRIAL_HOME` / `HARNESS_STATE_DIR` / 端口 / accounts stub),
> 否则 E2E-007 会把四个 Lark adapter 起到生产 daemon 上。
