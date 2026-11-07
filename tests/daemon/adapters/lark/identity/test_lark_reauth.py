"""Tests for the lark-cli user-credential watchdog (hyprial.daemon.impl.adapters.lark.credentials.reauth).

The negative cases are the point: a *ready* credential must never bother the
human, and a token the lazy refresh can save must never escalate into a
device flow.  Happy-path-only testing is what let #142-shaped mechanisms
ship unTriggered and #141's lying `verified` field pass as a criterion.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest
import typer

from hyprial.daemon.impl.adapters.lark import reauth
from hyprial.daemon.impl.adapters.lark.credentials.reauth import (
    ReauthError,
    ReauthState,
    check,
    classify_user_identity,
    complete,
    load_state,
    save_state,
)
NOW = 1_800_000_000.0


def _payload(
    status: str = "ready",
    token_status: str | None = "valid",
    refresh_expires: str | None = None,
    verified: bool = True,
) -> dict[str, Any]:
    user: dict[str, Any] = {"status": status}
    if token_status is not None:
        user["tokenStatus"] = token_status
    if refresh_expires is not None:
        user["refreshExpiresAt"] = refresh_expires
    return {"verified": verified, "appId": "cli_test", "identities": {"user": user}}


def _runner(script: list[dict[str, Any] | Exception]) -> tuple[reauth.CliRunner, list[tuple[str, ...]]]:
    """A scripted lark-cli: pops one response per call, records argv."""

    calls: list[tuple[str, ...]] = []
    queue = list(script)

    def run(arguments: tuple[str, ...] | list[str], *, timeout: float) -> dict[str, Any]:
        calls.append(tuple(arguments))
        if not queue:
            raise AssertionError(f"unexpected lark-cli call: {arguments}")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return run, calls


def _sends() -> tuple[reauth.NotifySender, list[tuple[str, str, str]]]:
    sent: list[tuple[str, str, str]] = []

    def send(sender: str, route: str, text: str) -> None:
        sent.append((sender, route, text))

    return send, sent


# Strings that must NEVER leave this machine.  Field names AND the actual
# per-test credential values: the assertion target is the message content
# itself, not a result payload adjacent to it (a zero-discrimination test is
# worse than none -- it teaches the next reader coverage exists).
_FORBIDDEN_FIELD_NAMES = (
    "app_secret",
    "appSecret",
    "client_secret",
    "access_token",
    "refresh_token",
    "device_code",
)


def assert_outbound_messages_clean(
    sent: list[tuple[str, str, str]], *forbidden_values: str
) -> None:
    """Every string that leaves the machine (sender, route, text) of EVERY
    pushed message carries neither a secret field name nor a secret value."""

    forbidden = [*_FORBIDDEN_FIELD_NAMES, *(v for v in forbidden_values if v)]
    for sender, route, text in sent:
        for value in (sender, route, text):
            for token in forbidden:
                assert token not in value, (
                    f"outbound message carries forbidden {token!r}: {value!r}"
                )


def issued_device_code(state_path: Path) -> str:
    """The device_code THIS run actually minted, read from the state file.

    The forbidden value in a leak assertion must be the value the flow really
    issued -- never a second literal copy, which would silently lose
    discrimination the day someone changes the fake's issued code without
    updating the assertion.
    """

    state = load_state(state_path)
    assert state is not None and state.device_code, (
        f"no pending device flow recorded at {state_path}"
    )
    return state.device_code


# ---------------------------------------------------------------------------
# classification


def test_classify_ready_valid_is_ok() -> None:
    assert classify_user_identity(_payload(), now=NOW) == "ok"


def test_classify_ignores_top_level_verified() -> None:
    """#141: verified reflects the default/auto identity, not the user."""

    assert (
        classify_user_identity(_payload(verified=False), now=NOW) == "ok"
    )
    assert (
        classify_user_identity(
            _payload(status="missing", token_status=None, verified=True), now=NOW
        )
        == "needs_human"
    )


def test_classify_needs_refresh_with_live_refresh_token() -> None:
    payload = _payload(token_status="needs_refresh", refresh_expires="2999-01-01T00:00:00+00:00")
    assert classify_user_identity(payload, now=NOW) == "refreshable"


