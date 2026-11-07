"""owner_migration: addresses scenarios."""

from __future__ import annotations
import json
import sqlite3
from pathlib import Path
import pytest
from hyprial.daemon.impl.owner_migration import (
    OwnerMigrationAborted,
    build_plan,
    migrate_owner,
    rewrite_value,
)
from hyprial.daemon.impl.configuration.identity import classify_target_identity
from hyprial.daemon.impl.squire import UserDeliveryTarget
from hyprial.identity import UserProfileStore

from .support import (
    FORMER_SELF,
    NEW,
    OLD,
    _seed_agents,
    _seed_identities,
    _seed_inbox,
    _state,
)


def test_rewrite_is_anchored_never_a_substring_replace() -> None:
    """The unit-level statement of all three negative assertions."""

    # ① a filesystem path is untouched
    assert rewrite_value(f"/Users/{OLD}/x", OLD, NEW) == f"/Users/{OLD}/x"

    # ② a machine segment is untouched — the old owner is its PREFIX
    assert rewrite_value(f"agent:{NEW}:{FORMER_SELF}:a", OLD, NEW) == f"agent:{NEW}:{FORMER_SELF}:a"

    # ③ one value holding both: the address moves, the path does not
    mixed = f'{{"a":"agent:{OLD}:hq:w","cwd":"/Users/{OLD}/c"}}'
    assert rewrite_value(mixed, OLD, NEW) == (
        f'{{"a":"agent:{NEW}:hq:w","cwd":"/Users/{OLD}/c"}}'
    )

    # the bare owner column, and the four address prefixes
    assert rewrite_value(OLD, OLD, NEW) == NEW
    for prefix in ("agent", "adapter", "channel", "route"):
        assert (
            rewrite_value(f"{prefix}:{OLD}:m:n", OLD, NEW) == f"{prefix}:{NEW}:m:n"
        )

    # ⛔ the mutation this whole design exists to prevent
    assert rewrite_value(f"agent:{NEW}:{FORMER_SELF}:a", OLD, NEW) != f"agent:{NEW}:{NEW}mac-studio.example-tailnet.ts.net:a"


def test_rewrite_preserves_legacy_user_uri_output_for_colon_owners() -> None:
    assert (
        rewrite_value("user:old:owner", "old:owner", "new:owner")
        == "user:new:owner"
    )


def test_migration_rewrites_addresses_and_leaves_the_rest_byte_identical(
    tmp_path: Path,
) -> None:
    """The three negative assertions, at the database level."""

    state = _state(tmp_path)
    _seed_agents(state)
    _seed_inbox(state)

    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    agents = sqlite3.connect(state / "agents.sqlite3")
    actor, owner, uri, cwd = agents.execute(
        "SELECT actor, owner, uri, cwd FROM agents"
    ).fetchone()
    payload = agents.execute("SELECT payload FROM lifecycle_resources").fetchone()[0]
    agents.close()

    # rewritten
    assert owner == NEW
    assert uri == f"agent:{NEW}:node-hq-01:squire"
    # ① ⛔ path byte-identical
    assert cwd == f"/Users/{OLD}/Workspace/hyprial"
    # ③ same cell: address moved, path did not
    document = json.loads(payload)
    assert document["actor"] == f"agent:{NEW}:node-hq-01:worker"
    assert document["cwd"] == f"/Users/{OLD}/checkout"

    inbox = sqlite3.connect(state / "inbox.sqlite3")
    sender, recipient = inbox.execute("SELECT sender, recipient FROM inbox").fetchone()
    dlq_owner = inbox.execute("SELECT owner FROM dlq").fetchone()[0]
    inbox.close()

    assert sender == f"agent:{NEW}:node-hq-01:me"
    # ② ⛔ former-machine-name segment byte-identical, in both spellings
    assert recipient == f"agent:{NEW}:{FORMER_SELF}:hq-adjutant"
    assert dlq_owner == FORMER_SELF


def test_migration_is_idempotent(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _seed_agents(state)
    _seed_inbox(state)

    first = migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)
    assert first > 0

    after_first = (state / "agents.sqlite3").read_bytes()
    second = migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    assert second == 0
    assert (state / "agents.sqlite3").read_bytes() == after_first


