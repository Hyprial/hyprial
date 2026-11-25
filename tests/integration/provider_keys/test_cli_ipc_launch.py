"""Scenario 1: CLI -> IPC registry -> owner service -> managed-worker launch composition [security]."""
from __future__ import annotations

from .flows import cli_flow


def test_cli_login_allow_launch_revoke_logout_end_to_end(world, monkeypatch, launch_clock):
    outcome = cli_flow(world, monkeypatch)
    assert outcome["key"]["keyId"].startswith("pk.")
