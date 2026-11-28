# Gate scope step 2 fix notes

日期：2026-11-28。

本轮修复了四项遗留问题。测试辅助模块或非 Python 测试数据现在会作为反向依赖闭包的种子，闭包可跨 `tests/` 顶层目录继续传播。动态导入、Python 语法错误和不可读测试输入仍然 fail-closed 到完整套件。文档分类器逐级检查目标路径的父目录，dangling 文件和 dangling 父目录 symlink 都拒绝 docs-only。

`gate.yml` 和 `nightly-full.yml` 的 shard metadata 校验现在要求六个受控键各出现一次。`PLAN_MODE`、`PLAN_KIND` 使用受控值，计数值使用十进制形状，`PLAN_DIGEST` 使用完整 64 位小写 SHA-256 形状。

S7 的只读回放入口是 `scripts/ci/replay_pytest_selector.py`，固定 snapshot/cohort 是 `scripts/ci/pytest-selector-cohort.json`。snapshot 为 `22eb4003760a4af0cac43db5030c7dc8cb748921`，cohort 固定为 239 个历史非合并提交、39 个公开窗口非合并提交和 7 个公开窗口 first-parent merge 提交。入口把固定 snapshot 解包到临时目录，再调用最终的 `pytest_impact.plan_paths`，不读取已删除的 `scripts/ci/pytest-durations.json`。

回放命令如下。

```sh
uv run --no-project --python 3.12 python scripts/ci/replay_pytest_selector.py
```

2026-11-28 实测回放 PASS。snapshot 含 2320 个 tracked test 文件。历史 cohort 结果为 232 full、7 selected，公开窗口非合并 cohort 为 32 full、7 selected，公开窗口 merge cohort 为 7 full。三个 cohort 均为 0 docs-only。入口输出每个提交的 reason 和 selectedCount，以及 cohort 汇总比例。

焦点回归命令如下。

```sh
uv run --no-project --python 3.12 pytest -q tests/tooling/pytest/test_pytest_impact.py tests/tooling/pytest/test_pytest_impact_guards.py tests/tooling/ci/planning/test_ci_docs_only_classifier.py tests/tooling/ci/gate/test_gate_scope_invariants.py tests/tooling/ci/gate/nightly/test_nightly_full_workflow.py
```

结构门、模块边界、Ruff、compileall 和 Node 检查在交付验证阶段执行，结果写入本轮交接报告。
