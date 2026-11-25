# actor-rustify 第一步：5 平台默认 native 的实施排期（2026-11-06）

状态：排期稿，待 Allen 确认 §5 各项。本文不改任何代码。
基线：`intg/actor-rustify` @ `86a5111b1`（已同步 `release/v0.5.5.4@c32b005ff`）。
本文区分**实测**与**推断**；推断项标 *推断*。

## 1. 正式改判（Allen 2026-11-06）

| 项 | 原规则 | 改判后 |
|---|---|---|
| 目标版本 | HQ 11-03 B1 裁定：0.5.5.4 只收小修，rustify 进 0.5.5.5 | **作废**。目标 `release/v0.5.5.4` |
| 平台（G0 ⑧ / 附录 G、handoff §4.4） | 本 goal 硬格只有 darwin/arm64、linux/amd64；另外三格移到"进 dev/发布前"的后续门 | **5 个平台全部默认 native**：darwin/arm64、darwin/amd64、linux/amd64、linux/arm64、windows/amd64。每个平台都要构建 native wheel，并实际安装、调用通过。取代原来的"两硬格 + 三后续格"。缺 `_core` 的平台不发布，也不回退 pykka |
| 集成路径（附录 J、D7） | 短分支 → intg → #1897 面向 dev；去持有重构走 release → dev | `intg/actor-rustify` **直接**合入 release，不经过 `intg/cell-model`。何时合入（#2514）由 Allen 决定 |
| 分支纪律 | 长期工作分支（如 #2251） | 每段开发单独开一个 PR，base 为 `intg/actor-rustify`。不保留长期工作分支 |
| 复现材料 | `docs/notes/repro` 可以追加 | 原始样本和日志堆**不进仓库**。PR 里只放结论和命令 |
| CI-I（G0 ⑨ / 附录 I） | 按 base 名触发、两点 diff | 采用推荐方案：按 rustify 的路径和分支前缀触发，比较基准用 merge-base，step 级 env 加 guard。实现见 #2529（取代 #1945） |

第一步终点（不变）：`HYPRIAL_ACTOR_BACKEND` 默认值为 native（prerequisites :324、handoff :53）。前提：
1. 替换线做到 P7；
2. native conformance 全绿；
3. 5 个平台的 wheel 都能构建，并安装、调用通过；
4. HQ R6：切默认的那个提交，在两种后端上都通过 `./contract/run-all.sh --isolated-only`，以及结构门和模块边界门；
5. 缺 `_core` 时 fail-closed。pykka 在第一步里仍是可选后端，P8 之后才删除。

## 2. 现状（实测 2026-11-06，linux/amd64，CPython 3.12.3）

| 项 | 结果 |
|---|---|
| conformance pykka @ AR | 95 failed / 292 passed / 10 xfailed |
| conformance pykka @ #2520 P2a | 53 failed / 344 passed：95 个里修掉 56 个；SUP-09 的 10 个 strict xfail 转为 XPASS；新增 4 个 TMR-04/05 回归 |
| conformance pykka @ P2b（`refactor/actor-port-p2b`） | **397 passed，PASS** |
| conformance reference-abi @ P2b | 397 passed |
| conformance native | 396 passed + 1 BLOCKED（FREE-03，本机没有 free-threaded Python；brief 实测，本文未复测） |
| CI-I | #2529：本地 37 个用例通过；对 #2520/#2521/#2522/release 同步做 dry-run，结果符合预期 |

## 3. 批次与依赖

```text
                    ┌─ #2529 CI-I ──────────────────────────────┐ (所有改 workflow 的 PR 的前置)
                    │                                            ▼
#2520 P2a ─► P2b ─┬─► P2c 端口对象 ─┬─► P6 native 选择缝 ─┐   W1 wheel 按平台构建（只加行）
                  │  (含 timer/pool) │                       │   W2 native CI job（各平台跑 conformance）
#2521 P4 ─────────┼──────────────────┴─► P5 组合根接线 ──┐     │        ▲ 运维 runner 就位
                  │                                     ▼     │        │
                  │        第 1 波：B2 orgfs、B3 lark/transport（与 cell-model 零重叠）
                  │        第 2 波：B6（与 cell-model 有 2 个文件重叠，需要冻结窗口）
                  │        第 3 波：B1、B4、B5（S10 区域，等 cell-model 进 release 或另定冻结窗口）
                  │                                     ▼
                  │                     P7 每进程一个共享引擎（附证据）
                  │                                     ▼
                  └──────────► S 切默认 native（R6 门，两种后端）◄── W1+W2 五个平台全部 PASS
                                                        ▼
                                              P8 删除兼容门面（第一步之后）
```

