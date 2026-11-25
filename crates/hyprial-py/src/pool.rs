//! Worker-pool actors (spec section 9): one logical actor whose payloads are
//! executed by N worker threads, custody-token admission, at-least-once
//! completion delivery. Semantics freeze the shipped EffectLane behaviour
//! exactly (R2): workers own their completion delivery and re-tell at the
//! fixed retry interval until the token settles; a worker error is an
//! EffectCompleted, never a pool failure; workers exit only when the queue
//! is empty AND every token is settled.
//!
//! Decisions:
//! - A Rust panic caught at the FFI boundary in a worker becomes
//!   `EffectCompleted(error="PanicException")`: `error` is a type name
//!   (spec 3.3, 9.3), so the panic message is not appended; it is the
//!   same code the serial path reports in `child_failed` (E-9).

use std::collections::VecDeque;
use std::panic::AssertUnwindSafe;
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use hyprial_core::{
    classify_completion_delivery, CellState, CustodyKey, CustodyTable, ReserveOutcome,
};
use pyo3::prelude::*;

use crate::cell::lock;
use crate::effects::EffectCompleted;
use crate::execution::ExecutionGuard;
use crate::handlers::Handler;

/// `EffectCompleted.error` for a worker whose handler panicked (E-9).
pub(crate) const PANIC_ERROR: &str = "PanicException";

pub(crate) struct PoolJob {
    pub key: CustodyKey,
    pub payload: Py<PyAny>,
    pub owner_id: String,
    pub owner_name: String,
}

pub(crate) struct PoolInner {
    /// `running` / `stopping` / `stopped`; pools never restart (9.3).
    pub state: CellState,
    pub custody: CustodyTable,
    pub queue: VecDeque<PoolJob>,
    pub in_flight: usize,
    /// Part of an in-progress drain: external admission closed.
    pub draining: bool,
    pub live_workers: usize,
    /// `child_stopped` emitted exactly once.
    pub confirmed: bool,
}

pub(crate) struct Pool {
    pub id: String,
    pub name: String,
    pub retry_interval: Duration,
    pub dead_letter_sink: Option<Py<PyAny>>,
    pub sinks: crate::events::EventSinks,
    pub inner: Mutex<PoolInner>,
    pub cv: Condvar,
}

impl Pool {
    pub(crate) fn snapshot_tuple(&self) -> (String, u64, usize, usize, u64) {
        let inner = lock(&self.inner);
        (
            inner.state.as_str().to_string(),
            1, // pools never restart in v1 (9.3)
            inner.queue.len(),
            inner.in_flight,
            0, // no pool failure path exists (EFF-02)
        )
    }
}

/// Admission outcomes shared with the serial path (E-2): 0 / 1 / 2.
pub(crate) enum PoolAdmission {
    Accepted,
    Overloaded,
    Closed,
}

impl Pool {
    fn admission_state(&self) -> PoolAdmission {
        let inner = lock(&self.inner);
        match inner.state {
            CellState::Running if !inner.draining => PoolAdmission::Accepted,
            _ => PoolAdmission::Closed,
        }
    }

    /// `pool_reserve`: O(1) from the token table, never behind backlog.
    pub(crate) fn reserve(&self, key: &CustodyKey) -> PoolAdmission {
        match self.admission_state() {
            PoolAdmission::Closed => return PoolAdmission::Closed,
            PoolAdmission::Overloaded => return PoolAdmission::Overloaded,
            PoolAdmission::Accepted => {}
        }
        let mut inner = lock(&self.inner);
        match inner.custody.reserve(key) {
            ReserveOutcome::Reserved | ReserveOutcome::AlreadyHeld => PoolAdmission::Accepted,
            ReserveOutcome::AtCapacity => PoolAdmission::Overloaded,
        }
    }

