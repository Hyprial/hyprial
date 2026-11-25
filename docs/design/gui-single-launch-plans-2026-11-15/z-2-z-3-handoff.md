# 交接：Z-2 / Z-3 —— daemon 在无 owner 的 home 上也能启动，并在同一进程内完成“登录 → 服务 → 登出”

日期：2026-11-16。发起：GUI 单一启动线。状态：进行中（WIP）。

## 要解决的问题

今天 daemon 只能在已登录（有 owner）的 home 上启动。在一个无 owner 的隔离 home 上：

```
hyprial daemon start --json   →  ok:false, code DAEMON_START_FAILED，退出码 1
```

owner 已决定的目标模型是“daemon 是唯一常驻后端，`init` 与 `login` 解耦”：daemon 在任何 home 上都能启动，
无 owner 时处于 `bootstrap` 阶段，登录完成后在**同一进程内**进入服务阶段，登出后回到 `bootstrap`。
Z-1（#2850，已合入）提供了 bootstrap 阶段的 socket 宿主 `BootstrapWireHost`，但还没有任何调用者。
Z-2 把它接进 `daemon run`；Z-3 关闭由此暴露的崩溃恢复缺口。

## 必读（按此顺序，冲突时取前者）

1. [daemon 先行设计](../daemon-first-init-login-decoupling-2026-11-15.md)：§3.1、§3.2、§3.5、§3.6、§4.2（原型测量）、
   §5（native 引擎的影响）、§6 的 Z-2 与 Z-3 两行、**§6.3 清单**（Z-1 复核留给 Z-2 的五项，开工前逐条认领）。
2. 同目录 [`daemon-first.md`](daemon-first.md)：实施计划，含文件与行号。计划写于 Z-1 之前，行号已漂移，以当前 `dev` 为准。
3. 仓库根 `AGENTS.md`。
4. Z-1 的产物：`src/hyprial/daemon/impl/application/onboarding/bootstrap/{wire,dispatcher,peer}.py` 的文件头决策块。

## 范围

本交接分两个 PR，顺序完成。**本 PR 是 Z-2；Z-3 请在 Z-2 完成后从它的分支另开一个 PR。**

### Z-2（本 PR，分支 `feat/daemon-lifecycle-loop`）

| 项 | 今天 | 之后 |
|---|---|---|
| `daemon run` 在无 owner 或不存在的 home 上 | 拒绝启动 | 运行生命周期循环：bootstrap → promote → serve → logout → bootstrap，同一 PID |
| `daemon start` 遇到 `phase: bootstrap` | 视为失败 | 视为已启动 |
| `daemon status` 在 bootstrap | `CALLER_NOT_AUTHORIZED`（设计 A7） | 回退到 `ping`，如实报告 `running: true, phase: bootstrap` |
| `daemon stop` 在 bootstrap | 不可用 | `shutdown` 被服务，退出码 0 |
| `daemon run --bootstrap` | 终端登录循环 | 保留为被接受、不起作用的参数（OD-Z8） |
| 进入 bootstrap 之前 | 不检查引擎 | 验证 native 引擎可加载（`import hyprial._core.actors` 一次），损坏的安装在启动时失败 |

另外必须处理 §6.3 的第 1–4 项，以及第 5 项中标明属于 Z-2 的调用方：

1. KP-079：`kill -9` 之后陈旧的 `state/bootstrap.json` 使下一次准入失败。先写红测试再修。
2. `serve()` 返回 `initialize-unacknowledged` 之后循环进入哪个阶段（重新准入还是退出），要规定并有测试；
   包括“initialize 已被持久接受之后编码回复或 registry 抛异常”的分支。
3. `shutdown` 与已接受的 initialize 同时到达时的结果，要规定并有测试。
4. `serve()` 返回 `promote` 之后构造真正 `DaemonApplication` 并交接锁流的执行者：今天只在 GUI 路径
   （`GuiRuntimeHost.run_promoted`），循环要接上它。
5. 把 bootstrap daemon 当作 serving daemon 读取 `ping` 的调用方：`init.py`、`login/flow.py`、`logout`、`ps`、
   `upgrade` 的 restore 轮询、`doctor`、`socialware`。每一处要么改对，要么在 PR 正文说明为什么留给 Z-4 / Z-5。
   GUI 准入与 `request_http/server.py` 的 owner 检查属 Z-6a，**不在本交接范围**。

**从设计的 Z-2 行里拆出、不在本交接范围的一项**：§3.4 规则 1（阶段不可用一律报 `not-ready`，不再报 `disabled`）
及随之的 `contract/daemon-request` 改动。原因：它改的是 `daemon/impl/request_catalog/daemon/catalog.py` 与
`contract/daemon-request/openapi.json`，这两处正被“CLI → daemon request 14 组集成”逐组改动并重新生成。
该项只在 Z-6a / Z-6c 才被需要，由 GUI 线与 CLI 开发者另行安排。请不要在本 PR 里做。

### Z-3（Z-2 之后另开 PR）

serving 启动时，完成当前 owner 的非终态 initialize 回执，关闭 KP-059（设计 §3.6，owner 决定 OD-Z4：
回执的 `account_owner` 等于当前 owner 且设备记录存在时才完成）。崩溃矩阵：分别在 `awaiting-auth`、
`authenticating`、`promoting` 阶段 `kill -9`，下一次启动必须进入合法阶段，`onboarding.status` 如实。
改动预计在 `daemon/impl/application/onboarding/__init__.py` 与 `account_lifecycle.py`，约 +90 / −10 行源码。

## 测试（先失败再通过）

