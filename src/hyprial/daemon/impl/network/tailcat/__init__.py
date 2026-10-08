"""Tailcat sidecar access: binary location/verification and device keys."""

from .binary import (  # noqa: F401
    BINARY_ENV,
    SIDECAR_BINARY_NAME,
    TAILCAT_COMMIT,
    TailcatSidecarError,
    locate_tailcat_sidecar,
    sidecar_binary_name,
    sidecar_binary_path,
    verify_tailcat_sidecar,
    wheel_binary_path,
)
from .keys import (  # noqa: F401
    ensure_device_key,
)
