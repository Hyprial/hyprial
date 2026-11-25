//! Spawning serial actors and worker pools (spec sections 3.2, 4, 6.2, 9).
//!
//! Decisions:
//! - Split out of `engine.rs` (1000-line rule) with no behaviour change: the
//!   `#[pymethods]` entry points `spawn` / `spawn_pool` stay on `Engine`
//!   (one `#[pymethods]` block, no `multiple-pymethods` dependency) and
//!   delegate validation and record construction here.
//! - Validation order is fixed: lifecycle gate (E-7) first, then
//!   pool-specific arguments, then name / capacity / profile / factory kind
//!   (E-8, ADM-05); a native factory handle is recognised by type, any
//!   other callable is a Python factory.
//! - First-start factory failure raises `ActorSpawnError` with the factory
//!   exception as `__cause__` and leaves no record, generation, budget,
//!   event or dead letter behind (E-10, SUP-10).
//! - A serial actor's record is inserted before its pump starts and removed
//!   again if the pump cannot start; the pump is `spawn_linked` to the
//!   engine supervisor (6.5) and starts at generation 1 (5.1 row 1).
//! - A pool builds `pool_size` fresh handler instances up front, one per
//!   worker, since pools never restart (4, 9.3); if any worker thread
//!   cannot start, the pool accepts stop, its record is removed and the
//!   call raises `ActorSpawnError`.
//! - I2 keyed spawns claim a pair in a Starting slot before building the
//!   handler. Any other spawn sees the occupied slot and fails immediately
//!   with the stable `key-in-use` spawn refusal. There are no waiters or
//!   shared outcomes. After the factory returns, the starter re-checks the
//!   lifecycle gate (an early, non-final exit that keeps the shutdown
//!   refusal ahead of establishment errors and the conformance injection),
//!   then establishes and registers the pump while the pair is still
//!   Starting (no key lock held), and finally adjudicates in ONE key-index
//!   critical section: when the engine is still active and the slot is
//!   still this start's claim, the cell is installed together with the
//!   Live slot and spawn returns a fully live actor; otherwise shutdown (or
//!   an engine-wide drain) won, the established pump is torn down quietly
//!   (no event, no dead letters — no actor was ever visible) and spawn
//!   returns the shutdown refusal. A start that never installed
//!   successfully therefore never returns an id, `lookup` never exposes a
//!   starting pair, and a failed or cancelled start leaves no cell, key or
//!   pump-registry residue.
//!   Linearization (design decision 2026-11-20): a
//!   keyed spawn linearizes at the installation itself — the moment the
//!   cell is installed as Live under the key lock. The returned id states
//!   that this creation was successfully installed; a stop or shutdown
//!   that lands after the installation and before the caller observes the
//!   id is not distinguishable from — and needs no distinction from — the
//!   actor being stopped immediately after spawn returned, exactly like a
//!   keyless spawn. No publication protocol exists and shutdown does not
//!   wait for an in-flight spawn to return.
//!   the fourth review (2026-11-20) confirmed the previous order (install then start
//!   the pump) let a free-threaded shutdown or lookup cross that window.

use std::collections::VecDeque;
use std::sync::{Arc, Condvar, Mutex};
use std::time::Duration;

use hyprial_core::{CellState, RestartBudget};
use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyString;
use ractor::Actor;

use crate::cell::{lock, Cell, CellInner, StopControl};
use crate::engine::{EngineInner, Lifecycle};
use crate::errors::ActorSpawnError;
use crate::events::EventSinks;
use crate::handlers::{Handler, HandlerFactory};
use crate::pool::{run_worker, Pool, PoolInner};
use crate::pump::{PumpActor, PumpArgs, PumpMsg};

pub(crate) enum SpawnResult {
    Id(String),
}

/// Slot state of the engine key index.
#[derive(Clone)]
pub(crate) enum KeySlot {
    /// A first same-key spawn claimed the pair and is building/starting.
    Starting(Arc<StartingKey>),
    /// A started serial actor owns the pair.
    Live(Arc<Cell>),
}

/// Result of trying to claim a key pair for spawn.
pub(crate) enum KeyClaim {
    /// This caller is the starter and holds the returned slot.
    Start(Arc<StartingKey>),
    /// The pair is already occupied by a live or starting actor.
    Occupied,
}

/// The in-progress first start one key slot coordinates (round 2 S2/S5).
pub(crate) struct StartingKey;

/// Outcome of establishing one cell's linked pump.
enum Established {
    Started(ractor::ActorRef<PumpMsg>),
    Failed(String),
}

