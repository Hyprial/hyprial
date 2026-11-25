# Z-8 交接：每个 HYPRIAL_HOME 一个 OS 服务

日期：2026-11-15。状态：交接给有 macOS 实机的开发者实现；本分支
`feat/per-home-os-service` 是工作分支，PR 保持 WIP 直到三步都完成。
审查与合并由 GUI 单一启动线负责。

设计依据：[daemon 先行设计](daemon-first-init-login-decoupling-2026-11-15.md)
§3.7（每个 home 的 OS 服务）、§3.8（首次运行 ensure）、§5（actor-rustify 的影响）、
§6（PR 序列）。本文只写交接所需的范围、边界和验收，不重复设计理由。

## 1. 为什么交给有 Mac 的人

launchd 的真实行为（注册、`KeepAlive` 重启、重启电脑后恢复、卸载）在 Linux 沙箱里
无法验证，设计文档里这些项都标为 BLOCKED。单元测试可以在任何平台写，但本工作的
验收必须包含 macOS 实机记录。

## 2. 范围：三步，按顺序，各自一个或多个可测试的提交

| 步 | 内容 | 主要文件 |
|---|---|---|
| Z-8a | 把 `src/hyprial/daemon/impl/operations/service.py`（996 行，所在目录已有 7 个直接文件）**机械拆分**为包 `operations/service/`：`types.py`、`templates.py`、`identity.py`、`supervisor.py`、`manager.py`。只移动，不改行为；公共导入面不变 | `daemon/impl/operations/**` |
| Z-8b | 非默认 home 的单元名：默认 home（`$HOME/.hyprial`）保留 `com.hyprial.hyprial.daemon` / `hyprial.service`；其他 home 用 `com.hyprial.hyprial.daemon.<id>` / `hyprial-<id>.service`，`<id>` 为 home 解析路径 sha256 的前 12 位十六进制。移除“非默认 home 拒绝”（`service-home-nondefault`）。程序路径：没有 console script 解析到本安装时，渲染 `[sys.executable, "-m", "hyprial.cli", "daemon", "run"]`；`launchd_jobs.py` 的 job 扫描器认识该形态并按单元的 `HYPRIAL_HOME` 匹配。安装单元时把代理环境变量渲染进单元（待 owner 确认，见 §6） | `operations/service/**`、`daemon/impl/configuration/launchd_jobs.py`、`shell/impl/cli/commands/daemon/service.py`、模板、`contract/daemon-lifecycle/service-supervision.md` |
| Z-8c | 唯一的启动原语（`shell/impl/cli/commands/common/daemon_start.py` 的 `_launch_daemon_process`）在允许时先安装单元再经管理器启动，否则保持今天的 detached spawn。新增 opt-out 环境变量 `HYPRIAL_SERVICE_AUTOINSTALL=0` 与 `HYPRIAL_DAEMON_AUTOSTART=0`，由 `tests/conftest.py` 的 autouse fixture 与 `tests/support/runtime/environment.py` 的 `isolated_environment()` 设置；产品代码永不设置 | `daemon_start.py`、`tests/conftest.py`、`tests/support/runtime/environment.py` |

退出策略（`KeepAlive true`、`Restart=always`）**不变**。不新增 CLI 命令。

## 3. 不要碰

- `src/hyprial/shell/impl/request_http/**`、`src/hyprial/shell/impl/http_contract/**`、
  kernel IPC、CLI 请求函数（`cli/commands/common/support.py`）：CLI 收敛工作正在改。
- `src/hyprial/shell/impl/gui/**`、`gui/**`、`contract/gui-session/**`：GUI host 线正在改。
- GUI 不安装服务（2026-11-16，DL-1 其后落地）：`hyprial gui` 的 ensure hook 传
  `autoinstall=False`；安装仍归 `init` / `daemon service install` / 显式 CLI 路径。
- daemon 的 bootstrap/onboarding 运行时（Z-1…Z-3 的范围）。
- Windows：本轮不做，不新增 Windows 分支或测试；现有 Windows 可移植性检查保持通过。

## 4. 测试安全（必须保持，审查重点）

测试用临时 `HOME` 运行，但 launchd/systemd 的用户域不受 `HOME` 隔离。移除非默认 home
拒绝之后，下面三道保护必须仍然成立，并各有测试：

1. operating home 之外的 home 被拒。
2. `HOME` 与账号 passwd home 不同时，拒绝一切真实管理器命令
   （`service-account-home-mismatch`）。这是真正保护伪造 `HOME` 的测试的那一道。
3. 对真实单元目录的 tripwire（`tests/support/runtime/environment.py`）。

单元测试继续用 fake runner，不调用真实的 `launchctl` / `systemctl`。任何一个测试在开发者
机器或 CI 上注册了真实服务，都是阻断问题。

## 5. 验收

仓库内（任何平台）：

