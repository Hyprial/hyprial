//! Generation-tagged one-shot timers (spec section 8).
//!
//! One engine thread owns the deadline heap. Cancellation removes the entry;
//! firing classifies exactly once against the target's current state and
//! generation (TMR-02/04). A payload whose target mailbox is full retries at
//! the completion-custody cadence until accepted, closed, or cancelled.
//!
//! Decisions:
//! - Fire time copies the payload reference (py-clone needs an attached
//!   thread) by attaching FIRST and taking the table lock inside the scope:
//!   the lock order is GIL then table lock everywhere, like a Python caller
//!   of `send_after`, so the timer thread never waits for the GIL while it
//!   holds the table lock, and a timer thread parked by the exit gate
//!   (`gate.rs`) holds no engine lock. A user-cancelled entry in the race
//!   window is skipped.

use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashMap};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use hyprial_core::{classify_timer_fire, FireDecision};
use pyo3::prelude::*;

use crate::cell::{lock, Cell};
use crate::pump::TIMER_RETRY;

pub(crate) struct TimerEntry {
    pub cell: Arc<Cell>,
    /// Generation recorded at schedule time; the restart fence (TMR-02).
    pub generation: u64,
    pub command: Py<PyAny>,
    /// Absolute fire deadline (also kept in the lazy heap queue).
    pub deadline: Instant,
    /// Set by stop-acceptance cancellation for entries whose deadline had
    /// already passed: their payload must still be fire-time classified
    /// against the live state, never silently lost (TMR-05).
    pub stop_fenced: bool,
}

#[derive(Default)]
struct TimerInner {
    entries: HashMap<String, TimerEntry>,
    heap: BinaryHeap<Reverse<(Instant, String)>>,
    shutdown: bool,
}

pub(crate) struct TimerTable {
    inner: Mutex<TimerInner>,
    cv: std::sync::Condvar,
    counter: AtomicU64,
}

impl TimerTable {
    pub(crate) fn new() -> TimerTable {
        TimerTable {
            inner: Mutex::new(TimerInner::default()),
            cv: std::sync::Condvar::new(),
            counter: AtomicU64::new(1),
        }
    }

    /// Schedule one timer; the caller has validated the target synchronously
    /// (unknown/forged handle -> KeyError, pool target / negative delay ->
    /// ValueError happen in the engine method).
    pub(crate) fn schedule(&self, cell: Arc<Cell>, delay: Duration, command: Py<PyAny>) -> String {
        let id = format!(
            "timer-{:012x}",
            self.counter.fetch_add(1, Ordering::Relaxed)
        );
        let deadline = Instant::now() + delay;
        let generation = lock(&cell.inner).generation;
        {
            let mut inner = lock(&self.inner);
            inner.entries.insert(
                id.clone(),
                TimerEntry {
                    cell,
                    generation,
                    command,
                    deadline,
                    stop_fenced: false,
                },
            );
            inner.heap.push(Reverse((deadline, id.clone())));
        }
        self.cv.notify_all();
        id
    }

    /// Cancel a pending timer: true when it was still pending (TMR-03).
    /// An entry mid-retry (fired into a full mailbox) is still pending.
    pub(crate) fn cancel(&self, timer_id: &str) -> bool {
        let removed = lock(&self.inner).entries.remove(timer_id).is_some();
        if removed {
            self.cv.notify_all();
        }
        removed
    }

    /// Cancel every pending timer of one cell (stop acceptance,
    /// quarantine, drain start, shutdown — TMR-05). Entries whose deadline
    /// is still in the future are removed (they would never fire); entries
    /// already due keep a stop fence so their payload is classified at
    /// processing time against the live cell state instead of being lost.
    pub(crate) fn cancel_cell(&self, cell_id: &str) {
        {
            let mut inner = lock(&self.inner);
            let now = Instant::now();
            for entry in inner.entries.values_mut() {
                if entry.cell.id == cell_id {
                    entry.stop_fenced = true;
                }
            }
            // Future deadlines are simply cancelled: nothing fires, so
            // nothing is lost. Due entries stay fenced for classification.
            inner
                .entries
                .retain(|_, entry| entry.cell.id != cell_id || entry.deadline <= now);
            // Heap entries for removed ids are skipped lazily at fire time.
        }
        self.cv.notify_all();
    }

    pub(crate) fn cancel_all(&self) {
        {
            let mut inner = lock(&self.inner);
            inner.entries.clear();
        }
        self.cv.notify_all();
    }

    pub(crate) fn begin_shutdown(&self) {
        lock(&self.inner).shutdown = true;
        self.cv.notify_all();
    }

