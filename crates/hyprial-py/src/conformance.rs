//! Test-only native handlers for the black-box conformance suite (spec
//! clarification CL-2). Compiled only with the `conformance-handlers` cargo
//! feature, which wheels never enable.
//!
//! Decisions:
//! - Injection goes through the Rust-side `register_native_factory` during
//!   module init, before the process registry is populated; it is not a
//!   public ABI entry point and adds no Python-visible registration surface.
//! - Two factories are registered, mirroring the suite's Python twins in
//!   `conformance/actor_engine/backends/__init__.py`:
//!   - `conformance.trampoline` (twin `trampoline`): a `str` command equal
//!     to `"panic"` panics with `"conformance panic"`, a callable command
//!     is called with no arguments and its result (or its Python
//!     exception) is returned, anything else is echoed back;
//!   - `conformance.env_panic_factory` (twin `env_panic_build`): `build()`
//!     panics with `"conformance factory panic"` iff the process environment
//!     variable `HYPRIAL_CONFORMANCE_BUILD_PANIC` equals `"1"` when `build()`
//!     runs (read with `std::env::var`, which sees `os.environ` changes made
//!     through `putenv`/`unsetenv`); otherwise it builds the trampoline. It
//!     lets one case observe a factory panic at first start (SUP-10) and at
//!     restart (SUP-06, E-9) without a process-global build counter.
//! - The `str` test is `isinstance` (subclasses included) and the equality
//!   is Python `==`, matching the twin's `isinstance(command, str) and
//!   command == "panic"`; an exception raised by that comparison propagates.
//! - Panics are deliberately left to the engine's catch points, so the suite
//!   observes the serial child-failure path, the pool's
//!   `EffectCompleted(error="PanicException")` row and the factory-panic
//!   classification of 6.2 (E-9).
//! - Every successful `build()` returns a fresh, stateless handler instance
//!   (spec 4).

use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::PyString;

use crate::handlers::{register_native_factory, NativeHandler, NativeHandlerFactory};

/// Registry name of the trampoline factory.
pub(crate) const TRAMPOLINE: &str = "conformance.trampoline";

/// Registry name of the environment-controlled panicking factory.
pub(crate) const ENV_PANIC_FACTORY: &str = "conformance.env_panic_factory";

/// Environment variable that makes `ENV_PANIC_FACTORY`'s `build()` panic.
pub(crate) const BUILD_PANIC_ENV: &str = "HYPRIAL_CONFORMANCE_BUILD_PANIC";

struct Trampoline;

impl NativeHandler for Trampoline {
    fn handle(&self, command: Py<PyAny>) -> PyResult<Py<PyAny>> {
        Python::attach(|py| {
            let bound = command.bind(py);
            if bound.is_instance_of::<PyString>() && bound.eq("panic")? {
                panic!("conformance panic");
            }
            if bound.is_callable() {
                return Ok(bound.call0()?.unbind());
            }
            Ok(command)
        })
    }
}

struct TrampolineFactory;

impl NativeHandlerFactory for TrampolineFactory {
    fn build(&self) -> PyResult<Box<dyn NativeHandler>> {
        Ok(Box::new(Trampoline))
    }
}

struct EnvPanicFactory;

impl NativeHandlerFactory for EnvPanicFactory {
    fn build(&self) -> PyResult<Box<dyn NativeHandler>> {
        if std::env::var(BUILD_PANIC_ENV).as_deref() == Ok("1") {
            panic!("conformance factory panic");
        }
        Ok(Box::new(Trampoline))
    }
}

/// Stage the conformance factories; module init populates the registry
/// right after this call.
pub(crate) fn register() {
    register_native_factory(TRAMPOLINE, Arc::new(TrampolineFactory));
    register_native_factory(ENV_PANIC_FACTORY, Arc::new(EnvPanicFactory));
}
