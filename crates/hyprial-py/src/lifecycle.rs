//! Stop, drain and shutdown coordination (spec sections 5.4, 5.5, 9.5, 12).

use std::sync::Arc;
use std::time::Instant;

use hyprial_core::CellState;
use pyo3::prelude::*;

use crate::cell::{lock, Cell, QueueEntry};
use crate::engine::EngineInner;
use crate::pool::Pool;

/// Deliver queued messages to `sink(message, reason)` in admission order.
/// Called with the GIL held (or attaching here) and no engine lock held;
/// sink exceptions are swallowed (EVT-03). The engine never interprets the
/// message (CUS-04).
pub(crate) fn deliver_dead_letters(
    sink: &Option<Py<PyAny>>,
    items: Vec<QueueEntry>,
    reason: &'static str,
) {
    let Some(sink) = sink else { return };
    crate::gate::attach(|py| {
        for entry in items {
            let _ = sink.bind(py).call1((entry.command.bind(py), reason));
        }
    });
}

/// Stop acceptance (spec 5.4 step 2): exactly once per actor; state ->
/// `stopping`, every pending timer cancelled, a pending restart delay
/// cancelled (STP-03), admission closes immediately. Generation unchanged,
/// no event at acceptance.
pub(crate) fn accept_stop(engine: &Arc<EngineInner>, cell: &Arc<Cell>) {
    {
        let mut inner = lock(&cell.inner);
        if inner.stop.requested {
            return; // already accepted: later calls join the confirmation
        }
        inner.stop.requested = true;
        inner.state = CellState::Stopping;
        if let Some(handle) = inner.restart_timer.take() {
            handle.abort();
        }
    }
    engine.timers.cancel_cell(&cell.id);
    let pump = lock(&cell.inner).pump.clone();
    if let Some(pump) = pump {
        // Wake an idle pump so it runs the confirmation path.
        let _ = pump.send_message(crate::pump::PumpMsg::Wake);
    } else {
        // No live pump (restart_pending / quarantined): a waiting stopper
        // claims the confirmation below.
    }
    cell.cv.notify_all();
    engine.notify_activity();
}

/// Wait for confirmation up to `deadline`, performing the confirmation on
/// the calling thread when the actor is idle and nobody claimed it. Returns
/// true when the actor reached `stopped` (STP-01/CUS-05/CUS-07).
pub(crate) fn wait_stop_confirmation(
    engine: &Arc<EngineInner>,
    cell: &Arc<Cell>,
    deadline: Instant,
) -> bool {
    loop {
        {
            let mut inner = lock(&cell.inner);
            if inner.state == CellState::Stopped {
                return true;
            }
            if !inner.stop.confirming && !inner.in_flight && !inner.respawning {
                // Claim and perform the confirmation here: the actor has no
                // in-flight work, so confirmation cannot wait on a handler.
                inner.stop.confirming = true;
                let items = inner.queue.drain(..).collect::<Vec<_>>();
                drop(inner);
                deliver_dead_letters(&cell.dead_letter_sink, items, "stopped");
                // Same 5.4 steps 4-5 order as the executor path: emit before
                // the state change so `stopped` never reads before the event.
                crate::gate::attach(|py| engine.emit_child_stopped(py, cell));
                {
                    let mut inner = lock(&cell.inner);
                    inner.state = CellState::Stopped;
                    inner.pump = None;
                    inner.in_flight = false;
                }
                engine.release_key_if_held(cell);
                cell.cv.notify_all();
                engine.notify_activity();
                return true;
            }
            let now = Instant::now();
            if now >= deadline {
                // CUS-05: no dead letters for this attempt; the actor
                // remains `stopping` (retryable).
                return false;
            }
            let (guard, _timeout) = cell
                .cv
                .wait_timeout(inner, deadline - now)
                .expect("cell lock");
            inner = guard;
        }
    }
}

/// Full external stop against one cell with an explicit deadline.
pub(crate) fn stop_cell(engine: &Arc<EngineInner>, cell: &Arc<Cell>, deadline: Instant) -> bool {
    // Snapshot the state WITHOUT holding the guard across accept_stop:
    // acceptance takes the cell lock itself and the mutex is not reentrant.
    let state = lock(&cell.inner).state;
    match state {
        CellState::Stopped => return true,
        CellState::Stopping => {} // join the pending confirmation
        _ => accept_stop(engine, cell),
    }
    wait_stop_confirmation(engine, cell, deadline)
}

/// Pool stop (9.5): acceptance closes token creation; workers exit when the
/// queue is empty AND every token is settled (ack, cancel, or owner-death
/// dead-letter). True only once the workers are gone.
pub(crate) fn stop_pool(_engine: &Arc<EngineInner>, pool: &Arc<Pool>, deadline: Instant) -> bool {
    pool.accept_stop();
    loop {
        {
            let inner = lock(&pool.inner);
            if inner.state == CellState::Stopped {
                return true;
            }
            let now = Instant::now();
            if now >= deadline {
                return false; // stays `stopping`; retryable (EFF-09)
            }
            let (guard, _) = pool
                .cv
                .wait_timeout(inner, deadline - now)
                .expect("pool lock");
            drop(guard);
        }
    }
}

