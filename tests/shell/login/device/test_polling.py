"""login: polling scenarios."""

from __future__ import annotations
from pathlib import Path
import pytest
from tests.fixtures.network.fake_oidc import FakeOidcConfig, fake_oidc  # noqa: E402
from hyprial.shell.impl.login.flow import (
    LoginError,
)
from hyprial.daemon.impl.configuration.identity import resolve_node_owner

from .support import (
    _Clock,
    _fail_token_polls,
    _home,
    _run,
    _snapshot,
)


# -- T4: the polling rhythm is the server's alone --------------------------------


def test_t4_interval_slow_down_and_expiry_use_server_values(tmp_path: Path) -> None:
    home = _home(tmp_path)
    clock = _Clock()
    config = FakeOidcConfig(
        device_interval=2, device_expires_in=30, pending_polls=2, slow_down_at=1
    )
    with fake_oidc(config) as server:
        result = _run(server, home, sleep=clock.sleep, clock=clock.clock)
    assert result.owner == "alicesmith"
    # Poll 1 (t=0): slow_down ⇒ interval 2+5=7 ⇒ sleep(7).
    # Poll 2 (t=7): pending ⇒ sleep(7).  Poll 3 (t=14): success.
    assert clock.slept[0] >= 7.0, clock.slept
    assert clock.slept[1] >= 7.0, clock.slept


def test_t4_no_interval_measured_below_server_value(tmp_path: Path) -> None:
    home = _home(tmp_path)
    clock = _Clock()
    config = FakeOidcConfig(device_interval=2, pending_polls=2)
    with fake_oidc(config) as server:
        _run(server, home, sleep=clock.sleep, clock=clock.clock)
    # Without slow_down every sleep is exactly the server's interval.
    assert clock.slept == [2.0, 2.0], clock.slept


def test_t4_expires_in_stops_the_poll(tmp_path: Path) -> None:
    home = _home(tmp_path)
    clock = _Clock()
    config = FakeOidcConfig(device_interval=30, device_expires_in=10, pending_polls=99)
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home, sleep=clock.sleep, clock=clock.clock)
    assert caught.value.code == "DEVICE_CODE_EXPIRED"
    assert "re-run hyprial login" in str(caught.value)
    assert "sign in with an existing account rather than Feishu" in str(caught.value)
    # Expired without sleeping past the budget: poll 1 pending sleeps 30,
    # then the deadline (10s) has passed and the loop stops.
    assert clock.slept == [30.0], clock.slept
    assert _snapshot(home) == {}


def test_t4_real_clock_honors_server_interval(tmp_path: Path) -> None:
    """One real-time proof that the computed rhythm reaches time.sleep."""

    import time as _time

    home = _home(tmp_path)
    config = FakeOidcConfig(device_interval=1, pending_polls=2)
    with fake_oidc(config) as server:
        start = _time.monotonic()
        _run(server, home)
        elapsed = _time.monotonic() - start
    assert elapsed >= 2.0, f"two interval=1 waits did not happen: {elapsed}"


# -- T5: the five token-endpoint states ------------------------------------------


def test_t5_pending_then_success(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(pending_polls=3)) as server:
        result = _run(server, home)
    assert result.owner == "alicesmith"
    assert resolve_node_owner({"HYPRIAL_OWNER": ""}, hyprial_home=home) == "alicesmith"


def test_t5_denied_terminates_with_zero_writes(tmp_path: Path) -> None:
    # Guard: a mutation that treats access_denied as pending polls forever
    # at real time.sleep — the guard turns that hang into a fast, legible
    # red instead of a suite timeout (there is no pytest-timeout here).
    sleeps: list[float] = []

    def guard_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) > 5:
            raise AssertionError(
                "access_denied must be terminal — the client keeps polling"
            )

    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(outcome="denied")) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home, sleep=guard_sleep)
    assert caught.value.code == "ACCESS_DENIED"
    assert sleeps == [], "a terminal denial must not be slept over"
    assert _snapshot(home) == {}


def test_t5_expired_token_terminates_and_hint_rerun(tmp_path: Path) -> None:
    home = _home(tmp_path)
    with fake_oidc(FakeOidcConfig(outcome="expired")) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
    assert caught.value.code == "DEVICE_CODE_EXPIRED"
    assert "re-run" in str(caught.value)
    assert "sign in with an existing account rather than Feishu" in str(caught.value)
    assert _snapshot(home) == {}


def test_t5_unknown_device_code_is_expired(tmp_path: Path) -> None:
    """Casdoor answers an unknown device_code with expired_token
    (controllers/token.go:280-287; its device state is process memory) —
    the client maps it to the same terminal state, not an endless poll."""

    home = _home(tmp_path)
    config = FakeOidcConfig(known_device_code="a-code-the-server-forgets")
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home)
        polls = [r for r in server.records if r.path == "/api/token"]
    assert caught.value.code == "DEVICE_CODE_EXPIRED"
    assert polls and polls[0].form["device_code"] == [config.device_code]


def test_transport_errors_mid_poll_are_retried_while_the_code_is_valid(
    tmp_path: Path, monkeypatch
) -> None:
    import ssl

    home = _home(tmp_path)
    clock = _Clock()
    polls = _fail_token_polls(
        monkeypatch,
        {
            2: ConnectionResetError(54, "Connection reset by peer"),
            3: ssl.SSLEOFError(8, "[SSL: UNEXPECTED_EOF_WHILE_READING]"),
        },
    )
    config = FakeOidcConfig(device_interval=2, pending_polls=2)
    with fake_oidc(config) as server:
        result = _run(server, home, sleep=clock.sleep, clock=clock.clock)
    assert result.owner == "alicesmith"
    # Poll 1 pending, 2 reset, 3 TLS EOF, 4 pending, 5 success: every retry
    # waited the server's interval, never faster.
    assert len(polls) == 5, polls
    assert clock.slept == [2.0, 2.0, 2.0, 2.0], clock.slept


def test_transport_errors_until_expiry_end_as_expired_naming_the_cause(
    tmp_path: Path, monkeypatch
) -> None:
    home = _home(tmp_path)
    clock = _Clock()
    _fail_token_polls(
        monkeypatch,
        {n: ConnectionResetError(54, "Connection reset by peer") for n in range(1, 50)},
    )
    config = FakeOidcConfig(device_interval=5, device_expires_in=12, pending_polls=0)
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home, sleep=clock.sleep, clock=clock.clock)
    # Bounded by the device code's own lifetime, not a new budget.
    assert caught.value.code == "DEVICE_CODE_EXPIRED"
    assert "Connection reset by peer" in str(caught.value)
    assert sum(clock.slept) >= 12.0
    assert _snapshot(home) == {}


def test_access_denied_after_a_transport_error_is_still_terminal(
    tmp_path: Path, monkeypatch
) -> None:
    """Retrying transport errors must not soften a real terminal answer."""

    home = _home(tmp_path)
    clock = _Clock()
    _fail_token_polls(monkeypatch, {1: ConnectionResetError(54, "reset")})
    config = FakeOidcConfig(device_interval=2, outcome="denied")
    with fake_oidc(config) as server:
        with pytest.raises(LoginError) as caught:
            _run(server, home, sleep=clock.sleep, clock=clock.clock)
    assert caught.value.code == "ACCESS_DENIED"
    assert _snapshot(home) == {}
