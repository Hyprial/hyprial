"""Waits that cannot become wall-clock bets.

2026-09-18: five test fixes in one night had the same shape -- a test slept a
literal duration, then asserted something about state.  The duration was
simultaneously the defect (too small under load -> false red) and the whole
detection power (too small to let a broken system show -> vacuous pass).  A
census of ``tests/`` found 26 of 381 files carrying that template.

⛔ SCOPE, stated first so nobody scores the wrong reds against this module:
this replaces the TEMPLATE.  It does not touch product defects (a real bug in
``channel.py``) or resource contention (``sqlite3.OperationalError: database
is locked``).  Reds from those two classes survive this module by design.

Three properties are enforced by the API rather than by discipline:

* a wait that could not have failed raises :class:`VacuousWait` -- so a
  rewrite that preserves only "no false red" breaks on EVERY run, instead of
  going quiet until the day a real defect slips through;
* a bound is DERIVED from product constants; the caller never writes a
  number, and the path must resolve inside the product package, so a bound
  cannot drift from the product's -- nor be faked by parking a literal in
  ``tests/`` under a constant-shaped name (the first version of this module
  only checked "imports and is numeric", and its own tests were the first
  thing to walk through that hole);
* the one legitimate literal -- "prove this does NOT happen" -- must say so
  in code, which is also how the lint gate recognises it.
"""

from __future__ import annotations

import importlib
import time
from collections.abc import Callable
from dataclasses import dataclass

#: How often a poll-based wait re-reads its condition.  An implementation
#: detail of *this module*: call sites no longer spell a poll interval, which
#: is what made ``time.sleep(0.005)`` appear ten times in one test file.
_POLL_SECONDS = 0.005

#: A budget must be owned by the PRODUCT.  Enforced, not merely documented:
#: the first version of this module only checked that the path imported and
#: was numeric, so a constant parked in ``tests/`` satisfied it -- the same
#: wall-clock bet wearing a longer name.  This module's own tests were the
#: first thing to exploit that hole, which is how it was found.
_PRODUCT_ROOT = "hyprial."

#: Clock seams for THIS module's own tests.  A helper that waits cannot test
#: itself against the wall clock: the first version timed a 0.01s timer
#: against a 0.05s budget and went red on a loaded CI runner -- the defect
#: this module exists to delete, shipped inside it.  Tests patch these; call
#: sites never see them, so the public signatures stay free of a clock knob.
_monotonic = time.monotonic
_sleep = time.sleep


class VacuousWait(AssertionError):
    """A wait that proved nothing on this run.

    Raised when the awaited condition already held before waiting: a broken
    system and a working one would have produced the same observation, so
    this run carried no information about the thing under test.
    """


class WaitTimeout(AssertionError):
    """The awaited state never arrived inside the product's own budget."""


def _resolve(path: str) -> float:
    """Import ``package.module.CONSTANT`` and return its value.

    ⛔ Never falls back to a caller-supplied number.  A renamed or deleted
    constant must fail loudly here: the failure mode this guards against is
    a silent return to a wall-clock bet on the day someone renames it.
    """

    module_path, _, name = path.rpartition(".")
    if not module_path or not name:
        raise ValueError(f"budget constant must be a dotted import path, got {path!r}")
    if not path.startswith(_PRODUCT_ROOT):
        raise ValueError(
            f"budget constant {path!r} is not under {_PRODUCT_ROOT!r}. A bound has to be "
            "the product's own declaration of its patience; a constant defined in the "
            "test suite is the same wall-clock guess with a longer name, and it drifts "
            "from the product silently because nothing ties the two together. If the "
            "product states this budget only as a function default, promote it to a "
            "module-level constant and point here at that -- that moves an existing "
            "declaration, it does not invent one."
        )
    try:
        module = importlib.import_module(module_path)
    except ImportError as error:  # pragma: no cover - exercised via tests
        raise ImportError(
            f"budget constant {path!r} names a module that cannot be imported; "
            "the bound must come from the product, so this is not recoverable "
            "by substituting a literal"
        ) from error
    try:
        value = getattr(module, name)
    except AttributeError as error:
        raise AttributeError(
            f"budget constant {path!r} does not exist. If it was renamed, point "
            "this budget at the new name -- do NOT replace it with a literal, "
            "which is how this file's whole defect class comes back."
        ) from error
    if not isinstance(value, (int, float)):
        raise TypeError(f"budget constant {path!r} is {type(value).__name__}, not a number")
    return float(value)