/// Which keyed-spawn park point one armed hold uses.
#[cfg(feature = "conformance-handlers")]
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum TestHoldPoint {
    /// Right after the spawnability gate, before the pump is established
    /// (`HYPRIAL_ACTOR_TEST_PUMP_GATE_HOLD`): the start is committed to
    /// establishing its pump, so a shutdown completing here must arbitrate
    /// against the establishment failure, not the gate refusal.
    PreEstablish,
    /// After the pump is established and registered, before the
    /// installation adjudication (`HYPRIAL_ACTOR_TEST_PUMP_START_HOLD`).
    PostEstablish,
}

/// Conformance-only controllable hold at one keyed-spawn park point
/// (`conformance-handlers` feature; never compiled into wheels). One env
/// var arms the one-shot hold at engine construction; the held spawn parks
/// with the GIL released until `_test_release_pump_start_hold` is called
/// (bounded, so a broken test cannot wedge the process), and
/// `_test_pump_start_hold_reached` lets the test wait until the spawn has
/// really reached the hold point.
#[cfg(feature = "conformance-handlers")]
pub(crate) struct TestPumpHold {
    point: Option<TestHoldPoint>,
    armed: std::sync::atomic::AtomicBool,
    state: Mutex<TestPumpHoldState>,
    cv: Condvar,
}

#[cfg(feature = "conformance-handlers")]
#[derive(Default)]
struct TestPumpHoldState {
    reached: bool,
    released: bool,
}

#[cfg(feature = "conformance-handlers")]
const TEST_HOLD_LIMIT: Duration = Duration::from_secs(30);

#[cfg(feature = "conformance-handlers")]
impl TestPumpHold {
    pub(crate) fn from_env() -> Self {
        let flagged = |name: &str| std::env::var(name).as_deref() == Ok("1");
        let point = if flagged("HYPRIAL_ACTOR_TEST_PUMP_START_HOLD") {
            Some(TestHoldPoint::PostEstablish)
        } else if flagged("HYPRIAL_ACTOR_TEST_PUMP_GATE_HOLD") {
            Some(TestHoldPoint::PreEstablish)
        } else {
            None
        };
        Self {
            point,
            armed: std::sync::atomic::AtomicBool::new(point.is_some()),
            state: Mutex::new(TestPumpHoldState::default()),
            cv: Condvar::new(),
        }
    }

    /// Park one armed keyed spawn at `point` with the GIL released until
    /// released (or the bounded limit elapses).
    pub(crate) fn hold_until_released(&self, py: Python<'_>, point: TestHoldPoint) {
        if self.point != Some(point) {
            return;
        }
        if !self.armed.swap(false, std::sync::atomic::Ordering::SeqCst) {
            return;
        }
        py.detach(|| {
            let mut state = lock(&self.state);
            state.reached = true;
            self.cv.notify_all();
            let deadline = std::time::Instant::now() + TEST_HOLD_LIMIT;
            while !state.released {
                let now = std::time::Instant::now();
                if now >= deadline {
                    break;
                }
                let (guard, timeout) = self
                    .cv
                    .wait_timeout(state, deadline - now)
                    .expect("test hold lock");
                state = guard;
                if timeout.timed_out() {
                    break;
                }
            }
        });
    }

    pub(crate) fn reached(&self) -> bool {
        lock(&self.state).reached
    }

    pub(crate) fn release(&self) {
        let mut state = lock(&self.state);
        state.released = true;
        self.cv.notify_all();
    }
}

impl EngineInner {
    /// E-7: `spawn` / `spawn_pool` raise `RuntimeError` during an
    /// in-progress engine-wide drain, after it completed, or after shutdown.
    pub(crate) fn require_spawnable(&self) -> PyResult<()> {
        match self.lifecycle() {
            Lifecycle::Active => Ok(()),
            Lifecycle::EngineWideDrain | Lifecycle::Drained => Err(PyRuntimeError::new_err(
                "engine-wide drain in progress or completed: spawn is refused",
            )),
        }
    }

    fn mint_id(&self, prefix: &str) -> String {
        let n = self
            .id_counter
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        format!("{prefix}-{n:08x}")
    }

