from __future__ import annotations

from hyprial.shell.impl.cli.commands.service.presentation import (
    render_connect,
    render_disconnect,
    render_guide,
    render_list,
)


def _row(*, state: str = "listening", path: str = "unknown", last_dial_ms: int | None = None) -> dict[str, object]:
    return {
        "name": "api",
        "deviceId": "sub2api",
        "remotePort": 8080,
        "recordGeneration": 7,
        "localPort": 18080,
        "activeConnections": 0,
        "path": path,
        "pathDetail": None,
        "observedAtMs": None,
        "lastDialState": "unknown",
        "lastDialMs": last_dial_ms,
        "lastError": None,
        "lastErrorAtMs": None,
        "state": state,
        "errorCode": None if state == "listening" else "SERVICE_UNAVAILABLE",
    }


def test_connect_render_is_loopback_and_manual_without_secret_injection() -> None:
    text = render_connect(
        {
            "service": _row(path="direct"),
            "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:18080"},
            "auth": "create an application key separately",
            "guide": "Use the loopback endpoint.",
        }
    )

    assert "Connected: api (sub2api:8080) → http://127.0.0.1:18080   [direct]" in text
    assert "Use:  export ANTHROPIC_BASE_URL=http://127.0.0.1:18080" in text
    assert "Guide: hyprial org service guide api" in text
    assert "address" not in text
    assert "target" not in text


def test_connect_render_shell_quotes_environment_values() -> None:
    text = render_connect(
        {
            "service": _row(path="relay"),
            "env": {"SERVICE_URL": "http://127.0.0.1:18080/x y"},
            "auth": "public app login",
            "guide": "Use the service.",
        }
    )

    assert "export SERVICE_URL='http://127.0.0.1:18080/x y'" in text
    assert "[relay]" in text


def test_list_distinguishes_unknown_path_and_tcp_dial_duration_from_rtt() -> None:
    text = render_list(
        {
            "services": [_row(path="unknown", last_dial_ms=12)],
            "registry": {"source": "builtin", "error": None},
        }
    )

    assert "api" in text
    assert "sub2api:8080" in text
    assert "unknown" in text
    assert "12ms" in text
    assert "rtt" not in text.lower()


def test_failed_row_is_not_presented_as_listening_and_registry_warning_is_visible() -> None:
    text = render_list(
        {
            "services": [
                {
                    **_row(state="failed", path="unknown"),
                    "errorCode": "SERVICE_UNAVAILABLE",
                    "lastError": "upstream unavailable",
                }
            ],
            "registry": {"source": "cache", "error": "using cached catalog"},
        }
    )

    assert "failed" in text
    assert "listening" not in text
    assert "using cached catalog" in text
    assert "source: cache" in text


def test_guide_supports_short_and_full_views() -> None:
    data = {
        "source": "orgfs",
        "entry": {
            "name": "db",
            "deviceId": "database",
            "remotePort": 5432,
            "protocol": "tcp",
            "localPort": None,
            "usage": "psql",
            "env": {"DATABASE_URL": "tcp://127.0.0.1:{port}"},
            "auth": "Use the app's own login.",
            "guide": "Connect with the assigned loopback port.",
        },
    }

    short = render_guide(data, full=False)
    full = render_guide(data, full=True)

    assert short.endswith("Connect with the assigned loopback port.")
    assert "untrusted" in short.lower()
    assert "source: orgfs" in full
    assert "deviceId: database" in full
    assert "DATABASE_URL" in full
    assert "Use the app's own login." in full
    assert "untrusted" in full.lower()


def test_disconnect_render_is_literal_and_small() -> None:
    assert render_disconnect({"name": "api", "removed": True}) == "Disconnected: api"
    assert render_disconnect({"name": "api", "removed": False}) == "Not connected: api"