- 每步有先失败后通过的测试；Z-8a 以现有测试全绿为准。
- `uv run --with libcst==1.9.0 pytest tests/`（全量，本工作改产品 Python）；与目标分支上
  已知的固有失败对照，逐项说明。
- `uv run ruff check .`；`scripts/ci/check_structural_limits.py`（每目录 ≤7 个直接文件、
  每文件 ≤1000 行）；`check_module_boundaries.py`；`check_inline_comments.py --strict`
  （新代码不写行内注释，决策写文件头）；`./contract/run-all.sh --isolated-only`
  （报告退出码与 RESULT 行）；`scripts/verify/check_contract_impact.py`。
- 改了 `contract/daemon-lifecycle/service-supervision.md` 的命名规则，PR 正文写
  `Contract-Impact:`。

macOS arm64 实机记录（写进 PR 正文或 `docs/evidence/`，逐项 PASS / FAIL / BLOCKED，
附命令与输出摘要；用一个专门的测试账号或专门的非默认 home，不要动日常使用的
`~/.hyprial` 与其服务）：

1. 默认 home：`daemon service install` → `launchctl print` 显示原标识；行为与改动前一致。
2. 非默认 home：安装后标识为 `com.hyprial.hyprial.daemon.<id>`，plist 里的
   `HYPRIAL_HOME` 正确；与默认 home 的服务同时存在、互不影响（各自的 socket、锁）。
3. 杀掉 daemon 进程后被 launchd 拉起；`daemon stop` / `daemon start` / `daemon restart`
   经管理器生效。
4. 重启电脑并登录后 daemon 自动回来。
5. `daemon service uninstall` 后单元文件与 launchd 中的 job 都消失。
6. 程序路径为 module 形式（没有 console script 时）的单元能启动，且加载的是该安装自己的
   `hyprial._core`（Rust 引擎；与解释器同一次构建）。
7. Z-8c：全新的非默认 home 上第一次 `daemon start` 注册并启动单元；设置
   `HYPRIAL_SERVICE_AUTOINSTALL=0` 时不注册，退回 detached spawn。
8. socket 路径长度：macOS 的 AF_UNIX 上限是 104 字节。用一个路径较长的 home
   （例如 `~/Library/Application Support/com.hyprial.hyprialos/home`）实测能否启动，
   失败时的报错是什么。

Linux（有 systemd 用户会话的机器，若有条件）：对应的 1、2、3、5、7 项。没有条件就写
UNKNOWN，不要写 PASS。

## 6. 待 owner 确认的默认值

实现按默认值做，PR 正文列出；owner 改口径时再调整。

- 首次运行不询问就注册服务（macOS 与有用户会话的 Linux；失败不致命；提示一次）。
- 代理环境变量在安装时渲染进单元。
- 非默认 home 的单元名用 home 路径的哈希，而不是 app 的 bundle id。

## 7. 已知未决（遇到请记录，不要自行决定）

- macOS App Translocation：从下载目录直接运行的 app 路径是随机只读路径，单元若记录该路径
  之后会失效。§3.8 的规则是“程序路径不稳定时不安装单元”。如何判定“稳定”需要实测。
- 从 GUI app 启动的 daemon 继承 app 的窄 `PATH`；服务模板里的 `PATH` 更宽。
- 卸载 app 后留下的单元（指向已删除的 bundle）会每 5–60 秒失败一次，需要一个显式的
  “移除后台服务”动作；不在本工作范围，记录即可。
- 同一账号在两个 home（终端安装与 app）同时登录时网络侧的表现未实测。

## 8. launchd 验收后的修复轮（2026-11-16）

验收报告（#2830 评论 “launchd 验收已完成”）的 D1–D3 与 F1–F6 在本 PR 内处理，决定与
根因见 [实施计划 §9](z8-per-home-service-implementation-plan-2026-11-15.md)。边界不变：
修复只落在 `daemon/impl/operations/service/**`、`daemon/impl/configuration/launchd_jobs.py`、
`shell/impl/cli/commands/daemon/**` 与 `shell/impl/cli/commands/common/` 下的
`daemon_start.py`、`daemon_stop.py`、`pending_restart.py`；`common/support.py` 与 CLI 请求
函数仍不碰（detached 停止走 `daemon_stop._request_shutdown` 这一个既有 shutdown 请求点）。

- 已知问题编号：本分支占用 KP-081、KP-082、KP-087 至 KP-090、KP-092、KP-093（KP-083 至 KP-086 已由 dev 的 Z-2 占用；KP-091 留给另一条分支；本分支原 KP-086 已改号为 KP-093）。
- 修复后的 macOS 复验清单由复验评论给出（D1、D2、D3、F2、F4 各自的步骤与应见输出）；
  真实 launchd/systemd 行为在本仓库内只有 fake 覆盖，复验前一律是 UNKNOWN。
