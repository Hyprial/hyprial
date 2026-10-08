"""Read-only process existence observation; no termination side effects."""

import os


def probe_process(pid: int) -> None:
    if os.name == "nt":
        from hyprial.kernel.impl.platform.windows_process  import process_identity

        process_identity(pid)
    else:
        os.kill(pid, 0)
