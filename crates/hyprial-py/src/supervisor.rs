//! The one engine supervisor (spec section 6.5, D2).
//!
//! Every serial engine actor is spawned `spawn_linked` to it. The machine is
//! composed from Ractor primitives only: `SupervisionEvent::ActorFailed`
//! drives the failure/budget/backoff logic, restart delays are Ractor
//! `send_after` messages carrying `(cell_id, generation)` (stale ones are
//! dropped by the generation fence of 5.3), and `ActorTerminated` is clean
//! stop cleanup. Pool actors are never supervised children (EFF-02).
//!
//! Decisions:
//! - CL-1: the failure that crosses the restart budget emits `child_failed`
//!   exactly like a restarting failure, then `child_quarantined` with the
//!   same code and detail, and no `restart_scheduled`; both paths share
//!   `emit_child_failed`.
//! - A restart build never panics into this actor: `HandlerFactory::build`
//!   returns a caught panic as a `PanicException` error, which takes the
//!   row-4b path like any raising factory (E-9, SUP-06).

use std::sync::Arc;
use std::time::{Duration, Instant};

use hyprial_core::{BudgetOutcome, CellState};
use pyo3::prelude::*;
use ractor::Actor;

use crate::cell::{lock, Cell, FailureInfo};
use crate::events::{emit, EventValues};
use crate::pump::{PumpActor, PumpArgs, PumpMsg};

pub(crate) enum SupervisorMsg {
    /// A restart-backoff delay elapsed for `(cell_id, generation)`.
    Respawn { cell_id: String, generation: u64 },
}

#[derive(Clone)]
pub(crate) struct SupervisorActor {
    pub engine: Arc<crate::engine::EngineInner>,
}

impl ractor::Actor for SupervisorActor {
    type Msg = SupervisorMsg;
    type State = ();
    type Arguments = ();

    async fn pre_start(
        &self,
        _myself: ractor::ActorRef<Self::Msg>,
        _args: Self::Arguments,
    ) -> Result<Self::State, ractor::ActorProcessingErr> {
        Ok(())
    }

    async fn handle(
        &self,
        _myself: ractor::ActorRef<Self::Msg>,
        message: Self::Msg,
        _state: &mut Self::State,
    ) -> Result<(), ractor::ActorProcessingErr> {
        let SupervisorMsg::Respawn {
            cell_id,
            generation,
        } = message;
        if let Some(cell) = self.engine.cell_by_id(&cell_id) {
            self.respawn(&cell, generation).await;
        }
        Ok(())
    }

    async fn handle_supervisor_evt(
        &self,
        _myself: ractor::ActorRef<Self::Msg>,
        message: ractor::SupervisionEvent,
        _state: &mut Self::State,
    ) -> Result<(), ractor::ActorProcessingErr> {
        match message {
            ractor::SupervisionEvent::ActorFailed(actor_cell, _err) => {
                let ractor_id = actor_cell.get_id();
                if let Some(cell) = self.engine.cell_for_ractor_id(ractor_id) {
                    self.engine.forget_pump(ractor_id);
                    self.on_child_failure(&cell);
                }
            }
            ractor::SupervisionEvent::ActorTerminated(actor_cell, _last_state, _reason) => {
                self.engine.forget_pump(actor_cell.get_id());
            }
            _ => {}
        }
        Ok(())
    }
}

impl SupervisorActor {
    /// Rows 3/4b of the transition table: consume one budget slot, prune the
    /// window, then either quarantine or schedule the next backoff.
    fn on_child_failure(&self, cell: &Arc<Cell>) {
        let failure = {
            let mut inner = lock(&cell.inner);
            // SUP-08: a report from a dead generation is discarded silently.
            match inner.failure.take() {
                Some(info) if info.generation == inner.generation => info,
                Some(_) | None => return,
            }
        };

        let outcome = {
            let mut inner = lock(&cell.inner);
            inner.budget.record(Instant::now())
        };
        match outcome {
            BudgetOutcome::Quarantine => self.quarantine(cell, failure),
            BudgetOutcome::Restart { attempt } => self.schedule_restart(cell, failure, attempt),
        }
    }