def test_unaccounted_residual_aborts_before_writing_anything(
    tmp_path: Path,
) -> None:
    """⭐ Fail-closed, and the abort happens before the first write.

    A migration that rewrote as it scanned and then stopped would leave the
    state neither old nor new — the one outcome nobody can reason about.  So
    this asserts two things: that the unknown shape raises, and that the
    database is byte-identical afterwards.
    """

    state = _state(tmp_path)
    _seed_agents(state)
    # A spelling that is neither an anchored address nor a known residual.
    db = sqlite3.connect(state / "workflows.sqlite3")
    with db:
        db.execute("CREATE TABLE runs (sender TEXT)")
        db.execute("INSERT INTO runs VALUES (?)", (f"legacy-owner={OLD};v=2",))
    db.close()

    before_agents = (state / "agents.sqlite3").read_bytes()
    before_runs = (state / "workflows.sqlite3").read_bytes()

    with pytest.raises(OwnerMigrationAborted) as aborted:
        migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    # ⛔ nothing was written — not even the databases that classified cleanly
    assert (state / "agents.sqlite3").read_bytes() == before_agents
    assert (state / "workflows.sqlite3").read_bytes() == before_runs

    # the report names where to look
    message = str(aborted.value)
    assert "workflows.sqlite3" in message
    assert "runs.sender" in message
    assert "legacy-owner=" in message
    # and says what NOT to do about it
    assert "Do not widen the rewrite" in message


def test_documents_use_the_same_rule_as_the_databases(tmp_path: Path) -> None:
    """`.v1` and users.json go through the identical anchored rewrite.

    Giving documents their own mechanism would be a second thing to keep
    correct, and the failure mode is the same one: a substring replace here
    would corrupt every `/Users/<old>` path stored in a spec.
    """

    state = _state(tmp_path)
    (state / "desired-state.json.v1").write_text(
        json.dumps(
            {
                "providers": [{"name": "w", "cwd": f"/Users/{OLD}/w"}],
                "pin": f"agent:{OLD}:node-hq-01:w",
            }
        ),
        encoding="utf-8",
    )
    (state / "users.json").write_text(
        json.dumps({"owner": NEW, "ownerKey": OLD, "loginName": OLD}),
        encoding="utf-8",
    )

    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    document = json.loads((state / "desired-state.json.v1").read_text(encoding="utf-8"))
    assert document["pin"] == f"agent:{NEW}:node-hq-01:w"
    assert document["providers"][0]["cwd"] == f"/Users/{OLD}/w"

    # ⭐ users.json keeps ownerKey/loginName: they are HOST-side lookup keys,
    # not the user identity, and #335's `user:<x>` resolution still reads them.
    users = json.loads((state / "users.json").read_text(encoding="utf-8"))
    assert users == {"owner": NEW, "ownerKey": OLD, "loginName": OLD}


def test_plan_is_read_only(tmp_path: Path) -> None:
    """Classification must not touch the databases it reads."""

    state = _state(tmp_path)
    _seed_agents(state)
    before = (state / "agents.sqlite3").read_bytes()

    plan = build_plan(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    assert plan.changed
    assert (state / "agents.sqlite3").read_bytes() == before


def test_bare_old_owner_outside_a_host_login_key_still_aborts(
    tmp_path: Path,
) -> None:
    """⭐ Proves HOST_LOGIN_JSON_KEYS is doing work, not just widening the gate.

    ``ownerKey``/``loginName`` legitimately hold the host login.  The same bare
    string under any other key is unexplained, and must stop the migration
    rather than be waved through by the exemption next door.
    """

    state = _state(tmp_path)
    (state / "users.json").write_text(
        json.dumps({"ownerKey": OLD, "somethingElse": OLD}),
        encoding="utf-8",
    )
    before = (state / "users.json").read_bytes()

    with pytest.raises(OwnerMigrationAborted) as aborted:
        migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    assert (state / "users.json").read_bytes() == before
    message = str(aborted.value)
    assert "somethingElse" in message
    # ⛔ and it must NOT have reported the legitimate one
    assert "ownerKey" not in message


def test_previous_owner_is_detected_from_the_data(tmp_path: Path) -> None:
    """No stored "previous owner" setting — it is read off ``agents.owner``.

    A setting would be one more thing that can be wrong, and it would have had
    to be written by the very version that did not yet know it would be needed.
    """

    from hyprial.daemon.impl.owner_migration import detect_previous_owner

    state = _state(tmp_path)
    _seed_agents(state)

    assert detect_previous_owner(state, NEW) == OLD
    # After the rewrite there is nothing foreign left, so a second start is a
    # no-op — this is what makes the startup hook idempotent.
    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)
    assert detect_previous_owner(state, NEW) is None


