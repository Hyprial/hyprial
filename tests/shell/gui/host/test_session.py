"""Session store, cookies and the same-origin rule (ports of the Node host cases)."""

from __future__ import annotations

import asyncio
import itertools
import json

import pytest

from hyprial.shell.impl.gui.host.envelope import (
    HostFailure,
    Request,
    failure_reply,
    new_request_id,
    success_reply,
)
from hyprial.shell.impl.gui.host.session import (
    CONTROL_COOKIE,
    SESSION_COOKIE,
    SessionAuthority,
    SessionMethod,
    SessionStore,
    expire_cookies,
    parse_cookies,
    require_same_origin,
    session_cookies,
)
from tests.shell.gui.host.support import GRANT, FakeClock, host_config


def authority(phase: str = "ready") -> SessionAuthority:
    return SessionAuthority(
        driver="daemon",
        phase=phase,
        catalog_digest="sha256:catalog",
        declaration_digest="sha256:declaration",
        backend_epoch="epoch-1",
        http_contract_version=1,
        catalog_version="1",
        methods=(SessionMethod(name="onboarding.status", mutates=False, callable=True, state="available"),),
    )


def store(clock: FakeClock | None = None, **overrides: object) -> tuple[SessionStore, FakeClock]:
    clock = clock or FakeClock()
    counter = itertools.count(1)
    return (
        SessionStore(host_config(**overrides), clock=clock, tokens=lambda: f"token-{next(counter)}"),
        clock,
    )


def failure_of(call) -> HostFailure:
    with pytest.raises(HostFailure) as caught:
        call()
    return caught.value


def test_a_bootstrap_session_rejects_the_superseded_generation_after_refresh() -> None:
    sessions, _ = store()
    first = sessions.mint(authority("bootstrap"))
    assert first.session.phase == "bootstrap"

    refreshed = sessions.refresh(first.control.id, first.control.view, authority("bootstrap"))
    assert refreshed.session.phase == "bootstrap"
    assert refreshed.session.view == first.session.view

    stale = failure_of(
        lambda: sessions.validate(first.session.id, first.session.view, first.session.generation)
    )
    assert (stale.status, stale.code) == (401, "SESSION_REQUIRED")
    wrong_generation = failure_of(
        lambda: sessions.validate(refreshed.session.id, refreshed.session.view, first.session.generation)
    )
    assert (wrong_generation.status, wrong_generation.code) == (403, "GENERATION_REJECTED")
    assert (
        sessions.validate(refreshed.session.id, refreshed.session.view, refreshed.session.generation)
        is refreshed.session
    )


def test_a_refresh_rotates_generation_and_csrf_but_keeps_the_view() -> None:
    sessions, _ = store()
    first = sessions.mint(authority())
    refreshed = sessions.refresh(first.control.id, first.control.view, authority())
    assert refreshed.session.generation != first.session.generation
    assert refreshed.session.csrf != first.session.csrf
    assert refreshed.session.id != first.session.id
    assert refreshed.session.view == first.session.view


def test_a_revoked_session_is_refused() -> None:
    sessions, _ = store()
    rotated = sessions.mint(authority())
    sessions.revoke(rotated.session.id)
    refused = failure_of(
        lambda: sessions.validate(rotated.session.id, rotated.session.view, rotated.session.generation)
    )
    assert refused.code == "SESSION_REQUIRED"


def test_logout_revocation_can_take_the_control_session_with_it() -> None:
    sessions, _ = store()
    rotated = sessions.mint(authority())
    sessions.revoke(rotated.session.id, include_control=True)
    refused = failure_of(lambda: sessions.require_control(rotated.control.id, rotated.control.view))
    assert refused.code == "SESSION_REQUIRED"


def test_an_expired_session_is_refused_and_pruned() -> None:
    sessions, clock = store(session_ttl_ms=1_000)
    rotated = sessions.mint(authority())
    clock.advance(1_001)
    refused = failure_of(
        lambda: sessions.validate(rotated.session.id, rotated.session.view, rotated.session.generation)
    )
    assert refused.code == "SESSION_REQUIRED"
    assert sessions.active_count == 0


def test_a_business_session_never_outlives_its_control_session() -> None:
    sessions, clock = store(session_ttl_ms=10_000)
    first = sessions.mint(authority())
    clock.advance(6_000)
    refreshed = sessions.refresh(first.control.id, first.control.view, authority())
    assert refreshed.session.expires_at == first.control.expires_at


