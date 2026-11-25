#!/bin/sh
# E2E-001 container entrypoint: persist role state, then run the daemon as
# PID 1 (so `docker stop` SIGTERMs the daemon for a graceful close).
#
# The launcher supplies the private Unix backend, pinned Tailcat binary and
# private relay on every boot. Device keys are created only in this node's
# disposable home and adopted unchanged on its normal container restart.
# The fixture's OS machine-id is unique per container and persists through
# stop/start; product copied-home protection must never be disabled for tests.
set -eu

mkdir -p "${HYPRIAL_HOME}" "${HARNESS_STATE_DIR}"

# The mailbox role IS still desired state: a node that only takes the role
# from its environment loses it on a restart, and a mailbox that quietly
# stops being one strands custody.
/app/.venv/bin/python - <<'PY'
import os
from pathlib import Path

from e2e_machine_identity import ensure_machine_id
from hyprial.daemon import DesiredStateStore, ensure_device_key

ensure_machine_id(Path("/etc/machine-id"))
home = Path(os.environ["HYPRIAL_HOME"])
(home / "net").mkdir(mode=0o700, parents=True, exist_ok=True)
ensure_device_key(
    home, owner=os.environ["HYPRIAL_OWNER"],
    device_id=os.environ["HYPRIAL_NODE_ID"], environ=os.environ,
)

state_dir = Path(os.environ["HARNESS_STATE_DIR"])
store = DesiredStateStore(state_dir / "desired-state.json")
store.set_mailbox_role(os.environ.get("HYPRIAL_AS_MAILBOX", "") not in ("", "0"))
PY

exec hyprial daemon run
