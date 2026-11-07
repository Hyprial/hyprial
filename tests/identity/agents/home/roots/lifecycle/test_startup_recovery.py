"""agent_home_dirs: startup recovery scenarios."""

from __future__ import annotations
import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
import pytest
from typer.main import get_command
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
from hyprial.daemon.impl.owner_migration import (
    OwnerMigrationCustodyConflict,
    OwnerMigrationCustodyUnreadable,
    _live_custody_counts,
    migrate_owner,
    migrate_owner_if_needed,
)

from .support import (
    _agents_table_facts,
    _isolated_environment as _isolated_environment,
    _legacy_agents_database,
    _nullable_token_database,
    _old_owner_state_with_live_grant,
    _registry,
    _track,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


# --------------------------------------------------------------------------- #
# A1: the startup owner rewrite must not silently carry live custody        #
# --------------------------------------------------------------------------- #


def test_t01_startup_auto_migration_fences_live_custody_until_chosen(
    tmp_path: Path,
) -> None:
    """A previous owner with grants/active homes stops the daemon, loudly.

    The auto path models only the benign alias rewrite; a real account
    switch must not inherit credential authority by silence (strong review
    A1, fix per its third option: fail closed on custody signals).
    """

    registry = _registry(tmp_path, owner="old-owner")
    resolver = SecretResolver(tmp_path / "home", registry)
    registry.create("worker")
    resolver.write_user_provider("deepseek", {"apiKey": "sentinel"})
    registry.grant_secret(
        "worker",
        grant_id="deepseek",
        source=SecretSource.USER_PROVIDER,
        entry_id="deepseek",
        field_name="apiKey",
        environment_names=("DEEPSEEK_API_KEY",),
        revision=1,
    )
    registry.close()
    state = tmp_path / "state"

    with pytest.raises(OwnerMigrationCustodyConflict) as refused:
        migrate_owner_if_needed(
            state_dir=state, hyprial_home=tmp_path / "home", owner="new-owner"
        )
    assert refused.value.grants == 1
    assert refused.value.homes == 1
    assert refused.value.old == "old-owner"
    db = sqlite3.connect(state / "agents.sqlite3")
    try:
        owners = [row[0] for row in db.execute("SELECT owner FROM agents")]
    finally:
        db.close()
    assert owners == ["old-owner"]  # the refusal wrote nothing

    # The explicit path stays available for a *confirmed* alias rewrite, and
    # an alias choice deliberately keeps the grants (same human, new
    # spelling) — that is the operator's loud choice, not an inheritance.
    assert (
        migrate_owner(
            state_dir=state,
            hyprial_home=tmp_path / "home",
            old="old-owner",
            new="new-owner",
        )
        > 0
    )
    reopened = _registry(tmp_path, owner="new-owner")
    assert reopened.secret_grant("worker", "deepseek") is not None
    reopened.close()
    # Idempotent afterwards: no foreign owner remains, so the gate never
    # fires again.
    assert (
        migrate_owner_if_needed(
            state_dir=state, hyprial_home=tmp_path / "home", owner="new-owner"
        )
        == 0
    )


def test_t01_startup_auto_migration_without_custody_still_runs(tmp_path: Path) -> None:
    """Positive control: a pre-P1a database (no homes, no grants) migrates."""

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = _track(
        AgentRegistry(
            tmp_path / "state" / "agents.sqlite3",
            owner="old-owner",
            machine="node",
        )
    )
    registry.create("legacy")
    registry.close()

    assert (
        migrate_owner_if_needed(
            state_dir=tmp_path / "state", hyprial_home=home, owner="new-owner"
        )
        > 0
    )
    reopened = _track(
        AgentRegistry(
            tmp_path / "state" / "agents.sqlite3",
            owner="new-owner",
            machine="node",
        )
    )
    assert reopened.require("legacy").owner == "new-owner"


def test_t01_startup_auto_migration_with_homes_but_no_grants_still_runs(
    tmp_path: Path,
) -> None:
    """Active homes alone do not gate: every session-registered agent has one.

    A home without a grant carries no delivery authority (the resolver needs
    a named grant), while the alias rewrite *must* keep homes (T01: the
    directory key survives an owner-alias rewrite).  Gating on homes would
    brick #352's alias rewrite for every realistic state; this test pins the
    grants-only trigger so widening it is a deliberate act, not a drift.
    """

    registry = _registry(tmp_path, owner="old-owner")
    agent = registry.create("sessioned")
    receipt = registry.home_receipt("sessioned")
    registry.close()

    assert (
        migrate_owner_if_needed(
            state_dir=tmp_path / "state",
            hyprial_home=tmp_path / "home",
            owner="new-owner",
        )
        > 0
    )
    reopened = _registry(tmp_path, owner="new-owner")
    assert reopened.home_receipt("sessioned") == receipt
    assert reopened.require("sessioned").entity_token == agent.entity_token


def test_t01_migration_with_genuinely_absent_custody_tables_still_runs(
    tmp_path: Path,
) -> None:
    """A pre-P1a database (tables verified absent) migrates as before.

    The only tolerated reading failure is a table that ``sqlite_master``
    proves absent.  The legacy fixture predates both custody tables, so the
    counts are honestly zero and the rewrite proceeds.
    """

    state = tmp_path / "state"
    _legacy_agents_database(state)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)

    assert _live_custody_counts(state) == (0, 0)
    assert (
        migrate_owner_if_needed(state_dir=state, hyprial_home=home, owner="new-owner")
        > 0
    )
    db = sqlite3.connect(state / "agents.sqlite3")
    try:
        owners = [row[0] for row in db.execute("SELECT owner FROM agents")]
    finally:
        db.close()
    assert owners == ["new-owner"]


