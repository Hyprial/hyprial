"""Passive cache of the existing PID/birth sampler, never a process policy."""
from collections import OrderedDict
from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True, slots=True)
class ProcessObservation:
    supported: bool = False
    exit_verified: bool = False


class SessionProcessObservations:
    def __init__(self, identity, read_session, generation, *, capacity=4096):
        self._identity = identity
        self._read_session = read_session
        self._generation = generation
        self._capacity = capacity
        self._rows = OrderedDict()
        self._lock = threading.Lock()

    def _fence(self, session):
        try:
            return self._current_fence(session)
        except Exception:
            return None  # Diagnostics must not break the existing sampler.

    def _current_fence(self, session):
        if self._identity is None or session.process_pid is None or session.process_identity is None:
            return None
        current = self._read_session(session.actor)
        if current is None or (current.session_ref, current.process_pid, current.process_identity) != (
                session.session_ref, session.process_pid, session.process_identity):
            return None
        binding = self._identity.binding_key(session.actor)
        entity = self._identity.entity_token(session.actor)
        if entity is None or binding[2:] != ("interactive", session.session_ref):
            return None
        return (self._generation(), entity, binding, session.session_ref,
                session.process_pid, session.process_identity)

    def sample(self, session):
        """Called only from existing off-status process probes/maintenance."""
        from hyprial.daemon.impl.mcp.channel.ownership import _owner_process_status
        fence = self._fence(session)
        verdict = _owner_process_status(session.process_pid, session.process_identity)
        if fence is not None and fence == self._fence(session):
            dead = verdict.value in {"pid-missing", "identity-mismatch"}
            with self._lock:
                previous = self._rows.get(session.actor)
                # A proven exit for this birth cannot be undone by a late read.
                if previous is not None and previous[0] == fence and previous[1].exit_verified:
                    return verdict
                self._rows[session.actor] = (fence, ProcessObservation(True, dead), time.monotonic_ns())
                self._rows.move_to_end(session.actor)
                while len(self._rows) > self._capacity:
                    self._rows.popitem(last=False)
        return verdict

    def read(self, session):
        fence = self._fence(session)
        with self._lock:
            row = self._rows.get(session.actor)
        if row is None or fence is None or row[0] != fence:
            return ProcessObservation()
        if not row[1].exit_verified and time.monotonic_ns() - row[2] > 5_000_000_000:
            return ProcessObservation()
        return row[1]
