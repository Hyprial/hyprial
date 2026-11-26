"""Discovery contract and the secret-in-argv ban of the workflow tree.

``tests.support.ci.ci_workflow_policy`` is the reader shared by the download
audit and by the GitHub merge-gate policy (``tests/tooling/ci/gate``).  This
module pins its two entry points that do not belong to the gate policy:

* discovery covers ``*.yml`` and ``*.yaml`` and finds jobs structurally, so a
  newly added workflow cannot escape any scan built on it;
* no workflow expands a secret-named variable into the argv of an external
  command, because an argv value is visible in the process table and in any
  trace of the command line.

The ``pull_request_target`` ban and the read-only-token rule for
``pull_request`` workflows used to be read here with ``yaml.safe_load``; both
are now judged by the compose-based reader in
``tests.support.ci_download.github_gate_policy`` and tested in
``tests/tooling/ci/gate/test_github_gate_policy.py``.  The artifact-upload
tail shape (a later step reporting the upload outcome through
``ci-conditions.sh artifact``) was read from the Forgejo-era workflows and
retired with them; the merge gate's own upload steps are bounded by a test
there.
"""

from pathlib import Path

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
