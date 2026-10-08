from __future__ import annotations

from hyprial.identity.impl.provider_auth.coordinator._base import AuthNoticeIntent
from hyprial.identity.impl.provider_auth.coordinator._coordinator import ProviderAuthCoordinator
import time



class _StopView:
    def __init__(self, daemon_stop, local_stop):
        self._daemon = daemon_stop
        self._local = local_stop

    def is_set(self):
        return self._daemon.is_set() or self._local.is_set()

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                return self.is_set()
            remaining = (
                0.1
                if deadline is None
                else min(0.1, max(0.0, deadline - time.monotonic()))
            )
            self._local.wait(remaining)
        return True
class _EpisodeWriter(ProviderAuthCoordinator):
    def __init__(self, authority, **options):
        self._authority = authority
        super().__init__(**options)

    def _spawn_round(self, episode):
        if self._stop.is_set():
            episode.inflight = False
            return
        self._authority._claim_episode_credit(
            episode.key, episode.episode_id
        )
        successor = bool(
            getattr(self._authority._helper_decision_context, "active", False)
        )
        if not self._authority._start_helper(episode, successor=successor):
            episode.inflight = False
            self._log("provider.auth.helper.overloaded", provider=episode.provider)

    def _new_episode(self, *args, **kwargs):
        episode = super()._new_episode(*args, **kwargs)
        self._authority._claim_episode_credit(
            episode.key, episode.episode_id
        )
        return episode

    def _restore_child_available(self, episode_key, episode_id):
        return self._authority._restore_child_available(
            episode_key, episode_id
        )

    def _notify(self, intent: AuthNoticeIntent) -> bool:
        self._authority._accept_notice(intent)
        return False  # admission is pending, never a delivered receipt
