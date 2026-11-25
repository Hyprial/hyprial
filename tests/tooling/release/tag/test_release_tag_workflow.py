"""The release-tag workflow must implement the contract it documents.

Every assertion here exists because the property it pins is one whose loss
would be **silent**: the job would still run, still go green, and still create
a tag -- only the guarantee would be gone.
"""

from pathlib import Path

import yaml

from tests.support.ci.ci_workflow_policy import unpinned_uses_offenders_in


WORKFLOW = (
    Path(__file__).resolve().parents[4] / '.github/workflows/release-tag.yml'
)
SYNC_WORKFLOW = (
    Path(__file__).resolve().parents[4] / '.github/workflows/homepage-changelog-sync.yml'
)


def _doc() -> dict:
    # PyYAML parses the bare `on:` key as the boolean True (YAML 1.1); accept
    # either so the test pins the workflow, not the parser's spelling.
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    doc["_triggers"] = doc.get("on", doc.get(True))
    return doc


def test_the_job_can_only_be_dispatched_never_triggered_by_a_push() -> None:
    """A push trigger here would dissolve the authorization model.

    Authorising a *job* has no "spent once" property; what replaces it is that
    every run records its inputs.  A ``push`` trigger would create runs with no
    inputs at all -- and it would create them without anyone asking.
    """
    triggers = _doc()["_triggers"]
    assert set(triggers) == {"workflow_dispatch"}, triggers


def test_all_three_inputs_are_explicit_and_dry_run_defaults_to_true() -> None:
    """Nothing that does not travel through a parameter can be audited later."""
    inputs = _doc()["_triggers"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"tag", "sha", "dry_run"}, sorted(inputs)
    assert inputs["tag"]["required"] is True
    assert inputs["sha"]["required"] is True
    # the default is the safe direction: a real push has to be asked for
    assert str(inputs["dry_run"]["default"]).lower() == "true"


def test_the_sha_input_refuses_ref_names() -> None:
    """``dev`` names a branch AND a lagging tag in this repo, and the server
    resolves the name to the tag.  A sha cannot change meaning between two
    readers; a ref name can."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "'^[0-9a-f]{40}$'" in text
    assert "ref names are not accepted" in text


def test_every_rejection_says_whether_the_pinned_sha_is_still_dev() -> None:
    """"Version mismatch" has two causes with opposite remedies: a typo (retype)
    and the repository having moved (go back for the authorized sha).  Read as
    the first, the operator retypes a value *he believes is right* -- which is
    him editing the authorization.  That misreading is also the cheaper one, so
    it is the one this line has to prevent."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "pinned_state" in text
    # each of the three gates must carry it, not just the one that inspired it
    assert text.count('${pinned_state}') >= 3, text.count('${pinned_state}')


def test_the_actual_sha_reaches_the_report_body_not_only_the_log() -> None:
    """The three gates ask whether a sha is *admissible*, never whether it is
    *the authorized one* -- provenance is not a property, so no gate can answer
    it.  The report row is the only place a substitution becomes visible after
    the fact, which is why it must not be demoted to a log line."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "GITHUB_STEP_SUMMARY" in text
    assert "sha actually used" in text
    assert "provenance question" in text


def test_the_report_is_written_from_a_trap_so_a_failure_still_reports() -> None:
    """A report emitted only on success does not say *what happened*, it says
    *whether it went well* -- which the exit code already said."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "trap report EXIT" in text


def test_readback_distinguishes_lightweight_from_absent() -> None:
    """A lightweight tag has no ``^{}`` line, so peeling it reads as empty --
    which looks exactly like "the tag is not there".  v0.4.0 in this very repo
    is lightweight, so this is a live case, not a hypothetical."""
    text = WORKFLOW.read_text(encoding="utf-8")
    assert 'refs/tags/${TAG_NAME}^{}' in text
    assert "LIGHTWEIGHT" in text
    assert "is absent after a push that reported success" in text


def test_this_job_does_not_also_move_track_tags() -> None:
    """A release anchor and a track pointer are two actions; bound into one job
    there is no way to do only one of them."""
    text = WORKFLOW.read_text(encoding="utf-8")
    body = text.split("name: release-tag", 1)[1]
    for moving in ("internal", "nightly", "stable"):
        assert f'"{moving}"' not in body, moving
        assert f"tag -a {moving}" not in body


def test_a_step_that_sources_from_the_tree_has_a_checkout_before_it() -> None:
    """`git init` leaves an EMPTY working tree.

    Found in review: the first draft fetched twice and never checked out, so
    `source scripts/ci/ci-step.sh` in the next step would have died on its first
    line and **not one of the three gates would have run** -- while the job
    still looked like it had gates.  Every other job in this repo checks out
    after fetching, so a workflow copied from this one will make the same
    omission; this test is what makes it loud.
    """
    # ⚠️ Compare EXECUTABLE lines only.  The first version of this test matched
    # the comment that *mentions* `source scripts/ci/ci-step.sh` -- which sits
    # above the checkout -- and failed on correct code.  A grep counts
    # mentions, not uses.
    lines = [
        line for line in WORKFLOW.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    ]
    sourced = next(
        (i for i, line in enumerate(lines) if "source scripts/" in line), None
    )
    assert sourced is not None, "no step reads anything from the tree; retire this test"
    checked_out = next(
        (i for i, line in enumerate(lines) if "git checkout" in line), None
    )
    assert checked_out is not None, "the job never checks out, so the tree is empty"
    assert checked_out < sourced, (
        "the checkout must come before the first step that reads from the tree"
    )