def test_classify_needs_refresh_without_expiry_gets_one_chance() -> None:
    payload = _payload(token_status="needs_refresh")
    assert classify_user_identity(payload, now=NOW) == "refreshable"


def test_classify_needs_refresh_with_dead_refresh_token_needs_human() -> None:
    payload = _payload(token_status="needs_refresh", refresh_expires="2001-01-01T00:00:00+00:00")
    assert classify_user_identity(payload, now=NOW) == "needs_human"


def test_classify_missing_needs_human() -> None:
    assert classify_user_identity(_payload(status="missing", token_status=None), now=NOW) == "needs_human"


def test_classify_absent_user_identity_needs_human() -> None:
    assert classify_user_identity({"appId": "cli_test"}, now=NOW) == "needs_human"


# ---------------------------------------------------------------------------
# state


def test_state_roundtrip_is_0600(tmp_path: Path) -> None:
    path = tmp_path / "lark-reauth.json"
    state = ReauthState(
        app_id="cli_test",
        device_code="dc-secret",
        verification_url="https://example.com/activate",
        expires_at=NOW + 600,
        notify_route="route:app:dm",
        sender="agent:o:m:w",
        domains=("all",),
    )
    save_state(path, state)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = load_state(path)
    assert loaded == state


def test_load_state_missing_or_corrupt(tmp_path: Path) -> None:
    assert load_state(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    assert load_state(bad) is None


def test_pending_valid_and_cooldown() -> None:
    pending = ReauthState(device_code="dc", expires_at=NOW + 100)
    assert pending.pending_valid(NOW)
    assert not pending.pending_valid(NOW + 200)
    fresh = ReauthState(notified_at=NOW)
    assert not fresh.may_notify(NOW + 100, cooldown=3600)
    assert fresh.may_notify(NOW + 3700, cooldown=3600)
    never_notified = ReauthState()
    assert never_notified.may_notify(NOW)


# ---------------------------------------------------------------------------
# check: negative paths first


def test_check_ok_never_notifies_never_initiates(tmp_path: Path) -> None:
    runner, calls = _runner([_payload()])
    send, sent = _sends()
    result = check(
        runner,
        state_path=tmp_path / "state.json",
        notify_route="route:app:dm",
        sender="agent:o:m:w",
        send=send,
        now=NOW,
    )
    assert result.status == "ok"
    assert sent == []  # not bothered
    # exactly one call: the status read; no refresh read, no device flow
    assert calls == [("auth", "status", "--json", "--verify")]
    assert not (tmp_path / "state.json").exists()


def test_check_needs_refresh_recovered_stays_silent(tmp_path: Path) -> None:
    """The unattended path: lazy refresh saves it -> no device flow, no message."""

    runner, calls = _runner([
        _payload(token_status="needs_refresh"),
        {"ok": True},  # the side-effect-free read
        _payload(),  # re-verify: valid
    ])
    send, sent = _sends()
    result = check(
        runner,
        state_path=tmp_path / "state.json",
        notify_route="route:app:dm",
        sender="agent:o:m:w",
        send=send,
        now=NOW,
    )
    assert result.status == "recovered"
    assert sent == []
    assert not any("login" in call for call in calls)
    assert not (tmp_path / "state.json").exists()


def test_check_permanent_needs_refresh_escalates(tmp_path: Path) -> None:
    """Refresh that never heals (run 977's successor) must escalate honestly."""

    runner, _ = _runner([
        _payload(token_status="needs_refresh"),
        {"ok": True},
        _payload(token_status="needs_refresh"),
        {"device_code": "dc-1", "verification_url": "https://ex.com/a", "expire_in": 600},
    ])
    send, sent = _sends()
    result = check(
        runner,
        state_path=tmp_path / "state.json",
        notify_route="route:app:dm",
        sender="agent:o:m:w",
        send=send,
        now=NOW,
    )
    assert result.status == "awaiting_user"
    assert result.notified is True
    assert len(sent) == 1
    assert_outbound_messages_clean(sent, issued_device_code(tmp_path / "state.json"))


# ---------------------------------------------------------------------------
# check: needs_human paths


def test_check_missing_initiates_and_notifies_once(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    runner, _ = _runner([
        _payload(status="missing", token_status=None),
        {"device_code": "dc-1", "verification_url": "https://ex.com/a", "expire_in": 600},
    ])
    send, sent = _sends()
    result = check(
        runner,
        state_path=state_path,
        notify_route="route:app:dm",
        sender="agent:o:m:w",
        send=send,
        now=NOW,
    )
    assert result.status == "awaiting_user"
    assert result.notified is True
    assert result.notify_route == "route:app:dm"
    assert result.verification_url == "https://ex.com/a"
    assert len(sent) == 1
    sender, route, text = sent[0]
    assert (sender, route) == ("agent:o:m:w", "route:app:dm")
    assert "https://ex.com/a" in text
    # the device_code never reaches the pushed message or the result payload
    issued = issued_device_code(state_path)
    assert_outbound_messages_clean(sent, issued)
    assert issued not in json.dumps(result.to_json())
    # but it is in the 0600 state file, so complete() can finish the flow
    assert issued == "dc-1"


def test_check_pending_within_cooldown_does_not_renotify(tmp_path: Path) -> None:
    """Anti-storm: a still-valid pending link is neither replaced nor re-pushed."""

    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        ReauthState(
            app_id="cli_test",
            device_code="dc-1",
            verification_url="https://ex.com/a",
            expires_at=NOW + 600,
            notified_at=NOW - 60,  # notified a minute ago
            notify_route="route:app:dm",
            sender="agent:o:m:w",
        ),
    )
    runner, calls = _runner([_payload(status="missing", token_status=None)])
    send, sent = _sends()
    result = check(
        runner,
        state_path=state_path,
        send=send,
        now=NOW,
    )
    assert result.status == "awaiting_user"
    assert result.notified is False
    assert sent == []
    # no new device flow was minted
    assert not any("login" in call for call in calls)


def test_check_pending_past_cooldown_renotifies_same_link(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        ReauthState(
            app_id="cli_test",
            device_code="dc-1",
            verification_url="https://ex.com/a",
            expires_at=NOW + 600,
            notified_at=NOW - 7200,
            notify_route="route:app:dm",
            sender="agent:o:m:w",
        ),
    )
    runner, calls = _runner([_payload(status="missing", token_status=None)])
    send, sent = _sends()
    result = check(runner, state_path=state_path, send=send, now=NOW)
    assert result.notified is True
    assert len(sent) == 1
    assert "https://ex.com/a" in sent[0][2]
    assert not any("login" in call for call in calls)
    assert_outbound_messages_clean(sent, issued_device_code(state_path))


def test_check_expired_pending_regenerates_fresh_link(tmp_path: Path) -> None:
    """An expired link is never reused; the next check mints a new one."""

    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        ReauthState(
            app_id="cli_test",
            device_code="dc-old",
            verification_url="https://ex.com/old",
            expires_at=NOW - 10,
            notified_at=NOW - 7200,
        ),
    )
    runner, _ = _runner([
        _payload(status="missing", token_status=None),
        {"device_code": "dc-new", "verification_url": "https://ex.com/new", "expire_in": 600},
    ])
    send, sent = _sends()
    result = check(runner, state_path=state_path, send=send, now=NOW)
    assert result.verification_url == "https://ex.com/new"
    state = load_state(state_path)
    assert state is not None and state.device_code == "dc-new"
    # "dc-old" is this test's own authored input (the expired link's code);
    # the fresh code is read back from what the flow actually minted.
    assert_outbound_messages_clean(sent, "dc-old", issued_device_code(state_path))


