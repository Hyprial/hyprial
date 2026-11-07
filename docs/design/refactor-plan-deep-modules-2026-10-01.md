# 深模块重构方案：kernel / identity / daemon / biz / shell

- 日期：2026-10-01
- 基线：dev @ f862424c（354 个 .py / 212,872 行 / 27 包 + 46 顶层散文件；tests 708 个文件）
- 状态：分析与方案；**已经 C0 扫描证据修订**（见下）
- 方法：Ousterhout 深模块（模块少、接口远小于实现）+ 8 轮 grill 裁决
- **修订关系**：本文件是原方案（领域划分与裁决记录仍然有效）；[`refactor-c0-and-tooling-2026-10-01.md`](refactor-c0-and-tooling-2026-10-01.md)（同目录，GPT-6 规划模型 + 快模型 C0 扫描）是** governing 修订**——凡两处数字/顺序冲突，以 C0 实测为准。主要修订见 §0 第 11 行与各节的"⚠ C0 修订"注记。

---

## 0. 决策记录（grill 裁决汇总）

| # | 问题 | 裁决 |
|---|---|---|
| 1 | import 路径兼容策略 | **一次性破坏，不留 shim**；仅保留 `hyprial.cli:main` 入口与 `python -m hyprial.cli` 两个对外契约 |
| 2 | 模块划分轴心 | 不按现有目录名，按业务语义；一级模块个数压到 5 |
| 3 | 消息脊柱归属 | inbox/transport/mcp 属 daemon（design.md：daemon 是消息与恢复的权威边界） |
| 4 | biz 判据 | 删除测试：删掉后平台核心承诺（消息连续、派单、agent 存活、组织文档）仍成立 → biz |
| 5 | 五模块分类 | kernel（基础核心依赖）/ daemon（平台层）/ identity（agent、user 封装）/ shell（人机交互接口）/ biz（业务封装：usage、hooks 等） |
| 6 | 依赖方向 | **kernel ← identity ← daemon ← biz ← shell**，单向；daemon → identity 是合法方向（流量比 41:11） |
| 7 | 上帝文件 | 借大搬家顺手拆 cli.py 与 application.py（一次剧痛代替两次） |
| 8 | interface/impl | 每模块一个 `interface.py` 公共面，`__init__.py` 仅 `from .interface import *` 转发；实现全部进 `impl/`；gate 静态扫描"import 路径含 `.impl` 即违规"（src 内跨模块执法；tests 首轮豁免；contract 黑盒必须只走公共面） |
| 9 | 附带文件 | 一次性脚本 → `scripts/archived/`；tests 按新模块归堆；spikes/zenoh 修正为存活探针 → `scripts/verify/zenoh-capability/`；docs/ 收拢为 design/guides/reference/domains/evidence 五桶 + 321 处链接 codemod 改写；**notes/ 移入 `docs/notes/`**（WORM 内部结构保留，链接改写）；顶层目录按 §5.4 裁决，src/gui、contract 等独立工程不动 |
| 10 | CI 仓库清洁 | 新增 repo 布局 gate（沿用 ci_workflow_policy 模式）：顶层条目 fail-closed 白名单；禁止重现 `docs/`/`notes/` 双顶层目录、禁止新增根级 md、新顶层目录必须同步登记 ci-scope 映射 |
| 11 | C0 证据修订（GPT-6 + 快模型扫描，2026-10-01） | 逆向依赖实测 **74 站点 / 6 方向对**（非原估 12 处）；ports 有 83 站点在用（"无人走"不成立）；DaemonApplication 284 个直接方法、cli.py 144 command + 27 add_typer；重写引擎选定 **LibCST 1.9.0**（1,236 文件零失败、无操作逐字节一致，Bowler 落选）；执行序列改为 **S1–S6**（解耦先于搬家）；interface 不用 import \*，gate 需理解相对 import/alias。详见同目录 `refactor-c0-and-tooling-2026-10-01.md` |

---

## 1. 现状诊断（为什么必须重构）

