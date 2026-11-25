"""release-ci 专用池的路由必须可验证，而不是靠口号。

背景（Allen 2026-10-30 裁定）: 新增 6 台 `release-ci` runner，要求**发版不与其他任务混**。
⇒ 两条路由都必须成立:
   ① 三个发布 job（tag / wheel / desktop publish-reviewed-legacy）直接写在 [release-ci] 上
   ② release 分支的 PR 进入 tests 链，且 tests 的每个 job 都按 base_ref 选池:
      `startsWith(github.base_ref, 'release/')` ⇒ release-ci，否则共享池
⛔ nightly-promote 不是发版 ⇒ 必须留在 ubuntu-latest（本测试同样钉住它，防回潮）

补充（Allen 2026-11-03 裁定）: skill-impact 类纯文本检查迁到 `ci-light` host runner，
不再占用主 CI runner。在 gate-python.yml 内，这体现为 `pr-hygiene` 的非 release 回退池从
ubuntu-latest 改为 ci-light；其余五个 job 仍需要依赖/构建/测试套件，回退池不变。
release/** ⇒ release-ci 的隔离语义保持不变。

钉子的形状（e2e-verifier 2026-10-30 判词第三条）:
    正面形状断言只说明"现在是这么写的"，不说明"写错了会红"。
    ⇒ 每组正向断言都配一条**变异**（把该性质改坏），并断言同一个谓词返回 False。
    ⇒ 变异必须先自证生效（`old` 在文本里出现），否则变异是空操作、负控恒绿。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[4] / ".forgejo/workflows"

# ① 直接钉在 release-ci 的发布 job
RELEASE_JOBS = [
    ("release-tag.yml", "tag"),
    ("wheel-release.yml", "build-publish"),
]

# ② gate-python.yml 里每个 job 都必须带选择器；顺序即文件中出现顺序
TESTS_JOBS = ["cli-help-inventory", "tailcat-go", "checks", "pytest-shard", "unit"]
# ③ gate-pr-hygiene.yml（2026-11-06 从 tests.yml 拆出）只有 pr-hygiene 一个 job
HYGIENE_WORKFLOW = "gate-pr-hygiene.yml"

BASE_REF_SELECTOR = "startsWith(github.base_ref, 'release/')"
SHARED_POOL_SELECTOR = "runs-on: ${{ startsWith(github.base_ref, 'release/') && 'release-ci' || 'ubuntu-latest' }}"
CI_LIGHT_SELECTOR = "runs-on: ${{ startsWith(github.base_ref, 'release/') && 'release-ci' || 'ci-light' }}"
JOB_SELECTORS = {
    job: (CI_LIGHT_SELECTOR if job == "pr-hygiene" else SHARED_POOL_SELECTOR)
    for job in TESTS_JOBS
}
RELEASE_CI_LINE = "runs-on: [release-ci]"


def pr_hygiene_falls_back_to_ci_light(text: str) -> bool:
    job = _jobs(text).get("pr-hygiene")
    return isinstance(job, dict) and str(job.get("runs-on", "")) == (
        "${{ startsWith(github.base_ref, 'release/') && 'release-ci' || 'ci-light' }}"
    )


# ---------------------------------------------------------------- 谓词（正向形状）
def _jobs(text: str) -> dict:
    doc = yaml.safe_load(text) or {}
    return doc.get("jobs") or {}


def job_runs_on_release_ci(text: str, job_name: str) -> bool:
    job = _jobs(text).get(job_name)
    return isinstance(job, dict) and job.get("runs-on") == ["release-ci"]


def nightly_stays_shared(text: str) -> bool:
    job = _jobs(text).get("promote")
    return isinstance(job, dict) and job.get("runs-on") == "ubuntu-latest"


def selector_offenders(text: str) -> dict:
    """返回没按 base_ref 选池的 job（空 dict = 全通过）。"""
    return {
        name: job.get("runs-on")
        for name, job in _jobs(text).items()
        if isinstance(job, dict) and BASE_REF_SELECTOR not in str(job.get("runs-on", ""))
    }


def release_pr_branch_declared(text: str) -> bool:
    doc = yaml.safe_load(text) or {}
    triggers = doc.get("on") or doc.get(True) or {}
    branches = ((triggers.get("pull_request") or {}).get("branches")) or []
    return "release/**" in branches


def _read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _replace_nth(text: str, old: str, new: str, n: int) -> str:
    """把第 n 个（0 基）old 换成 new；找不到第 n 个 ⇒ 抛错（变异必须生效）。"""
    pos = -1
    for _ in range(n + 1):
        pos = text.find(old, pos + 1)
        if pos < 0:
            raise AssertionError(f"变异无效: 文本里没有第 {n} 个 {old!r}")
    return text[:pos] + new + text[pos + len(old):]


# ---------------------------------------------------------------- 正向断言
@pytest.mark.parametrize(("workflow", "job_name"), RELEASE_JOBS)
def test_release_jobs_run_on_release_ci(workflow: str, job_name: str) -> None:
    assert job_runs_on_release_ci(_read(workflow), job_name), (
        f"{workflow}:{job_name} 必须 runs-on [release-ci]"
    )


def test_nightly_promote_stays_on_shared_pool() -> None:
    """nightly-promote 是每夜提升，不是发版 ⇒ 不得占用专用池。"""
    assert nightly_stays_shared(_read("release-nightly-promote.yml"))


def test_tests_workflow_every_job_selects_pool_by_base_ref() -> None:
    """release/** 的 PR 测试不能落在共享池里排队。"""
    offenders = selector_offenders(_read("gate-python.yml"))
    assert not offenders, f"这些 job 没按 base_ref 选池: {offenders}"


def test_pr_hygiene_non_release_fallback_is_ci_light() -> None:
    """非 release 的 PR pr-hygiene 必须走 ci-light（2026-11-03 迁移令）。"""
    assert pr_hygiene_falls_back_to_ci_light(_read(HYGIENE_WORKFLOW))
    assert not selector_offenders(_read(HYGIENE_WORKFLOW))


def test_tests_workflow_runs_on_release_branch_prs() -> None:
    """release/** 必须真的进 tests 链（否则再好的选池也没有事件触发）。"""
    assert release_pr_branch_declared(_read("gate-python.yml"))


# ---------------------------------------------------------------- 负控（变异必红）
MUTATIONS = [
    pytest.param(
        "release-tag.yml", "tag", RELEASE_CI_LINE, "runs-on: ubuntu-latest",
        id="tag-back-to-shared-pool",
    ),
    pytest.param(
        "wheel-release.yml", "build-publish", RELEASE_CI_LINE, "runs-on: ubuntu-latest",
        id="wheel-back-to-shared-pool",
    ),
]


@pytest.mark.parametrize(("workflow", "job_name", "old", "new"), MUTATIONS)
def test_mutation_release_pool_reverts_go_red(workflow: str, job_name: str, old: str, new: str) -> None:
    """把发布 job 挪回共享池 ⇒ 同一个谓词必须变红。"""
    text = _read(workflow)
    assert old in text, f"变异无效: {workflow} 里没有 {old!r}"
    mutated = text.replace(old, new, 1)
    assert mutated != text
    assert job_runs_on_release_ci(mutated, job_name) is False


def test_mutation_nightly_promoted_to_release_ci_goes_red() -> None:
    """把 nightly 塞进专用池 ⇒ 反向断言必须变红（证明它不是在检查一个空集合）。"""
    text = _read("release-nightly-promote.yml")
    old = "runs-on: ubuntu-latest"
    assert old in text, f"变异无效: release-nightly-promote.yml 里没有 {old!r}"
    assert nightly_stays_shared(text.replace(old, RELEASE_CI_LINE, 1)) is False


def _selector_position(job_name: str) -> tuple[str, int]:
    """返回该 job 的选择器行，以及它是该行在文件中的第几次出现（0 基）。

    pr-hygiene 的 ci-light 行全文件唯一；其余五个 job 共用同一行文本，
    必须按它们在同组内的顺序定位。
    """
    line = JOB_SELECTORS[job_name]
    same = [job for job in TESTS_JOBS if JOB_SELECTORS[job] == line]
    return line, same.index(job_name)


@pytest.mark.parametrize("job_name", TESTS_JOBS)
def test_mutation_dropping_selector_on_one_job_goes_red(job_name: str) -> None:
    """逐个 job 去掉选择器 ⇒ 必须被点名为 offender（一次只弄坏一个）。"""
    text = _read("gate-python.yml")
    old, nth = _selector_position(job_name)
    mutated = _replace_nth(text, old, "runs-on: ubuntu-latest", nth)
    offenders = selector_offenders(mutated)
    assert job_name in offenders, f"去掉 {job_name} 的选择器后没被点名: {offenders}"
    # 只弄坏了一个: 其他 job 不应连带失败（否则断言只是"文件整体坏了"）
    assert list(offenders) == [job_name], f"连带误报: {offenders}"


RELEASE_BRANCH_TOKEN = re.compile(r'\s*,\s*"release/\*\*"(?=[,\]])|"release/\*\*"\s*,\s*')


def _drop_release_branch(text: str) -> str:
    """把 `"release/**"` 这个 token 从 branches 列表里删掉（不依赖它在列表中的位置）。"""
    out = RELEASE_BRANCH_TOKEN.sub("", text, count=1)
    assert out != text, "变异无效: 没有删掉任何 release/** token"
    return out


def test_mutation_cutting_release_branch_filter_goes_red() -> None:
    """把 release/** 从 PR 分支过滤里砍掉 ⇒ 事件断言必须变红。"""
    text = _read("gate-python.yml")
    assert release_pr_branch_declared(text), "前置: 真实文件里必须有 release/**"
    assert release_pr_branch_declared(_drop_release_branch(text)) is False


def test_mutation_reverting_pr_hygiene_to_shared_pool_goes_red() -> None:
    """把 pr-hygiene 的非 release 回退池改回 ubuntu-latest ⇒ 迁移令失效必须红。"""
    text = _read(HYGIENE_WORKFLOW)
    assert CI_LIGHT_SELECTOR in text, "变异无效: gate-pr-hygiene.yml 里没有 ci-light 选择器"
    mutated = text.replace(CI_LIGHT_SELECTOR, SHARED_POOL_SELECTOR, 1)
    assert pr_hygiene_falls_back_to_ci_light(mutated) is False


def test_mutations_self_verify() -> None:
    """变异必须先自证生效: old 必须真的在文本里出现，且替换后文本必须改变。

    没有这条，变异写法一旦过时（old 字符串改了就会过期），那些"变异测试"会变成空操作，
    而 18 passed 仍然全绿 —— 钉子悄悄失效。
    """
    cases = []
    for m in MUTATIONS:
        cases.append((m.values[0], m.values[2], 0))
    for job in TESTS_JOBS:
        old, nth = _selector_position(job)
        cases.append(("gate-python.yml", old, nth))
    cases.append(("gate-pr-hygiene.yml", CI_LIGHT_SELECTOR, 0))
    cases.append(("release-nightly-promote.yml", "runs-on: ubuntu-latest", 0))
    cases.append(("gate-python.yml::release-token", '"release/**"', 0))

    ok = 0
    bad = []
    for workflow, old, nth in cases:
        if "::" in workflow:
            text = _read(workflow.split("::", 1)[0])
            if workflow.endswith("::release-token") and _drop_release_branch(text) == text:
                bad.append("gate-python.yml: 删 release/** token 无效果 ⇒ 变异是空操作")
                continue
            ok += 1
            continue
        text = _read(workflow)
        if old not in text:
            bad.append(f"{workflow}: old 不在文本里 ⇒ 变异是空操作: {old!r}")
            continue
        try:
            mutated = _replace_nth(text, old, "MUTATED", nth) if nth else text.replace(old, "MUTATED", 1)
        except AssertionError as exc:
            bad.append(str(exc))
            continue
        if mutated == text:
            bad.append(f"{workflow}: 替换后文本未改变 ⇒ 变异是空操作: {old!r}")
            continue
        ok += 1
    print(f"mutations_verified={ok}/{len(cases)} selftest_meta={'ok' if not bad else 'BROKEN'}")
    for b in bad:
        print(f"  MUTATION_NOOP: {b}")
    assert not bad, f"有 {len(bad)} 条变异没有真正改到文件 ⇒ 那些钉子已失效"


def test_every_positive_assertion_has_a_mutation() -> None:
    """覆盖率自检: 正向断言 4 组，负控至少 4 组（release 池 / nightly / 选择器 / 分支过滤）。"""
    positives = {
        "release_pool": len(RELEASE_JOBS),
        "nightly": 1,
        "selector": 1,
        "pr_hygiene_fallback": 1,
        "branches": 1,
    }
    negatives = {
        "release_pool": len(MUTATIONS),
        "nightly": 1,
        "selector": len(TESTS_JOBS),
        "pr_hygiene_fallback": 1,
        "branches": 1,
    }
    assert set(positives) == set(negatives), "正向与负控组不对齐"
    assert all(negatives[k] >= positives[k] for k in positives), (positives, negatives)
