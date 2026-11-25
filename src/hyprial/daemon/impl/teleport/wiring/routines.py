# Decision record (file header; no inline comments by repository rule):
# - ``AppTargetRoutines`` is the real ``TargetRoutines`` over the daemon's
#   routine service.  ``create_paused`` stores the routine disabled
#   (``add(enabled=False)``), so nothing dispatches before the source has
#   removed its copy and commit resumes it.
# - The URI rewrite happens here, never in the target service.  It touches the
#   same agent slots as ``biz/impl/routine/storage/agent_uris.py`` (``actor``,
#   ``produces``, ``policy.routes[*].escalate_to``,
#   ``on_task_timeout.escalate_to``) and only a value equal to the source
#   agent's URI; other agents and free text stay.  The slot list is repeated
#   here because the daemon may not import biz ``impl``.
# - Text with nothing to rewrite is returned byte for byte; a rewritten
#   document is re-dumped (comments and quoting are not preserved).
# - The owner of a created routine is the daemon owner's user URI, the same
#   owner the daemon gives the default agent routine.
# - The routine service is read from the application on every call, so a
#   routine runtime that starts later is picked up; without one every method
#   raises ``TELEPORT_RUNTIME_UNSUPPORTED``.
"""Target-side routine adapter of agent teleport and its agent URI rewrite."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import yaml

from hyprial.daemon.impl.teleport.errors import TeleportError
from hyprial.kernel import canonical_user_uri


def _agent_slots(document: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    slots: list[tuple[dict[str, Any], str]] = [
        (document, key) for key in ("actor", "produces")
    ]
    policy = document.get("policy")
    routes = policy.get("routes") if isinstance(policy, dict) else None
    if isinstance(routes, list):
        slots.extend((item, "escalate_to") for item in routes if isinstance(item, dict))
    timeout = document.get("on_task_timeout")
    if isinstance(timeout, dict):
        slots.append((timeout, "escalate_to"))
    return slots


def rewrite_agent_uri(yaml_text: str, *, source_uri: str, target_uri: str) -> str:
    """Point every agent slot that names ``source_uri`` at ``target_uri``."""

    try:
        document = yaml.safe_load(yaml_text)
    except yaml.YAMLError:
        return yaml_text
    if not isinstance(document, dict):
        return yaml_text
    changed = False
    for holder, key in _agent_slots(document):
        value = holder.get(key)
        if isinstance(value, str) and value.strip() == source_uri:
            holder[key] = target_uri
            changed = True
    if not changed:
        return yaml_text
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


class AppTargetRoutines:
    """``TargetRoutines`` over the daemon application's routine service."""

    def __init__(self, app: Any) -> None:
        self._app = app

    def _service(self) -> Any:
        service = self._app._routine_service
        if service is None:
            raise TeleportError(
                "TELEPORT_RUNTIME_UNSUPPORTED",
                "the routine runtime is not available on this daemon",
            )
        return service

    def existing(self, names: Sequence[str]) -> list[str]:
        stored = {item["name"] for item in self._service().list()["routines"]}
        return [name for name in names if name in stored]

    def create_paused(self, *, yaml_text: str, source_uri: str, target_uri: str) -> str:
        service = self._service()
        rewritten = rewrite_agent_uri(
            yaml_text, source_uri=source_uri, target_uri=target_uri
        )
        added = service.add(
            yaml_text=rewritten,
            owner=canonical_user_uri(str(self._app.owner)),
            enabled=False,
        )
        return str(added["name"])

    def resume(self, name: str) -> None:
        self._service().resume(name=name)

    def remove(self, name: str) -> None:
        self._service().remove(name=name)
