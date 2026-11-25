"""Fakes for the slice 6 (routine) target-side teleport tests.

Behaviour contract pinned by this package (docs/design/agent-teleport-protocol.md
§2 prepare/receive/commit, §3, §5 ``TargetRoutines``, §6 "切片 6").  Decisions
on details the plan leaves open are marked D and recorded only here.

prepare
- ``ROUTINE_CONFLICT`` consults ``routines.existing(names)`` (unchanged).
- D: a non-empty ``routines`` list with no routines port is
  ``TELEPORT_RUNTIME_UNSUPPORTED`` at the routine check's place (fifth), and
  registers nothing.
- The operation remembers prepare's ``sourceAgentUri`` and routine names.

receive
- D: the names in receive's ``routines`` must be exactly prepare's names (no
  duplicates, no extra, none missing, any order); otherwise
  ``TELEPORT_BAD_FRAME`` before any host or routine call, like every receive
  failure forgetting the operation and removing its staging.
- Names are checked again with ``routines.existing`` before ``land``; a name
  taken since prepare is ``TELEPORT_ROUTINE_CONFLICT`` and nothing is landed
  or created.
- Order on success: ``existing`` -> ``land`` -> ``activate`` -> one
  ``create_paused`` per payload in request order -> ``start``.
  ``create_paused(yaml_text=<payload yaml, unchanged>, source_uri=<prepare
  sourceAgentUri>, target_uri=<Landed.uri>)``: the target rewrite of the URI
  happens inside the port (the real adapter), never in the service.
- The reply's ``routines`` is ``RoutineRestore(name, restore_enabled=<payload
  enabled>)`` per payload in request order; nothing is resumed by receive.
- Undo: a created routine registers ``remove(name)`` right after its
  ``create_paused`` returns (D: a ``create_paused`` that raises created
  nothing).  §3 reverse order: ``stop``, remove routines newest first,
  ``deactivate``, ``undo_landing``.  A ``TeleportError`` from
  ``create_paused`` keeps its code; any other exception is
  ``TELEPORT_LANDING_FAILED``.
- A repeated receive returns the stored reply and creates nothing again.

commit
- Resumes, in reply order, exactly the routines with ``restore_enabled`` true.
- D: ``RoutineOutcome(name, enabled=<restore_enabled>, resumed=<resume
  succeeded>)``; disabled routines are ``enabled=False, resumed=False`` and
  are never resumed.  A ``resume`` that raises gives ``resumed=False``, the
  other routines are still resumed and commit is still a reply.
- A repeated commit returns the same reply without resuming again.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import yaml

from hyprial.daemon.impl.teleport.protocol import RoutinePayload

from ..support import SOURCE_URI, FakeHost, make_service, staged

NIGHTLY = "nightly"
WEEKLY = "weekly"
HOURLY = "hourly"
OTHER_URI = "agent://owner-1/node-a/reviewer"


def routine_yaml(name: str) -> str:
    return (
        f"version: 2\nname: {name}\nactor: {SOURCE_URI}\n"
        f"on_task_timeout:\n  action: escalate\n  escalate_to: {OTHER_URI}\n"
    )


def payload(name: str, enabled: bool) -> RoutinePayload:
    return RoutinePayload(name=name, yaml=routine_yaml(name), enabled=enabled)


PAYLOADS = (payload(NIGHTLY, True), payload(WEEKLY, False))


@dataclass
class LoggingRoutines:
    """A ``TargetRoutines`` that logs into the host's call list."""

    log: list[str]
    taken: set[str] = field(default_factory=set)
    failures: dict[str, BaseException] = field(default_factory=dict)
    created: dict[str, bool] = field(default_factory=dict)
    create_args: list[tuple[str, str, str]] = field(default_factory=list)

    def existing(self, names: Sequence[str]) -> list[str]:
        self.log.append("existing")
        return [name for name in names if name in self.taken or name in self.created]

    def create_paused(self, *, yaml_text: str, source_uri: str, target_uri: str) -> str:
        name = yaml.safe_load(yaml_text)["name"]
        self.log.append(f"create_paused:{name}")
        self.create_args.append((yaml_text, source_uri, target_uri))
        if f"create:{name}" in self.failures:
            raise self.failures[f"create:{name}"]
        self.created[name] = False
        return name

    def resume(self, name: str) -> None:
        self.log.append(f"resume:{name}")
        if f"resume:{name}" in self.failures:
            raise self.failures[f"resume:{name}"]
        self.created[name] = True

    def remove(self, name: str) -> None:
        self.log.append(f"routine_remove:{name}")
        self.created.pop(name, None)


def routine_service(tmp_path, *, names: Sequence[str] = (NIGHTLY, WEEKLY)):
    """A service with a logging routines port and one staged operation ``op-1``.

    Returns ``(service, host, routines, receiver, packed)``; the host call log
    is cleared after prepare and staging so it shows receive's calls only.
    """

    host = FakeHost()
    routines = LoggingRoutines(log=host.calls)
    service, _host, receiver = make_service(tmp_path, host, routines=routines)
    packed = staged(service, tmp_path, "op-1", routines=tuple(names))
    host.calls.clear()
    return service, host, routines, receiver, packed