    /// Row 5: quarantine. `child_failed` for the budget-crossing failure,
    /// then `child_quarantined`, never `restart_scheduled` (CL-1). Mailbox
    /// drained to dead letters with reason `quarantined`; generation
    /// unchanged; pending timers cancelled.
    fn quarantine(&self, cell: &Arc<Cell>, failure: FailureInfo) {
        let (generation, items) = {
            let mut inner = lock(&cell.inner);
            inner.state = CellState::Quarantined;
            inner.pump = None;
            inner.in_flight = false;
            inner.respawning = false;
            if let Some(handle) = inner.restart_timer.take() {
                handle.abort();
            }
            (inner.generation, inner.queue.drain(..).collect::<Vec<_>>())
        };
        self.engine.timers.cancel_cell(&cell.id);
        crate::gate::attach(|py| {
            emit_child_failed(py, cell, &failure);
            let (code, detail) = failure.code_and_detail(py);
            let (group, key) = cell
                .keyed()
                .map_or((None, None), |(g, k)| (Some(g), Some(k)));
            emit(
                py,
                &cell.sinks,
                EventValues {
                    kind: "child_quarantined",
                    actor_id: &cell.id,
                    name: &cell.name,
                    generation,
                    command: None,
                    command_type_override: None,
                    code: Some(code),
                    detail: Some(detail),
                    restart_delay: None,
                    group,
                    key,
                },
            );
        });
        crate::lifecycle::deliver_dead_letters(&cell.dead_letter_sink, items, "quarantined");
        cell.cv.notify_all();
        self.engine.notify_activity();
    }

    /// Row 3/4b restart branch: `child_failed` then `restart_scheduled`
    /// (EVT-01 order), delay from the capped-backoff formula with one
    /// jitter sample; the delay timer is a Ractor `send_after` carrying
    /// the generation.
    fn schedule_restart(&self, cell: &Arc<Cell>, failure: FailureInfo, attempt: u64) {
        let sample = {
            let mut rng = lock(&self.engine.rng);
            rng.sample01()
        };
        let delay = hyprial_core::restart_delay(&cell.policy, attempt, sample);
        {
            let mut inner = lock(&cell.inner);
            inner.state = CellState::RestartPending;
        }
        crate::gate::attach(|py| {
            emit_child_failed(py, cell, &failure);
            let (group, key) = cell
                .keyed()
                .map_or((None, None), |(g, k)| (Some(g), Some(k)));
            emit(
                py,
                &cell.sinks,
                EventValues {
                    kind: "restart_scheduled",
                    actor_id: &cell.id,
                    name: &cell.name,
                    generation: failure.generation,
                    command: None,
                    command_type_override: None,
                    code: None,
                    detail: None,
                    restart_delay: Some(delay),
                    group,
                    key,
                },
            );
        });
        let target_cell = cell.clone();
        let handle =
            self.engine
                .supervisor_ref()
                .send_after(Duration::from_secs_f64(delay), move || {
                    SupervisorMsg::Respawn {
                        cell_id: target_cell.id.clone(),
                        generation: failure.generation,
                    }
                });
        {
            let mut inner = lock(&cell.inner);
            if inner.stop.requested {
                // Stop acceptance raced the scheduling; the fence at fire
                // time would drop it anyway, abort eagerly (STP-03).
                handle.abort();
            } else {
                inner.restart_timer = Some(handle);
            }
        }
        cell.cv.notify_all();
        self.engine.notify_activity();
    }

