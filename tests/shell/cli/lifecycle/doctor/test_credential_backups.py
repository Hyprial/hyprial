from __future__ import annotations

from pathlib import Path

from hyprial.shell.impl.cli.commands.doctor.credential_backups import (
    codex_credential_backup_doctor_check,
)


def test_doctor_finding_names_agent_and_codex_auth_backup(tmp_path: Path) -> None:
    backup = (
        tmp_path
        / "agents"
        / "worker"
        / "state"
        / "credential-backups"
        / "codex"
        / "auth.json"
    )
    backup.parent.mkdir(parents=True)
    backup.write_bytes(b"test-only")
    backup.chmod(0o600)

    finding = codex_credential_backup_doctor_check(tmp_path)

    assert finding is not None
    assert finding["status"] == "warn"
    assert finding["metrics"] == {
        "findings": [
            {
                "code": "CODEX_CUSTOM_PROVIDER_AUTH_BACKUP",
                "agent": "worker",
                "path": str(backup),
            }
        ]
    }
