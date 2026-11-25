"""Route filtering and deterministic contract digests."""

from __future__ import annotations

import pytest

from hyprial import __version__
from hyprial.daemon import daemon_catalog as local_catalog
from hyprial.shell.impl.request_http.catalog import (
    CatalogProblem,
    build_view,
    canonical_digest,
    declaration_digest,
    http_contract_digest,
    http_contract_document,
)

PING = {
    "version": __version__,
    "nodeId": "h01-node",
    "owner": "h01-owner",
    "epoch": "epoch-1",
    "phase": "serving",
}


def _entries(**states: str) -> list[dict]:
    entries = []
    for entry in local_catalog()["methods"]:
        item = dict(entry)
        item["declared"] = True
        item["state"] = states.get(entry["name"], "disabled" if not entry["httpExposed"] else "available")
        item["callable"] = item["state"] == "available"
        item["reason"] = None if item["state"] != "not-ready" else "SERVICE_NOT_READY"
        entries.append(item)
    return entries


def _catalog(entries: list[dict], **overrides: object) -> dict:
    document = {
        "schemaVersion": 1,
        "catalogVersion": "1",
        "epoch": "epoch-1",
        "packageVersion": __version__,
        "methods": entries,
    }
    document.update(overrides)
    return document


def _view(entries: list[dict] | None = None, *, local: list[dict] | None = None):
    selected = _entries() if entries is None else entries
    return build_view(
        PING,
        _catalog(selected),
        socket_path="/tmp/daemon.sock",
        local_entries=selected if local is None else local,
    )


def test_only_eligible_unary_operator_routes_become_routes() -> None:
    view = _view()
    assert view.routes["adapter.list"].http_binding["identity"] == {"mode": "none", "param": None}
    assert view.routes["message.send.lookup"].http_binding["identity"] == {"mode": "local-owner-user-selector", "param": "actor"}
    assert view.routes["message.history"].http_binding["identity"] == {"mode": "local-owner-user-selector", "param": "actor"}
    assert view.routes["message.query"].http_binding["identity"] == {"mode": "local-owner-user-selector", "param": "actor"}
    assert set(view.routes) == {
        "onboarding.status", "auth.login.begin", "auth.operation.get",
        "auth.operation.cancel", "onboarding.device.ensure", "onboarding.initialize",
        "auth.logout", "auth.relogin",
        "onboarding.firstRun.begin", "onboarding.firstRun.bind",
        "onboarding.firstRun.confirm", "onboarding.firstRun.cancel",
        "onboarding.firstRun.skip",
        "adapter.list",
        "adapter.identities.list",
        "adapter.identities.sync",
        "adapter.register",
        "adapter.authorization.status",
        "adapter.authorization.begin",
        "adapter.authorization.operation",
        "adapter.authorization.cancel",
        "adapter.onboard.begin",
        "adapter.onboard.operation",
        "adapter.onboard.cancel",
        "adapter.routes.list",
        "adapter.routes.candidates",
        "adapter.routes.add",
        "adapter.routes.remove",
        "adapter.status",
        "management.adapter.remove",
        "management.adapter.remove.preview",
        "adapter.start",
        "adapter.stop",
        "adapter.reload",
        "agent.cell.get",
        "agent.cell.update",
        "agent.home-census",
        "agent.list",
        "agent.provider.allow",
        "agent.provider.auth.list",
        "agent.provider.revoke",
        "agent.provider.status",
        "user.provider.login",
        "user.provider.list",
        "user.provider.logout",
        "user.provider.rotate",
        "agent.provider.default",
        "agent.provider.login",
        "agent.provider.logout",
        "agent.secret.list",
        "agent.secret.grant",
        "agent.secret.provider-write",
        "agent.secret.provider-list",
        "agent.secret.provider-configure",
        "agent.secret.provider-test",
        "agent.secret.revoke",
        "agent.create",
        "agent.destroy.preview",
        "agent.destroy",
        "agent.stop",
        "agent.start",
        "agent.rename",
        "routine.list",
        "routine.status",
        "workflow.list",
        "hosts",
        "autoupdate.status",
        "daemon.status",
        "identity.users.list",
        "log.page",
        "upgrade.steps.status",
        "identity.users.add",
        "message.send",
        "message.send.key_lookup",
        "message.send.lookup",
        "message.history",
        "message.query",
        "progress.list",
        "targets",
        "org.create",
        "org.delete",
        "org.execute",
        "org.fetch",
        "org.invite",
        "org.join",
        "org.leave",
        "org.list",
        "org.invites.list",
        "org.network",
        "org.publish",
        "org.remove",
        "org.repair",
        "orgfs.checkout",
        "orgfs.create",
        "orgfs.export",
        "orgfs.history",
        "orgfs.import",
        "orgfs.invite",
        "orgfs.join",
        "orgfs.ls",
        "orgfs.members",
        "orgfs.mkdir",
        "orgfs.move",
        "orgfs.purge",
        "orgfs.purge_plan",
        "orgfs.purge_status",
        "orgfs.read",
        "orgfs.read_at",
        "orgfs.remove",
        "orgfs.remove_member",
        "orgfs.resolve",
        "orgfs.restore",
        "orgfs.serve",
        "orgfs.space_purge",
        "orgfs.spaces",
        "orgfs.stat",
        "orgfs.stat_at",
        "orgfs.status",
        "orgfs.trash",
        "orgfs.unban",
        "orgfs.watch",
        "orgfs.write",
        "top.snapshot",
        # cell-model phase B first slice: HTTP-exposed conversation reads.
        "conversations.history",
        "conversations.list",
        "conversations.create",
        "conversations.get",
        "conversations.members.add",
        "conversations.members.remove",
        "conversations.post",
    }
    assert view.routes["conversations.list"].path == "/daemon/request/conversations.list"
    assert view.routes["conversations.list"].http_binding["identity"] == {
        "mode": "local-owner-user-selector",
        "param": "from",
    }
    assert "daemon.catalog" not in view.routes
    # Internal owner-gated org methods are never routable from the browser.
    for name in ("org.acl.grant", "org.acl.ls", "org.acl.revoke"):
        assert name not in view.routes
    assert "onBehalfOf" not in view.routes["message.send"].http_binding
    assert view.routes["message.send"].http_binding["paramsConstraints"]["required"] == [
        "to",
        "message",
        "summary",
        "idempotencyKey",
    ]


