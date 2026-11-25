"""Same-origin admission, launch grant, session rotation and upstream refusals."""

from __future__ import annotations

from typing import Any

import pytest

from tests.shell.gui.host.support import (
    DECLARATION,
    DIGEST,
    EPOCH,
    GRANT,
    PRINCIPAL,
    UPSTREAM_TOKEN,
    FakeClock,
    FakeUpstream,
    browser_headers,
    catalog_payload,
    host_config,
    ready_payload,
    run_gateway,
)


def test_exact_loopback_host_origin_and_same_origin_fetch_metadata_are_required() -> None:
    upstream = FakeUpstream()

    async def scenario(gateway) -> None:
        body = {"grant": GRANT}
        missing = await gateway.call("/daemon/session", method="POST", body=body, omit_origin=True)
        assert missing.status == 403
        assert missing.json["code"] == "ORIGIN_REQUIRED" and missing.json["dispatch"] == "not-dispatched"
        assert missing.json["ok"] is False

        wrong_origin = await gateway.call("/daemon/session", method="POST", body=body, origin="http://hostile.invalid")
        assert (wrong_origin.status, wrong_origin.json["code"]) == (403, "ORIGIN_REJECTED")

        wrong_host = await gateway.call("/daemon/session", method="POST", body=body, host="localhost:9999")
        assert (wrong_host.status, wrong_host.json["code"]) == (403, "HTTP_HOST_DENIED")

        cross_site = await gateway.call(
            "/daemon/session", method="POST", body=body, headers={"sec-fetch-site": "cross-site"}
        )
        assert (cross_site.status, cross_site.json["code"]) == (403, "ORIGIN_REJECTED")

    run_gateway(scenario, upstream=upstream)
    assert upstream.captured == []


def test_an_authenticated_read_may_omit_origin_but_state_changes_may_not() -> None:
    async def scenario(gateway) -> None:
        opened = await gateway.open()
        assert opened.reply.status == 200
        read = await gateway.call("/daemon/readyz", omit_origin=True, headers=opened.headers())
        assert read.status == 200
        assert read.json["ready"] is True and read.json["transport"] == "ready"

        drift = await gateway.call(
            "/daemon/readyz", headers=opened.headers(digest="sha256:other")
        )
        assert drift.status == 409
        assert drift.json["code"] == "CATALOG_MISMATCH" and drift.json["dispatch"] == "not-dispatched"

        write = await gateway.call(
            "/daemon/session",
            method="POST",
            body={},
            omit_origin=True,
            headers={"x-hyprial-view": str(opened.session["view"])},
        )
        assert (write.status, write.json["code"]) == (403, "ORIGIN_REQUIRED")

    run_gateway(scenario)


def test_one_launch_grant_is_consumed_once_expires_and_never_returns_the_bearer() -> None:
    consumed: list[int] = []

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        assert opened.reply.status == 200
        assert UPSTREAM_TOKEN not in opened.reply.raw
        assert UPSTREAM_TOKEN not in str(dict(opened.reply.headers))
        assert opened.session["driver"] == "daemon" and opened.session["phase"] == "ready"
        assert opened.session["ok"] is True and opened.session["schemaVersion"] == 1
        assert consumed == [1]

        repeated = await gateway.call("/daemon/session", method="POST", body={"grant": GRANT})
        assert (repeated.status, repeated.json["code"]) == (409, "LAUNCH_GRANT_USED")
        assert consumed == [1]

    run_gateway(scenario, on_grant_consumed=lambda: consumed.append(1))

    clock = FakeClock()

    async def expiring(gateway) -> None:
        clock.advance(1_001)
        expired = await gateway.call("/daemon/session", method="POST", body={"grant": GRANT})
        assert (expired.status, expired.json["code"]) == (401, "LAUNCH_GRANT_EXPIRED")
        wrong = await gateway.call("/daemon/session", method="POST", body={"grant": "wrong-grant"})
        assert (wrong.status, wrong.json["code"]) == (403, "LAUNCH_GRANT_INVALID")

    run_gateway(expiring, config=host_config(launch_ttl_ms=1_000), clock=clock)


