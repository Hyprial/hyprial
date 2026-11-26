"""Static policy checks for the Windows smoke and suite workflows.

Decision (2026-11-26, tier 1 smoke and tier 2 full suite).  Enforces the
GitHub-native compliance rules for both Windows workflows, plus workflow
coexistence and tier file-list checks.  A file listed in tier1-rejected.tsv
must not also be admitted in tier1-tests.txt.

Decision (2026-11-27, tier 2 full suite).  The four test legs' if: strings and the
concurrency group are asserted in full.  A truth table over event and selection
evaluates those expressions with a small evaluator that follows GitHub's operator
precedence and value-returning && and ||, then checks that the lit legs (named
from each job's upload artifact, the matrix expanded) equal requiredLegs from
build_suite_plan for the selection the planning job computes.  The push and
pull_request rows are hypothetical: no trigger exists for them, and the table
records that they would light no leg while the plan still requires every shard,
so the summary would fail.  Each of four mutations (|| to &&, a deleted leg if:,
swapped tier1-next and arm-next conditions, unparenthesised group) is applied to
the loaded workflow and must change the table.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.ci.windows.shard_files import build_suite_plan

ROOT = Path(__file__).parents[4]
WORKFLOW = ROOT / ".github/workflows/windows-smoke.yml"
SUITE_WORKFLOW = ROOT / ".github/workflows/windows-suite.yml"
PINS = {
    "actions/checkout": "3d3c42e5aac5ba805825da76410c181273ba90b1",
    "astral-sh/setup-uv": "1db35dd8eee11c5894872e0b383ae6c406cf738d",
    "actions/upload-artifact": "cf430e030ddbb5b0abf93d22962f4752f3646cd9",
    "actions/download-artifact": "9000827ccba6bdab643e8b6fd33ac0654aef8333",
}
EXPECTED_CHECKOUT_REF = "${{ github.event.pull_request.head.sha || github.sha }}"
SUITE_CHECKOUT_REF = "${{ github.sha }}"
NATIVE = re.compile(r"^(?:uv|rustup|git|\.venv[\\/]Scripts[\\/]python\.exe)\b")
PRIVATE_IP = re.compile(
    r"10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.|100\.\d+\.\d+\.\d+"
)
EMAIL = re.compile(r"[\w.+-]+@([\w-]+\.[\w.]+)")
ALLOWED_SUITE_STEP_ENV = frozenset(
    {
        "HYPRIAL_BUILD_NATIVE",
        "WINDOWS_SUITE_ACL_PROBE",
        "WINDOWS_SUITE_SELECTION",
        "WINDOWS_SUITE_SHARD",
    }
)


def document(path: Path = WORKFLOW) -> dict[Any, Any]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def trigger(value: dict[Any, Any]) -> dict[str, Any]:
    result = value.get("on") if "on" in value else value.get(True, {})
    return result if isinstance(result, dict) else {}


def jobs(value: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result = value["jobs"]
    return {name: job for name, job in result.items() if isinstance(job, dict)}


def steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for step in job.get("steps", []) if isinstance(step, dict)]


def test_action_pins_and_permissions() -> None:
    value = document()
    text = WORKFLOW.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    checkout_count = 0
    for job in jobs(value).values():
        assert "permissions" not in job or job["permissions"] == {"contents": "read"}
        for step in steps(job):
            uses = step.get("uses")
            if not isinstance(uses, str):
                continue
            action, _, sha = uses.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", sha), uses
            assert action in PINS, action
            assert sha == PINS[action], uses
            found[action] = sha
            if action == "actions/checkout":
                checkout_count += 1
                with_block = step.get("with", {})
                assert isinstance(with_block, dict)
                assert with_block.get("persist-credentials") is False
                assert with_block.get("ref") == EXPECTED_CHECKOUT_REF
    assert found == PINS
    assert checkout_count == 5
    assert value["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in text
    assert "secrets." not in text


def test_runner_labels_and_job_timeouts() -> None:
    value = document()
    expected = {
        "build-wheel": "windows-latest",
        "w1-x64": "windows-latest",
        "smoke-x64": "windows-latest",
        "smoke-arm": "windows-11-arm",
        "windows-smoke-summary": "ubuntu-latest",
    }
    all_jobs = jobs(value)
    assert set(all_jobs) == set(expected)
    for name, runner in expected.items():
        actual = all_jobs[name]["runs-on"]
        assert isinstance(actual, str) and "${{" not in actual
        assert actual == runner
        assert isinstance(all_jobs[name]["timeout-minutes"], int)
        assert all_jobs[name]["timeout-minutes"] > 0
    summary = all_jobs["windows-smoke-summary"]
    assert summary.get("if") == "always()"
    assert summary.get("needs") == ["build-wheel", "w1-x64", "smoke-x64", "smoke-arm"]
    assert all_jobs["smoke-arm"].get("needs") == ["build-wheel"]
    summary_uses = [step.get("uses", "").split("@", 1)[0] for step in steps(summary)]
    assert "actions/checkout" in summary_uses
    assert "actions/download-artifact" in summary_uses

    step_timeouts = {
        ("w1-x64", "Install the editable test environment"): 20,
        ("smoke-x64", "Run x64 smoke"): 10,
        ("smoke-arm", "Run ARM smoke"): 10,
    }
    for (job_name, step_name), minutes in step_timeouts.items():
        matching = [step for step in steps(all_jobs[job_name]) if step.get("name") == step_name]
        assert len(matching) == 1, (job_name, step_name)
        assert type(matching[0].get("timeout-minutes")) is int
        assert matching[0]["timeout-minutes"] == minutes

    validate_steps = [step for step in steps(summary) if step.get("name") == "Validate all legs"]
    assert len(validate_steps) == 1
    validate_run = str(validate_steps[0].get("run", ""))
    inputs = re.findall(r'--input\s+"([^"=]+)=([^"]+)"', validate_run)
    legs = [leg for _, leg in inputs]
    assert len(legs) == 3 and set(legs) == {"w1", "x64", "arm"}, inputs
    for path, _ in inputs:
        assert re.fullmatch(
            r"\$\{\{\s*runner\.temp\s*\}\}/wci-artifacts/[A-Za-z0-9._-]+", path
        ), path

    w1_run_steps = [
        step for step in steps(all_jobs["w1-x64"]) if step.get("name") == "Run W1 waves"
    ]
    assert len(w1_run_steps) == 1
    w1_run = str(w1_run_steps[0].get("run", ""))
    assert "--tier 1" in w1_run
    assert "--files scripts/ci/windows/tier1-tests.txt" in w1_run

    concurrency = value.get("concurrency")
    assert isinstance(concurrency, dict)
    assert str(concurrency.get("group", "")).startswith(
        "windows-smoke-${{ github.event_name }}-"
    )
    assert concurrency.get("cancel-in-progress") == "${{ github.event_name == 'pull_request' }}"


def test_windows_env_keys_and_proxy_policy() -> None:
    value = document()
    text = WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(r"(?im)^\s*[A-Z_]*(?:HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY)\s*:", text)
    assert "continue-on-error" not in text
    top_env = [str(key).casefold() for key in value.get("env", {})]
    assert len(top_env) == len(set(top_env))
    for job in jobs(value).values():
        if job.get("runs-on") == "ubuntu-latest":
            continue
        job_keys = [str(key).casefold() for key in job.get("env", {})]
        assert len(job_keys) == len(set(job_keys))
        for step in steps(job):
            step_keys = [str(key).casefold() for key in step.get("env", {})]
            assert len(step_keys) == len(set(step_keys))


def test_upload_steps_have_outcome_followups() -> None:
    value = document()
    for job in jobs(value).values():
        job_steps = steps(job)
        for index, step in enumerate(job_steps):
            if step.get("uses", "").split("@", 1)[0] != "actions/upload-artifact":
                continue
            assert isinstance(step.get("id"), str) and step["id"]
            assert type(step.get("timeout-minutes")) is int and step["timeout-minutes"] == 10
            assert step.get("if") == "always()"
            assert step.get("with", {}).get("if-no-files-found") == "error"
            assert index + 1 < len(job_steps)
            next_run = str(job_steps[index + 1].get("run", ""))
            assert f"steps.{step['id']}.outcome" in next_run


def test_pwsh_runs_have_one_native_command_and_no_start_process() -> None:
    value = document()
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "Start-Process" not in text
    for job in jobs(value).values():
        if job.get("runs-on") == "ubuntu-latest":
            continue
        for step in steps(job):
            body = step.get("run")
            if not isinstance(body, str):
                continue
            lines = [line.strip() for line in body.splitlines() if line.strip()]
            native_indices = [idx for idx, line in enumerate(lines) if NATIVE.match(line)]
            assert len(native_indices) <= 1, body
            if native_indices:
                trailing = lines[native_indices[0] + 1 :]
                assert trailing == ["$code = $LASTEXITCODE", "exit $code"], body


def test_triggers_paths_and_sensitive_text() -> None:
    value = document()
    text = WORKFLOW.read_text(encoding="utf-8")
    event = trigger(value)
    assert "pull_request" in event and "push" in event and "workflow_dispatch" in event
    assert text.index("\nname:") < text.index("\non:") < text.index("\npermissions:")
    assert PRIVATE_IP.search(text) is None
    emails = [match.group(0) for match in EMAIL.finditer(text) if match.group(1) != "example.com"]
    assert emails == []
    assert not (ROOT / ".github/workflows/windows-followup.yml").exists()
    tier = ROOT / "scripts/ci/windows/tier1-tests.txt"
    for path in tier.read_text(encoding="utf-8").splitlines():
        assert (ROOT / path).is_file(), path
    tier_next = ROOT / "scripts/ci/windows/tier1-next.txt"
    for line in tier_next.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            assert (ROOT / stripped).is_file(), stripped
    arm_lines = [
        line.strip()
        for line in (ROOT / "scripts/ci/windows/tier1-arm-tests.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert arm_lines == []


def test_rejected_files_are_not_admitted() -> None:
    admitted = {
        line.strip()
        for line in (ROOT / "scripts/ci/windows/tier1-tests.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    lines = (ROOT / "scripts/ci/windows/tier1-rejected.tsv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "path\treason\trun_id"
    rows = [line.split("\t") for line in lines[1:] if line.strip() and not line.startswith("#")]
    for row in rows:
        assert len(row) == 3 and all(field.strip() for field in row), row
        assert (ROOT / row[0]).is_file(), row[0]
    rejected = [row[0] for row in rows]
    assert len(rejected) == len(set(rejected))
    assert admitted.isdisjoint(rejected), sorted(admitted.intersection(rejected))


def test_suite_action_pins_permissions_and_triggers() -> None:
    value = document(SUITE_WORKFLOW)
    text = SUITE_WORKFLOW.read_text(encoding="utf-8")
    found: dict[str, str] = {}
    checkout_count = 0
    for job in jobs(value).values():
        assert "permissions" not in job or job["permissions"] == {"contents": "read"}
        for step in steps(job):
            uses = step.get("uses")
            if not isinstance(uses, str):
                continue
            action, _, sha = uses.partition("@")
            assert re.fullmatch(r"[0-9a-f]{40}", sha), uses
            assert action in PINS, action
            assert sha == PINS[action], uses
            found[action] = sha
            if action == "actions/checkout":
                checkout_count += 1
                with_block = step.get("with", {})
                assert isinstance(with_block, dict)
                assert with_block.get("persist-credentials") is False
                assert with_block.get("ref") == SUITE_CHECKOUT_REF
    assert found == PINS
    assert checkout_count == 7
    assert value["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in text
    assert "secrets." not in text

    event = trigger(value)
    assert set(event) == {"schedule", "workflow_dispatch"}
    assert event["schedule"] == [{"cron": "0 19 * * *"}]
    dispatch_inputs = event["workflow_dispatch"]["inputs"]
    assert set(dispatch_inputs) == {"selection", "acl_probe"}
    assert dispatch_inputs["selection"]["type"] == "choice"
    assert dispatch_inputs["selection"]["default"] == "full"
    assert dispatch_inputs["selection"]["options"] == ["full", "tier1-next", "arm-next", "targets"]
    assert dispatch_inputs["acl_probe"]["type"] == "boolean"
    assert dispatch_inputs["acl_probe"]["default"] is False
    assert text.index("\nname:") < text.index("\non:") < text.index("\npermissions:")

    concurrency = value.get("concurrency")
    assert isinstance(concurrency, dict)
    assert concurrency.get("cancel-in-progress") is False
    group = str(concurrency.get("group", ""))
    assert "windows-suite-full" in group
    assert "github.event_name == 'schedule'" in group
    assert "inputs.selection" in group
    assert "github.ref" in group
    assert group == SUITE_CONCURRENCY_GROUP
    assert {name: jobs(value)[name].get("if") for name in SUITE_LEG_IFS} == SUITE_LEG_IFS


SUITE_LEG_IFS = {
    "tier2-pytest": "github.event_name == 'schedule' || inputs.selection == 'full'",
    "targets-run": "github.event_name == 'workflow_dispatch' && inputs.selection == 'targets'",
    "tier1-admission": "github.event_name == 'workflow_dispatch' && inputs.selection == 'tier1-next'",
    "arm-admission": "github.event_name == 'workflow_dispatch' && inputs.selection == 'arm-next'",
}
SUITE_CONCURRENCY_GROUP = (
    "${{ (github.event_name == 'schedule' || inputs.selection == 'full') && 'windows-suite-full' "
    "|| format('windows-suite-{0}-{1}', inputs.selection, github.ref) }}"
)
SUITE_REF = "refs/heads/dev"
SUITE_TRUTH_TABLE = [
    ("schedule", "", ("tier2-pytest",), "windows-suite-full", True),
    ("workflow_dispatch", "full", ("tier2-pytest",), "windows-suite-full", True),
    ("workflow_dispatch", "tier1-next", ("tier1-admission",), f"windows-suite-tier1-next-{SUITE_REF}", True),
    ("workflow_dispatch", "arm-next", ("arm-admission",), f"windows-suite-arm-next-{SUITE_REF}", True),
    ("workflow_dispatch", "targets", ("targets-run",), f"windows-suite-targets-{SUITE_REF}", True),
    ("push", "", (), f"windows-suite--{SUITE_REF}", False),
    ("pull_request", "", (), f"windows-suite--{SUITE_REF}", False),
]
EXPRESSION_TOKEN = re.compile(r"\s*(?:(\|\||&&|==|!=|[(),])|'((?:[^']|'')*)'|([A-Za-z_][\w.]*))")


class Expression:
    """Evaluate the GitHub expression subset used by the suite workflow.

    Supports string literals, context names, ==, !=, &&, ||, parentheses and
    format().  && binds tighter than ||; || returns the first truthy operand and
    && the first falsy one, as GitHub does, so a missing pair of parentheses
    changes the value instead of being ignored.
    """

    def __init__(self, text: str, context: dict[str, str]) -> None:
        text = text.strip()
        if text.startswith("${{") and text.endswith("}}"):
            text = text[3:-2].strip()
        self.tokens: list[tuple[str, str]] = []
        position = 0
        while position < len(text):
            match = EXPRESSION_TOKEN.match(text, position)
            assert match is not None, text[position:]
            operator, literal, name = match.groups()
            if operator:
                self.tokens.append(("op", operator))
            elif literal is not None:
                self.tokens.append(("str", literal.replace("''", "'")))
            else:
                self.tokens.append(("name", name))
            position = match.end()
        self.position = 0
        self.context = context

    def value(self) -> object:
        result = self.either()
        assert self.position == len(self.tokens), self.tokens[self.position :]
        return result

    def peek(self) -> tuple[str, str] | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def take(self, expected: tuple[str, str] | None = None) -> tuple[str, str]:
        token = self.peek()
        assert token is not None and expected in (None, token), (expected, token)
        self.position += 1
        return token

    def either(self) -> object:
        result = self.both()
        while self.peek() == ("op", "||"):
            self.take()
            right = self.both()
            result = result if result else right
        return result

    def both(self) -> object:
        result = self.compare()
        while self.peek() == ("op", "&&"):
            self.take()
            right = self.compare()
            result = right if result else result
        return result

    def compare(self) -> object:
        left = self.primary()
        if self.peek() in (("op", "=="), ("op", "!=")):
            operator = self.take()[1]
            right = self.primary()
            return left == right if operator == "==" else left != right
        return left

    def primary(self) -> object:
        kind, text = self.take()
        if (kind, text) == ("op", "("):
            result = self.either()
            self.take(("op", ")"))
            return result
        if kind == "str":
            return text
        if self.peek() == ("op", "("):
            assert text == "format", text
            self.take()
            arguments = [self.either()]
            while self.peek() == ("op", ","):
                self.take()
                arguments.append(self.either())
            self.take(("op", ")"))
            result = str(arguments[0])
            for index, argument in enumerate(arguments[1:]):
                result = result.replace(f"{{{index}}}", str(argument))
            return result
        return self.context[text]


def suite_leg_names(job: dict[str, Any]) -> list[str]:
    matrix = job.get("strategy", {}).get("matrix", {})
    names: list[str] = []
    for step in steps(job):
        artifact = str(step.get("with", {}).get("name", ""))
        if not str(step.get("uses", "")).startswith("actions/upload-artifact@"):
            continue
        if not artifact.startswith("windows-suite-"):
            continue
        leg = artifact.removeprefix("windows-suite-")
        if "${{ matrix.shard }}" in leg:
            names.extend(leg.replace("${{ matrix.shard }}", str(shard)) for shard in matrix["shard"])
        else:
            names.append(leg)
    return names


def suite_plan_for(value: dict[str, Any], context: dict[str, str], root: Path) -> dict[str, Any]:
    plan_step = next(step for step in steps(jobs(value)["plan-suite"]) if step.get("name") == "Plan test suite shards")
    selection = Expression(plan_step["env"]["WINDOWS_SUITE_SELECTION"], context).value()
    shard_count = int(re.search(r"--shards (\d+)", plan_step["run"]).group(1))
    sample = root / "tests/test_sample.py"
    sample.parent.mkdir(parents=True, exist_ok=True)
    sample.write_text("def test_ok():\n    pass\n", encoding="utf-8")
    listing = root / "listing.txt"
    listing.write_text("tests/test_sample.py\n", encoding="utf-8")
    return build_suite_plan(
        selection=str(selection),
        root=root,
        shard_count=shard_count,
        targets_path=listing,
        tier1_next_path=listing,
        tier1_tests_path=listing,
        tier1_arm_tests_path=listing,
        tracked_files={"tests/test_sample.py"},
    )


def suite_truth_table_rows(value: dict[str, Any], root: Path) -> list[tuple[Any, ...]]:
    rows: list[tuple[Any, ...]] = []
    all_jobs = jobs(value)
    for event, selection, _, _, _ in SUITE_TRUTH_TABLE:
        context = {"github.event_name": event, "inputs.selection": selection, "github.ref": SUITE_REF}
        lit_jobs = tuple(
            name
            for name in SUITE_LEG_IFS
            if "if" not in all_jobs[name] or Expression(str(all_jobs[name]["if"]), context).value()
        )
        lit_legs = [leg for name in lit_jobs for leg in suite_leg_names(all_jobs[name])]
        required = suite_plan_for(value, context, root / f"{event}-{selection or 'none'}")["requiredLegs"]
        group = Expression(str(value["concurrency"]["group"]), context).value()
        rows.append((event, selection, lit_jobs, group, lit_legs == required))
    return rows


def test_suite_truth_table_lights_exactly_the_planned_legs(tmp_path: Path) -> None:
    assert suite_truth_table_rows(document(SUITE_WORKFLOW), tmp_path) == SUITE_TRUTH_TABLE


def _swap_tier1_and_arm(value: dict[str, Any]) -> None:
    all_jobs = jobs(value)
    all_jobs["tier1-admission"]["if"], all_jobs["arm-admission"]["if"] = (
        all_jobs["arm-admission"]["if"],
        all_jobs["tier1-admission"]["if"],
    )


SUITE_MUTATIONS = {
    "or-to-and": lambda value: jobs(value)["tier2-pytest"].update(
        {"if": jobs(value)["tier2-pytest"]["if"].replace("||", "&&")}
    ),
    "delete-leg-if": lambda value: jobs(value)["targets-run"].pop("if"),
    "swap-tier1-next-and-arm-next": _swap_tier1_and_arm,
    "drop-group-parentheses": lambda value: value["concurrency"].update(
        {"group": value["concurrency"]["group"].replace("(github", "github", 1).replace("'full')", "'full'", 1)}
    ),
}


@pytest.mark.parametrize("mutation", sorted(SUITE_MUTATIONS))
def test_suite_truth_table_rejects_each_mutation(tmp_path: Path, mutation: str) -> None:
    value = document(SUITE_WORKFLOW)
    SUITE_MUTATIONS[mutation](value)

    assert suite_truth_table_rows(value, tmp_path) != SUITE_TRUTH_TABLE
    assert (
        value["concurrency"]["group"] != SUITE_CONCURRENCY_GROUP
        or {name: jobs(value)[name].get("if") for name in SUITE_LEG_IFS} != SUITE_LEG_IFS
    )


def test_suite_inputs_never_interpolated_in_run_blocks() -> None:
    value = document(SUITE_WORKFLOW)
    for job_name, job in jobs(value).items():
        for step in steps(job):
            body = step.get("run")
            if isinstance(body, str):
                assert "${{ inputs." not in body, (job_name, step.get("name"), body)
                assert "${{ github.event.inputs." not in body, (job_name, step.get("name"), body)
            for key in step.get("env", {}):
                assert str(key) in ALLOWED_SUITE_STEP_ENV, (job_name, key)


def test_suite_jobs_runners_timeouts_and_wave_budget() -> None:
    value = document(SUITE_WORKFLOW)
    expected = {
        "plan-suite": ("ubuntu-latest", 10),
        "build-native": ("windows-latest", 35),
        "tier2-pytest": ("windows-latest", 170),
        "targets-run": ("windows-latest", 45),
        "tier1-admission": ("windows-latest", 45),
        "arm-admission": ("windows-11-arm", 45),
        "windows-suite-summary": ("ubuntu-latest", 15),
    }
    all_jobs = jobs(value)
    assert set(all_jobs) == set(expected)
    for name, (runner, job_timeout) in expected.items():
        actual = all_jobs[name]["runs-on"]
        assert isinstance(actual, str) and "${{" not in actual
        assert actual == runner
        assert type(all_jobs[name]["timeout-minutes"]) is int
        assert all_jobs[name]["timeout-minutes"] == job_timeout

    tier2 = all_jobs["tier2-pytest"]
    assert tier2["needs"] == ["plan-suite", "build-native"]
    assert tier2["strategy"]["fail-fast"] is False
    assert tier2["strategy"]["max-parallel"] == 4
    assert tier2["strategy"]["matrix"]["shard"] == [0, 1, 2, 3]

    for name in ("targets-run", "tier1-admission", "arm-admission"):
        assert all_jobs[name]["needs"] == ["plan-suite", "build-native"]

    summary = all_jobs["windows-suite-summary"]
    assert summary.get("if") == "always()"
    assert summary.get("needs") == [
        "plan-suite",
        "build-native",
        "tier2-pytest",
        "targets-run",
        "tier1-admission",
        "arm-admission",
    ]
    summary_steps = [step for step in steps(summary) if step.get("name") == "Summarize suite results"]
    assert len(summary_steps) == 1
    summary_run = str(summary_steps[0].get("run", ""))
    assert "--mode suite" in summary_run
    assert "--plan-manifest" in summary_run
    inputs = re.findall(r'--input\s+"([^"=]+)=([^"]+)"', summary_run)
    legs = [leg for _, leg in inputs]
    assert set(legs) == {
        "shard-0",
        "shard-1",
        "shard-2",
        "shard-3",
        "targets",
        "tier1-next",
        "arm-next",
    }
    for path, leg in inputs:
        assert path == f"${{{{ runner.temp }}}}/suite-artifacts/windows-suite-{leg}", (path, leg)
    assert "--expected-failures scripts/ci/windows/expected-failures.json" in summary_run
    assert (
        "--plan-manifest \"${{ runner.temp }}/suite-artifacts/windows-suite-plan/plan-manifest.json\""
        in summary_run
    )

    step_timeouts = {
        ("build-native", "Build the editable native module"): 20,
        ("tier2-pytest", "Install the editable test environment"): 20,
        ("tier2-pytest", "Run tier 2 shard waves"): 150,
        ("targets-run", "Install the editable test environment"): 20,
        ("targets-run", "Run targets waves"): 30,
        ("tier1-admission", "Install the editable test environment"): 20,
        ("tier1-admission", "Run tier1-next waves"): 30,
        ("arm-admission", "Install the editable test environment"): 20,
        ("arm-admission", "Run arm-next waves"): 30,
    }
    for (job_name, step_name), minutes in step_timeouts.items():
        matching = [step for step in steps(all_jobs[job_name]) if step.get("name") == step_name]
        assert len(matching) == 1, (job_name, step_name)
        assert type(matching[0].get("timeout-minutes")) is int
        assert matching[0]["timeout-minutes"] == minutes

    expected_budgets = {
        ("tier2-pytest", "Run tier 2 shard waves"): (4, 600, 900, 40, 140, 150, 170),
        ("targets-run", "Run targets waves"): (2, 600, 900, 20, 25, 30, 45),
        ("tier1-admission", "Run tier1-next waves"): (2, 300, 600, 20, 25, 30, 45),
        ("arm-admission", "Run arm-next waves"): (2, 300, 600, 20, 25, 30, 45),
    }
    for (job_name, step_name), (workers, watchdog, idle, wave, deadline, step_m, job_m) in expected_budgets.items():
        step = next(item for item in steps(all_jobs[job_name]) if item.get("name") == step_name)
        body = str(step.get("run", ""))
        assert "--tier 2" in body
        assert f"--workers {workers}" in body
        assert f"--watchdog-seconds {watchdog}" in body
        assert f"--idle-seconds {idle}" in body
        assert f"--wave-minutes {wave}" in body
        assert f"--deadline-minutes {deadline}" in body
        assert f"--step-minutes {step_m}" in body
        assert f"--job-minutes {job_m}" in body
        assert watchdog < idle < wave * 60 < deadline * 60 < step_m * 60 <= (job_m - 15) * 60
        assert "WINDOWS_SUITE_ACL_PROBE" in (step.get("env") or {})
        install_step = next(
            item for item in steps(all_jobs[job_name]) if item.get("name") == "Install the editable test environment"
        )
        assert "HYPRIAL_BUILD_NATIVE" not in (install_step.get("env") or {})


def test_suite_env_uploads_pwsh_and_sensitive_text() -> None:
    value = document(SUITE_WORKFLOW)
    text = SUITE_WORKFLOW.read_text(encoding="utf-8")
    assert not re.search(r"(?im)^\s*[A-Z_]*(?:HTTP_PROXY|HTTPS_PROXY|ALL_PROXY|NO_PROXY)\s*:", text)
    assert "continue-on-error" not in text
    assert "Start-Process" not in text
    assert PRIVATE_IP.search(text) is None
    emails = [match.group(0) for match in EMAIL.finditer(text) if match.group(1) != "example.com"]
    assert emails == []

    top_env = [str(key).casefold() for key in value.get("env", {})]
    assert len(top_env) == len(set(top_env))
    upload_count = 0
    for job in jobs(value).values():
        job_steps = steps(job)
        for index, step in enumerate(job_steps):
            if step.get("uses", "").split("@", 1)[0] == "actions/upload-artifact":
                upload_count += 1
                assert isinstance(step.get("id"), str) and step["id"]
                assert type(step.get("timeout-minutes")) is int and step["timeout-minutes"] == 10
                assert step.get("if") == "always()"
                assert step.get("with", {}).get("if-no-files-found") == "error"
                assert index + 1 < len(job_steps)
                next_run = str(job_steps[index + 1].get("run", ""))
                assert f"steps.{step['id']}.outcome" in next_run
        if job.get("runs-on") == "ubuntu-latest":
            continue
        job_keys = [str(key).casefold() for key in job.get("env", {})]
        assert len(job_keys) == len(set(job_keys))
        for step in job_steps:
            step_keys = [str(key).casefold() for key in step.get("env", {})]
            assert len(step_keys) == len(set(step_keys))
            body = step.get("run")
            if not isinstance(body, str):
                continue
            lines = [line.strip() for line in body.splitlines() if line.strip()]
            native_indices = [idx for idx, line in enumerate(lines) if NATIVE.match(line)]
            assert len(native_indices) <= 1, body
            if native_indices:
                trailing = lines[native_indices[0] + 1 :]
                assert trailing == ["$code = $LASTEXITCODE", "exit $code"], body
    assert upload_count == 6


def test_suite_paths_are_neutral_and_consistent() -> None:
    value = document(SUITE_WORKFLOW)
    text = SUITE_WORKFLOW.read_text(encoding="utf-8")
    all_jobs = jobs(value)
    runner = "${{ runner.temp }}"
    temp_paths = re.findall(r"runner\.temp\s*\}\}(/[^\"\s']+)", text) + re.findall(
        r"RUNNER_TEMP\\([^\"\s*]+)", text
    )
    assert temp_paths
    assert all(path.lstrip("/\\").startswith("suite-") for path in temp_paths), sorted(set(temp_paths))

    def step_of(job_name: str, name: str) -> dict[str, Any]:
        matching = [step for step in steps(all_jobs[job_name]) if step.get("name") == name]
        assert len(matching) == 1, (job_name, name, len(matching))
        return matching[0]

    def uploads(job_name: str) -> list[tuple[str, str]]:
        return [
            (str(step.get("id")), str(step.get("with", {}).get("path", "")))
            for step in steps(all_jobs[job_name])
            if step.get("uses", "").split("@", 1)[0] == "actions/upload-artifact"
        ]

    def downloads(job_name: str) -> dict[str, str]:
        return {
            str(step.get("with", {}).get("name")): str(step.get("with", {}).get("path", ""))
            for step in steps(all_jobs[job_name])
            if step.get("uses", "").split("@", 1)[0] == "actions/download-artifact"
        }

    def artifacts(job_name: str) -> dict[str, str]:
        return {
            str(step.get("with", {}).get("name")): str(step.get("id"))
            for step in steps(all_jobs[job_name])
            if step.get("uses", "").split("@", 1)[0] == "actions/upload-artifact"
        }

    staging = str(step_of("build-native", "Stage editable native bundle")["run"])
    assert '$out = "$env:RUNNER_TEMP\\suite-native-bundle"' in staging, staging
    assert uploads("build-native") == [
        ("upload-editable-native", f"{runner}/suite-native-bundle/")
    ]
    assert '--out "${{ runner.temp }}/suite-plan/plan-manifest.json"' in str(
        step_of("plan-suite", "Plan test suite shards")["run"]
    )
    assert uploads("plan-suite") == [("upload-suite-plan", f"{runner}/suite-plan/")]

    expected_inputs = {
        "windows-suite-plan": f"{runner}/suite-in/plan",
        "windows-editable-native": f"{runner}/suite-in/native",
    }
    expected_outputs = f"{runner}/suite-results/\n{runner}/suite-ledger/\n"
    for job_name, wave_step, upload_id, artifact in (
        ("tier2-pytest", "Run tier 2 shard waves", "upload-tier2-shard", "windows-suite-shard-${{ matrix.shard }}"),
        ("targets-run", "Run targets waves", "upload-targets", "windows-suite-targets"),
        ("tier1-admission", "Run tier1-next waves", "upload-tier1-next", "windows-suite-tier1-next"),
        ("arm-admission", "Run arm-next waves", "upload-arm-next", "windows-suite-arm-next"),
    ):
        assert downloads(job_name) == expected_inputs, (job_name, downloads(job_name))
        assert artifacts(job_name) == {artifact: upload_id}, (job_name, artifacts(job_name))
        assert uploads(job_name) == [(upload_id, expected_outputs)], (job_name, uploads(job_name))
        restore = str(step_of(job_name, "Restore editable native bundle")["run"])
        assert '"$env:RUNNER_TEMP\\suite-in\\native\\*"' in restore, (job_name, restore)
        wave = str(step_of(job_name, wave_step)["run"])
        assert '"$env:RUNNER_TEMP\\suite-in\\plan\\plan-manifest.json"' in wave, (job_name, wave)
        assert '--results "$env:RUNNER_TEMP\\suite-results"' in wave, (job_name, wave)
        assert '--ledger-root "$env:RUNNER_TEMP\\suite-ledger"' in wave, (job_name, wave)
        assert '--report "$env:RUNNER_TEMP\\suite-results\\run-manifest.json"' in wave, (job_name, wave)

    summary_downloads = [
        step
        for step in steps(all_jobs["windows-suite-summary"])
        if step.get("uses", "").split("@", 1)[0] == "actions/download-artifact"
    ]
    assert len(summary_downloads) == 1
    assert summary_downloads[0].get("with") == {"path": f"{runner}/suite-artifacts"}
