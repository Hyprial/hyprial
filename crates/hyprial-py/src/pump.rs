//! The per-generation executor: a Ractor actor ("pump") that drains the
//! cell's canonical mailbox, invoking the handler with the GIL held and no
//! engine lock held (spec sections 5.2, 12).
//!
//! One pump per generation; on child failure it records the failure in the
//! cell and returns an error so Ractor delivers
//! `SupervisionEvent::ActorFailed` to the engine supervisor. The mailbox
//! lives in the cell, so a restart keeps it (CUS-01).
//!
//! Decisions:
//! - Handlers never run on a tokio worker. Each pump owns one dedicated OS
//!   thread (the generation's executor) that owns the handler instance; a
//!   wake hands that thread one drain request and the pump awaits the
//!   outcome asynchronously. A blocked handler therefore occupies only its
//!   own actor's thread, never the async worker set, so any number of
//!   blocked actors leaves every other actor runnable (LIV-01, FREE-02).
//!   Per-actor FIFO and one-handler-at-a-time follow from the pump handling
//!   one wake at a time and the executor draining sequentially.
//! - A pump that observes stop acceptance stops itself after its drain
//!   (confirming the stop first unless a racing stopper already did), and
//!   a failing generation stops through the Ractor error path (which skips
//!   `post_stop`). Either way dropping the pump state closes the request
//!   channel and joins the executor, which is idle at that point, so a
//!   stopped actor holds no OS thread (LIV-02) and a successful `shutdown`
//!   leaves none behind (LIFE-02). A pump torn down mid-request (runtime
//!   shutdown after a stop timed out) does not join: its executor exits
//!   once the running handler returns.
//! - The executor is an engine-owned thread for the exit gate
//!   (`gate.rs`): it attaches only through `gate::attach`.

use std::panic::AssertUnwindSafe;
use std::sync::Arc;
use std::time::Duration;

use pyo3::prelude::*;

use crate::cell::{lock, Cell, FailureInfo, QueueEntry};
use crate::events::{emit, EventValues};
use crate::execution::ExecutionGuard;

/// Ask result handed back to the waiting `ask` caller.
pub(crate) enum AskOutcome {
    Returned(Py<PyAny>),
    /// Expected rejection: the original exception instance is re-raised.
    Expected(PyErr),
    /// Child failure while this ask was pending.
    ChildFailed,
}

pub(crate) enum PumpMsg {
    Wake,
}

/// Ractor error payload when a child fails; the real information travels in
/// `Cell::failure`.
pub(crate) struct ChildFailed;

impl std::fmt::Debug for ChildFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("ChildFailed")
    }
}

impl std::fmt::Display for ChildFailed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("actor child failed")
    }
}

impl std::error::Error for ChildFailed {}

#[derive(Clone)]
pub(crate) struct PumpActor {
    pub cell: Arc<Cell>,
    pub engine: Arc<crate::engine::EngineInner>,
}

pub(crate) struct PumpArgs {
    pub handler: crate::handlers::Handler,
}

/// What one drain request ended with, reported by the executor thread.
enum DrainOutcome {
    /// Mailbox empty: the pump stays alive until the next wake or stop.
    Idle,
    /// Stop acceptance observed (and confirmed by someone): the pump stops.
    Stopped,
    /// Child failure recorded in the cell: the pump fails (Ractor error).
    Failed,
}

type DrainRequest = tokio::sync::oneshot::Sender<DrainOutcome>;

/// Pump state: the request channel into the generation's executor thread.
pub(crate) struct PumpState {
    requests: Option<std::sync::mpsc::Sender<DrainRequest>>,
    executor: Option<std::thread::JoinHandle<()>>,
    /// A drain request is outstanding (the executor may be in a handler).
    busy: bool,
}

impl Drop for PumpState {
    /// Closing the channel ends the executor; it is joined when idle, and
    /// left to finish its drain on its own when the pump was torn down
    /// mid-request (runtime shutdown, kill).
    fn drop(&mut self) {
        self.requests.take();
        if let Some(executor) = self.executor.take() {
            if !self.busy {
                let _ = executor.join();
            }
        }
    }
}

impl ractor::Actor for PumpActor {
    type Msg = PumpMsg;
    type State = PumpState;
    type Arguments = PumpArgs;

