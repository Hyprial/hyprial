//! `EffectCompleted` — the one concrete completion type (spec section 3.3).
//!
//! The engine constructs every instance; the pool delivers it to the owner
//! as a plain `tell` and never inspects its content afterwards.
//!
//! Decisions:
//! - The Python constructor follows the 3.3 signature exactly so callers
//!   (adapters, the conformance suite) can build completions for
//!   comparison; it performs no cross-field validation because the spec
//!   defines none, and `generation` is a non-negative `int` like the
//!   engine-built instances.
//! - `error` carries the exception type name only (`type(exc).__name__`, or
//!   `"PanicException"` for a caught Rust panic), never a message.

use pyo3::prelude::*;

#[pyclass(frozen, module = "hyprial._core.actors")]
#[derive(Clone)]
pub struct EffectCompleted {
    #[pyo3(get)]
    pub operation_id: String,
    #[pyo3(get)]
    pub generation: u64,
    #[pyo3(get)]
    pub result: Py<PyAny>,
    #[pyo3(get)]
    pub error: Option<String>,
}

#[pymethods]
impl EffectCompleted {
    /// `EffectCompleted(operation_id, generation, result=None, error=None)`
    /// (spec 3.3). The class stays frozen: attribute assignment raises.
    #[new]
    #[pyo3(signature = (operation_id, generation, result=None, error=None))]
    fn new(
        py: Python<'_>,
        operation_id: String,
        generation: u64,
        result: Option<Py<PyAny>>,
        error: Option<String>,
    ) -> Self {
        EffectCompleted {
            operation_id,
            generation,
            result: result.unwrap_or_else(|| py.None()),
            error,
        }
    }

    pub fn __repr__(&self, py: Python<'_>) -> String {
        let result_repr = match self.result.bind(py).repr() {
            Ok(r) => r.to_string(),
            Err(_) => "<unreprable>".to_string(),
        };
        match &self.error {
            Some(err) => format!(
                "EffectCompleted(operation_id={:?}, generation={}, result=None, error={:?})",
                self.operation_id, self.generation, err
            ),
            None => format!(
                "EffectCompleted(operation_id={:?}, generation={}, result={}, error=None)",
                self.operation_id, self.generation, result_repr
            ),
        }
    }
}