def test_the_sha_format_gate_constrains_every_character() -> None:
    """A shell `case` glob like `[0-9a-f]*` constrains only the FIRST character.

    Found in review: a 40-char string with hex at the front and rubbish after it
    passed the format gate, then failed later as a git error rather than as the
    message written here -- the gate's promise and its behaviour disagreed.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "'^[0-9a-f]{40}$'" in text
    assert 'case "${IN_SHA}" in' not in text

def test_the_job_is_truthful_about_what_happens_after_the_push() -> None:
    """The workflow must answer "and then what?", and answer it correctly.

    History, kept because it is the reason this is pinned at all.  The first
    version ended by announcing that github-publish "will pick this up from the
    tag push", and that claim survived two review rounds: both readers checked
    the three gates, and neither asked what happens *after* the push.  It was
    false -- release-tag run 4092 pushed `v0.4.6` -> dd13e702 and **zero**
    github-publish runs followed, because a tag pushed with
    `secrets.GITHUB_TOKEN` does not trigger workflows (upstream stops the actions
    user from starting runs so a job cannot trigger itself forever).  A job's
    acceptance stops at its own exit code, while its point usually lives in the
    step after it.

    This test then pinned the honest admission "github-publish is NOT started by
    this push".  ⚠️ That string is now stale -- not because the guard was
    inconvenient, but because the gap it described was **closed**: the job now
    dispatches github-publish explicitly.  The purpose is unchanged (the
    workflow's self-description must match reality, and "what happens next" must
    be answered); only the value being pinned moves with the reality.

    So this pins the closed loop, and keeps the one claim that must never come
    back -- that a *tag push* is enough.
    """
    text = WORKFLOW.read_text(encoding="utf-8")

    # The original false claim, and any return of it.
    assert "will pick this up from the tag push" not in text, (
        "a tag push does not start github-publish; only an explicit dispatch does"
    )
    # The mechanism explanation stays: it is why the dispatch step has to exist.
    assert "does not trigger workflows" in text

    # The loop is closed by an explicit dispatch of github-publish, at the tag.
    assert "github-publish.yml/dispatches" in text, (
        "nothing starts the publish; the tag would be created and then nothing "
        "would happen -- the exact failure this test was written for"
    )
    assert "Start github-publish against the tag just created" in text

    # ⚠️ Its guard must read the OUTCOME, not the input.  `success() &&` cannot be
    # dropped: an explicit `if` REPLACES the implicit success(), so without it the
    # step would run after the push step failed.  And the dry-run decision must
    # come from env.DRY (what the gates actually computed), never from
    # github.event.inputs.dry_run -- an omitted input is the empty string, and
    # `'' != 'true'` is true, which would publish a tag that was never created.
    assert "if: ${{ success() && env.DRY == 'false' }}" in text
    assert "github.event.inputs.dry_run != 'true'" not in text

    # A dispatch that is accepted is not a run that started, and a silent
    # non-publish is the disease being cured -- so the step reads back and is red
    # when nothing appeared.
    assert "Accepted is not started" in text

def test_any_uses_in_this_workflow_pins_its_version() -> None:
    """#503: a remote action is fetched before the step runs, so the script's
    own fail-soft cannot cover it -- which is why this workflow still uses none.

    Since Allen 2026-09-21 the repo allows `uses:` with a pinned ref, so the
    invariant here is no longer "never" but "if present, pinned".  Reading it
    the old way would leave this workflow silently exempt from the new rule.
    """

    offenders = unpinned_uses_offenders_in(
        WORKFLOW.read_text(encoding="utf-8"), WORKFLOW.name
    )
    assert offenders == [], "unpinned `uses:` in release-tag.yml:\n" + "\n".join(
        offenders
    )
    control = "jobs:\n  tag:\n    runs-on: ubuntu-latest\n    steps:\n" \
        "      - uses: actions/checkout@main\n"
    assert unpinned_uses_offenders_in(control) != [], (
        "the same reader must reject a floating ref, or this test proves nothing"
    )


def test_homepage_sync_is_dispatch_only_and_has_per_tag_concurrency() -> None:
    """The sync must never be a PR/push listener, and one tag must serialize.

    Inputs are explicit; there is no default that could select a moving ref.
    """
    doc = yaml.safe_load(SYNC_WORKFLOW.read_text(encoding="utf-8"))
    triggers = doc.get("on", doc.get(True))
    assert set(triggers) == {"workflow_dispatch"}, triggers
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "source_tag" in inputs
    assert "source_sha" in inputs
    assert doc["concurrency"]["group"] == "homepage-changelog-${{ inputs.source_tag }}"


def test_formal_publication_is_held_until_candidate_evidence_is_reviewed() -> None:
    job = _doc()["jobs"]["tag"]
    assert job["env"]["FORMAL_PUBLICATION_ENABLED"] == "false"
    step = next(s for s in job["steps"] if s["name"] == "Create and push the tag")
    run = step["run"]
    assert run.index('"${DRY}" = "true"') < run.index('"${FORMAL_PUBLICATION_ENABLED:-false}" != "true"')
    assert run.index('"${FORMAL_PUBLICATION_ENABLED:-false}" != "true"') < run.index('tag -a "${TAG_NAME}"')
    assert 'refs/remotes/origin/dev' in WORKFLOW.read_text(encoding="utf-8")
    assert 'refs/remotes/origin/main' not in WORKFLOW.read_text(encoding="utf-8")
