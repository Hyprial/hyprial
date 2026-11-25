"""agent_home_dirs: startup recovery scenarios."""

from __future__ import annotations
import logging
import sqlite3
from pathlib import Path
import pytest
from hyprial.identity.impl.agents import (
    Agent,
    AgentExistsError,
    AgentHomeError,
    AgentRegistry,
    SecretGrant,
    SecretResolver,
    SecretResolutionError,
    SecretSource,
)

from .support import (
    _agents_table_facts,
    _isolated_environment as _isolated_environment,
    _legacy_agents_database,
    _nullable_token_database,
    _registry,
    _track,
)


def test_t01_legacy_database_migrates_to_the_new_token_contract(
    tmp_path: Path,
) -> None:
    """The upgrade rebuilds the table: NOT NULL, single-column UNIQUE, no NULL.

    Strong review A3: ``ALTER TABLE ... ADD COLUMN entity_token TEXT`` left
    the column nullable and only (actor, entity_token)-unique, so old and
    rebuilt databases disagreed on the incarnation contract.
    """

    _legacy_agents_database(tmp_path / "state")
    registry = _registry(tmp_path)  # opens and rebuilds
    agent = registry.require("squire")
    assert agent.entity_token
    registry.close()

    columns, indexes = _agents_table_facts(tmp_path / "state" / "agents.sqlite3")
    (tmp_path / "fresh").mkdir()
    fresh = _registry(tmp_path / "fresh")
    fresh.create("control")
    fresh.close()
    fresh_columns, fresh_indexes = _agents_table_facts(
        tmp_path / "fresh" / "state" / "agents.sqlite3"
    )
    assert columns == fresh_columns  # schema parity with a new database
    assert indexes == fresh_indexes  # including a unique single-column index
    assert (("entity_token",), True) in indexes

    db = sqlite3.connect(tmp_path / "state" / "agents.sqlite3")
    try:
        nulls = db.execute(
            "SELECT count(*) FROM agents WHERE entity_token IS NULL"
        ).fetchone()[0]
        assert nulls == 0
        # The mixed-version writer (an older binary whose INSERT predates the
        # column) is rejected at the write instead of storing a NULL.
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO agents (actor, owner, machine, uri, capabilities, "
                "harness_args, created_at_ms) VALUES ('old-bin', 'o', 'm', "
                "'agent:o:m:old-bin', '{}', '{}', 9)"
            )
    finally:
        db.close()


def test_t01_mixed_version_null_token_rows_heal_and_never_deliver(
    tmp_path: Path,
) -> None:
    """The strong-review A3 chain, end to end: NULL agent row + NULL grant row.

    Before the fix the NULL token stringified to ``"None"`` on both sides of
    the resolver's incarnation comparison, the fence was constant-true, and
    the resolver happily delivered the entry.  Now the open heals the agent
    to a fresh incarnation and the stale grant refuses to resolve.
    """

    _nullable_token_database(tmp_path / "state", tokens={"worker": None})
    db = sqlite3.connect(tmp_path / "state" / "agents.sqlite3")
    with db:
        # A grant row the NULL-writing binary could have left behind: its
        # entity_token is NULL too, so no FK ever covered it.
        db.execute(
            "INSERT INTO agent_secret_grants VALUES "
            "('worker', NULL, 'shared', 'user-provider', 'shared', 'key', "
            "'[\"DEEPSEEK_API_KEY\"]', 1)"
        )
        # A hosted agent from the pre-entity-token era: its hosting authority
        # must survive the rebuild byte-for-byte.
        db.execute(
            "INSERT INTO agents (actor, owner, machine, uri, entity_token, "
            "hosted_by, capabilities, harness_args, created_at_ms) VALUES "
            "('hosted', 'old-owner', 'node', 'agent:old-owner:node:hosted', "
            "'stable-token', 'transfer-receive', '{}', '{}', 2)"
        )
    db.close()

    registry = _registry(tmp_path)
    healed = registry.require("worker")
    assert healed.entity_token  # fresh incarnation, not "None"
    assert healed.entity_token != "None"
    hosted = registry.require("hosted")
    assert hosted.entity_token == "stable-token"
    assert hosted.hosted_by == "transfer-receive"
    resolver = SecretResolver(tmp_path / "home", registry)
    resolver.write_user_provider("shared", {"key": "sentinel"})
    with pytest.raises(SecretResolutionError) as stale:
        resolver.resolve("worker", "shared")
    assert stale.value.category == "grant-stale"
    assert "sentinel" not in str(stale.value)


