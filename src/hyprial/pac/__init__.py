"""PAC v2: the graph file + flag reactor (concept 2026-09-04, slice 1).

Three layers, each owned by exactly one module here:

- **编写** (edit): :mod:`hyprial.pac.graph` + :mod:`hyprial.pac.store` — the SQLite
  graph file (``pac-graph.sqlite3``, five tables per concept §2) and the
  CAS-versioned edit operations with write-time validation.
- **触发** (react): :mod:`hyprial.pac.reactor` — notifications as a pure
  function of ``flag_events``.  It never reads reply bodies; the tripwire
  test (:mod:`tests.test_pac_tripwire`) keeps that a gate, not a hope.
- **监控** (project): :mod:`hyprial.pac.projection` — read-only subscribers
  that restate the flow from ``flag_events``.

The workflow CLI compiles into this graph engine. Legacy workflow execution
is retired; its database is a sealed archive/write barrier, not an executor.
"""

from __future__ import annotations

from .errors import PacError

__all__ = ["PacError"]