def test_the_view_proof_is_compared_not_assumed() -> None:
    sessions, _ = store()
    rotated = sessions.mint(authority())
    wrong = failure_of(lambda: sessions.require_control(rotated.control.id, "wrong"))
    assert wrong.code == "SESSION_REQUIRED"
    assert failure_of(lambda: sessions.require_control(None, rotated.control.view)).code == "SESSION_REQUIRED"
    assert failure_of(lambda: sessions.validate(rotated.session.id, None, None)).code == "SESSION_REQUIRED"


def test_capacity_is_enforced_for_mint_and_refresh() -> None:
    sessions, _ = store(max_sessions=1)
    first = sessions.mint(authority())
    full = failure_of(lambda: sessions.mint(authority()))
    assert (full.status, full.code, full.dispatch) == (429, "SESSION_CAPACITY", "not-dispatched")
    refreshed = sessions.refresh(first.control.id, first.control.view, authority())
    assert refreshed.session.id != first.session.id


def test_is_current_follows_the_control_the_generation_and_revocation() -> None:
    sessions, _ = store()
    first = sessions.mint(authority())
    args = (first.control.id, first.control.view, first.session.id, first.session.generation)
    assert sessions.is_current(*args) is True
    assert sessions.is_current(first.control.id, first.control.view, first.session.id, "other") is False
    sessions.invalidate_business_sessions()
    assert sessions.is_current(*args) is False
    assert failure_of(lambda: sessions.is_current(None, None, None, None)).code == "SESSION_REQUIRED"


def test_csrf_comparison_is_exact() -> None:
    sessions, _ = store()
    rotated = sessions.mint(authority())
    assert sessions.csrf_matches(rotated.session, rotated.session.csrf)
    assert not sessions.csrf_matches(rotated.session, None)
    assert not sessions.csrf_matches(rotated.session, "other")


def test_a_session_pins_the_method_list_it_was_minted_with() -> None:
    sessions, _ = store()
    rotated = sessions.mint(authority())
    assert list(rotated.session.methods) == ["onboarding.status"]
    assert rotated.session.methods["onboarding.status"].mutates is False


def test_the_launch_grant_is_exact_one_use_and_expires() -> None:
    sessions, clock = store(launch_ttl_ms=1_000)
    assert sessions.grant_status("wrong") == "invalid"
    assert sessions.grant_status(GRANT) == "ok"
    assert sessions.consume_grant(GRANT) == "ok"
    assert sessions.consume_grant(GRANT) == "used"
    fresh, clock = store(launch_ttl_ms=1_000)
    clock.advance(1_001)
    assert fresh.grant_status(GRANT) == "expired"


def test_slow_start_grant_expiry_begins_only_when_publication_appears(tmp_path) -> None:
    publication = tmp_path / "launch.ready"
    sessions, clock = store(
        launch_ttl_ms=1_000, publication_path=str(publication), handoff_path=str(tmp_path / "launch.url")
    )
    clock.advance(1_501)
    assert sessions.grant_status(GRANT) == "expired"
    publication.write_text("published\n")
    clock.advance(99)
    assert sessions.grant_status(GRANT) == "ok"
    clock.advance(1_000)
    assert sessions.grant_status(GRANT) == "expired"


def test_activation_reports_the_expiry_only_once_publication_exists(tmp_path) -> None:
    publication = tmp_path / "launch.ready"
    sessions, clock = store(launch_ttl_ms=1_000, publication_path=str(publication))
    assert sessions.activate_launch_grant() is None
    publication.write_text("published\n")
    assert sessions.activate_launch_grant() == clock.now_ms() + 1_000
    clock.advance(500)
    assert sessions.activate_launch_grant() == clock.now_ms() + 500


def test_cookie_headers_are_path_scoped_http_only_and_strict() -> None:
    sessions, clock = store(session_ttl_ms=60_000)
    rotated = sessions.mint(authority())
    control, business = session_cookies(rotated, clock.now_ms())
    for line, name, value in (
        (control, CONTROL_COOKIE, rotated.control.id),
        (business, SESSION_COOKIE, rotated.session.id),
    ):
        assert line == f"{name}={value}; Path=/daemon; Max-Age=60; HttpOnly; SameSite=Strict"
        assert "Secure" not in line
    assert expire_cookies() == (
        f"{CONTROL_COOKIE}=; Path=/daemon; Max-Age=0; HttpOnly; SameSite=Strict",
        f"{SESSION_COOKIE}=; Path=/daemon; Max-Age=0; HttpOnly; SameSite=Strict",
    )