    /// The timer thread body. Runs until `begin_shutdown`. Never holds an
    /// engine lock while calling into Python.
    pub(crate) fn run(self: &Arc<Self>, engine: Arc<crate::engine::EngineInner>) {
        loop {
            let due: Vec<String> = {
                let mut inner = lock(&self.inner);
                if inner.shutdown {
                    return;
                }
                let now = Instant::now();
                let mut due = Vec::new();
                while let Some(Reverse((deadline, _id))) = inner.heap.peek() {
                    if *deadline > now {
                        break;
                    }
                    let Reverse((_, id)) = inner.heap.pop().expect("peeked entry exists");
                    due.push(id);
                }
                if due.is_empty() {
                    let wait = inner
                        .heap
                        .peek()
                        .map(|Reverse((deadline, _))| deadline.saturating_duration_since(now))
                        .unwrap_or(Duration::from_millis(200))
                        .min(Duration::from_millis(200));
                    let (guard, _timeout) =
                        self.cv.wait_timeout(inner, wait).expect("timer table lock");
                    drop(guard);
                    continue;
                }
                due
            };
            for id in due {
                self.fire(&engine, &id);
            }
        }
    }

    /// Fire-time classification (spec section 8). The entry is consumed
    /// exactly once; retries for a full mailbox keep the entry pending.
    fn fire(self: &Arc<Self>, engine: &Arc<crate::engine::EngineInner>, timer_id: &str) {
        let entry = crate::gate::attach(|py| {
            let inner = lock(&self.inner);
            inner.entries.get(timer_id).map(|entry| TimerEntry {
                cell: entry.cell.clone(),
                generation: entry.generation,
                deadline: entry.deadline,
                stop_fenced: entry.stop_fenced,
                command: entry.command.clone_ref(py),
            })
        });
        let Some(entry) = entry else {
            return;
        };
        let cell = entry.cell.clone();
        let (state, generation, capacity_open) = {
            let inner = lock(&cell.inner);
            (
                inner.state,
                inner.generation,
                inner.queue.len() < cell.capacity,
            )
        };
        let generation_matches = generation == entry.generation || entry.stop_fenced;
        match classify_timer_fire(state, generation_matches) {
            FireDecision::FenceDrop => {
                lock(&self.inner).entries.remove(timer_id);
            }
            FireDecision::DeadLetter(reason) => {
                lock(&self.inner).entries.remove(timer_id);
                let command = entry.command;
                crate::lifecycle::deliver_dead_letters(
                    &cell.dead_letter_sink,
                    vec![crate::cell::QueueEntry {
                        command,
                        reply: None,
                    }],
                    reason,
                );
            }
            FireDecision::Admit => {
                if capacity_open {
                    // py-clone: attach for the refcount increment; the tell
                    // itself needs no Python.
                    let command_copy = crate::gate::attach(|py| entry.command.clone_ref(py));
                    match engine.tell_internal(&cell, command_copy) {
                        crate::engine::Admission::Accepted => {
                            lock(&self.inner).entries.remove(timer_id);
                        }
                        // The state snapshot raced a transition; reclassify
                        // on the next pass with fresh state.
                        crate::engine::Admission::Overloaded => self.rearm(timer_id),
                        crate::engine::Admission::Closed => self.reclassify(timer_id, entry),
                    }
                } else {
                    // Full mailbox: retry at the completion-custody cadence
                    // until accepted, closed, or cancelled (TMR-04).
                    self.rearm(timer_id);
                }
            }
        }
    }

    fn rearm(self: &Arc<Self>, timer_id: &str) {
        let mut inner = lock(&self.inner);
        if !inner.entries.contains_key(timer_id) {
            return; // cancelled while retrying
        }
        inner.heap.push(Reverse((
            Instant::now() + TIMER_RETRY,
            timer_id.to_string(),
        )));
        drop(inner);
        self.cv.notify_all();
    }

    /// The target left the restart lineage (stopping/stopped/quarantined)
    /// between the snapshot and the internal tell: classify again by the
    /// fresh state so the payload is never silently lost (TMR-05).
    fn reclassify(self: &Arc<Self>, timer_id: &str, entry: TimerEntry) {
        let cell = entry.cell.clone();
        let (state, generation) = {
            let inner = lock(&cell.inner);
            (inner.state, inner.generation)
        };
        let generation_matches = generation == entry.generation || entry.stop_fenced;
        match classify_timer_fire(state, generation_matches) {
            FireDecision::Admit => {
                // Still admits (e.g. raced with a restart): retry later.
                self.rearm(timer_id);
            }
            FireDecision::FenceDrop => {
                lock(&self.inner).entries.remove(timer_id);
            }
            FireDecision::DeadLetter(reason) => {
                lock(&self.inner).entries.remove(timer_id);
                let command = entry.command;
                crate::lifecycle::deliver_dead_letters(
                    &cell.dead_letter_sink,
                    vec![crate::cell::QueueEntry {
                        command,
                        reply: None,
                    }],
                    reason,
                );
            }
        }
    }
}
