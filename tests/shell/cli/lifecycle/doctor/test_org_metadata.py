"""Doctor visibility for malformed organization lifecycle metadata."""

from __future__ import annotations

from typing import Any

import pytest

import hyprial.shell.impl.cli.commands.doctor.checks.org_metadata as org_metadata_module
from hyprial.shell.impl.cli.commands.common import support as _cli_support


def test_org_metadata_data_problems_warn_with_reported_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    problem = {
        "org": "group-acme",
        "path": "directory/org.json",
        "reason": "unknown-state",
        "value": "paused",
    }

    def request(method: str, *_args: Any, **_kwargs: Any) -> dict[str, object]:
        assert method == "org.network"
        return {"dataProblems": [problem]}

    monkeypatch.setattr(_cli_support, "_daemon_request", request)

    check = org_metadata_module.org_metadata_doctor_check()

    assert check == {
        "name": "org-metadata",
        "status": "warn",
        "detail": "1 organization data problem(s)",
        "action": {
            "command": "hyprial org network --json",
            "description": "Inspect dataProblems and repair the reported organization data.",
        },
        "metrics": {"dataProblems": [problem]},
    }