def test_t01_duplicate_entity_tokens_refuse_to_open(tmp_path: Path) -> None:
    """Two agents sharing one token (composite-index era residue) fail loudly."""

    _nullable_token_database(
        tmp_path / "state", tokens={"one": "duplicated", "two": "duplicated"}
    )
    with pytest.raises(RuntimeError, match="incarnation tokens are not globally"):
        _registry(tmp_path)


def test_t01_resolver_fails_closed_on_a_blank_incarnation_token(
    tmp_path: Path,
) -> None:
    """``secrets.resolve`` must not stringify a missing token into a match."""

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    grant = SecretGrant(
        actor="worker",
        # Exactly what ``str(row["entity_token"])`` produced for a NULL row.
        entity_token="None",
        grant_id="shared",
        source=SecretSource.USER_PROVIDER,
        entry_id="shared",
        field_name="key",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )

    class _MixedVersionAgentRow:
        entity_token = None

    class _MixedVersionRegistry:
        def require(self, actor: str) -> object:
            return _MixedVersionAgentRow()

        def secret_grant(self, actor: str, grant_id: str) -> SecretGrant | None:
            return grant

        def home_receipt(self, actor: str, *, require_ready: bool = True):
            raise AssertionError("the fence must refuse before any path work")

    resolver = SecretResolver(home, _MixedVersionRegistry())  # type: ignore[arg-type]
    with pytest.raises(SecretResolutionError) as stale:
        resolver.resolve("worker", "shared")
    assert stale.value.category == "grant-stale"


# --------------------------------------------------------------------------- #
# Review findings C2/C3: typed home errors and observable compensation      #
# --------------------------------------------------------------------------- #


def test_t02_home_receipt_without_provisioner_is_a_typed_error(
    tmp_path: Path,
) -> None:
    """C2: the ``assert`` was control flow; a typed error survives ``-O``."""

    registry = _registry(tmp_path)
    registry.create("worker")
    registry.close()
    reopened = _track(
        AgentRegistry(
            tmp_path / "state" / "agents.sqlite3", owner="owner", machine="node"
        )
    )
    with pytest.raises(AgentHomeError) as unconfigured:
        reopened.home_receipt("worker")
    assert unconfigured.value.category == "not-configured"


def test_t02_failed_compensation_reaches_the_log_surface(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """C3: a compensation that cannot remove the home is logged, not dropped."""

    registry = _registry(tmp_path)
    incumbent = registry.create("incumbent")
    forged = Agent(
        uri=incumbent.uri,
        actor="candidate",
        owner="owner",
        machine="node",
    )
    attempts: list[object] = []
    real_provision = registry._home.provision

    def observing_provision(**kwargs: object):
        attempt = real_provision(**kwargs)  # type: ignore[arg-type]
        attempts.append(attempt)
        return attempt

    monkeypatch.setattr(registry._home, "provision", observing_provision)
    monkeypatch.setattr(
        registry._home, "compensate", lambda attempt: False
    )
    with caplog.at_level(logging.WARNING, logger="hyprial.identity.impl.agents.registry"):
        with pytest.raises(AgentExistsError):
            registry._create_record(forged)

    assert attempts, "the provision actually ran before the SQL failure"
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "compensation left a residue" in message and "candidate" in message
        for message in messages
    ), messages