def test_check_notify_failure_still_saves_state(tmp_path: Path) -> None:
    """Degradation: the link survives in the state file even when the push fails."""

    state_path = tmp_path / "state.json"
    runner, _ = _runner([
        _payload(status="missing", token_status=None),
        {"device_code": "dc-1", "verification_url": "https://ex.com/a", "expire_in": 600},
    ])

    def failing_send(sender: str, route: str, text: str) -> None:
        raise RuntimeError("daemon down")

    result = check(
        runner,
        state_path=state_path,
        notify_route="route:app:dm",
        sender="agent:o:m:w",
        send=failing_send,
        now=NOW,
    )
    assert result.status == "awaiting_user"
    assert result.notified is False
    assert result.error == "RuntimeError"
    state = load_state(state_path)
    assert state is not None and state.device_code == "dc-1"
    # notified_at stays unset, so the next check re-attempts immediately
    assert state.notified_at is None


def test_check_no_route_no_sender_degrades_to_local(tmp_path: Path) -> None:
    runner, _ = _runner([
        _payload(status="missing", token_status=None),
        {"device_code": "dc-1", "verification_url": "https://ex.com/a", "expire_in": 600},
    ])
    send, sent = _sends()
    result = check(
        runner,
        state_path=tmp_path / "state.json",
        send=send,
        env={},
        now=NOW,
    )
    assert result.status == "awaiting_user"
    assert result.notified is False
    assert sent == []