@pytest.mark.parametrize(
    "body",
    [{"grant": ""}, {"grant": 5}, {"grant": GRANT, "extra": 1}, {"other": 1}],
)
def test_an_initial_session_body_is_exactly_the_grant(body: dict[str, Any]) -> None:
    async def scenario(gateway) -> None:
        reply = await gateway.call("/daemon/session", method="POST", body=body)
        assert (reply.status, reply.json["code"]) == (400, "INVALID_REQUEST")

    run_gateway(scenario)


def test_a_copied_business_cookie_without_the_view_proof_is_rejected() -> None:
    async def scenario(gateway) -> None:
        opened = await gateway.open()
        reply = await gateway.call(
            "/daemon/readyz",
            headers={"cookie": opened.cookies, "x-hyprial-generation": str(opened.session["generation"])},
        )
        assert reply.status == 401
        assert reply.json["code"] == "SESSION_REQUIRED" and reply.json["dispatch"] == "not-dispatched"

    run_gateway(scenario)


def test_refresh_rotates_generation_and_csrf_and_retires_the_old_credentials() -> None:
    upstream = FakeUpstream()

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        control = next(p for p in opened.cookies.split("; ") if p.startswith("hyprial_gui_control="))
        refreshed = await gateway.call(
            "/daemon/session",
            method="POST",
            body={},
            headers={"cookie": control, "x-hyprial-view": str(opened.session["view"])},
        )
        assert refreshed.status == 200
        following = refreshed.json
        assert following["view"] == opened.session["view"]
        assert following["generation"] != opened.session["generation"]
        assert following["csrf"] != opened.session["csrf"]

        retired = await gateway.call(
            "/daemon/request/agent.list",
            method="POST",
            body={},
            headers={
                "cookie": opened.cookies,
                "x-hyprial-view": str(opened.session["view"]),
                "x-hyprial-generation": str(opened.session["generation"]),
            },
        )
        assert retired.status == 401
        assert retired.json["ok"] is False and retired.json["dispatch"] == "not-dispatched"

        stale_csrf = await gateway.call(
            "/daemon/request/agent.create",
            method="POST",
            body={"name": "blocked"},
            headers=browser_headers(following, refreshed.cookies(), str(opened.session["csrf"])),
        )
        assert (stale_csrf.status, stale_csrf.json["code"]) == (403, "CSRF_REJECTED")
        assert stale_csrf.json["dispatch"] == "not-dispatched"

        no_csrf_needed = await gateway.call(
            "/daemon/request/agent.list",
            method="POST",
            body={},
            headers={
                "cookie": refreshed.cookies(),
                "x-hyprial-view": str(following["view"]),
                "x-hyprial-generation": str(following["generation"]),
                "x-hyprial-catalog-digest": DIGEST,
            },
        )
        assert no_csrf_needed.status == 200

    run_gateway(scenario, upstream=upstream)
    assert [r for r in upstream.captured if r.url.path.startswith("/daemon/request/agent.create")] == []


def test_refresh_needs_the_control_cookie_and_view_proof() -> None:
    async def scenario(gateway) -> None:
        opened = await gateway.open()
        without = await gateway.call("/daemon/session", method="POST", body={})
        assert (without.status, without.json["code"]) == (401, "SESSION_REQUIRED")
        wrong_view = await gateway.call(
            "/daemon/session", method="POST", body={}, headers={"cookie": opened.cookies, "x-hyprial-view": "wrong"}
        )
        assert wrong_view.status == 401
        filled = await gateway.call(
            "/daemon/session",
            method="POST",
            body={"grant": GRANT, "x": 1},
            headers={"cookie": opened.cookies, "x-hyprial-view": str(opened.session["view"])},
        )
        assert filled.status == 400
        nonempty = await gateway.call(
            "/daemon/session",
            method="POST",
            body={"x": 1},
            headers={"cookie": opened.cookies, "x-hyprial-view": str(opened.session["view"])},
        )
        assert (nonempty.status, nonempty.json["code"]) == (400, "INVALID_REQUEST")

    run_gateway(scenario)