def test_cookie_max_age_is_at_least_one_second() -> None:
    sessions, clock = store(session_ttl_ms=1_000)
    rotated = sessions.mint(authority())
    clock.advance(900)
    assert "Max-Age=1;" in session_cookies(rotated, clock.now_ms())[0]


def test_cookie_parsing_takes_the_first_value_and_ignores_unnamed_or_empty_parts() -> None:
    parsed = parse_cookies("a=1; a=2; =x; b=; c=%41%20; hostile=value; novalue")
    assert parsed == {"a": "1", "c": "A ", "hostile": "value"}
    assert parse_cookies(None) == {}
    assert parse_cookies("") == {}


@pytest.mark.parametrize("header", ["a=%zz", "a=%", "a=%E0%A4%A", "a=%ff"])
def test_an_undecodable_cookie_value_is_a_400(header: str) -> None:
    refused = failure_of(lambda: parse_cookies(header))
    assert (refused.status, refused.code) == (400, "INVALID_REQUEST")


def test_a_bad_duplicate_cookie_value_never_replaces_the_first() -> None:
    assert parse_cookies("a=1; a=%zz") == {"a": "1"}


def origin_check(host: str | None, origin: str | None = None, site: str | None = None, *,
                 port: int = 4173, require: bool = False) -> str:
    return require_same_origin(
        host=host, origin=origin, fetch_site=site, listener_port=port, require_origin=require
    )


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "example.com:4173",
        "localhost:9999",
        "127.0.0.1",
        "127.0.0.1:4173/x",
        "user@127.0.0.1:4173",
        "127.0.0.1:4173?x=1",
        "[::1",
        "0.0.0.0:4173",
        "127.0.0.1:abc",
    ],
)
def test_the_host_header_must_name_this_loopback_listener(host: str | None) -> None:
    refused = failure_of(lambda: origin_check(host))
    assert (refused.status, refused.code, refused.dispatch) == (403, "HTTP_HOST_DENIED", "not-dispatched")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1:4173", "http://127.0.0.1:4173"),
        ("localhost:4173", "http://localhost:4173"),
        ("LOCALHOST:4173", "http://localhost:4173"),
        ("[::1]:4173", "http://[::1]:4173"),
    ],
)
def test_a_loopback_host_yields_its_origin(host: str, expected: str) -> None:
    assert origin_check(host) == expected
    assert origin_check(host, origin=expected) == expected


def test_origin_is_required_only_when_asked_and_must_match_exactly() -> None:
    assert origin_check("127.0.0.1:4173") == "http://127.0.0.1:4173"
    assert failure_of(lambda: origin_check("127.0.0.1:4173", require=True)).code == "ORIGIN_REQUIRED"
    for bad in ("http://hostile.invalid", "http://localhost:4173", "http://127.0.0.1:4173/", "null"):
        refused = failure_of(lambda bad=bad: origin_check("127.0.0.1:4173", origin=bad))
        assert (refused.status, refused.code) == (403, "ORIGIN_REJECTED")


def test_fetch_metadata_may_only_say_same_origin_or_none() -> None:
    assert origin_check("127.0.0.1:4173", site="same-origin")
    assert origin_check("127.0.0.1:4173", site="none")
    for bad in ("cross-site", "same-site", ""):
        refused = failure_of(lambda bad=bad: origin_check("127.0.0.1:4173", site=bad))
        assert refused.code == "ORIGIN_REJECTED"


def scope(headers: list[tuple[str, str]], method: str = "POST", path: str = "/daemon/session") -> dict:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [(k.encode(), v.encode()) for k, v in headers],
        "server": ("127.0.0.1", 4173),
    }


def request_with(chunks: list[bytes], headers: list[tuple[str, str]]) -> Request:
    messages = [
        {"type": "http.request", "body": chunk, "more_body": index < len(chunks) - 1}
        for index, chunk in enumerate(chunks or [b""])
    ]

    async def receive() -> dict:
        return messages.pop(0)

    return Request(scope(headers), receive)


JSON_TYPE = ("content-type", "application/json")