/// Drain (spec 5.5). Targets are pre-validated; `engine_wide` marks the
/// engine-wide lifecycle so `spawn` is refused. Polls the idle predicate
/// with the GIL released; on success every target is stopped.
pub(crate) fn run_drain(
    engine: &Arc<EngineInner>,
    cells: &[Arc<Cell>],
    pools: &[Arc<Pool>],
    engine_wide: bool,
    timeout: f64,
) -> (bool, f64, Vec<String>) {
    let start = Instant::now();
    let deadline = start + std::time::Duration::from_secs_f64(timeout.max(0.0));
    if engine_wide {
        engine.enter_engine_wide_drain();
    }
    for cell in cells {
        {
            let mut inner = lock(&cell.inner);
            inner.draining = true;
        }
        engine.timers.cancel_cell(&cell.id);
    }
    for pool in pools {
        lock(&pool.inner).draining = true;
    }
    engine.notify_activity();

    let mut drained;
    loop {
        drained = targets_idle(engine, cells, pools);
        if drained || Instant::now() >= deadline {
            break;
        }
        // GIL-02: wait with the GIL released.
        std::thread::sleep(std::time::Duration::from_millis(2));
    }
    let elapsed = start.elapsed().as_secs_f64();
    if !drained {
        let mut remaining: Vec<String> = cells
            .iter()
            .map(|c| c.name.clone())
            .chain(pools.iter().map(|p| p.name.clone()))
            .collect();
        remaining.sort();
        return (false, elapsed, remaining);
    }
    for cell in cells {
        stop_cell(engine, cell, far_future(deadline));
    }
    for pool in pools {
        stop_pool(engine, pool, far_future(deadline));
    }
    if engine_wide {
        engine.mark_engine_drained();
    }
    (true, elapsed, Vec::new())
}

fn far_future(deadline: Instant) -> Instant {
    // Post-quiescence confirmations are immediate (queues are empty and no
    // handler is in flight); the extension only guards against a stalled
    // completion retry wedging a stop forever.
    deadline + std::time::Duration::from_secs(5)
}

/// Engine `shutdown` (spec 9.5, 12): close global admission, stop serial
/// actors FIRST, then pools; completions to already-stopped owners
/// dead-letter and settle their tokens, so every pool reaches zero tokens
/// and its workers exit. True when all engine threads stopped within the
/// timeout (LIFE-01/02). Idempotent: the second call returns the first
/// result (LIFE-03). Its spawn effect equals a completed engine-wide drain
/// (DRN-04).
pub(crate) fn shutdown_engine(engine: &Arc<EngineInner>, timeout: f64) -> bool {
    // LIFE-03: exactly one shutdown runs; losers join and return its result.
    if engine
        .shutdown_started
        .compare_exchange(
            false,
            true,
            std::sync::atomic::Ordering::SeqCst,
            std::sync::atomic::Ordering::SeqCst,
        )
        .is_err()
    {
        let mut slot = lock(&engine.shutdown_result);
        while slot.is_none() {
            slot = engine
                .shutdown_done
                .wait(slot)
                .expect("shutdown result lock");
        }
        return slot.expect("shutdown result set");
    }

    let start = Instant::now();
    let deadline = start + std::time::Duration::from_secs_f64(timeout.max(0.0));
    let mut ok = true;
    let within = |deadline: Instant| Instant::now() < deadline;

    // Spawn effect equals a completed engine-wide drain (DRN-04); the
    // lifecycle gate also closes external admission engine-wide.
    engine.mark_engine_drained();
    engine.cancel_key_starts();

    // Pending timers: cancelled by shutdown (TMR-05); the timer thread ends.
    engine.timers.cancel_all();
    engine.timers.begin_shutdown();

    // Serial actors first (9.5).
    let cells = engine
        .cells
        .read()
        .expect("cells lock")
        .values()
        .cloned()
        .collect::<Vec<_>>();
    for cell in cells {
        if !stop_cell(engine, &cell, deadline) {
            ok = false;
        }
    }

    // Then pools: queue drains, completions settle against stopped owners,
    // tokens reach zero, workers exit (EFF-07/EFF-09).
    let pools = engine
        .pools
        .read()
        .expect("pools lock")
        .values()
        .cloned()
        .collect::<Vec<_>>();
    for pool in pools {
        if !stop_pool(engine, &pool, deadline) {
            ok = false;
        }
    }

    // Engine internals: supervisor, timer thread, runtime (LIFE-02).
    engine.supervisor_cell().stop(None);
    if let Some(timer_handle) = lock(&engine.timer_thread).take() {
        while !timer_handle.is_finished() {
            if !within(deadline) {
                ok = false;
                break;
            }
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        if timer_handle.is_finished() {
            let _ = timer_handle.join();
        }
    }
    let runtime = lock(&engine.rt).take();
    if let Some(rt) = runtime {
        let budget = deadline
            .saturating_duration_since(Instant::now())
            .max(std::time::Duration::from_millis(100));
        rt.shutdown_timeout(budget);
    }
    if !within(deadline) {
        ok = false;
    }

    engine.store_shutdown_result(ok);
    engine.notify_activity();
    ok
}

fn targets_idle(_engine: &Arc<EngineInner>, cells: &[Arc<Cell>], pools: &[Arc<Pool>]) -> bool {
    for cell in cells {
        let inner = lock(&cell.inner);
        // An idle restart_pending actor stops directly: acceptance cancels
        // the pending restart (STP-03), so only pending work blocks here.
        if !inner.queue.is_empty() || inner.in_flight {
            return false;
        }
    }
    for pool in pools {
        let inner = lock(&pool.inner);
        if !inner.queue.is_empty() || inner.in_flight > 0 || !inner.custody.is_empty() {
            return false;
        }
    }
    true
}
