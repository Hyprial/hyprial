import pytest

from hyprial.shell.impl.cli.commands.common.services import get_services as _cli_get_services
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_init_does_not_claim_readiness_when_database_probe_failed(monkeypatch):
    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", lambda **kwargs: {
        "running": True, "storageHealth": {"healthy": False, "checkedAtMs": 123},
    })
    with pytest.raises(_cli_get_services().CliError, match="database health check failed"):
        _cli_get_services()._wait_for_daemon(timeout=1)


def test_legacy_probe_without_health_keeps_compatibility(monkeypatch):
    monkeypatch.setattr(_cli_get_services(), "_daemon_probe", lambda **kwargs: {"running": True})
    assert _cli_get_services()._wait_for_daemon(timeout=1)["running"]