1. **18 包巨型循环依赖环**：daemon、agents、harnesses、pac、inbox、orgfs、contracts、dispatch、routine、squire、transfer、mcp、adapters、transport、platform、provider_auth、users、org、log 全在同一个 Tarjan SCC 内；双向边 16 对。包边界名存实亡。
2. **包接口被绕过**：daemon 被钻子模块 import 79 次、走包级 0 次；agents 74 vs 5；pac 65 vs 5。`daemon/__init__.py` 精心重导出 20+ 名字，全仓 0 处使用。⚠ C0 修订：ports 并非"无人走"——C0 AST 扫描实测产品有 83 个站点使用 10 个 ports 模块；成立的结论是"包级公共面被绕过"，不是"ports 不存在"。
3. **上帝文件持续长大**：⚠ C0 修订数字（AST 实测）：`DaemonApplication` 直接类成员方法 **284 个**（原方案按 grep 计 356，含嵌套闭包）；`cli.py` **144 个 command 调用**（143 装饰器 + 1 处动态注册）+ 27 个 add_typer 挂载。抽取定位必须用模块 + qualified name + 哈希，不用行段。
4. **浅模块爆炸**：31 个文件 <60 行，51 个在 60–150 行；46 个顶层散文件（29k 行）中大量放错层（`*_actor`、`*_watchdog`、`assign_*`、`tsnet_*`、`gui_*`）。
5. **代码自己感觉到了错误**：agents → daemon 的 11 处反向 import 中 10 处是函数体 lazy import（手写躲环）；`contracts/__init__.py` 用 lazy `__getattr__` 躲 wire→inbox 环并在 docstring 里承认。

~~**关键发现（重构比想象便宜的原因）**：五模块归并后，上述 16 对双向边中 14 对变成模块内部事务；真正需要动手解耦的跨模块渗漏只有 12 处、两类（见 §4.1）。~~

**⚠ C0 修订（推翻上述估算）**：原方案的 import 分析基于 grep 正则，漏掉相对 import、函数内、TYPE_CHECKING 与 alias 形态。C0 用 Python 3.12 AST 全量扫描（354 个产品文件、13,169 个 import 站点、18,592 条符号引用，零解析失败）实测：**逆向依赖 74 个独立站点 / 6 个方向对 / 45 条源模块边**——daemon→biz 31、identity→daemon 21、biz→shell 14、kernel→daemon 5、daemon→shell 2、identity→biz 1。解耦工作量以 C0 `boundaries.json` 为准，逐符号审定生成 manifest（C0 §4），不沿用"十二处即可"的估算。同时注意：daemon 仍占产品源码约 73.6%，"目录压成五个"本身不等于深模块目标达成。

---

## 2. 目标结构

```text
src/hyprial/
├── __init__.py            # 版本（不动）
├── cli.py                 # 对外契约入口：app 装配 + main()（瘦身后保留）
├── kernel/
│   ├── __init__.py        # 仅转发公共面（⚠ C0 修订：审定的 __all__ + 显式 lazy 转发，不用 import *）
│   ├── interface.py       # 公共面：词汇表类型 + ports + facade（lazy __getattr__）；允许引用本模块 impl
│   └── impl/              # 全部实现
├── identity/
│   ├── __init__.py
│   ├── interface.py
│   └── impl/
├── daemon/
│   ├── __init__.py
│   ├── interface.py
│   └── impl/
├── biz/
│   ├── __init__.py
│   ├── interface.py
│   └── impl/
└── shell/
    ├── __init__.py
    ├── interface.py
    └── impl/
```

**依赖规则**（gate 机械执法， exemptions 从空开始）：

- 方向：`kernel ← identity ← daemon ← biz ← shell`，不可逆；⚠ C0 修订：允许跨层向下引用（如 shell 直接用 kernel），不要求相邻层；daemon 核心运行时不直接 import biz/shell，上层装配负责注入；
- 跨模块只允许 `import hyprial.<mod>` / `from hyprial.<mod> import …`（即公共面）；
- import 路径含 `.<mod>.impl` 的跨模块引用 = 违规；
- interface.py 不 import impl（facade 用 lazy `__getattr__`，沿用 `contracts/__init__.py` 现成模式）。

---

## 3. 第一步：机械化移动

原则：**整文件搬运，内容逐字节不变**；import 改写由 codemod 生成；映射表即本节的全部依据。

### 3.1 包级映射（27 个现有包）

