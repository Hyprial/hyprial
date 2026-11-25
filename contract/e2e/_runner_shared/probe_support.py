"""Bounded probe diagnostics, loopback allocation and failure-safe teardown."""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager

def bounded_repr(value: object, *, per_key: int = 220) -> str:
    """Render a probe result so every key survives the report's length cap.

    ⚠️ This exists because of a specific loss. `Report.add` trims a detail to a
    thousand characters, and `hyprial daemon status --json` puts `daemon` and `zenoh` before
    `connectors`. Since endpoints stopped being persisted, that `zenoh` value
    carries forty-odd tailnet addresses and eats the whole budget on its own --
    so the E2E-006 and E2E-012 restart-restore failures reported the daemon as
    `running: True` and were cut off **before** the connector status, which is
    the only thing `wait_running` was actually waiting on.

    🔑 The half that survived made the failure look like a healthy daemon. The
    half that explained it was never written. Giving each key its own budget
    costs nothing and makes the truncation unable to choose which key to lose.
    """

    if not isinstance(value, dict):
        text = repr(value)
        return text if len(text) <= per_key * 4 else text[: per_key * 4] + "…"
    parts: list[str] = []
    for key, item in value.items():
        text = repr(item)
        if len(text) > per_key:
            text = f"{text[:per_key]}…(+{len(text) - per_key} chars)"
        parts.append(f"{key!r}: {text}")
    return "{" + ", ".join(parts) + "}"

def free_tcp_endpoint() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    return f"tcp/127.0.0.1:{port}"

@contextmanager
def teardown_guard(
    *steps: Callable[[], None],
    record: Callable[[str], None] | None = None,
) -> Iterator[None]:
    """Run teardown ``steps`` without letting them mask the scenario's failure.

    The defect this closes: a bare ``finally: daemon.stop()`` runs while the
    stack is already unwinding a scenario failure, and when that teardown *also*
    raises (e.g. a wedged daemon whose ``stop`` IPC times out) the teardown
    exception REPLACES the original one -- so the runner reports the teardown
    error and the true cause is lost (E2E-007 needed temporary instrumentation
    to recover it).

    Behaviour:
      * If the body raised, every step is still attempted, each step error is
        handed to ``record`` (so it lands as its own report line rather than
        being dropped), and the ORIGINAL exception is re-raised unchanged --
        it is what the caller reports.
      * If the body completed cleanly, every step is attempted and its error
        recorded; the first such error is then re-raised so a broken teardown
        still fails the run.

    Steps are caught at ``BaseException`` so one step failing never skips the
    remaining steps, and a ``KeyboardInterrupt``-derived body failure (e.g. the
    ``RunnerInterrupted`` some runners raise from signal handlers) still runs
    teardown before propagating.
    """

    def _drain() -> list[BaseException]:
        errors: list[BaseException] = []
        for step in steps:
            try:
                step()
            except BaseException as error:  # noqa: BLE001 - teardown must not abort teardown
                errors.append(error)
                if record is not None:
                    try:
                        record(f"{type(error).__name__}: {error}")
                    except BaseException:  # noqa: BLE001
                        # Recording (e.g. a print to a closed pipe) must never
                        # replace the failure it is trying to record.
                        pass
        return errors

    try:
        yield
    except BaseException:
        # Body already failed: record teardown errors but re-raise the original.
        _drain()
        raise
    # Clean body: a teardown failure is itself the run's failure.
    errors = _drain()
    if errors:
        raise errors[0]
