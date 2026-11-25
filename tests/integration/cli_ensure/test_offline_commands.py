"""Commands that rely on "no daemon answers" keep doing so with autostart enabled (Z-4).

Decision: daemon-first-z4-offline-commands-20261117.  Real CLI on an isolated
owned home with ``HYPRIAL_DAEMON_AUTOSTART`` removed from the child
environment: each command below must not start a daemon, and its result is the
one the same command gives without this change (captured on the base commit
with autostart off).
"""

from __future__ import annotations

import json

import pytest

from .conftest import pytestmark  # noqa: F401


@pytest.fixture
def owned(lifecycle):
    (lifecycle.home / "settings.json").write_text(
        json.dumps({"version": 1, "owner": "z4-owner@example.test"}), encoding="utf-8"
    )
    lifecycle.env.pop("HYPRIAL_DAEMON_AUTOSTART", None)
    return lifecycle


def _assert_no_daemon(home) -> None:
    assert home.recorded_pids() == set()
    assert not (home.state / "daemon.sock").exists()


def test_adapter_remove_removes_offline(owned):
    result = owned.cli("adapter", "remove", "nosuch", "--json")

    assert result.returncode == 1 and result.stderr == ""
    assert json.loads(result.stdout) == {
        "ok": False,
        "code": "ADAPTER_NOT_FOUND",
        "error": "adapter 'nosuch' is not configured; configured adapters: (none)",
    }
    _assert_no_daemon(owned)


def test_retired_onboarding_plan_changes_no_state(owned):
    result = owned.cli("init", "onboarding", "plan", "--json")

    assert result.returncode == 2
    assert json.loads(result.stdout) == {
        "ok": False,
        "code": "INVALID_ARGUMENT",
        "error": "No such command 'onboarding'.",
    }
    _assert_no_daemon(owned)


def test_agent_tiers_reports_without_a_daemon(owned):
    result = owned.cli("agent", "tiers", "--json")

    assert result.returncode == 0 and result.stderr == ""
    assert json.loads(result.stdout)["ok"] is True
    _assert_no_daemon(owned)


ORG_CONTEXT = """# Harness Bridge organization

The owner-adopted view.

```yaml
meta:
  version: 1
  issued_at: 2026-08-16
  publisher: alicesmith
lines:
  - {line: hyprial, owner: gregpaws}
members: []
routing: []
norms:
  autonomy: [task-local autonomy]
  escalation: escalate beyond the task domain
  etiquette: introduce before delegating
residents: []
```
"""


def test_org_context_import_adopts_and_its_best_effort_publish_starts_nothing(owned, tmp_path):
    from hyprial.daemon import parse_document, serialize_document

    incoming = tmp_path / "incoming.md"
    incoming.write_text(serialize_document(parse_document(ORG_CONTEXT)), encoding="utf-8")

    result = owned.cli("org", "context", "import", str(incoming), "--force", "--json")

    assert result.returncode == 0, (result.stdout, result.stderr[-1000:])
    payload = json.loads(result.stdout)
    assert payload["ok"] is True and payload["adopted"] is True
    _assert_no_daemon(owned)
