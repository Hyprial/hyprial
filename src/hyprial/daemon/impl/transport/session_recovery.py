"""Small recovery helpers shared by the transport session actor.

Kept out of ``session_actor.py`` so that module stays under the structural
line gate; these are pure functions over the native transport.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

import zenoh

if TYPE_CHECKING:
    from hyprial.daemon.impl.transport.zenoh.config import ZenohConfig

_FALLBACK_REOPEN_LOCK = threading.Lock()


def session_closed(error: BaseException) -> bool:
    text = str(error).lower()
    return "session closed" in text or "session is closed" in text


def reopen_native_once(native, *, failed=None) -> bool:
    """Reopen the native session at its current connect set, once.

    ``failed`` is the session object that raised ``session closed``.  Under
    the native's reopen lock, if the current session is no longer that one,
    another caller already reopened: return True and let the caller retry on
    the new session.  This compare-and-swap prevents N concurrent publishers
    from closing each other's fresh sessions in a reopen storm.
    """

    # Lock order: _reopen_lock -> self._lock; never the reverse.  The open
    # itself (inside reconfigure_connect) runs with self._lock released.
    lock = getattr(native, "_reopen_lock", None) or _FALLBACK_REOPEN_LOCK
    with lock:
        if failed is not None and getattr(native, "_session", None) is not failed:
            return True
        config = getattr(native, "config", None)
        connect = tuple(getattr(config, "connect", ()) or ())
        try:
            native.reconfigure_connect(connect)
        except BaseException:  # noqa: BLE001 - the caller owns the retry policy
            return False
    return True


def session_health(native) -> dict[str, object]:
    health = getattr(native, "session_health", None)
    if health is None:
        return {"state": "unknown"}
    try:
        return dict(health())
    except Exception:  # noqa: BLE001 - status must stay answerable
        return {"state": "unknown"}


class SessionRecoveryMixin:
    """Small recovery operations mixed into the session authority."""

    def session_health(self) -> dict[str, object]:
        return session_health(self._native)

    def _recovery_allowed(self) -> bool:
        health = getattr(self._native, "session_health", None)
        if health is None:
            return True
        try:
            state = health()
        except Exception:  # noqa: BLE001 - status must not block recovery
            return True
        return not (isinstance(state, dict) and state.get("state") != "open")

    def _replay_declarations(self, effect, error):
        for declaration in effect.declarations:
            old = self._native_registrations.get(declaration.token)
            if old is not None:
                try:
                    old.close()
                except BaseException as close_error:
                    if error is None:
                        error = close_error
            try:
                self._native_registrations[declaration.token] = self._declare_native(
                    declaration, effect.generation
                )
            except BaseException as replay_error:
                if error is None:
                    error = replay_error
                else:
                    error.add_note(f"declaration replay failed: {replay_error}")
                break
        return error



class TransportRecoveryMixin:
    """Degraded-session status and the unbounded reopen loop.

    Lock order is fixed everywhere: ``_reopen_lock`` -> ``self._lock`` (the
    session actor's ``_settled`` is the ``self._lock`` layer).  Acquiring
    ``_reopen_lock`` while holding ``self._lock`` is forbidden; every reopen,
    reconfigure, redial, recovery, and settle path follows this order.
    ``zenoh.open``/``close`` never run while holding ``self._lock``; holding
    ``_reopen_lock`` across them is allowed.
    """

    _DEGRADED_RECOVERY_INITIAL_SECONDS = 1.0
    _DEGRADED_RECOVERY_MAX_SECONDS = 30.0

    def _schedule_degraded_recovery(self, config: ZenohConfig) -> None:
        """Reopen a degraded session until it succeeds or the transport closes.

        Both the candidate and fallback opens failed, so the transport has no
        session.  One background worker retries the previous config with
        capped exponential backoff; callers keep getting a clear "degraded"
        error instead of a closed session.
        """

        with self._lock:
            if self._recovery_thread is not None and self._recovery_thread.is_alive():
                return
            thread = threading.Thread(
                target=self._recover_degraded,
                args=(config,),
                name="hyprial-zenoh-session-recovery",
                daemon=True,
            )
            self._recovery_thread = thread
        thread.start()

    def _recover_degraded(self, config: ZenohConfig) -> None:
        attempt = 0
        delay = self._DEGRADED_RECOVERY_INITIAL_SECONDS
        while not self._closed:
            attempt += 1
            with self._lock:
                if self._closed:
                    return
                self._recovery_attempts = attempt
            time.sleep(delay)
            delay = min(self._DEGRADED_RECOVERY_MAX_SECONDS, delay * 2)
            if self._closed:
                return
            with self._reopen_lock:
                # A redial or another reopen may have installed a session (or
                # be rebuilding) while this loop slept. Do not open a second
                # one and do not touch a rebuild gate we do not own.
                with self._lock:
                    if self._closed:
                        return
                    if self._session is not None or self._rebuilding is not None:
                        return
                try:
                    opened = zenoh.open(config.build())
                except Exception as error:  # noqa: BLE001 - retried forever
                    with self._lock:
                        self._recovery_last_error = (
                            f"{type(error).__name__}: {error}"
                        )[:300]
                    continue
                if self._closed:
                    opened.close()
                    return
                self._install_diagnostic_listeners(opened)
                with self._lock:
                    if self._closed or self._session is not None or self._rebuilding is not None:
                        opened.close()
                        return
                    self._session = opened
                    self._config = config
                    self._session_state = "open"
                    self._degraded = False
                    self._recovery_attempts = 0
                    self._recovery_last_error = None
                    self._session_generation += 1
                    try:
                        self._replay()
                    except Exception as error:  # noqa: BLE001 - the session is usable
                        self._emit_diagnostic(
                            "zenoh.session.replay_failed",
                            "zenoh.session.replay_failed detail=%s",
                            str(error)[:300],
                            detail=str(error)[:300],
                            errorType=type(error).__name__,
                        )
            zid = self._session_zid(opened)
            self._emit_diagnostic(
                "zenoh.session.reopened",
                "zenoh.session.reopened zid=%s attempts=%s",
                zid,
                attempt,
                zid=zid,
                attempts=attempt,
            )
            return

    def session_health(self) -> dict[str, object]:
        """Read the session state for status and doctor without touching I/O."""

        with self._lock:
            return {
                "state": self._session_state,
                "degraded": self._degraded,
                "recoveryAttempts": self._recovery_attempts,
                "recoveryLastError": self._recovery_last_error,
                "generation": self._session_generation,
            }


