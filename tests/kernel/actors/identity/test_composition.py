"""actor_identity: composition scenarios."""

from __future__ import annotations
from tests.support.runtime._owned_applications import owned_applications as owned_applications
import json
from pathlib import Path
import pytest
from hyprial.daemon.impl.configuration.identity import resolve_node_owner
from hyprial.kernel import agent_uri_actor, canonical_agent_uri



def test_canonical_uri_composition() -> None:
    assert (
        canonical_agent_uri("allen", "mac-studio", "kanban-watcher")
        == "agent:allen:mac-studio:kanban-watcher"
    )


@pytest.mark.parametrize(
    ("owner", "machine", "actor"),
    [("", "m", "a"), ("o", " ", "a"), ("o", "m", ""), ("o:x", "m", "a")],
)
def test_canonical_uri_rejects_empty_or_colon_segments(
    owner: str, machine: str, actor: str
) -> None:
    with pytest.raises(ValueError):
        canonical_agent_uri(owner, machine, actor)


def test_agent_uri_accepts_immutable_id_segments() -> None:
    assert canonical_agent_uri("u.abc", "d.def", "a.ghi") == (
        "agent:u.abc:d.def:a.ghi"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("agent:allen:mac:worker", "worker"),
        ("agent:mac", None),
        ("agent:a:b:c:d", None),
        ("agent:a::c", None),
        ("user:allen", None),
        ("worker", None),
    ],
)
def test_agent_uri_actor_extracts_only_four_segment_uris(
    value: str, expected: str | None
) -> None:
    assert agent_uri_actor(value) == expected


def test_owner_env_override_wins_and_colon_fails_loudly() -> None:
    assert resolve_node_owner({"HYPRIAL_OWNER": "allen"}) == "allen"
    with pytest.raises(ValueError):
        resolve_node_owner({"HYPRIAL_OWNER": "no:colons"})


def test_owner_source_order_env_then_settings_then_loud_failure(
    tmp_path: Path,
) -> None:
    """Identity source order: ``HYPRIAL_OWNER`` > ``settings.json`` > raise.

    ⭐ Retires ``test_owner_falls_back_to_the_local_login``, which pinned the
    ``getpass.getuser()`` fallback.  That test was correct for the contract it
    described — the fallback was deliberate — and this step is the one
    authorised to supersede that semantics: the host login is not a user
    identity, and deriving one from it is precisely the defect being removed.
    """

    home = tmp_path / "home"
    home.mkdir()
    settings = home / "settings.json"

    # Cell 1 — the environment wins, even when settings.json disagrees.
    settings.write_text(json.dumps({"owner": "from-settings"}), encoding="utf-8")
    assert resolve_node_owner({"HYPRIAL_OWNER": "from-env"}, hyprial_home=home) == "from-env"

    # Cell 2 — settings.json answers when the variable is unset or blank.
    assert resolve_node_owner({}, hyprial_home=home) == "from-settings"
    assert resolve_node_owner({"HYPRIAL_OWNER": "  "}, hyprial_home=home) == "from-settings"

    # Cell 3 — neither source: fail loudly, naming BOTH places looked at, so
    # the operator does not have to guess which one they forgot.
    settings.write_text(json.dumps({"version": 1}), encoding="utf-8")
    with pytest.raises(ValueError) as missing:
        resolve_node_owner({}, hyprial_home=home)
    assert "HYPRIAL_OWNER" in str(missing.value)
    assert str(settings) in str(missing.value)

    # A home with no settings.json at all is the same cell, not a fallback.
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        resolve_node_owner({}, hyprial_home=empty)


def test_owner_never_falls_back_to_the_host_login() -> None:
    """The removed fallback must not return by any route.

    ⚠️ A behavioural test cannot prove this on its own: a re-introduced
    fallback would simply make the "not set" cell pass again by returning the
    login name, and nothing would go red.  Pin the absence at the source
    instead — the same shape as the guard in
    ``test_lifecycle_process_manager``.  The owner segment is a *user*
    identity; the host login answers a question about the *machine*.

    ⛔ Assert on the parsed imports, not on the file text.  A substring check
    for the module name also matches the docstring above, which explains why
    the fallback was removed — and the only way to green that check would be
    to delete the explanation.  The criterion has to name the import, so that
    prose about the defect stays legal and re-importing it does not.
    """

    import ast

    source = Path("src/hyprial/daemon/impl/configuration/identity.py").read_text(
        encoding="utf-8"
    )
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "getpass" not in imported


def test_malformed_settings_owner_fails_loudly_not_as_unset(
    tmp_path: Path,
) -> None:
    """A typo must not be reported as "identity is not set".

    Collapsing the two would answer a misspelled key with "run hyprial init",
    which is the one instruction that cannot help.
    """

    home = tmp_path / "home"
    home.mkdir()
    settings = home / "settings.json"

    settings.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot parse"):
        resolve_node_owner({}, hyprial_home=home)

    settings.write_text(json.dumps({"owner": ""}), encoding="utf-8")
    with pytest.raises(ValueError, match="non-empty string"):
        resolve_node_owner({}, hyprial_home=home)

    settings.write_text(json.dumps({"owner": "has:colon"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must not contain"):
        resolve_node_owner({}, hyprial_home=home)
