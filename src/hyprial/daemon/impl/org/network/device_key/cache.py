# Decision record (service grant S1 fix, 2026-11-26):
# - Periodic directory refreshes run every 10 s on the daemon-runtime lane.
#   Caching ``{keyId, publicKey}`` in memory by ``(user_id, device_uid)`` keeps
#   steady-state refreshes off the key store's 15 s file lock, while invalidating
#   automatically whenever the committed account or device uid changes.
# - ``single_read_pass`` scopes one ``publish_self_for_app`` sweep so multi-org
#   publication resolves the local encryption key at most once per pass, even
#   when pre-existing per-org actors rebuild their service per org.
# - The cache lives on the application object (not module-global), so a fresh
#   daemon or test app starts cold and tests stay isolated.
"""In-memory cache and per-pass memo for this device's published encryption key."""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

__all__ = ["EncryptionKeyCache"]


class EncryptionKeyCache:
    """Thread-safe ``(user_id, device_uid)`` cache plus per-pass single-read memo."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subject: tuple[str, str] | None = None
        self._pair: dict[str, str] | None = None
        self._pass_depth = 0
        self._pass_id = 0
        self._pass_has_result = False
        self._pass_result: Any = None

    def get(self, subject_key: tuple[str, str]) -> dict[str, str] | None:
        """Return the cached public pair for ``subject_key``, invalidating on change."""

        with self._lock:
            if self._subject == subject_key and self._pair is not None:
                return dict(self._pair)
            if self._subject != subject_key:
                self._subject = None
                self._pair = None
            return None

    def put(self, subject_key: tuple[str, str], pair: dict[str, str]) -> None:
        """Record the resolved public pair for ``subject_key``."""

        with self._lock:
            self._subject = subject_key
            self._pair = dict(pair)

    def invalidate(self) -> None:
        """Drop any cached pair."""

        with self._lock:
            self._subject = None
            self._pair = None

    @contextmanager
    def single_read_pass(self) -> Iterator[None]:
        """Memoize one reader outcome across all org publishes inside the block."""

        with self._lock:
            if self._pass_depth == 0:
                self._pass_id += 1
                self._pass_has_result = False
                self._pass_result = None
            self._pass_depth += 1
        try:
            yield
        finally:
            with self._lock:
                self._pass_depth -= 1
                if self._pass_depth == 0:
                    self._pass_has_result = False
                    self._pass_result = None

    def read_in_pass(self, loader: Callable[[], Any]) -> Any:
        """Run ``loader`` once per active ``single_read_pass``, or directly outside one."""

        with self._lock:
            in_pass = self._pass_depth > 0
            if in_pass and self._pass_has_result:
                cached = self._pass_result
                return dict(cached) if isinstance(cached, dict) else cached
            pass_id = self._pass_id
        result = loader()
        if in_pass:
            with self._lock:
                if (
                    self._pass_depth > 0
                    and self._pass_id == pass_id
                    and not self._pass_has_result
                ):
                    self._pass_result = (
                        dict(result) if isinstance(result, dict) else result
                    )
                    self._pass_has_result = True
        return result
