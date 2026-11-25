"""Plan propagation must reject missing/failed evidence before heavy work."""
import json
import os
from pathlib import Path
import subprocess
import tomllib

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]


def workflow(name):
    return yaml.safe_load((ROOT / f".github/workflows/{name}.yml").read_text())


@pytest.mark.parametrize("name", ["gate-python", "gate-gui"])
def test_refactor_target_prs_have_ci_without_enabling_push_publication(name):
    data = workflow(name)
    triggers = data.get("on", data.get(True))  # YAML 1.1's boolean spelling.
    assert "refactor/reconstruct-modules" in triggers["pull_request"]["branches"]
    assert "refactor/reconstruct-modules" not in triggers.get("push", {}).get("branches", [])


def test_each_workflow_computes_exactly_one_common_plan():
    for name, expected in [
        ("gate-python", ["cli-help-inventory"]),
        ("gate-gui", ["gui-unit", "gui-acceptance"]),
    ]:
        jobs = workflow(name)["jobs"]
        planners = [job for job, data in jobs.items() for step in data.get("steps", [])
                    if "node " in step.get("run", "") and "ci-plan.mjs" in step["run"]]
        assert planners == expected
        assert jobs[expected[0]]["outputs"]


def test_workflows_consume_the_central_dev_first_policy():
    policy = json.loads((ROOT / ".github/git-flow.json").read_text())
    assert policy["integration"] == {
        "branch": "dev", "pullRequestSources": "any",
        "suiteSelection": "changed-paths", "wipPullRequests": "draft",
    }
    for name, expected_count in (("gate-python", 1), ("gate-gui", 2)):
        data = workflow(name)
        triggers = data.get("on", data.get(True))
        assert policy["integration"]["branch"] in triggers["pull_request"]["branches"]
        planning = [step for job in data["jobs"].values() for step in job.get("steps", [])
                    if "node scripts/ci/ci-plan.mjs" in step.get("run", "")]
        assert len(planning) == expected_count
        assert all(".github/git-flow.json" in step["name"] for step in planning)
    planner = (ROOT / "scripts/ci/ci-plan.mjs").read_text()
    assert "readGitFlow(root)" in planner
    assert "dev accepts only release" not in planner


@pytest.mark.parametrize("name", ["gate-gui", "gate-pr-declarations"])
def test_checkout_uses_the_current_repository_instead_of_the_redirected_old_name(name):
    commands = [step.get("run", "") for job in workflow(name)["jobs"].values()
                for step in job.get("steps", [])]
    assert any('/${GITHUB_REPOSITORY}.git' in command for command in commands)
    assert all("/HyprialOS/harness-bridge.git" not in command for command in commands)


def test_dev_first_policy_retains_preparation_only_release_and_frozen_writers():
    policy = json.loads((ROOT / ".github/git-flow.json").read_text())
    assert policy["release"]["preparationTarget"] == policy["integration"]["branch"]
    assert policy["release"]["preparationPurpose"] == "preparation-only"
    assert policy["release"]["candidateSource"] == "dev-history"
    assert policy["main"]["publishesOnPush"] is False
    formal = workflow(policy["release"]["entryWorkflow"].removesuffix(".yml"))
    assert set(formal.get("on", formal.get(True))) == {"workflow_dispatch"}
    for name, job, flag in (
        ("gate-python", "unit", "INTERNAL_PUBLICATION_ENABLED"),
        ("release-nightly-promote", "promote", "NIGHTLY_PUBLICATION_ENABLED"),
        ("release-tag", "tag", "FORMAL_PUBLICATION_ENABLED"),
    ):
        assert workflow(name)["jobs"][job]["env"][flag] == "false"


def test_tests_workflow_keeps_light_planning_but_skips_heavy_setup():
    planner = workflow("gate-python")["jobs"]["cli-help-inventory"]
    assert planner["outputs"]["full"] == "${{ steps.scope.outputs.full }}"
    assert planner["outputs"]["light"] == "${{ steps.scope.outputs.light }}"
    steps = {step["name"]: step for step in planner["steps"]}
    assert steps["Verify shared CI policy"].get("if") is None
    assert "steps.scope.outputs.full == 'true'" in steps["Ensure uv"]["if"]
    assert "steps.scope.outputs.full == 'true'" in steps[
        "Enforce product module and file size limits"
    ]["if"]


def test_contract_gate_fetches_a_real_base_and_passes_the_seed_flag():
    steps = {
        step["name"]: step
        for step in workflow("gate-python")["jobs"]["cli-help-inventory"]["steps"]
    }
    fetch = steps["Fetch daemon contract comparison base"]
    assert "steps.scope.outputs.full == 'true'" in fetch["if"]
    assert fetch["env"]["EVENT_NAME"] == "${{ github.event_name }}"
    assert fetch["env"]["BASE_BRANCH"] == "${{ github.base_ref }}"
    assert fetch["env"]["BEFORE_SHA"] == "${{ github.event.before }}"
    assert "CONTRACT_BASE_REF" in fetch["run"]
    assert "CONTRACT_ALLOW_FIRST_SEED" in fetch["run"]
    assert "accepted_sha" in fetch["run"]
    assert 'fetch_origin "$accepted_sha"' in fetch["run"]
    check = steps["Check daemon request contracts and ledgers"]
    assert "scripts/ci/check_request_contracts.py" in check["run"]
    assert "--base-ref" in check["run"]
    assert "--allow-first-seed" in check["run"]
    assert check["timeout-minutes"] == 5
    architecture = steps[
        "Check daemon request architecture for Python, GUI adapter, and Native"
    ]
    assert "steps.scope.outputs.full == 'true'" in architecture["if"]
    assert "scripts/ci/check_daemon_request_architecture.py" in architecture["run"]
    assert "--base-ref" in architecture["run"]
    assert "--allow-first-seed" in architecture["run"]
    assert architecture["timeout-minutes"] == 5