| 批次 | 内容 | 依赖 | 可并行 | 估时（人日，*推断*） |
|---|---|---|---|---|
| P2b | pykka 后端 conformance 转绿（本 PR） | #2520 | — | 已完成，待评审 |
| P2c | `create_engine` / `create_actor_host` / `ActorHost`。端口要带上 `send_after` 和 pool（D3），B 批次迁移 EffectLane / GenerationScheduler 需要用到；conformance 的 pykka 适配层改走端口，不再碰 `_guardian` | P2b | 与 P4、W1 并行 | 2.5 |
| P4 | #2521 engine-holding 门禁（71 个文件） | — | 已开 PR | 0（合入）+ 0.5（cell-model 进 release 后重新生成清单） |
| P5 | 组合根接线 + `actor_host` fixture，先用隔离模式 | P2c、P4 | 与 P6 并行 | 2 |
| P6 | `backends/native.py` + `HYPRIAL_ACTOR_BACKEND`（缺 `_core` 时 fail-closed） | P2c | 与 P5、B 并行 | 2.5 |
| B2 | orgfs 9 个 + 透传 4 个（13 条豁免） | P5 | 第 1 波 | 3 |
| B3 | lark 5 + transport 3 + transfer 2 + network 1（11 条） | P5 | 第 1 波 | 2.5 |
| B6 | biz 3 + kernel log/hooks + application + operator_alarm 等（11 条） | P5 | 第 2 波 | 2 |
| B1 | dispatch / ipc / correlation / processes / settlement（12 条） | P5 + 冻结窗口 | 第 3 波 | 2.5 |
| B4 | lifecycle / operations / squire 3 / forwarding / configuration / autoupdate / state_persistence（13 条） | P5 + cell-model 进 release | 第 3 波 | 2.5 |
| B5 | identity 2 / session_actor / inbox 2 / harnesses 3 / org 2 / pac 1（11 条） | P5 + 冻结窗口 | 第 3 波 | 3 |
| P7 | 每进程一个共享引擎；线程数 / tell 延迟 / 全量测试证据 | B1–B6 | — | 1.5 |
| W1 | `wheel-release.yml` 按平台构建（只加行，见 §4）+ #2522 合入 | #2529、runner | 与 P2c–B 并行 | 2.5 |
| W2 | native CI job：各平台跑 conformance native，加一个不阻断的 3.15t job（D12） | W1、#2529 | 同上 | 1.5 |
| S | 切默认 native，按 R6 门两种后端都跑 | P7、P6、W1、W2 | — | 0.5 |

合计约 30 人日（*推断*，含 D3 收编 EffectLane / GenerationScheduler 的增量）。按单 session 串行，关键路径为 P2c → P5 → B（三波）→ P7 → S。W 线可以和这条路径并行。

## 4. 平台与 CI 设计要点

- **CI-I 限制下只能加行**：4 个 workflow 文件只能加行。`wheel-release.yml` 现有的 `build-publish` job 在 `release-ci` 一台机器上循环 5 个 target。#2522 的 native stage 遇到"目标平台 ≠ 构建主机"会直接报错，所以必须先让这个循环不再依赖本机构建 native 模块。方案（全部是加行）：
  1. 新增按平台的 `native-*` job，各自在对应 runner 上构建 `_core`，然后用该平台自己构建的完整 wheel 做安装、import、公共调用冒烟。产物是模块文件加上它的 SHA-256。
  2. 现有 job 加一行 `needs:`，再加一个 step 下载这些预构建模块，并导出 `HYPRIAL_NATIVE_PREBUILT_DIR`。build hook（`scripts/build/**` / `native_engine.py`，不在 workflow 守卫范围内）遇到预构建模块时直接装入、核对哈希、跳过 cargo。
  3. 组装后的 wheel 里每个模块的哈希必须等于该平台冒烟时的哈希，否则发布失败。
- **runner 映射**（以运维回报为准）：`release-ci` → linux/amd64（manylinux 容器）；`release-linux-arm64` → linux/arm64；`release-macos-arm64` → darwin/arm64；`release-windows-amd64` → windows/amd64。**darwin/amd64 没有计划中的 runner**，见 §5-2。
- FREE-03 在 CI 上需要 free-threaded 解释器。D12 已经裁定 3.15t job 不阻断；而"native 全绿"要求 FREE-03 不是 BLOCKED，因此由 W2 在至少一个平台上安装 3.14t/3.15t 来跑 FREE-03。

## 5. 待 Allen 确认

1. **排期与并行方式**：按 §3 分三波推进 B 批次，第 1 波（B2、B3）不需要和 cell-model 协调，可以先开。
2. **darwin/amd64 的构建方式**：没有 x86_64 macOS runner。建议在 `release-macos-arm64` 上用 `x86_64-apple-darwin` 交叉构建，然后在同一台机器上用 Rosetta 安装和调用（G0 附录 G.2 原口径）。这需要 #2522 的"目标平台 ≠ 主机"报错放行这一对显式白名单。另一种做法是请运维再配一台 x86_64 Mac。
3. **cell-model 冻结窗口**：B1/B4/B5 与 S10（dispatch、PAC、shutdown、squire 退役）重叠；B6 与 `application/__init__.py`、`operator_alarm.py` 重叠。建议 cell-model 先进 release，AR 同步 release 之后再做第 3 波，并在合并后的树上重新生成 P4 清单。如果 cell-model 进 release 的时间晚于第 2 波完成，再通过 Allen 约定冻结窗口。
4. **PR 合入权**：#2520 / #2521 / #2522 / #2529 以及后续批次 PR 的 base 都是 `intg/actor-rustify`。请指定由谁合入（我、e2e-verifier，还是 Allen 本人）。`.forgejo/workflows/` 的变更按 forgejo-ops 规则由 Allen 合入。
