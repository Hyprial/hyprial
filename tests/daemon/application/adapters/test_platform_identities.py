"""Platform identity requests retain the existing adapter namespace and collector."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from hyprial.daemon.impl.adapters.lark.credentials import identities
from hyprial.daemon.impl.adapters.lark.state.records import Identity
from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY
from hyprial.kernel import DaemonRequestError
from tests.daemon.adapters.lark.identity.book.support import FakeCollectionGateway
from tests.daemon.harnesses.providers.test_daemon_provider_login import provider_app as provider_app
from tests.support.runtime._owned_applications import owned_applications as owned_applications


@pytest.fixture
def platform_app(provider_app, monkeypatch):
    app = provider_app
    app._lark_client = SimpleNamespace()
    monkeypatch.setattr(app, "_lark_gateway_names", lambda: ("main", "other"))
    return app


def test_identity_list_reads_only_named_namespace_and_names_no_owner(platform_app):
    app = platform_app
    for name in ("main", "other"):
        store = identities.open_identity_store(app.state_dir, name)
        try:
            store.upsert_identity(Identity(
                kind="user", platform_id="ou_one", display_name=name,
                union_id="on_one",
                standing="verified", source="manual", first_seen_ms=100, last_seen_ms=200,
            ))
        finally:
            store.close()
    result = app.handle("adapter.identities.list", {"name": "main"})
    assert result == {"ok": True, "adapter": "main", "count": 1, "identities": [{
        "kind": "user", "platformId": "ou_one", "displayName": "main", "unionId": "on_one",
        "standing": "verified", "source": "manual",
        "firstSeenMs": 100, "lastSeenMs": 200,
    }]}
    assert app.handle("adapter.identities.list", {"name": "main", "kind": "bot"})["count"] == 0


def test_sync_drains_pages_reports_partial_and_preserves_verified_rows(platform_app, monkeypatch):
    app = platform_app
    gateway = FakeCollectionGateway()
    gateway.fail_members_for.add("oc_dev")
    monkeypatch.setattr(identities, "configured_identity_gateway", lambda *args, **kwargs: ("cli_test", gateway))
    store = identities.open_identity_store(app.state_dir, "main")
    try:
        store.upsert_identity(Identity(
            kind="user", platform_id="ou_oc_all_0",
            standing="verified", source="manual", first_seen_ms=1,
        ))
    finally:
        store.close()
    result = app.handle("adapter.identities.sync", {"name": "main"})
    assert result["ok"] is False
    assert result["adapter"] == "main"
    assert result["identitiesRecorded"] == 7
    assert result["chatsComplete"] is True
    assert result["errors"] == ["oc_dev: members-unavailable (RuntimeError)"]
    assert "hush" not in json.dumps(result)
    records = app.handle("adapter.identities.list", {"name": "main"})["identities"]
    bound = next(row for row in records if row["platformId"] == "ou_oc_all_0")
    assert (bound["standing"], bound["firstSeenMs"]) == ("verified", 1)
    assert app.handle("adapter.identities.list", {"name": "other"})["identities"] == []


@pytest.mark.parametrize("method", ["adapter.identities.list", "adapter.identities.sync"])
def test_identity_requests_reject_workers_unknown_names_and_body_identity(platform_app, monkeypatch, method):
    app = platform_app
    monkeypatch.setattr(identities, "open_identity_store", lambda *args: pytest.fail("store accessed before admission"))
    for params, origin, code in (({"name": "main"}, "worker", "CALLER_NOT_AUTHORIZED"),
                                 ({"name": "missing"}, None, "ADAPTER_NOT_FOUND"),
                                 ({"name": "main", "actor": "forged"}, None, "INVALID_ARGUMENT")):
        with pytest.raises(DaemonRequestError) as error:
            app.handle(method, params, _trusted_message_origin=origin)
        assert error.value.code == code


def test_identity_request_specs_have_separate_read_and_write_semantics():
    read = METHOD_REGISTRY.get("adapter.identities.list")
    write = METHOD_REGISTRY.get("adapter.identities.sync")
    assert read is not None and write is not None
    assert read.spec.mutates is False and read.spec.retry == "safe"
    assert write.spec.mutates is True and write.spec.retry == "never"
    assert read.spec.http_exposed and write.spec.http_exposed
    assert read.spec.audience == write.spec.audience == ("local-operator",)


def test_collection_budget_stops_new_requests_without_claiming_complete(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(identities.time, "monotonic", lambda: clock[0])
    gateway = FakeCollectionGateway()
    original = gateway.list_chats_page

    def slow_page(**kwargs):
        clock[0] += 10
        return original(**kwargs)

    gateway.list_chats_page = slow_page
    store = identities.open_identity_store(tmp_path, "main")
    try:
        report = identities.sync_identities(state=store, gateway=gateway, app_id="cli_test", max_duration_seconds=5)
    finally:
        store.close()
    assert report["ok"] is False
    assert report["chatsComplete"] is False
    assert report["identitiesRecorded"] == 2
    assert gateway.chat_page_tokens == [None]
    assert gateway.member_page_tokens == []
    assert any("budget-exhausted" in error for error in report["errors"])


def test_identity_sync_initial_failure_is_redacted_and_never_replayed(platform_app, monkeypatch):
    calls = []

    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise RuntimeError("https://secret.example/?token=hush")

    monkeypatch.setattr(identities, "configured_identity_gateway", fail)
    with pytest.raises(DaemonRequestError) as error:
        platform_app.handle("adapter.identities.sync", {"name": "main"})
    assert error.value.code == "DAEMON_ERROR"
    assert "hush" not in str(error.value)
    assert calls == [{"http_timeout_seconds": 2.0}]


def test_identity_sdk_timeout_can_be_shorter_without_changing_other_callers():
    from hyprial.daemon.impl.adapters.lark.outbound.sdk import LarkSdkGateway, SDK_HTTP_TIMEOUT_SECONDS

    default = LarkSdkGateway.from_credentials("cli_test", "synthetic-only")
    bounded = LarkSdkGateway.from_credentials("cli_test", "synthetic-only", http_timeout_seconds=2.0)
    assert default._client._config.timeout == SDK_HTTP_TIMEOUT_SECONDS
    assert bounded._client._config.timeout == 2.0


def test_member_budget_keeps_collected_page_without_claiming_all_members(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(identities.time, "monotonic", lambda: clock[0])
    gateway = FakeCollectionGateway()
    original = gateway.list_chat_members_page

    def slow_members(*args, **kwargs):
        clock[0] += 10
        return original(*args, **kwargs)

    gateway.list_chat_members_page = slow_members
    store = identities.open_identity_store(tmp_path, "main")
    try:
        report = identities.sync_identities(state=store, gateway=gateway, app_id="cli_test", max_duration_seconds=5)
        assert len(store.identities(kind="user")) == 3
    finally:
        store.close()
    assert report["ok"] is False and report["chatsComplete"] is True
    assert report["identitiesRecorded"] == 5
    assert report["chats"][0]["complete"] is False
    assert len(report["chats"]) == 1
    assert len(gateway.member_page_tokens) == 1


@pytest.mark.parametrize("chat_name,bot_name", [
    ("Group", "App"), (None, "App"), ("Group", None), (None, None),
])
def test_real_http_lists_and_syncs_with_operator_admission(platform_app, monkeypatch, tmp_path, chat_name, bot_name):
    from hyprial.shell.impl.request_http.server import RequestHttpServer
    from tests.shell.request_http.test_agent_reference import _config, _fake_request_once, _post

    gateway = FakeCollectionGateway()
    bot_info = gateway.bot_info
    chat_page = gateway.list_chats_page

    def named_page(**kwargs):
        chats, cursor = chat_page(**kwargs)
        return tuple(replace(chat, name=chat_name) for chat in chats), cursor

    monkeypatch.setattr(gateway, "bot_info", lambda: replace(bot_info(), app_name=bot_name))
    monkeypatch.setattr(gateway, "list_chats_page", named_page)
    monkeypatch.setattr(identities, "configured_identity_gateway", lambda *a, **kw: ("cli_test", gateway))
    calls = []
    discovery = _fake_request_once([])

    def dispatch(method, params, **kwargs):
        if method in {"ping", "daemon.catalog"}:
            return discovery(method, params, **kwargs)
        calls.append(method)
        return platform_app.handle(method, params)

    server = RequestHttpServer(_config(tmp_path), request_once=dispatch)
    server.start()
    try:
        for method in ("adapter.identities.list", "adapter.identities.sync"):
            status, result = _post(server, method, {"name": "main"}, token=None)
            assert status == 401 and result["dispatch"] == "not-dispatched"
            status, result = _post(server, method, {"name": "main"}, origin="https://evil.example")
            assert status == 403 and result["dispatch"] == "not-dispatched"
            status, result = _post(server, method, {"name": "main", "actor": "forged"})
            assert status == 400 and result["dispatch"] == "not-dispatched"
        assert calls == []
        status, result = _post(server, "adapter.identities.list", {"name": "main"})
        assert status == 200 and result["result"]["count"] == 0
        status, result = _post(server, "adapter.identities.sync", {"name": "main"})
        assert status == 200 and result["result"]["identitiesRecorded"] == 12
        assert result["result"]["bot"]["appName"] == bot_name
        assert [chat["name"] for chat in result["result"]["chats"]] == [chat_name, chat_name]
        status, result = _post(server, "adapter.identities.list", {"name": "main"})
        assert status == 200 and result["result"]["count"] == 12
        bot = next(row for row in result["result"]["identities"] if row["kind"] == "bot")
        assert bot["displayName"] == bot_name
        assert calls == ["adapter.identities.list", "adapter.identities.sync", "adapter.identities.list"]
    finally:
        server.shutdown()
