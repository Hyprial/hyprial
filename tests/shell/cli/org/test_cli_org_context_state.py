"""``org show`` / ``org status`` when the accepted slot has no adoption record.

The accepted body and its audit record are separate facts.  A parseable body
without ``org/adoptions/<sha256>.json`` must still be shown, with a warning,
and must never be presented as adopted (no invented ``adoptedAt``).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.org.document import parse_document, serialize_document

ROOT = Path(__file__).resolve().parents[4]

DOCUMENT = """# Harness Bridge organization

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
  autonomy: [任务域内自治]
  escalation: 超域必须升级
  etiquette: 先引荐再委派
residents: []
```
"""


def _environment(home: Path) -> dict[str, str]:
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    return {
        **os.environ,
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "NO_COLOR": "1",
        "TERM": "dumb",
    }


def _run(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "org", "context", *args, "--json"],
        cwd=ROOT,
        env=_environment(home),
        text=True,
        capture_output=True,
        check=False,
    )


def _run_human(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "org", "context", *args],
        cwd=ROOT,
        env=_environment(home),
        text=True,
        capture_output=True,
        check=False,
    )


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.stdout, result.stderr
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _accepted_without_record(home: Path) -> None:
    """Write the accepted body exactly as adoption would, minus its record."""

    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "org-context.md").write_text(
        serialize_document(parse_document(DOCUMENT)), encoding="utf-8"
    )


def test_org_show_falls_back_when_the_adoption_record_is_missing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _accepted_without_record(home)

    shown = _payload(_run(home, "show"))

    assert shown["ok"] is True
    assert shown["status"] == "accepted"
    assert shown["meta"]["version"] == 1
    assert shown["meta"]["publisher"] == "alicesmith"
    assert "adoptedAt" not in shown
    warnings = shown["warnings"]
    assert warnings and warnings[0]["code"]
    assert "adoption" in warnings[0]["message"]


def test_org_show_full_falls_back_when_the_adoption_record_is_missing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _accepted_without_record(home)

    shown = _payload(_run(home, "show", "--full"))

    assert shown["ok"] is True
    assert shown["documentSha256"]
    assert shown["document"]["lines"][0]["line"] == "hyprial"
    assert "adoptedAt" not in shown
    assert shown["warnings"][0]["code"]


def test_org_show_warns_on_stderr_when_the_adoption_record_is_missing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _accepted_without_record(home)

    result = _run_human(home, "show")

    assert result.returncode == 0, result.stderr
    assert "adoption" in result.stderr.lower()
    assert "showing the parsed body only" not in result.stdout


@pytest.mark.parametrize("args", [("show",), ("show", "--full"), ("status",)])
def test_org_human_warnings_stay_off_stdout(
    tmp_path: Path, args: tuple[str, ...]
) -> None:
    """A person reads the warning on stderr; stdout carries the result only.

    ``--json`` keeps the structured ``warnings`` field, but the human stdout
    must not repeat the same warning the ``warn`` channel already wrote.
    """

    home = tmp_path / "home"
    _accepted_without_record(home)

    result = _run_human(home, *args)

    assert result.returncode == 0, result.stderr
    assert "adoption" in result.stderr.lower()
    assert "ORG_ACCEPTANCE_METADATA_UNAVAILABLE" not in result.stdout
    assert "showing the parsed body only" not in result.stdout
    assert "warnings" not in result.stdout


def test_org_status_falls_back_when_the_adoption_record_is_missing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _accepted_without_record(home)

    status = _payload(_run(home, "status"))

    assert status["ok"] is True
    assert status["slot"] == "accepted"
    assert "accepted" not in status
    assert status["meta"]["version"] == 1
    assert status["meta"]["publisher"] == "alicesmith"
    assert "adoptedAt" not in status["meta"]
    warnings = status["warnings"]
    assert warnings and "adoption" in warnings[0]["message"]
