"""Workflow-policy gates: discovery plus the two token-exposure bans.

Discovery contract (shared by the download audit) comes first.  The two bans
added 2026-10-09 (hq ruling, M2) are the reason this module also reads the
real workflow tree:

4. ``pull_request_target`` is forbidden.  It is the one trigger that gives a
   workflow the BASE repository's secrets AND a WRITE-scoped automatic token
   while it runs against untrusted pull-request code (Forgejo v16.0: "the
   automatic token has write permission to the repository"; "All action steps
   are executed with the FORGEJO_TOKEN and GITHUB_TOKEN environment variables;
   interacting with untrusted code in the pull request may leak these tokens").

5. A workflow triggered by ``pull_request`` declares a read-only token.  On
   GitHub the top-level ``permissions:`` key is honoured, and without it the
   automatic token takes the repository default, which can be write.  The
   top level must be exactly ``contents: read``; a job that needs more widens
   its own ``permissions:``.  Until the 2026-11-25 move to GitHub this gate
   was the opposite (the key was forbidden because Forgejo 16.0.5 ignored
   it); the old failure text named this inversion as its off-switch.

Both gates read through ``policy.workflow_files()`` (globs ``*.yml`` AND
``*.yaml``), so a newly added workflow cannot escape either one, and
monkeypatching ``policy.WORKFLOW_DIR`` gives the mutation checks.
"""

from pathlib import Path

import yaml

import tests.support.ci.ci_workflow_policy as policy


def test_discovery_includes_new_yaml_and_yml(monkeypatch, tmp_path):
    for name in ("new.yml", "new.yaml", "ignored.txt"):
        (tmp_path / name).write_text("jobs: {}\n")
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    assert {p.name for p in policy.workflow_files()} == {"new.yml", "new.yaml"}


def test_job_discovery_is_structural():
    jobs = policy.runner_jobs_in(
        "jobs:\n  new:\n    runs-on: test\n    steps:\n      - run: echo ok\n",
        Path("new.yml"),
    )
    assert [j.key for j in jobs] == ["new.yml::new"]
    assert jobs[0].steps == [{"run": "echo ok"}]


def test_no_workflow_expands_a_secret_named_env_var_in_external_argv() -> None:
    offenders = policy.secret_argv_offenders()
    assert offenders == [], "workflow secret exposed in process argv:\n" + "\n".join(
        offenders
    )


def test_secret_argv_reader_allows_builtin_stdin_and_catches_continuations() -> None:
    safe = (
        "printf 'header = \\\"Authorization: token %s\\\"\\n' \\\"${API_TOKEN}\\\" | "
        "curl --config - https://example.invalid\n"
    )
    assert policy.secret_argv_offenders_in(safe) == []
    unsafe = (
        "curl --silent \\\n"
        "  -H \\\"Authorization: token ${API_TOKEN}\\\" https://example.invalid\n"
    )
    offenders = policy.secret_argv_offenders_in(unsafe, "bad")
    assert offenders and "API_TOKEN" in offenders[0]


# --- gate 1: `pull_request_target` is forbidden ------------------------------

PULL_REQUEST_TARGET = "pull_request_target"

PULL_REQUEST_TARGET_FAILURE = (
    f"`{PULL_REQUEST_TARGET}` is forbidden.\n"
    "It is the one trigger that gives a workflow the BASE repository's secrets and a\n"
    "WRITE-scoped automatic token while it runs against untrusted pull-request code\n"
    "(Forgejo v16.0 reference: \"the automatic token has write permission to the\n"
    "repository\"; \"All action steps are executed with the FORGEJO_TOKEN and\n"
    "GITHUB_TOKEN environment variables; interacting with untrusted code in the pull\n"
    "request may leak these tokens\").\n"
    "Measured baseline: 0 of 14 workflow files use it (2026-10-09).\n"
    "If it is genuinely needed, that is a T3 gate change: get hq-adjutant's review of\n"
    "the exact workflow and of which tokens it will be able to read BEFORE landing."
)


def _trigger_events(document: dict) -> set[str]:
    """Normalize an ``on:`` value (string, list or mapping) to event names.

    PyYAML resolves the bare key ``on`` to the boolean ``True`` (YAML 1.1), so
    the document may carry either the string or the boolean key.  The shape is
    normalized here so the gate never depends on which YAML form was used.
    """

    value = document.get("on")
    if value is None:
        value = document.get(True)  # YAML 1.1: a bare `on:` key parses as True
    if value is None:
        return set()
    if isinstance(value, str):
        return {value}
    if isinstance(value, (list, tuple)):
        return {str(item) for item in value}
    if isinstance(value, dict):
        return {str(key) for key in value}
    return set()


