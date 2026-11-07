"""The remediation commands named by identity guidance must actually exist.

⛔ Why this file exists (2026-09-04, real incident).  ``resolve_node_owner``
raised ``"... Run: hyprial init --owner <id>"`` while ``hyprial init`` had no
``--owner`` option: the operator following it got ``NoSuchOption``.  That
sentence is the *only* instruction anyone sees, because the daemon refuses to
start without an owner -- so the audience for a wrong instruction is exactly
the audience with no other way out.

The generalisable defect is not the typo.  It is that a cited command is
checked for being *good advice* and never for *existing*, and prose in an
error branch is never executed by any test that exercises the happy path.
"""

from __future__ import annotations

import re

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common.root import app
from hyprial.daemon.impl.configuration.identity import resolve_node_owner, write_settings_owner

_COMMAND = re.compile(r"hyprial ((?:[a-z][a-z-]*)(?: [a-z][a-z-]*)?) (--[a-z][a-z-]*)")


def _guidance_text() -> str:
    """Both surfaces that name a remediation command, concatenated.

    ⚠️ The docstring is in here deliberately.  The first version of this test
    read only the raised message, while ``resolve_node_owner.__doc__`` claimed
    "every command named here is real" -- so the docstring's own
    ``hyprial squire setup --owner`` went unchecked by the test that promised to
    check it.  That is the same defect as the bug this file exists to prevent,
    one level up: a claim about verification that nothing verifies.
    """

    with pytest.raises(ValueError) as caught:
        resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home="/nonexistent-hyprial-home")
    return f"{caught.value}\n{resolve_node_owner.__doc__ or ''}"


def test_every_command_named_by_identity_guidance_exists() -> None:
    """Every ``hyprial <cmd> --<opt>`` in error text or docstring resolves.

    ⚠️ U5 (2026-09-04) removed the guidance's only option-anchored command
    (``hyprial init --owner <id>`` — the option this file was written after);
    the guidance now names **bare** ``hyprial login`` plus ``HYPRIAL_OWNER``, so
    zero option-anchored citations is the correct steady state, not a
    regression — the load-bearing check is
    ``test_login_named_by_identity_guidance_exists`` below.  This test
    stays for the other direction: if an option-anchored command is ever
    named again (a future ``hyprial login --account x`` etc.), it must exist
    and carry that option — the original NoSuchOption defect.
    """

    cited = _COMMAND.findall(_guidance_text())
    runner = CliRunner()
    for command, option in cited:
        result = runner.invoke(app, [*command.split(), "--help"])
        assert result.exit_code == 0, (
            f"guidance cites 'hyprial {command}', which has no --help: {result.output}"
        )
        assert option in result.output, (
            f"guidance cites 'hyprial {command} {option}', but {option} is not in "
            f"its --help -- following the instruction yields NoSuchOption.\n"
            f"{result.output}"
        )


def test_login_named_by_identity_guidance_exists() -> None:
    """D-U2-5: the failure hint now also names bare ``hyprial login`` — the
    option-anchored regex above cannot see a bare command, so this checks
    the one remediation it would miss: the command must exist (it does —
    login U2 landed it)."""

    text = _guidance_text()
    assert "hyprial login" in text, (
        f"identity guidance must mention hyprial login (D-U2-5): {_guidance_text()!r}"
    )
    result = CliRunner().invoke(app, ["login", "--help"])
    assert result.exit_code == 0, (
        f"guidance cites 'hyprial login', which has no --help: {result.output}"
    )


def test_init_owner_persists_and_is_idempotent(tmp_path) -> None:
    """The option the guidance names writes what the resolver reads back."""

    settings = tmp_path / "settings.json"
    settings.write_text('{"updateTrack": "dev"}\n', encoding="utf-8")
    env = {"HYPRIAL_HOME": str(tmp_path)}

    write_settings_owner("alicesmith", environ=env)
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=tmp_path) == "alicesmith"

    # Idempotent, and it does not eat the keys it was not asked about.
    write_settings_owner("alicesmith", environ=env)
    import json

    record = json.loads(settings.read_text(encoding="utf-8"))
    assert record == {"updateTrack": "dev", "owner": "alicesmith"}


@pytest.mark.parametrize("bad", ["", "   ", "has:colon"])
def test_a_value_the_resolver_would_reject_never_reaches_disk(tmp_path, bad) -> None:
    """Rejected at the command, not one boot later with nothing to blame."""

    settings = tmp_path / "settings.json"
    with pytest.raises(ValueError):
        write_settings_owner(bad, environ={"HYPRIAL_HOME": str(tmp_path)})
    assert not settings.exists()