def test_t01_locked_custody_database_refuses_to_migrate_without_writing(
    tmp_path: Path,
) -> None:
    """A locked database is refused, not counted as zero grants.

    ``database is locked`` is an ``OperationalError`` just like ``no such
    table``, but it means the opposite: live custody may exist and simply
    cannot be seen.  The startup hook must fail closed (拒启) instead of
    silently re-opening the gate (hyprial-developer #513 comment 12723 附条件 2).
    """

    state = _old_owner_state_with_live_grant(tmp_path)
    locker = sqlite3.connect(state / "agents.sqlite3")
    locker.execute("PRAGMA locking_mode=EXCLUSIVE")
    locker.execute("BEGIN IMMEDIATE")
    locker.execute("UPDATE agents SET owner = owner")
    locker.commit()
    try:
        with pytest.raises(OwnerMigrationCustodyUnreadable) as refused:
            migrate_owner_if_needed(
                state_dir=state, hyprial_home=tmp_path / "home", owner="new-owner"
            )
        assert refused.value.table == "agents"  # detect_previous_owner, first read
        assert "locked" in str(refused.value)
    finally:
        locker.close()
    db = sqlite3.connect(state / "agents.sqlite3")
    try:
        owners = [row[0] for row in db.execute("SELECT owner FROM agents")]
        grants = db.execute("SELECT count(*) FROM agent_secret_grants").fetchone()[0]
    finally:
        db.close()
    assert owners == ["old-owner"]  # zero writes
    assert grants == 1  # the live custody the lock must not have hidden away


def test_t01_locked_custody_counts_fail_closed(tmp_path: Path) -> None:
    """The counters themselves refuse to return zero under a real lock.

    Direct node for the broad-except mutation (M-R2a): restoring
    ``except OperationalError: grants = 0`` makes this green-when-red.
    """

    state = _old_owner_state_with_live_grant(tmp_path)
    locker = sqlite3.connect(state / "agents.sqlite3")
    locker.execute("PRAGMA locking_mode=EXCLUSIVE")
    locker.execute("BEGIN IMMEDIATE")
    locker.execute("UPDATE agents SET owner = owner")
    locker.commit()
    try:
        with pytest.raises(OwnerMigrationCustodyUnreadable) as refused:
            _live_custody_counts(state)
        assert refused.value.table == "agent_secret_grants"
        assert "locked" in str(refused.value)
    finally:
        locker.close()


