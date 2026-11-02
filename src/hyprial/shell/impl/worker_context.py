"""Shell composition bindings for worker identity and launch argument assembly.

The shell owns these cross-domain assembly points so CLI command modules do not
import backend helpers directly.  The unique implementation stays in the
identity/daemon domains; this module only exposes the composition-root
bindings.
"""

from __future__ import annotations

from typing import Any


def worker_session_binding() -> Any:
    from hyprial.identity import worker_session_binding as _reader

    return _reader()
