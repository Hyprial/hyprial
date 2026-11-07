"""C3-S2-C: a landed payload's nested directories must be private.

``AgentHomeProvisioner.materialise_payload`` writes a bundle's files into an
agent home.  The home's topology roots (``state``, ``config``, ``secrets``,
``workspace``) are provisioned 0700, but a nested path below one of them (a
native credential at ``secrets/native/claude/.credentials.json``, a session
file under ``state/sessions/...``) used to come out with the umbrella default:
``Path.mkdir(mode=..., parents=True)`` applies ``mode`` to the leaf only.  The
P2 runtime owner validates every level from the home to each resolved root at
0700, so a landed claude home was refused as ``runtime directory native is not
private`` before any launch (found live in the C3-S2-C two-daemon run,
``docs/notes/c3-claude-run.txt``).

This pins the fix on the owner that does the writing: the source build under
test has the branch, this asserts the destination modes.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from uuid import uuid4

from hyprial.identity import snapshot_home_payload
from hyprial.identity.impl.agents.home.provisioner import AgentHomeProvisioner


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def test_a_landed_native_credential_has_private_parents(tmp_path: Path) -> None:
    provisioner = AgentHomeProvisioner(tmp_path / "hyprial-home")
    attempt = provisioner.provision(
        actor="worker", entity_token=uuid4().hex, incumbent=None
    )

    # Reproduce the shape a real bundle has: the file is 0600, but the staged
    # parents are created by the extractor at the umbrella default, exactly as
    # ``receive_bundle`` leaves them.
    staged = tmp_path / "staged"
    credential = staged / "secrets" / "native" / "claude" / ".credentials.json"
    credential.parent.mkdir(parents=True)
    credential.write_text('{"placeholder": true}')
    os.chmod(credential, 0o600)

    provisioner.materialise_payload(
        attempt.receipt, staged, snapshot_home_payload(staged)
    )

    home = Path(attempt.receipt.path)
    for relative in ("secrets", "secrets/native", "secrets/native/claude"):
        assert _mode(home / relative) == 0o700, relative
    assert _mode(home / "secrets/native/claude/.credentials.json") == 0o600
