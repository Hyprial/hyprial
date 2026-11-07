"""``hyprial org context validate`` — the manual pre-flight for hand edits."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.org.document import parse_document
from hyprial.daemon.impl.org.store import OrgContextStore

ROOT = Path(__file__).resolve().parents[4]
CORRUPT_ESCAPED = (
    ROOT
    / "tests/daemon/organizations/document/fixtures/org-context-corrupt-escaped-brace.md"
)

VALID = """# Org context

Body.

```yaml
meta:
  version: 3
  issued_at: 2026-10-16
  publisher: alice
lines:
  - {line: alpha, owner: bob}
members: []
routing: []
norms:
  autonomy: [x]
  escalation: y
  etiquette: z
residents: []
```
"""

#: The same legal document with every newline spelling CRLF.  The format
#: pre-flight reads newline-normalized text, so this must stay valid.
CRLF_VALID = VALID.replace("\n", "\r\n")


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


def _payload(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert result.stdout, result.stderr
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return value


def _adopt(home: Path) -> OrgContextStore:
    """Write the accepted slot exactly as adoption does, with its audit record."""

    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    store = OrgContextStore(home)
    # Adopt unconditionally: a previous accepted file (even a corrupt one)
    # must not block the fixture from establishing a clean accepted slot.
    store.accepted_path.unlink(missing_ok=True)
    store.adopt(parse_document(VALID))
    return store


def _adoption_path(store: OrgContextStore) -> Path:
    digest = hashlib.sha256(VALID.encode("utf-8")).hexdigest()
    return store.adoptions_dir / f"{digest}.json"


def test_validate_reports_a_corrupt_file_with_line_and_column(tmp_path: Path) -> None:
    result = _run(tmp_path / "home", "validate", "--path", str(CORRUPT_ESCAPED))

    assert result.returncode == 1, result.stderr
    body = _payload(result)
    assert body["ok"] is False
    assert body["valid"] is False
    assert body["line"] == 20
    assert body["column"] == 33
    assert "mapping values are not allowed here" in body["reason"]


def test_validate_accepts_a_well_formed_file(tmp_path: Path) -> None:
    valid = tmp_path / "valid.md"
    valid.write_text(VALID, encoding="utf-8")

    result = _run(tmp_path / "home", "validate", "--path", str(valid))

    assert result.returncode == 0, result.stderr
    body = _payload(result)
    assert body["ok"] is True
    assert body["valid"] is True
    assert body["meta"]["version"] == 3


def test_validate_path_accepts_a_fully_crlf_file(tmp_path: Path) -> None:
    """A legal file whose newlines are all CRLF still passes the pre-flight.

    The audit digest is over the raw bytes, but the format check runs on the
    newline-normalized text ``read_text`` used to produce; CRLF is Markdown,
    not a fence violation.
    """

    crlf = tmp_path / "crlf.md"
    crlf.write_bytes(CRLF_VALID.encode("utf-8"))

    result = _run(tmp_path / "home", "validate", "--path", str(crlf))

    assert result.returncode == 0, result.stderr
    body = _payload(result)
    assert body["ok"] is True
    assert body["valid"] is True
    assert body["meta"]["version"] == 3


def test_validate_accepts_a_fully_crlf_accepted_slot_with_its_record(
    tmp_path: Path,
) -> None:
    """Default-slot positive: CRLF accepted bytes backed by their own digest.

    The adoption record names the sha256 of the raw CRLF bytes on disk, and
    the default slot must accept the pair instead of failing on the fences.
    """

    home = tmp_path / "home"
    store = OrgContextStore(home)
    store.ensure_layout()
    raw = CRLF_VALID.encode("utf-8")
    store.accepted_path.write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    (store.adoptions_dir / f"{digest}.json").write_text(
        json.dumps(
            {
                "version": 3,
                "publisher": "alice",
                "adoptedAt": "2026-10-16T00:00:00Z",
                "documentSha256": digest,
            }
        ),
        encoding="utf-8",
    )

    result = _run(home, "validate")

    assert result.returncode == 0, result.stderr
    body = _payload(result)
    assert body["ok"] is True
    assert body["valid"] is True
    assert body["meta"]["version"] == 3
    assert hashlib.sha256(store.accepted_path.read_bytes()).hexdigest() == digest


def test_validate_path_never_checks_adoption_state(tmp_path: Path) -> None:
    """``--path`` is a format pre-flight even when no adoption record exists."""

    valid = tmp_path / "valid.md"
    valid.write_text(VALID, encoding="utf-8")

    result = _run(tmp_path / "home", "validate", "--path", str(valid))

    assert result.returncode == 0, result.stderr
    assert _payload(result)["valid"] is True


def test_validate_accepts_the_accepted_slot_with_its_adoption_record(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _adopt(home)

    result = _run(home, "validate")

    assert result.returncode == 0, result.stderr
    body = _payload(result)
    assert body["ok"] is True
    assert body["valid"] is True


def test_validate_defaults_to_the_accepted_slot(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    (home / "org-context.md").write_text(
        CORRUPT_ESCAPED.read_text("utf-8"), encoding="utf-8"
    )

    corrupt = _run(home, "validate")
    assert corrupt.returncode == 1, corrupt.stderr
    assert _payload(corrupt)["line"] == 20

    (home / "org-context.md").write_text(VALID, encoding="utf-8")
    _adopt(home)
    valid = _run(home, "validate")
    assert valid.returncode == 0, valid.stderr
    assert _payload(valid)["valid"] is True


def test_validate_rejects_the_accepted_slot_without_an_adoption_record(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    store = _adopt(home)
    _adoption_path(store).unlink()

    result = _run(home, "validate")

    assert result.returncode == 1, result.stderr
    body = _payload(result)
    assert body["ok"] is False
    assert body["valid"] is True
    assert "adoption record" in body["reason"]


def test_validate_rejects_a_hand_edited_body_without_a_new_record(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    _adopt(home)
    (home / "org-context.md").write_text(
        VALID.replace("version: 3", "version: 4"), encoding="utf-8"
    )

    result = _run(home, "validate")

    assert result.returncode == 1, result.stderr
    body = _payload(result)
    assert body["ok"] is False
    assert body["valid"] is True
    assert "adoption record" in body["reason"]


@pytest.mark.parametrize(
    ("field", "wrong"),
    [
        ("version", 999),
        ("publisher", "mallory"),
        ("documentSha256", "0" * 64),
        ("adoptedAt", "not-a-timestamp"),
    ],
)
def test_validate_rejects_an_inconsistent_adoption_record(
    tmp_path: Path, field: str, wrong: object
) -> None:
    home = tmp_path / "home"
    store = _adopt(home)
    path = _adoption_path(store)
    record = json.loads(path.read_text(encoding="utf-8"))
    record[field] = wrong
    path.write_text(json.dumps(record), encoding="utf-8")

    result = _run(home, "validate")

    assert result.returncode == 1, result.stderr
    body = _payload(result)
    assert body["ok"] is False
    assert body["valid"] is True
    assert body["reason"]


def test_validate_rejects_an_adoption_record_that_is_not_json(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = _adopt(home)
    _adoption_path(store).write_text("{not json", encoding="utf-8")

    result = _run(home, "validate")

    assert result.returncode == 1, result.stderr
    body = _payload(result)
    assert body["ok"] is False
    assert body["valid"] is True
    assert "JSON" in body["reason"]


def test_validate_rejects_publish_pending_for_another_document(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    store = _adopt(home)
    store.queue_orgfs_publish("0" * 64)

    result = _run(home, "validate")

    assert result.returncode == 1, result.stderr
    body = _payload(result)
    assert body["ok"] is False
    assert body["valid"] is True
    assert "publish pending" in body["reason"]


def test_validate_rejects_publish_pending_that_is_not_json(tmp_path: Path) -> None:
    home = tmp_path / "home"
    store = _adopt(home)
    store.ensure_layout()
    store.publish_pending_path.write_text("{", encoding="utf-8")

    result = _run(home, "validate")

    assert result.returncode == 1, result.stderr
    body = _payload(result)
    assert body["ok"] is False
    assert body["valid"] is True
    assert "publish pending" in body["reason"]


def test_validate_reports_an_absent_default_slot(tmp_path: Path) -> None:
    result = _run(tmp_path / "home", "validate")

    assert result.returncode == 1, result.stderr
    body = _payload(result)
    assert body["ok"] is False
    assert body["code"] == "ORG_CONTEXT_ABSENT"


def test_validate_reports_an_invalid_yaml_date_with_location(tmp_path: Path) -> None:
    bad = tmp_path / "bad-date.md"
    bad.write_text(VALID.replace("2026-10-16", "2026-99-99"), encoding="utf-8")

    result = _run(tmp_path / "home", "validate", "--path", str(bad))

    assert result.returncode == 1, result.stderr
    body = _payload(result)
    assert body["ok"] is False
    assert body["valid"] is False
    assert body["line"] == 8
    assert isinstance(body["column"], int)
    assert body["reason"]


def test_validate_help_names_the_path_preflight_as_format_only(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "hyprial.cli", "org", "context", "validate", "--help"],
        cwd=ROOT,
        env=_environment(tmp_path / "home"),
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--path" in result.stdout
    assert "format" in result.stdout.lower()
    assert "adoption" in result.stdout.lower()
