"""owner_migration: home residuals scenarios."""

from __future__ import annotations
import json
import sqlite3
from pathlib import Path
import pytest
from hyprial.daemon.impl.owner_migration import (
    OwnerMigrationAborted,
    build_plan,
    migrate_owner,
)

from .support import (
    _state,
)


# ── residual: the hyprial home's own path prefix (CI task 15458, 2026-09-17) ──


def test_home_path_containing_the_old_owner_as_a_substring_classifies_cleanly(
    tmp_path: Path,
) -> None:
    """The home's own path is a machine-local location, not an owner segment.

    Earned on CI (task 15458): P1a's home provisioning stores the agent-home
    path inside ``lifecycle_resources.payload``, and pytest-xdist's
    ``popen-gw<N>`` worker directories contain ``op`` — which was that
    test's OLD owner spelling.  The value must classify cleanly, the real
    address in the SAME value must still be rewritten, and the path must
    survive byte-for-byte.
    """

    old, new = "op", "boss"
    # The CI shape: the owner spelling occurs inside an unrelated directory
    # segment ("popen") of the home's path.
    home = tmp_path / "nested" / "popen-gw1" / "home"
    home.mkdir(parents=True)
    state = _state(tmp_path)
    payload = json.dumps(
        {
            "path": f"{home}/agents/alice",
            "uri": f"agent:{old}:node:alice",
        }
    )
    db = sqlite3.connect(state / "agents.sqlite3")
    with db:
        db.execute(
            "CREATE TABLE lifecycle_resources "
            "(resource_key TEXT, resource_token TEXT, active INTEGER, payload TEXT)"
        )
        db.execute(
            "INSERT INTO lifecycle_resources VALUES ('agent-home:alice', "
            "'token', 1, ?)",
            (payload,),
        )
    db.close()
    before = (state / "agents.sqlite3").read_bytes()

    plan = build_plan(state_dir=state, hyprial_home=home, old=old, new=new)

    assert plan.unclassified == []
    # The real address in the same value was rewritten…
    (rewrite,) = plan.writes
    rewritten = json.loads(rewrite.new_value)
    assert rewritten["uri"] == f"agent:{new}:node:alice"
    # …and the home path survived byte-for-byte, coincidental substring and all.
    assert rewritten["path"] == f"{home}/agents/alice"
    assert "popen-gw1" in rewritten["path"]
    # The applied migration keeps that split on the real path too.
    applied = migrate_owner(state_dir=state, hyprial_home=home, old=old, new=new)
    assert applied >= 1
    assert (state / "agents.sqlite3").read_bytes() != before


def test_home_root_residual_does_not_forgive_values_outside_the_home(
    tmp_path: Path,
) -> None:
    """Anti-widening: the same coincidental substring, outside the home path,
    still aborts before writing anything."""

    old, new = "op", "boss"
    home = tmp_path / "nested" / "popen-gw1" / "home"
    home.mkdir(parents=True)
    state = _state(tmp_path)
    db = sqlite3.connect(state / "agents.sqlite3")
    with db:
        db.execute("CREATE TABLE stray (value TEXT)")
        # Same "op" substring, but under a DIFFERENT root: the home-root
        # residual must not match it.
        db.execute(
            "INSERT INTO stray VALUES (?)",
            (f"/elsewhere/popen-gw1/work; legacy-owner={old};v=2",),
        )
    db.close()
    before = (state / "agents.sqlite3").read_bytes()

    with pytest.raises(OwnerMigrationAborted) as aborted:
        migrate_owner(state_dir=state, hyprial_home=home, old=old, new=new)
    assert "stray.value" in str(aborted.value)
    assert (state / "agents.sqlite3").read_bytes() == before  # zero writes


def test_home_root_residual_row_is_data_not_a_blindfold(tmp_path: Path) -> None:
    """The constructed row stays a literal string match, never a pattern.

    Braces in a home path must not leak into ``str.format``; the degenerate
    home roots (empty and ``/``) add no row at all, because prefix-matching
    them would forgive every value.
    """

    from hyprial.daemon.impl.owner_migration import _home_root_residual

    braced = _home_root_residual(Path("/srv/{odd}/hyprial"))
    assert braced is not None
    assert braced.matches("/srv/{odd}/hyprial/agents/alice", "op") is True
    assert braced.matches("/srv/{odd}/elsewhere", "op") is False

    assert _home_root_residual(Path("/")) is None
    assert _home_root_residual(Path("")) is None