    /// Build the first handler, then either (keyless) insert the record and
    /// start the linked pump, or (keyed) start and register the pump first
    /// and install cell + Live slot in one adjudicated key critical section.
    #[allow(clippy::too_many_arguments)]
    #[cfg_attr(not(feature = "conformance-handlers"), allow(unused_variables))]
    pub(crate) fn spawn_cell(
        self: &Arc<Self>,
        py: Python<'_>,
        name: String,
        factory: HandlerFactory,
        capacity: usize,
        policy_name: String,
        dead_letter_sink: Option<Py<PyAny>>,
        event_sink: Option<Py<PyAny>>,
        key_pair: Option<(String, String)>,
    ) -> PyResult<SpawnResult> {
        let starter = match key_pair.clone() {
            None => None,
            Some(pair) => match self.claim_key_start(pair.clone()) {
                KeyClaim::Occupied => return Err(key_in_use()),
                KeyClaim::Start(start) => Some((pair, start)),
            },
        };
        let handler = match crate::gate::attach(|py| factory.build(py)) {
            Ok(handler) => handler,
            Err(err) => {
                if let Some((pair, start)) = &starter {
                    self.retire_keyed_start(pair, start);
                }
                return Err(spawn_error(err));
            }
        };

        let policy = self
            .policies
            .lookup(&policy_name)
            .expect("profile validated")
            .clone();
        let id = self.mint_id("act");
        let sinks = EventSinks {
            spec_sink: event_sink,
            engine_sink: self.engine_sink.clone(),
        };
        let (group, key) = match &key_pair {
            Some((group, key)) => (Some(group.clone()), Some(key.clone())),
            None => (None, None),
        };
        let cell = Arc::new(Cell {
            id: id.clone(),
            name,
            group,
            key,
            capacity,
            policy: policy.clone(),
            factory,
            dead_letter_sink,
            sinks,
            inner: Mutex::new(CellInner {
                state: CellState::Running,
                generation: 1,
                queue: VecDeque::new(),
                in_flight: false,
                budget: RestartBudget::new(policy.max_restarts, policy.restart_window),
                pump: None,
                restart_timer: None,
                respawning: false,
                stop: StopControl::default(),
                draining: false,
                failure: None,
            }),
            cv: Condvar::new(),
        });
        let Some((pair, start)) = &starter else {
            self.cells
                .write()
                .expect("cells lock")
                .insert(id.clone(), cell.clone());
            return match self.establish_pump(&cell, handler) {
                Established::Started(pump_ref) => {
                    self.wire_pump(&cell, &pump_ref);
                    let _ = pump_ref.send_message(PumpMsg::Wake);
                    Ok(SpawnResult::Id(id))
                }
                Established::Failed(message) => {
                    self.remove_failed_cell(&cell, None);
                    Err(ActorSpawnError::new_err(message))
                }
            };
        };

        if self.lifecycle() != Lifecycle::Active {
            self.remove_failed_cell(&cell, Some((pair, start)));
            return Err(PyRuntimeError::new_err(
                "engine-wide drain in progress or completed: spawn is refused",
            ));
        }
        #[cfg(feature = "conformance-handlers")]
        self.test_pump_hold
            .hold_until_released(py, TestHoldPoint::PreEstablish);
        #[cfg(feature = "conformance-handlers")]
        if self
            .test_pump_start_failure
            .swap(false, std::sync::atomic::Ordering::SeqCst)
        {
            let message = format!("engine failed to start actor {id}: injected pump start failure");
            self.remove_failed_cell(&cell, Some((pair, start)));
            return Err(ActorSpawnError::new_err(message));
        }
        let pump_ref = match self.establish_pump(&cell, handler) {
            Established::Started(pump_ref) => pump_ref,
            Established::Failed(message) => {
                self.remove_failed_cell(&cell, Some((pair, start)));
                return Err(ActorSpawnError::new_err(message));
            }
        };
        self.wire_pump(&cell, &pump_ref);
        #[cfg(feature = "conformance-handlers")]
        self.test_pump_hold
            .hold_until_released(py, TestHoldPoint::PostEstablish);
        let installed = {
            let mut keyed = self.keyed.write().expect("keyed lock");
            let owns_start = matches!(
                keyed.get(pair),
                Some(KeySlot::Starting(current)) if Arc::ptr_eq(current, start)
            );
            if self.lifecycle() == Lifecycle::Active && owns_start {
                self.cells
                    .write()
                    .expect("cells lock")
                    .insert(id.clone(), cell.clone());
                keyed.insert(pair.clone(), KeySlot::Live(cell.clone()));
                true
            } else {
                if owns_start {
                    keyed.remove(pair);
                }
                false
            }
        };
        if !installed {
            {
                let mut inner = lock(&cell.inner);
                inner.state = CellState::Stopped;
                inner.pump = None;
            }
            pump_ref.stop(None);
            self.forget_pump(pump_ref.get_id());
            return Err(PyRuntimeError::new_err(
                "engine-wide drain in progress or completed: spawn is refused",
            ));
        }
        let _ = pump_ref.send_message(PumpMsg::Wake);
        Ok(SpawnResult::Id(id))
    }

