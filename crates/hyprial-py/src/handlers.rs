//! Handler kinds (spec section 4, D1).
//!
//! Two kinds exist — a Python callable and a Rust-native handler — and one
//! code path consumes them: [`Handler::call`] is the single branching point
//! at the invocation boundary; no policy code branches on the kind.
//!
//! Decisions:
//! - Factory builds of both kinds run under `catch_unwind` in
//!   [`HandlerFactory::build`], the one place every start and restart
//!   passes through: a panic becomes a `PanicException` `PyErr`, so first
//!   start raises `ActorSpawnError` chaining it as `__cause__` and a restart
//!   takes the row-4b child-failure path with code `PanicException`; no
//!   panic unwinds into the caller, the supervisor actor, or across the FFI.

use std::collections::{HashMap, HashSet};
use std::panic::AssertUnwindSafe;
use std::sync::{Arc, Mutex, OnceLock, RwLock};

use pyo3::exceptions::{PyImportError, PyValueError};
use pyo3::panic::PanicException;
use pyo3::prelude::*;

use crate::pump::panic_message;

/// Rust-native handler shape (normative in spec section 4).
pub trait NativeHandler: Send + Sync {
    fn handle(&self, command: Py<PyAny>) -> PyResult<Py<PyAny>>;
}

/// Rust-native factory shape (normative in spec section 4).
pub trait NativeHandlerFactory: Send + Sync {
    fn build(&self) -> PyResult<Box<dyn NativeHandler>>;
}

/// A built handler instance: exactly one per actor generation (restarts
/// always rebuild through the factory).
pub(crate) enum Handler {
    Python(Py<PyAny>),
    Native(Box<dyn NativeHandler>),
}

impl Handler {
    /// Call `handler(command)`. The caller holds the GIL and no engine lock.
    pub(crate) fn call(&self, py: Python<'_>, command: Py<PyAny>) -> PyResult<Py<PyAny>> {
        match self {
            Handler::Python(cb) => {
                let bound = cb.bind(py);
                let result = bound.call1((command.bind(py),))?;
                Ok(result.unbind())
            }
            Handler::Native(native) => native.handle(command),
        }
    }
}

/// The factory a spawn call receives, in either kind.
pub(crate) enum HandlerFactory {
    Python(Py<PyAny>),
    Native(Arc<dyn NativeHandlerFactory>),
}

impl HandlerFactory {
    /// Classify the Python-level `factory` argument: a native factory handle
    /// obtained from `native_handler_factory(name)`, or any callable.
    pub(crate) fn from_object(
        py: Python<'_>,
        factory: Py<PyAny>,
        native_handle_type: &Py<PyAny>,
    ) -> PyResult<HandlerFactory> {
        let bound = factory.bind(py);
        if bound.is_instance(native_handle_type.bind(py))? {
            let handle = bound.extract::<PyRef<'_, NativeHandlerFactoryHandle>>()?;
            return Ok(HandlerFactory::Native(handle.0.clone()));
        }
        if bound.is_callable() {
            return Ok(HandlerFactory::Python(factory));
        }
        Err(PyValueError::new_err(
            "handler_factory must be a callable or a NativeHandlerFactory",
        ))
    }

    /// Build one handler instance. Called exactly once per start and once
    /// per restart; a raising factory is classified by the caller (6.2).
    /// A Rust panic during the build, including one PyO3 resumes from a
    /// Python-raised `PanicException`, is caught here and returned as a
    /// `PanicException` error carrying the panic message (E-9).
    pub(crate) fn build(&self, py: Python<'_>) -> PyResult<Handler> {
        std::panic::catch_unwind(AssertUnwindSafe(|| self.build_unguarded(py)))
            .unwrap_or_else(|payload| Err(PanicException::new_err(panic_message(payload))))
    }

    fn build_unguarded(&self, py: Python<'_>) -> PyResult<Handler> {
        match self {
            HandlerFactory::Python(f) => {
                let instance = f.bind(py).call0()?;
                Ok(Handler::Python(instance.unbind()))
            }
            HandlerFactory::Native(native) => native.build().map(Handler::Native),
        }
    }
}

