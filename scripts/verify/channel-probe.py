"""Run the opt-in synthetic worker channel VM test. Never a model acceptance.

Run this through contract/e2e/with-machine-lock.py after obtaining
the monitor resource window. All dependencies are explicit local paths.
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

parser = argparse.ArgumentParser(description=__doc__)
for name in ("smolvm", "rootfs", "resize2fs", "client", "output"):
    parser.add_argument("--" + name, required=True, type=Path)
args = parser.parse_args()
env = dict(os.environ)
for name, value in vars(args).items():
    env["AT08_" + name.upper()] = str(value.absolute())
root = Path(__file__).resolve().parents[2]
result = subprocess.run(
    [sys.executable, "-m", "pytest", "-q", "tests/daemon/workers/test_worker_channel_smolvm.py"],
    cwd=root,
    env=env,
)
raise SystemExit(result.returncode)
