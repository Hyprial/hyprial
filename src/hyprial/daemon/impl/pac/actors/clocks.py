"""Clock batch worker: one authority round trip per admitted batch.

Split out of ``daemon`` to keep that module inside the 1000-line gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from hyprial.daemon.impl.pac.graphs.reactor import PacReactor
from hyprial.daemon.impl.pac.storage.store import PacGraphStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hyprial.daemon.impl.pac.actors.daemon import PacActorService

#: Clock jobs travel as batches: one authority round trip per batch instead of
#: one per graph.  Capped so a large graph set cannot queue behind a single
#: slow batch (head-of-line blocking stays bounded by this number of graphs).
PAC_CLOCK_BATCH = 64

#: The clock queue admits about this many clock jobs before it reports a full
#: queue: the cap is expressed in jobs and converted to batches, so batching
#: does not silently multiply how much backlog the queue holds.
PAC_CLOCK_QUEUE_JOBS = 128


def run_clock_worker(service: "PacActorService") -> None:
    """Drain clock batches from the service queue until it is closed."""

    while True:
        batch = service._clock_queue.get()
        if batch is None:
            return
        if service._closed:
            # close() has begun: drop the remaining backlog instead of
            # draining it.  Queued ticks are a replayable view of the
            # graphs table -- the next service instance's submit_tick
            # re-derives every clock job, and tick side effects are
            # SQL-idempotent across restarts -- so discarding them here
            # loses nothing, while draining them would tie close()
            # latency to the backlog size.
            return
        try:
            if service._graph_authority is not None:
                # Per-graph isolation happens inside the batch handler, so
                # an exception here means the batch itself failed.
                service._graph_authority.clock_tick_many(batch)
            else:
                store = PacGraphStore(service.database)
                try:
                    reactor = PacReactor(
                        store, sender=service.sender, logger=service.logger,
                        publisher=service._hook_publisher,
                    )
                    for graph_id in batch:
                        try:
                            reactor.tick_clocks(graph_id)
                        except Exception as error:  # noqa: BLE001 - isolate
                            _clock_tick_failed(service, graph_id, error)
                finally:
                    store.close()
        except Exception as error:  # noqa: BLE001 - isolate one batch
            service.logger(
                "error",
                "pac",
                "pac.clock.batch_failed",
                graphs=len(batch),
                errorType=type(error).__name__,
                detail=str(error)[:500],
            )
        finally:
            with service._lock:
                service._active_clocks.difference_update(batch)

def _clock_tick_failed(service: "PacActorService", graph_id: str, error: BaseException) -> None:
    service.logger(
        "error",
        "pac",
        "pac.clock.tick_failed",
        graphId=graph_id,
        errorType=type(error).__name__,
        detail=str(error)[:500],
    )