| 新位置 | 吸收 |
|---|---|
| `kernel/impl/` | actor_runtime、contracts、platform、log、hooks（事件总线） |
| `identity/impl/` | agents、users、provider_auth |
| `daemon/impl/` | daemon、inbox、transport、mcp、pac、dispatch、harnesses、orgfs、org、adapters、squire、transfer、installers、autoupdate、status |
| `biz/impl/` | routine、workflow（见备注） |
| `shell/impl/` | cli_commands |

备注：workflow 是"命令组"（Execution belongs to PAC runtime），放 shell 或 biz 均可；本方案放 **biz**（与 routine 同族：pac 之上的业务封装），其 typer 命令经 cli.py 挂载。

### 3.2 顶层散文件映射（46 个）

| 新位置 | 文件 |
|---|---|
| `kernel/impl/` | backoff、duration、uri、persistent_config、home（H2B_HOME 解析，被 23 处引用）、`_dsh_resolver`（备注：harness 支持，亦可 daemon，建议随 dsh 使用方定） |
| `identity/impl/` | identity_transaction、tailnet_identity |
| `daemon/impl/` | adapter_registration、assign、assign_reconcile、assign_store、forwarding_config、home_migration、inbox_watchdog、management、management_actor、network_profile、peer_reachability、process_diagnostics、proxy_route、tsnet_join、tsnet_sidecar、updates、watchdog_actor |
| `biz/impl/`（含 `biz/impl/hooks/`） | alarm、availability_loud、cost_counters、quota_watchdog、turn_pulse、usage、usage_actor；daemon/turn_hooks.py（recap）在解耦阶段迁入 `biz/impl/hooks/` |
| `shell/impl/` | cli_inventory、desktop_network、gui_apps、gui_bundle、gui_product、gui_runtime、login、login_preview、onboarding、onboarding_apply、plugins |
| 特殊 | `gui_build_hook.py`：pyproject `[tool.hatch.build.hooks.custom]` 按路径引用，移动必须同 commit 改 pyproject；`cli.py`：保留原位（对外契约），瘦身为装配层 |

### 3.3 字符串引用清单（codemod 无法覆盖，须同 commit 手工处理）

1. `cli.py` 的 `_prime_post_install_restart_imports()`：autoupdate / daemon.desired_state / daemon.readiness_budget / uri / mcp.channel 五个字符串模块名 → 新路径；
2. 7 处 `importlib.resources.files`：dispatch/matrix、routine/templates、autoupdate、squire/setup、daemon/service、agents/migration_entry、pac/overview——随包移动后验证资源相对路径仍解析；
3. `pyproject.toml`：`gui_build_hook.py` 路径（若移动）；入口 `hyprial.cli:main` 不动；
4. `adapters/lark/sdk.py` 的 `importlib.import_module(self._module)`：配置驱动的 SDK 模块名，确认不受迁移影响；
5. docker/contract/README 中 `python -m hyprial.cli`：不动（cli.py 保留）。

### 3.4 验证

- 文件内容逐字节 diff（git mv 语义）；
- `uv run pytest` 全绿（tests 的 2,573 处 import 由 codemod 同步改写）；
- `uv run ruff check .`、`./contract/run-all.sh --isolated-only`；
- `scripts/ci/pytest-impact-map.json` 与 `scripts/ci/ci-scope.mjs` 的路径映射重新生成。

---

## 4. 第二步：解耦

### 4.1 跨模块渗漏清零（⚠ C0 修订：以 74 站点审定为准）

⚠ **本节原估算（12 处）已被 C0 实测推翻**：真实逆向依赖为 74 个独立站点 / 6 个方向对（分布见 §1 C0 注记）。下表仅保留为最早发现的三类样例；正式解耦范围由主模型按 C0 `boundaries.json` 逐符号审定、生成 manifest（流程与分类见 C0 文档 §4/§5），**不能只用模块归并或动态 import 冒充解耦**。C0 已识别的处置类别：词汇表契约下沉、观测/投递契约与业务策略分离、上层装配注入（routine/usage 的 service/handler/hook 所有权）、CLI 展示逻辑归 shell（禁止 lower-layer 依赖 CliError）、纯契约下沉或 port 注入（不得把整个 PAC/harness/profile 实现塞入 kernel）。

