"""发布失败时的日志不许声称"已进 registry"。

现场（2026-10-30）:
    `.forgejo/workflows/wheel-release.yml` 的 `Report wheel artifact upload failure` 步骤里，
    `the wheels reached the registry but were NOT saved as a workflow artifact` 这句是**无条件打印**的。
    而更早的步骤（R2 发布 / 越界守卫 r2_upload.py guard_changes）把 job 判红时，
    `Publish wheel to Forgejo PyPI registry` **根本没跑** —— 日志却声称"已进 registry"，
    导致排查方向被带偏（stable job 98409 / nightly run 40654 都读成了"registry 里有 wheel"）。

判据:
    · registry 发布步骤必须有 `id:`（否则后续无法引用它的 outcome）
    · 那句"reached the registry"只能出现在 **outcome == 'success'** 的分支里
    · 必须存在"没进 registry"的分支（否则等于没改）
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[4] / ".forgejo/workflows/wheel-release.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
CLAIM = "the wheels reached the registry"


def _steps() -> list[dict]:
    doc = yaml.safe_load(TEXT) or {}
    jobs = doc.get("jobs") or {}
    out: list[dict] = []
    for job in jobs.values():
        if isinstance(job, dict):
            out.extend(s for s in (job.get("steps") or []) if isinstance(s, dict))
    return out


def test_registry_step_has_an_id() -> None:
    step = next((s for s in _steps() if s.get("name") == "Publish wheel to Forgejo PyPI registry"), None)
    assert step is not None, "找不到 registry 发布步骤"
    assert step.get("id"), "registry 发布步骤必须有 id（后续要用它的 outcome）"


def test_claim_is_guarded_by_registry_outcome() -> None:
    step = next((s for s in _steps() if s.get("name") == "Report wheel artifact upload failure"), None)
    assert step is not None, "找不到失败上报步骤"
    env = step.get("env") or {}
    assert "steps.pypi_registry.outcome" in str(env.get("PYPI_REGISTRY_OUTCOME", "")), (
        "失败上报步骤必须把 registry 步骤的 outcome 传进环境变量（否则无法分支）"
    )
    run = str(step.get("run", ""))
    assert CLAIM in run, "失败上报步骤应保留该句（作 success 分支的文案）"
    # 该句必须出现在 `= "success"` 分支内：取它之前最近的一次 if 判断
    idx = run.index(CLAIM)
    before = run[:idx]
    assert re.search(r'\[\s*"\$\{PYPI_REGISTRY_OUTCOME:-\}"\s*=\s*"success"\s*\]', before), (
        "『reached the registry』这句必须在 outcome=success 的分支里，不能无条件打印"
    )


def test_there_is_a_not_reached_branch() -> None:
    step = next((s for s in _steps() if s.get("name") == "Report wheel artifact upload failure"), None)
    run = str((step or {}).get("run", ""))
    assert "did NOT reach the registry" in run, "必须有『没进 registry』的分支，否则等于没改"


def test_claim_is_not_printed_unconditionally_anywhere_else() -> None:
    """除了这个失败上报步骤，其它步骤不得再出现该断言（避免别处再造同样的误导）。"""
    offenders = [
        s.get("name")
        for s in _steps()
        if s.get("name") != "Report wheel artifact upload failure" and CLAIM in str(s.get("run", ""))
    ]
    assert not offenders, f"这些步骤也在无条件声称已进 registry: {offenders}"


def test_mutation_making_claim_unconditional_is_detected() -> None:
    """变异自证: 把 success 分支判断删掉（退回无条件打印）⇒ 同一个检测必须命中。"""
    step = next((s for s in _steps() if s.get("name") == "Report wheel artifact upload failure"), None)
    run = str((step or {}).get("run", ""))
    mutated = re.sub(r'if \[ "\$\{PYPI_REGISTRY_OUTCOME:-\}" = "success" \]; then', "if true; then", run, count=1)
    assert mutated != run, "变异未生效"
    idx = mutated.index(CLAIM)
    before = mutated[:idx]
    assert not re.search(r'PYPI_REGISTRY_OUTCOME:-\}"\s*=\s*"success"', before), (
        "变异后检测仍未命中 ⇒ 检测已失效"
    )