    /// `pool_submit_reserved`: binds the owner, queues the payload; success
    /// is unconditional for a live unsubmitted token (EFF-04) — including
    /// while the pool is stopping/draining, mirroring EffectLane where
    /// `submit_reserved` answers from the token table only. Stop acceptance
    /// closes token CREATION (reserve), not protocol completion of a token
    /// that already owns a capacity slot.
    pub(crate) fn submit_reserved(
        &self,
        key: &CustodyKey,
        payload: Py<PyAny>,
        owner_id: String,
        owner_name: String,
    ) -> PoolAdmission {
        let mut inner = lock(&self.inner);
        match inner.custody.state_of(key) {
            Some(hyprial_core::TokenState::Submitted) => PoolAdmission::Accepted,
            Some(hyprial_core::TokenState::Reserved) => {
                inner.custody.submit(key);
                inner.queue.push_back(PoolJob {
                    key: key.clone(),
                    payload,
                    owner_id,
                    owner_name,
                });
                drop(inner);
                self.cv.notify_all();
                PoolAdmission::Accepted
            }
            None => PoolAdmission::Closed,
        }
    }

    pub(crate) fn cancel_reservation(&self, key: &CustodyKey) -> bool {
        let mut inner = lock(&self.inner);
        let cancelled = inner.custody.cancel(key);
        if cancelled {
            drop(inner);
            self.cv.notify_all();
        }
        cancelled
    }

    /// `acknowledge`: settles a submitted token; second call -> false.
    pub(crate) fn acknowledge(&self, key: &CustodyKey) -> bool {
        let settled = {
            let mut inner = lock(&self.inner);

            inner.custody.acknowledge(key)
        };
        if settled {
            self.cv.notify_all();
        }
        settled
    }

    /// Stop acceptance (9.5): closes token creation; already-submitted
    /// payloads still execute and completions still retry. Returns false if
    /// another caller already accepted.
    pub(crate) fn accept_stop(&self) -> bool {
        let mut inner = lock(&self.inner);
        if inner.state != CellState::Running {
            return false;
        }
        inner.state = CellState::Stopping;
        drop(inner);
        self.cv.notify_all();
        true
    }
}

/// Worker body. `handler` was built once per worker at pool start (spec 4).
pub(crate) fn run_worker(
    pool: Arc<Pool>,
    engine: Arc<crate::engine::EngineInner>,
    handler: Handler,
) {
    crate::gate::mark_engine_thread();
    loop {
        let job = {
            let mut inner = lock(&pool.inner);
            loop {
                if let Some(job) = inner.queue.pop_front() {
                    inner.in_flight += 1;
                    break Some(job);
                }
                if inner.state != CellState::Running && inner.custody.is_empty() {
                    inner.live_workers -= 1;
                    break None;
                }
                let (guard, _) = pool
                    .cv
                    .wait_timeout(inner, Duration::from_millis(200))
                    .expect("pool lock");
                inner = guard;
            }
        };
        let Some(job) = job else {
            // Exit condition reached: queue empty, every token settled.
            finish_worker(&pool, &engine);
            return;
        };

        let effect = crate::gate::attach(|py| {
            let _guard = ExecutionGuard::pool(pool.clone());
            let outcome = std::panic::catch_unwind(AssertUnwindSafe(|| {
                handler.call(py, job.payload.clone())
            }));
            match outcome {
                Ok(Ok(value)) => EffectCompleted {
                    operation_id: job.key.0.clone(),
                    generation: job.key.1,
                    result: value,
                    error: None,
                },
                Ok(Err(err)) => {
                    // ANY exception — expected, unexpected, or any
                    // BaseException — becomes the error completion (EFF-02).
                    let type_name = crate::events::exception_type_name(err.value(py));
                    EffectCompleted {
                        operation_id: job.key.0.clone(),
                        generation: job.key.1,
                        result: py.None(),
                        error: Some(type_name),
                    }
                }
                Err(_panic) => EffectCompleted {
                    operation_id: job.key.0.clone(),
                    generation: job.key.1,
                    result: py.None(),
                    error: Some(PANIC_ERROR.to_string()),
                },
            }
        });
        {
            let mut inner = lock(&pool.inner);
            inner.in_flight -= 1;
        }
        pool.cv.notify_all();
        engine.notify_activity();

        deliver_until_settled(&pool, &engine, job, effect);
    }
}

