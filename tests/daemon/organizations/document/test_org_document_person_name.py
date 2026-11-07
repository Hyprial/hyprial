"""The org-context publisher is a person name, not a machine identifier.

Covers the 2026-09-15 grammar fix: the production owner is a tailnet login
(``bob.smith@example.com``), and ``meta.publisher`` must accept that shape
while keeping the pre-fix hygiene -- whitespace, ``':'``, ``'/'``, control
characters and over-length names stay rejected, and the legacy bare short
name keeps parsing.  The judgment under test is
:func:`hyprial.org.document.is_person_name`, reached both directly and
through the real :func:`parse_document` path (the mutation check for this
suite reverts ``_PERSON_NAME`` to the pre-``@`` pattern).
"""

from __future__ import annotations

import json

import pytest

from hyprial.daemon.impl.org.document import OrgDocumentError, is_person_name, parse_document


def _source(publisher: str) -> str:
    """One minimal org-context whose only variable is the publisher."""

    return (
        "# Organization\n\n"
        "```yaml\n"
        "meta:\n"
        "  version: 6\n"
        "  issued_at: 2026-09-15\n"
        f"  publisher: {json.dumps(publisher)}\n"
        "lines: []\n"
        "members: []\n"
        "routing: []\n"
        "norms:\n"
        "  autonomy: [任务域内自治]\n"
        "  escalation: 超域必须升级\n"
        "  etiquette: 先引荐再委派\n"
        "residents: []\n"
        "```\n"
    )


@pytest.mark.parametrize(
    "publisher",
    [
        "alicesmith",  # legacy bare short name keeps parsing
        "bob.smith@example.com",  # the production tailnet login (this fix)
        "devusr@github",  # domain without a dot is still a login domain
        "hq_adjutant.dev-2",  # old charset untouched by the change
        "a" * 128,  # the pre-existing cap is not lowered
        ("a" * 116) + "@example.com",  # exactly 128 including the domain
    ],
)
def test_person_name_publisher_parses(publisher: str) -> None:
    document = parse_document(_source(publisher))

    assert document.meta.publisher == publisher


@pytest.mark.parametrize(
    "publisher",
    [
        "bob smith@example.com",  # space anywhere in the name
        "bob.smith@example.com ",  # trailing whitespace
        " bob.smith@example.com",  # leading whitespace
        "bob:smith@example.com",  # ':' would corrupt URI segmenting
        "bob/smith@example.com",  # '/' would corrupt path rendering
        "bob.smith@example.com\n",  # control character (newline)
        "bob.smith@example.com\x00",  # control character (NUL)
        "a" * 129,  # over the 128-character cap
        ("a" * 117) + "@example.com",  # 129 characters: the cap bounds the whole name
        "@example.com",  # empty local part
        "bob.smith@",  # empty domain
        "a@b@c",  # '@' is a separator, never a free character
        ".bob.smith@example.com",  # leading punctuation was rejected before, still is
        "",
    ],
)
def test_non_names_are_rejected(publisher: str) -> None:
    with pytest.raises(OrgDocumentError, match="meta.publisher must be a person name"):
        parse_document(_source(publisher))


def test_is_person_name_is_the_predicate_for_both_shapes() -> None:
    assert is_person_name("bob.smith@example.com") is True
    assert is_person_name("devusr@github") is True
    assert is_person_name("alicesmith") is True
    # Non-string candidates (None, numbers) are not names; parse_document
    # feeds meta.get("publisher") straight in, so the predicate must own
    # the type check instead of assuming it happened upstream.
    assert is_person_name(None) is False
    assert is_person_name(6) is False
    assert is_person_name("a b") is False
    assert is_person_name("a:b") is False
    assert is_person_name("a/b") is False
    assert is_person_name("a" * 129) is False


def test_at_sign_members_and_targets_still_parse() -> None:
    """Person-name fields other than publisher were never gated by a regex.

    Guards the boundary of this fix: members/routing/residents with ``@``
    parsed before it (the v6 fixture relies on that), and must keep parsing.
    """

    source = (
        "# Organization\n\n"
        "```yaml\n"
        "meta:\n"
        "  version: 6\n"
        "  issued_at: 2026-09-15\n"
        "  publisher: bob.smith@example.com\n"
        "lines:\n"
        "  - {line: hyprial, owner: gregpaws, repos: [harness-bridge]}\n"
        "members:\n"
        "  - {username: bob.smith@example.com, display: 林懿伦}\n"
        "routing:\n"
        "  - {kind: decision, to: bob.smith@example.com}\n"
        "norms:\n"
        "  autonomy: [任务域内自治]\n"
        "  escalation: 超域必须升级\n"
        "  etiquette: 先引荐再委派\n"
        "residents:\n"
        "  - {name: squire, node: node-hq-01, role: squire, serves: bob.smith@example.com}\n"
        "```\n"
    )

    document = parse_document(source)

    assert document.meta.publisher == "bob.smith@example.com"
    assert document.data["members"][0]["username"] == "bob.smith@example.com"
