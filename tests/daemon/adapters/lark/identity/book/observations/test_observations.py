from __future__ import annotations

from pathlib import Path
import pytest
from hyprial.daemon.impl.adapters.lark import Identity

from ..support import (
    _minimal_adapter,
    inbound,
    store_at,
)


def test_observe_is_idempotent_and_merges_platform_facts(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.observe_identity(
        "user", "ou_a", source="event-sender:oc_g", now_ms=1_000
    )
    store.observe_identity(
        "user",
        "ou_a",
        display_name="林某某",
        union_id="on_a",
        source="chat-members:oc_g",
        now_ms=2_000,
    )
    # A later observation without a name must not erase the known name.
    store.observe_identity(
        "user", "ou_a", source="event-sender:oc_g", now_ms=3_000
    )

    rows = store.identities()
    assert len(rows) == 1
    row = rows[0]
    assert row.display_name == "林某某"
    assert row.union_id == "on_a"
    assert row.standing == "observed"
    assert row.source == "event-sender:oc_g"  # observed source may refresh
    assert row.first_seen_ms == 1_000  # never moves
    assert row.last_seen_ms == 3_000


def test_observation_never_downgrades_a_verified_mapping(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.upsert_identity(
        Identity(
            kind="user",
            platform_id="ou_mate",
            display_name="黄佳佳",
            hyprial_owner="mate",
            standing="verified",
            source="manual:allen-confirmed 2026-08-14",
        ),
        now_ms=1_000,
    )

    store.observe_identity(
        "user",
        "ou_mate",
        display_name="黄佳佳(改名)",
        union_id="on_mate",
        source="chat-members:oc_all",
        now_ms=2_000,
    )

    row = store.identity("user", "ou_mate")
    assert row is not None
    # The human confirmation is untouchable by mechanical collection.
    assert row.standing == "verified"
    assert row.hyprial_owner == "mate"
    assert row.source == "manual:allen-confirmed 2026-08-14"
    # Platform facts still merged in.
    assert row.display_name == "黄佳佳(改名)"
    assert row.union_id == "on_mate"
    assert row.last_seen_ms == 2_000


def test_upsert_overwrites_but_preserves_first_seen(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.observe_identity(
        "user", "ou_a", source="chat-members:oc_g", now_ms=1_000
    )

    stored = store.upsert_identity(
        Identity(
            kind="user",
            platform_id="ou_a",
            display_name="林懿伦",
            hyprial_owner="alicesmith",
            standing="verified",
            source="manual:coordination 2026-08-14",
        ),
        now_ms=5_000,
    )

    assert stored.standing == "verified"
    assert stored.hyprial_owner == "alicesmith"
    assert stored.first_seen_ms == 1_000
    assert stored.last_seen_ms == 5_000
    # The operator surface may also revoke a verification.
    revoked = store.upsert_identity(
        Identity(
            kind="user",
            platform_id="ou_a",
            standing="observed",
            source="manual:revoked 2026-08-15",
        ),
        now_ms=6_000,
    )
    assert revoked.standing == "observed"


def test_identity_taxonomies_are_closed(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    with pytest.raises(ValueError, match="kind"):
        store.observe_identity("human", "ou_a", source="test")
    with pytest.raises(ValueError, match="standing"):
        store.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_a",
                standing="probably",
                source="test",
            )
        )
    with pytest.raises(ValueError, match="source"):
        store.observe_identity("user", "ou_a", source="")
    with pytest.raises(ValueError, match="platform_id"):
        store.observe_identity("user", "", source="test")


def test_identities_are_namespaced_per_adapter(tmp_path: Path) -> None:
    """open_ids are per-App namespaces; rows must never leak across."""

    alpha = store_at(tmp_path, "lark:alpha")
    beta = store_at(tmp_path, "lark:beta")
    alpha.observe_identity(
        "user", "ou_shared_looking_id", source="chat-members:oc_g"
    )

    assert beta.identity("user", "ou_shared_looking_id") is None
    assert beta.identities() == ()
    assert alpha.identity("user", "ou_shared_looking_id") is not None


def test_find_by_name_substring_and_exact_id(tmp_path: Path) -> None:
    store = store_at(tmp_path)
    store.observe_identity(
        "user",
        "ou_lin",
        display_name="林懿伦",
        source="chat-members:oc_g",
    )
    store.observe_identity(
        "user",
        "ou_huang",
        display_name="黄佳佳",
        source="chat-members:oc_g",
    )
    store.observe_identity(
        "bot", "ou_bot", display_name="HQ Adjutant", source="bot-info:cli_a"
    )

    assert [row.platform_id for row in store.find_identities(name="佳")] == [
        "ou_huang"
    ]
    assert [
        row.platform_id for row in store.find_identities(platform_id="ou_lin")
    ] == ["ou_lin"]
    both = store.find_identities(name="佳", platform_id="ou_lin")
    assert both == ()
    # LIKE wildcards in the query are literals, not patterns.
    assert store.find_identities(name="%") == ()


def test_inbound_messages_record_their_sender_identity(tmp_path: Path) -> None:
    adapter, state = _minimal_adapter(tmp_path)

    adapter.handle_inbound(inbound("om_1"))

    row = state.identity("user", "ou_sender")
    assert row is not None
    assert row.union_id == "on_sender"
    assert row.standing == "observed"
    assert row.source == "event-sender:oc_group"


def test_app_senders_are_recorded_as_bots_not_users(tmp_path: Path) -> None:
    adapter, state = _minimal_adapter(tmp_path)

    adapter.handle_inbound(
        inbound(
            "om_2",
            sender_id="ou_other_bot",
            sender_type="app",
            sender_union_id=None,
        )
    )

    assert state.identity("bot", "ou_other_bot") is not None
    assert state.identity("user", "ou_other_bot") is None


def test_identity_bookkeeping_failure_never_breaks_inbound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter, state = _minimal_adapter(tmp_path)
    monkeypatch.setattr(
        state,
        "observe_identity",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("db gone")),
    )

    outcome = adapter.handle_inbound(inbound("om_3"))

    # The message still went through routing (ignored: group, no mention).
    assert outcome.status == "ignored"