def test_two_foreign_owners_abort_rather_than_pick_one(tmp_path: Path) -> None:
    """⭐ Fail-closed on detection too, not only on the rewrite.

    State written under several identities is not something to guess about:
    picking either one silently rewrites the other's addresses.
    """

    from hyprial.daemon.impl.owner_migration import detect_previous_owner

    state = _state(tmp_path)
    _seed_agents(state)
    db = sqlite3.connect(state / "agents.sqlite3")
    with db:
        db.execute(
            "INSERT INTO agents VALUES (?, ?, ?, ?)",
            ("other", "someone-else", "agent:someone-else:hq:other", "/tmp"),
        )
    db.close()

    with pytest.raises(OwnerMigrationAborted) as aborted:
        detect_previous_owner(state, NEW)
    assert "someone-else" in str(aborted.value)
    assert OLD in str(aborted.value)


def test_startup_hook_runs_once_and_is_a_no_op_afterwards(tmp_path: Path) -> None:
    """The entry point the daemon calls, including its second start."""

    from hyprial.daemon.impl.owner_migration import migrate_owner_if_needed

    state = _state(tmp_path)
    _seed_agents(state)
    _seed_inbox(state)

    first = migrate_owner_if_needed(state_dir=state, hyprial_home=tmp_path, owner=NEW)
    assert first > 0

    settled = (state / "agents.sqlite3").read_bytes()
    second = migrate_owner_if_needed(state_dir=state, hyprial_home=tmp_path, owner=NEW)

    assert second == 0
    assert (state / "agents.sqlite3").read_bytes() == settled

    # and a fresh home with no state at all is simply nothing to do
    empty = tmp_path / "fresh"
    empty.mkdir()
    assert migrate_owner_if_needed(state_dir=empty, hyprial_home=tmp_path, owner=NEW) == 0


def test_owner_key_nested_in_json_is_rewritten(tmp_path: Path) -> None:
    """⭐ Found by the first rehearsal, not by any synthetic fixture.

    ``lifecycle_resources.payload`` stores a serialized agent record, so the
    same bare owner that ``agents.owner`` holds in a column also appears as
    ``"owner": "<old>"`` inside a JSON blob — where neither the anchored
    rewrite nor the whole-cell exact match reaches it.

    ⛔ ``ownerKey``/``loginName`` in the same blob must still survive: that is
    the user-identity vs host-login split, applied inside a document.
    """

    blob = json.dumps(
        {
            "actor": "squire",
            "owner": OLD,
            "ownerKey": OLD,
            "loginName": OLD,
            "cwd": f"/Users/{OLD}/w",
            "nested": [{"owner": OLD}],
        }
    )
    rewritten = json.loads(rewrite_value(blob, OLD, NEW))

    assert rewritten["owner"] == NEW
    assert rewritten["nested"][0]["owner"] == NEW
    assert rewritten["ownerKey"] == OLD
    assert rewritten["loginName"] == OLD
    assert rewritten["cwd"] == f"/Users/{OLD}/w"