def pull_request_target_offenders() -> list[str]:
    """Files whose parsed trigger set contains ``pull_request_target``.

    STRUCTURAL (parsed YAML), by declared refinement: a textual scan would
    false-positive on a ``run:`` heredoc that merely mentions the trigger (an
    echoed policy reminder).  Belt-and-braces: if a file cannot be parsed as
    YAML it is textually swept on its non-comment lines, so an unparsable
    workflow cannot hide the trigger.
    """

    offenders: list[str] = []
    for path in policy.workflow_files():
        text = policy.workflow_text(path)
        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError:
            document = None
        if isinstance(document, dict):
            if PULL_REQUEST_TARGET in _trigger_events(document):
                offenders.append(f"{path.name}: {PULL_REQUEST_TARGET_FAILURE}")
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            if PULL_REQUEST_TARGET in line:
                offenders.append(
                    f"{path.name}:{lineno}: {PULL_REQUEST_TARGET_FAILURE}"
                )
    return offenders


def test_no_workflow_uses_pull_request_target() -> None:
    """`pull_request_target` is the only trigger that pairs the base
    repository's secrets and a WRITE-scoped automatic token with untrusted
    pull-request code.  Measured 2026-10-09: 0 of 14 workflow files.
    """

    offenders = pull_request_target_offenders()
    assert offenders == [], "workflow uses the forbidden trigger:\n" + "\n".join(
        offenders
    )


def test_pull_request_target_mutation_is_rejected(monkeypatch, tmp_path) -> None:
    (tmp_path / "bad.yml").write_text("on:\n  pull_request_target:\njobs: {}\n")
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    offenders = pull_request_target_offenders()
    assert offenders, "the trigger slipped through the structural reader"
    assert "write permission" in offenders[0]
    assert "FORGEJO_TOKEN" in offenders[0]
    assert "0 of 14" in offenders[0]
    assert "T3" in offenders[0]


def test_a_run_heredoc_mentioning_the_trigger_is_not_a_false_positive(
    monkeypatch, tmp_path
) -> None:
    """A policy reminder echoed inside a step is not a trigger (the refinement)."""

    (tmp_path / "ok.yml").write_text(
        "on: push\n"
        "jobs:\n"
        "  j:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - run: |\n"
        "          echo 'never use pull_request_target here'\n"
    )
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    assert pull_request_target_offenders() == []


def test_unparsable_workflow_is_textually_swept(monkeypatch, tmp_path) -> None:
    """An unparsable file must not be able to hide the trigger."""

    (tmp_path / "broken.yml").write_text("on: [pull_request_target\njobs: {}\n")
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    assert pull_request_target_offenders()


READ_ONLY_PERMISSIONS = {"contents": "read"}

PERMISSIONS_REASON = (
    "GitHub honours the top-level `permissions:` key. A workflow triggered by\n"
    "pull_request must declare exactly `permissions: contents: read` at the top level,\n"
    "so the automatic token cannot write while pull-request code runs; a job that\n"
    "needs more widens its own `permissions:`."
)

PERMISSIONS_MISSING = (
    "{location}: is triggered by pull_request and declares no top-level `permissions:`.\n"
    + PERMISSIONS_REASON
)

PERMISSIONS_TOO_WIDE = (
    "{location}: top-level `permissions:` is {found!r}, not `contents: read`.\n"
    + PERMISSIONS_REASON
)

PERMISSIONS_UNVERIFIABLE = (
    "{location}: the workflow does not parse as YAML, so `permissions:` cannot be "
    "structurally verified; fail closed.\n" + PERMISSIONS_REASON
)


def _triggers(document: dict) -> set[str]:
    declared = document.get("on", document.get(True))
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {str(item) for item in declared}
    if isinstance(declared, dict):
        return {str(key) for key in declared}
    return set()


def permissions_offenders() -> list[str]:
    """Pull-request workflows whose top-level token is not read-only.

    Reads the parsed document only, so ``permissions:`` inside a ``run:``
    heredoc is not mistaken for the key, and names the location as
    ``{file}::top-level``.  An unparsable workflow fails closed.
    """

    offenders: list[str] = []
    for path in policy.workflow_files():
        location = f"{path.name}::top-level"
        try:
            document = yaml.safe_load(policy.workflow_text(path))
        except yaml.YAMLError as error:
            offenders.append(
                PERMISSIONS_UNVERIFIABLE.format(location=location)
                + f"\n(parse error: {error})"
            )
            continue
        if not isinstance(document, dict) or "pull_request" not in _triggers(document):
            continue
        declared = document.get("permissions")
        if declared is None:
            offenders.append(PERMISSIONS_MISSING.format(location=location))
        elif declared != READ_ONLY_PERMISSIONS:
            offenders.append(
                PERMISSIONS_TOO_WIDE.format(location=location, found=declared)
            )
    return offenders


def test_every_pull_request_workflow_declares_a_read_only_token() -> None:
    offenders = permissions_offenders()
    assert offenders == [], "pull-request workflow without a read-only token:\n" + "\n".join(
        offenders
    )


def test_a_pull_request_workflow_without_permissions_is_rejected(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "bad1.yml").write_text("on:\n  pull_request:\njobs: {}\n")
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    offenders = permissions_offenders()
    assert offenders and "bad1.yml::top-level" in offenders[0]
    assert "declares no top-level `permissions:`" in offenders[0]
    assert "permissions: contents: read" in offenders[0]


