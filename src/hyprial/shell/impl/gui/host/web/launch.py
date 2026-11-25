# Decision record (GUI session host, PR 2b):
# - The launcher writes launch.url (the private handoff) and launch.ready (the
#   publication marker); the host only ever removes them: at grant expiry, on
#   grant consumption and at close. It writes no file.
# - With a publication path the grant TTL starts only when launch.ready exists,
#   so a slow launcher cannot lose the grant; the host looks for it on a fixed
#   short interval until it appears. Without one the TTL starts at construction.
# - Only regular files are unlinked, never a symlink and never through one: the
#   launch directory is private but the host does not trust that blindly.
# - All timing goes through the injected Clock, so the five lifecycle cases are
#   tested with a fake clock instead of sleeping.
"""Removal of the private launch handoff and publication marker."""

from __future__ import annotations

import os
import stat

from ..config import HostConfig
from ..session import Clock, Handle, SessionStore

PUBLICATION_POLL_SECONDS = 0.1


class LaunchFiles:
    """Arms the expiry timer for the launch files and removes them on time."""

    def __init__(self, config: HostConfig, store: SessionStore, clock: Clock) -> None:
        self._paths = tuple(p for p in (config.handoff_path, config.publication_path) if p)
        self._store = store
        self._clock = clock
        self._expiry: Handle | None = None
        self._poll: Handle | None = None
        self._closed = False

    def start(self) -> None:
        """Arm the expiry now, or watch for the publication marker until it appears."""
        if not self._activate() and not self._closed:
            self._poll = self._clock.call_later(PUBLICATION_POLL_SECONDS, self._poll_publication)

    def grant_consumed(self) -> None:
        self._stop_poll()
        self.remove()

    def close(self) -> None:
        self._closed = True
        self._stop_poll()
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        self.remove()

    def remove(self) -> None:
        for path in self._paths:
            try:
                if stat.S_ISREG(os.lstat(path).st_mode):
                    os.unlink(path)
            except OSError:
                continue

    def _activate(self) -> bool:
        expires_at = self._store.activate_launch_grant()
        if expires_at is None:
            return False
        if self._paths and self._expiry is None:
            delay = max(0, expires_at - self._clock.now_ms()) / 1000
            self._expiry = self._clock.call_later(delay, self.remove)
        return True

    def _poll_publication(self) -> None:
        self._poll = None
        if self._closed or self._activate():
            return
        self._poll = self._clock.call_later(PUBLICATION_POLL_SECONDS, self._poll_publication)

    def _stop_poll(self) -> None:
        if self._poll is not None:
            self._poll.cancel()
            self._poll = None
