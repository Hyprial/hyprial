"""Turn process family roots."""


from typing import TYPE_CHECKING
from dataclasses import replace
from hyprial.daemon.impl.cells.observability.model import RunFacts



if TYPE_CHECKING:
    pass


class BaseTurnProcess:
    """Common process-family marker for shared turn lifecycle implementations.

    The sequential implementation below retains the established pump and all
    of its public behavior.  Concurrent implementations use this same family
    marker while owning a correlated multi-in-flight adapter.
    """

    def read_run_facts(self) -> RunFacts:
        """Read the concrete turn owner; marker membership alone grants no coverage."""
        runtime = getattr(self, "_turn_runtime", None)
        if runtime is None:
            return RunFacts()
        facts = runtime.read_run_facts()
        # A pump thread before native readiness is not evidence of idle.
        ready = self._ready.is_set() and not self._stopping.is_set()
        with self._lock:
            client = self._client
        read_activity = getattr(client, "read_run_activity", None)
        activity = read_activity() if callable(read_activity) else None
        return replace(facts, running=ready and self.running,
                       activity_supported=activity is not None,
                       quiet=activity[0] if activity is not None else False,
                       quiet_after_ms=activity[2] if activity is not None else None,
                       last_activity_at_ms=activity[1] if activity is not None else None)

    @property
    def dispatch_ready(self) -> bool:
        """Whether dispatch may hand this process a turn without blocking."""

        wait_ready = getattr(self, "wait_ready", None)
        if callable(wait_ready):
            return bool(wait_ready(timeout=0))
        return bool(getattr(self, "running", False))


class ConcurrentTurnProcess(BaseTurnProcess):
    """Bounded concurrent turn-process family seam.

    Concrete adapters own their child wire, while this family-level contract
    validates the declared execution bound.  The Jev adapter supplies the
    complete process implementation and calls this initializer exactly once.
    """

    def __init__(self, *, concurrency: int) -> None:
        if concurrency < 1:
            raise ValueError("concurrent turn-process capacity must be positive")
        self.concurrency = concurrency