def test_archival_columns_are_left_entirely_alone(tmp_path: Path) -> None:
    """Run records are what someone WROTE, not where anything is addressed.

    ``runs.yaml_text`` holds prose quoting the old owner as an observation
    ("today users.json actually contains bobadmin").  Rewriting it would
    falsify a record of what was seen — the same boundary as #351's "peer
    inbox history is not migrated".  Live routing is ``targets.target``, which
    is NOT archival and is rewritten.
    """

    state = _state(tmp_path)
    _seed_agents(state)
    prose = f'summary: users.json today actually holds "{OLD}"'
    address = f"sender: agent:{OLD}:hq:a"
    db = sqlite3.connect(state / "workflows.sqlite3")
    with db:
        db.execute("CREATE TABLE runs (yaml_text TEXT, report_text TEXT)")
        db.execute("INSERT INTO runs VALUES (?, ?)", (prose, f"{prose}\n{address}"))
        db.execute("CREATE TABLE targets (target TEXT)")
        db.execute("INSERT INTO targets VALUES (?)", (f"agent:{OLD}:hq:a",))
    db.close()

    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    db = sqlite3.connect(state / "workflows.sqlite3")
    yaml_text, report_text = db.execute(
        "SELECT yaml_text, report_text FROM runs"
    ).fetchone()
    target = db.execute("SELECT target FROM targets").fetchone()[0]
    db.close()

    # A completed record is byte-identical, including any address it quotes.
    assert yaml_text == prose
    assert report_text == f"{prose}\n{address}"
    # live routing in the same database: rewritten
    assert target == f"agent:{NEW}:hq:a"


def test_r1_production_app_names_are_kept_not_rewritten(tmp_path: Path) -> None:
    state = _state(tmp_path)
    _seed_agents(state)
    _seed_identities(state, (f"lark:{OLD}-hyprial-e2e", f"{OLD}-hyprial-e2"))

    rewritten = migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    assert rewritten >= 1
    db = sqlite3.connect(state / "adapters.sqlite3")
    adapter, display, actor = db.execute("SELECT adapter, display_name, actor FROM identities").fetchone()
    db.close()
    # the external names are byte-identical ...
    assert adapter == f"lark:{OLD}-hyprial-e2e"
    assert display == f"{OLD}-hyprial-e2"
    # ... while the anchored owner segment in the same row was rewritten
    assert actor == f"agent:{NEW}:{FORMER_SELF}:hq-adjutant"


def test_new_hyprial_app_convention_is_also_kept_precisely(tmp_path: Path) -> None:
    """A post-rename external app name is equally external, not owner state."""

    state = _state(tmp_path)
    _seed_agents(state)
    _seed_identities(state, (f"lark:{OLD}-hyprial-e2e", f"{OLD}-hyprial-e2"))

    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    db = sqlite3.connect(state / "adapters.sqlite3")
    adapter, display = db.execute(
        "SELECT adapter, display_name FROM identities"
    ).fetchone()
    db.close()
    assert adapter == f"lark:{OLD}-hyprial-e2e"
    assert display == f"{OLD}-hyprial-e2"


def test_r2_an_owner_prefixed_name_outside_the_conventions_still_aborts(
    tmp_path: Path,
) -> None:
    """Exact forms are allowed, never the broad and unsafe `{old}-` prefix."""
    state = _state(tmp_path)
    _seed_agents(state)
    _seed_identities(state, (f"lark:{OLD}-other", "unrelated"))
    before = (state / "adapters.sqlite3").read_bytes()

    with pytest.raises(OwnerMigrationAborted) as aborted:
        migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    assert (state / "adapters.sqlite3").read_bytes() == before
    assert "identities.adapter" in str(aborted.value)


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (f'"user:{OLD}"', f'"user:{NEW}"'),
        (f"user:{OLD}", f"user:{NEW}"),
        (f"user:{OLD},", f"user:{NEW},"),
        (f"notify user:{OLD} }}", f"notify user:{NEW} }}"),
        (f"user:{OLD}/inbox", f"user:{NEW}/inbox"),
    ],
)
def test_r3_user_addresses_rewrite_at_non_identifier_boundaries(
    given: str, expected: str
) -> None:
    assert rewrite_value(given, OLD, NEW) == expected


@pytest.mark.parametrize(
    "value",
    [
        f"user:{OLD}x",
        f"user:{OLD}-bot",
        f"user:{OLD}_bot",
        f"user:{OLD}.bot",
        f"user:{OLD}@example.com",
    ],
)
def test_r4_user_address_prefixes_are_not_rewritten(value: str) -> None:
    assert rewrite_value(value, OLD, NEW) == value


def test_r4_unaccounted_user_prefix_still_aborts_fail_closed(tmp_path: Path) -> None:
    state = _state(tmp_path)
    db = sqlite3.connect(state / "routines.sqlite3")
    with db:
        db.execute("CREATE TABLE routines (yaml_text TEXT)")
        db.execute("INSERT INTO routines VALUES (?)", (f"to: user:{OLD}-bot",))
    db.close()

    with pytest.raises(OwnerMigrationAborted, match="routines.yaml_text"):
        migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)


