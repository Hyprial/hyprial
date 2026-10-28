"""Doctor finding for credentials displaced by model-vendor policy repair."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from hyprial.shell.impl.cli.commands.common.support import JsonObject


_FINDING_CODE = "CODEX_CUSTOM_PROVIDER_AUTH_BACKUP"


def codex_credential_backup_doctor_check(hyprial_home: Path) -> JsonObject | None:
    """List private Codex auth backups without reading credential contents."""

    agents = Path(hyprial_home) / "agents"
    try:
        entries = tuple(os.scandir(agents))
    except FileNotFoundError:
        return None
    except OSError:
        return None
    findings: list[JsonObject] = []
    for entry in entries:
        try:
            metadata = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(metadata.st_mode):
            continue
        root = Path(entry.path) / "state" / "credential-backups" / "codex"
        try:
            backups = tuple(os.scandir(root))
        except OSError:
            continue
        for backup in backups:
            try:
                backup_metadata = backup.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(backup_metadata.st_mode):
                continue
            findings.append(
                {
                    "code": _FINDING_CODE,
                    "agent": entry.name,
                    "path": backup.path,
                }
            )
    if not findings:
        return None
    return {
        "name": "codex-credential-backups",
        "status": "warn",
        "detail": (
            f"{len(findings)} inactive Codex auth.json backup(s) were retained "
            "after non-OpenAI credential reconciliation"
        ),
        "metrics": {"findings": findings},
        "action": {
            "command": "hyprial doctor --json",
            "description": (
                "Review each named agent and backup path. After switching an "
                "agent to OpenAI, restore with `hyprial agent provider "
                "login codex --actor <agent> --force --source <path>`; otherwise "
                "retain or archive it under the credential-retention policy."
            ),
        },
    }
