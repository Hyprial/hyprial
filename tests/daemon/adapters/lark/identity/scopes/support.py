from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from hyprial.daemon.impl.adapters.lark.credentials.scopes import (
    LarkScopeThrottleStore,
)
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)
def _claim_throttle_once(path: Path, start: object, output: object) -> None:
    store = LarkScopeThrottleStore(path, now=lambda: 1_000.0)
    start.wait()  # type: ignore[attr-defined]
    output.put(  # type: ignore[attr-defined]
        store.claim("same-app", ("im:message",), 3600)
    )


def _claim_throttle_after_corruption(
    path: Path, start: object, output: object
) -> None:
    start.wait()  # type: ignore[attr-defined]
    try:
        value = LarkScopeThrottleStore(path, now=lambda: 1000.0).claim(
            "app", ("im:message",), 3600
        )
        output.put(("ok", value))  # type: ignore[attr-defined]
    except Exception as error:  # noqa: BLE001 - report child failure to parent
        output.put(("error", type(error).__name__))  # type: ignore[attr-defined]























































def _configured_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / "secrets").mkdir(parents=True)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": "feishu-main",
                        "appId": "cli_test_app",
                        "credentialRef": "lark-feishu-main",
                        "routes": [
                            {"name": "dm", "type": "direct", "nativeId": "oc_x"}
                        ],
                        "defaultRoute": "dm",
                    }
                ],
            }
        )
    )
    (home / "secrets" / "lark-feishu-main.json").write_text(
        json.dumps({"appSecret": "must-not-leak"})
    )
    return home














def _interactive_authorize(
    tmp_path,  # type: ignore[no-untyped-def]
    monkeypatch,  # type: ignore[no-untyped-def]
    *arguments: str,
    result: dict[str, object] | None = None,
):
    home = _configured_home(tmp_path)
    calls: list[dict[str, object]] = []

    def fake_register_app(on_qr_code, **options):  # type: ignore[no-untyped-def]
        calls.append(options)
        on_qr_code({"url": "https://accounts.example.invalid/x?d=1", "expire_in": 600})
        return result or {
            "client_id": "cli_test_app",
            "client_secret": "must-not-leak-either",
        }

    monkeypatch.setenv("HYPRIAL_HOME", str(home))
    monkeypatch.setenv("HARNESS_STATE_DIR", str(home))
    monkeypatch.setattr("lark_oapi.register_app", fake_register_app)
    invocation = CliRunner().invoke(
        _cli_root_app,
        [
            "adapter",
            "scopes",
            "authorize",
            "feishu-main",
            "--interactive",
            *arguments,
            "--json",
        ],
    )
    return invocation, calls


















# Shared builders/data and fixture registrations used by the focused modules.
__all__ = [
    '_claim_throttle_after_corruption',
    '_claim_throttle_once',
    '_configured_home',
    '_interactive_authorize',
]