@dataclass(frozen=True)
class Budget:
    """An upper bound that can say where it came from.

    ⛔ Not constructible from a bare number: the two constructors below are
    the only ways in, and they record provenance that a reader can check.
    """

    seconds: float
    #: Human-readable provenance, for assertion messages only.  It carries no
    #: decision weight -- everything load-bearing is the import path or the
    #: explicit ``must_time_out`` call.
    origin: str
    #: True only for the "prove it does NOT happen" family.  The lint gate
    #: keys on THIS, never on the prose in ``why``.
    is_timeout_proof: bool = False

    @classmethod
    def from_product(cls, *constants: str) -> "Budget":
        """Derive the bound by summing product constants.

        The caller passes import paths, never a number, so there is no
        caller-side value that can drift from the product's.  Summing is what
        the existing fixes needed (#609 used deadline + wait margin).
        """

        if not constants:
            raise ValueError("from_product needs at least one product constant")
        total = sum(_resolve(path) for path in constants)
        return cls(seconds=total, origin=" + ".join(constants))

    @classmethod
    def must_time_out(cls, seconds: float, *, why: str) -> "Budget":
        """The one legitimate literal: a wait that is SUPPOSED to expire.

        ``assert not event.wait(0.3)`` proves an absence, so its bound is part
        of the claim and must stay small and literal.  ``why`` is mandatory
        and must be non-empty -- it carries no decision weight, but it gives
        the next reader something they can argue with.  Without it, the
        exemption decays into "it has always been this way".
        """

        if not why or not why.strip():
            raise ValueError("must_time_out requires a non-empty why")
        if seconds <= 0:
            raise ValueError("must_time_out needs a positive bound")
        return cls(seconds=float(seconds), origin=f"must_time_out: {why}", is_timeout_proof=True)


def wait_until(
    condition: Callable[[], bool],
    *,
    budget: Budget,
    proves: str,
) -> None:
    """Wait for a state that IS expected to arrive.

    Raises :class:`VacuousWait` when ``condition()`` already holds on entry.
    That check is the point: it makes "this run had no discriminating power"
    a loud failure instead of a green.
    """

    if budget.is_timeout_proof:
        raise ValueError(
            "wait_until expects a state that will arrive; a must_time_out budget "
            "belongs to assert_absent_while"
        )
    if condition():
        raise VacuousWait(
            f"wait_until proved nothing: {proves!r} already held before waiting. "
            "A broken system would have produced this same observation, so move "
            "the wait to the side that actually changes."
        )
    deadline = _monotonic() + budget.seconds
    while _monotonic() < deadline:
        if condition():
            return
        _sleep(_POLL_SECONDS)
    if condition():
        return
    raise WaitTimeout(
        f"{proves!r} did not hold within {budget.seconds}s (budget from {budget.origin})"
    )