def test_sessions_expire_and_logout_revokes_both_cookies() -> None:
    clock = FakeClock()

    async def expiring(gateway) -> None:
        opened = await gateway.open()
        clock.advance(1_001)
        expired = await gateway.call("/daemon/readyz", headers=opened.headers())
        assert (expired.status, expired.json["code"]) == (401, "SESSION_REQUIRED")

    run_gateway(expiring, config=host_config(session_ttl_ms=1_000), clock=clock)

    async def revoking(gateway) -> None:
        live = await gateway.open()
        logout = await gateway.call(
            "/daemon/session/logout", method="POST", body={}, headers=live.headers()
        )
        assert logout.status == 200
        assert logout.json["ok"] is True and logout.json["revoked"] is True
        cleared = logout.headers.get_list("set-cookie")
        assert cleared == [
            "hyprial_gui_control=; Path=/daemon; Max-Age=0; HttpOnly; SameSite=Strict",
            "hyprial_gui_session=; Path=/daemon; Max-Age=0; HttpOnly; SameSite=Strict",
        ]
        after = await gateway.call("/daemon/readyz", headers=live.headers())
        assert after.status == 401
        refresh = await gateway.call(
            "/daemon/session",
            method="POST",
            body={},
            headers={"cookie": live.cookies, "x-hyprial-view": str(live.session["view"])},
        )
        assert refresh.status == 401

    run_gateway(revoking)


def test_logout_requires_csrf_and_a_current_generation() -> None:
    async def scenario(gateway) -> None:
        live = await gateway.open()
        no_csrf = await gateway.call(
            "/daemon/session/logout", method="POST", body={}, headers=live.headers(csrf="wrong")
        )
        assert (no_csrf.status, no_csrf.json["code"]) == (403, "CSRF_REJECTED")
        stale = live.headers()
        stale["x-hyprial-generation"] = "old"
        generation = await gateway.call("/daemon/session/logout", method="POST", body={}, headers=stale)
        assert (generation.status, generation.json["code"]) == (403, "GENERATION_REJECTED")
        wrong_verb = await gateway.call("/daemon/session/logout", headers=live.headers())
        assert (wrong_verb.status, wrong_verb.json["code"]) == (405, "HTTP_METHOD_NOT_ALLOWED")

    run_gateway(scenario)


UPSTREAM_REFUSALS = [
    ({"ready_status": 503, "ready": {"ok": False, "code": "DAEMON_RESTORING"}}, 503, "HTTP_NOT_READY"),
    ({"ready_status": 401, "ready": {"ok": False}}, 502, "UPSTREAM_AUTH_REJECTED"),
    (
        {"ready": ready_payload(backend={"available": True, "owner": "other", "nodeId": PRINCIPAL.node, "epoch": EPOCH})},
        502,
        "PRINCIPAL_MISMATCH",
    ),
    (
        {"ready": ready_payload(backend={"available": True, "owner": PRINCIPAL.owner, "nodeId": "other", "epoch": EPOCH})},
        502,
        "PRINCIPAL_MISMATCH",
    ),
    (
        {"catalog": catalog_payload(principal={"owner": PRINCIPAL.owner, "actor": "agent:owner:node:other"})},
        502,
        "PRINCIPAL_MISMATCH",
    ),
    ({"catalog": catalog_payload(catalogDigest="sha256:other")}, 502, "CATALOG_MISMATCH"),
    (
        {"ready": ready_payload(catalog={"fresh": False, "catalogDigest": DIGEST, "declarationDigest": DECLARATION})},
        503,
        "HTTP_NOT_READY",
    ),
]


@pytest.mark.parametrize(("overrides", "status", "code"), UPSTREAM_REFUSALS)
def test_the_probe_refuses_before_minting_and_does_not_burn_the_grant(overrides, status, code) -> None:
    upstream = FakeUpstream(**overrides)

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        assert opened.reply.status == status
        assert opened.reply.json["code"] == code and opened.reply.json["dispatch"] == "not-dispatched"
        assert "phase" not in opened.reply.json
        assert opened.cookies == ""
        upstream.ready, upstream.ready_status = None, 200
        upstream.catalog, upstream.catalog_status = None, 200
        retried = await gateway.open()
        assert retried.reply.status == 200

    run_gateway(scenario, upstream=upstream)


def test_the_restoring_refusal_is_relayed_with_its_upstream_code() -> None:
    restoring = {"schemaVersion": 1, "ok": False, "ready": False, "code": "DAEMON_RESTORING", "requestId": "r"}

    async def scenario(gateway) -> None:
        opened = await gateway.open()
        assert opened.reply.status == 503
        assert opened.reply.json["code"] == "HTTP_NOT_READY"
        assert opened.reply.json["data"] == {"upstreamCode": "DAEMON_RESTORING"}

    run_gateway(scenario, upstream=FakeUpstream(ready_status=503, ready=restoring))