def test_check_remembers_route_and_sender_from_first_run(tmp_path: Path) -> None:
    """Configure once via flag/env; the timer's bare later runs reuse it."""

    state_path = tmp_path / "state.json"
    runner, _ = _runner([
        _payload(status="missing", token_status=None),
        {"device_code": "dc-1", "verification_url": "https://ex.com/a", "expire_in": 600},
    ])
    send, sent = _sends()
    check(
        runner,
        state_path=state_path,
        notify_route="route:app:dm",
        sender="agent:o:m:w",
        send=send,
        env={},
        now=NOW,
    )
    state = load_state(state_path)
    assert state is not None
    assert state.notify_route == "route:app:dm"
    assert state.sender == "agent:o:m:w"


def test_check_env_var_route(tmp_path: Path) -> None:
    runner, _ = _runner([
        _payload(status="missing", token_status=None),
        {"device_code": "dc-1", "verification_url": "https://ex.com/a", "expire_in": 600},
    ])
    send, sent = _sends()
    result = check(
        runner,
        state_path=tmp_path / "state.json",
        send=send,
        env={
            reauth.ENV_NOTIFY_ROUTE: "route:env:dm",
            reauth.ENV_SENDER: "agent:o:m:env",
        },
        now=NOW,
    )
    assert result.notified is True
    assert sent[0][:2] == ("agent:o:m:env", "route:env:dm")
    assert_outbound_messages_clean(sent, issued_device_code(tmp_path / "state.json"))


# ---------------------------------------------------------------------------
# complete


def test_complete_no_pending(tmp_path: Path) -> None:
    runner, _ = _runner([])
    result = complete(runner, state_path=tmp_path / "state.json", now=NOW)
    assert result.status == "no_pending"


def test_complete_expired_pending_clears_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        ReauthState(device_code="dc-1", expires_at=NOW - 10),
    )
    runner, calls = _runner([])
    result = complete(runner, state_path=state_path, now=NOW)
    assert result.status == "no_pending"
    assert result.to_json().get("expired") is True
    assert calls == []
    assert load_state(state_path) is None


