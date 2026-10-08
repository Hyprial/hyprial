"""Canonical harness runtime actor package.

The former flat ``hyprial.daemon.impl.harness_actor`` module is split here by
semantic responsibility: :mod:`.contracts` carries the value/record/projection
vocabulary, :mod:`.io_port` the process I/O port, :mod:`.actor` the single
canonical actor that owns mailbox/generation/in-flight custody and all mutable
state, :mod:`behaviors` the behaviour mixins that borrow that state, and
:mod:`.facade` the public compatibility facade.  This package is the actual
module owner; there is no flat forwarder.
"""

from hyprial.daemon.impl.harnesses.actor.actor import HarnessRuntimeActor
from hyprial.daemon.impl.harnesses.actor.contracts import (
    HarnessProjection,
    HarnessRestoreSummary,
    HarnessRuntimeClosed,
    ProcessIdentity,
    _DeliveryReservation,
    _LIFECYCLE_SETTLED_CAPACITY,
    _Record,
    _default_identity_reader,
    _launch_spec,
    process_identities_match,
)
from hyprial.daemon.impl.harnesses.actor.facade import HarnessRuntimeFacade
from hyprial.daemon.impl.harnesses.actor.io_port import ProcessIoPort

__all__ = [
    "HarnessProjection",
    "HarnessRestoreSummary",
    "HarnessRuntimeActor",
    "HarnessRuntimeClosed",
    "HarnessRuntimeFacade",
    "ProcessIdentity",
    "ProcessIoPort",
    "_DeliveryReservation",
    "_LIFECYCLE_SETTLED_CAPACITY",
    "_Record",
    "_default_identity_reader",
    "_launch_spec",
    "process_identities_match",
]