/// Python-visible handle around a Rust-registered factory. Python can only
/// look these up; there is no Python-side registration entry point.
#[pyclass(frozen, name = "NativeHandlerFactory", module = "hyprial._core.actors")]
pub struct NativeHandlerFactoryHandle(pub(crate) Arc<dyn NativeHandlerFactory>);

/// Rust-side registry keyed by name. One registry exists per process: it
/// is populated once during module init and every `Engine` looks factories
/// up in it.
#[derive(Default)]
struct NativeRegistry {
    entries: RwLock<HashMap<String, Arc<dyn NativeHandlerFactory>>>,
}

impl NativeRegistry {
    fn lookup(&self, name: &str) -> Option<Arc<dyn NativeHandlerFactory>> {
        self.entries
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .get(name)
            .cloned()
    }

    /// Insert a batch atomically: a name that is already registered, or
    /// that repeats inside the batch, rejects the whole batch and is
    /// returned as the error.
    fn insert_all(&self, staged: StagedFactories) -> Result<(), String> {
        let mut entries = self
            .entries
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let mut seen = HashSet::new();
        for (name, _) in &staged {
            if entries.contains_key(name) || !seen.insert(name.as_str()) {
                return Err(name.clone());
            }
        }
        for (name, factory) in staged {
            entries.insert(name, factory);
        }
        Ok(())
    }
}

fn registry() -> &'static NativeRegistry {
    static REGISTRY: OnceLock<NativeRegistry> = OnceLock::new();
    REGISTRY.get_or_init(NativeRegistry::default)
}

/// Staging list filled by `register_native_factory` before the module
/// registry is populated; module init moves it into the process registry.
type StagedFactories = Vec<(String, Arc<dyn NativeHandlerFactory>)>;

fn staging() -> &'static Mutex<StagedFactories> {
    static STAGING: OnceLock<Mutex<StagedFactories>> = OnceLock::new();
    STAGING.get_or_init(|| Mutex::new(Vec::new()))
}

/// Register a native handler factory from Rust code, before the extension
/// module is initialized (i.e. before Python imports it). Duplicate names
/// abort module init.
pub fn register_native_factory(name: &str, factory: Arc<dyn NativeHandlerFactory>) {
    staging()
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .push((name.to_string(), factory));
}

/// Module init: move the staged factories into the process registry. A
/// duplicate name fails module init with `ImportError` and registers
/// nothing from that batch (spec section 4).
pub(crate) fn populate_registry() -> PyResult<()> {
    let staged = std::mem::take(
        &mut *staging()
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()),
    );
    registry().insert_all(staged).map_err(|name| {
        PyImportError::new_err(format!("duplicate native handler factory name: {name:?}"))
    })
}

/// `Engine.native_handler_factory(name)`: lookup in the process registry.
pub(crate) fn lookup_native_factory(name: &str) -> Option<Arc<dyn NativeHandlerFactory>> {
    registry().lookup(name)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Inert;

    impl NativeHandlerFactory for Inert {
        fn build(&self) -> PyResult<Box<dyn NativeHandler>> {
            Err(PyValueError::new_err("inert"))
        }
    }

    fn staged(names: &[&str]) -> StagedFactories {
        names
            .iter()
            .map(|name| {
                let factory: Arc<dyn NativeHandlerFactory> = Arc::new(Inert);
                (name.to_string(), factory)
            })
            .collect()
    }

    #[test]
    fn lookups_repeat_after_population() {
        let registry = NativeRegistry::default();
        assert!(registry.insert_all(staged(&["a", "b"])).is_ok());
        assert!(registry.lookup("a").is_some());
        assert!(registry.lookup("a").is_some());
        assert!(registry.lookup("b").is_some());
        assert!(registry.lookup("missing").is_none());
    }

    #[test]
    fn duplicate_inside_batch_rejects_whole_batch() {
        let registry = NativeRegistry::default();
        assert_eq!(
            registry.insert_all(staged(&["a", "b", "a"])),
            Err("a".to_string())
        );
        assert!(registry.lookup("b").is_none());
    }

    #[test]
    fn duplicate_of_registered_name_is_rejected() {
        let registry = NativeRegistry::default();
        assert!(registry.insert_all(staged(&["a"])).is_ok());
        assert_eq!(
            registry.insert_all(staged(&["c", "a"])),
            Err("a".to_string())
        );
        assert!(registry.lookup("c").is_none());
    }
}
