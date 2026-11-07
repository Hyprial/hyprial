"""Public ``hyprial message send`` resource-manifest regressions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hyprial.shell.impl.cli.commands.common import support as _cli_support
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


def test_send_file_and_image_reach_daemon_as_typed_absolute_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = tmp_path / "proof.txt"
    image = tmp_path / "pixel.png"
    attachment.write_text("proof", encoding="utf-8")
    image.write_bytes(b"png")
    requests: list[tuple[str, dict[str, Any]]] = []

    def daemon_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        requests.append((method, params))
        return {"ok": True, "deliveries": []}

    monkeypatch.setattr(_cli_support, "_daemon_request", daemon_request)

    result = CliRunner().invoke(
        _cli_root_app,
        [
            "message",
            "send",
            "--from",
            "agent:e2e:sender:worker",
            "--to",
            "route:alicesmith-hyprial-e2e:py-test",
            "--file",
            str(attachment),
            "--image",
            str(image),
            "--summary",
            "delivery proof",
            "--json",
            "delivery proof",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True, "deliveries": []}
    assert requests == [
        (
            "message.send",
            {
                "from": "agent:e2e:sender:worker",
                "to": ["route:alicesmith-hyprial-e2e:py-test"],
                "message": "delivery proof",
                "summary": "delivery proof",
                "resourcePaths": [
                    {
                        "path": str(attachment.resolve()),
                        "kind": "file",
                        "mediaType": "text/plain",
                    },
                    {
                        "path": str(image.resolve()),
                        "kind": "image",
                        "mediaType": "image/png",
                    },
                ],
            },
        )
    ]