| 渗漏（样例） | 处数 | 处理 |
|---|---|---|
| identity → daemon：`lifecycle_receipts`（36 个纯数据类） | 10 | **词汇表下沉**：移入 `kernel/interface.py`（或 identity/interface，建议 kernel——daemon 也用它） |
| identity → daemon：`HarnessLaunchSpec`（被 10 个 harness + transfer 引用） | 1 | 随 desired_state 的 spec 类型下沉 kernel |
| kernel → daemon：`contracts/wire.py` 引用 inbox 消息类型 | 1 | wire.py 是 inbox 类型的执行适配器，移到 `daemon/impl/`（与使用方同层）；contracts 保留纯 golden case |

处理完后 identity、kernel 对外零上漏；懒加载躲环的 10 处函数内 import 全部还原为模块级正常 import。

### 4.2 cli.py 拆分（⚠ C0 数字：144 个 command 调用 / 27 个 add_typer）

沿用 PR #1097 的 `cli_commands/transfer.py` 模式（`register_<family>_commands()`），按 typer group 一族一个文件，落 `shell/impl/commands/`：

- 已声明的族：adapter（含 user-auth）、daemon、mcp、squire、user-proxy、outbox、delivery、autoupdate、config、profile、network、org、fs、agent(+secret/migrate/keep)、transfer-runtime、workflow、dispatch、routine、onboarding + 顶层命令（init/login/doctor/version/upgrade…）；
- 共享助手 → `shell/impl/commands/_common.py`；
- cli.py 最终只剩：typer app 声明、add_typer 装配、`main()`；
- **机器可验证**：`check_cli_help_inventory.py` 的 help 输出逐字不变；`cli_inventory.py` 清单不变。

### 4.3 application.py 拆分（⚠ C0 数字：DaemonApplication 直接成员方法 284 个；定位用 qualified name + 哈希，不用行段）

按现成方法聚类抽取协作者对象，落 `daemon/impl/`：

| 新协作者 | 吸收方法簇 | 大致行段 |
|---|---|---|
| `_ipc_server.py` | _IpcStream/_IpcResponseWriter/_IpcListener/_write_ipc_response/_serve_*/_start_ipc_client/_close_ipc_clients/_response/_timed_handle | 390–790、6010–6373 |
| `_ipc_dispatch.py` | `handle` 巨型分发器（2.7k 行）→ handler 注册表 | 7097–9863 |
| `_startup_shutdown.py` | run/_startup_step/_finish_shutdown_and_leave/exit 所有权/_arm_* | 1766–2403 |
| `_restore.py` | _restore_*/_wake_dormant_agent/_classify_restore_specs | 4686–5407 |
| `_lifecycle_ops.py` | _start_lifecycle_manager/_run_lifecycle_operation/_lifecycle_* | 3603–3899 |
| `_routines.py` | _routine_*/_reconcile_routine_coordinators/_ensure/_retire | 6389–6730 |
| `_agents_admin.py` | _handle_agent/_handle_agent_migration/agent custody | 9863–10791 |
| `_orgfs_bridge.py` | _handle_orgfs/_require_orgfs_runtime/_notify_orgfs_owner | 6730–7097 |
| `_delivery.py` | deliver_human_alarm/_quota_watchdog_deliver/_deliver_* | 4041–4246 |
| `_maintenance.py` | _schedule_maintenance/_scheduled_maintenance/_run_scheduled_domains | 5486–6010 |

过渡手法：协作者构造时注入 `host` 回指（DaemonApplication 本体），方法整体搬迁、行为逐条等价；后续迭代再收窄 host 为具体依赖。**不在本阶段追求纯净**。

### 4.4 边界 gate

- 新增 `tests/boundary_gate.py`：静态 import 扫描，执法 §2 三条规则；
- 现有 `actor_boundary_gate.py`、`supervision_boundary_gate.py`、`inbox_authority_gate.py`、`session_ref_authority_gate.py`、`atomic_lifecycle_authority_gate.py` 更新路径后保留（authority 类语义不变）；
- 三个 exemptions JSON 清零重起，新 gate 不设豁免口。

---

## 5. 第三步：附带文件清理

