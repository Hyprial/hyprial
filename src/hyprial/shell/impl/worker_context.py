# Decision record (W1E-2, 2026-11-21):
# - Exposes apply_process_umask on the shell composition seam so CLI process
#   entry applies manysys.apply_private_umask() once at startup without adding
#   direct backend imports under shell/impl/cli.
"""Shell composition bindings for worker identity and launch argument assembly.

The shell owns these cross-domain assembly points so CLI command modules do not
import backend helpers directly.  The unique implementation stays in the
identity/daemon domains; this module only exposes the composition-root
bindings.
"""

from __future__ import annotations

from typing import Any


def apply_process_umask() -> int:
    from hyprial.kernel import apply_private_umask

    return apply_private_umask()


def worker_session_binding() -> Any:
    from hyprial.identity import worker_session_binding as _reader

    return _reader()
