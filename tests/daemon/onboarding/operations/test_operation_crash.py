"""Real interrupted-writer recovery for durable operation receipts.

Decision: gui-m2-operation-receipts-20261105.  Each child is a self-owned
subprocess using a temporary isolated home.  No production daemon, identity,
credential, browser or another agent's process is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.onboarding.operations import (
    OPERATION_STORE_IN_USE,
    OperationReceiptError,
    OperationService,
    RECOVERY_CONTROLLER_GONE,
    RECOVERY_PROOF_REQUIRED,
    RecoveryAuthority,
)
from hyprial.daemon.impl.request_catalog.onboarding import (
    AUTH_OPERATION_CONFLICT,
    CallContext,
    OnboardingStateError,
    Surface,
)

OWNER = "user:alice@example.test"
KEY = "opkey-0123456789abcdef"
OP = "op-0123456789abcdef"
RCPT = "rcpt-0123456789abcdef"

_CHILD = """
import os
import sys
from pathlib import Path
from hyprial.daemon.impl.onboarding.operations import OperationService
from hyprial.daemon.impl.request_catalog.onboarding import CallContext, Surface

root = Path(sys.argv[1])
mode = sys.argv[2]
context = CallContext(
    surface=Surface("home-1", "host-1", "view-1", "gen-1"),
    phase="bootstrap",
    caller="local-bootstrap",
    owner=None if mode == "pending" else "user:alice@example.test",
)
service = OperationService.open(root)
if mode == "pending":
    service.begin_auth(context, "login", sys.argv[3], sys.argv[4])
else:
    service.accept_initialize(
        context, sys.argv[3], sys.argv[4], sys.argv[5],
        promotion=lambda record: None,
    )
print(service.get(context, sys.argv[4]).state, flush=True)
os._exit(0)
"""

_LIVE_CHILD = """
import sys
from pathlib import Path
from hyprial.daemon.impl.onboarding.operations import (
    OPERATION_STORE_IN_USE,
    OperationReceiptError,
    OperationService,
)

try:
    OperationService.open(Path(sys.argv[1]))
except OperationReceiptError as error:
    print(error.code, flush=True)
    sys.exit(0 if error.code == OPERATION_STORE_IN_USE else 2)
print("opened", flush=True)
sys.exit(3)
"""


def context(
    owner: str | None = OWNER, generation: str = "gen-1"
) -> CallContext:
    return CallContext(
        surface=Surface("home-1", "host-1", "view-1", generation),
        phase="bootstrap",
        caller="local-bootstrap",
        owner=owner,
    )


def child_environment(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    data = tmp_path / "data"
    for path in (home / ".hyprial", state, config, cache, data):
        path.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "HYPRIAL_HOME": str(home / ".hyprial"),
        "XDG_STATE_HOME": str(state),
        "XDG_CONFIG_HOME": str(config),
        "XDG_CACHE_HOME": str(cache),
        "XDG_DATA_HOME": str(data),
    }


def repository_root() -> Path:
    return Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("mode", ["pending", "accepted"])
def test_crashed_writer_reopen_is_unknown_and_not_replayed(
    tmp_path, mode
) -> None:
    root = tmp_path / "operations"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CHILD,
            str(root),
            mode,
            KEY,
            OP,
            RCPT,
        ],
        cwd=repository_root(),
        env=child_environment(tmp_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert completed.stdout.strip() == ("pending" if mode == "pending" else "promoting")

    service = OperationService.open(root)
    owner = None if mode == "pending" else OWNER
    record = service.get(context(owner=owner), OP)
    assert record.state == "unknown"
    persisted = json.loads(root.joinpath("operations.json").read_text())
    stored = persisted["records"][0]
    assert stored["state"] == "unknown"
    with pytest.raises(OperationReceiptError) as missing_proof:
        service.migrate_generation(
            context(owner=owner, generation="gen-2"), OP, None
        )
    assert missing_proof.value.code == RECOVERY_PROOF_REQUIRED
    forged = RecoveryAuthority(
        "home-1",
        "host-1",
        "view-1",
        owner,
        stored["controllerPid"],
        stored["controllerBirth"],
    )
    with pytest.raises(OperationReceiptError) as dead_controller:
        service.migrate_generation(
            context(owner=owner, generation="gen-2"), OP, forged
        )
    assert dead_controller.value.code == RECOVERY_CONTROLLER_GONE

    replay: list[object] = []
    if mode == "pending":
        with pytest.raises(OnboardingStateError) as error:
            service.mark_authenticated(context(owner=None), OP, OWNER)
        assert error.value.code == AUTH_OPERATION_CONFLICT
        cancelled = service.cancel(context(owner=None), OP)
        assert cancelled.cancelled is False
        assert cancelled.state == "unknown"
    else:
        again = service.accept_initialize(
            context(), KEY, OP, RCPT, promotion=replay.append
        )
        assert again.state == "unknown"
        assert replay == []


def test_live_writer_is_not_taken_over(tmp_path) -> None:
    root = tmp_path / "operations"
    service = OperationService.open(root)
    service.begin_auth(context(), "login", KEY, OP)
    completed = subprocess.run(
        [sys.executable, "-c", _LIVE_CHILD, str(root)],
        cwd=repository_root(),
        env=child_environment(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == OPERATION_STORE_IN_USE
    assert service.get(context(), OP).state == "pending"