def test_t01_unreadable_custody_file_is_not_counted_as_zero(tmp_path: Path) -> None:
    """A file that is not a database at all refuses instead of counting 0."""

    state = tmp_path / "state"
    state.mkdir(parents=True)
    (state / "agents.sqlite3").write_bytes(b"this is not a database\n" * 8)

    with pytest.raises(OwnerMigrationCustodyUnreadable) as counted:
        _live_custody_counts(state)
    assert counted.value.table == "agent_secret_grants"
    with pytest.raises(OwnerMigrationCustodyUnreadable) as startup:
        migrate_owner_if_needed(
            state_dir=state, hyprial_home=tmp_path / "home", owner="new-owner"
        )
    assert startup.value.table == "agents"


def test_t01_malformed_custody_table_refuses_instead_of_counting_zero(
    tmp_path: Path,
) -> None:
    """A present-but-unqueryable table is a refusal, not a silent zero.

    "No such column" is the same ``OperationalError`` shape as a lock: the
    schema exists but cannot answer the custody question, and by the
    round-2 rule only a table *verified absent* from ``sqlite_master`` may
    count as zero (the real historical ``lifecycle_resources`` has always
    had ``resource_key``/``active`` — the malformed shape is corruption, so
    it must fail closed, with nothing written).
    """

    state = tmp_path / "state"
    state.mkdir(parents=True)
    db = sqlite3.connect(state / "agents.sqlite3")
    with db:
        db.execute("CREATE TABLE agents (actor TEXT, owner TEXT)")
        db.execute("INSERT INTO agents VALUES ('squire', 'old-owner')")
        db.execute("CREATE TABLE lifecycle_resources (payload TEXT)")
        db.execute("INSERT INTO lifecycle_resources VALUES ('{}')")
    db.close()

    with pytest.raises(OwnerMigrationCustodyUnreadable) as counted:
        _live_custody_counts(state)
    assert counted.value.table == "lifecycle_resources"
    assert "resource_key" in str(counted.value)
    with pytest.raises(OwnerMigrationCustodyUnreadable):
        migrate_owner_if_needed(
            state_dir=state, hyprial_home=tmp_path / "home", owner="new-owner"
        )
    check = sqlite3.connect(state / "agents.sqlite3")
    try:
        owners = [row[0] for row in check.execute("SELECT owner FROM agents")]
    finally:
        check.close()
    assert owners == ["old-owner"]  # zero writes


def test_t01_custody_refusal_names_only_real_cli_commands() -> None:
    """The refusal's way out must be real (hyprial-developer #513 附条件 3, 12899 ②).

    Every ``hyprial …`` invocation the message names — flags included —
    must exist in the registered CLI tree; the first recourse must be
    the non-destructive login switch-back to the previous spelling;
    where no command exists (revoke / explicit migrate) the message must
    say so (follow-up / #493); the destructive ``agent destroy`` may only
    be named last, after everything lighter; and no instruction to edit
    the state store by hand may remain (12899 ②-3).
    """

    conflict = OwnerMigrationCustodyConflict(
        old="old-owner", new="new-owner", grants=2, homes=1
    )
    text = str(conflict)

    # Non-destructive first: the login switch-back is named, and no
    # hand-edit-the-database instruction survives (comment 12899 ②-3).
    assert "`hyprial login --switch-account`" in text
    assert "directly in state" not in text

    # Honest declaration where no CLI exists: the lighter revoke is named
    # as a follow-up, not offered as a phantom command, and the explicit
    # account-switch choice stays pinned to #493.
    assert "revoke_secret_grant" in text
    assert "no CLI entry point in this build" in text
    assert "follow-up" in text
    assert "#493" in text

    root = get_command(_cli_root_app)
    invocations = [
        span[len("hyprial") :].strip()
        for span in re.findall(r"`(hyprial [^`]+)`", text)
    ]
    assert invocations, "the refusal must name at least one real recourse command"
    for invocation in invocations:
        words = invocation.split()
        options = [word for word in words if word.startswith("-")]
        path = [word for word in words if not word.startswith("-")]
        node = root
        for component in path:
            assert component in node.commands, f"phantom CLI command: {invocation}"
            node = node.commands[component]
        registered = {opt for param in node.params for opt in param.opts}
        for option in options:
            assert option in registered, f"phantom CLI flag: {invocation}"

    # The destructive command is the last recourse named, never the first.
    assert invocations[-1] == "agent destroy"
    assert invocations.index("login --switch-account") < invocations.index(
        "agent destroy"
    )


