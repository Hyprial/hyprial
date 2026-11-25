"""O2a: one daemon-owned default resolver; inactive orgs never qualify."""

from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.org.network import ipc
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError

A = "group-a"
B = "group-b"


@pytest.fixture
def context_app(tmp_path, monkeypatch):
    rows = [
        {"org": name, "spaceId": "space-" + name, "state": "active", "role": "owner", "devices": 0}
        for name in (A, B)
    ]
    events = []
    app = SimpleNamespace(hyprial_home=tmp_path, _log=lambda *args, **kw: events.append((args, kw)))
    monkeypatch.setattr(ipc, "_request_service", lambda app: SimpleNamespace(list=lambda: {"orgs": rows, "leftOrgs": []}))
    return app, rows, events


def call(app, method, **params):
    return METHOD_REGISTRY.invoke(app, method, params)


def test_current_explicit_clear_and_ambiguity(context_app):
    app, rows, events = context_app
    assert call(app, "org.use", org=B)["current"] == B
    assert call(app, "org.resolve")["spaceId"] == "space-" + B
    assert call(app, "org.resolve", org=A)["spaceId"] == "space-" + A
    listing = call(app, "org.list")["orgs"]
    assert [(r["kind"], r["current"]) for r in listing] == [("work", False), ("work", True)]
    call(app, "org.use", clear=True)
    with pytest.raises(DaemonRequestError) as error:
        call(app, "org.resolve")
    assert error.value.code == "ORG_REQUIRED"
    assert error.value.data["candidates"] == [A, B]
    assert any("org.current.changed" in args for args, kw in events)


@pytest.mark.parametrize("state", ["left", "deleted", "sync-problem", "partial", "legacy-active", "creating"])
def test_inactive_current_clears_and_reports(context_app, state):
    app, rows, events = context_app
    call(app, "org.use", org=B)
    rows[1]["state"] = state
    result = call(app, "org.resolve")
    assert result["org"] == A
    assert result["notices"]
    assert not any(r["current"] for r in call(app, "org.list")["orgs"])
    with pytest.raises(DaemonRequestError):
        call(app, "org.resolve", org=B)


def test_home_isolation_and_work_org_listing(context_app, tmp_path):
    app, rows, events = context_app
    call(app, "org.use", org=B)
    other = SimpleNamespace(hyprial_home=tmp_path / "other", _log=app._log)
    with pytest.raises(DaemonRequestError) as error:
        call(other, "org.resolve")
    assert error.value.code == "ORG_REQUIRED"
    rows[:] = [{**rows[0], "org": A}]
    result = call(app, "org.resolve")
    assert result["org"] == A
    assert call(app, "org.list")["orgs"][0]["kind"] == "work"


@pytest.mark.parametrize("params", [{"spaceId": "space-group-a", "org": B}, {"org": "../bad"}])
def test_untrusted_selection_refused(context_app, params):
    app, rows, events = context_app
    with pytest.raises(DaemonRequestError) as error:
        call(app, "org.resolve", **params)
    assert error.value.code == "INVALID_ARGUMENT"


@pytest.mark.parametrize("spaceId", ["foreign", "plain-space"])
@pytest.mark.parametrize("org", ["../bad", "no-such-org", B])
def test_foreign_space_with_invalid_explicit_org_refused(context_app, spaceId, org):
    app, rows, events = context_app
    if org == B:
        rows[1]["state"] = "left"
    with pytest.raises(DaemonRequestError) as error:
        call(app, "org.resolve", spaceId=spaceId, org=org)
    assert error.value.code == "INVALID_ARGUMENT"


def test_foreign_space_without_explicit_org_passes_through(context_app):
    app, rows, events = context_app
    rows[1]["state"] = "left"
    result = call(app, "org.resolve", spaceId="plain-space")
    assert result["org"] is None
    assert result["spaceId"] == "plain-space"
    assert result["notices"]
    result = call(app, "org.resolve", spaceId="foreign", org=A)
    assert result["org"] == A
    assert result["spaceId"] == "foreign"
    assert result["notices"]


def test_foreign_space_passes_through_with_observe_notice(context_app):
    app, rows, events = context_app
    result = call(app, "org.resolve", spaceId="foreign")
    assert result["org"] is None
    assert result["spaceId"] == "foreign"
    assert any("observe" in notice.lower() for notice in result["notices"])
    result = call(app, "org.resolve", spaceId="foreign", org=B)
    assert result["org"] == B
    assert result["spaceId"] == "foreign"
    assert result["notices"]
    assert any("org.observe.space_passthrough" in args for args, kw in events)


def test_space_outside_active_orgs_resolves_without_orgs(context_app, monkeypatch):
    app, rows, events = context_app
    monkeypatch.setattr(
        ipc, "_request_service", lambda app: SimpleNamespace(list=lambda: {"orgs": [], "leftOrgs": []})
    )
    result = call(app, "org.resolve", spaceId="plain-space")
    assert result["org"] is None
    assert result["spaceId"] == "plain-space"
    assert result["notices"]


def test_stale_current_notice_survives_required_error(context_app):
    app, rows, events = context_app
    call(app, "org.use", org=B)
    rows.clear()
    with pytest.raises(DaemonRequestError) as error:
        call(app, "org.resolve")
    assert error.value.code == "ORG_REQUIRED"
    assert error.value.data["notices"]


@pytest.mark.parametrize("params", [{}, {"org": A, "clear": True}, {"clear": False}])
def test_use_requires_exactly_one_action(context_app, params):
    with pytest.raises(DaemonRequestError) as error:
        call(context_app[0], "org.use", **params)
    assert error.value.code == "INVALID_ARGUMENT"
