//! The interpreter-exit attach gate (spec section 12: the engine MUST NOT
//! acquire the GIL after interpreter finalization has begun; actor threads
//! are daemon threads; LIFE-01).
//!
//! Every engine-owned thread (serial executors, pool workers, the timer
//! thread, the tokio workers that run the supervisor) attaches to the
//! interpreter only through [`attach`]. Threads that called in from Python
//! keep plain `Python::attach` semantics.
//!
//! Decisions:
//! - On CPython 3.12/3.13 a thread that takes the GIL after finalization
//!   began is ended with `pthread_exit`, a glibc forced unwind. Any
//!   `catch_unwind` on that stack (the engine's E-9 panic fences, Ractor's
//!   handler fence, tokio's task harness) then aborts the process with
//!   "FATAL: exception not rethrown" (SIGABRT). Checking `Py_IsFinalizing`
//!   before attaching is racy and does not cover a handler that releases
//!   the GIL inside Python and re-takes it later, so the engine closes its
//!   own gate BEFORE finalization starts instead.
//! - The gate closes from an `atexit` callback that module init registers.
//!   `atexit` runs before `Py_FinalizeEx` marks the runtime finalizing and
//!   after `threading._shutdown`; callbacks run last-registered-first, so
//!   an adapter hook that calls `shutdown` (registered after the module is
//!   imported) runs before the gate closes.
//! - Closing the gate refuses every new outermost attach of an engine
//!   thread and then waits, with the GIL released, until the engine threads
//!   that are inside an attach scope leave it, bounded by
//!   [`EXIT_ATTACH_GRACE`]. Interpreter exit is therefore delayed at most
//!   that long by a handler that is still running; a handler still inside
//!   Python after the grace is the one case left to CPython's own thread
//!   exit.
//! - A refused engine thread parks forever, like CPython 3.14's
//!   `PyThread_hang_thread`: it never touches the interpreter again and the
//!   process exit reclaims it. It holds no thread state while parked.
//! - A nested attach on an engine thread (a native handler attaching inside
//!   the executor's scope, an engine call made from inside a handler) is
//!   never refused: the outermost scope already counts that thread, and
//!   refusing while the GIL may be held would wedge the exiting thread.
//! - The gate never relies on the GIL: its state is one engine-owned mutex
//!   and condition variable (D12).

use std::cell::Cell;
use std::sync::{Condvar, Mutex};
use std::time::{Duration, Instant};

use pyo3::prelude::*;

use crate::cell::lock;

/// Upper bound on how long the exit gate waits for engine threads that are
/// inside the interpreter when the `atexit` callback runs.
pub(crate) const EXIT_ATTACH_GRACE: Duration = Duration::from_secs(2);

struct GateState {
    closed: bool,
    attached: usize,
}

static GATE: Mutex<GateState> = Mutex::new(GateState {
    closed: false,
    attached: 0,
});
static GATE_CV: Condvar = Condvar::new();

thread_local! {
    static ENGINE_THREAD: Cell<bool> = const { Cell::new(false) };
    static DEPTH: Cell<usize> = const { Cell::new(0) };
}

/// Mark the calling thread as engine-owned: its attaches go through the
/// gate from now on.
pub(crate) fn mark_engine_thread() {
    ENGINE_THREAD.with(|flag| flag.set(true));
}

/// Leaves one attach scope of an engine thread; the outermost one also
/// leaves the gate count (runs on unwind too).
struct Scope {
    outermost: bool,
}

impl Drop for Scope {
    fn drop(&mut self) {
        DEPTH.with(|depth| depth.set(depth.get() - 1));
        if self.outermost {
            let mut gate = lock(&GATE);
            gate.attached -= 1;
            drop(gate);
            GATE_CV.notify_all();
        }
    }
}

/// `Python::attach` for engine code. On an engine-owned thread whose
/// outermost attach finds the gate closed, the thread parks forever and
/// this call never returns.
pub(crate) fn attach<F, R>(f: F) -> R
where
    F: for<'py> FnOnce(Python<'py>) -> R,
{
    if !ENGINE_THREAD.with(|flag| flag.get()) {
        return Python::attach(f);
    }
    let outermost = DEPTH.with(|depth| depth.get() == 0);
    if outermost {
        let mut gate = lock(&GATE);
        if gate.closed {
            drop(gate);
            park_forever();
        }
        gate.attached += 1;
    }
    DEPTH.with(|depth| depth.set(depth.get() + 1));
    let _scope = Scope { outermost };
    Python::attach(f)
}

fn park_forever() -> ! {
    loop {
        std::thread::park();
    }
}

/// Close the gate and wait (GIL released) for attached engine threads to
/// leave, at most [`EXIT_ATTACH_GRACE`].
fn close_and_wait() {
    let deadline = Instant::now() + EXIT_ATTACH_GRACE;
    let mut gate = lock(&GATE);
    gate.closed = true;
    while gate.attached > 0 {
        let now = Instant::now();
        if now >= deadline {
            return;
        }
        gate = GATE_CV
            .wait_timeout(gate, deadline - now)
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .0;
    }
}

/// The `atexit` callback registered by module init.
#[pyfunction]
#[pyo3(name = "_close_engine_attach_gate")]
fn close_engine_attach_gate(py: Python<'_>) {
    py.detach(close_and_wait);
}

/// Register the exit gate with `atexit` (module init).
pub(crate) fn register_exit_hook(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let hook = wrap_pyfunction!(close_engine_attach_gate, m)?;
    m.py().import("atexit")?.call_method1("register", (hook,))?;
    Ok(())
}