    async fn pre_start(
        &self,
        _myself: ractor::ActorRef<Self::Msg>,
        args: PumpArgs,
    ) -> Result<Self::State, ractor::ActorProcessingErr> {
        let (tx, rx) = std::sync::mpsc::channel::<DrainRequest>();
        let actor = self.clone();
        let handler = args.handler;
        let executor = std::thread::Builder::new()
            .name("hyprial-actor".into())
            .spawn(move || actor.run_executor(handler, rx))?;
        Ok(PumpState {
            requests: Some(tx),
            executor: Some(executor),
            busy: false,
        })
    }

    async fn handle(
        &self,
        myself: ractor::ActorRef<Self::Msg>,
        _message: Self::Msg,
        state: &mut Self::State,
    ) -> Result<(), ractor::ActorProcessingErr> {
        let (reply, outcome) = tokio::sync::oneshot::channel();
        let sent = state
            .requests
            .as_ref()
            .is_some_and(|requests| requests.send(reply).is_ok());
        if !sent {
            return Err(Box::new(ChildFailed) as ractor::ActorProcessingErr);
        }
        state.busy = true;
        let outcome = outcome.await;
        state.busy = false;
        match outcome {
            Ok(DrainOutcome::Idle) => Ok(()),
            Ok(DrainOutcome::Stopped) => {
                myself.stop(None);
                Ok(())
            }
            Ok(DrainOutcome::Failed) | Err(_) => {
                Err(Box::new(ChildFailed) as ractor::ActorProcessingErr)
            }
        }
    }
}

enum CommandOutcome {
    Success(Py<PyAny>),
    Expected(PyErr),
    Failed {
        command: Py<PyAny>,
        exception: Option<PyErr>,
        panic_text: Option<String>,
    },
}

enum ExecutionOutcome {
    Continue,
    ChildFailed(FailureInfo),
}

impl PumpActor {
    /// Executor thread body: serve drain requests until the pump closes the
    /// channel; the handler instance lives and dies on this thread.
    fn run_executor(
        &self,
        mut handler: crate::handlers::Handler,
        requests: std::sync::mpsc::Receiver<DrainRequest>,
    ) {
        crate::gate::mark_engine_thread();
        while let Ok(reply) = requests.recv() {
            let outcome = self.drain_and_execute(&mut handler);
            let _ = reply.send(outcome);
        }
    }

    fn drain_and_execute(&self, handler: &mut crate::handlers::Handler) -> DrainOutcome {
        loop {
            let popped = {
                let mut inner = lock(&self.cell.inner);
                if inner.stop.requested {
                    drop(inner);
                    self.confirm_stop_and_exit();
                    return DrainOutcome::Stopped;
                }
                match inner.queue.pop_front() {
                    Some(entry) => {
                        inner.in_flight = true;
                        Some(entry)
                    }
                    None => None,
                }
            };
            let Some(entry) = popped else {
                return DrainOutcome::Idle;
            };

            match self.execute_one(handler, entry) {
                ExecutionOutcome::Continue => {
                    {
                        let mut inner = lock(&self.cell.inner);
                        inner.in_flight = false;
                    }
                    self.cell.cv.notify_all();
                }
                ExecutionOutcome::ChildFailed(failure) => {
                    {
                        let mut inner = lock(&self.cell.inner);
                        inner.in_flight = false;
                        inner.failure = Some(failure);
                    }
                    self.cell.cv.notify_all();
                    self.engine.notify_activity();
                    return DrainOutcome::Failed;
                }
            }
        }
    }

    /// Execute one popped command. Never holds an engine lock while calling
    /// into Python; panics are caught and converted to the child-failure
    /// path so nothing unwinds across the FFI (E-9, SUP-09).
    fn execute_one(
        &self,
        handler: &crate::handlers::Handler,
        entry: QueueEntry,
    ) -> ExecutionOutcome {
        let _guard = ExecutionGuard::serial(self.cell.clone());
        let engine = self.engine.clone();
        let outcome = crate::gate::attach(|py| {
            let command = entry.command.clone();
            let result =
                std::panic::catch_unwind(AssertUnwindSafe(|| handler.call(py, command.clone())));
            match result {
                Ok(Ok(value)) => {
                    after_success(&self.cell, py, &command);
                    CommandOutcome::Success(value)
                }
                Ok(Err(err)) => {
                    if engine.is_expected_error(py, &err) {
                        after_rejection(&self.cell, py, &command, &err);
                        CommandOutcome::Expected(err)
                    } else {
                        CommandOutcome::Failed {
                            command,
                            exception: Some(err),
                            panic_text: None,
                        }
                    }
                }
                Err(panic) => CommandOutcome::Failed {
                    command,
                    exception: None,
                    panic_text: Some(panic_message(panic)),
                },
            }
        });

        match outcome {
            CommandOutcome::Success(value) => {
                reply(&entry, AskOutcome::Returned(value));
                ExecutionOutcome::Continue
            }
            CommandOutcome::Expected(err) => {
                reply(&entry, AskOutcome::Expected(err));
                ExecutionOutcome::Continue
            }
            CommandOutcome::Failed {
                command,
                exception,
                panic_text,
            } => {
                reply(&entry, AskOutcome::ChildFailed);
                let generation = lock(&self.cell.inner).generation;
                ExecutionOutcome::ChildFailed(FailureInfo {
                    generation,
                    // The failed command itself is dropped (6.3.5); only its
                    // type name survives for the child_failed event.
                    command: Some(command),
                    exception,
                    panic_text,
                })
            }
        }
    }

