//! Event tuple stream (spec section 11).
//!
//! Events are best-effort diagnostics, never control flow. Every callback
//! into a sink happens with the GIL held and no engine lock held; sink
//! exceptions are swallowed and change nothing (EVT-03). Routing: the
//! spec-level (per-actor) sink first, then the engine-level sink (EVT-02).
//!
//! Decisions (I2 keyed actors): a keyed actor's tuple carries its
//! (group, key) appended after `restart_delay`; a keyless actor's tuple
//! stays the frozen eight fields so existing consumers see no change.

use pyo3::prelude::*;
use pyo3::types::PyTuple;
use pyo3::IntoPyObjectExt;

use crate::cell::{lock, Cell};
use crate::engine::EngineInner;
use crate::pool::Pool;

/// Sinks attached to one actor (spawn-time) plus the engine-level sink.
#[derive(Clone)]
pub(crate) struct EventSinks {
    /// Spawn-time `event_sink`; receives this actor's events first.
    pub spec_sink: Option<Py<PyAny>>,
    /// Engine-level sink; receives every event.
    pub engine_sink: Py<PyAny>,
}

/// One event emission. `code`/`detail` are already prepared Python objects
/// (expected-error attributes pass through verbatim; the failure code is
/// the exception type name as a string).
pub(crate) struct EventValues<'a> {
    pub kind: &'a str,
    pub actor_id: &'a str,
    pub name: &'a str,
    pub generation: u64,
    /// Associated command; `command_type` is `type(command).__name__`.
    pub command: Option<&'a Py<PyAny>>,
    /// Overrides the derived command_type (used when the command was
    /// dropped, e.g. restart-factory failures report None).
    pub command_type_override: Option<Option<String>>,
    pub code: Option<Py<PyAny>>,
    pub detail: Option<Py<PyAny>>,
    pub restart_delay: Option<f64>,
    /// I2 keyed identity: both present exactly for a keyed actor; the two
    /// elements are appended after `restart_delay` so a keyless event tuple
    /// keeps the frozen eight fields byte-identically.
    pub group: Option<&'a str>,
    pub key: Option<&'a str>,
}

pub(crate) fn obj<'py, T: IntoPyObjectExt<'py>>(py: Python<'py>, value: T) -> Py<PyAny> {
    value
        .into_bound_py_any(py)
        .expect("infallible conversion")
        .unbind()
}

fn call_sink(py: Python<'_>, sink: &Py<PyAny>, event: &Bound<'_, PyTuple>) {
    // EVT-03: sink exceptions are swallowed and must not affect the engine.
    let _ = sink.bind(py).call1((event,));
}

pub(crate) fn emit(py: Python<'_>, sinks: &EventSinks, values: EventValues<'_>) {
    emit_selected(py, sinks, values, true, true);
}

pub(crate) fn emit_selected(
    py: Python<'_>,
    sinks: &EventSinks,
    values: EventValues<'_>,
    emit_spec: bool,
    emit_engine: bool,
) {
    let command_type: Option<String> = match values.command_type_override {
        Some(overridden) => overridden,
        None => values.command.map(|command| type_name(command.bind(py))),
    };
    let mut elements: Vec<Py<PyAny>> = vec![
        obj(py, values.kind),
        obj(py, values.actor_id),
        obj(py, values.name),
        obj(py, values.generation),
        obj(py, command_type),
        obj(py, values.code),
        obj(py, values.detail),
        obj(py, values.restart_delay),
    ];
    if let (Some(group), Some(key)) = (values.group, values.key) {
        elements.push(obj(py, group));
        elements.push(obj(py, key));
    }
    let tuple = PyTuple::new(py, elements).expect("event tuple construction");
    if emit_spec {
        if let Some(spec) = &sinks.spec_sink {
            call_sink(py, spec, &tuple);
        }
    }
    if emit_engine {
        call_sink(py, &sinks.engine_sink, &tuple);
    }
}

impl EngineInner {
    pub(crate) fn emit_child_stopped(&self, py: Python<'_>, cell: &Cell) {
        let generation = lock(&cell.inner).generation;
        let (group, key) = cell
            .keyed()
            .map_or((None, None), |(g, k)| (Some(g), Some(k)));
        emit(
            py,
            &cell.sinks,
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
}

/// `type(command).__name__` for the event's `command_type` field.
pub(crate) fn type_name(obj: &Bound<'_, PyAny>) -> String {
    match obj.get_type().name() {
        Ok(name) => name.to_string(),
        Err(_) => "<unknown>".to_string(),
    }
}

/// Exception type name for failure events (`code` field).
pub(crate) fn exception_type_name(err: &Bound<'_, PyAny>) -> String {
    type_name(err)
}
