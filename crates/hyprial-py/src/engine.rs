//! `hyprial._core.actors.Engine` — the Python-facing surface (spec section 3).
//!
//! One engine hosts many actors inside one CPython process. All engine
//! state is guarded by engine-owned synchronization that stays correct if
//! the GIL disappears (D12, FREE-01); every callback into Python happens
//! with the GIL held and no engine-internal lock held (GIL-01).
//!
//! Decisions:
//! - Spawning (`require_spawnable`, argument validation, record and pump /
//!   worker construction) lives in `spawn.rs` to keep this file under the
//!   1000-line rule; the `#[pymethods]` surface stays in this one block.
//! - `native_handler_factory` reads the process-global registry populated
//!   at module init (`handlers.rs`), not per-engine state, so every
//!   `Engine` in a process resolves the same names (spec 4).
//! - Conformance-only pump failure injection, the post-establishment hold
//!   (`HYPRIAL_ACTOR_TEST_PUMP_START_HOLD`) and registry readout are compiled
//!   only with the `conformance-handlers` feature.

use std::collections::HashMap;
use std::sync::{Arc, Condvar, Mutex, RwLock};
use std::time::{Duration, Instant};

use hyprial_core::{CellState, CustodyKey, Jitter, PolicyTable};
use pyo3::exceptions::{PyKeyError, PyRuntimeError, PyTimeoutError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyType;
use ractor::Actor;

use crate::cell::{lock, Cell, QueueEntry};
use crate::errors::{ActorAdmissionError, ActorFailedError};
use crate::events::{emit, EventValues};
use crate::handlers::{lookup_native_factory, NativeHandlerFactoryHandle};
use crate::lifecycle;
use crate::pool::{Pool, PoolAdmission};
use crate::pump::{AskOutcome, PumpMsg};
use crate::spawn::{validate_and_classify_factory, validate_key_pair, KeySlot, SpawnResult};
use crate::supervisor::{SupervisorActor, SupervisorMsg};
use crate::timers::TimerTable;

/// Admission outcomes as values (E-1/E-2): 0 accepted / 1 overloaded / 2
/// closed. `tell` and the pool admissions never raise for these.
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Admission {
    Accepted,
    Overloaded,
    Closed,
}

impl Admission {
    fn code(self) -> u8 {
        match self {
            Admission::Accepted => 0,
            Admission::Overloaded => 1,
            Admission::Closed => 2,
        }
    }
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum Lifecycle {
    Active,
    /// An engine-wide drain is in progress: `spawn` raises RuntimeError.
    EngineWideDrain,
    /// An engine-wide drain completed (or `shutdown` finished): `spawn`
    /// raises RuntimeError forever (E-7, DRN-04).
    Drained,
}

pub(crate) struct EngineInner {
    pub policies: PolicyTable,
    pub rng: Mutex<Jitter>,
    pub engine_sink: Py<PyAny>,
    pub expected_error: Py<PyAny>,
    pub native_handle_type: Py<PyAny>,

    pub cells: RwLock<HashMap<String, Arc<Cell>>>,
    pub pools: RwLock<HashMap<String, Arc<Pool>>>,
    /// I2 key index: (group, key) -> live serial actor or an in-progress
    /// first spawn. A live entry is released exactly when its cell confirms
    /// its stop; a failed start retires its own slot. Never held together
    /// with a cell-inner lock in the reverse order (claim takes keyed then
    /// inner; release takes keyed only, after the state change), and no
    /// Python runs under this lock.
    pub(crate) keyed: RwLock<HashMap<(String, String), KeySlot>>,
    /// Ractor actor id -> cell, for supervision event routing.
    pump_registry: Mutex<HashMap<ractor::ActorId, Arc<Cell>>>,

    pub timers: Arc<TimerTable>,
    /// Taken by `shutdown` and ended with `shutdown_timeout`; other users
    /// go through `rt_block_on`.
    pub(crate) rt: Mutex<Option<tokio::runtime::Runtime>>,
    supervisor: Mutex<Option<(ractor::ActorRef<SupervisorMsg>, ractor::ActorCell)>>,
    pub(crate) timer_thread: Mutex<Option<std::thread::JoinHandle<()>>>,

    lifecycle: Mutex<Lifecycle>,
    activity: Condvar,
    pub(crate) id_counter: std::sync::atomic::AtomicU64,
    /// First shutdown result; later calls return it without side effects.
    pub(crate) shutdown_result: Mutex<Option<bool>>,
    pub(crate) shutdown_started: std::sync::atomic::AtomicBool,
    pub(crate) shutdown_done: Condvar,
    #[cfg(feature = "conformance-handlers")]
    pub(crate) test_pump_start_failure: std::sync::atomic::AtomicBool,
    #[cfg(feature = "conformance-handlers")]
    pub(crate) test_pump_hold: crate::spawn::TestPumpHold,
}

impl EngineInner {
    pub(crate) fn rt_block_on<F: std::future::Future>(&self, future: F) -> Option<F::Output> {
        lock(&self.rt).as_ref().map(|rt| rt.block_on(future))
    }

    /// Run `f` with the runtime context installed on this thread (for calls
    /// that spawn runtime tasks, e.g. Ractor timers) without blocking on it.
    pub(crate) fn with_rt_enter<T>(&self, f: impl FnOnce() -> T) -> Option<T> {
        let guard = lock(&self.rt);
        guard.as_ref().map(|rt| {
            let _enter = rt.enter();
            f()
        })
    }
    // ----- handle resolution (forged-handle rule, ADM-04) -----

    pub(crate) fn find_cell(&self, actor_id: &str, name: &str) -> Option<Arc<Cell>> {
        let cell = self
            .cells
            .read()
            .expect("cells lock")
            .get(actor_id)
            .cloned()?;
        if cell.name != name {
            return None;
        }
        Some(cell)
    }

    pub(crate) fn find_pool(&self, actor_id: &str, name: &str) -> Option<Arc<Pool>> {
        let pool = self
            .pools
            .read()
            .expect("pools lock")
            .get(actor_id)
            .cloned()?;
        if pool.name != name {
            return None;
        }
        Some(pool)
    }

    pub(crate) fn cell_by_id(&self, cell_id: &str) -> Option<Arc<Cell>> {
        self.cells.read().expect("cells lock").get(cell_id).cloned()
    }

    /// Owner-handle validation for pool completions: pair must match exactly.
    pub(crate) fn cell_pair(&self, actor_id: &str, name: &str) -> Option<Arc<Cell>> {
        self.find_cell(actor_id, name)
    }

    pub(crate) fn cell_for_ractor_id(&self, id: ractor::ActorId) -> Option<Arc<Cell>> {
        self.pump_registry
            .lock()
            .expect("pump registry")
            .get(&id)
            .cloned()
    }

    pub(crate) fn remember_pump(&self, ractor_id: ractor::ActorId, cell: Arc<Cell>) {
        self.pump_registry
            .lock()
            .expect("pump registry")
            .insert(ractor_id, cell);
    }

    pub(crate) fn forget_pump(&self, ractor_id: ractor::ActorId) {
        self.pump_registry
            .lock()
            .expect("pump registry")
            .remove(&ractor_id);
    }

    // ----- supervision plumbing -----

    pub(crate) fn supervisor_ref(&self) -> ractor::ActorRef<SupervisorMsg> {
        self.supervisor
            .lock()
            .expect("supervisor slot")
            .as_ref()
            .expect("supervisor started")
            .0
            .clone()
    }

    pub(crate) fn supervisor_cell(&self) -> ractor::ActorCell {
        self.supervisor
            .lock()
            .expect("supervisor slot")
            .as_ref()
            .expect("supervisor started")
            .1
            .clone()
    }

    /// Expected-error classification (6.2): applies ONLY to exceptions
    /// raised while executing a command in a handler.
    pub(crate) fn is_expected_error(&self, py: Python<'_>, err: &PyErr) -> bool {
        let expected = self.expected_error.bind(py);
        err.value(py).is_instance(expected).unwrap_or(false)
    }

    pub(crate) fn emit_child_stopped(&self, py: Python<'_>, cell: &Cell) {
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
                kind: "child_stopped",
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
    }

    pub(crate) fn emit_child_stopped_for_pool(&self, py: Python<'_>, pool: &Pool) {
        emit(
            py,
            &pool.sinks,
            EventValues {
                kind: "child_stopped",
                actor_id: &pool.id,
                name: &pool.name,
                generation: 1,
                command: None,
                command_type_override: None,
                code: None,
                detail: None,
                restart_delay: None,
                group: None,
                key: None,
            },
        );
    }

    pub(crate) fn notify_activity(&self) {
        self.activity.notify_all();
    }

    // ----- lifecycle gates -----

    pub(crate) fn lifecycle(&self) -> Lifecycle {
        *lock(&self.lifecycle)
    }

    pub(crate) fn enter_engine_wide_drain(&self) {
        let mut gate = lock(&self.lifecycle);
        if *gate == Lifecycle::Active {
            *gate = Lifecycle::EngineWideDrain;
        }
    }

    pub(crate) fn mark_engine_drained(&self) {
        *lock(&self.lifecycle) = Lifecycle::Drained;
    }

    // ----- admission (5.2) -----

    /// External admission for `tell`/`ask`: counts only `queued` against
    /// capacity; closed for stopping/stopped/quarantined/draining/unknown.
    /// Never blocks (ORD-04, LIV-01).
    fn external_admit(
        &self,
        cell: &Arc<Cell>,
        command: Py<PyAny>,
        reply: Option<std::sync::mpsc::Sender<AskOutcome>>,
    ) -> Admission {
        // Global admission gate: an engine-wide drain or shutdown closes
        // every cell's external admission (5.5, 9.5).
        if self.lifecycle() != Lifecycle::Active {
            return Admission::Closed;
        }
        let pump = {
            let mut inner = lock(&cell.inner);
            match inner.state {
                CellState::Stopping | CellState::Stopped | CellState::Quarantined => {
                    return Admission::Closed
                }
                CellState::Running | CellState::RestartPending => {}
            }
            // ADM-03: a draining cell closes EXTERNAL admission.
            // Engine-internal completion custody goes through `tell_internal`
            // instead (DRN-09).
            if inner.draining {
                return Admission::Closed;
            }
            // ADM-02: only queued counts against capacity; in_flight does not.
            if inner.queue.len() >= cell.capacity {
                return Admission::Overloaded;
            }
            inner.queue.push_back(QueueEntry { command, reply });
            inner.pump.clone()
        };
        if let Some(pump) = pump {
            let _ = pump.send_message(PumpMsg::Wake);
        }
        Admission::Accepted
    }

    /// Engine-internal admission (completion custody, fired timer payloads):
    /// NOT refused by drain closure (DRN-09); otherwise identical outcomes.
    pub(crate) fn tell_internal(&self, cell: &Arc<Cell>, command: Py<PyAny>) -> Admission {
        let pump = {
            let mut inner = lock(&cell.inner);
            match inner.state {
                CellState::Stopping | CellState::Stopped | CellState::Quarantined => {
                    return Admission::Closed
                }
                CellState::Running | CellState::RestartPending => {}
            }
            if inner.queue.len() >= cell.capacity {
                return Admission::Overloaded;
            }
            inner.queue.push_back(QueueEntry {
                command,
                reply: None,
            });
            inner.pump.clone()
        };
        if let Some(pump) = pump {
            let _ = pump.send_message(PumpMsg::Wake);
        }
        Admission::Accepted
    }

    pub(crate) fn store_shutdown_result(&self, result: bool) {
        {
            let mut slot = lock(&self.shutdown_result);
            if slot.is_none() {
                *slot = Some(result);
            }
        }
        self.shutdown_done.notify_all();
    }
}

fn deadline_after(timeout: f64) -> Instant {
    Instant::now() + Duration::from_secs_f64(timeout.max(0.0))
}

impl Engine {
    /// Pool target resolution for the `pool_*` methods: a serial actor (or
    /// unknown handle) classifies closed (EFF-11).
    fn pool_target(&self, actor_id: &str, name: &str) -> Option<Arc<Pool>> {
        if self.inner.find_cell(actor_id, name).is_some() {
            return None;
        }
        self.inner.find_pool(actor_id, name)
    }
}

#[pyclass(frozen, name = "Engine", module = "hyprial._core.actors")]
pub struct Engine {
    pub(crate) inner: Arc<EngineInner>,
}

#[pymethods]
impl Engine {
    #[new]
    #[pyo3(signature = (*, policies, jitter_seed, event_sink, expected_error_type))]
    fn new(
        py: Python<'_>,
        policies: HashMap<String, (i64, f64, f64, f64, f64)>,
        jitter_seed: Option<i64>,
        event_sink: Py<PyAny>,
        expected_error_type: Py<PyAny>,
    ) -> PyResult<Engine> {
        // E-8: validation errors are ValueError at construction.
        let table =
            PolicyTable::build(policies).map_err(|e| PyValueError::new_err(e.to_string()))?;
        if !expected_error_type.bind(py).is_instance_of::<PyType>() {
            return Err(PyValueError::new_err("expected_error_type must be a type"));
        }
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(
                std::thread::available_parallelism()
                    .map(|n| n.get().clamp(4, 16))
                    .unwrap_or(4),
            )
            .on_thread_start(crate::gate::mark_engine_thread)
            .enable_all()
            .build()
            .map_err(|e| PyRuntimeError::new_err(format!("tokio runtime: {e}")))?;

        let native_handle_type = py
            .get_type::<NativeHandlerFactoryHandle>()
            .into_any()
            .unbind();

        let inner = Arc::new(EngineInner {
            policies: table,
            rng: Mutex::new(Jitter::new(jitter_seed.map(|s| s as u64))),
            engine_sink: event_sink,
            expected_error: expected_error_type,
            native_handle_type,
            cells: RwLock::new(HashMap::new()),
            pools: RwLock::new(HashMap::new()),
            keyed: RwLock::new(HashMap::new()),
            pump_registry: Mutex::new(HashMap::new()),
            timers: Arc::new(TimerTable::new()),
            rt: Mutex::new(Some(rt)),
            supervisor: Mutex::new(None),
            timer_thread: Mutex::new(None),
            lifecycle: Mutex::new(Lifecycle::Active),
            activity: Condvar::new(),
            id_counter: std::sync::atomic::AtomicU64::new(1),
            shutdown_result: Mutex::new(None),
            shutdown_started: std::sync::atomic::AtomicBool::new(false),
            shutdown_done: Condvar::new(),
            #[cfg(feature = "conformance-handlers")]
            test_pump_start_failure: std::sync::atomic::AtomicBool::new(
                std::env::var("HYPRIAL_ACTOR_TEST_PUMP_START_FAILURE").as_deref() == Ok("1"),
            ),
            #[cfg(feature = "conformance-handlers")]
            test_pump_hold: crate::spawn::TestPumpHold::from_env(),
        });

        // Start the engine supervisor inside the runtime (6.5).
        let supervisor = {
            let engine = inner.clone();
            let future = SupervisorActor::spawn(None, SupervisorActor { engine }, ());
            inner.rt_block_on(future)
        };
        match supervisor {
            Some(Ok((sup_ref, _handle))) => {
                *inner.supervisor.lock().expect("supervisor slot") =
                    Some((sup_ref.clone(), sup_ref.get_cell()));
            }
            Some(Err(err)) => {
                return Err(PyRuntimeError::new_err(format!(
                    "engine supervisor failed to start: {err}"
                )))
            }
            None => {
                return Err(PyRuntimeError::new_err(
                    "engine supervisor failed to start: runtime is shut down",
                ))
            }
        }

        // The timer thread owns fire-time classification (section 8).
        {
            let timers = inner.timers.clone();
            let engine = inner.clone();
            let handle = std::thread::Builder::new()
                .name("hyprial-actors-timers".into())
                .spawn(move || {
                    crate::gate::mark_engine_thread();
                    timers.run(engine)
                })
                .map_err(|e| PyRuntimeError::new_err(format!("timer thread: {e}")))?;
            *inner.timer_thread.lock().expect("timer thread slot") = Some(handle);
        }

        Ok(Engine { inner })
    }

    // ----- spawning (3.2) -----

    #[pyo3(signature = (name, handler_factory, capacity, profile, dead_letter_sink=None, event_sink=None, group=None, key=None))]
    #[allow(clippy::too_many_arguments)]
    fn spawn(
        &self,
        py: Python<'_>,
        name: &str,
        handler_factory: Py<PyAny>,
        capacity: i64,
        profile: &str,
        dead_letter_sink: Option<Py<PyAny>>,
        event_sink: Option<Py<PyAny>>,
        group: Option<Py<PyAny>>,
        key: Option<Py<PyAny>>,
    ) -> PyResult<String> {
        self.inner.require_spawnable()?;
        let key_pair = validate_key_pair(py, group, key)?;
        let factory = validate_and_classify_factory(
            py,
            name,
            capacity,
            profile,
            handler_factory,
            &self.inner,
        )?;
        let result = self.inner.spawn_cell(
            py,
            name.to_string(),
            factory,
            capacity as usize,
            profile.to_string(),
            dead_letter_sink,
            event_sink,
            key_pair,
        )?;
        match result {
            SpawnResult::Id(id) => Ok(id),
        }
    }

    /// I2: resolve a live keyed actor to (actor_id, name).
    #[pyo3(signature = (group, key))]
    fn lookup(
        &self,
        py: Python<'_>,
        group: Py<PyAny>,
        key: Py<PyAny>,
    ) -> PyResult<(String, String)> {
        let pair = validate_key_pair(py, Some(group), Some(key))?.expect("validated pair");
        match self.inner.live_keyed_cell(&pair) {
            Some(cell) => Ok((cell.id.clone(), cell.name.clone())),
            None => Err(PyKeyError::new_err(format!(
                "no live actor under key {:?}/{:?}",
                pair.0, pair.1
            ))),
        }
    }

    #[pyo3(signature = (name, worker_handler_factory, pool_size, capacity, profile, dead_letter_sink=None, event_sink=None, retry_interval=0.01))]
    #[allow(clippy::too_many_arguments)]
    fn spawn_pool(
        &self,
        py: Python<'_>,
        name: &str,
        worker_handler_factory: Py<PyAny>,
        pool_size: i64,
        capacity: i64,
        profile: &str,
        dead_letter_sink: Option<Py<PyAny>>,
        event_sink: Option<Py<PyAny>>,
        retry_interval: f64,
    ) -> PyResult<String> {
        self.inner.require_spawnable()?;
        if pool_size < 1 {
            return Err(PyValueError::new_err("pool_size must be >= 1"));
        }
        if retry_interval <= 0.0 {
            return Err(PyValueError::new_err("retry_interval must be > 0"));
        }
        let factory = validate_and_classify_factory(
            py,
            name,
            capacity,
            profile,
            worker_handler_factory,
            &self.inner,
        )?;
        self.inner.spawn_pool_actor(
            name.to_string(),
            factory,
            pool_size as usize,
            capacity as usize,
            dead_letter_sink,
            event_sink,
            Duration::from_secs_f64(retry_interval),
        )
    }

    // ----- tell / ask (5.2, 10) -----

    /// 0 = accepted, 1 = overloaded, 2 = closed; never raises for admission
    /// outcomes (E-1).
    fn tell(&self, actor_id: &str, name: &str, command: Py<PyAny>) -> u8 {
        if let Some(cell) = self.inner.find_cell(actor_id, name) {
            return self.inner.external_admit(&cell, command, None).code();
        }
        // Pool targets classify as closed (EFF-11); so do unknown handles.
        let _ = self.inner.find_pool(actor_id, name);
        Admission::Closed.code()
    }

    fn ask(
        &self,
        py: Python<'_>,
        actor_id: &str,
        name: &str,
        command: Py<PyAny>,
        timeout: f64,
    ) -> PyResult<Py<PyAny>> {
        if self.inner.find_pool(actor_id, name).is_some() {
            // A pool target classifies as closed (3.2, ASK-04).
            return Err(ActorAdmissionError::new_err("closed"));
        }
        let Some(cell) = self.inner.find_cell(actor_id, name) else {
            return Err(ActorAdmissionError::new_err("closed"));
        };
        let (tx, rx) = std::sync::mpsc::channel();
        match self.inner.external_admit(&cell, command, Some(tx)) {
            Admission::Overloaded => return Err(ActorAdmissionError::new_err("overloaded")),
            Admission::Closed => return Err(ActorAdmissionError::new_err("closed")),
            Admission::Accepted => {}
        }
        let deadline = deadline_after(timeout);
        // GIL-02: release the GIL while waiting (LIV-03, ASK-01). A deadline
        // <= 0 is checked exactly once (TOL-2).
        // TOL-2: one recv with the remaining budget; a deadline <= 0 lands
        // on Duration::ZERO, which still polls the channel exactly once.
        let outcome = py.detach(move || {
            let now = Instant::now();
            let remaining = if deadline > now {
                deadline - now
            } else {
                Duration::ZERO
            };
            match rx.recv_timeout(remaining) {
                Ok(outcome) => Ok(outcome),
                Err(std::sync::mpsc::RecvTimeoutError::Timeout) => Err(()),
                Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => Ok(AskOutcome::ChildFailed),
            }
        });
        match outcome {
            Err(()) => Err(PyTimeoutError::new_err(
                "ask timed out; the command may still execute",
            )),
            Ok(AskOutcome::Returned(value)) => Ok(value),
            Ok(AskOutcome::Expected(err)) => Err(err),
            Ok(AskOutcome::ChildFailed) => Err(ActorFailedError::new_err(
                "the target actor failed while the ask was pending",
            )),
        }
    }

    // ----- timers (8) -----

    fn send_after(
        &self,
        actor_id: &str,
        name: &str,
        delay: f64,
        command: Py<PyAny>,
    ) -> PyResult<String> {
        if self.inner.find_pool(actor_id, name).is_some() {
            return Err(PyValueError::new_err(
                "send_after target must be a serial actor",
            ));
        }
        let Some(cell) = self.inner.find_cell(actor_id, name) else {
            return Err(PyKeyError::new_err(format!("unknown actor {actor_id:?}")));
        };
        if delay < 0.0 {
            return Err(PyValueError::new_err("delay must be >= 0"));
        }
        // A valid serial actor in ANY state accepts the timer (8).
        Ok(self
            .inner
            .timers
            .schedule(cell, Duration::from_secs_f64(delay), command))
    }

    fn cancel_after(&self, timer_id: &str) -> bool {
        self.inner.timers.cancel(timer_id)
    }

    // ----- pools (9) -----

    #[pyo3(signature = (actor_id, name, owner_id, owner_name, operation_id, generation, payload))]
    #[allow(clippy::too_many_arguments)]
    fn pool_submit(
        &self,
        actor_id: &str,
        name: &str,
        owner_id: &str,
        owner_name: &str,
        operation_id: &str,
        generation: i64,
        payload: Py<PyAny>,
    ) -> u8 {
        let Some(pool) = self.pool_target(actor_id, name) else {
            return Admission::Closed.code();
        };
        // Submit-time owner validation (EFF-13): unknown/forged owner ->
        // 2 and no token is created. Shutdown closes global admission.
        if self.inner.lifecycle() != Lifecycle::Active
            || self.inner.cell_pair(owner_id, owner_name).is_none()
        {
            return Admission::Closed.code();
        }
        let key: CustodyKey = (operation_id.to_string(), generation as u64);
        match pool.reserve(&key) {
            PoolAdmission::Closed => return Admission::Closed.code(),
            PoolAdmission::Overloaded => return Admission::Overloaded.code(),
            PoolAdmission::Accepted => {}
        }
        match pool.submit_reserved(&key, payload, owner_id.to_string(), owner_name.to_string()) {
            PoolAdmission::Accepted => Admission::Accepted.code(),
            _ => Admission::Closed.code(),
        }
    }

    fn pool_reserve(&self, actor_id: &str, name: &str, operation_id: &str, generation: i64) -> u8 {
        let Some(pool) = self.pool_target(actor_id, name) else {
            return Admission::Closed.code();
        };
        // Engine-wide drain and shutdown close reservation (5.5, DRN-06).
        if self.inner.lifecycle() != Lifecycle::Active {
            return Admission::Closed.code();
        }
        let key: CustodyKey = (operation_id.to_string(), generation as u64);
        match pool.reserve(&key) {
            PoolAdmission::Accepted => Admission::Accepted.code(),
            PoolAdmission::Overloaded => Admission::Overloaded.code(),
            PoolAdmission::Closed => Admission::Closed.code(),
        }
    }

    #[pyo3(signature = (actor_id, name, owner_id, owner_name, operation_id, generation, payload))]
    #[allow(clippy::too_many_arguments)]
    fn pool_submit_reserved(
        &self,
        actor_id: &str,
        name: &str,
        owner_id: &str,
        owner_name: &str,
        operation_id: &str,
        generation: i64,
        payload: Py<PyAny>,
    ) -> u8 {
        let Some(pool) = self.pool_target(actor_id, name) else {
            return Admission::Closed.code();
        };
        // EFF-13: forged/unknown owner returns 2 with the live token
        // REMAINING (the caller's protocol cancels it).
        if self.inner.cell_pair(owner_id, owner_name).is_none() {
            return Admission::Closed.code();
        }
        let key: CustodyKey = (operation_id.to_string(), generation as u64);
        match pool.submit_reserved(&key, payload, owner_id.to_string(), owner_name.to_string()) {
            PoolAdmission::Accepted => Admission::Accepted.code(),
            // submit_reserved never overloads: the token owns the slot.
            _ => Admission::Closed.code(),
        }
    }

    fn pool_cancel_reservation(
        &self,
        actor_id: &str,
        name: &str,
        operation_id: &str,
        generation: i64,
    ) -> bool {
        let Some(pool) = self.pool_target(actor_id, name) else {
            return false;
        };
        let key: CustodyKey = (operation_id.to_string(), generation as u64);
        if pool.cancel_reservation(&key) {
            self.inner.notify_activity();
            true
        } else {
            false
        }
    }

    fn acknowledge(&self, actor_id: &str, name: &str, operation_id: &str, generation: i64) -> bool {
        let Some(pool) = self.inner.find_pool(actor_id, name) else {
            return false;
        };
        let key: CustodyKey = (operation_id.to_string(), generation as u64);
        if pool.acknowledge(&key) {
            self.inner.notify_activity();
            true
        } else {
            false
        }
    }

    // ----- snapshot / stop / reset / drain / shutdown -----

    fn snapshot(&self, actor_id: &str, name: &str) -> PyResult<(String, u64, usize, usize, u64)> {
        if let Some(cell) = self.inner.find_cell(actor_id, name) {
            return Ok(cell.snapshot_tuple());
        }
        if let Some(pool) = self.inner.find_pool(actor_id, name) {
            return Ok(pool.snapshot_tuple());
        }
        Err(PyKeyError::new_err(format!("unknown actor {actor_id:?}")))
    }

    fn stop(&self, py: Python<'_>, actor_id: &str, name: &str, timeout: f64) -> bool {
        // Self-stop recognition reads the CALLING thread's execution marker.
        // Self-stop (5.4, R3-2) is non-blocking accept-or-join in every
        // state; the external path waits below with the GIL released
        // (GIL-02).
        if let Some(pool) = self.inner.find_pool(actor_id, name) {
            if let Some(crate::execution::ExecutionContext::Pool { pool: ctx_pool }) =
                crate::execution::current_execution()
            {
                if ctx_pool.id == pool.id {
                    pool.accept_stop();
                    return true;
                }
            }
            let inner = self.inner.clone();
            return py.detach(move || lifecycle::stop_pool(&inner, &pool, deadline_after(timeout)));
        }
        let Some(cell) = self.inner.find_cell(actor_id, name) else {
            return false;
        };
        if let Some(crate::execution::ExecutionContext::Serial { cell: ctx_cell }) =
            crate::execution::current_execution()
        {
            if ctx_cell.id == cell.id {
                {
                    let inner = lock(&cell.inner);
                    if inner.state == CellState::Stopped {
                        return true;
                    }
                }
                lifecycle::accept_stop(&self.inner, &cell);
                return true;
            }
        }
        let inner = self.inner.clone();
        py.detach(move || lifecycle::stop_cell(&inner, &cell, deadline_after(timeout)))
    }

    fn reset(&self, actor_id: &str, name: &str) -> bool {
        let Some(cell) = self.inner.find_cell(actor_id, name) else {
            return false;
        };
        let generation;
        {
            let mut inner = lock(&cell.inner);
            // SUP-07: reset only from quarantined and never while draining.
            if inner.state != CellState::Quarantined || inner.draining {
                return false;
            }
            inner.budget.clear();
            inner.state = CellState::RestartPending;
            generation = inner.generation;
        }
        // Row 6: restart_scheduled with delay 0, then child_restarted on
        // re-entry (generation +1 there).
        crate::gate::attach(|py| {
            let sinks = cell.sinks.clone();
            let (group, key) = cell
                .keyed()
                .map_or((None, None), |(g, k)| (Some(g), Some(k)));
            emit(
                py,
                &sinks,
                EventValues {
                    kind: "restart_scheduled",
                    actor_id: &cell.id,
                    name: &cell.name,
                    generation,
                    command: None,
                    command_type_override: None,
                    code: None,
                    detail: None,
                    restart_delay: Some(0.0),
                    group,
                    key,
                },
            );
        });
        let cell_id = cell.id.clone();
        // Ractor timers spawn onto the tokio runtime: they need the runtime
        // context on this thread, not block_on.
        let supervisor_ref = self.inner.supervisor_ref();
        let handle = self
            .inner
            .with_rt_enter(|| {
                supervisor_ref.send_after(Duration::ZERO, move || SupervisorMsg::Respawn {
                    cell_id,
                    generation,
                })
            })
            .expect("engine runtime is shut down");
        lock(&cell.inner).restart_timer = Some(handle);
        self.inner.notify_activity();
        true
    }

    #[pyo3(signature = (actor_ids, timeout))]
    fn drain(
        &self,
        py: Python<'_>,
        actor_ids: Option<Vec<String>>,
        timeout: f64,
    ) -> PyResult<(bool, f64, Vec<String>)> {
        // Resolve before any state change; unknown id in the list -> KeyError.
        let (cells, pools, engine_wide) = match &actor_ids {
            None => {
                let cells = self
                    .inner
                    .cells
                    .read()
                    .expect("cells lock")
                    .values()
                    .cloned()
                    .collect::<Vec<_>>();
                let pools = self
                    .inner
                    .pools
                    .read()
                    .expect("pools lock")
                    .values()
                    .cloned()
                    .collect::<Vec<_>>();
                (cells, pools, true)
            }
            Some(ids) => {
                let mut cells = Vec::new();
                let mut pools = Vec::new();
                for id in ids {
                    if let Some(cell) = self.inner.cell_by_id(id) {
                        cells.push(cell);
                    } else if let Some(pool) = self
                        .inner
                        .pools
                        .read()
                        .expect("pools lock")
                        .get(id)
                        .cloned()
                    {
                        pools.push(pool);
                    } else {
                        return Err(PyKeyError::new_err(format!("unknown actor {id:?}")));
                    }
                }
                (cells, pools, false)
            }
        };
        // GIL-02: the whole drain wait runs with the GIL released.
        let inner = self.inner.clone();
        Ok(py.detach(move || lifecycle::run_drain(&inner, &cells, &pools, engine_wide, timeout)))
    }

    fn shutdown(&self, py: Python<'_>, timeout: f64) -> bool {
        let inner = self.inner.clone();
        // GIL-02: wait with the GIL released. LIFE-03: idempotent — the
        // second call returns the first result without side effects.
        py.detach(move || lifecycle::shutdown_engine(&inner, timeout))
    }

    // ----- native handler registry (4) -----

    fn native_handler_factory(&self, name: &str) -> PyResult<NativeHandlerFactoryHandle> {
        match lookup_native_factory(name) {
            Some(factory) => Ok(NativeHandlerFactoryHandle(factory)),
            None => Err(PyValueError::new_err(format!(
                "unknown native handler factory {name:?}"
            ))),
        }
    }

    fn __repr__(&self) -> String {
        let cells = self.inner.cells.read().expect("cells lock").len();
        let pools = self.inner.pools.read().expect("pools lock").len();
        format!("Engine(actors={cells}, pools={pools})")
    }

    #[cfg(feature = "conformance-handlers")]
    #[pyo3(name = "_test_registry_counts")]
    fn test_registry_counts(&self) -> (usize, usize, usize) {
        let actors = self.inner.cells.read().expect("cells lock").len()
            + self.inner.pools.read().expect("pools lock").len();
        let keyed = self.inner.keyed.read().expect("keyed lock").len();
        let pumps = self
            .inner
            .pump_registry
            .lock()
            .expect("pump registry")
            .len();
        (actors, keyed, pumps)
    }

    #[cfg(feature = "conformance-handlers")]
    #[pyo3(name = "_test_pump_start_hold_reached")]
    fn test_pump_start_hold_reached(&self) -> bool {
        self.inner.test_pump_hold.reached()
    }

    #[cfg(feature = "conformance-handlers")]
    #[pyo3(name = "_test_release_pump_start_hold")]
    fn test_release_pump_start_hold(&self) {
        self.inner.test_pump_hold.release();
    }
}
