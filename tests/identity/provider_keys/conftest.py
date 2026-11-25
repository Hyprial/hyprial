"""Synthetic, temporary inputs only; never use an operator's credential files."""

from pathlib import Path

import pytest

from hyprial.identity import AgentRegistry


@pytest.fixture
def provider_registry(tmp_path: Path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    config = tmp_path / "config" / "native" / "codex"
    config.mkdir(parents=True)
    (config / "config.toml").write_text('cli_auth_credentials_store = "file"\n')
    registry = AgentRegistry(
        tmp_path / "agents.sqlite3", owner="test-owner", machine="test-node",
        hyprial_home=home,
    )
    try:
        registry.create(
            "test-agent", cwd=str(tmp_path / "workspace"),
            config={
                "sources": [{"path": str(tmp_path / "config"), "required": True}],
                "discovery": "explicit-only",
            },
        )
        yield registry
    finally:
        registry.close()