def test_gui_request_architecture_gate_runs_generation_tests_and_base_comparison():
    steps = {
        step["name"]: step
        for step in workflow("gate-gui")["jobs"]["gui-unit"]["steps"]
    }
    fetch = steps["Fetch daemon request architecture base"]
    assert fetch["if"] == "success() && steps.scope.outputs.gui == 'true'"
    assert "GUI_ARCHITECTURE_BASE_REF" in fetch["run"]
    assert "GUI_ARCHITECTURE_ALLOW_FIRST_SEED" in fetch["run"]
    check = steps["Check daemon request client generation and architecture"]
    assert check["if"] == "success() && steps.scope.outputs.gui == 'true'"
    assert "npm --prefix gui run client:reference:check" in check["run"]
    assert "npm --prefix gui run client:fixture:check" in check["run"]
    assert "node --test gui/tests/architecture/daemon-request.node.mjs" in check["run"]
    assert "scripts/ci/check_gui_request_architecture.mjs" in check["run"]
    assert "--base-ref" in check["run"]
    assert "--allow-first-seed" in check["run"]
    assert "node --test scripts/ci/check_gui_request_paths.selftest.mjs" in check["run"]
    assert 'node scripts/ci/check_gui_request_paths.mjs "${args[@]}"' in check["run"]
    assert check["run"].index("args=(--base-ref") < check["run"].index("check_gui_request_paths.mjs")
    assert "node --test scripts/ci/check_gui_mock_source.selftest.mjs" in check["run"]
    assert 'node scripts/ci/check_gui_mock_source.mjs "${args[@]}"' in check["run"]
    assert check["run"].index("args=(--base-ref") < check["run"].index("check_gui_mock_source.mjs")
    assert check["timeout-minutes"] == 5


def test_gui_unavailable_copy_ledger_is_compared_with_the_base_branch():
    steps = {
        step["name"]: step
        for step in workflow("gate-gui")["jobs"]["gui-unit"]["steps"]
    }
    ledger = steps["Check GUI unavailable-copy ledger against the base"]
    assert ledger["if"] == "success() && steps.scope.outputs.gui == 'true'"
    assert "node --test gui/tests/architecture/unavailable-copy-ledger.node.mjs" in ledger["run"]
    assert "scripts/ci/check_gui_unavailable_copy.mjs" in ledger["run"]
    assert '--base-ref "$GUI_ARCHITECTURE_BASE_REF"' in ledger["run"]
    assert '--base-branch "$GUI_ARCHITECTURE_BASE_BRANCH"' in ledger["run"]
    names = list(steps)
    assert names.index("Check GUI unavailable-copy ledger against the base") > names.index(
        "Check daemon request client generation and architecture"
    )


def test_daemon_contract_diff_gate_diffs_the_real_pr_base():
    """wf-61d46aa513404f45a938d9e764f8dd55#implGRS3 (revGRS2 finding c).

    The daemon contract projection gate must diff HEAD's committed
    projection against the projection at merge-base with the PR base, in a
    job whose checkout actually carries that base (shallow clones deepen in
    bounded rounds; an unresolvable merge-base stays fail-closed). A gate
    invoked with ``--base == --head`` proves nothing and is rejected.
    """

    steps = {
        step["name"]: step
        for step in workflow("gate-python")["jobs"]["cli-help-inventory"]["steps"]
    }
    gate = steps["Check daemon contract projection against the PR base"]
    run = gate["run"]
    assert "scripts/ci/check_daemon_contract_diff.py" in run
    assert "--base-ref" in run
    assert "--base " not in run
    assert "merge-base" in run
    assert "--deepen" in run
    assert any("github.event.pull_request.base.sha" in value for value in gate["env"].values())
    assert any("${{ github.sha }}" == value for value in gate["env"].values())
    assert gate["timeout-minutes"] == 10


def test_tailcat_gate_consumes_sidecar_plan_and_runs_the_live_go_module():
    jobs = workflow("gate-python")["jobs"]
    assert "tailcat-go" in jobs, "gate-python.yml must define the tailcat-go Go gate"

    planner = jobs["cli-help-inventory"]
    assert planner["outputs"]["sidecar"] == "${{ steps.scope.outputs.sidecar }}"

    tailcat = jobs["tailcat-go"]
    # 2026-10-29: pr-hygiene is a SEPARATE required status, not a needs edge of the
    # content jobs.  A red there used to skip every shard/tailcat, so one tooling
    # failure (artifact upload 500) zeroed the whole round's evidence.
    assert tailcat["needs"] == ["cli-help-inventory"]
    assert tailcat["if"] == (
        "always() && needs.cli-help-inventory.result == 'success' && "
        "needs.cli-help-inventory.outputs.sidecar == 'true'"
    )
    assert "GO_VERSION=1.27.1 bash scripts/ci/ci-ensure-go.sh" in "\n".join(
        str(step.get("run", "")) for step in tailcat["steps"]
    )
    go_gate = next(
        step for step in tailcat["steps"] if "go test ./..." in step.get("run", "")
    )
    assert go_gate["working-directory"] == "sidecar/hyprial-tailcat"
    assert "go version" in go_gate["run"]
    assert "go vet ./..." in go_gate["run"]

    unit = jobs["unit"]
    assert "tailcat-go" in unit["needs"]
    scope = next(step for step in unit["steps"] if step.get("id") == "scope")
    assert scope["env"]["SIDECAR_SELECTED"] == (
        "${{ needs.cli-help-inventory.outputs.sidecar }}"
    )
    assert scope["env"]["TAILCAT_RESULT"] == "${{ needs.tailcat-go.result }}"


SHARDS = 4


def run_scope(tmp_path, **env):
    unit = workflow("gate-python")["jobs"]["unit"]
    step = next(step for step in unit["steps"] if step.get("id") == "scope")
    output = tmp_path / "output"
    # Default: a pull_request. pr-hygiene is no longer an input of `unit`
    # (2026-11-06: its own workflow and required context, gate-pr-hygiene).
    defaults = {"EVENT_NAME": "pull_request"}
    result = subprocess.run(["bash", "-c", step["run"]], capture_output=True, text=True,
                            env={**os.environ, "GITHUB_OUTPUT": str(output), **defaults, **env})
    return result, output