    /// Rows 4a/4b: the restart delay elapsed. Re-run the factory; on
    /// success bump the generation on re-entry into `running` and spawn a
    /// fresh pump; on failure treat as a child failure of the generation
    /// that failed to start (SUP-06): never a running actor without a
    /// handler.
    async fn respawn(&self, cell: &Arc<Cell>, generation: u64) {
        // Fence: only a still-pending restart of the same generation runs.
        {
            let mut inner = lock(&cell.inner);
            if inner.stop.requested
                || inner.state != CellState::RestartPending
                || inner.generation != generation
                || inner.respawning
            {
                return;
            }
            inner.respawning = true;
            if let Some(handle) = inner.restart_timer.take() {
                handle.abort();
            }
        }

        let build = crate::gate::attach(|py| cell.factory.build(py));

        match build {
            Ok(handler) => {
                let engine = self.engine.clone();
                let spawn_cell = cell.clone();
                let supervisor = engine.supervisor_cell();
                let spawn_actor = PumpActor {
                    cell: spawn_cell.clone(),
                    engine: engine.clone(),
                };
                let spawn_result =
                    PumpActor::spawn_linked(None, spawn_actor, PumpArgs { handler }, supervisor)
                        .await;
                match spawn_result {
                    Ok((pump_ref, _handle)) => {
                        let mut accepted = false;
                        {
                            let mut inner = lock(&cell.inner);
                            if inner.stop.requested
                                || inner.state != CellState::RestartPending
                                || inner.generation != generation
                            {
                                // Stop acceptance (or reset) raced the
                                // factory: the fresh handler instance is
                                // discarded; the pump exits on its wake.
                            } else {
                                inner.generation += 1;
                                inner.state = CellState::Running;
                                inner.respawning = false;
                                inner.pump = Some(pump_ref.clone());
                                engine.remember_pump(pump_ref.get_id(), cell.clone());
                                accepted = true;
                            }
                        }
                        // The pump sees the kept mailbox either way; when a
                        // stop raced in, its first wake exits immediately.
                        let _ = pump_ref.send_message(PumpMsg::Wake);
                        if accepted {
                            crate::gate::attach(|py| {
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
                                        kind: "child_restarted",
                                        actor_id: &cell.id,
                                        name: &cell.name,
                                        generation,
                                        command: None,
                                        command_type_override: None,
                                        code: None,
                                        detail: None,
                                        restart_delay: None,
                                        group,
                                        key,
                                    },
                                );
                            });
                            cell.cv.notify_all();
                            engine.notify_activity();
                        }
                    }
                    Err(err) => {
                        // Ractor spawn failure is an engine-internal fault;
                        // route it through the same budget machinery as a
                        // restart-factory failure so the actor can never be
                        // lost silently.
                        let failure = FailureInfo {
                            generation,
                            command: None,
                            exception: None,
                            panic_text: Some(format!("pump spawn failed: {err}")),
                        };
                        {
                            let mut inner = lock(&cell.inner);
                            inner.respawning = false;
                            inner.failure = Some(failure);
                        }
                        self.on_child_failure(cell);
                    }
                }
            }
            Err(factory_err) => {
                // Row 4b: child failure of the generation that failed to
                // start; stays `restart_pending` (SUP-06).
                let failure = FailureInfo {
                    generation,
                    command: None,
                    exception: Some(factory_err),
                    panic_text: None,
                };
                {
                    let mut inner = lock(&cell.inner);
                    inner.respawning = false;
                    inner.failure = Some(failure);
                }
                self.on_child_failure(cell);
            }
        }
    }
}

/// `child_failed` for one supervised failure: code = exception type name
/// (or `PanicException`), detail = `str(exc)` (or the panic message),
/// command_type from the failed command (row 3) or None (row 4b), at the
/// generation that failed. Emitted before `restart_scheduled` or
/// `child_quarantined` (EVT-01, CL-1).
fn emit_child_failed(py: Python<'_>, cell: &Cell, failure: &FailureInfo) {
    let (code, detail) = failure.code_and_detail(py);
    let (group, key) = cell
        .keyed()
        .map_or((None, None), |(g, k)| (Some(g), Some(k)));
    emit(
        py,
        &cell.sinks,
        EventValues {
            kind: "child_failed",
            actor_id: &cell.id,
            name: &cell.name,
            generation: failure.generation,
            command: failure.command.as_ref(),
            command_type_override: None,
            code: Some(code),
            detail: Some(detail),
            restart_delay: None,
            group,
            key,
        },
    );
}