def test_complete_success_clears_and_notifies_recovery(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    issued = "dc-1"  # authored by this test; single source for setup + assert
    save_state(
        state_path,
        ReauthState(
            app_id="cli_test",
            device_code=issued,
            verification_url="https://ex.com/a",
            expires_at=NOW + 600,
            notify_route="route:app:dm",
            sender="agent:o:m:w",
        ),
    )
    runner, calls = _runner([
        {"ok": True},  # auth login --device-code
        _payload(),  # re-verify
    ])
    send, sent = _sends()
    result = complete(runner, state_path=state_path, send=send, now=NOW)
    assert result.status == "authorized"
    assert load_state(state_path) is None
    assert calls[0][:3] == ("auth", "login", "--device-code")
    assert len(sent) == 1  # exactly one all-clear, not a storm
    assert_outbound_messages_clean(sent, issued)


def test_complete_still_not_ready_raises_and_keeps_state(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        ReauthState(device_code="dc-1", verification_url="https://ex.com/a", expires_at=NOW + 600),
    )
    runner, _ = _runner([
        {"ok": True},
        _payload(status="missing", token_status=None),
    ])
    with pytest.raises(ReauthError, match="still is not ready"):
        complete(runner, state_path=state_path, now=NOW)
    assert load_state(state_path) is not None


# ---------------------------------------------------------------------------
# notify anchor guard (the "isolated HYPRIAL_HOME + configured route" trap)


def test_guard_allows_canonical_anchor(tmp_path: Path) -> None:
    reauth.ensure_notify_anchor_canonical(
        state_path=tmp_path / "state.json",
        canonical_path=tmp_path / "state.json",
        route="route:app:dm",
        sender="agent:o:m:w",
    )


def test_guard_allows_local_only_from_isolated_anchor(tmp_path: Path) -> None:
    """No route configured -> the link degrades to local-only, which can
    never orphan a flow; the guard stays out of the way."""

    reauth.ensure_notify_anchor_canonical(
        state_path=tmp_path / "isolated" / "state.json",
        canonical_path=tmp_path / "canonical" / "state.json",
        route=None,
        sender=None,
    )


def test_guard_raises_loudly_and_names_the_fix(tmp_path: Path) -> None:
    """The future scenario, simulated: HYPRIAL_HOME globally isolated AND a route
    configured.  Notification from here would mint a flow no default
    `complete` can find -> red at mint time, naming both anchors."""

    isolated = tmp_path / "isolated" / "lark-reauth.json"
    canonical = tmp_path / "canonical" / "lark-reauth.json"
    with pytest.raises(ReauthError) as caught:
        reauth.ensure_notify_anchor_canonical(
            state_path=isolated,
            canonical_path=canonical,
            route="route:app:dm",
            sender="agent:o:m:w",
        )
    assert caught.value.code == "REAUTH_ANCHOR_ISOLATED"
    message = str(caught.value)
    assert str(isolated) in message and str(canonical) in message
    assert "complete" in message  # says what breaks
    assert reauth.ENV_NOTIFY_ROUTE in message  # and what to change


def test_effective_notification_config_precedence(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    save_state(
        state_path,
        ReauthState(notify_route="route:remembered:dm", sender="agent:o:m:old"),
    )
    # flag > env > remembered
    assert reauth.effective_notification_config(
        state_path,
        notify_route="route:flag:dm",
        sender=None,
        env={reauth.ENV_NOTIFY_ROUTE: "route:env:dm", reauth.ENV_SENDER: "agent:o:m:env"},
    ) == ("route:flag:dm", "agent:o:m:env")
    assert reauth.effective_notification_config(
        state_path, notify_route=None, sender=None, env={}
    ) == ("route:remembered:dm", "agent:o:m:old")


def test_cli_check_refuses_notify_from_isolated_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """CLI level: the guard fires before any device flow is minted."""

    from hyprial.shell.impl.cli.commands.adapter.lark_auth import lark_auth_check

    (tmp_path / "isolated-home").mkdir(mode=0o700)
    monkeypatch.setenv("HYPRIAL_HOME", str(tmp_path / "isolated-home"))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(tmp_path / "isolated-state"))
    monkeypatch.setenv(reauth.ENV_NOTIFY_ROUTE, "route:app:dm")
    monkeypatch.setenv(reauth.ENV_SENDER, "agent:o:m:w")
    monkeypatch.setattr(reauth, "find_lark_cli", lambda: "/nonexistent/lark-cli")

    def no_runner(executable: str) -> reauth.CliRunner:
        raise AssertionError("the guard must fire before any lark-cli call")

    monkeypatch.setattr(reauth, "make_cli_runner", no_runner)
    with pytest.raises(typer.Exit):
        lark_auth_check(
            notify_route=None, sender=None, domain=[], json_output=True
        )
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["ok"] is False
    assert emitted["code"] == "REAUTH_ANCHOR_ISOLATED"
    assert "canonical" in emitted["error"]


# ---------------------------------------------------------------------------
# result payloads never carry secrets


def test_result_json_never_contains_device_code(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    runner, _ = _runner([
        _payload(status="missing", token_status=None),
        {"device_code": "dc-should-not-leak", "verification_url": "https://ex.com/a", "expire_in": 600},
    ])
    send, sent = _sends()
    result = check(
        runner,
        state_path=state_path,
        notify_route="route:app:dm",
        sender="agent:o:m:w",
        send=send,
        now=NOW,
    )
    blob = json.dumps(result.to_json())
    issued = issued_device_code(state_path)
    assert issued == "dc-should-not-leak"
    assert issued not in blob
    assert_outbound_messages_clean(sent, issued)
    for forbidden in ("app_secret", "appSecret", "access_token", "refresh_token"):
        assert forbidden not in blob
