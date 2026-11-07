"""R1 inbound resolution: union_id first, with legacy no-downgrade."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.lark import (
    Identity,
    LarkAdapter,
    LarkInboundMessage,
    LarkStateStore,
)
from hyprial.identity import UserStore


OWNER = "aaron@example.com"


@pytest.fixture(autouse=True)
def _without_process_owner_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


def _identity(
    *,
    open_id: str,
    union_id: str | None,
    owner: str | None,
    standing: str,
    display_name: str | None = None,
) -> Identity:
    return Identity(
        kind="user",
        platform_id=open_id,
        display_name=display_name,
        union_id=union_id,
        hyprial_owner=owner,
        standing=standing,
        source="manual:pre-r1",
    )


def _adapter(tmp_path: Path) -> tuple[LarkAdapter, LarkStateStore, UserStore]:
    home = tmp_path / "home"
    state_dir = home / "state"
    home.mkdir()
    (home / "settings.json").write_text(
        json.dumps(
            {
                "owner": OWNER,
                "identityMode": "casdoor",
                "identityIssuer": "https://auth.hyprial.com",
                "identityClaims": {
                    "properties": {"oauth_Lark_unionId": "on_allen"}
                },
            }
        ),
        encoding="utf-8",
    )
    state = LarkStateStore(
        state_dir / "adapters.sqlite3", adapter="lark:main"
    )
    users = UserStore(state_dir / "users.sqlite3", hyprial_home=home)
    adapter = LarkAdapter(
        state=state,
        users=users,
        lark=SimpleNamespace(),  # type: ignore[arg-type]
        harness=SimpleNamespace(),  # type: ignore[arg-type]
        routes=SimpleNamespace(),  # type: ignore[arg-type]
        channel_actor_id="adapter:lark:main",
        org_context_path=home / "org-context.md",
    )
    return adapter, state, users


def _message(*, open_id: str, union_id: str | None) -> LarkInboundMessage:
    return LarkInboundMessage(
        event_id="evt-r1",
        message_id="om-r1",
        chat_id="oc-dm",
        chat_type="p2p",
        text="hello",
        sender_id=open_id,
        sender_type="user",
        sender_union_id=union_id,
    )


def test_missing_union_id_is_unbound_even_when_open_id_was_verified(
    tmp_path: Path,
) -> None:
    adapter, _state, users = _adapter(tmp_path)
    try:
        member = users.add_user(
            kind="member", owner=OWNER, confirmed_by="operator"
        )
        users.bind_account(
            member.user_key,
            adapter="lark:main",
            open_id="ou_exact",
            union_id="on_allen",
            confirmed_by="operator",
        )
        resolved = adapter._resolved_sender(
            _message(open_id="ou_exact", union_id=None)
        )
        assert resolved["standing"] == "unresolved"
        assert resolved["owner"] is None
    finally:
        adapter.close()


def test_adapter_guard_blocks_legacy_open_id_match_without_union_id(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    users = UserStore(tmp_path / "state" / "users.sqlite3", hyprial_home=home)
    member = users.add_user(
        kind="member", owner=OWNER, confirmed_by="operator"
    )
    users.bind_account(
        member.user_key,
        adapter="lark:main",
        open_id="ou_exact",
        confirmed_by="operator",
        source="override",
    )
    users.close()
    state = LarkStateStore(
        tmp_path / "state" / "adapters.sqlite3", adapter="lark:main"
    )
    state.upsert_identity(
        Identity(
            kind="user",
            platform_id="ou_exact",
            union_id="on_allen",
            hyprial_owner=OWNER,
            standing="verified",
            source="manual:pre-r1",
        )
    )
    adapter = LarkAdapter(
        state=state,
        lark=SimpleNamespace(),  # type: ignore[arg-type]
        harness=SimpleNamespace(),  # type: ignore[arg-type]
        routes=SimpleNamespace(),  # type: ignore[arg-type]
        channel_actor_id="adapter:lark:main",
        org_context_path=tmp_path / "home" / "org-context.md",
    )
    try:
        resolved = adapter._resolved_sender(
            _message(open_id="ou_exact", union_id=None)
        )
        assert resolved["standing"] == "unresolved"
        assert resolved["owner"] is None
        assert "lookupFailed" not in resolved
    finally:
        adapter.close()


def test_fallback_resolver_never_reads_process_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaked_home = tmp_path / "process-home"
    leaked_settings = leaked_home / ".hyprial" / "settings.json"
    leaked_settings.parent.mkdir(parents=True)
    leaked_settings.write_text(json.dumps({"owner": "LEAKED"}), encoding="utf-8")
    monkeypatch.setenv("HOME", str(leaked_home))
    monkeypatch.setenv("HYPRIAL_HOME", str(leaked_home / ".hyprial"))
    opened: list[Path] = []
    original_open = Path.open

    def guarded_open(path: Path, *args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        if path.is_relative_to(leaked_home):
            opened.append(path)
        return original_open(path, *args, **kwargs)

    state = LarkStateStore(
        tmp_path / "adapter-state" / "adapters.sqlite3", adapter="lark:main"
    )
    monkeypatch.setattr(Path, "open", guarded_open)
    adapter = LarkAdapter(
        state=state,
        lark=SimpleNamespace(),  # type: ignore[arg-type]
        harness=SimpleNamespace(),  # type: ignore[arg-type]
        routes=SimpleNamespace(),  # type: ignore[arg-type]
        channel_actor_id="adapter:lark:main",
    )
    try:
        assert adapter._identity_resolver.owner == "unknown"
        assert opened == []
    finally:
        adapter.close()


def test_application_transport_wires_the_shared_identity_resolver() -> None:
    source = (
        Path(__file__).resolve().parents[5]
        / "src/hyprial/daemon/impl/application/wiring/transport.py"
    ).read_text(encoding="utf-8")
    assert "identity_resolver=self._identity_resolver" in source


def test_production_shaped_owner_claim_resolves_casdoor_login(
    tmp_path: Path,
) -> None:
    adapter, state, users = _adapter(tmp_path)
    try:
        member = users.add_user(
            kind="member", owner=OWNER, confirmed_by="operator"
        )
        users.bind_account(
            member.user_key,
            adapter="lark:old-app",
            open_id="ou_old",
            union_id="on_allen",
            confirmed_by="operator",
        )
        state.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_event",
                union_id="on_allen",
                hyprial_owner=OWNER,
                standing="verified",
                source="manual:pre-r1",
            )
        )
        resolved = adapter._resolved_sender(
            _message(open_id="ou_event", union_id="on_allen")
        )
        assert resolved["standing"] == "verified"
        assert resolved["owner"] == OWNER
        assert resolved["source"] == "casdoor-login"
        assert resolved["userKey"] == member.user_key
    finally:
        adapter.close()


def test_verified_legacy_identity_is_not_downgraded(tmp_path: Path) -> None:
    adapter, state, users = _adapter(tmp_path)
    try:
        colleague = users.add_user(
            kind="member",
            owner="colleague@example.com",
            confirmed_by="operator",
        )
        users.bind_account(
            colleague.user_key,
            adapter="lark:old-app",
            open_id="ou_colleague_old",
            union_id="on_colleague",
            confirmed_by="operator",
        )
        state.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_colleague_main",
                union_id="on_colleague",
                hyprial_owner="colleague@example.com",
                standing="verified",
                source="manual:pre-r1",
            )
        )
        resolved = adapter._resolved_sender(
            _message(
                open_id="ou_colleague_main", union_id="on_colleague"
            )
        )
        assert resolved["standing"] == "verified"
        assert resolved["owner"] == "colleague@example.com"
        assert resolved["source"] == "legacy-identities"
        assert resolved["userKey"] == colleague.user_key
    finally:
        adapter.close()


@pytest.mark.parametrize(
    ("creation", "standing", "source"),
    [
        ("legacy-user-bind", "verified", "legacy-user-bind"),
        ("legacy-identities-row", "verified", "legacy-identities"),
        ("account-only-override", "unresolved", None),
    ],
)
def test_legacy_open_id_and_account_override_have_distinct_inbound_authority(
    tmp_path: Path, creation: str, standing: str, source: str | None
) -> None:
    adapter, _state, users = _adapter(tmp_path)
    try:
        member = users.add_user(
            kind="member", owner="colleague@example.com", confirmed_by="operator"
        )
        if creation == "legacy-user-bind":
            users.bind_account(
                member.user_key,
                adapter="lark:main",
                open_id="ou_exact",
                confirmed_by="operator",
            )
        elif creation == "legacy-identities-row":
            _state.upsert_identity(
                _identity(
                    open_id="ou_exact",
                    union_id=None,
                    owner="colleague@example.com",
                    standing="verified",
                    display_name="Colleague",
                )
            )
        else:
            adapter._identity_resolver.set_account_override(
                adapter="main",
                open_id="ou_exact",
                user_key=member.user_key,
                confirmed_by="operator",
            )

        resolved = adapter._resolved_sender(
            _message(open_id="ou_exact", union_id="on_new")
        )

        assert resolved["standing"] == standing
        assert resolved["source"] == source
        assert resolved.get("userKey") == (
            None if creation == "account-only-override" else member.user_key
        )
    finally:
        adapter.close()


def test_verified_legacy_guest_without_owner_stays_verified(tmp_path: Path) -> None:
    adapter, state, _users = _adapter(tmp_path)
    try:
        state.upsert_identity(
            _identity(
                open_id="ou_guest",
                union_id="on_guest",
                owner=None,
                standing="verified",
                display_name="Guest",
            )
        )

        resolved = adapter._resolved_sender(
            _message(open_id="ou_guest", union_id="on_guest")
        )

        assert resolved["standing"] == "verified"
        assert resolved["owner"] is None
        assert resolved["displayName"] == "Guest"
        assert resolved["source"] == "legacy-identities"
    finally:
        adapter.close()


def test_observed_legacy_sender_keeps_display_name(tmp_path: Path) -> None:
    adapter, state, _users = _adapter(tmp_path)
    try:
        state.upsert_identity(
            _identity(
                open_id="ou_observed",
                union_id="on_observed",
                owner=None,
                standing="observed",
                display_name="Observed Person",
            )
        )

        resolved = adapter._resolved_sender(
            _message(open_id="ou_observed", union_id="on_observed")
        )

        assert resolved["standing"] == "observed"
        assert resolved["displayName"] == "Observed Person"
    finally:
        adapter.close()


def test_legacy_identity_without_user_uses_recorded_display_name(
    tmp_path: Path,
) -> None:
    adapter, state, _users = _adapter(tmp_path)
    try:
        state.upsert_identity(
            _identity(
                open_id="ou_bob",
                union_id="on_bob",
                owner="bob@example.com",
                standing="verified",
                display_name="Bob Recorded",
            )
        )

        resolved = adapter._resolved_sender(
            _message(open_id="ou_bob", union_id="on_bob")
        )

        assert resolved["standing"] == "verified"
        assert resolved["displayName"] == "Bob Recorded"
        assert resolved["owner"] == "bob@example.com"
    finally:
        adapter.close()


@pytest.mark.parametrize("exact_kind", ["legacy-user-bind", "legacy-identities"])
def test_legacy_exact_match_conflicting_with_union_is_ambiguous(
    tmp_path: Path, exact_kind: str
) -> None:
    adapter, state, users = _adapter(tmp_path)
    try:
        exact = users.add_user(
            kind="member", owner="exact@example.com", confirmed_by="operator"
        )
        if exact_kind == "legacy-user-bind":
            users.bind_account(
                exact.user_key,
                adapter="lark:main",
                open_id="ou_shared",
                confirmed_by="operator",
            )
        else:
            state.upsert_identity(
                _identity(
                    open_id="ou_shared",
                    union_id=None,
                    owner="exact@example.com",
                    standing="verified",
                )
            )
        union = users.add_user(
            kind="member", owner="union@example.com", confirmed_by="operator"
        )
        users.bind_account(
            union.user_key,
            adapter="lark:other",
            open_id="ou_union",
            union_id="on_union",
            confirmed_by="operator",
        )

        resolved = adapter._resolved_sender(
            _message(open_id="ou_shared", union_id="on_union")
        )

        assert resolved["standing"] == "ambiguous"
        assert resolved["owner"] is None
    finally:
        adapter.close()
