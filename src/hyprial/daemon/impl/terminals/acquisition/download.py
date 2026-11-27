"""Verified, bounded acquisition of pinned hc archives."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
from pathlib import Path
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request

from hyprial.daemon.impl.network.proxy_route import url_opener
from hyprial.kernel import secure_private_directory
from hyprial.kernel import user_agent
from hyprial.kernel import with_user_agent

from .lock import HcAcquisitionError
from .lock import HcTargetSpec


OpenUrl = Callable[..., object]
Sleep = Callable[[float], None]


def download_hc_archive(
    target: HcTargetSpec,
    destination: Path,
    *,
    opener: OpenUrl | None = None,
    sleep: Sleep = time.sleep,
    attempts: int = 3,
    timeout: float = 30.0,
) -> Path:
    """Download one archive and verify its exact size before its digest."""
    if attempts < 1:
        raise ValueError("download attempts must be positive")
    destination = Path(destination)
    secure_private_directory(destination.parent, create=True)
    open_url = opener or url_opener()[0]
    request = Request(target.url, headers=with_user_agent({}, user_agent("daemon")))
    last_error: Exception | None = None
    for attempt in range(attempts):
        temporary = destination.parent / f".download-{uuid.uuid4().hex}"
        try:
            with open_url(request, timeout=timeout) as response:
                actual_size = 0
                digest = hashlib.sha256()
                with temporary.open("wb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        actual_size += len(chunk)
                        if actual_size > target.size:
                            raise HcAcquisitionError(
                                f"hc archive exceeds expected size {target.size}"
                            )
                        output.write(chunk)
                        digest.update(chunk)
            if actual_size != target.size:
                raise HcAcquisitionError(
                    f"hc archive size mismatch: expected {target.size}, got {actual_size}"
                )
            actual_digest = digest.hexdigest()
            if actual_digest != target.sha256:
                raise HcAcquisitionError("hc archive sha256 mismatch")
            temporary.replace(destination)
            return destination
        except HcAcquisitionError as error:
            last_error = error
        except HTTPError as error:
            if error.code < 500:
                raise HcAcquisitionError(f"hc archive request failed with HTTP {error.code}") from error
            last_error = error
        except (OSError, TimeoutError, URLError) as error:
            last_error = error
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
        if attempt + 1 < attempts:
            sleep(0.5 * (2**attempt))
    raise HcAcquisitionError("unable to download pinned hc archive") from last_error


__all__ = ["download_hc_archive"]