def await_recorded(
    condition: Callable[[], bool],
    *,
    budget: Budget,
    proves: str,
    monotonic_because: str,
) -> None:
    'Wait for a MONOTONIC condition -- one that, once true, stays true.\n\n    ⛔ No vacuous-wait check, and that is the whole point of having a second\n    function.  :func:`wait_until` refuses a condition that already holds\n    because for a TRANSIENT state ("a thread is blocked on this lock right\n    now") already-true means you may have missed the window, so the run\n    proved nothing.  For a CUMULATIVE record ("this event was appended to the\n    log") already-true means *the thing arrived* -- which is exactly what the\n    caller is asserting.  Applying the transient rule there turns a correct\n    test red whenever the machine is fast enough to publish before the wait\n    begins, which is the same wall-clock bet this module exists to delete,\n    merely re-aimed at scheduling instead of duration.\n\n    That is not hypothetical: ``tests/daemon/delivery/custody/test_delivery_custody.py``\n    waits on an accumulating typed-event log in 22 of its 28 waits, and\n    inserting a 0.05s pause before the wait was enough to make ``wait_until``\n    fail every one of them.\n\n    Status of that file, because a list without one reads like a finished\n    investigation -- this sentence sat here for a month while none of those\n    waits moved, and one of them took dev red at f05e19fe (task 18169):\n\n      * its local helper now derives its bound from\n        ``hyprial.daemon.impl.inbox.authority.facade.DELIVERY_CUSTODY_CALL_TIMEOUT_SECONDS``\n        instead of the hand-written ``1.0`` it used to carry, which was half\n        the product\'s own declared patience for the same round trip\n      * the 22 append-only-log waits are NOT yet on :func:`await_recorded`\n      * 6 waits there are NOT monotonic and must not be moved here: a list\n        equality, three ``state is RUNNING and generation == 2`` checks, a\n        delivery-status probe needing its own justification, and an\n        ``outbox_count == 0`` carrying its own larger budget\n\n    ``monotonic_because`` is mandatory and must be non-empty.  It is the only\n    thing that makes skipping the vacuity check legitimate, so a wrong claim\n    here puts the hole back.  It carries no runtime weight -- it exists so a\n    reviewer has a specific sentence to disagree with, e.g. "events.snapshot()\n    is append-only; entries are never removed".\n\n    ⛔ Deliberately a separate function rather than ``wait_until(...,\n    monotonic=True)``: a flag is reached for by the first person the vacuity\n    check inconveniences, and it would be flipped at exactly the call sites\n    where that check was doing its job.  Two names make the choice visible in\n    review and in the diff.\n    '

    if budget.is_timeout_proof:
        raise ValueError(
            "await_recorded expects a record that will appear; a must_time_out "
            "budget belongs to assert_absent_while"
        )
    if not monotonic_because or not monotonic_because.strip():
        raise ValueError(
            "await_recorded requires a non-empty monotonic_because: state why this "
            "condition, once true, stays true. Without that claim the missing "
            "vacuity check is unjustified, and a transient condition waited on "
            "here will pass without proving anything."
        )
    deadline = _monotonic() + budget.seconds
    while True:
        if condition():
            return
        if _monotonic() >= deadline:
            break
        _sleep(_POLL_SECONDS)
    raise WaitTimeout(
        f"{proves!r} was never recorded within {budget.seconds}s "
        f"(budget from {budget.origin}; held monotonic because {monotonic_because!r})"
    )


def assert_absent_while(
    observable: Callable[[], bool],
    *,
    while_: Callable[[], bool],
    budget: Budget,
    proves: str,
) -> None:
    """Assert ``observable`` does NOT happen while ``while_`` holds.

    Replaces ``time.sleep(<literal>); assert not observable()``.  The sleep
    was standing in for an event -- "the other party has had its chance by
    now" -- and guessing that duration is the defect.  Here the wait ends
    when ``while_`` actually holds, and only then is the absence read.

    Raises if ``while_`` never holds: "the other party never even entered
    contention" and "the exclusion worked" are the same observation in the
    old form, which is precisely how that template passed vacuously.
    """

    deadline = _monotonic() + budget.seconds
    while _monotonic() < deadline:
        if while_():
            break
        if observable():
            raise AssertionError(
                f"{proves!r}: the guarded thing happened before the guard even engaged"
            )
        _sleep(_POLL_SECONDS)
    else:
        raise WaitTimeout(
            f"{proves!r}: the precondition never held within {budget.seconds}s "
            f"(budget from {budget.origin}); without it this assertion proves "
            "nothing, because 'never contended' and 'correctly excluded' look identical"
        )
    if observable():
        raise AssertionError(f"{proves!r}: it happened while the guard was engaged")


def ensure_reached(condition: Callable[[], bool], *, proves: str) -> None:
    """Confirm a state that is ALREADY expected to hold.

    ⛔ Deliberately has no budget parameter: it does not wait, so it is not
    interchangeable with :func:`wait_until` at the call site.  Swapping one
    for the other is a semantic change and must be explained in the pull
    request -- it is the obvious way to silence a :class:`VacuousWait`, and
    the lint gate flags the substitution for that reason.
    """

    if not condition():
        raise AssertionError(f"{proves!r} was expected to already hold, and does not")


__all__ = [
    "Budget",
    "await_recorded",
    "VacuousWait",
    "WaitTimeout",
    "assert_absent_while",
    "ensure_reached",
    "wait_until",
]
