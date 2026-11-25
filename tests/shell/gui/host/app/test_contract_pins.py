"""Node-host behaviours the session contract must pin (independent verification of PR 2a)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from hyprial.shell.impl.gui.host.config import ConfigError, read_config
from tests.shell.gui.host.support import (
    DIGEST,
    GRANT,
    FakeClock,
    FakeUpstream,
    catalog_payload,
    host_config,
    json_response,
    method_success,
    run_gateway,
)


def test_a_relayed_digest_prefers_the_upstream_header_then_the_body_then_the_pin() -> None:
    def body_digest(request: httpx.Request, body: bytes):
        return json_response(200, {**method_success("agent.list"), "catalogDigest": "body-digest"})

    def header_digest(request: httpx.Request, body: bytes):
        return json_response(
            200,
            {**method_success("agent.list"), "catalogDigest": "body-digest"},
            {"x-hyprial-catalog-digest": "upstream-digest"},
        )

    def neither(request: httpx.Request, body: bytes):
        return json_response(200, method_success("agent.list"))

    async def call(gateway):
        opened = await gateway.open()
        reply = await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=opened.headers())
        return reply.headers["x-hyprial-catalog-digest"]

    assert run_gateway(call, upstream=FakeUpstream(method=header_digest)) == "upstream-digest"
    assert run_gateway(call, upstream=FakeUpstream(method=body_digest)) == "body-digest"
    assert run_gateway(call, upstream=FakeUpstream(method=neither)) == DIGEST


def test_a_relayed_request_id_prefers_the_body_then_the_upstream_header() -> None:
    def both(request: httpx.Request, body: bytes):
        return json_response(200, method_success("agent.list", "body-id"), {"x-hyprial-request-id": "header-id"})

    async def call(gateway):
        opened = await gateway.open()
        return await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=opened.headers())

    assert run_gateway(call, upstream=FakeUpstream(method=both)).headers["x-hyprial-request-id"] == "body-id"

    async def readyz(gateway):
        opened = await gateway.open()
        return await gateway.call("/daemon/readyz", headers=opened.headers())

    assert run_gateway(readyz).status == 200


def test_the_relay_drops_every_other_upstream_header_and_sets_the_content_length() -> None:
    def noisy(request: httpx.Request, body: bytes):
        return json_response(
            503,
            method_success("agent.list"),
            {"set-cookie": "leak=1", "server": "x", "x-extra": "1", "x-hyprial-dispatch": "unknown"},
        )

    async def call(gateway):
        opened = await gateway.open()
        return await gateway.call("/daemon/request/agent.list", method="POST", body={}, headers=opened.headers())

    reply = run_gateway(call, upstream=FakeUpstream(method=noisy))
    assert reply.status == 503
    assert "set-cookie" not in reply.headers and "x-extra" not in reply.headers and "server" not in reply.headers
    assert reply.headers["x-hyprial-dispatch"] == "unknown"
    assert reply.headers["content-length"] == str(len(reply.raw.encode()))


def test_readyz_drops_the_query_while_catalog_and_methods_keep_it() -> None:
    upstream = FakeUpstream()

    async def call(gateway):
        opened = await gateway.open()
        await gateway.call("/daemon/readyz?a=1", headers=opened.headers())
        await gateway.call("/daemon/request?b=2", headers=opened.headers())
        await gateway.call("/daemon/request/agent.list?c=3", method="POST", body={}, headers=opened.headers())

    run_gateway(call, upstream=upstream)
    paths = [r.url.raw_path.decode() for r in upstream.captured[2:]]
    assert paths == ["/readyz", "/daemon/request?b=2", "/daemon/request/agent.list?c=3"]


def test_a_non_json_relayed_readyz_or_catalog_is_a_502() -> None:
    class Garbled(FakeUpstream):
        calls = 0

        def handle(self, request: httpx.Request) -> httpx.Response:
            type(self).calls += 1
            if type(self).calls > 2:
                return httpx.Response(200, content=b"<html>")
            return super().handle(request)

    async def call(gateway):
        opened = await gateway.open()
        ready = await gateway.call("/daemon/readyz", headers=opened.headers())
        catalog = await gateway.call("/daemon/request", headers=opened.headers())
        return ready, catalog

    ready, catalog = run_gateway(call, upstream=Garbled())
    assert (ready.status, ready.json["code"]) == (502, "UPSTREAM_INVALID_RESPONSE")
    assert (catalog.status, catalog.json["code"]) == (502, "UPSTREAM_INVALID_RESPONSE")


def test_a_non_json_probe_refusal_is_invalid_not_auth_rejected() -> None:
    async def call(gateway):
        return await gateway.open()

    opened = run_gateway(call, upstream=httpx.MockTransport(lambda r: httpx.Response(401, content=b"denied")))
    assert (opened.reply.status, opened.reply.json["code"]) == (502, "UPSTREAM_INVALID_RESPONSE")


def test_the_refreshed_session_never_outlives_its_control_and_cookies_carry_the_remainder() -> None:
    clock = FakeClock()

    async def call(gateway):
        opened = await gateway.open()
        control = next(p for p in opened.cookies.split("; ") if p.startswith("hyprial_gui_control="))
        clock.advance(1_200)
        refreshed = await gateway.call(
            "/daemon/session",
            method="POST",
            body={},
            headers={"cookie": control, "x-hyprial-view": str(opened.session["view"])},
        )
        return opened, refreshed

    opened, refreshed = run_gateway(call, config=host_config(session_ttl_ms=2_000), clock=clock)
    assert refreshed.status == 200
    assert refreshed.json["expiresAt"] == opened.session["expiresAt"]
    lines = refreshed.headers.get_list("set-cookie")
    assert all("Max-Age=1;" in line for line in lines)


def test_a_grant_used_before_the_publication_marker_exists_is_expired(tmp_path: Path) -> None:
    marker = tmp_path / "launch.ready"

    async def call(gateway):
        early = await gateway.call("/daemon/session", method="POST", body={"grant": GRANT})
        marker.write_text("published\n")
        late = await gateway.call("/daemon/session", method="POST", body={"grant": GRANT})
        return early, late

    early, late = run_gateway(
        call, config=host_config(publication_path=str(marker), launch_ttl_ms=60_000), clock=FakeClock()
    )
    assert (early.status, early.json["code"]) == (401, "LAUNCH_GRANT_EXPIRED")
    assert late.status == 200


def test_the_grant_check_order_is_invalid_then_used_then_expired() -> None:
    clock = FakeClock()

    async def call(gateway):
        first = await gateway.open()
        clock.advance(2_000)
        used = await gateway.call("/daemon/session", method="POST", body={"grant": GRANT})
        wrong = await gateway.call("/daemon/session", method="POST", body={"grant": "x" * 64})
        return first, used, wrong

    first, used, wrong = run_gateway(call, config=host_config(launch_ttl_ms=1_000), clock=clock)
    assert first.reply.status == 200
    assert used.json["code"] == "LAUNCH_GRANT_USED"
    assert wrong.json["code"] == "LAUNCH_GRANT_INVALID"


@pytest.mark.parametrize("value", ["", "abc", "1.0", "1, 1", " 1"])
def test_a_malformed_or_repeated_contract_header_is_refused_with_409(value: str) -> None:
    async def call(gateway):
        return await gateway.call(
            "/daemon/session", method="POST", body={"grant": GRANT}, headers={"x-hyprial-session-contract": value}
        )

    reply = run_gateway(call)
    assert (reply.status, reply.json["code"]) == (409, "SESSION_CONTRACT_UNSUPPORTED")
    assert reply.json["data"] == {"min": 1, "max": 1}


def test_the_contract_check_follows_the_method_body_and_shape_checks_and_precedes_the_grant() -> None:
    upstream = FakeUpstream()

    async def call(gateway):
        headers = {"x-hyprial-session-contract": "9"}
        wrong_verb = await gateway.call("/daemon/session", headers=headers)
        bad_media = await gateway.call(
            "/daemon/session", method="POST", raw_body=b"{}", headers={**headers, "content-type": "text/plain"}
        )
        bad_json = await gateway.call("/daemon/session", method="POST", raw_body=b"nope", headers=headers)
        wrong_grant = await gateway.call("/daemon/session", method="POST", body={"grant": "x" * 64}, headers=headers)
        malformed_body = await gateway.call("/daemon/session", method="POST", body={"grant": 5}, headers=headers)
        return wrong_verb, bad_media, bad_json, wrong_grant, malformed_body

    results = run_gateway(call, upstream=upstream)
    assert [r.json["code"] for r in results] == [
        "HTTP_METHOD_NOT_ALLOWED",
        "HTTP_UNSUPPORTED_MEDIA_TYPE",
        "INVALID_REQUEST",
        "SESSION_CONTRACT_UNSUPPORTED",
        "INVALID_REQUEST",
    ]
    assert upstream.captured == []


def test_method_routes_check_in_the_order_405_401_404_409_pin_csrf_body() -> None:
    upstream = FakeUpstream(
        catalog=catalog_payload(
            methods=[
                {"name": "agent.list", "state": "available", "callable": True, "mutates": False},
                {"name": "agent.create", "state": "available", "callable": True, "mutates": True},
                {"name": "hosts", "state": "blocked", "callable": False, "mutates": False},
            ]
        )
    )

    async def call(gateway):
        junk = {"content-type": "text/plain"}
        anonymous = await gateway.call("/daemon/request/agent.list", method="POST", raw_body=b"junk", headers=junk)
        opened = await gateway.open()
        good = opened.headers()
        wrong_verb_unauth = await gateway.call("/daemon/request/agent.list", headers={})
        malformed_name = await gateway.call("/daemon/request/Bad", method="GET")
        unknown = await gateway.call("/daemon/request/nope", method="POST", raw_body=b"junk", headers={**good, **junk})
        blocked = await gateway.call("/daemon/request/hosts", method="POST", raw_body=b"junk", headers={**good, **junk})
        pin = await gateway.call(
            "/daemon/request/agent.create", method="POST", raw_body=b"junk",
            headers={**good, **junk, "x-hyprial-catalog-digest": "other", "x-hyprial-csrf": "bad"},
        )
        csrf = await gateway.call(
            "/daemon/request/agent.create", method="POST", raw_body=b"junk", headers={**good, **junk, "x-hyprial-csrf": "bad"}
        )
        media = await gateway.call("/daemon/request/agent.create", method="POST", raw_body=b"junk", headers={**good, **junk})
        return anonymous, wrong_verb_unauth, malformed_name, unknown, blocked, pin, csrf, media

    results = run_gateway(call, upstream=upstream)
    assert [r.json["code"] for r in results] == [
        "SESSION_REQUIRED",
        "HTTP_METHOD_NOT_ALLOWED",
        "METHOD_NOT_FOUND",
        "METHOD_NOT_FOUND",
        "METHOD_NOT_CALLABLE",
        "CATALOG_MISMATCH",
        "CSRF_REJECTED",
        "HTTP_UNSUPPORTED_MEDIA_TYPE",
    ]


def test_every_failure_on_a_method_path_carries_the_method_and_no_dispatch_header() -> None:
    async def call(gateway):
        origin = await gateway.call(
            "/daemon/request/agent.list", method="POST", body={}, origin="http://hostile.invalid"
        )
        session = await gateway.call("/daemon/request/agent.list", method="POST", body={})
        return origin, session

    for reply in run_gateway(call):
        assert reply.json["method"] == "agent.list"
        assert "x-hyprial-dispatch" not in reply.headers
        assert reply.headers["x-hyprial-request-id"] == reply.json["requestId"]


def test_logout_accepts_any_json_object_but_still_needs_csrf() -> None:
    async def call(gateway):
        opened = await gateway.open()
        no_csrf = await gateway.call(
            "/daemon/session/logout", method="POST", body={"extra": 1}, headers=opened.headers(csrf="stale")
        )
        accepted = await gateway.call(
            "/daemon/session/logout", method="POST", body={"extra": 1}, headers=opened.headers()
        )
        return no_csrf, accepted

    no_csrf, accepted = run_gateway(call)
    assert (no_csrf.status, no_csrf.json["code"]) == (403, "CSRF_REJECTED")
    assert accepted.status == 200 and accepted.json["revoked"] is True


def test_status_does_not_probe_and_is_false_without_the_session_cookie() -> None:
    upstream = FakeUpstream()

    async def call(gateway):
        opened = await gateway.open()
        before = len(upstream.captured)
        control = next(p for p in opened.cookies.split("; ") if p.startswith("hyprial_gui_control="))
        headers = {"cookie": control, "x-hyprial-view": str(opened.session["view"])}
        reply = await gateway.call("/daemon/session/status", method="POST", body={}, headers=headers)
        return reply, len(upstream.captured) - before

    reply, probes = run_gateway(call, upstream=upstream)
    assert reply.status == 200 and reply.json["current"] is False and probes == 0


def test_the_contract_mock_driver_requires_a_principal() -> None:
    env = {
        "HYPRIAL_GUI_BACKEND_DRIVER": "contract-mock",
        "HYPRIAL_BACKEND_ENDPOINT": "http://127.0.0.1:8791",
        "HYPRIAL_GUI_UPSTREAM_TOKEN": "t",
        "HYPRIAL_GUI_LAUNCH_GRANT": GRANT,
    }
    with pytest.raises(ConfigError, match="contract-mock"):
        read_config(env)
    assert read_config({**env, "HYPRIAL_GUI_PRINCIPAL": "user:owner", "HYPRIAL_NODE_ID": "n"}).principal


@pytest.mark.parametrize(
    "raw",
    [
        b"/daemon/x/../session",
        b"/x/../daemon/session",
        b"/daemon/./session",
        b"/daemon/%2e%2e/daemon/session",
        b"/daemon/.%2E/daemon/session",
        b"/daemon\\session",
        b"/daemon/request/../session",
    ],
)
def test_target_dot_segments_are_normalised_like_a_url_parser_not_rejected(raw: bytes) -> None:
    async def call(gateway):
        return await gateway.raw(
            raw,
            method="POST",
            headers={"content-type": "application/json"},
            body=json.dumps({"grant": "x" * 64}).encode(),
        )

    status, _, body = run_gateway(call)
    assert (status, json.loads(body)["code"]) == (403, "LAUNCH_GRANT_INVALID")


def test_a_port_80_host_is_always_refused_because_a_url_parser_drops_the_port() -> None:
    from hyprial.shell.impl.gui.host.envelope import HostFailure
    from hyprial.shell.impl.gui.host.session import require_same_origin

    with pytest.raises(HostFailure) as caught:
        require_same_origin(
            host="127.0.0.1:80", origin=None, fetch_site=None, listener_port=80, require_origin=False
        )
    assert caught.value.code == "HTTP_HOST_DENIED"
    assert require_same_origin(
        host="127.0.0.1:04173", origin="http://127.0.0.1:4173", fetch_site=None, listener_port=4173,
        require_origin=True,
    ) == "http://127.0.0.1:4173"


def test_a_url_fragment_on_the_target_is_not_part_of_the_path() -> None:
    async def call(gateway):
        return await gateway.raw(
            b"/daemon/session#fragment",
            method="POST",
            headers={"content-type": "application/json"},
            body=json.dumps({"grant": "x" * 64}).encode(),
        )

    status, _, body = run_gateway(call)
    assert (status, json.loads(body)["code"]) == (403, "LAUNCH_GRANT_INVALID")


def test_the_refresh_shape_is_checked_before_the_version_and_the_response_names_the_applied_version() -> None:
    async def call(gateway):
        opened = await gateway.open(headers={"x-hyprial-session-contract": "1"})
        shape = await gateway.call(
            "/daemon/session", method="POST", body={"x": 1}, headers={"x-hyprial-session-contract": "9"}
        )
        control = next(p for p in opened.cookies.split("; ") if p.startswith("hyprial_gui_control="))
        version = await gateway.call(
            "/daemon/session",
            method="POST",
            body={},
            headers={"cookie": control, "x-hyprial-view": str(opened.session["view"]), "x-hyprial-session-contract": "9"},
        )
        return opened, shape, version

    opened, shape, version = run_gateway(call)
    assert opened.session["sessionContract"] == {"version": 1, "min": 1, "max": 1}
    assert shape.json["code"] == "INVALID_REQUEST"
    assert version.json["code"] == "SESSION_CONTRACT_UNSUPPORTED"
