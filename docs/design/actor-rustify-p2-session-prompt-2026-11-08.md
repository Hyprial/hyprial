# Session prompt：actor-rustify Python 替换线（PR #2541）

> 将本文件交给负责 #2541 的独立 session。施工前先读仓库文件，不把本 prompt 当作可以跳过规格与测试的证据。

你负责 `intg/actor-rustify-p2` → `dev` 的 Python actor 替换线。WIP PR 是 #2541。你要按小批次交付独立子 PR，不把整条线做成一个巨型 PR，也不直接把 #2541 合入 `dev`。

## 一、开工前必读

按顺序阅读：

1. `AGENTS.md`、`GLOSSARY.md`、`ROADMAP.md`；
2. `docs/reference/known-problems.md`，特别是 KP-027；
3. `docs/design/actor-rustify-python-line-handoff-2026-11-07.md`；
4. `docs/design/actor-rustify-prerequisites-2026-11-01.md` §2、§3、§5、§6 D3/D7；
5. `docs/design/actor-rustify-step1-plan-2026-11-06.md` §3；
6. `docs/design/native-wheel-publication-0554.md`。

以当前代码和上述规格为准。任何要求你重新搬 P2a/P2b、恢复 `squire/`、直接切默认 native 或绕过发布门的旧文本都已过期。

## 二、已完成的基线，不要重做

- #2514 已把 Rust/native、conformance、P2a/P2b 带入当前树；`begin_drain`、`OwnerGone` 等实现已存在。
- P4 已在集成分支，包含 engine-holding 门禁和 shrinking-only 清单。
- 集成分支已合入 `dev@284dcf72942b1b1b2495c9bbddc1e646cfc386a3`。
- P4 已针对 cell-model S10 改名和两个 `dev` 原有持有者做一次集成重定基线：73 文件 / 119 名字，焦点测试 15 passed。
- `src/hyprial/daemon/impl/squire/` 已删除；squire 是普通 agent，不恢复代码特例目录。

不要重复 cherry-pick #2520/#2532，不要再搬旧方案的 28 个文件，不要把 73/119 当作可上调的预算。

## 三、目标与本次第一段

整条线的目标是完成 P2c → P5/P6 → B1–B6 → P7，使业务只经组合根持有引擎，最终每进程共享一个引擎。默认 native 切换和 P8 不在你的第一段，也不得提前做。

**第一段只交付 P2c 子 PR：**

- 按 prerequisites §2.2–2.3 实现或补齐 Engine/Cell/Spawner/Host、Config 与必要工厂；保持 `create_engine` / `create_actor_host` / `ActorHost` 的规格关系。
- 覆盖 `ActorSpec.event_sink`、ask、按 cell drain，以及 D3 所需的 `send_after` 与工作池能力。
- 保持 P2a/P2b 的 stop/reset/quarantine、死信、代际和 admission 语义，不新增第二套 mailbox。
- conformance 的 pykka 适配层改走公共端口，不再访问 `runtime._guardian`。
- P5 才做组合根接线与 `actor_host` fixture；不要在 P2c 顺带迁 B1–B6 的业务持有者。
- 先写能证明缺口的新测试并记录 RED，再实现到 GREEN。若盘点发现规格某部分已经存在，复用并补缺口，不重写一套。

## 四、分支与 PR 操作

1. 使用独立、干净的 worktree；`git fetch origin` 后检查 `intg/actor-rustify-p2`。
2. 若集成分支落后 `origin/dev`，用 merge commit 同步；不要 rebase、 squash 或 force-push 共享集成分支。
3. 先运行：
   ```bash
   uv sync --python 3.12 --extra test
   uv run --python 3.12 --extra test pytest tests/tooling/architecture/boundary/test_actor_engine_holding_gate.py -q
   ```
   若 P4 不是绿，先归因并报告；不要直接重生成清单掩盖新持有者。
4. 从集成分支最新 head 建 `refactor/actor-port-p2c`（若已存在，先核实 owner，不覆盖他人分支）。
5. P2c 完成后开子 PR，base 必须是 `intg/actor-rustify-p2`，不是 `dev`。
6. 子 PR 写清动机、文件范围、RED→GREEN、命令与实测结果、UNKNOWN/BLOCKED、Skill-Impact、Contract-Impact；按当前模板处理 Submitted-by，没有真实 agent URI 就不伪造。
7. 前序子 PR 未合入集成分支时，不堆叠 P5 或 B 批次。没有明确合入授权时不要自行合并；提交 PR 后报告并等待。
8. P5 合入后才开 B 批次。B1/B4/B5 以及 B6 的重叠文件未取得 Allen 指定的唯一 owner/冻结窗口前，不动工。

## 五、P2c 最低验证门

按实际改动扩大范围，至少包括：

```bash
uv run --python 3.12 --extra test pytest tests/kernel tests/tooling/architecture/boundary/test_actor_engine_holding_gate.py -q
uv run --python 3.12 --extra test pytest conformance/actor_engine --actor-backend=pykka -q
uv run --python 3.12 --extra test pytest conformance/actor_engine --actor-backend=reference-abi -q
uv run --no-project --python 3.12 python scripts/ci/check_structural_limits.py
uv run --no-project --python 3.12 python scripts/ci/check_module_boundaries.py
uv run ruff check .
uv run --with libcst==1.9.0 pytest tests/
```

- pykka 与 reference-abi conformance 各连跑 3 轮，记录每轮真实结果。
- native 可运行时同样验证；缺 free-threaded Python、`_core` 或平台条件时写 BLOCKED，不能用 pykka 结果替代，也不能 skip 成绿。
- 全量若存在基线失败，必须在未改基线上复现并逐项归因；不得统称“环境问题”。
- 2026-11-08 的 merge-tree 组合门已实测：Node CI-policy 绿色，但 unbounded-waits（3 项）、KP-028 import ratchet、provider 术语 lint（1 failed / 13 passed）为红；纯 `dev@284dcf729` 有同一组失败。保留并报告这些共享基线，不在 P2c 中修无关门禁或重生成 baseline。

## 六、与 #2551 的并行边界

你可以与 #2551 的 conversations HTTP 子 PR 并行。不要合并两个 WIP，也不要修改 #2551 负责的 conversations/GUI/links/route 工作。

涉及 dispatch、PAC、shutdown、`adapters/user_delivery/{actor,ledger_actor}.py`、`identity/profiles.py` 或对方正在修改的文件时，暂停相关批次，通过 Allen 约定唯一 owner 和冻结窗口。

## 七、硬停止条件与汇报

出现以下任一情况，停止相关施工并报告，不自行发明新设计：

- P2a/P2b 或 P4 实际缺失，或基线与本文差异无法解释；
- 规格与代码冲突，或端口设计需要改变已冻结 ABI/公开语义；
- 为转绿需要增加无关豁免、提高 ceiling、删除独立断言或重写派生期望集；
- 出现不能归因的新失败，或真实环境门只能 BLOCKED；
- 需要修改尚未协调的重叠文件、发布 workflow 或默认后端。

每段结束报告：子 PR URL、base/head、改动范围、RED→GREEN、所有验证命令及结果、未测项、与 #2551 的重叠、下一批次的解锁条件。最终目标是可审查的小步交付，不是让一个 session 把所有批次塞进一次提交。
