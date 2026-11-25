"""A night with no promotion is healthy -- and it must not look like a failure.

`release-nightly-promote.yml` says so in its own contract: "A night with no promotion is
a healthy, expected outcome (red gate, SHA drift between the two gates, or a
missing marker)". Every one of those paths is written to print `NO-PROMOTE` and
exit 0.

⚠️ One of them could not. Under `set -euo pipefail`, reading a marker file that
does not exist killed the script before the guard that names that exact case,
so the job went **red** on a healthy night. A workflow that is red for normal
reasons trains everyone to ignore it, and after that a real failure is invisible
too -- the guard and the alarm share one channel, and the noise wins.

These run **the real `run:` block out of the real workflow file**, not a copy of
it. A copy would pass while the file drifted, and this repository has already
been bitten by "two places hold the same thing, and nobody notices the half that
is wrong".

`git` is stubbed so nothing touches the network. The stub records every
invocation, which is what makes the strongest assertion here possible: on every
NO-PROMOTE path, **`git push` is never called**. "It printed NO-PROMOTE" and "it
did not move the tag" are different claims, and only the second one is the
contract for E2E selection. Since Allen's 2026-10-26 ruling, a separate
reconciliation step applies the stable floor even when selection says
NO-PROMOTE; real-repository tests in release/track cover that second step.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOW = ROOT / ".github" / "workflows" / "release-nightly-promote.yml"

GIT_STUB = """#!/bin/sh
printf '%s\\n' "$*" >> "$GIT_CALLS"
case "$1" in
  ls-remote) exit "${STUB_LS_REMOTE_RC:-0}" ;;
  *) exit 0 ;;