> **执行状态（2026-10-01，`refactor/reconstruct-modules` 分支）**：以下两项已提前落地——
> ① `notes/`（845 文件）已并入 `docs/notes/`（commit `d1edf6e0`：存在性校验的引用改写 60 处 / 48 文件，夹具字符串与 `hq/` 他仓引用自动豁免；受影响测试子集全绿）；根目录 `CHANGELOG-v0.4.2.dev7.md`、`NOTES-e2e009-load-experiment.md` 同批迁入，`review-input/` 已删除（commits `a5928c40`/`d19a0768`）。
> ② `tools/`、`spikes/`、`docker/` 已解散进 `scripts/`（commit `b1023d69`）：`tools/verify/*` → `scripts/verify/`、`tools/probe_agent_home_p20.py` → `scripts/archived/`、`spikes/zenoh` → `scripts/verify/zenoh-capability/`、`docker/*` → `scripts/build/docker/`（修复 `build-worker-image.sh` 的 `cd` 深度，`container.py`/`orchestrator.py` 引用同步；受影响测试 232 passed，2 个失败为基线既有环境问题：本机缺 `tea`）。
> ③ **contract/ 内部聚类已执行**（commit `4186817d`）：`wire/`（golden + trajectory.* + message-sender-auth）、`workers/`（python-worker + user-proxy + pi-attach-probe）、`pac/`（mfu-agent-task-v1 + context + events + remote）、`e2e-scenarios` → `e2e/`、`migration-cutover` → `migration/`；cli-blackbox / mcp-tools / daemon-lifecycle 保持原位。组织轴是**外部可观察行为面**，刻意不镜像五模块（契约须在内部重构中保持稳定）。引用改写 102 文件字面量 + 43 文件 Path 拼接形态 + run-all.sh / .dockerignore / e2e/docker/Dockerfile 手工修复；`contract/run-all.sh --isolated-only` 全 PASS，受影响测试全绿（test_e2e001_image_inputs 的 10 个 error 为本机 Xcode git 环境问题，基线同样失败）。README 已加"语料地图"。ROADMAP.md 应急命令路径已同步。
> ④ **docs/ 五桶收拢已执行**（commit `6c6bd774`）：根部 76 个零散 md 全部入桶（design 29 / guides 10 / reference 22 / domains 12 / evidence 8 + 既有子目录），`gui-migration/`、`squire/` 并入 `domains/`；根相对引用改写 90 文件 + 裸文件名相对链接按原位置语义重算 + gui-migration 深度修正；全仓 markdown 断链校验清零（1 处既存断链 workflow/service.py 遗留待清）。`test_stale_repo_url_lint` 白名单路径与证据目录排除已同步。同批：CHANGELOG.md 重排为 Keep a Changelog（commit `95f7376d`，0.4.1–0.4.6 由 tag 区间重建，专题原始记录按考据日期归入 0.4.2/0.4.3/0.4.5）；GOALS.md 已拆分为 ROADMAP.md + AGENTS.md 失联韧性原则 + `docs/notes/roadmap/` WORM 证据（commit `ad4ce68b`）；GLOSSARY.md 新增于根部（`180e4e45`）。
> **注意**：C0 基线 commit（`613893e4`）之后已发生上述搬迁，迁移脚本套件的 baseline/manifest 必须以新 HEAD 重新生成；`scripts/refactor/c0_scan.py` 内的 `notes/` 识别模式亦需同步。
> ⑥ **scripts/ 存活脚本收拢已执行**（2026-10-02，commits `4331d90c`/`fddf7f7a`）：27 个平铺脚本按 §5.2 表迁入 `ci/`（分类/步骤/工具链/pytest 影响面/发布门）、`build/`（wheel + GUI 发布 + docker 已就位）、`verify/`（check_*、verify_*、channel-probe）、`archived/`（六个一次性脚本）；修复四个脚本的 `parents[1]`→`parents[2]` 深度、ci-scope 包装层 import 深度、pytest_impact 映射路径、8 个测试的 Path 拼接引用与夹具 mkdir 深度、ci-ensure-uv 的用户指引自引用。验证：node 分类器 195/0、Python 影响面 318+10 passed。`scripts/README.md` 重写为分桶说明。
> 至此 §5 仅剩 **tests/ 归堆**（随 S5 迁移脚本套件执行，需 codemod 支撑）。
> ⑤ **src/gui 内部收拢已执行**（2026-10-02，commits `21ff771c`/`6b6ca52d`）：`browser-tests/` → `tools/browser/`（它是浏览器工具链锁定包而非测试目录，测试用例本就在 `tests/*.browser.mjs`）；`src/gui/.agents/` → 根 `.agents/skills/gui-workflow/`（skill 被发现性）；`src/gui/docs/`（40+ 文件）→ `docs/domains/gui/`。守卫同步：发布快照排除项保留作纵深防御、双份 ci-scope 分类器与夹具、GUI README 相对链接重算、AGENTS.md skill 索引。验证：ci-scope 166/0、Python 影响面 132 passed。

