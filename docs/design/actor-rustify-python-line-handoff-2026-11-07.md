# actor-rustify Python 替换线 handoff

状态：2026-11-08 已核对并同步，可交给独立 session 施工。WIP PR #2541 的 base 为 `dev`，保持 WIP，不作为一次全量合并的实施 PR。

**新 session 从 [actor-rustify-p2-session-prompt-2026-11-08.md](actor-rustify-p2-session-prompt-2026-11-08.md) 开始。** 本文记录当前基线、完整范围和批次依赖；早期 release 分支与搬运方案已被现状取代。

## 1. 当前基线（2026-11-08 实测）

- 集成分支：`intg/actor-rustify-p2`；目标分支：`dev`。已用 merge commit `a5a0c84de` 合入 `dev@284dcf72942b1b1b2495c9bbddc1e646cfc386a3`，共享分支不 rebase。
- #2514 已合入，`intg/actor-rustify` 的 Rust/native、conformance、P2a/P2b 已在当前树中。`begin_drain`、`OwnerGone`、mailbox/quarantine 等实现已直接核对存在。
- #2532 已合入并带入其 P2a 前置提交；#2520、#2521 已关闭且未合入。**不要再 cherry-pick P2a/P2b，也不要重复搬运旧方案中的 28 个文件。**
- P4 已在本分支：PAC Protocol 改名、engine-holding AST 门禁、shrinking-only 清单和变异测试均在。
- cell-model 已进入当前树；`src/hyprial/daemon/impl/squire/` 不存在。squire 是由模板创建的普通 agent，不为它恢复代码特例目录。
- P4 在合并后最初为 **2 failed / 13 passed**：3 个旧 squire 路径失效，5 个持有者未登记。`2a79b43f7` 完成一次性集成重定基线：
  - `squire/actors/delivery.py` → `adapters/user_delivery/actor.py`；
  - `squire/actors/ledger.py` → `adapters/user_delivery/ledger_actor.py`；
  - `squire/actors/profile.py` → `identity/profiles.py`；
  - 登记 `dev` 原有的 `adapters/worker/control.py`（B3）与 `pac/held_reminder/core.py`（B5）。
- 新基线为 **73 个文件 / 119 个文件-名字对**。这是合并 `dev` 后的一次性起点，不是放宽门禁；后续 B 批次仍只能删除条目和名字，不能增加。

本次交接更新的实测：

| 检查 | 结果 |
|---|---|
| `uv run --python 3.12 --extra test pytest tests/tooling/architecture/boundary/test_actor_engine_holding_gate.py -q` | 重定基线前 2 failed / 13 passed；之后 **15 passed** |
| `uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py` | PASS |
| `uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py` | PASS |
| 相关 ruff 与 `git diff --check` | PASS |
| 全量 `tests/`、conformance、五平台 wheel | **NOT_RUN / BLOCKED**，本次未重新验证 |

组合门复核：`Submitted-by` 与 Contract-Impact 本地检查通过；merge-tree 的 Node CI-policy 为绿色，但 unbounded-waits（3 项）、KP-028 import ratchet、provider 术语 lint（1 failed / 13 passed）为红。未修改的 `dev@284dcf729` 复跑得到同一组失败，属于共享基线，不在本交接中修无关门禁或重生成 baseline。

## 2. 第一步终点与本线边界

第一步最终目标仍是：

1. Python 替换线做到 **P7**：全仓只经组合根持有引擎，每个进程共享一个引擎。
2. native conformance 全绿。
3. darwin/arm64、darwin/amd64、linux/amd64、linux/arm64、windows/amd64 都能构建 native wheel，并实际安装、调用通过。
4. 切默认 native 的独立提交，在 pykka 与 native 两种后端上通过 `./contract/run-all.sh --isolated-only`、结构门和模块边界门。
5. 选择 native 而缺少 `_core` 时 fail-closed，绝不自动回退 pykka；pykka 在第一步仍是可选后端。

本 session 的负责面是 **P2c、P5、P6、B1–B6、P7 的 Python/测试工作**。不要在本线顺带实施发布运维 W1/W2，也不要提前修改 `HYPRIAL_ACTOR_BACKEND` 默认值；默认切换是满足全部前置后的独立提交。P8 清理不在第一步。

规格以当前树中的以下文档为准：

- `docs/design/actor-rustify-prerequisites-2026-11-01.md` §2、§3、§5、§6 D3/D7；
- `docs/design/actor-rustify-step1-plan-2026-11-06.md` §3 的批次依赖；
- `docs/reference/known-problems.md`，特别是 **KP-027**；
- `docs/design/native-wheel-publication-0554.md` 的当前发布阻断口径。

旧文档中 `release/v0.5.5.4`、`intg/actor-rustify` 的起点描述是历史记录，不再作为当前分支指令。

## 3. 施工顺序与依赖

