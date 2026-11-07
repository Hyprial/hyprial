"""Shared scaffolding for the ``hyprial doctor`` test modules.

The doctor report is a list of checks assembled from several daemon probes, so
most tests here need the same three things: a minimal ``ps`` payload, a request
port that answers ``agent.list``/``adapter.list`` before the payload, and a
name-indexed view of the resulting checks. They live here because
``test_cli_doctor.py`` sits at the module line budget.

Nothing in this module asserts; it only builds the seams a test patches.
"""

from __future__ import annotations

from typing import Any

from hyprial.shell.impl.cli.commands.common import support as _cli_support
import hyprial.shell.impl.cli.commands.doctor.commands as _cli_owner_1


def running_ps(zenoh: dict[str, object] | None) -> dict[str, object]:
    return {"daemon": {"running": True}, "zenoh": zenoh}


def doctor(
    monkeypatch: Any, ps_result: dict[str, object], *, delivery_paths_ok: bool = True,
) -> dict[str, object]:
    def request(method: str, *_args: Any, **_kwargs: Any) -> object:
        if method == "agent.list":
            return {"agents": []}
        if method == "adapter.list":
            return {"adapters": []}
        return ps_result

    monkeypatch.setattr(_cli_support, "_daemon_request", request)
    if delivery_paths_ok:
        # The delivery-path check reads a probe file that test homes never
        # have; stub it green so unrelated doctor tests keep their verdicts.
        from hyprial.shell.impl.cli.commands.doctor.checks import delivery_paths

        monkeypatch.setattr(
            delivery_paths,
            "delivery_paths_doctor_check",
            lambda: {"name": "delivery-paths", "status": "ok", "detail": "test probe"},
        )
    result = _cli_owner_1._doctor_result()
    assert isinstance(result, dict)
    return result


def checks(result: dict[str, object]) -> dict[str, dict[str, object]]:
    items = result["checks"]
    assert isinstance(items, list)
    return {item["name"]: item for item in items if isinstance(item, dict)}