@pytest.mark.parametrize("state,python,gui,ok", [
    ("success", "true", "false", True), ("success", "false", "true", True),
    ("success", "false", "false", True), ("success", "true", "true", True),
    ("failure", "true", "true", False), ("cancelled", "true", "true", False),
    ("skipped", "false", "false", False), ("success", "", "", False),
    ("success", "maybe", "true", False),
])
def test_required_unit_gate_refuses_failed_or_incomplete_plan(tmp_path, state, python, gui, ok):
    unit = workflow("gate-python")["jobs"]["unit"]
    assert unit["needs"] == ["cli-help-inventory", "checks", "pytest-shard", "tailcat-go"]
    assert unit["if"] == "always() && needs.cli-help-inventory.outputs.wip != 'true'"
    shards = "success" if python == "true" else "skipped"
    result, output = run_scope(tmp_path, PLAN_RESULT=state, PYTHON_SELECTED=python, GUI_SELECTED=gui,
                               FULL_SELECTED="true", LIGHT_SELECTED="false", DOCS_ONLY="false",
                               CHECKS_RESULT="success", SHARDS_RESULT=shards,
                               SIDECAR_SELECTED="false", TAILCAT_RESULT="skipped")
    assert (result.returncode == 0) is ok, result.stderr
    if ok:
        assert output.read_text() == (
            f"unit={python}\ngui_integration={gui}\nfull=true\nlight=false\ndocs_only=false\n"
        )
    else:
        assert not output.exists()


@pytest.mark.parametrize("python,full,light,docs,checks,shards,ok", [
    ("true", "true", "false", "false", "success", "success", True),
    ("true", "true", "false", "false", "success", "failure", False),
    ("true", "true", "false", "false", "success", "cancelled", False),
    ("true", "true", "false", "false", "success", "skipped", False),
    ("true", "true", "false", "false", "success", "", False),
    ("true", "true", "false", "false", "failure", "success", False),
    ("true", "true", "false", "false", "skipped", "success", False),
    ("false", "true", "false", "false", "success", "skipped", True),
    ("false", "true", "false", "false", "success", "success", False),
    ("false", "false", "true", "false", "success", "skipped", True),
    ("false", "false", "true", "true", "success", "skipped", True),
    ("false", "false", "true", "false", "success", "success", False),
    ("false", "true", "true", "false", "success", "skipped", False),
    ("true", "false", "false", "true", "success", "skipped", True),
    ("true", "false", "false", "true", "success", "success", False),
    ("true", "false", "false", "false", "success", "skipped", False),
])
def test_required_unit_gate_refuses_any_shard_or_checks_outcome_but_the_expected_one(
    tmp_path, python, full, light, docs, checks, shards, ok
):
    # always() lets the aggregator report; it must never make it green by default.
    result, output = run_scope(tmp_path, PLAN_RESULT="success", PYTHON_SELECTED=python, GUI_SELECTED="false",
                               FULL_SELECTED=full, LIGHT_SELECTED=light, DOCS_ONLY=docs,
                               CHECKS_RESULT=checks, SHARDS_RESULT=shards,
                               SIDECAR_SELECTED="false", TAILCAT_RESULT="skipped")
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    assert output.exists() is ok


@pytest.mark.parametrize("selected,result,ok", [
    ("true", "success", True),
    ("true", "failure", False),
    ("true", "cancelled", False),
    ("true", "skipped", False),
    ("false", "skipped", True),
    ("false", "success", False),
])
def test_required_unit_gate_matches_tailcat_result_to_the_plan(
    tmp_path, selected, result, ok
):
    completed, output = run_scope(
        tmp_path,
        PLAN_RESULT="success",
        PYTHON_SELECTED="false",
        GUI_SELECTED="false",
        FULL_SELECTED="true",
        LIGHT_SELECTED="false",
        SIDECAR_SELECTED=selected,
        DOCS_ONLY="false",
        CHECKS_RESULT="success",
        SHARDS_RESULT="skipped",
        TAILCAT_RESULT=result,
    )
    assert (completed.returncode == 0) is ok, completed.stdout + completed.stderr
    assert output.exists() is ok


def test_integration_lane_local_gate_evidence_is_required_in_docs_and_template():
    readme = (ROOT / ".github/workflows/README.md").read_text(encoding="utf-8")
    template = (ROOT / ".github/PULL_REQUEST_TEMPLATE.md").read_text(encoding="utf-8")
    for text in (readme, template):
        assert "Local gates:" in text
        assert "pass/skip" in text
    assert "gate ④" in readme
    assert "intg/batch-" in readme


def test_pr_hygiene_is_its_own_required_workflow():
    """pr-hygiene is a merge gate by its own context, not through `unit`.

    2026-11-06 split: it moved from tests.yml to gate-pr-hygiene.yml, so it can
    no longer be a `needs:` of `unit` (needs are workflow-local). Blocking now
    rests on `gate-pr-hygiene / pr-hygiene*` being a required check. It must
    trigger on every PR the Python gate triggers on, plus `edited` (the body
    rule reads the body as of the event). gate-python triggers on `edited` too
    (2026-11-15: removing a `WIP:` title prefix must start the full set without
    a push); an edit replans from the diff like any event, so it never
    publishes a shardless `unit` success for code that has shards.
    """

    hygiene_doc = workflow("gate-pr-hygiene")
    python_doc = workflow("gate-python")
    hygiene = hygiene_doc["jobs"]["pr-hygiene"]
    assert list(hygiene_doc["jobs"]) == ["pr-hygiene"]
    assert hygiene["if"] == "github.event_name == 'pull_request'"
    triggers = hygiene_doc[True]["pull_request"]
    assert triggers["types"] == ["opened", "synchronize", "reopened", "edited"]
    assert triggers["branches"] == python_doc[True]["pull_request"]["branches"]
    assert python_doc[True]["pull_request"]["types"] == [
        "opened", "synchronize", "reopened", "edited"
    ]
    assert "pr-hygiene" not in python_doc["jobs"]
    assert "pr-hygiene" not in python_doc["jobs"]["unit"]["needs"]
    scope = next(step for step in python_doc["jobs"]["unit"]["steps"] if step.get("id") == "scope")
    assert "PRHYGIENE_RESULT" not in scope["env"]
    runs = "\n".join(str(step.get("run", "")) for step in hygiene["steps"])
    for rule in ("merge-tree", "registry-union", "submitted-by"):
        assert rule in runs, f"pr-hygiene no longer runs the {rule} rule"
    # The full-history checkout is what makes rules 1 and 2 non-vacuous: a
    # --depth=1 fetch cannot answer an ancestry question or build a merge tree.
    checkout = hygiene["steps"][0]["run"]
    assert "fetch --depth" not in checkout
    assert "refs/remotes/origin/${BASE_REF}" in checkout