### 5.1 tests/（708 文件 → 按新模块归堆）

- 按每个测试文件的**主导 import 目标**机械归类到 `tests/{kernel,identity,daemon,biz,shell}/`；
- `conftest.py`、`fixtures/`、`actor_runtime/`、`contract_gates/` 留 tests/ 根部；
- 测试间互相 import 的辅助文件（`tests_helpers_pac.py`、`waiting_support.py`、`release_support.py` 等）收进 `tests/_support/`；
- 首轮允许 tests import impl（白盒）；contract/ 黑盒测试只走公共面。

### 5.2 scripts/

| 类别 | 内容 | 去向 |
|---|---|---|
| CI 存活 | ci/、ci-conditions.sh、ci-ensure-*.sh、ci-plan.mjs(+test)、ci-scope.mjs(+test)、ci-step.sh、pytest_impact.py、pytest-impact-map.json、github_publish.py | `scripts/ci/` |
| 构建存活 | build-wheel.sh、build_gui_release.py | `scripts/build/` |
| 验证存活 | check_cli_help_inventory、check_contract_impact、check_hyprial_merge_residuals、verify_gui_*、verify_public_install | `scripts/verify/` |
| 一次性 | rehearse_login_switch、pac-slice1-mutations、timeout_candidate_measurement ×2、cleanup_resolved_retry_dlq、count_cli_alias_references | `scripts/archived/` |
| 探针 | spikes/zenoh（P1 能力闸门，**存活**） | `scripts/verify/zenoh-capability/` |
| tools/ | probe_agent_home_p20 → `scripts/archived/`；tools/verify → 并入 `scripts/verify/`（若被 CI 引用） | — |

### 5.3 docs/ 收拢（204 文件 / 80 个顶层条目 + notes/ 并入）

现状问题：80 个顶层条目混合五种性质——设计决策（design-\*.md ×22、spec-\* ×2）、运维指南（operator-guide、runbook 类）、协议参考（proto、p1–p4、integration-wire 类）、域文档（lark-\* ×4、gui-\* ×9、transfer-\* ×2）、证据（\*-evidence ×7、field-report、handoff），且已有 design/、evidence/、gui-migration/、squire/ 四个子目录各自为政；另有 notes/（845 文件）作为独立顶层 WORM 证据库与 docs/ 长期并存，"文档到底放哪"没有单一答案。

**收拢为五桶 + notes 并入**（与既有子目录合并）：

| 桶 | 吸收 |
|---|---|
| `docs/design/` | design.md、design-\*.md ×22、spec-\*.md ×2 + 现有 design/ 子目录 |
| `docs/guides/` | operator-guide、onboarding-checklist、cutover-runbook、upgrade-tracks、wheel-distribution、release-checklist-template、zenoh-two-machine、network-isolation、transfer-at12-cross-machine-runbook、lark-app-onboarding |
| `docs/reference/` | proto、integration-wire、p1–p4 ×6、logging、naming-audit、harness-model-providers、route-outbound、turn-hooks、workflow-node-observation、dsh-session-identity、agent-restore-policy、acceptance-agent-identity、cli-top-level-command-consolidation、pytest-impact-selection、ci-\* ×3 |
| `docs/domains/` | lark-\* ×3（onboarding 除外）、gui-\* ×9 + gui-migration/、transfer-smolvm-runtime、squire/、org-context.example、mate-gui-integration-workflow |
| `docs/evidence/` | \*-evidence ×7、field-report-\*、handoff-\*、windows-gui-snapshot + 现有 evidence/ 子目录 |
| `docs/notes/` | **整个 notes/（845 文件）原结构迁入**，WORM 语义不变：内部目录树、文件命名、追加式证据链全部保留；`notes/repro` 的 ruff 豁免同步改指 `docs/notes/repro` |