def test_t01_custody_refusal_states_the_host_login_blind_spot() -> None:
    """The first recourse must declare when it cannot apply (round-3 note).

    hyprial-developer's non-blocking note on head 89063212: the switch-back
    recourse presumes the previous spelling belongs to a loggable account.
    The most common trigger of this gate is exactly the #352 benign
    host-login alias rewrite, where the old spelling has no account to
    switch back to — and the operator should learn that from the refusal,
    not after working through a recourse that cannot apply.
    """

    text = str(
        OwnerMigrationCustodyConflict(
            old="bobadmin", new="alicesmith", grants=1, homes=0
        )
    )
    assert "came from the host login" in text
    assert "no account to switch back to" in text
    assert "no non-destructive CLI path" in text
    assert "#493" in text


def test_t01_custody_refusal_pairs_destroy_with_its_consequences() -> None:
    """Naming a destructive way out obliges its cost (hyprial-developer 12899 ②).

    The refusal names ``hyprial agent destroy`` only as a last resort,
    and the same message — right where the command is named — must state
    what destroying costs.  Removing the consequences clause (M-R3b)
    reds this node alone.
    """

    text = str(OwnerMigrationCustodyConflict(old="o", new="n", grants=1, homes=0))
    assert "agent destroy" in text  # otherwise this contract node is moot
    window = text[text.index("agent destroy") :]
    for fragment in (
        "irreversible",
        "deletes the agent record",
        "its pins and grants",
        "historical messages to it will no longer resolve",
        "recreating the same name does not restore it",
    ):
        assert fragment in window, f"destroy named without consequences: {fragment!r}"


def test_t01_custody_reads_wait_out_a_transient_lock(tmp_path: Path) -> None:
    """A lock released inside the busy-timeout budget is waited out (12900 ①).

    hyprial-developer's correction: the read-only custody connections keep
    sqlite3's default busy timeout, so a transient exclusive writer is
    waited out and only a lock that outlasts that budget reaches
    ``OwnerMigrationCustodyUnreadable`` (pinned by the lock tests
    above).  The migration here must therefore proceed once the lock
    lifts — after actually waiting, not by racing past the holder.
    """

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    registry = _track(
        AgentRegistry(
            tmp_path / "state" / "agents.sqlite3",
            owner="old-owner",
            machine="node",
        )
    )
    registry.create("legacy")
    registry.close()
    database = tmp_path / "state" / "agents.sqlite3"

    hold = 1.25
    locked = threading.Event()

    def hold_then_release() -> None:
        locker = sqlite3.connect(database)
        try:
            locker.execute("PRAGMA locking_mode=EXCLUSIVE")
            locker.execute("BEGIN IMMEDIATE")
            locker.execute("UPDATE agents SET owner = owner")
            locker.commit()
            locked.set()
            time.sleep(hold)
        finally:
            locker.close()

    holder = threading.Thread(target=hold_then_release)
    holder.start()
    assert locked.wait(timeout=10), "holder never acquired the exclusive lock"
    started = time.monotonic()
    try:
        rewrites = migrate_owner_if_needed(
            state_dir=tmp_path / "state", hyprial_home=home, owner="new-owner"
        )
    finally:
        holder.join()
    waited = time.monotonic() - started
    assert rewrites > 0  # waited out the lock, then migrated
    assert waited >= hold * 0.5  # it waited, not sneaked past the holder


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