Z-2：

1. 红测试（集成）：隔离的无 owner home 上 `hyprial daemon start --json`，今天 `DAEMON_START_FAILED`；
   改后 `ok`，`daemon status` 显示 `running: true, phase: bootstrap`，`daemon stop` 退出码 0。
2. **最先运行**：用受控 identity 端口的自定义子进程入口，经产品循环复现设计 §4.2 的 B–E 步
   （登录 → promote → serve → logout → 回到 bootstrap → 再次登录），同一 PID。这同时是“双 promotion 在
   当前 native 引擎上是否仍成立”的测量（设计 §5.2 标为 UNKNOWN）。原型入口见
   `docs/notes/gui-single-launch-prototypes-2026-11-15/daemon-first-prototype/`，只作起点，不是产品代码。
   如果这一步在当前基线上不成立，**停下并在 PR 评论里报告**，不要绕过；设计 §3.5 给出了退路
   （进程退出，下一次启动进入 bootstrap）。
3. §6.3 第 1–3 项各有测试。
4. `owns_process_exit()` 置位时登出的行为有测试（设计风险 K1）。

Z-3：

1. 红测试：日志里有一条 `promoting` 的 initialize 回执且 home 有 owner → `on_daemon_ready` →
   今天 `onboarding.status` 报 `initialized: false`，期望 `true`。
2. 上述三阶段崩溃矩阵。

启动真实 application 的测试必须用 native 引擎（发布内容就是它），不用 `HYPRIAL_ACTOR_BACKEND=reference-abi` 代替。
测试目录每目录 ≤ 7 个直接文件、每文件 ≤ 1000 行。

## 与其他在途工作的关系

- **CLI → daemon request 14 组集成**（另一位开发者，逐组 PR）：不要改
  `src/hyprial/daemon/impl/request_http/**`、`src/hyprial/daemon/impl/http_contract/**`、kernel `ipc_errors.py`、
  `src/hyprial/shell/impl/cli/commands/common/support.py`、`contract/daemon-request/openapi.json`。
  `daemon/impl/request_catalog/**` 也在他们的改动范围内，本交接不需要动它。若发现必须改上述任何一处，
  停下并在 PR 评论里说明。方法一经声明，在 bootstrap 阶段的返回码是 `ACCOUNT_REQUIRED`（Z-1 已实现），不要改这个语义。
- **GUI 线 PR 3**（进行中）：改 `src/hyprial/shell/impl/gui/**` 与 `gui/**`。本交接不碰这两处。
  `daemon/impl/application/onboarding/gui_host.py` 的 `run_promoted` 如需搬到 daemon 侧共用，可以搬，
  但 GUI 路径的行为与其测试必须保持通过。
- **#2830（Z-8a–c，每个 home 一个 OS 服务）**：暂缓，未合入。本交接不依赖它，也不要在本 PR 里注册或改动 OS 服务。
- 冲突时以普通 merge 同步 `dev`，不 force-push，不改写历史。

## 红线

- 绝不读取、改写、启动或停止真实 `~/.hyprial` 下的 daemon、真实 OS 服务或凭据；home、socket、端口全部隔离。
- 真实 OIDC 登录在测试环境不可用：用受控 identity 端口；真实登录写 BLOCKED，不写 PASS。
- 不用 skip / xfail、自动重试、删除测试、放宽断言、扩大 allowlist 或单纯延长 timeout 来变绿。
- CLI 输出只经 `shell/impl/cli/output`；不新增行内注释（决策写文件头决策块，docstring 保留）；一律 `uv`，Python 3.12。
- 已发布字段语义变化（JSON 键、错误码、事件名，例如 `daemon status` 在 bootstrap 的输出、`daemon start` 的结果）
  要同步 `contract/`，或在 PR 正文写 `Contract-Impact:` 说明。
- Windows：不删除已有的 Windows 测试与门禁；Windows 独有的问题只记录，不处理。
- `docs/notes/**` 只追加，不修改已有文件。
- 更新 `docs/reference/known-problems.md`：KP-079（Z-2）与 KP-059（Z-3）只更新原条目的状态，不删除。

## 验证命令

```bash
uv sync --extra test --python 3.12
uv run pytest -q tests/daemon/onboarding tests/daemon/application tests/shell/cli/daemon   # 以及你改动到的其他目录
uv run ruff check .
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
./contract/run-all.sh --isolated-only        # 若改动了 contract/
uv run --with libcst==1.9.0 pytest tests/
```

全量 pytest 的失败项对照 `docs/reference/dev-red-baseline-2026-11-15.md`（含 4.1 节的偶发项）归因，写进 PR 正文；
基线内的失败不算新缺陷，但须列出，UNKNOWN 如实写 UNKNOWN。

## 完成时请这样交回

1. 普通提交并推送到本 PR 的分支 `feat/daemon-lifecycle-loop`（不 force-push）。
2. Z-2 完成后：去掉本 PR 标题里的 `WIP:`，并在本 PR 评论写明“已完成”，列出：改动文件、§6.3 五项各自的处理结果、
   各验证命令的真实结果、未完成或未测项（标 UNKNOWN / BLOCKED）。
3. Z-3：从 Z-2 的分支另开 PR（标题以 `WIP:` 开头），完成后同样去掉 `WIP:` 并评论“已完成”。
4. 进行中遇到需要 owner 或 GUI 线裁定的问题，在 PR 评论里提出，不要自行扩大范围。

GUI 线以“标题去掉 WIP + 完成评论”作为接手审查与合入的信号。合入由 GUI 线执行，开发者不自行合入。
