"""The daemon's outbound route sender follows the secret file.

2026-09-26 allen-channel: after its secret file was repaired, every route
send still failed with Lark 10014 until the daemon restarted, because the
SDK gateway was cached by adapter name for the daemon's lifetime.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from hyprial.daemon.impl import application
from hyprial.daemon.impl.application.messaging.delivery import lark as gateway_owner
from hyprial.daemon.impl.adapters.lark.outbound.gateway import GatewayIoAuthority


def _owner(tmp_path: Path, built: list[str]) -> SimpleNamespace:
    def build(config: object, secret: str, state_dir: Path) -> object:
        built.append(secret)
        return SimpleNamespace(secret=secret)

    return SimpleNamespace(
        hyprial_home=tmp_path,
        state_dir=tmp_path,
        _route_gateway_cache=application._RouteGatewayCache(),
        _lark_gateway_with_scope_recovery=build,
    )


def _write_secret(tmp_path: Path, value: str) -> None:
    (tmp_path / "secrets").mkdir(exist_ok=True)
    (tmp_path / "secrets" / "lark-person.json").write_text(
        json.dumps({"appSecret": value}), encoding="utf-8"
    )


CONFIG = SimpleNamespace(name="person", credential_ref="lark-person", app_id="cli_x")


def test_a_repaired_secret_file_reaches_the_next_send_without_a_restart(
    tmp_path: Path,
) -> None:
    built: list[str] = []
    owner = _owner(tmp_path, built)
    _write_secret(tmp_path, "broken-value")
    first = application.DaemonApplication._route_lark_gateway(owner, CONFIG)

    _write_secret(tmp_path, "repaired-value")
    second = application.DaemonApplication._route_lark_gateway(owner, CONFIG)

    assert first.secret == "broken-value"
    assert second.secret == "repaired-value"
    assert built == ["broken-value", "repaired-value"]


def test_an_unchanged_secret_file_reuses_the_cached_gateway(tmp_path: Path) -> None:
    """Negative control: the cache still caches."""

    built: list[str] = []
    owner = _owner(tmp_path, built)
    _write_secret(tmp_path, "same-value")
    first = application.DaemonApplication._route_lark_gateway(owner, CONFIG)
    second = application.DaemonApplication._route_lark_gateway(owner, CONFIG)

    assert second is first
    assert built == ["same-value"]


def test_production_gateway_factory_owns_primary_and_notification_io(
    monkeypatch,
    tmp_path: Path,
) -> None:  # type: ignore[no-untyped-def]
    gateways = []

    class FakeGateway:
        def __init__(self) -> None:
            self.sent = []
            self.recovery = None

        def set_logger(self, *_args, **_kwargs):  # type: ignore[no-untyped-def]
            pass

        def set_permission_recovery(self, callback):  # type: ignore[no-untyped-def]
            self.recovery = callback

        def send_chat(self, chat_id, text, *, idempotency_key):  # type: ignore[no-untyped-def]
            self.sent.append((chat_id, text, idempotency_key))
            return "om_test"

    def from_credentials(_app_id: str, _secret: str) -> FakeGateway:
        gateway = FakeGateway()
        gateways.append(gateway)
        return gateway

    class FakeScopeClient:
        def __init__(self, *_args):  # type: ignore[no-untyped-def]
            pass

        def apply_scopes(self, *_args):  # type: ignore[no-untyped-def]
            return None

    monkeypatch.setattr(
        gateway_owner.LarkSdkGateway, "from_credentials", from_credentials
    )
    monkeypatch.setattr(gateway_owner, "LarkScopeClient", FakeScopeClient)
    config = SimpleNamespace(
        name="person",
        app_id="cli_x",
        default_route="dm",
        routes=(
            SimpleNamespace(
                name="dm",
                type="direct",
                native_id="oc_test",
            ),
        ),
    )
    owner = application.DaemonApplication._lark_gateway_with_scope_recovery(
        config,
        "secret",
        tmp_path,
        owned=True,
    )
    assert isinstance(owner, GatewayIoAuthority)
    assert len(gateways) == 2
    assert gateways[0].recovery is not None
    assert gateways[1].recovery is None
    assert owner.send_chat("oc_main", "hello", idempotency_key="main") == "om_test"
    assert gateways[0].sent == [("oc_main", "hello", "main")]
    assert owner.close(2.0)


def test_route_cache_bounds_undrained_replacements_and_reuses_drained_credit():
    import pytest

    class Gateway:
        drained = False

        def close(self, timeout):
            return self.drained

    cache = application._RouteGatewayCache(retirement_capacity=2)
    old, middle, current, rejected = (Gateway() for _ in range(4))
    cache.put("route", "old", old)
    cache.put("route", "middle", middle)
    cache.put("route", "current", current)
    with pytest.raises(RuntimeError, match="capacity"):
        cache.put("route", "new", rejected)
    assert cache.get("route", "current") is current
    old.drained = True
    assert cache.put("route", "new", rejected) is rejected
    middle.drained = current.drained = rejected.drained = True
    assert cache.close(1)
    with pytest.raises(RuntimeError, match="closed"):
        cache.put("route", "late", Gateway())


def test_route_cache_close_uses_one_total_budget():
    import time

    budgets = []

    class SlowGateway:
        def close(self, timeout):
            budgets.append(timeout)
            if timeout > 0:
                time.sleep(timeout)
            return False

    cache = application._RouteGatewayCache()
    cache.put("one", "f", SlowGateway())
    cache.put("two", "f", SlowGateway())
    assert not cache.close(0.01)
    assert budgets[0] > 0
    assert budgets[1] == 0


def test_owned_gateway_factory_failure_closes_its_private_log_writer(
    monkeypatch, tmp_path
):
    import pytest

    loggers = []
    original = gateway_owner.Logger.adapter

    def logger(*args, **kwargs):
        result = original(*args, **kwargs)
        loggers.append(result)
        return result

    def fail(*args, **kwargs):
        raise OSError("synthetic SDK construction failure")

    monkeypatch.setattr(gateway_owner.Logger, "adapter", logger)
    monkeypatch.setattr(gateway_owner.LarkSdkGateway, "from_credentials", fail)
    with pytest.raises(OSError, match="construction"):
        application.DaemonApplication._lark_gateway_with_scope_recovery(
            CONFIG,
            "fake",
            tmp_path,
            owned=True,
        )
    loggers[0].info("after-failed-construction")
    assert loggers[0].writer_status().accepted == 0
    assert loggers[0].writer_status().rejected == 1
    assert loggers[0].close(0)
