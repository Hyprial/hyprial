# C2 平台与 harness CLI 审核说明（2026-11-13）

本文件给 backend owner 复核 C2a+C2b 使用，排在 C1a/C1b 之后。

## 1. 分支与提交

- 分支：`feat/c2-platform-harness-20261113`
- 提交：`23e469f63 feat(packaging): port C2a platform seam and C2b managed harness CLI`
- 基于 C1b `d8fed9925`；已 rebase 到该 head。
- 已 push；未创建 PR、未合入。

## 2. 内容

| 区域 | 文件 | 说明 |
|---|---|---|
| PTY | `kernel/impl/platform/pty/{__init__,posix,windows}.py` | POSIX/Windows PTY 从 15c0b3707 移植 |
| privacy/ACL | `kernel/impl/platform/privacy/private_access.py`、`windows_home_layout.py` | Windows 私有文件/ACL/home layout |
| ports/process | `kernel/impl/platform/{ports,process}.py`、`processes/owned_process.py` | 端口/进程 seam |
| harness protocol | `daemon/impl/harnesses/protocol/{common,windows_pty}.py` | Windows ConPTY/隐藏控制台 |
| harness CLI | `shell/impl/onboarding/{harnesses,harness_activation,node_preparation}.py` | 检测、选择、受管安装、Node 准备 |
| managed harness | `kernel/impl/primitives/managed_harness.py` | 受管 harness 原语 |
| CLI | `shell/impl/cli/commands/init/onboarding.py` | `harnesses`/`harness-select`/`harness-install` |
| templates | `daemon/impl/agent_templates/{home_layout,setup}.py` | agent home/setup 兼容 |
| tests | kernel/platform、daemon harness、shell onboarding、squire setup | 从旧线移植并适配 |

## 3. 验证

| 命令 | 结果 |
|---|---|
| 组合 focused suite（kernel platform + onboarding + squire setup + CLI + architecture/platform + kernel contracts/ipc/processes + daemon harnesses/pi/orphan-gc + harness platform ports） | **546 passed, 1 skipped** |
| `check_structural_limits.py` | PASS（violations=[]） |
| `check_module_boundaries.py` | PASS（violations=[]） |
| `ruff check .` | PASS |
| `git diff --stat 15c0b3707 HEAD -- <C2 scope>` | 空（源码/测试/baseline 与旧线逐字节一致） |
| full `pytest tests/ -n 8` | **EXIT=124（15 分钟超时，无 summary）— 不是 PASS**；并发 pytest/主机负载干扰，需安静机器重跑 |

skip 是 `test_harness_activation_windows.py:234` 的 Windows-only 真实 `os.homedir()`/USERPROFILE 探针。

## 4. UNKNOWN / 待确认

1. **Windows 实际行为**：PTY/ConPTY、ACL、隐藏控制台、named pipe 只在 Linux 上跑单测/条件 skip；真实 Windows 未测。
2. **macOS PTY**：POSIX 分支只在 Linux 上跑；macOS 未测。
3. **受管 harness 真实安装**：Node/Pi 下载与安装未在隔离网络/真实 Windows 上跑；单测用 fixture。
4. **与 C1 的合并顺序**：C2 基于 C1b；backend owner 先确认 C1，再看 C2，避免把平台/harness 变更混入 C1 审核。
5. **C2b 依赖 C2a**：已同分支合并；如果 backend 要求拆 PR，可按文件表拆分，但测试需整体跑。
6. **Full suite**：组合 focused 546 passed，但全量 `pytest tests/` 15 分钟超时（EXIT=124），不算通过；需安静机器重跑。
7. **inline comment**：新增 87 条注释提醒全部逐字来自 15c0b3707；检查器当前非阻断，未改成 strict。

## 5. 后续

- 建议 C1a/C1b 先审；C2 作为后续 backend PR/分支。
- C2 合入后，A 的 setup 页 harness 检测/安装才能从 feature-gate 转为可用；
  正式 2727 包再打开 harness 旅程。
- 不自行合入；等 backend owner / verifier。
