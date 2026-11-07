"""Account-only rows and override.clear scope (§5 at design head cc2c7184).

* An account-only (``outboundOnly``) row lists only override accounts with no
  unionId; an override account that carries a unionId belongs in that union
  row's ``accounts[]``.
* ``override.clear {adapter, openId}`` removes only that account entry, never
  the union-level binding; ``{platform, unionId}`` removes the union binding.
* Symmetry: each clear removes exactly what its own set created, and an
  account clear reports ``unionBindingRemains`` when the union still resolves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.daemon.impl.application import DaemonApplication
from tests.daemon.identity.test_identity_resolver_r1b import USER_KEY, _application


def _observe(app: DaemonApplication, open_id: str, union_id: str) -> None:
    from hyprial.daemon.impl.adapters.lark import LarkStateStore

    state = LarkStateStore(app.state_dir / "adapters.sqlite3", adapter="lark:main")
    try:
        state.observe_identity(
            "user", open_id, union_id=union_id, source="event-sender:oc_dm", now_ms=101
        )
    finally:
        state.close()


def _override_account(app: DaemonApplication, open_id: str) -> dict:
    return app.handle(
        "identity.override.set",
        {"adapter": "main", "openId": open_id, "user": USER_KEY, "confirmedBy": "operator"},
    )


def test_an_account_carrying_a_union_is_never_listed_as_account_only(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _observe(app, "ou_union", "on_y")
    assert _override_account(app, "ou_union")["unionId"] == "on_y"
    assert _override_account(app, "ou_alone")["outboundOnly"] is True

    rows = app.handle("identity.bindings.list", {})["bindings"]

    (account_only,) = [row for row in rows if row.get("outboundOnly")]
    assert [account["openId"] for account in account_only["accounts"]] == ["ou_alone"]
    union_rows = [row for row in rows if row["unionId"] == "on_y"]
    assert union_rows
    for row in union_rows:
        assert "ou_union" in [account["openId"] for account in row["accounts"]]
        assert "ou_alone" not in [account["openId"] for account in row["accounts"]]


def test_clearing_an_account_keeps_the_union_binding(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _observe(app, "ou_union", "on_y")
    app.handle(
        "identity.override.set",
        {"platform": "lark", "unionId": "on_y", "user": USER_KEY, "confirmedBy": "operator"},
    )
    _override_account(app, "ou_union")

    cleared = app.handle("identity.override.clear", {"adapter": "main", "openId": "ou_union"})

    # The independently set union binding survives, and the clear says so.
    assert cleared == {
        "cleared": True,
        "unionBindingRemains": {"unionId": "on_y", "source": "local-override"},
    }
    row = app.handle("identity.resolve", {"platform": "lark", "unionId": "on_y"})
    assert (row["user"], row["source"]) == (USER_KEY, "local-override")
    overrides = [
        account
        for account in row["accounts"]
        if account["openId"] == "ou_union" and account["source"] == "override"
    ]
    assert overrides == []


def test_clearing_the_union_removes_the_union_binding(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    app.handle(
        "identity.override.set",
        {"platform": "lark", "unionId": "on_z", "user": USER_KEY, "confirmedBy": "operator"},
    )

    cleared = app.handle("identity.override.clear", {"platform": "lark", "unionId": "on_z"})

    assert cleared == {"cleared": True}
    rows = app.handle("identity.bindings.list", {"source": "local-override"})["bindings"]
    assert [row for row in rows if row["unionId"] == "on_z"] == []


def test_an_account_set_then_clear_restores_the_prior_resolution(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _observe(app, "ou_union", "on_y")
    with pytest.raises(Exception) as before:
        app.handle("identity.resolve", {"platform": "lark", "unionId": "on_y"})

    assert _override_account(app, "ou_union")["unionId"] == "on_y"
    row = app.handle("identity.resolve", {"platform": "lark", "unionId": "on_y"})
    assert (row["user"], row["source"]) == (USER_KEY, "local-override")

    cleared = app.handle("identity.override.clear", {"adapter": "main", "openId": "ou_union"})

    assert cleared == {"cleared": True}
    with pytest.raises(Exception) as after:
        app.handle("identity.resolve", {"platform": "lark", "unionId": "on_y"})
    assert getattr(after.value, "code", None) == getattr(before.value, "code", None)
    assert getattr(after.value, "code", None) == "IDENTITY_UNBOUND"


def test_a_union_clear_never_touches_an_account_set(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    app = _application(request, tmp_path)
    _observe(app, "ou_union", "on_y")
    _override_account(app, "ou_union")

    cleared = app.handle("identity.override.clear", {"platform": "lark", "unionId": "on_y"})

    assert cleared == {"cleared": False}
    row = app.handle("identity.resolve", {"platform": "lark", "unionId": "on_y"})
    assert (row["user"], row["source"]) == (USER_KEY, "local-override")


def test_an_account_whose_union_is_learned_later_moves_to_the_union_row(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """Review 1047: account-only set, union observed later, set again."""

    app = _application(request, tmp_path)
    assert _override_account(app, "ou_r")["outboundOnly"] is True
    _observe(app, "ou_r", "on_r")

    assert _override_account(app, "ou_r")["unionId"] == "on_r"

    rows = app.handle("identity.bindings.list", {})["bindings"]
    # The user's only account now has a union: no account-only row at all,
    # not even an empty one.
    assert [row for row in rows if row.get("outboundOnly")] == []
    (union_row,) = [row for row in rows if row["unionId"] == "on_r"]
    assert "ou_r" in [account["openId"] for account in union_row["accounts"]]
    # Only the account-keyed override binds on_r: the account row has no union.
    row = app.handle("identity.resolve", {"platform": "lark", "unionId": "on_r"})
    assert (row["user"], row["source"]) == (USER_KEY, "local-override")


def test_a_remaining_union_bound_to_two_users_is_reported_as_conflict(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    from hyprial.identity import UserStore

    app = _application(request, tmp_path)
    other = app.handle(
        "identity.users.add",
        {"kind": "guest", "displayName": "Other", "confirmedBy": "operator"},
    )["user"]["userKey"]
    app.handle(
        "identity.override.set",
        {"platform": "lark", "unionId": "on_c", "user": USER_KEY, "confirmedBy": "operator"},
    )
    users = UserStore(app.state_dir / "users.sqlite3", hyprial_home=app.hyprial_home)
    try:
        # Legacy data can hold what override.set would refuse today.
        users.bind_account(
            other, adapter="lark:main", open_id="ou_c", union_id="on_c",
            confirmed_by="operator",
        )
    finally:
        users.close()

    cleared = app.handle("identity.override.clear", {"adapter": "main", "openId": "ou_c"})

    assert cleared == {
        "cleared": False,
        "unionBindingRemains": {"unionId": "on_c", "source": "conflict"},
    }


def test_a_later_union_account_leaves_a_sibling_account_only_row(
    request: pytest.FixtureRequest, tmp_path: Path
) -> None:
    """The same sequence when the user also keeps a true account-only account."""

    app = _application(request, tmp_path)
    _override_account(app, "ou_alone")
    _override_account(app, "ou_r")
    _observe(app, "ou_r", "on_r")
    _override_account(app, "ou_r")

    rows = app.handle("identity.bindings.list", {})["bindings"]

    (account_only,) = [row for row in rows if row.get("outboundOnly")]
    assert [account["openId"] for account in account_only["accounts"]] == ["ou_alone"]