    /// Confirmation path on the executor thread (spec 5.4 steps 4-5): claim
    /// the confirmation, dead-letter the remaining mailbox in admission
    /// order, emit `child_stopped`, mark `stopped`.
    fn confirm_stop_and_exit(&self) {
        let items = {
            let mut inner = lock(&self.cell.inner);
            if inner.state == hyprial_core::CellState::Stopped || inner.stop.confirming {
                return;
            }
            inner.stop.confirming = true;
            inner.queue.drain(..).collect::<Vec<_>>()
        };
        crate::lifecycle::deliver_dead_letters(&self.cell.dead_letter_sink, items, "stopped");
        // Steps 4-5 order emission before the state change: an observer that
        // reads `stopped` must already see `child_stopped` (STP-04). Marking
        // first left a window where the state read `stopped` before the
        // event landed (native conformance: 1/20 runs failed).
        crate::gate::attach(|py| {
            self.engine.emit_child_stopped(py, &self.cell);
        });
        {
            let mut inner = lock(&self.cell.inner);
            inner.state = hyprial_core::CellState::Stopped;
            inner.pump = None;
            inner.in_flight = false;
        }
        self.engine.release_key_if_held(&self.cell);
        self.cell.cv.notify_all();
        self.engine.notify_activity();
    }
}

fn after_success(cell: &Arc<Cell>, py: Python<'_>, command: &Py<PyAny>) {
    let (generation, sinks) = {
        let inner = lock(&cell.inner);
        (inner.generation, cell.sinks.clone())
    };
    let (group, key) = cell
        .keyed()
        .map_or((None, None), |(g, k)| (Some(g), Some(k)));
    emit(
        py,
        &sinks,
        EventValues {
            kind: "command_completed",
            actor_id: &cell.id,
            name: &cell.name,
            generation,
            command: Some(command),
            command_type_override: None,
            code: None,
            detail: None,
            restart_delay: None,
            group,
            key,
        },
    );
}

/// Expected rejection: `command_rejected` with the exception's `code` and
/// `detail` when present; the actor keeps running, budget untouched
/// (EXP-01/02). Emission happens with the GIL held and no lock held.
fn after_rejection(cell: &Arc<Cell>, py: Python<'_>, command: &Py<PyAny>, err: &PyErr) {
    let (generation, sinks) = {
        let inner = lock(&cell.inner);
        (inner.generation, cell.sinks.clone())
    };
    let value = err.value(py);
    let code = value.getattr("code").ok().map(|o| o.unbind());
    let detail = value.getattr("detail").ok().map(|o| o.unbind());
    let (group, key) = cell
        .keyed()
        .map_or((None, None), |(g, k)| (Some(g), Some(k)));
    emit(
        py,
        &sinks,
        EventValues {
            kind: "command_rejected",
            actor_id: &cell.id,
            name: &cell.name,
            generation,
            command: Some(command),
            command_type_override: None,
            code,
            detail,
            restart_delay: None,
            group,
            key,
        },
    );
}

fn reply(entry: &QueueEntry, outcome: AskOutcome) {
    if let Some(tx) = &entry.reply {
        // If the asker already timed out, the send failure is irrelevant.
        let _ = tx.send(outcome);
    }
}

pub(crate) fn panic_message(panic: Box<dyn std::any::Any + Send>) -> String {
    if let Some(text) = panic.downcast_ref::<&str>() {
        (*text).to_string()
    } else if let Some(text) = panic.downcast_ref::<String>() {
        text.clone()
    } else {
        "rust panic in engine or native handler".to_string()
    }
}

/// Retry cadence for fired-timer payloads whose target mailbox is full
/// (spec section 8, TMR-04; the completion-custody cadence of 9.4).
pub(crate) const TIMER_RETRY: Duration = Duration::from_millis(10);