    /// Start the linked pump of one freshly built cell (spec 6.5).
    fn establish_pump(self: &Arc<Self>, cell: &Arc<Cell>, handler: Handler) -> Established {
        let supervisor = self.supervisor_cell();
        let engine = self.clone();
        let actor = PumpActor {
            cell: cell.clone(),
            engine,
        };
        let future = PumpActor::spawn_linked(None, actor, PumpArgs { handler }, supervisor);
        match self.rt_block_on(future) {
            Some(Ok((pump_ref, _handle))) => Established::Started(pump_ref),
            Some(Err(err)) => {
                Established::Failed(format!("engine failed to start actor {}: {err}", cell.id))
            }
            None => Established::Failed(format!(
                "engine failed to start actor {}: runtime is shut down",
                cell.id
            )),
        }
    }

    /// Publish an established pump on its cell and in the supervision
    /// registry, so the actor is fully serviceable once it becomes visible.
    fn wire_pump(&self, cell: &Arc<Cell>, pump_ref: &ractor::ActorRef<PumpMsg>) {
        lock(&cell.inner).pump = Some(pump_ref.clone());
        self.remember_pump(pump_ref.get_id(), cell.clone());
    }

    /// The live serial actor under a key pair, if any. A starting first
    /// spawn and a stopped cell that has not been unindexed yet both report
    /// as absent to lookup.
    pub(crate) fn live_keyed_cell(&self, pair: &(String, String)) -> Option<Arc<Cell>> {
        let cell = match self.keyed.read().expect("keyed lock").get(pair) {
            Some(KeySlot::Live(cell)) => cell.clone(),
            _ => return None,
        };
        if lock(&cell.inner).state == CellState::Stopped {
            return None;
        }
        Some(cell)
    }

    /// Claim a key pair for a first spawn. Any existing slot is an immediate
    /// key-in-use refusal, including a starting actor and every non-stopped
    /// lifecycle state of a live actor.
    pub(crate) fn claim_key_start(&self, pair: (String, String)) -> KeyClaim {
        let mut map = self.keyed.write().expect("keyed lock");
        match map.get(&pair).cloned() {
            Some(KeySlot::Live(cell)) if lock(&cell.inner).state == CellState::Stopped => {
                map.remove(&pair);
            }
            Some(KeySlot::Live(_)) | Some(KeySlot::Starting(_)) => {
                return KeyClaim::Occupied;
            }
            None => {}
        }
        let start = Arc::new(StartingKey);
        map.insert(pair, KeySlot::Starting(start.clone()));
        KeyClaim::Start(start)
    }

    /// Drop a failed start's claim so the pair is immediately reusable.
    pub(crate) fn retire_keyed_start(&self, pair: &(String, String), start: &Arc<StartingKey>) {
        let mut map = self.keyed.write().expect("keyed lock");
        let ours = matches!(map.get(pair), Some(KeySlot::Starting(s)) if Arc::ptr_eq(s, start));
        if ours {
            map.remove(pair);
        }
    }

    fn remove_failed_cell(
        &self,
        cell: &Arc<Cell>,
        claim: Option<(&(String, String), &Arc<StartingKey>)>,
    ) {
        self.cells.write().expect("cells lock").remove(&cell.id);
        if let Some((pair, start)) = claim {
            self.retire_keyed_start(pair, start);
        }
    }

    /// Release a cell's key when it still owns the index entry (a stop
    /// confirmation raced by a same-key respawn changes nothing).
    pub(crate) fn release_key_if_held(&self, cell: &Arc<Cell>) {
        let Some(pair) = cell.key_pair() else {
            return;
        };
        let mut map = self.keyed.write().expect("keyed lock");
        let still_ours =
            matches!(map.get(&pair), Some(KeySlot::Live(held)) if Arc::ptr_eq(held, cell));
        if still_ours {
            map.remove(&pair);
        }
    }

    pub(crate) fn cancel_key_starts(&self) {
        self.keyed
            .write()
            .expect("keyed lock")
            .retain(|_, slot| matches!(slot, KeySlot::Live(_)));
    }

