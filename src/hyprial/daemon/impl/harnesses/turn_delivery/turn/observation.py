"""Read only the existing turn owner's locked projection."""
from hyprial.daemon.impl.cells.observability.model import RunFacts


def read_turn_facts(projection):
    with projection.lock:
        busy = projection.in_flight is not None
        return RunFacts(source_epoch=projection.run_epoch, known=projection.run_known,
                        running=True, open_count=int(busy), resync_required=not projection.run_known,
                        since_ms=projection.run_since_ms if busy else None,
                        since_monotonic_ms=projection.run_since_monotonic_ms if busy else None)