/// Completion custody (9.4): retry the internal tell at the fixed interval
/// until the token settles by acknowledge or by owner-death dead-lettering.
/// Waits never hold the GIL (GIL-02).
fn deliver_until_settled(
    pool: &Arc<Pool>,
    engine: &Arc<crate::engine::EngineInner>,
    job: PoolJob,
    effect: EffectCompleted,
) {
    let effect_obj = crate::gate::attach(|py| {
        Py::new(py, effect)
            .expect("EffectCompleted construction")
            .into_any()
    });
    loop {
        // Settled meanwhile (owner acked or another path settled it)?
        if !token_live(pool, &job.key) {
            return;
        }
        let Some(owner) = engine.cell_pair(&job.owner_id, &job.owner_name) else {
            // Owners are validated synchronously at submit; the record
            // persists in `stopped`, so this is unreachable in practice.
            settle_to_dead_letter(
                pool,
                &job.key,
                crate::gate::attach(|py| effect_obj.clone_ref(py)),
                "stopped",
            );
            return;
        };
        let owner_state = lock(&owner.inner).state;
        match classify_completion_delivery(owner_state) {
            hyprial_core::DeliveryDecision::Admit => {
                // py-clone: attach for the refcount increment; the tell
                // itself needs no Python.
                let effect_copy = crate::gate::attach(|py| effect_obj.clone_ref(py));
                match engine.tell_internal(&owner, effect_copy) {
                    crate::engine::Admission::Accepted => { /* wait for ack below */ }
                    crate::engine::Admission::Overloaded => { /* retry below */ }
                    crate::engine::Admission::Closed => continue, // reclassify next loop
                }
            }
            hyprial_core::DeliveryDecision::Retry => { /* owner stopping; retry below */ }
            hyprial_core::DeliveryDecision::DeadLetter(reason) => {
                settle_to_dead_letter(pool, &job.key, effect_obj, reason);
                return;
            }
        }
        // Wait one retry interval (at-least-once: re-tell after it).
        if wait_for_settlement(pool, &job.key, pool.retry_interval) {
            return;
        }
    }
}

fn token_live(pool: &Pool, key: &CustodyKey) -> bool {
    lock(&pool.inner).custody.state_of(key).is_some()
}

/// Wait up to `interval` for the token to settle; true when settled.
fn wait_for_settlement(pool: &Pool, key: &CustodyKey, interval: Duration) -> bool {
    let deadline = Instant::now() + interval;
    let mut inner = lock(&pool.inner);
    loop {
        if inner.custody.state_of(key).is_none() {
            return true;
        }
        let now = Instant::now();
        if now >= deadline {
            return false;
        }
        let (guard, _) = pool
            .cv
            .wait_timeout(inner, deadline - now)
            .expect("pool lock");
        inner = guard;
    }
}

/// Owner-death terminal row: hand the EffectCompleted instance ONCE to the
/// pool's dead-letter sink with the owner's reason and settle the token in
/// the same step (EFF-07). A racing acknowledge wins instead and no dead
/// letter is sent for an already-handled completion.
fn settle_to_dead_letter(
    pool: &Arc<Pool>,
    key: &CustodyKey,
    effect: Py<PyAny>,
    reason: &'static str,
) {
    let won = {
        let mut inner = lock(&pool.inner);

        inner.custody.settle(key)
    };
    if !won {
        return;
    }
    pool.cv.notify_all();
    crate::gate::attach(|py| {
        if let Some(sink) = &pool.dead_letter_sink {
            // CUS-04: the engine never interprets the message; sink
            // exceptions are swallowed (EVT-03).
            let _ = sink.bind(py).call1((effect.bind(py), reason));
        }
    });
}

/// Last worker out confirms the stop: `child_stopped` emitted exactly once,
/// state `stopped` (9.5, EFF-09).
fn finish_worker(pool: &Arc<Pool>, engine: &Arc<crate::engine::EngineInner>) {
    let claimed = {
        let mut inner = lock(&pool.inner);
        inner.live_workers == 0 && inner.state == CellState::Stopping && !inner.confirmed && {
            inner.confirmed = true;
            true
        }
    };
    if !claimed {
        return;
    }
    crate::gate::attach(|py| {
        engine.emit_child_stopped_for_pool(py, pool);
    });
    {
        let mut inner = lock(&pool.inner);
        inner.state = CellState::Stopped;
    }
    pool.cv.notify_all();
    engine.notify_activity();
}