    /// Build one handler per worker, insert the record, start the workers.
    #[allow(clippy::too_many_arguments)]
    pub(crate) fn spawn_pool_actor(
        self: &Arc<Self>,
        name: String,
        factory: HandlerFactory,
        pool_size: usize,
        capacity: usize,
        dead_letter_sink: Option<Py<PyAny>>,
        event_sink: Option<Py<PyAny>>,
        retry_interval: Duration,
    ) -> PyResult<String> {
        let handlers: Vec<Handler> = crate::gate::attach(|py| {
            (0..pool_size)
                .map(|_| factory.build(py))
                .collect::<PyResult<Vec<_>>>()
        })
        .map_err(spawn_error)?;

        let id = self.mint_id("pool");
        let sinks = EventSinks {
            spec_sink: event_sink,
            engine_sink: self.engine_sink.clone(),
        };
        let pool = Arc::new(Pool {
            id: id.clone(),
            name,
            retry_interval,
            dead_letter_sink,
            sinks,
            inner: Mutex::new(PoolInner {
                state: CellState::Running,
                custody: hyprial_core::CustodyTable::new(capacity),
                queue: VecDeque::new(),
                in_flight: 0,
                draining: false,
                live_workers: pool_size,
                confirmed: false,
            }),
            cv: Condvar::new(),
        });
        self.pools
            .write()
            .expect("pools lock")
            .insert(id.clone(), pool.clone());

        for handler in handlers {
            let worker_pool = pool.clone();
            let engine = self.clone();
            let spawned =
                std::thread::Builder::new().spawn(move || run_worker(worker_pool, engine, handler));
            if let Err(err) = spawned {
                pool.accept_stop();
                pool.cv.notify_all();
                self.pools.write().expect("pools lock").remove(&id);
                return Err(ActorSpawnError::new_err(format!(
                    "engine failed to start pool worker: {err}"
                )));
            }
        }
        Ok(id)
    }
}

/// E-10: ActorSpawnError chains the factory exception as `__cause__`.
fn spawn_error(cause: PyErr) -> PyErr {
    let err = ActorSpawnError::new_err(FIRST_START_MESSAGE);
    crate::gate::attach(|py| {
        let value = err.value(py);
        let _ = value.setattr("__cause__", cause.value(py));
    });
    err
}

/// Message of the first-start factory failure.
pub(crate) const FIRST_START_MESSAGE: &str = "handler factory raised at first start";

fn key_in_use() -> PyErr {
    ActorSpawnError::new_err("key-in-use")
}

/// Shared argument validation for spawn/spawn_pool (E-8, ADM-05).
pub(crate) fn validate_and_classify_factory(
    py: Python<'_>,
    name: &str,
    capacity: i64,
    profile: &str,
    factory: Py<PyAny>,
    inner: &EngineInner,
) -> PyResult<HandlerFactory> {
    if name.trim().is_empty() {
        return Err(PyValueError::new_err("name must be non-blank"));
    }
    if capacity < 1 {
        return Err(PyValueError::new_err("capacity must be >= 1"));
    }
    if inner.policies.lookup(profile).is_none() {
        return Err(PyValueError::new_err(format!(
            "unknown policy profile {profile:?}"
        )));
    }
    HandlerFactory::from_object(py, factory, &inner.native_handle_type)
}

/// I2 key-pair validation: both halves or neither, strings, non-blank, no
/// folding. Non-string halves raise ValueError here (round 2 B2), not a
/// PyO3 conversion TypeError, so both backends report the same error class.
pub(crate) fn validate_key_pair(
    py: Python<'_>,
    group: Option<Py<PyAny>>,
    key: Option<Py<PyAny>>,
) -> PyResult<Option<(String, String)>> {
    let as_string = |value: Option<Py<PyAny>>| -> PyResult<Option<String>> {
        match value {
            None => Ok(None),
            Some(obj) => {
                let bound = obj.bind(py);
                if bound.is_instance_of::<PyString>() {
                    Ok(Some(bound.extract::<String>()?))
                } else {
                    Err(PyValueError::new_err("group and key must be strings"))
                }
            }
        }
    };
    let group = as_string(group)?;
    let key = as_string(key)?;
    match (group, key) {
        (None, None) => Ok(None),
        (Some(group), Some(key)) => {
            if group.trim().is_empty() {
                return Err(PyValueError::new_err("group must be a non-blank string"));
            }
            if key.trim().is_empty() {
                return Err(PyValueError::new_err("key must be a non-blank string"));
            }
            Ok(Some((group, key)))
        }
        (Some(_), None) | (None, Some(_)) => Err(PyValueError::new_err(
            "group and key must be given together",
        )),
    }
}
