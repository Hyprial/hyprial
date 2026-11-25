//! `hyprial._core.actors` — the native actor engine (ENGINE_ABI_VERSION 1).
//!
//! The engine hosts many actors inside one CPython process: bounded
//! mailboxes with strict per-actor FIFO, one fixed supervision state machine
//! composed on Ractor primitives, dead letters, generation-tagged one-shot
//! timers, worker-pool actors with custody-token admission, `ask`, an event
//! tuple stream, and Python-callable as well as Rust-native handlers.
//!
//! The frozen interface is
//! `docs/design/actor-engine-native-abi-spec-2026-11-01.md`.
//!
//! Decisions:
//! - The native handler registry is process-global: module init stages the
//!   optional test-only conformance factories (`conformance-handlers`
//!   feature, CL-2), then populates the registry exactly once, before any
//!   class is added; every `Engine` looks factories up there, so a second
//!   `Engine` in the same process sees the same names as the first.
//! - A duplicate factory name fails module init with `ImportError` instead
//!   of panicking (spec section 4: duplicate names abort module init).
//! - The exported classes `Engine`, `EffectCompleted` and
//!   `NativeHandlerFactory` declare `module = "hyprial._core.actors"`, so
//!   their `__module__` and qualified names name the real module (section 3).
//! - Module init registers the interpreter-exit attach gate with `atexit`
//!   (`gate.rs`, spec 12, LIFE-01); the callback is not a module attribute.

mod cell;
#[cfg(feature = "conformance-handlers")]
mod conformance;
mod effects;
mod engine;
mod errors;
mod events;
mod execution;
mod gate;
mod handlers;
mod lifecycle;
mod pool;
mod pump;
mod spawn;
mod supervisor;
mod timers;

use pyo3::prelude::*;

/// Rust-side registration entry point for native handler factories. Rust
/// code registers each factory once, during module init, before the Python
/// adapter wires anything; duplicate names abort module init (spec 4).
pub use handlers::register_native_factory;

#[pymodule]
fn actors(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // FREE-03: PyO3 >= 0.28 declares the module GIL-compatible by default
    // (gil_used = false), so importing on a free-threaded build does not
    // re-enable the GIL because of this module.
    #[cfg(feature = "conformance-handlers")]
    conformance::register();
    handlers::populate_registry()?;
    gate::register_exit_hook(m)?;
    m.add("ENGINE_ABI_VERSION", 1u32)?;
    m.add_class::<engine::Engine>()?;
    m.add_class::<effects::EffectCompleted>()?;
    m.add_class::<handlers::NativeHandlerFactoryHandle>()?;
    m.add(
        "ActorAdmissionError",
        m.py().get_type::<errors::ActorAdmissionError>(),
    )?;
    m.add(
        "ActorFailedError",
        m.py().get_type::<errors::ActorFailedError>(),
    )?;
    m.add(
        "ActorSpawnError",
        m.py().get_type::<errors::ActorSpawnError>(),
    )?;
    Ok(())
}
