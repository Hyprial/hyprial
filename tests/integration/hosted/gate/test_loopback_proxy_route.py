"""The loopback gate keeps the job's proxy and bypasses it only for loopback.

Regression for runs 2026-10-30..11-06: the runner blanked every proxy name, so
the real Claude worker's cold ``uv run --with claude-agent-sdk`` resolve had no
route out of the CI container and hung in ``sdk-ready`` until the 60 s start
budget killed it (run 50549). These tests start no harness.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest


RUNNER = Path(__file__).resolve().parents[4] / "contract/e2e/run-real-harness-loopback.py"


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("loopback_proxy_route", RUNNER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec.loader.exec_module(module)
    return module


def test_proxy_names_are_not_overridden(runner: Any) -> None:
    route = runner._loopback_no_proxy({"HTTPS_PROXY": "http://proxy:7896"})
    assert set(route) == {"NO_PROXY", "no_proxy"}


def test_loopback_hosts_lead_and_inherited_entries_survive(runner: Any) -> None:
    route = runner._loopback_no_proxy(
        {"NO_PROXY": "localhost, tailscale,.ts.net,127.0.0.1", "no_proxy": "ignored"}
    )
    assert route["NO_PROXY"] == "127.0.0.1,localhost,::1,tailscale,.ts.net"
    assert route["no_proxy"] == route["NO_PROXY"]


def test_lowercase_no_proxy_is_read_when_uppercase_is_absent(runner: Any) -> None:
    route = runner._loopback_no_proxy({"no_proxy": "forgejo"})
    assert route["NO_PROXY"] == "127.0.0.1,localhost,::1,forgejo"


def test_no_inherited_entries_still_bypasses_loopback(runner: Any) -> None:
    assert runner._loopback_no_proxy({})["NO_PROXY"] == "127.0.0.1,localhost,::1"


def test_main_does_not_blank_proxies() -> None:
    source = RUNNER.read_text()
    assert '{name: "" for name in' not in source
    assert "daemon.env.update(_loopback_no_proxy(daemon.env))" in source
