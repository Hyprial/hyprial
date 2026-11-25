"""WP8: the shipped skills must teach the send-links surface as implemented.

Two audiences read two files: an operator reads ``hyprial-ops`` and a
dispatcher reads ``hyprial-workflow``.  After the hard cut every
``hyprial message send`` needs a ``--summary``, the inbox only shows a summary, and a
new message is never a reply, so a skill that still teaches the old shape sends
an agent into a wall.  These tests pin the shape, not the prose.
2026-11-20 (S2b): the packaged squire SKILL template test is gone with the
template.
2026-11-21 (T1b): sends no longer pass a link gate, so the skills must not
teach the send-time hold codes or the link refusals that only the gate
produced.
2026-11-22 (T2a): ``hyprial agent link`` and ``hyprial message thread`` are
removed from the CLI and must no longer be taught in the skills.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OPS = ROOT / "src" / "hyprial" / "skills" / "hyprial-ops" / "SKILL.md"
WORKFLOW = ROOT / ".agents" / "skills" / "hyprial-workflow" / "SKILL.md"

RETIRED_GATE_CODES = (
    "LINK_MINTING",
    "LINK_PEER_FULL",
    "LINK_SELF_FULL",
    "LINK_WINDOW_FULL",
    "LINK_RECLAIMED",
    "LINK_UNKNOWN",
)
REFUSAL_CODES = ("MESSAGE_REPLY_UNAVAILABLE",)

# A line may mention 自动回复 only inside one of these denials; an affirmative
# sentence would be the old "the carrier replies for the model" promise.
NEGATION_MARKERS = (
    "不会在轮末替你回复",
    "不会在轮末自动回复",
    "不自动回复",
    "不会自动回复",
    "从不自动回复",
    "不再自动回复",
    "不承诺自动回复",
    "不会替你回复",
    "不替你回复",
    "不代你回复",
)


# Every inline example touching the send-links surface, e.g.
# ``hyprial message send --from <自身> ... --summary "…"``.
SEND_LINK_EXAMPLE = re.compile(r"`(hyprial (?:message (?:send|reply|read|ack)|outbox)\b[^`]*)`")
FLAG = re.compile(r"--[a-z][a-z-]*")


def _example_arguments(example: str) -> list[list[str]]:
    """The concrete CLI invocations one example stands for (expanding ``a|b``)."""

    path: list[str] = []
    for token in example.split()[1:]:
        if token.startswith("--"):
            break
        path.append(token)
    variants: list[list[str]] = [[]]
    for token in path:
        variants = [variant + [part] for variant in variants
                    for part in token.split("|")]
    return variants


def _accepted_flags(arguments: list[str]) -> set[str]:
    from typer.testing import CliRunner

    import hyprial.shell.impl.cli.runtime.entry as cli

    result = CliRunner().invoke(cli.app, [*arguments, "--help"])
    assert result.exit_code == 0, (arguments, result.output)
    return set(FLAG.findall(result.output))


def test_ops_skill_documents_the_send_links_surface() -> None:
    text = OPS.read_text(encoding="utf-8")
    for token in (
        "--summary",
        "hyprial message read",
        "harness_read_body",
        "openRequestsFrom",
        *REFUSAL_CODES,
        "on-request",
    ):
        assert token in text, f"hyprial-ops SKILL.md no longer documents {token!r}"


def test_skills_no_longer_teach_retired_link_or_thread_cli() -> None:
    for path in (OPS, WORKFLOW):
        text = path.read_text(encoding="utf-8")
        assert "hyprial agent link" not in text, path.name
        assert "hyprial message thread" not in text, path.name


def test_skills_no_longer_teach_link_gate_holds() -> None:
    for path in (OPS, WORKFLOW):
        text = path.read_text(encoding="utf-8")
        for code in RETIRED_GATE_CODES:
            assert code not in text, f"{path.name} still teaches the retired gate code {code!r}"


def test_ops_skill_never_promises_an_automatic_reply() -> None:
    text = OPS.read_text(encoding="utf-8")
    for line in text.splitlines():
        if "自动回复" in line:
            assert any(marker in line for marker in NEGATION_MARKERS), (
                "hyprial-ops SKILL.md states an automatic reply affirmatively: "
                f"{line.strip()}"
            )
    assert "不会在轮末替你回复" in text, (
        "hyprial-ops SKILL.md must say the carrier does not reply for the model "
        "at end of turn"
    )


def test_ops_examples_only_use_flags_the_cli_accepts() -> None:
    """Every documented flag exists: a typo like ``outbox list --from`` fails here."""

    examples = [match.group(1) for match in SEND_LINK_EXAMPLE.finditer(
        OPS.read_text(encoding="utf-8"))]
    assert len(examples) >= 6, examples
    for example in examples:
        flags = set(FLAG.findall(example))
        for arguments in _example_arguments(example):
            missing = flags - _accepted_flags(arguments)
            assert not missing, (
                f"{example!r} uses {sorted(missing)}, which {arguments} rejects")


def test_ops_send_and_reply_examples_carry_a_summary() -> None:
    for example in (match.group(1) for match in SEND_LINK_EXAMPLE.finditer(
            OPS.read_text(encoding="utf-8"))):
        words = example.split()
        verbs = set((words[2] if len(words) > 2 and words[1] == "message" else words[1]).split("|"))
        if verbs in ({"send"}, {"reply"}) and "--from" in example:
            assert "--summary" in example, f"send/reply example without --summary: {example!r}"


def test_workflow_skill_says_pac_notices_carry_a_summary() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "summary" in text
    assert "harness_read_body" in text