esac
"""

# Forgejo injects these into every step's environment; the real run block
# builds its origin URL and extraHeader key from them, so the stub env must
# provide them too -- otherwise `set -u` kills the script for a reason that
# cannot occur in CI, and every test here would be red for a lie.
CI_DEFAULT_ENV = {
    "GITHUB_SERVER_URL": "http://tailscale:3000/",
    "GITHUB_REPOSITORY": "HyprialOS/harness-bridge",
}


def _run_block() -> str:
    """Pull the promotion shell out by step identity, not list position."""

    document = yaml.safe_load(WORKFLOW.read_text("utf-8"))
    steps = document["jobs"]["promote"]["steps"]
    script = next(
        str(step["run"])
        for step in steps
        if step.get("name")
        == "Verify dual-gate green on one SHA, then move the nightly tag"
    )
    assert "NO-PROMOTE" in script and 'echo "candidate=${sha_a}"' in script, (
        "the run block was not extracted correctly; the rest of this file "
        "would be asserting against the wrong text"
    )
    return script


def _stage(tmp_path: Path, markers: dict[str, object]) -> tuple[Path, Path]:
    work = tmp_path / "work"
    work.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(GIT_STUB)
    git.chmod(git.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    for name, payload in markers.items():
        (work / name).write_text(
            payload
            if isinstance(payload, str)
            # ⚠️ Compact, exactly as e2e-macos-isolated.yml writes it. `json.dumps` defaults
            # put a space after each colon, and the consumer's sed pattern does
            # not allow one -- a fixture in the prettier shape would make every
            # marker look "malformed" and quietly test nothing.
            else json.dumps(payload, separators=(",", ":")),
            "utf-8",
        )
    return work, bin_dir


def _promote(tmp_path: Path, markers: dict[str, object]) -> tuple[int, str, str]:
    work, bin_dir = _stage(tmp_path, markers)
    calls = tmp_path / "git-calls.txt"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GIT_CALLS": str(calls),
        "GITHUB_TOKEN": "t0ken",
        "FRESHNESS_SECONDS": "14400",
        # The runner gives every step a step-output file; the mover writes
        # `promoted=<sha>` there only after a real move (read by the dispatch).
        "GITHUB_OUTPUT": str(tmp_path / "github-output.txt"),
        **CI_DEFAULT_ENV,
    }
    completed = subprocess.run(
        ["bash", "-c", _run_block()],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    recorded = calls.read_text("utf-8") if calls.exists() else ""
    return completed.returncode, completed.stdout + completed.stderr, recorded


def _fresh(sha: str) -> dict[str, object]:
    return {"sha": sha, "epoch": int(time.time())}


def test_the_writer_and_the_reader_still_agree_on_the_marker_shape() -> None:
    """The coupling nothing else checks.

    The gate writes the marker with a heredoc and the promoter reads it with a
    `sed` pattern -- two files, one undeclared format. Reformat the writer (a
    `jq` rewrite, a Python emitter, anything that adds a space after a colon)
    and every marker starts reading as malformed: NO-PROMOTE, exit 0, green
    job, and the tag silently stops moving. That failure announces itself as a
    healthy night.

    ⚠️ This does not verify the whole format -- it pins the one property the
    reader's pattern depends on: no space between key and value.
    """

    writer = (ROOT / ".github" / "workflows" / "e2e-macos-isolated.yml").read_text("utf-8")
    assert '{"sha":"' in writer, (
        "the gate no longer writes compact JSON; nightly-promote's sed pattern "
        "requires it, and a mismatch degrades to a permanently quiet NO-PROMOTE"
    )
    assert '"epoch":$(' in writer, "the epoch field changed shape"


def test_a_missing_marker_is_a_quiet_no_promote_not_a_red_job(
    tmp_path: Path,
) -> None:
    """The guard that could not be reached.

    Before the fix this exited 1 with a bare `sed: ... No such file`, because
    `set -e` killed the script at the read. The message it was supposed to print
    was three lines further down.
    """

    rc, output, calls = _promote(tmp_path, {"gate-e2e.json": _fresh("aaa")})
    assert rc == 0, f"a missing marker must not fail the job; got rc={rc}\n{output}"
    assert "NO-PROMOTE" in output
    assert "gate-e2e-controlled.json is absent" in output
    assert "push" not in calls, "the tag was moved on a night that should not promote"


def test_both_markers_missing_is_also_quiet(tmp_path: Path) -> None:
    rc, output, calls = _promote(tmp_path, {})
    assert rc == 0
    assert "gate-e2e.json is absent" in output
    assert "push" not in calls


def test_a_present_but_malformed_marker_says_so_differently(
    tmp_path: Path,
) -> None:
    """Absent and malformed need different fixes, so they get different words.

    Absent means a gate never recorded green; malformed means one did and wrote
    something unusable. Collapsing them sends whoever reads it to the wrong
    place.
    """

    rc, output, calls = _promote(
        tmp_path,
        {"gate-e2e.json": "{}", "gate-e2e-controlled.json": _fresh("aaa")},
    )
    assert rc == 0
    assert "malformed" in output
    assert "push" not in calls


def test_two_gates_on_different_shas_never_promote(tmp_path: Path) -> None:
    rc, output, calls = _promote(
        tmp_path,
        {
            "gate-e2e.json": _fresh("aaa111"),
            "gate-e2e-controlled.json": _fresh("bbb222"),
        },
    )
    assert rc == 0
    assert "different commits" in output
    assert "push" not in calls
    # No E2E candidate. The later stable-floor reconciliation is independent.
    out = tmp_path / "github-output.txt"
    assert "promoted=" not in (out.read_text("utf-8") if out.exists() else "")


def test_a_stale_marker_never_promotes(tmp_path: Path) -> None:
    old = {"sha": "aaa111", "epoch": int(time.time()) - 90_000}
    rc, output, calls = _promote(
        tmp_path,
        {"gate-e2e.json": old, "gate-e2e-controlled.json": _fresh("aaa111")},
    )
    assert rc == 0
    assert "stale marker" in output
    assert "push" not in calls


def test_the_happy_path_still_selects_the_exact_candidate(tmp_path: Path) -> None:
    """⭐ The positive control.

    Without it, every assertion above would pass on a workflow that promotes
    nothing ever -- "it did not push" is trivially true for a script that cannot
    reach the push at all, and that is precisely the class of bug this file
    exists to catch.
    """

    rc, output, calls = _promote(
        tmp_path,
        {
            "gate-e2e.json": _fresh("aaa111"),
            "gate-e2e-controlled.json": _fresh("aaa111"),
        },
    )
    assert rc == 0, output
    assert "PROMOTE: both gates green on aaa111" in output
    # Selection keeps all original marker checks. The shared writer (covered
    # with real repositories in track/) applies the stable floor afterwards.
    assert "push" not in calls
    assert "candidate=aaa111" in (tmp_path / "github-output.txt").read_text("utf-8")


def test_no_gate_status_branch_is_quiet_too(tmp_path: Path) -> None:
    """The one guard that was already reachable, kept honest."""

    work, bin_dir = _stage(tmp_path, {})
    calls = tmp_path / "git-calls.txt"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GIT_CALLS": str(calls),
        "GITHUB_TOKEN": "t0ken",
        "FRESHNESS_SECONDS": "14400",
        "STUB_LS_REMOTE_RC": "2",
        **CI_DEFAULT_ENV,
    }
    completed = subprocess.run(
        ["bash", "-c", _run_block()],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0
    assert "ci/gate-status does not exist yet" in completed.stdout


def test_cleanup_hold_blocks_nightly_writer_and_publication() -> None:
    job = yaml.safe_load(WORKFLOW.read_text("utf-8"))["jobs"]["promote"]
    assert job["env"].get("NIGHTLY_PUBLICATION_ENABLED") == "false"
    guarded = [
        step for step in job["steps"]
        if step.get("id") == "move"
        or step.get("name") == "Dispatch the GitHub publication of the moved nightly tag"
    ]
    assert len(guarded) == 2
    for step in guarded:
        assert "env.NIGHTLY_PUBLICATION_ENABLED == 'true'" in step.get("if", "")
