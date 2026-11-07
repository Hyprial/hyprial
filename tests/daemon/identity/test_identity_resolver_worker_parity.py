"""The detached Lark worker and the daemon answer identity the same way.

The worker builds its own fallback ``IdentityResolver`` (lark/worker/entry.py
passes no ``identity_resolver=``), reading the same files as the daemon's.
Two resolvers over the same files can still drift: in how each is built
(home, owner, stores) and in the adapter-only logic in front of the worker's
one.  Every case below is answered by the daemon IPC, by the worker's
resolver, and by the worker's real inbound path, over one fixture.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.lark import (
    Identity,
    LarkAdapter,
    LarkInboundMessage,
    LarkStateStore,
)
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.identity import IdentityResolverError
from hyprial.daemon.impl.identity.bindings import (
    ORG_BINDING_CACHE_FILENAME,
)
from hyprial.identity import LazyUserStore, UserStore
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.shell.impl.daemon_wiring import daemon_dependencies
from tests.support.runtime._owned_applications import close_application


OWNER = "aaron@example.com"
USER_KEY = "aaron-example.com"
UNBOUND = ("error", ipc_errors.IDENTITY_UNBOUND)
CONFLICT = ("error", ipc_errors.IDENTITY_CONFLICT)


@pytest.fixture(autouse=True)
def _without_process_owner_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)


class _Inbox:
    pass


class _Harnesses:
    def status(self) -> tuple[()]:
        return ()


class _Presence:
    def actor_online(self, _actor: str) -> bool:
        return False

    def online_actors(self) -> tuple[()]:
        return ()

    def raw_online_actors(self) -> tuple[()]:
        return ()

    def liveness_keeps(self, _actor: str) -> bool:
        return False

    def online_mailboxes(self) -> tuple[()]:
        return ()


def _application(
    request: pytest.FixtureRequest, home: Path
) -> DaemonApplication:
    state = home / "state"
    home.mkdir(parents=True)
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
    app = DaemonApplication(
        state_dir=state,
        socket_path=state / "daemon.sock",
        node_id="identity-parity-node",
        hyprial_home=home,
        owner=OWNER,
        **daemon_dependencies(),
    )
    request.addfinalizer(lambda: close_application(app))
    app._inbox = _Inbox()  # type: ignore[assignment]
    app._harnesses = _Harnesses()  # type: ignore[assignment]
    app._presence = _Presence()  # type: ignore[assignment]
    return app


def _worker_adapter(
    request: pytest.FixtureRequest, home: Path
) -> LarkAdapter:
    """Built with exactly the identity inputs lark/worker/entry.py passes."""

    state_dir = home / "state"
    state = LarkStateStore(state_dir / "adapters.sqlite3", adapter="lark:main")
    users = LazyUserStore(state_dir / "users.sqlite3")
    request.addfinalizer(state.close)
    request.addfinalizer(users.close)
    return LarkAdapter(
        state=state,
        lark=SimpleNamespace(),  # type: ignore[arg-type]
        harness=SimpleNamespace(),  # type: ignore[arg-type]
        routes=SimpleNamespace(),  # type: ignore[arg-type]
        channel_actor_id="adapter:lark:main",
        org_context_path=home / "org-context.md",
        users=users,
    )


def _users(home: Path) -> UserStore:
    return UserStore(home / "state" / "users.sqlite3", hyprial_home=home)


def _observe(home: Path, open_id: str, union_id: str) -> None:
    state = LarkStateStore(
        home / "state" / "adapters.sqlite3", adapter="lark:main"
    )
    try:
        state.observe_identity(
            "user",
            open_id,
            union_id=union_id,
            source="event-sender:oc_dm",
            now_ms=101,
        )
    finally:
        state.close()


def _owner(app: DaemonApplication, home: Path) -> None:
    users = _users(home)
    try:
        assert (
            users.add_user(
                kind="member", owner=OWNER, confirmed_by="operator"
            ).user_key
            == USER_KEY
        )
    finally:
        users.close()
    _observe(home, "ou_allen", "on_allen")


def _guest(home: Path) -> str:
    users = _users(home)
    try:
        return users.add_user(
            kind="guest", nickname="Guest", confirmed_by="operator"
        ).user_key
    finally:
        users.close()


def _case_owner(app: DaemonApplication, home: Path) -> tuple[str, str]:
    _owner(app, home)
    return "ou_allen", "on_allen"


def _case_legacy(app: DaemonApplication, home: Path) -> tuple[str, str]:
    state = LarkStateStore(
        home / "state" / "adapters.sqlite3", adapter="lark:main"
    )
    try:
        state.upsert_identity(
            Identity(
                kind="user",
                platform_id="ou_bob",
                union_id="on_bob",
                hyprial_owner="bob@example.com",
                standing="verified",
                source="manual:pre-r1",
            ),
            now_ms=301,
        )
    finally:
        state.close()
    return "ou_bob", "on_bob"


def _case_union_override(
    app: DaemonApplication, home: Path
) -> tuple[str, str]:
    guest = _guest(home)
    app.handle(
        "identity.override.set",
        {
            "platform": "lark",
            "unionId": "on_carol",
            "user": guest,
            "confirmedBy": "operator",
        },
    )
    _observe(home, "ou_carol", "on_carol")
    return "ou_carol", "on_carol"


def _case_account_only_override(
    app: DaemonApplication, home: Path
) -> tuple[str, str]:
    # The override lands before any inbound event (no unionId known), then
    # that account is observed carrying the owner's unionId: two users.
    _owner(app, home)
    guest = _guest(home)
    row = app.handle(
        "identity.override.set",
        {
            "adapter": "main",
            "openId": "ou_shared",
            "user": guest,
            "confirmedBy": "operator",
        },
    )
    assert row["unionId"] is None
    _observe(home, "ou_shared", "on_allen")
    return "ou_shared", "on_allen"


def _case_cross_source_conflict(
    app: DaemonApplication, home: Path
) -> tuple[str, str]:
    _owner(app, home)
    guest = _guest(home)
    app.handle(
        "identity.override.set",
        {
            "platform": "lark",
            "unionId": "on_allen",
            "user": guest,
            "confirmedBy": "operator",
        },
    )
    return "ou_allen", "on_allen"


def _case_unbound(app: DaemonApplication, home: Path) -> tuple[str, str]:
    _observe(home, "ou_nobody", "on_nobody")
    return "ou_nobody", "on_nobody"


def _case_org_directory(app: DaemonApplication, home: Path) -> tuple[str, str]:
    cache = home / "state" / ORG_BINDING_CACHE_FILENAME
    cache.write_text(
        json.dumps(
            {
                "bindings": [
                    {
                        "user": "bob@example.com",
                        "userKey": "bob-example.com",
                        "unionId": "on_bob_org",
                        "issuedAtMs": 1_781_006_400_000,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    _observe(home, "ou_bob_org", "on_bob_org")
    return "ou_bob_org", "on_bob_org"


Answer = tuple[str, ...]
_GUEST = "guest"

# (fixture, by unionId, by {adapter, openId} and inbound).  A guest's key is
# minted at runtime, so it is compared by kind; parity still compares it whole.
CASES: dict[str, tuple[Callable[..., tuple[str, str]], Answer, Answer]] = {
    "casdoor-login": (
        _case_owner,
        ("bound", USER_KEY, "member", "casdoor-login"),
        ("bound", USER_KEY, "member", "casdoor-login"),
    ),
    "legacy-identities": (
        _case_legacy,
        ("bound", "bob-example.com", "member", "legacy-identities"),
        ("bound", "bob-example.com", "member", "legacy-identities"),
    ),
    "union-override": (
        _case_union_override,
        ("bound", _GUEST, "guest", "local-override"),
        ("bound", _GUEST, "guest", "local-override"),
    ),
    "account-only-override": (
        _case_account_only_override,
        ("bound", USER_KEY, "member", "casdoor-login"),
        CONFLICT,
    ),
    "cross-source-conflict": (
        _case_cross_source_conflict,
        CONFLICT,
        CONFLICT,
    ),
    "unbound": (_case_unbound, UNBOUND, UNBOUND),
    "org-directory": (
        _case_org_directory,
        ("bound", "bob-example.com", "member", "org-directory"),
        ("bound", "bob-example.com", "member", "org-directory"),
    ),
}


def _bound(user: str, kind: str, source: str) -> Answer:
    return ("bound", user, kind, source)


def _daemon(app: DaemonApplication, params: dict[str, str]) -> Answer:
    try:
        row = app.handle("identity.resolve", params)
    except DaemonRequestError as error:
        return ("error", error.code)
    return _bound(row["user"], row["kind"], row["source"])


def _worker_resolver(worker: LarkAdapter, **params: str) -> Answer:
    try:
        row = worker._identity_resolver.resolve(**params)
    except IdentityResolverError as error:
        return ("error", error.code)
    return _bound(row["user"], row["kind"], row["source"])


def _worker_inbound(adapter: LarkAdapter, open_id: str, union_id: str) -> Answer:
    sender = adapter._resolved_sender(
        LarkInboundMessage(
            event_id="evt-parity",
            message_id="om-parity",
            chat_id="oc-dm",
            chat_type="p2p",
            text="hello",
            sender_id=open_id,
            sender_type="user",
            sender_union_id=union_id,
        )
    )
    if sender["standing"] == "verified":
        return _bound(sender["userKey"], sender["userKind"], sender["source"])
    if sender["standing"] == "ambiguous":
        return CONFLICT
    if sender["standing"] == "unresolved" and not sender.get("lookupFailed"):
        return UNBOUND
    return ("other", repr(sender))


def _expected(answer: Answer, actual: Answer) -> Answer:
    if len(answer) == 4 and answer[1] == _GUEST and len(actual) == 4:
        return (answer[0], actual[1], *answer[2:])
    return answer


@pytest.mark.parametrize("case", sorted(CASES))
def test_worker_fallback_and_daemon_resolver_agree(
    request: pytest.FixtureRequest, tmp_path: Path, case: str
) -> None:
    home = tmp_path / "home"
    app = _application(request, home)
    arrange, by_union, by_account = CASES[case]
    open_id, union_id = arrange(app, home)
    adapter = _worker_adapter(request, home)

    daemon_union = _daemon(app, {"platform": "lark", "unionId": union_id})
    daemon_account = _daemon(app, {"adapter": "main", "openId": open_id})
    worker_union = _worker_resolver(
        adapter, platform="lark", union_id=union_id
    )
    worker_account = _worker_resolver(adapter, adapter="main", open_id=open_id)
    inbound = _worker_inbound(adapter, open_id, union_id)

    # Parity first: the two resolvers, and the worker's inbound path.
    assert worker_union == daemon_union
    assert worker_account == daemon_account
    assert inbound == daemon_account
    # Then the value, so two equally wrong answers cannot agree into a pass.
    assert daemon_union == _expected(by_union, daemon_union)
    assert daemon_account == _expected(by_account, daemon_account)


@pytest.mark.parametrize(
    ("environment_owner", "expected"),
    [("environment@example.com", "environment@example.com"), (None, OWNER)],
)
def test_worker_fallback_uses_shared_node_owner_source_order(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_owner: str | None,
    expected: str,
) -> None:
    home = tmp_path / "home"
    _application(request, home)
    if environment_owner is None:
        monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    else:
        monkeypatch.setenv("HYPRIAL_OWNER", environment_owner)

    worker = _worker_adapter(request, home)

    assert worker._identity_resolver.owner == expected