def test_eligible_declaration_without_a_selector_gets_the_no_selector_route() -> None:
    entries = _entries()
    source = next(entry for entry in entries if entry["name"] == "conversations.list")
    entries.append(
        {
            **source,
            "name": "synthetic.echo",
            "owner": "synthetic.probe",
            "httpBinding": {
                "identity": {"mode": "none", "param": None},
                "readiness": None,
            },
        }
    )
    view = _view(entries, local=entries)
    route = view.routes["synthetic.echo"]
    assert route.http_binding["identity"] == {"mode": "none", "param": None}
    assert route.state == "available"


def _synthetic_write(name: str = "synthetic.write", **changes: object) -> dict:
    source = next(entry for entry in _entries() if entry["name"] == "conversations.list")
    return {
        **source,
        "name": name,
        "owner": "synthetic.probe",
        "mutates": True,
        "retry": "never",
        "httpBinding": {
            "identity": {"mode": "local-owner-user-selector", "param": "from"},
            "readiness": None,
        },
        **changes,
    }


def test_synthetic_writes_with_never_retry_are_admitted_generically() -> None:
    entries = _entries()
    for name in ("synthetic.write", "demo.mutate"):
        entries.append(_synthetic_write(name))
    view = _view(entries, local=entries)
    for name in ("synthetic.write", "demo.mutate"):
        route = view.routes[name]
        assert route.mutates is True
        assert route.retry == "never"
        assert route.path == f"/daemon/request/{name}"


