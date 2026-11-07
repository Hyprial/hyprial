"""A squire that cannot find the instruction concludes the capability is missing.

On 2026-08-31 one was asked for a screenshot. It read `harness_send`, saw no
attachment parameter, and told its owner that hyprial could not send images -- five
days after the same setup had sent two PNGs with `hyprial message send --image`. The
capability was there the whole time; the instruction was not.

⚠️ Note what the squire actually did wrong, because the template fix only helps
if it addresses that: it checked **one surface** (`harness_send`'s parameters),
found nothing, and read the absence as a fact about hyprial. That surface does not
carry the answer -- outbound attachments live on `hyprial message send`. So the template now
says both the command *and* that this particular inference is wrong; the second
half is the part that generalises.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from hyprial.daemon.impl.squire.setup import SKILL_ATTACHMENT_MARKER

ROOT = Path(__file__).resolve().parents[4]
PACKAGED = ROOT / "src" / "hyprial" / "daemon" / "impl" / "squire" / "SKILL-template.md"
DOCUMENTED = ROOT / "docs" / "domains" / "squire" / "SKILL-template.md"


def test_the_two_template_copies_are_byte_identical() -> None:
    """Two files holding one thing; the wrong half is the one nobody reads.

    ⚠️ This repository has been bitten by exactly this several times -- a rule
    written twice, corrected once. The copies are pinned by digest rather than
    by "both mention --image", because a weaker check would let them drift in
    every other respect while still passing.
    """

    packaged = hashlib.sha256(PACKAGED.read_bytes()).hexdigest()
    documented = hashlib.sha256(DOCUMENTED.read_bytes()).hexdigest()
    assert packaged == documented, (
        "src/hyprial/daemon/impl/squire/SKILL-template.md and docs/domains/squire/SKILL-template.md "
        "have drifted; whichever one a reader happens to open decides what "
        "they learn, and nothing else compares them"
    )


def test_the_template_tells_a_squire_how_to_send_an_image_and_a_file() -> None:
    text = PACKAGED.read_text("utf-8")
    assert "--image" in text and "--file" in text
    assert SKILL_ATTACHMENT_MARKER in text, (
        "the marker used to detect stale installed skills is no longer in the "
        "template itself, so every installed skill would look stale"
    )


def test_the_template_says_attachments_do_not_go_through_harness_reply() -> None:
    """The command alone would not have prevented this failure.

    The squire never looked for a send command -- it looked at `harness_reply`
    and `harness_send`, found no attachment parameter, and stopped. So the
    template has to name that inference and reject it, not merely offer the
    right incantation elsewhere in the file.
    """

    text = PACKAGED.read_text("utf-8")
    assert "harness_reply" in text
    assert "harness_send" in text, (
        "the template must name the surface that misled the squire; telling "
        "someone the right command does not stop them concluding, from a "
        "different table, that the feature does not exist"
    )


def test_work_item_guidance_does_not_claim_user_proxy_is_refused() -> None:
    text = PACKAGED.read_text("utf-8")
    assert "不接受 user-proxy" not in text


def test_work_item_guidance_uses_org_work_v5_without_retired_space_terms() -> None:
    text = PACKAGED.read_text("utf-8")
    assert "版本行(v5,2026-10-08)" in text
    assert "hyprial org work collect --org <org>" in text
    assert "hyprial org work finish --org <org>" in text
    assert "hyprial org work ls --invalid --org <org>" in text
    assert "hyprial org work ls --org <org>" in text
    assert "workflow missions" not in text
    assert "work.space" not in text
    assert "--space" not in text
    assert "<missions-space>" not in text


def test_conflict_patrol_uses_only_org_work_and_keeps_both_originals() -> None:
    text = PACKAGED.read_text("utf-8")
    paragraph = text.split("**冲突巡检**", 1)[1].split("**已知限制**", 1)[0]

    assert "org work collect" in paragraph
    assert "org work ls --invalid" in paragraph
    assert "org work show" in paragraph and "--copy" in paragraph
    assert "org work resolve" in paragraph and "--keep" in paragraph
    assert paragraph.count("id:<nodeId>") == 1
    assert "org work show <id> --copy id:<nodeId>" in paragraph
    assert "fs ls" not in paragraph
    assert "fs read" not in paragraph
    assert "⛔ 不自动删除任何一份" in paragraph


def test_a_freshly_scaffolded_skill_carries_the_instructions(tmp_path: Path) -> None:
    """⭐ The end-to-end property: what gets written, not what is in the repo.

    The template is rendered through placeholder substitution before it lands on
    disk. Asserting only on the source file would leave "the renderer drops the
    section" undetected, and the file the squire actually reads is the rendered
    one.
    """

    from hyprial.daemon.impl.squire.setup import SquireSetup

    setup = SquireSetup.__new__(SquireSetup)
    setup.skill_template = PACKAGED.read_text("utf-8")
    home = tmp_path / "squire"
    changed = SquireSetup._ensure_scaffold(
        setup,
        home,
        display_name="Allen",
        login_name="allen",
        owner="allen",
        adapter="allen-squire",
        dm_route="owner",
    )
    assert "scaffold.skill" in changed
    rendered = (home / "config" / "shared" / "skills" / "squire" / "SKILL.md").read_text("utf-8")
    assert "--image" in rendered and "--file" in rendered
    assert "<adapter>" not in rendered, "placeholders survived into the skill"
    assert "allen-squire" in rendered


# ---------------------------------------------------------------------------
# ⭐ Existing installs: say so, never rewrite.
# ---------------------------------------------------------------------------


def test_an_old_installed_skill_is_reported_and_left_untouched(
    tmp_path: Path,
) -> None:
    """The promise not to rewrite is worth more than this paragraph.

    People edit their own skill. `_ensure_scaffold` promises never to touch an
    existing byte, and breaking that to deliver a documentation fix would be the
    worse trade. But silence is not the alternative: an install that quietly
    keeps the old file looks exactly like one that is current, and the squire
    reading it will keep telling its owner that attachments are unsupported.
    """

    from hyprial.daemon.impl.squire.setup import SquireSetup

    setup = SquireSetup.__new__(SquireSetup)
    home = tmp_path / "squire"
    skill = home / "config" / "shared" / "skills" / "squire" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    original = "# 我自己改过的 squire skill\n\n出站: hyprial message send --from squire ...\n"
    skill.write_text(original, "utf-8")

    warning = SquireSetup._stale_skill_warning(setup, home)
    assert warning is not None, (
        "an installed skill without the attachment instructions must be "
        "reported; setup cannot fix it and nothing else will notice"
    )
    assert "--image" in warning and str(skill) in warning
    # ⛔ And not one byte moved.
    assert skill.read_text("utf-8") == original


def test_a_current_installed_skill_is_not_reported(tmp_path: Path) -> None:
    """The control group: without it, "always warn" would pass the test above.

    A warning that fires on every install teaches people to ignore it, and then
    it is worth less than nothing -- it would be noise sitting exactly where the
    real notice needs to be seen.
    """

    from hyprial.daemon.impl.squire.setup import SquireSetup

    setup = SquireSetup.__new__(SquireSetup)
    home = tmp_path / "squire"
    skill = home / "config" / "shared" / "skills" / "squire" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(PACKAGED.read_text("utf-8"), "utf-8")
    assert SquireSetup._stale_skill_warning(setup, home) is None


def test_no_installed_skill_is_not_a_stale_skill(tmp_path: Path) -> None:
    """"Nothing there" and "there but old" need different answers."""

    from hyprial.daemon.impl.squire.setup import SquireSetup

    setup = SquireSetup.__new__(SquireSetup)
    assert SquireSetup._stale_skill_warning(setup, tmp_path / "nope") is None


def test_the_warning_reaches_the_result_and_not_only_the_function() -> None:
    """⭐ Everything above proves the warning is *computed*, not that it is *seen*.

    `_stale_skill_warning` could keep returning a perfect string forever while
    the one line that folds it into the result is deleted, and all six tests
    above stay green. The warning would be produced by a function nobody reads
    and would never reach a person -- which is the same outcome as not writing
    it at all, minus the evidence.

    So the assembly point is asserted here: the value has to flow into the
    result's `warnings` list. (`_execute` renders that dict on both paths --
    `--json` serialises it, and the human path prints it through Rich -- so
    landing in `warnings` is what "the user is told" means.)
    """

    source = (ROOT / "src" / "hyprial" / "daemon" / "impl" / "squire" / "setup.py").read_text("utf-8")
    assert "stale_skill = self._stale_skill_warning(" in source, (
        "the stale check is no longer called from the setup run"
    )
    folded = [
        line
        for line in source.splitlines()
        if '"warnings"' in line and "stale_skill" in line
    ]
    assert folded, (
        "the stale-skill warning is computed but never folded into the result's "
        "warnings; it would be produced and then dropped, and every unit test "
        "of the function itself would still pass"
    )