**链接重写**（已裁决：改写，不留重定向僵尸文件）：全仓 321 处 `docs/…md` 引用（docs 内部 125、src 注释/文档串 74、notes 53、contract 38、README 15、scripts 14）+ notes 迁入后的 `notes/…` 路径引用（pyproject 的 ruff 豁免、`.forgejo/workflows/` 快照排除清单注释、ROADMAP.md 等）由同一个 codemod 的第二规则集改写（Markdown 链接 + 裸路径两种形态）；notes 内部互相引用因其相对结构不变而天然保持有效。`scripts/ci/ci-scope.mjs` 的路径分类以顶层前缀为单位——`notes/` 前缀消失、并入 `docs/`，其分类表与 ci-scope.test.mjs 夹具必须同 commit 更新。

### 5.4 顶层目录总裁决

| 目录 | 规模 | 裁决 |
|---|---|---|
| `src/gui/` | 357 文件 / 7.7M | **不动**（README 明文：独立 Node 工程，CI 独立）——**生产的 GUI 产品源码**，经 build_gui_release 确定性打包、hash 校验安装 |
| `desktop/` | 176 文件 / 3.5M | 主体不动，进布局 gate 白名单；顶层 8 个过程文档（PACKAGING-\*、PRETEST、DISTRIBUTION 等，CI 无引用）收进 `desktop/docs/`——**Electron 消费端桌面宿主**（内测/预生产：有专属 desktop-check.yml / desktop-release.yml、r5 内测发行版、完整分发规划），GUI 仅为其六个 staging 组件之一。**不移入 scripts/**（它是应用子项目而非打包脚本）；若日后转正为正式桌面产品，仿 src/gui 先例晋升为 `src/desktop/`，属独立决策 |
| `sidecar/` | 45 文件 | **不动**（hyprial-tsnet Go 子项目） |
| `docker/` | 3 文件 | **不动** |
| `contract/` | 152 文件 | **不动**，仅同步 import 路径 |
| `notes/` | 845 文件 / 7.3M | **移入 `docs/notes/`**（见 §5.3；WORM 内部结构保留，链接 codemod 改写） |
| `review-input/` | 1 文件 | 撤目录：`hyprial-help-target-state.md` 是一次性评审输入 → `docs/notes/review-input/` |
| `tools/` | 10 文件 | 撤目录：见 §5.2 |
| `spikes/` | — | 撤目录：见 §5.2 |
| `.agents/`、`.claude/`、`.forgejo/`、`.github/` | — | **不动**（agent/CI 配置） |
| 仓库根 | — | `NOTES-e2e009-load-experiment.md` → `docs/notes/`；`CHANGELOG-v0.4.2.dev7.md` → `docs/notes/changelog/`；README、ROADMAP、CHANGELOG、CONTRIBUTING、VERSION、LICENSE 保留根部 |

### 5.5 CI 仓库布局 gate（防止再次腐烂）

收拢只能管一时，**防腐必须靠 CI 执法**。沿用本仓已有的策略测试模式（`tests/ci_workflow_policy.py`、`tests/ci_download_policy.py`：结构化 reader + fail-closed 表 + pytest 执法），新增 `tests/repo_layout_policy.py`：

1. **顶层条目白名单，fail-closed**：任何不在表内的新顶层文件/目录 = CI 红，报错信息要求"把新条目写进白名单并附理由"（与 ci_workflow_policy 的豁免表同一哲学：不进表不等于不需要，进表必须带理由）；
2. **根级 md 白名单**：README、ROADMAP、CHANGELOG、CONTRIBUTING、LICENSE、AGENTS.md、CLAUDE.md、GLOSSARY.md（agent 配置唯一事实源 + Claude 导入行 + 词汇表）；新根级 md 一律红，提示"文档归 docs/ 五桶之一，证据归 docs/notes/"；
3. **专项禁令**：禁止重新出现顶层 `notes/`（已并入 docs/notes/）；禁止在 `src/hyprial/` 下新增顶层散文件（新代码必须落五模块之一的 impl/）——与 boundary_gate 互补，一个管 import、一个管物理位置；
4. **ci-scope 联动**：新增顶层目录必须同步在 `scripts/ci/ci-scope.mjs` 登记变更范围映射（ci-scope.test.mjs 已有夹具测试模式，登记缺失会在该测试中立即暴露）。

### 5.6 不动

- `src/gui/`、`sidecar/`、`docker/`、`contract/`、`.agents/`、`.claude/`、`.forgejo/`、`.github/`：见 §5.4；
- `docs/` 重构落地后追加一篇本方案的终审记录（放 `docs/design/`）。

---

## 6. 执行顺序（⚠ C0 修订：以 S1–S6 为准）

⚠ C0 修订：执行序列改按 C0 文档 §6 的 **S1–S6**（解耦先于搬家——先把审定契约提取出来，再做原子迁移，避免把错误依赖搬进新结构）：S1 契约与解耦 → S2 原子迁移 → S3 CLI 拆分 → S4 application 拆分 → S5 仓库整理 → S6 终验。原 C1–C7 的映射：C2→S1，C1+C3→S2，C4→S3，C5→S4，C6+C7→S5（docs/notes/tests/scripts 分子阶段搬迁），新增 S6 终验（wheel 干净环境、入口/资源、升级连续性与隔离 daemon 场景）。另有两条执法纪律来自 C0：**目录/布局禁令与其对应搬迁同阶段上线**（不提前使 CI 变红）；**不通过取消门或清空旧豁免获得绿色**（3+66+60 条 authority 豁免只迁路径与 owner 身份）。

每阶段独立 checkpoint、逐阶段全绿（pytest + ruff + contract isolated）；PR 原子合并、无 shim 窗口。**执行窗口要短**：dev 日均多个合并 PR（本方案观察窗口 1 天内 10+ 个 merge），搬家前需通告冻结或接受 rebase 成本。脚本工程纪律（LibCST 1.9.0 重写引擎、manifest 逐符号记录、plan/apply/verify/status、隔离 checkout 执行、幂等双跑证明、WORM 哈希保持）全部见 C0 文档 §3/§5，本方案不再重复。

## 7. 风险清单

| 风险 | 缓解 |
|---|---|
| open PR 大面积冲突 | 短窗口 + 通告；codemod 可对 PR 分支重放 |
| `gui_build_hook.py` 被 pyproject 按路径钉住 | 同 commit 改 pyproject；构建验证 |
| `importlib.resources` 资源路径漂移 | §3.3-2 清单逐个验证 |
| autoupdate 跨安装边界的字符串预热清单 | §3.3-1 同 commit 更新；E2E-007 复跑 |
| tests 2,573 处 import 改写出错 | codemod 生成 + 全量 pytest；禁止手工改 |
| `ci-scope.mjs` / `pytest-impact-map.json` 路径映射失效 | C1 内重新生成；notes/ 前缀并入 docs/ 的分类表与夹具 C7 同 commit 更新 |
| gate 误伤存量合法引用 | S1 前以 C0 `boundaries.json` 74 站点为全量基线逐符号审定，gate 规则需理解相对 import/alias/动态 import（不只查 `.impl` 子串） |
| docs 链接改写漏网（src 注释 74 处、notes 53 处为非链接裸路径形态） | codemod 覆盖链接 + 裸路径双形态；C7 后全仓 `grep -r "docs/"` 零旧路径残留验证 |
| notes 迁入 docs/notes 后外部硬引用断裂（pyproject ruff 豁免、`.forgejo` 快照排除清单、ROADMAP.md、operator 肌肉记忆） | 已裁决**改写不留重定向**；C7 验收标准含 `grep -rn "notes/" --exclude-dir=.git` 仅命中 `docs/notes/` 形态 |
| 布局 gate 上线即红（存量违规未清） | C6 先出全量基线扫描，白名单初始内容 = 收拢后的真实顶层条目，gate 只挡增量 |
| workflow 归属（biz vs shell） | 本方案取 biz；若改 shell 仅影响 C1 映射表一行 |