def test_write_safe_retry_and_read_never_retry_are_still_excluded() -> None:
    safe_write = _synthetic_write(retry="safe")
    never_read = _synthetic_write("synthetic.read", mutates=False)
    entries = [*_entries(), safe_write, never_read]
    view = _view(entries, local=entries)
    assert "synthetic.write" not in view.routes
    assert view.skipped["synthetic.write"] == "HTTP_CALL_PROFILE_UNSUPPORTED"
    assert "synthetic.read" not in view.routes
    assert view.skipped["synthetic.read"] == "HTTP_CALL_PROFILE_UNSUPPORTED"


def test_not_ready_keeps_the_route_but_is_not_callable() -> None:
    view = _view(_entries(**{"conversations.list": "not-ready"}))
    route = view.routes["conversations.list"]
    assert route.state == "not-ready"
    assert route.callable is False
    assert route.reason == "SERVICE_NOT_READY"


def _mutated(name: str, **changes: object) -> list[dict]:
    entries = _entries()
    for entry in entries:
        if entry["name"] == name:
            entry.update(changes)
    return entries


@pytest.mark.parametrize(
    "changes, reason",
    [
        ({"audience": ["internal"]}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"audience": ["bound-agent"]}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"mode": "cursor-poll"}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"mutates": True}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"retry": "never"}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"confirmation": "preview"}, "HTTP_CALL_PROFILE_UNSUPPORTED"),
        ({"httpBinding": None}, "HTTP_BINDING_MISSING"),
        (
            {"httpBinding": {"identity": {"mode": "bound-agent-session", "param": "from"}}},
            "HTTP_CALL_PROFILE_UNSUPPORTED",
        ),
    ],
)
def test_http_enabled_but_out_of_profile_is_excluded(changes: dict, reason: str) -> None:
    view = _view(_mutated("conversations.list", **changes))
    assert "conversations.list" not in view.routes
    assert view.skipped["conversations.list"] == reason


def test_declaration_mismatch_fails_the_snapshot() -> None:
    entries = _entries()
    for entry in entries:
        if entry["name"] == "conversations.history":
            entry["owner"] = "daemon.other"
    with pytest.raises(CatalogProblem, match="declarations differ"):
        build_view(
            PING,
            _catalog(entries),
            socket_path="/tmp/daemon.sock",
            local_entries=local_catalog()["methods"],
        )


def test_catalog_shape_and_duplicate_names_are_refused() -> None:
    entries = _entries()
    with pytest.raises(CatalogProblem, match="version"):
        build_view(
            PING,
            _catalog(entries, catalogVersion="2"),
            socket_path="/tmp/daemon.sock",
            local_entries=entries,
        )
    with pytest.raises(CatalogProblem, match="duplicate"):
        build_view(
            PING,
            _catalog(entries + [entries[-1]]),
            socket_path="/tmp/daemon.sock",
            local_entries=entries,
        )


def test_http_contract_digest_tracks_contract_and_ignores_state() -> None:
    base = _view().http_contract_digest
    assert base == http_contract_digest(sorted(_view().routes.values(), key=lambda r: r.name))
    assert base == _view(_entries(**{"conversations.list": "not-ready"})).http_contract_digest

    # A changed mode/mutates/audience/path enters the HTTP contract.
    changed = _view(_mutated("conversations.list", mutates=True))
    assert changed.http_contract_digest != base
    # The declaration digest includes the same mutation.
    assert declaration_digest(_entries()) == _view().declaration_digest
    assert changed.declaration_digest != _view().declaration_digest
    # Adding an unexposed method would change declarations only; the local
    # projection is fixed, so this test is covered by the binding unit test.
    document = http_contract_document(sorted(_view().routes.values(), key=lambda r: r.name))
    assert document["methods"][0]["requiredAudience"] == ["local-operator"]
    assert "readiness" not in document["methods"][0]["httpBinding"]


def test_canonical_digest_is_sorted_and_stable() -> None:
    assert canonical_digest({"b": 1, "a": 2}) == canonical_digest({"a": 2, "b": 1})
    assert canonical_digest({"a": 2}) != canonical_digest({"a": 3})
