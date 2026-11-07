"""A moving tag can move backwards, and nothing about that reads as an error.

`upgrade_available` used to answer one question: does the installed commit
differ from the resolved one? On a *movable* track that is not enough. Withdraw
a bad release, repoint the track at an earlier commit, and every machine on that
track installs an older build -- emitting the same log line as a normal upgrade,
because from the code's point of view it *is* one.

⚠️ The interesting half of this file is not that rollbacks are refused. It is
that **deliberate** downgrades still work. `hyprial daemon upgrade --tag v0.4.1` is a person
choosing to go back, and a "never downgrade" implementation would pass every
rollback test in here while silently removing the one path whose entire purpose
is to downgrade. That test is the control group, and without it the suite cannot
tell a correct guard from a blanket refusal.
"""

from __future__ import annotations

from pathlib import Path

from hyprial.shell.impl.cli.commands.upgrade import commands as upgrade_commands

from hyprial.daemon.impl.updates import (
    Installation,
    RemoteResolution,
    resolution_is_a_downgrade,
    upgrade_available,
)



def _installed(version: str | None, commit: str | None = "c_installed") -> Installation:
    return Installation(
        version=version,
        commit=commit,
        requested_revision="v0.4.2",
        url="https://example.invalid/hyprial.git",
    )


def _remote(version: str | None, commit: str = "c_remote") -> RemoteResolution:
    tag = f"v{version}" if version else "nightly"
    return RemoteResolution(tag=tag, commit=commit, version=version)


def test_a_rolled_back_track_is_not_an_upgrade() -> None:
    """The case that had no name before.

    Same shape as a normal upgrade -- commits differ -- and that is exactly why
    it went through: nothing in the old check looked at *direction*.
    """

    assert upgrade_available(_installed("0.4.2"), _remote("0.4.1")) is False


def test_a_rolled_back_track_is_refused_even_across_a_dev_version() -> None:
    """`0.4.2.dev7` is newer than `0.4.1`, and the guard has to agree."""

    assert upgrade_available(_installed("0.4.2.dev7"), _remote("0.4.1")) is False


# ---------------------------------------------------------------------------
# ⭐ The control group: the guard must not eat the escape hatch.
# ---------------------------------------------------------------------------


def test_an_operator_who_asks_for_an_older_tag_still_gets_it() -> None:
    """Without this, "never downgrade" passes every other test in this file.

    `--tag v0.4.1` is the documented escape hatch ("an explicit --tag is the
    operator's escape hatch and always wins"). A guard that refuses it has not
    made the machine safer; it has removed the one lever a person has when a
    release turns out bad -- which is precisely the situation rollbacks exist
    for.
    """

    assert (
        upgrade_available(
            _installed("0.4.2"), _remote("0.4.1"), operator_chose_the_tag=True
        )
        is True
    )


def test_the_guard_is_on_by_default_so_a_forgetful_caller_keeps_it() -> None:
    """Defaults decide what happens to code nobody re-read.

    A new call site that omits the flag gets the guard. The other default would
    lose the protection silently, and silently is how it would stay.
    """

    assert upgrade_available(_installed("0.4.2"), _remote("0.4.1")) is False


# ---------------------------------------------------------------------------
# Everything the guard must NOT change.
# ---------------------------------------------------------------------------


def test_a_real_upgrade_is_still_available() -> None:
    assert upgrade_available(_installed("0.4.1"), _remote("0.4.2")) is True


def test_same_version_different_commit_is_still_an_upgrade() -> None:
    """The property the original docstring defends, kept intact.

    A branch install or an untracked wheel can carry the same version string
    over different code. That is not a downgrade, and the guard must not read
    it as one.
    """

    assert upgrade_available(_installed("0.4.2"), _remote("0.4.2")) is True


def test_the_exact_installed_commit_is_still_a_no_op() -> None:
    assert (
        upgrade_available(_installed("0.4.2"), _remote("0.4.2", commit="c_installed"))
        is False
    )


def test_an_unreadable_version_does_not_become_a_refusal() -> None:
    """"Cannot tell" must not turn into "refuse".

    A machine that stops upgrading because it could not parse its own version
    would go quiet in the way that takes days to notice -- and the symptom
    ("no upgrades happening") looks identical to "there is nothing to upgrade
    to".
    """

    assert upgrade_available(_installed(None), _remote("0.4.1")) is True
    assert upgrade_available(_installed("not-a-version"), _remote("0.4.1")) is True
    assert upgrade_available(_installed("0.4.2"), _remote(None)) is True


def test_no_commit_evidence_still_reinstalls() -> None:
    """Unchanged behaviour: without PEP 610 evidence, reinstall."""

    assert upgrade_available(_installed("0.4.2", commit=None), _remote("0.4.2")) is True


# ---------------------------------------------------------------------------
# A guard that leaves no trace cannot be seen to have worked.
# ---------------------------------------------------------------------------


def test_the_predicate_itself_is_available_to_callers() -> None:
    """The caller has to be able to say *why* it declined, not just that it did.

    `upgrade_available` returns a bool, and a bool cannot distinguish "already
    there" from "refused to go backwards". Those are different events and the
    second one is the interesting one.
    """

    assert resolution_is_a_downgrade(_installed("0.4.2"), _remote("0.4.1")) is True
    assert resolution_is_a_downgrade(_installed("0.4.1"), _remote("0.4.2")) is False
    assert resolution_is_a_downgrade(_installed(None), _remote("0.4.1")) is False


def test_the_cli_does_not_report_a_declined_downgrade_as_a_no_op() -> None:
    """⚠️ The old sentence was not merely vague, it was false.

    "already at the resolved tag's commit" says we are standing where the tag
    points. When the track has rolled back we are *not* -- we declined to move
    there. Someone reading that line would conclude nothing happened and there
    was nothing to happen, which is exactly the state a rollback should not be
    able to hide in.

    Checked structurally against the CLI source: the no-op branch must consult
    the downgrade predicate and must carry a distinct marker.
    """

    source = Path(upgrade_commands.__file__).read_text("utf-8")
    branch = source[source.index("def _perform_upgrade") :][:20000]
    assert "resolution_is_a_downgrade(" in branch, (
        "the no-op branch cannot tell a declined downgrade from a real no-op"
    )
    assert "declinedDowngrade" in branch, (
        "a guard that leaves no trace is indistinguishable from a rollback "
        "that never happened"
    )
    assert "refusing to move backwards" in branch
