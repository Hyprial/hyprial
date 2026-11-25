//! Serial actor cell: state, generation, the canonical mailbox, and the
//! restart budget (spec sections 5, 6).
//!
//! The cell owns the FIFO queue across restarts — that is what makes the
//! mailbox "kept" — while each generation's executor is a Ractor actor
//! ("pump") linked to the engine supervisor.
//!
//! Decisions (I2 keyed actors):
//! - `group`/`key` are plain data on the record; the engine owns the index
//!   from the pair to the live cell. A supervision restart keeps the record
//!   and therefore the key; only a confirmed stop releases it. Across the
//!   stop states (round 2 S3): `stopping`, `restart_pending` and
//!   `quarantined` cells keep the pair — lookup returns the original actor and
//!   same-key spawn is rejected — and the release at a confirmed stop hands
//!   the remaining mailbox to the existing dead-letter path (5.4), so the
//!   next same-key spawn starts with an empty mailbox.

use std::collections::VecDeque;
use std::sync::{Mutex, MutexGuard};

use hyprial_core::{CellState, RestartBudget, RestartPolicy};
use pyo3::prelude::*;

use crate::events::EventSinks;
use crate::handlers::HandlerFactory;
use crate::pump::{AskOutcome, PumpMsg};

/// Recover from a poisoned lock: a panic inside a critical section is an
/// engine bug, but the engine must stay observable rather than cascade.
pub(crate) fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
}

pub(crate) struct QueueEntry {
    pub command: Py<PyAny>,
    /// Present exactly when the entry was created by `ask`.
    pub reply: Option<std::sync::mpsc::Sender<AskOutcome>>,
}

/// Failure information recorded by the dying generation and consumed by the
/// supervisor's `SupervisionEvent::ActorFailed` handling.
pub(crate) struct FailureInfo {
    /// Generation that failed; stale reports are fenced (SUP-08).
    pub generation: u64,
    /// The command that failed, if any: dropped, never re-executed, but its
    /// type name feeds the `child_failed` event.
    pub command: Option<Py<PyAny>>,
    pub exception: Option<PyErr>,
    /// Set when the failure was a caught Rust panic rather than a Python
    /// exception (E-9).
    pub panic_text: Option<String>,
}

impl FailureInfo {
    /// `(code, detail)` for the `child_failed` / `child_quarantined` events.
    /// Called with the GIL held and no engine lock held.
    pub(crate) fn code_and_detail(&self, py: Python<'_>) -> (Py<PyAny>, Py<PyAny>) {
        if let Some(exc) = &self.exception {
            let value = exc.value(py);
            let code = crate::events::obj(py, crate::events::exception_type_name(value));
            let detail = match value.str() {
                Ok(text) => crate::events::obj(py, text.to_string()),
                Err(_) => py.None(),
            };
            (code, detail)
        } else if let Some(text) = &self.panic_text {
            let code = crate::events::obj(py, crate::pool::PANIC_ERROR);
            let detail = crate::events::obj(py, text.clone());
            (code, detail)
        } else {
            let code = crate::events::obj(py, crate::pool::PANIC_ERROR);
            (code, py.None())
        }
    }
}

/// Handle for the pending restart-backoff timer (a Ractor `send_after`).
pub(crate) type RestartTimerHandle = ractor::concurrency::JoinHandle<
    Result<(), ractor::errors::MessagingErr<crate::supervisor::SupervisorMsg>>,
>;

#[derive(Default)]
pub(crate) struct StopControl {
    /// Acceptance happened (spec 5.4 step 2): admission closed, timers
    /// cancelled. Exactly once per actor.
    pub requested: bool,
    /// Confirmation claimed (dead-letter drain + `child_stopped`); the pump
    /// and racing stoppers coordinate through this flag (CUS-07).
    pub confirming: bool,
}

pub(crate) struct CellInner {
    pub state: CellState,
    /// Monotone; increments exactly when the actor enters `running`.
    pub generation: u64,
    /// The canonical mailbox: survives restarts (CUS-01).
    pub queue: VecDeque<QueueEntry>,
    /// True while one handler execution is in progress (serial actor).
    pub in_flight: bool,
    pub budget: RestartBudget,
    /// Current generation's executor, if the actor is running.
    pub pump: Option<ractor::ActorRef<PumpMsg>>,
    /// Pending restart-backoff timer handle (Ractor `send_after`), cancelled
    /// at stop acceptance (STP-03).
    pub restart_timer: Option<RestartTimerHandle>,
    /// A restart factory call is in flight (fence for racing stops).
    pub respawning: bool,
    pub stop: StopControl,
    /// Cell is part of an in-progress drain: external admission closed.
    pub draining: bool,
    /// Set by the dying generation, consumed once by the supervisor.
    pub failure: Option<FailureInfo>,
}

pub(crate) struct Cell {
    pub id: String,
    pub name: String,
    /// I2 keyed identity: present exactly when the actor was spawned under a
    /// stable (group, key) pair; the engine keeps the live-key index.
    pub group: Option<String>,
    pub key: Option<String>,
    pub capacity: usize,
    pub policy: RestartPolicy,
    pub factory: HandlerFactory,
    pub dead_letter_sink: Option<Py<PyAny>>,
    pub sinks: EventSinks,
    pub inner: Mutex<CellInner>,
    /// Stoppers wait here for confirmation (spec 5.4).
    pub cv: std::sync::Condvar,
}

impl Cell {
    /// The owned key pair, when this cell is keyed (for index operations).
    pub(crate) fn key_pair(&self) -> Option<(String, String)> {
        match (&self.group, &self.key) {
            (Some(group), Some(key)) => Some((group.clone(), key.clone())),
            _ => None,
        }
    }

    /// (group, key) references for event emission; None when keyless.
    pub(crate) fn keyed(&self) -> Option<(&str, &str)> {
        match (&self.group, &self.key) {
            (Some(group), Some(key)) => Some((group.as_str(), key.as_str())),
            _ => None,
        }
    }
}

impl Cell {
    pub(crate) fn snapshot_tuple(&self) -> (String, u64, usize, usize, u64) {
        let inner = lock(&self.inner);
        (
            inner.state.as_str().to_string(),
            inner.generation,
            inner.queue.len(),
            if inner.in_flight { 1 } else { 0 },
            inner.budget.len() as u64,
        )
    }
}