def test_a_repeated_or_empty_header_reads_as_absent() -> None:
    request = request_with([b"{}"], [("x-a", "1"), ("x-a", "2"), ("x-b", ""), ("x-c", "ok"), JSON_TYPE])
    assert request.header("x-a") is None
    assert request.header("x-b") is None
    assert request.header("x-c") == "ok"
    assert request.raw_header("x-a") == "1, 2"
    assert request.raw_header("x-b") == ""
    assert request.raw_header("x-missing") is None


def test_the_request_reads_a_json_object_and_keeps_the_exact_bytes() -> None:
    request = request_with([b'{"a": ', b"1}"], [JSON_TYPE])
    value, raw = asyncio.run(request.json_object(1_024))
    assert value == {"a": 1} and raw == b'{"a": 1}'


@pytest.mark.parametrize(
    ("headers", "chunks", "status", "code"),
    [
        ([("content-type", "text/plain")], [b"{}"], 415, "HTTP_UNSUPPORTED_MEDIA_TYPE"),
        ([], [b"{}"], 415, "HTTP_UNSUPPORTED_MEDIA_TYPE"),
        ([JSON_TYPE], [b"nope"], 400, "INVALID_REQUEST"),
        ([JSON_TYPE], [b"[]"], 400, "INVALID_REQUEST"),
        ([JSON_TYPE], [b'"text"'], 400, "INVALID_REQUEST"),
        ([JSON_TYPE], [b"\xff"], 400, "INVALID_REQUEST"),
        ([JSON_TYPE, ("content-length", "9999")], [b"{}"], 413, "HTTP_REQUEST_TOO_LARGE"),
        ([JSON_TYPE], [b"x" * 600, b"y" * 600], 413, "HTTP_REQUEST_TOO_LARGE"),
    ],
)
def test_framing_failures_are_host_refusals_with_the_method(headers, chunks, status, code) -> None:
    request = request_with(chunks, headers)
    refused = failure_of(lambda: asyncio.run(request.json_object(1_000, "agent.list")))
    assert (refused.status, refused.code, refused.method, refused.dispatch) == (
        status,
        code,
        "agent.list",
        "not-dispatched",
    )


def test_a_declared_oversize_body_is_refused_before_it_is_read() -> None:
    consumed: list[int] = []

    async def receive() -> dict:
        consumed.append(1)
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(scope([JSON_TYPE, ("content-length", "2000")]), receive)
    assert failure_of(lambda: asyncio.run(request.body(1_000))).code == "HTTP_REQUEST_TOO_LARGE"
    assert consumed == []


def test_failure_and_success_envelopes_have_the_published_shape() -> None:
    refused = HostFailure(409, "CATALOG_MISMATCH", "drift", method="agent.list", data={"upstreamCode": "X"})
    reply = failure_reply("gui-1", refused)
    assert reply.status == 409
    assert json.loads(reply.body) == {
        "schemaVersion": 1,
        "ok": False,
        "requestId": "gui-1",
        "code": "CATALOG_MISMATCH",
        "message": "drift",
        "dispatch": "not-dispatched",
        "method": "agent.list",
        "data": {"upstreamCode": "X"},
    }
    headers = dict(reply.headers)
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-hyprial-request-id"] == "gui-1"

    bare = json.loads(failure_reply("gui-2", HostFailure(500, "HOST_INTERNAL_ERROR", "x")).body)
    assert "method" not in bare and "data" not in bare

    ok = success_reply("gui-3", {"revoked": True}, cookies=["a=b"])
    assert json.loads(ok.body) == {"schemaVersion": 1, "ok": True, "requestId": "gui-3", "revoked": True}
    assert ok.cookies == ("a=b",)


def test_host_request_ids_are_prefixed_and_unique() -> None:
    first, second = new_request_id(), new_request_id()
    assert first.startswith("gui-") and first != second


def test_a_failure_copy_changes_only_what_is_named() -> None:
    original = HostFailure(502, "UPSTREAM_UNAVAILABLE", "lost", data={"k": 1})
    changed = original.but(dispatch="unknown", method="agent.create")
    assert (changed.status, changed.code, changed.message, changed.data) == (502, "UPSTREAM_UNAVAILABLE", "lost", {"k": 1})
    assert (changed.dispatch, changed.method) == ("unknown", "agent.create")
    assert original.dispatch == "not-dispatched" and original.method is None
