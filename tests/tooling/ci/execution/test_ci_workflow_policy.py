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

5. ``permissions:`` is forbidden while this instance ignores it.  Forgejo
   16.0.5 SILENTLY drops the key (support is WIP upstream, ``forgejo#13079``;
   ``forgejo#13164`` only adds a warning), so the block creates the belief that
   the token was narrowed when nothing changed.

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


# --- gate 2: `permissions:` is forbidden while the instance ignores it -------

# The reason and the reversal are shared by the detected case and the
# fail-closed unparsable case, so the off-switch cannot drift between them.
PERMISSIONS_REASON_AND_REVERSAL = (
    "Forgejo 16.0.5 SILENTLY IGNORES this key: token-permission support is still WIP\n"
    "upstream (forgejo#13079), and forgejo#13164 only adds a warning. Nothing about the\n"
    "automatic token changed -- the block creates the belief that it did.\n"
    "Do not add it.\n"
    "WHEN this instance supports `permissions:` (forgejo#13079 lands), THIS TEST MUST BE\n"
    "INVERTED: pull_request-triggered workflows must then declare\n"
    "`permissions: contents: read` and widen per step/job. That reversal is written here,\n"
    "in the failure, on purpose: a rule whose lifetime is conditional must state its\n"
    "off-switch where the next reader cannot miss it, or today's \"forbidden\" becomes\n"
    "tomorrow's stale gate."
)

PERMISSIONS_FAILURE = (
    "{location}: declares `permissions:`.\n" + PERMISSIONS_REASON_AND_REVERSAL
)

PERMISSIONS_UNVERIFIABLE = (
    "{location}: the workflow does not parse as YAML, so `permissions:` cannot be "
    "structurally verified; fail closed.\n" + PERMISSIONS_REASON_AND_REVERSAL
)


def permissions_offenders() -> list[str]:
    """Every top-level or job-level ``permissions:``, by STRUCTURED YAML.

    Textual detection would false-positive on ``permissions:`` inside a ``run:``
    heredoc, so this reads the parsed document only and names the exact location
    as ``{file}::{job}`` / ``{file}::top-level`` (the ``JobRef.key`` style).  An
    unparsable workflow fails closed: it cannot be verified.
    """

    offenders: list[str] = []
    for path in policy.workflow_files():
        try:
            document = yaml.safe_load(policy.workflow_text(path))
        except yaml.YAMLError as error:
            offenders.append(
                PERMISSIONS_UNVERIFIABLE.format(location=f"{path.name}::top-level")
                + f"\n(parse error: {error})"
            )
            continue
        if not isinstance(document, dict):
            continue
        locations: list[str] = []
        if document.get("permissions") is not None:
            locations.append(f"{path.name}::top-level")
        for name, job in (document.get("jobs") or {}).items():
            if isinstance(job, dict) and job.get("permissions") is not None:
                locations.append(f"{path.name}::{name}")
        offenders.extend(
            PERMISSIONS_FAILURE.format(location=location) for location in locations
        )
    return offenders


def test_no_workflow_declares_permissions() -> None:
    """Forgejo 16.0.5 silently ignores `permissions:`, so the block buys false
    assurance, not a narrower token.  Measured 2026-10-09: 0 of 14.
    """

    offenders = permissions_offenders()
    assert offenders == [], "workflow declares the ignored key:\n" + "\n".join(
        offenders
    )


def test_permissions_mutation_is_rejected(monkeypatch, tmp_path) -> None:
    (tmp_path / "bad2.yml").write_text("permissions: read-all\njobs: {}\n")
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    offenders = permissions_offenders()
    assert offenders, "the ignored key slipped through the structural reader"
    assert "bad2.yml::top-level" in offenders[0]
    assert "declares `permissions:`" in offenders[0]
    assert "SILENTLY IGNORES" in offenders[0]
    assert "forgejo#13079" in offenders[0]
    assert "INVERTED" in offenders[0]
    assert "permissions: contents: read" in offenders[0]


def test_unparsable_workflow_fails_closed_on_permissions(monkeypatch, tmp_path) -> None:
    """A file that cannot be parsed cannot be verified: fail closed, and carry
    the same reason/reversal so the off-switch is never lost."""

    (tmp_path / "broken.yml").write_text("permissions: [unclosed\njobs: {}\n")
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    offenders = permissions_offenders()
    assert offenders and "cannot be" in offenders[0]
    assert "forgejo#13079" in offenders[0]


def test_job_level_permissions_are_rejected_at_the_job_location(
    monkeypatch, tmp_path
) -> None:
    (tmp_path / "bad3.yml").write_text(
        "jobs:\n  deploy:\n    runs-on: ubuntu-latest\n"
        "    permissions: write-all\n    steps: []\n"
    )
    monkeypatch.setattr(policy, "WORKFLOW_DIR", tmp_path)
    offenders = permissions_offenders()
    assert offenders and "bad3.yml::deploy" in offenders[0]


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