def test_pr_hygiene_runs_the_base_ref_copy_of_the_gate_script():
    """The gate must not be supplied by the pull request it judges.

    Kanban `1d076332` (from the #1400 findings): the job used to run
    `scripts/ci/check_pr_hygiene.py` straight from the checked-out head tree,
    so a candidate could weaken its own gate and every gate fix had to land
    on the base branch before it could fail anything. The job must now
    overwrite the checked-out script with the base-ref copy -- same relative
    path, so the script's ROOT still resolves to the repository root --
    before any rule invocation.
    """

    steps = workflow("gate-pr-hygiene")["jobs"]["pr-hygiene"]["steps"]
    names = [step.get("name", "") for step in steps]
    restore_candidates = [
        index
        for index, step in enumerate(steps)
        if "git show" in step.get("run", "")
        and "check_pr_hygiene.py" in step["run"]
        and "BASE_REF" in step["run"]
        and '>"${gate}"' in step["run"]
    ]
    assert restore_candidates, names
    restore = restore_candidates[0]
    checkout_candidates = [
        index
        for index, step in enumerate(steps)
        if "git checkout --detach" in step.get("run", "")
    ]
    assert checkout_candidates, names
    checkout = checkout_candidates[0]
    assert checkout < restore, names
    invocations = [
        index
        for index, step in enumerate(steps)
        if index != restore and "check_pr_hygiene.py" in step.get("run", "")
    ]
    assert invocations, "no step runs check_pr_hygiene.py"
    assert min(invocations) > restore, names


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_required_unit_gate_does_not_read_pr_hygiene(tmp_path, event):
    completed, output = run_scope(
        tmp_path,
        PLAN_RESULT="success",
        PYTHON_SELECTED="false",
        GUI_SELECTED="false",
        FULL_SELECTED="true",
        LIGHT_SELECTED="false",
        DOCS_ONLY="false",
        CHECKS_RESULT="success",
        SHARDS_RESULT="skipped",
        SIDECAR_SELECTED="false",
        TAILCAT_RESULT="skipped",
        EVENT_NAME=event,
        PRHYGIENE_RESULT="failure",
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert output.exists()

def test_expensive_jobs_no_longer_gate_on_the_cheap_provenance_check():
    """SUPERSEDED 2026-10-29: the fast-fail coupling was removed on purpose.

    History: `pr-hygiene` takes 23-26 s while the four shards cost ~20 minutes, and
    67 of 151 pull_request runs were red on `pr-hygiene`, so gating the expensive
    jobs on it saved real time.  But the same edge meant that a *tooling* failure
    inside `pr-hygiene` (artifact upload 500) skipped every content job and the
    round produced no evidence at all (#2118 job 96383, run 40161).  Evidence
    beats speed: `pr-hygiene` is now a separate required status and no content job
    depends on it.  This test pins the removal so it is not re-introduced by
    accident.
    """
    jobs = workflow("gate-python")["jobs"]
    for name, selection in (
        ("checks", None),
        ("pytest-shard", "needs.cli-help-inventory.outputs.python == 'true'"),
        ("tailcat-go", "needs.cli-help-inventory.outputs.sidecar == 'true'"),
    ):
        job = jobs[name]
        # 2026-11-06: pr-hygiene lives in gate-pr-hygiene.yml; a red there no
        # longer skips the content jobs (release/v0.5.5.4 86adfb01d direction).
        assert job["needs"] == ["cli-help-inventory"], name
        assert job["if"].startswith("always()"), name
        assert "pr-hygiene" not in job["if"], name
        if selection is not None:
            assert selection in job["if"], name
    # The body rule is the cheapest of `pr-hygiene`'s four and must run before the
    # merge-tree work, so a missing declaration fails without materialising a merge.
    steps = [step.get("name") for step in workflow("gate-pr-hygiene")["jobs"]["pr-hygiene"]["steps"]]
    assert steps.index("Require the Submitted-by identity line") < steps.index("Gate the merge tree")


def test_shards_form_one_complete_matrix_bound_to_the_runner_argument():
    jobs = workflow("gate-python")["jobs"]
    shard = jobs["pytest-shard"]
    assert shard["needs"] == ["cli-help-inventory"]
    assert shard["if"] == (
        "always() && needs.cli-help-inventory.result == 'success' && needs.cli-help-inventory.outputs.python == 'true' "
        "&& needs.cli-help-inventory.outputs.docs_only != 'true'"
    )
    assert shard["strategy"]["fail-fast"] is False
    assert shard["strategy"]["matrix"] == {"shard": [*range(1, SHARDS + 1), "isolated"]}
    run = next(step["run"] for step in shard["steps"] if "python scripts/ci/pytest_impact.py" in step.get("run", ""))
    assert f'lane=(--shard "${{{{ matrix.shard }}}}/{SHARDS}")' in run
    assert 'lane=(--isolated)' in run
    assert 'python scripts/ci/pytest_impact.py "${lane[@]}" --' in run
    # The aggregator, not a shard, is the required check: no shard may match `gate-python / unit*`.
    assert not [name for name in jobs if name.startswith("unit") and name != "unit"]


def shard_run():
    shard = workflow("gate-python")["jobs"]["pytest-shard"]
    return next(step["run"] for step in shard["steps"] if "python scripts/ci/pytest_impact.py" in step.get("run", ""))


def test_full_python_gate_orders_without_increasing_worker_count():
    run = shard_run()
    assert '--junitxml="${RUNNER_TEMP}/pytest-results/unit.xml"' in run
    assert "-n 2 --dist=loadfile --max-worker-restart=0" in run
    assert "--grouped" not in run  # one wave per shard; the shard plan replaces the waves


def test_full_python_gate_provisions_refactor_tools_without_omitting_their_tests():
    tool = tomllib.loads((ROOT / "scripts/refactor/pyproject.toml").read_text())
    requirement, = tool["project"]["dependencies"]
    assert requirement.startswith("libcst==")
    run = shard_run()
    # tests/ includes the migration tools, whose dependency stays separate
    # from the frozen product lock. Do not hide collection failures by ignores.
    assert f"--with {requirement}" in run
    assert "--locked" in run and "--no-sync" in run
    assert "--ignore" not in run and "deselect" not in run


def test_shards_pin_the_project_cli_before_the_refactor_dependency_overlay():
    run = shard_run()
    pin = 'export HYPRIAL_BIN="${PWD}/.venv/bin/hyprial"'
    assert pin in run
    assert 'test -x "${HYPRIAL_BIN}"' in run
    assert run.index(pin) < run.index("uv run --locked")


@pytest.mark.parametrize("job", ["checks", "pytest-shard"])
def test_python_jobs_run_native_gui_fixtures_on_node_24(job):
    data = workflow("gate-python")["jobs"][job]
    # The installed-product fixture uses actual GUI sources. Its Node floor
    # is a product requirement, not a reason to skip two Python tests.
    assert data["container"]["image"] == "docker.io/library/node:24-bookworm"
    assert data["container"]["options"] == "--tmpfs /ci-tmpfs:rw,exec,size=2g,mode=1777"


def test_python_gui_consumers_have_locked_node_dependencies_before_pytest():
    jobs = workflow("gate-python")["jobs"]
    for job, consumer, condition in [
        ("pytest-shard", "Run unit tests shard (excludes E2E)", None),
        ("checks", "Run Python GUI integration for audited GUI changes",
         "steps.scope.outputs.docs_only != 'true' && steps.scope.outputs.unit != 'true' && "
         "steps.scope.outputs.gui_integration == 'true'"),
    ]:
        steps = jobs[job]["steps"]
        install = next(step for step in steps if step["name"] == "Install locked GUI fixture dependencies")
        assert install.get("if") == condition
        # Both installs carry the stall guard (review 1170 on #1264):
        # 3 x (timeout 150 + kill 10) + 2 x sleep 10 + the probe = 9 min.
        assert install["timeout-minutes"] == 9
        assert "npm --prefix src/gui ci --ignore-scripts --no-audit --no-fund" in install["run"]
        assert "npm install" not in install["run"]
        assert not install.get("continue-on-error", False)
        user = next(step for step in steps if step["name"] == consumer)
        assert steps.index(install) < steps.index(user)


PYTHON_STATIC_GATE_STEPS = ("Run Python static lint", "Compile Python sources")
PYTHON_STATIC_GATE_CONDITION = (
    "steps.scope.outputs.docs_only != 'true' && steps.scope.outputs.unit == 'true'"
)


def _python_static_gate_problems(text: str) -> list[str]:
    steps = {
        step["name"]: step
        for step in yaml.safe_load(text)["jobs"]["checks"]["steps"]
    }
    return [
        f"{name}: if is {steps[name].get('if')!r}, expected {PYTHON_STATIC_GATE_CONDITION!r}"
        for name in PYTHON_STATIC_GATE_STEPS
        if steps[name].get("if") != PYTHON_STATIC_GATE_CONDITION
    ]


def test_python_lint_and_compile_track_only_the_python_suite_selection():
    """2026-11-10 CI scope trim: a GUI-only diff runs no Ruff and no compileall.

    Both static gates used to fire on `gui_integration` too, forcing every
    audited GUI change through Python lint/compile in gate-python. They now
    read exactly the pytest-shard selection (`python` == true and not
    docs-only, surfaced by the checks job's scope step as `unit`); GUI
    validation stays in gate-gui, and docs-only keeps skipping both.
    """
    assert _python_static_gate_problems(
        (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8")
    ) == []


def test_mutation_python_lint_reintroduced_via_gui_integration_is_rejected():
    text = (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8")
    gui_integration_form = (
        "steps.scope.outputs.docs_only != 'true' && (steps.scope.outputs.unit == 'true' "
        "|| steps.scope.outputs.gui_integration == 'true')"
    )
    mutated = text.replace(
        f"- name: Run Python static lint\n        if: {PYTHON_STATIC_GATE_CONDITION}",
        f"- name: Run Python static lint\n        if: {gui_integration_form}",
    )
    problems = _python_static_gate_problems(mutated)
    assert len(problems) == 1
    assert problems[0].startswith("Run Python static lint:")


@pytest.mark.parametrize(
    "job,consumer",
    [
        ("pytest-shard", "Run unit tests shard (excludes E2E)"),
        ("checks", "Run Python GUI integration for audited GUI changes"),
    ],
)
def test_legacy_gui_npm_fixture_is_skipped_only_when_the_retired_tree_is_absent(
    job, consumer
):
    """Retirement must bound the old npm fixture, not skip the Python tests.

    The source-retirement PR removes ``src/gui`` before the new GUI is wired
    back.  Both public Python jobs must still reach their test step in that
    tree; only the legacy fixture install is preconditioned on the directory.
    """

    steps = workflow("gate-python")["jobs"][job]["steps"]
    install = next(
        step for step in steps if step["name"] == "Install locked GUI fixture dependencies"
    )
    run = install["run"]
    guard = "if [ ! -f src/gui/package-lock.json ]; then"
    assert guard in run
    assert "src/gui/package-lock.json absent (retired GUI): skipping fixture dependencies" in run
    assert "if [ ! -d src/gui ]" not in run
    assert run.index(guard) < run.index("node --version")
    assert run.index(guard) < run.index("npm --prefix src/gui ci")
    assert "exit 0" in run[run.index(guard) : run.index("node --version")]
    user = next(step for step in steps if step["name"] == consumer)
    assert steps.index(install) < steps.index(user)


def test_persistent_npm_cache_and_fetch_retries_back_the_npm_installs():
    # 2026-10-08 (infra-ops, blocks #1260): registry blips on the SDK download
    # (ECONNRESET/ETIMEDOUT/EIDLETIMEOUT) killed whole shard matrices because
    # every shard downloaded from scratch. Mirror the tailcat job's Go-cache
    # step: $CI_CACHE_ROOT/npm when present, an explicit cold fallback echo
    # otherwise, and cacache is safe for the four shards' concurrent writers.
    # Review 1170 on #1264: the `checks` job's GUI-fixture npm ci downloads
    # from the same registry, so it carries the same cache step -- no
    # exemption for the same failure mode.
    for job in ("pytest-shard", "checks"):
        steps = workflow("gate-python")["jobs"][job]["steps"]
        cache = next(step for step in steps if step["name"] == "Persist the npm cache")
        run = cache["run"]
        assert 'if [ -n "${CI_CACHE_ROOT:-}" ]' in run
        assert 'mkdir -p "$CI_CACHE_ROOT/npm"' in run
        assert 'echo "npm_config_cache=$CI_CACHE_ROOT/npm" >> "$GITHUB_ENV"' in run
        assert "CI_CACHE_ROOT unset: npm cache stays job-local (cold)" in run
        for setting in ("npm_config_fetch_retries=5",
                        "npm_config_fetch_retry_mintimeout=10000",
                        "npm_config_fetch_retry_maxtimeout=120000",
                        "npm_config_fetch_timeout=60000"):
            assert f'echo "{setting}" >> "$GITHUB_ENV"' in run
        gui = next(step for step in steps if step["name"] == "Install locked GUI fixture dependencies")
        assert steps.index(cache) < steps.index(gui)
        assert "npm --prefix src/gui ci --ignore-scripts --no-audit --no-fund --prefer-offline" in gui["run"]
    steps = workflow("gate-python")["jobs"]["pytest-shard"]["steps"]
    gui = next(step for step in steps if step["name"] == "Install locked GUI fixture dependencies")
    sdk = next(step for step in steps if step["name"] == "Install the pinned pi SDK for the real-SDK P2 guards")
    assert steps.index(gui) < steps.index(sdk)
    # The pin, the --prefer-offline warm-cache read and the version assertion
    # all stay in the pi-SDK step itself.
    assert ("npm install -g --no-audit --no-fund --prefer-offline "
            "@earendil-works/pi-coding-agent@0.85.1") in sdk["run"]
    assert 'test "${pi_version}" = "0.85.1"' in sdk["run"]


def _stall_guard_problems(text: str) -> list[str]:
    # 2026-10-08 follow-up to #1261 (infra-ops): #1260's third run hung ~6
    # minutes in the pi-SDK install with no output after the deprecation
    # warning -- a socket that never errors never trips npm's fetch retries,
    # so neither the cache nor fetch_timeout alone bounds the step. The
    # guard is fourfold: npm_config_fetch_timeout=60000 makes a hang a
    # retryable error, every attempt runs under `timeout -k 10 150`
    # (SIGKILL 10 s after TERM, so an npm that ignores TERM cannot run to
    # the step deadline -- review 1170 on #1264), the loop gives up loudly
    # after exactly 3 attempts, and timeout-minutes: 9 covers
    # 3 x (150 + 10) + 2 x 10 = 500 s plus the assertion. The `checks`
    # job's GUI-fixture npm ci is the same failure mode on the same
    # registry (review 1170): same cache step, same bounded retry.
    jobs = yaml.safe_load(text)["jobs"]
    problems: list[str] = []
    for job_name, name, attempt in (
        ("pytest-shard", "Install locked GUI fixture dependencies",
         "timeout -k 10 150 npm --prefix src/gui ci --ignore-scripts --no-audit --no-fund --prefer-offline"),
        ("pytest-shard", "Install the pinned pi SDK for the real-SDK P2 guards",
         "timeout -k 10 150 npm install -g --no-audit --no-fund --prefer-offline @earendil-works/pi-coding-agent@0.85.1"),
        ("checks", "Install locked GUI fixture dependencies",
         "timeout -k 10 150 npm --prefix src/gui ci --ignore-scripts --no-audit --no-fund --prefer-offline"),
    ):
        steps = jobs[job_name]["steps"]
        step = next((step for step in steps if step["name"] == name), None)
        run = str(step.get("run", "")) if step else ""
        if attempt not in run:
            problems.append(f"{job_name}/{name} does not run each attempt under timeout -k 10 150")
        if "for attempt in 1 2 3; do" not in run or 'if [ "$attempt" -eq 3 ]; then' not in run:
            problems.append(f"{job_name}/{name} has no bounded 3-attempt retry that fails after the third")
        if "sleep 10" not in run:
            problems.append(f"{job_name}/{name} retries without a sleep between attempts")
        if step is None or step.get("timeout-minutes") != 9:
            problems.append(f"{job_name}/{name} timeout-minutes does not cover 3 x (150+10) + 2 x 10 + the assertion")
        cache = next((step for step in steps if step["name"] == "Persist the npm cache"), None)
        if cache is None or "npm_config_fetch_timeout=60000" not in str(cache.get("run", "")):
            problems.append(f"{job_name}: the npm cache step has no npm_config_fetch_timeout=60000 (a hung socket never errors)")
        if cache is not None and step is not None and steps.index(cache) > steps.index(step):
            problems.append(f"{job_name}: the npm cache step must precede the npm install")
    return problems


def test_npm_installs_carry_the_stall_guard():
    assert _stall_guard_problems((ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8")) == []


def test_mutation_without_the_npm_fetch_timeout_is_rejected():
    mutated = (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8").replace(
        'echo "npm_config_fetch_timeout=60000" >> "$GITHUB_ENV"', ""
    )
    assert any("fetch_timeout" in p for p in _stall_guard_problems(mutated))


def test_mutation_unbounded_pi_sdk_install_is_rejected():
    mutated = (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8").replace(
        "timeout -k 10 150 npm install -g --no-audit --no-fund --prefer-offline",
        "npm install -g --no-audit --no-fund --prefer-offline",
    )
    assert any("timeout -k 10 150" in p for p in _stall_guard_problems(mutated))


def test_mutation_without_the_kill_timer_is_rejected():
    # Review 1170 on #1264: `timeout 150` alone sends only TERM; an npm that
    # ignores it still runs to the step deadline. The -k 10 SIGKILL is part
    # of the guard, not decoration.
    mutated = (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8").replace(
        "timeout -k 10 150", "timeout 150"
    )
    assert any("timeout -k 10 150" in p for p in _stall_guard_problems(mutated))


def test_mutation_step_timeouts_back_at_8_are_rejected():
    # 3 x (150 + 10) + 2 x 10 = 500 s plus the assertion no longer fits in
    # 8 minutes once the kill timer is part of every attempt.
    mutated = (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8").replace(
        "timeout-minutes: 9", "timeout-minutes: 8"
    )
    assert any("timeout-minutes" in p for p in _stall_guard_problems(mutated))


def _cut_step(text: str, name: str) -> str:
    start = text.index(f"      - name: {name}")
    end = text.index("      - name:", start + 1)
    return text[:start] + text[end:]


def _mutate_checks_job(text: str, mutate) -> str:
    # Slice out the `checks` job's section: its guarded commands are verbatim
    # the same as pytest-shard's, so a whole-file replace cannot target it.
    start = text.index("  checks:\n")
    end = text.index("  pytest-shard:\n")
    return text[:start] + mutate(text[start:end]) + text[end:]


def test_mutation_unguarded_checks_npm_ci_is_rejected():
    # Review 1170 on #1264: the checks job's npm ci is the same failure mode
    # on the same registry; reverting it to the bare unguarded form must go
    # red, not pass as an undocumented exemption.
    mutated = _mutate_checks_job(
        (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8"),
        lambda section: section.replace(
            "timeout -k 10 150 npm --prefix src/gui ci --ignore-scripts --no-audit --no-fund --prefer-offline",
            "npm --prefix src/gui ci --ignore-scripts --no-audit --no-fund",
        ),
    )
    assert any(p.startswith("checks/") and "timeout -k 10 150" in p
               for p in _stall_guard_problems(mutated))


def test_mutation_checks_without_the_npm_cache_step_is_rejected():
    mutated = _mutate_checks_job(
        (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8"),
        lambda section: _cut_step(section, "Persist the npm cache"),
    )
    assert any(p.startswith("checks:") and "fetch_timeout" in p
               for p in _stall_guard_problems(mutated))


def test_mutation_checks_step_timeout_back_at_6_is_rejected():
    mutated = _mutate_checks_job(
        (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8"),
        lambda section: section.replace("timeout-minutes: 9", "timeout-minutes: 6"),
    )
    assert any(p.startswith("checks/") and "timeout-minutes" in p
               for p in _stall_guard_problems(mutated))


def test_mutation_retry_without_fail_after_the_third_is_rejected():
    mutated = (ROOT / ".github/workflows/gate-python.yml").read_text(encoding="utf-8").replace(
        'if [ "$attempt" -eq 3 ]; then', 'if false; then'
    )
    assert any("3-attempt" in p for p in _stall_guard_problems(mutated))


def test_persistent_dependency_cache_never_replaces_the_tmpfs_test_dirs():
    for job in ("checks", "pytest-shard"):
        steps = workflow("gate-python")["jobs"][job]["steps"]
        cache = next(step for step in steps if "ci-uv-cache.sh" in step.get("run", ""))
        assert "TMPDIR=/ci-tmpfs/tmp" in cache["run"]
        preflight = next(step for step in steps if step["name"] == "Preflight diagnostics")
        assert "HYPRIAL_TEST_TMP_ROOT=${TEST_TMP_ROOT}" in preflight["run"]
        assert steps.index(cache) < steps.index(preflight)


def test_supersession_never_cancels_dev_publication_jobs():
    policy = workflow("gate-python")["concurrency"]
    # A PR supersedes its own earlier PR run, and an integration-branch push
    # supersedes its own earlier push -- dev/main pushes never cancel. Body
    # edits never reach this workflow; pr-hygiene re-runs them on its own
    # required context. The final segment is `github.sha`, never empty:
    # `|| github.run_id || 'adhoc'` put every dispatch and plain dev/main push
    # into one queue (release 30ec9ca93).
    assert policy["cancel-in-progress"] == "${{ github.event_name == 'pull_request' || (github.event_name == 'push' && startsWith(github.ref, 'refs/heads/intg/')) }}"
    assert policy["group"] == "gate-python-${{ github.event_name }}-${{ github.event.pull_request.number || (startsWith(github.ref, 'refs/heads/intg/') && github.ref) || github.sha }}"


def test_cold_bootstrap_has_explicit_bounds_separate_from_tests():
    jobs = workflow("gate-python")["jobs"]
    assert jobs["cli-help-inventory"]["timeout-minutes"] == 25
    # A shard runs about a quarter of the suite the single 45-minute gate ran;
    # checks only bounds cold setup; the aggregator runs no tests at all.
    assert jobs["pytest-shard"]["timeout-minutes"] == 30
    assert jobs["checks"]["timeout-minutes"] == 30
    assert jobs["unit"]["timeout-minutes"] == 10
    for name in ("cli-help-inventory", "checks", "pytest-shard"):
        steps = {step["name"]: step for step in jobs[name]["steps"]}
        assert steps["Ensure uv"]["timeout-minutes"] == 6
        dependency = "Install Python and locked dependencies" if name == "cli-help-inventory" else "Install Python and locked test dependencies"
        assert steps[dependency]["timeout-minutes"] == 12
        assert not steps[dependency].get("continue-on-error", False)
        assert not any("python-toolchain" in step.get("run", "") for step in steps.values())
    first = {step["name"]: step for step in jobs["cli-help-inventory"]["steps"]}
    assert first["Compare Typer tree with generated help inventory"]["timeout-minutes"] == 2


@pytest.mark.parametrize("name", ["gate-python", "gate-gui"])
def test_release_target_prs_trigger_pr_ci_but_not_push_publication(name):
    """Every supported PR target includes the release/* maintenance line.

    ``desktop-check`` is not in this list any more: A retired it together with
    its ``app/`` and ``src/gui/`` inputs, so it has no PR-target surface left.
    """

    data = workflow(name)
    triggers = data.get("on", data.get(True))  # YAML 1.1's boolean spelling.
    assert "release/**" in triggers["pull_request"]["branches"]
    assert "release/**" not in triggers.get("push", {}).get("branches", [])


def test_gui_unit_installs_the_root_lock_even_when_retired_tree_is_absent() -> None:
    steps = workflow("gate-gui")["jobs"]["gui-unit"]["steps"]
    names = {step["name"]: step for step in steps}
    install = names["Install locked Node dependencies"]
    assert install["if"] == "success() && steps.scope.outputs.gui == 'true'"
    assert "npm --prefix gui ci --ignore-scripts" in install["run"]
    assert "Detect retired packaged-GUI tree" not in names
    assert (ROOT / "gui/package-lock.json").is_file()


def _fixture_check(tmp_path, *roots):
    return subprocess.run(
        ["bash", str(ROOT / "scripts/ci/check-retired-fixture.sh"), *roots],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )


def test_retired_fixture_absent_roots_skip(tmp_path) -> None:
    result = _fixture_check(tmp_path, "app", "src/gui")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "present=false"
    assert "absent" in result.stderr


def test_retired_fixture_present_root_without_lock_is_red(tmp_path) -> None:
    (tmp_path / "src/gui").mkdir(parents=True)
    result = _fixture_check(tmp_path, "app", "src/gui")
    assert result.returncode == 1
    assert "src/gui/package-lock.json" in result.stderr


def test_retired_fixture_complete_roots_are_present(tmp_path) -> None:
    for root in ("app", "src/gui"):
        (tmp_path / root).mkdir(parents=True)
        (tmp_path / root / "package-lock.json").write_text("{}")
    result = _fixture_check(tmp_path, "app", "src/gui")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "present=true"


def test_retired_fixture_partial_deletion_is_red(tmp_path) -> None:
    (tmp_path / "src/gui").mkdir(parents=True)
    (tmp_path / "src/gui/package-lock.json").write_text("{}")
    result = _fixture_check(tmp_path, "app", "src/gui")
    assert result.returncode == 1
    assert "app" in result.stderr


def test_gui_package_job_validates_root_web_not_retired_wheel() -> None:
    gui = workflow("gate-gui")["jobs"]
    assert gui["gui-unit"]["outputs"]["package"] == "${{ steps.scope.outputs.package }}"
    assert gui["release-package"]["if"] == "needs.gui-unit.outputs.package == 'true'"
    steps = {step["name"]: step for step in gui["release-package"]["steps"]}
    guard = steps["Require committed GUI release inputs"]["run"]
    assert 'git rev-parse HEAD' in guard and '${GITHUB_SHA}' in guard
    assert 'git diff --cached --quiet' in guard
    assert 'git ls-files --others --exclude-standard' in guard
    artifact = steps["Verify root web GUI production artifact"]["run"]
    assert "npm --prefix gui run verify:production-copy" in artifact
    assert "test -s gui/dist/index.html" in artifact
    assert "uv build" not in artifact


def test_release_package_test_set_cannot_shrink_silently() -> None:
    """Pin dev's five source tests, then run the whole growing GUI lifecycle set."""
    steps = workflow("gate-gui")["jobs"]["gui-unit"]["steps"]
    run = next(
        step["run"] for step in steps
        if step["name"] == "Verify source-mode GUI lifecycle"
    )
    assert 'test -f "$source_test"' in run
    assert "pytest -q tests/shell/gui/lifecycle/" in run
    assert "tests/shell/gui/host/" in run
    assert "test_gui_*.py" not in run, "a glob would hide a shrunken test set"
    for name in (
        "test_gui_apps",
        "test_gui_launcher",
        "test_gui_signals",
        "test_gui_source_selection",
    ):
        assert f"tests/shell/gui/lifecycle/{name}.py" in run, name


def test_pr_hygiene_checkout_retries_instead_of_going_optional():
    """The checkout cannot be skipped (merge-tree needs git objects), so it retries.

    Forward-ported from release/v0.5.5.4 0ec4a7233. Measured 2026-10-29: 0
    checkout failures among 60 pr-hygiene failures, an upper bound of 4.9% of
    failures (2.6% of pull requests). A required status needs a bounded retry and
    must never degrade to continue-on-error: a skipped checkout would make the
    policy rules vacuous.
    """

    steps = workflow("gate-pr-hygiene")["jobs"]["pr-hygiene"]["steps"]
    step = next(s for s in steps if s.get("name") == "Check out the head commit with base history")
    run = step["run"]
    assert "for attempt in 1 2 3" in run, "checkout must retry a bounded number of times"
    assert "git fetch --no-tags origin" in run
    assert "fetch_ok" in run and "failed after 3 attempts" in run
    assert step.get("continue-on-error") is not True, "checkout must not be optional"


def test_artifact_upload_failure_is_advisory_not_a_policy_verdict():
    """An upload blip must not decide the required pr-hygiene job.

    Forward-ported from release/v0.5.5.4 ddb2c6357 (44 upload failures in 12h
    froze merges once the job was required). The downgrade is acceptable only
    because the loss stays visible: a ::warning annotation, a step-summary line
    and the ARTIFACT_UPLOAD_RETRY marker the watcher counts.
    """

    steps = workflow("gate-pr-hygiene")["jobs"]["pr-hygiene"]["steps"]
    step = next(s for s in steps if s.get("name", "").startswith("Report CI conditions artifact upload failure"))
    assert step.get("continue-on-error") is True, "the report must not fail the job"
    run = step["run"]
    assert "::warning" in run, "a downgrade without an annotation hides a lost artifact"
    assert "ARTIFACT_UPLOAD_RETRY" in run, "keep the marker the watcher greps"
    assert "GITHUB_STEP_SUMMARY" in run, "write the loss into the run summary"
    up = next(s for s in steps if s.get("name") == "Upload CI conditions artifact")
    assert up.get("continue-on-error") is True
    assert up.get("if") == "always()"