def test_r5_user_address_resolves_to_the_same_profile_after_migration(
    tmp_path: Path,
) -> None:
    new_owner = "bob.smith@example.com"
    state = _state(tmp_path)
    store = UserProfileStore(state / "users.json")
    before, _ = store.ensure(
        owner=OLD,
        owner_key="host-login-key",
        login_name="host-login",
        machine="node-hq-01.example-tailnet.ts.net",
        machine_key="machine-key",
    )
    before_target = UserDeliveryTarget.parse(f"user:{OLD}")
    assert store.resolve(before_target.owner) == before

    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=new_owner)

    after_target = UserDeliveryTarget.parse(f"user:{new_owner}")
    assert classify_target_identity(after_target.uri) == "user"
    after = UserProfileStore(state / "users.json").resolve(after_target.owner)
    assert after is not None
    assert after.owner == new_owner
    assert after.owner_key == before.owner_key
    assert after.login_name == before.login_name


def test_r6_historical_reply_excerpt_is_left_entirely_alone(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    db = sqlite3.connect(state / "workflows.sqlite3")
    historical = f"observed `{OLD}`; sender=agent:{OLD}:hq:a"
    with db:
        db.execute("CREATE TABLE targets (reply_excerpt TEXT)")
        db.execute("INSERT INTO targets VALUES (?)", (historical,))
    db.close()

    migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)

    db = sqlite3.connect(state / "workflows.sqlite3")
    actual = db.execute("SELECT reply_excerpt FROM targets").fetchone()[0]
    db.close()
    assert actual == historical


def test_r7_unregistered_historical_looking_column_still_aborts(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    db = sqlite3.connect(state / "workflows.sqlite3")
    with db:
        db.execute("CREATE TABLE targets (unregistered_excerpt TEXT)")
        db.execute("INSERT INTO targets VALUES (?)", (f"observed `{OLD}`",))
    db.close()

    with pytest.raises(OwnerMigrationAborted, match="targets.unregistered_excerpt"):
        migrate_owner(state_dir=state, hyprial_home=tmp_path, old=OLD, new=NEW)


def test_r8_production_nine_cell_shape_builds_a_fully_classified_plan(
    tmp_path: Path,
) -> None:
    state = _state(tmp_path)
    _seed_agents(state)

    adapters = sqlite3.connect(state / "adapters.sqlite3")
    with adapters:
        adapters.execute("CREATE TABLE identities (adapter TEXT, display_name TEXT)")
        adapters.executemany(
            "INSERT INTO identities VALUES (?, ?)",
            [
                *((f"lark:{OLD}-hyprial-e2e", "unrelated") for _ in range(5)),
                *(("unrelated", f"{OLD}-hyprial-e2") for _ in range(2)),
            ],
        )
    adapters.close()

    routines = sqlite3.connect(state / "routines.sqlite3")
    with routines:
        routines.execute("CREATE TABLE routines (yaml_text TEXT)")
        routines.execute(
            "INSERT INTO routines VALUES (?)",
            (f'…escalate_to: "user:{OLD}" }}…',),
        )
    routines.close()

    workflows = sqlite3.connect(state / "workflows.sqlite3")
    history = (
        f"OIDCNAME infra-op DONE PASS / … `{OLD}` 是**一次性手改**…"
    )
    with workflows:
        workflows.execute("CREATE TABLE targets (reply_excerpt TEXT)")
        workflows.execute("INSERT INTO targets VALUES (?)", (history,))
    workflows.close()

    plan = build_plan(
        state_dir=state,
        hyprial_home=tmp_path,
        old=OLD,
        new="bob.smith@example.com",
    )

    assert len(plan.unclassified) == 0
    routine_writes = [write for write in plan.writes if write.source.endswith("routines.sqlite3")]
    assert [write.new_value for write in routine_writes] == [
        '…escalate_to: "user:bob.smith@example.com" }…'
    ]
    assert not any(
        write.source.endswith(("adapters.sqlite3", "workflows.sqlite3"))
        for write in plan.writes
    )