```text
当前：P2a/P2b + native + P4 已在集成树
  │
  ▼
P2c 端口对象与工厂（含 timer/pool，conformance 改走公共端口）
  ├─► P5 组合根接线 + actor_host fixture（先隔离模式）
  │     ├─► 第 1 波 B2、B3
  │     ├─► 第 2 波 B6（先协调重叠文件）
  │     └─► 第 3 波 B1、B4、B5（先取得冻结窗口）
  │           ▼
  │         P7 每进程一个共享引擎
  └─► P6 native 后端适配与后端选择缝（不是切默认值）

P7 + native conformance + 五平台发布门完成后，另行切默认 native
```

### P2c：第一个子 PR

- 按 prerequisites §2.2–2.3 实现/补齐 `ActorEngine`、`ActorCell`、`ActorSpawner`、`ActorHost`、配置与必要工厂；旧方案中的 `create_engine` / `create_actor_host` 名称保持与规格一致。
- 端口必须覆盖 `send_after` 和工作池能力，为 D3 的 EffectLane / GenerationScheduler 收编预留正确语义，不另建一套 mailbox。
- `ActorSpec.event_sink`、ask、按 cell drain、stop/reset/quarantine 等 P2a/P2b 语义不得回退。
- conformance 的 pykka 适配层改走公共端口，不再访问 `runtime._guardian`。
- `create_actor_host` 的端口可以提供，但**组合根接线与 `actor_host` fixture 属于 P5**；不要把 B 批次的业务迁移提前塞进 P2c。
- 行为变化必须先有 RED 测试再有 GREEN；保留 pykka/reference-abi conformance 的连续运行证据。

### P5、P6 与 B 批次

- P5 依赖 P2c 与 P4 已合入集成分支；先用每次 spawn 独立引擎的隔离模式，不直接跳 P7。
- P6 依赖 P2c，可与 P5 分独立子 PR；native 缺 `_core` 必须 fail-closed。正向 native 验证以实际环境为准，不可用 pykka 结果代替。
- 每个 B 批次一个或多个小 PR，base 均为 `intg/actor-rustify-p2`。先删除本批豁免让 P4 变红，迁移完成后转绿；不得为转绿新增无关豁免或提高 ceiling。
- B2/B3 是第一波；B6 先核对 `application/__init__.py`、`messaging/operator_alarm.py` 等重叠；B1/B4/B5 涉及 dispatch/PAC/shutdown 与 S10 改名区域，未取得 owner/冻结窗口前不动工。
- P7 依赖 B1–B6 全部完成，必须附线程数、tell 延迟、全量测试和两种后端适用范围内的证据。

## 4. 与 #2551 的协调

两条线保留独立 WIP PR 和独立子 PR，不合并成一个大实施 PR。当前可并行的是本线 P2c 与 #2551 的 conversations HTTP。

#2551 负责 conversations HTTP、GUI 改读、links/route 退役、别名计划、S1 收尾和真实环境验收。涉及 dispatch、PAC、shutdown、`adapters/user_delivery/{actor,ledger_actor}.py`、`identity/profiles.py` 或 #2551 正在修改的文件时，先通过 Allen 约定唯一 owner 和冻结窗口；不要靠双方同时改同一文件后解决冲突。

## 5. 分支、验证与停止条件

- 每段从最新 `intg/actor-rustify-p2` 建短分支，开独立子 PR，base 为集成分支；前序依赖未合入时不堆叠后续批次。
- 集成分支同步 `dev` 只用 merge commit；共享分支不 rebase、不 squash、不 force-push。
- 每个子 PR 写动机、范围、RED→GREEN、实际命令与结果、UNKNOWN/BLOCKED、`Skill-Impact` 和 `Contract-Impact`。
- 焦点测试后按 AGENTS 跑结构门、模块边界门、ruff 和全量 `uv run --with libcst==1.9.0 pytest tests/`；不能用 skip 或重写期望集掩盖失败。
- 每个 worktree 使用自己的 `.venv`，测试 home/socket/端口与日常及生产环境隔离，不读取或修改生产 daemon、凭据和用户级配置。
- 当前发布阻断 **KP-027** 未解除；旧 runner 汇报不等于当前五平台 PASS。不得以 `HYPRIAL_BUILD_SKIP_NATIVE=1`、伪造 wheel 标签或提高 glibc 下限绕过。
- 发现规格与真实代码冲突、前序缺失、新的未归因失败或需要的冻结窗口未获批准时，停止相关批次并报告 Allen，不自行改设计。

## 6. 历史证据的使用边界

P2b 的 pykka/reference-abi 397 passed、P2a/P2b 旧搬运试验的 620 passed 等数据只证明当时提交，不是当前合并树的验收结果。新 session 必须在实际子 PR head 上重新运行并报告，不把本 handoff 的历史数字写成当前 PASS。
