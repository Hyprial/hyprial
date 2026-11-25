"""E2E-013 reads the pinned pi worker's transcript from pi's configured root.

The controlled CI job sets PI_CODING_AGENT_DIR=~/.pi-ci/agent (#841) and a
P1 pi worker inherits it (#828), so pi writes its sessions under that root.
The scenario scanned the hardcoded ~/.pi/agent/sessions instead: the probe
nonce was "never" seen (lark.pinned-agent-received), and the two outbound
checks then failed on a missing route transcript (run 14404, 2026-09-27).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCENARIO = ROOT / "contract" / "e2e" / "run-all-harness-lifecycle.py"


def _scenario():
    sys.path.insert(0, str(SCENARIO.parent))
    spec = importlib.util.spec_from_file_location("e2e013_under_test", SCENARIO)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
        sys.path.remove(str(SCENARIO.parent))
    return module


def test_pi_sessions_root_follows_the_configured_pi_agent_dir(
    tmp_path: Path, monkeypatch
) -> None:
    configured = tmp_path / "pi-ci" / "agent"
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(configured))

    assert _scenario().pi_sessions_root() == configured / "sessions"


def test_pi_sessions_root_defaults_to_the_home_pi_agent_dir(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("PI_CODING_AGENT_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    assert _scenario().pi_sessions_root() == tmp_path / ".pi" / "agent" / "sessions"