def test_a_wider_top_level_token_is_rejected(monkeypatch, tmp_path) -> None:
    (tmp_path / "bad2.yml").write_text(
        "on: [pull_request]\npermissions: read-all\njobs: {}\n"
    )
    (tmp_path / "bad3.yml").write_text(
        "on: pull_request\npermissions:\n  contents: write\njobs: {}\n"
    )
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    offenders = permissions_offenders()
    assert len(offenders) == 2
    assert "bad2.yml::top-level" in offenders[0] and "read-all" in offenders[0]
    assert "bad3.yml::top-level" in offenders[1] and "write" in offenders[1]


def test_unparsable_workflow_fails_closed_on_permissions(monkeypatch, tmp_path) -> None:
    (tmp_path / "broken.yml").write_text("permissions: [unclosed\njobs: {}\n")
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    offenders = permissions_offenders()
    assert offenders and "cannot be" in offenders[0]


def test_a_job_may_widen_its_own_token_and_other_triggers_are_not_judged(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "ok1.yml").write_text(
        "on:\n  pull_request:\npermissions:\n  contents: read\n"
        "jobs:\n  label:\n    runs-on: ubuntu-latest\n"
        "    permissions:\n      pull-requests: write\n    steps: []\n"
    )
    (tmp_path / "ok2.yml").write_text("on:\n  push:\njobs: {}\n")
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    assert permissions_offenders() == []


# --- artifact uploads are bounded and visible --------------------------------

UPLOAD_ARTIFACT = "actions/upload-artifact"


def artifact_upload_offenders(document: dict, source: str = "<inline>") -> list[str]:
    """Every upload step needs a timeout, an id, and a later visible warning.

    The 2026-10-29 incident (run 39194) showed upload-artifact retrying at 0.0%
    until the job's own ``timeout-minutes`` killed the job before any test ran.
    A failed upload may keep ``continue-on-error`` so it cannot red the test
    result, but its step outcome must reach the job summary; otherwise it is
    silently swallowed.  Retry attempts are allowed to report through their
    existing "Wait"/retry report, as long as one later step names that exact
    step id and writes ``GITHUB_STEP_SUMMARY``.
    """

    offenders: list[str] = []
    for job_name, job in (document.get("jobs") or {}).items():
        steps = job.get("steps") or []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses") or "")
            if not uses.startswith(UPLOAD_ARTIFACT):
                continue
            where = f"{source}::{job_name}::{step.get('name', index)}"
            timeout = step.get("timeout-minutes")
            if not isinstance(timeout, int) or timeout <= 0:
                offenders.append(f"{where}: upload-artifact lacks step timeout-minutes")
            step_id = step.get("id")
            if not isinstance(step_id, str) or not step_id:
                offenders.append(f"{where}: upload-artifact lacks an id")
                continue
            if "continue-on-error" in step and step["continue-on-error"] is not True:
                offenders.append(f"{where}: continue-on-error must be true or absent")
            visible = False
            runtime_origin = False
            for later in steps[index + 1 :]:
                if not isinstance(later, dict):
                    continue
                condition = str(later.get("if") or "")
                body = str(later.get("run") or "")
                if f"steps.{step_id}.outcome" not in condition + body:
                    continue
                if "GITHUB_STEP_SUMMARY" in body or "ci-conditions.sh artifact" in body:
                    visible = True
                if "ci-conditions.sh" in body and "artifact" in body:
                    runtime_origin = True
                if visible and runtime_origin:
                    break
            if not visible:
                offenders.append(
                    f"{where}: failed/timeout upload is not written to job summary"
                )
            if not runtime_origin:
                offenders.append(
                    f"{where}: upload failure path does not call ci-conditions.sh artifact"
                )
    return offenders


def test_every_artifact_upload_is_bounded_and_visible() -> None:
    import yaml

    offenders: list[str] = []
    for path in policy.workflow_files():
        document = yaml.safe_load(policy.workflow_text(path))
        offenders.extend(artifact_upload_offenders(document, path.name))
    assert offenders == [], "artifact upload policy violations:\n" + "\n".join(offenders)


def test_artifact_upload_policy_rejects_a_timeout_or_visibility_regression() -> None:
    good = """
jobs:
  checks:
    steps:
      - name: Upload
        id: upload
        timeout-minutes: 3
        uses: actions/upload-artifact@v3
        with: {name: x, path: y}
      - name: Warn
        if: always() && steps.upload.outcome == 'failure'
        run: |
          echo "artifact upload failed/timeout" >> "$GITHUB_STEP_SUMMARY"
          bash scripts/ci/ci-conditions.sh artifact || true
"""
    import yaml

    assert artifact_upload_offenders(yaml.safe_load(good)) == []
    no_timeout = good.replace("        timeout-minutes: 3\n", "")
    assert any(
        "timeout" in item for item in artifact_upload_offenders(yaml.safe_load(no_timeout))
    )
    no_summary = good.replace(
        """        run: |
          echo "artifact upload failed/timeout" >> "$GITHUB_STEP_SUMMARY"
          bash scripts/ci/ci-conditions.sh artifact || true
""",
        "        run: echo ignored\n",
    )
    assert any(
        "summary" in item for item in artifact_upload_offenders(yaml.safe_load(no_summary))
    )
