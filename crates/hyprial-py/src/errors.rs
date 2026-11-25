//! Python exception types exported by `hyprial._core.actors` (spec section 13).

use pyo3::create_exception;

create_exception!(
    actors,
    ActorAdmissionError,
    pyo3::exceptions::PyException,
    "Admission refused for `ask`: raised with the reason string `overloaded` or `closed`."
);

create_exception!(
    actors,
    ActorFailedError,
    pyo3::exceptions::PyException,
    "The target actor suffered a supervised child failure while an `ask` was waiting."
);

create_exception!(
    actors,
    ActorSpawnError,
    pyo3::exceptions::PyException,
    "The handler factory raised at first start; no actor record was created. Chains the factory exception as `__cause__`."
);
