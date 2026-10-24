"""Private runtime-directory materialization."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import TypeVar

from hyprial.kernel import (
    PrivateAccessUnsupported,
    harden_private_path,
    private_metadata_violation,
)


_Error = TypeVar("_Error", bound=ValueError)


def ensure_private_directory(
    root: Path,
    target: Path,
    *,
    error_type: type[_Error],
) -> None:
    """Create one private directory chain without following unsafe metadata."""

    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise error_type("runtime directory escaped the agent home") from error
    current = root
    for part in relative.parts:
        current = current / part
        created = False
        try:
            current.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as error:
            raise error_type(f"cannot create runtime directory {part}") from error
        try:
            if created:
                harden_private_path(current, mode=0o700)
            metadata = current.lstat()
        except PrivateAccessUnsupported as error:
            raise error_type(
                f"runtime directory {part} cannot be made private on this "
                f"platform: {error}"
            ) from error
        except OSError as error:
            raise error_type(f"cannot inspect runtime directory {part}") from error
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise error_type(f"runtime directory {part} is not private")
        try:
            violation = private_metadata_violation(current, metadata, mode=0o700)
        except OSError as error:
            raise error_type(f"cannot inspect runtime directory {part}") from error
        if violation is not None:
            raise error_type(
                f"runtime directory {part} is not private ({violation})"
            )
