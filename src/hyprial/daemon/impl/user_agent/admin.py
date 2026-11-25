# Decision record (UA1b, 2026-11-18; GUI owner review; UA1c review fixes):
# - Every ``agent.*`` catalog method that mutates (plus the destroy preview,
#   which gates ``agent.destroy``) and names one agent through its ``name``,
#   ``actor`` or ``agent`` param is refused for the user agent by the catalog
#   dispatcher, after parameter validation and the binding's preauthorize.
#   Today that is start, stop, rename, destroy preview, attach, detach,
#   harness-args set/clear, the credential allow/login/logout methods and
#   secret grant.
# - Revocation is never refused (PR 2941, after ``org.acl.revoke``): the
#   capability, secret and credential revokes only remove, and a grant an
#   older build wrote for the user agent must stay removable.  The matching
#   grant methods stay refused.  The revokes are the literal
#   ``NEVER_REFUSED``; a test pins it to the agent methods whose last
#   segment is ``revoke``, so a new one fails there until it is decided.
# - Legacy router ``agent.*`` methods never reach the catalog dispatcher, so
#   the router's one legacy ``agent.*`` branch asks ``refuse_legacy_agent_method``
#   with the literal ``LEGACY_TARGET_PARAMS`` (config rebind, migrate, capability
#   grant, ensure-home, resume restore, runtime context, unblock and the
#   restore policy).  The legacy family is frozen by the method
#   registration gate, so a literal list cannot silently miss a new method.
#   ``agent.restore-policy`` with ``list`` exactly ``true`` is the handler's
#   read form and passes (UA1g); any other ``list`` value reaches the write.
# - One stable answer: ``USER_AGENT_IMMUTABLE``, declared by every refused
#   catalog method.  Authorization keeps precedence: a method whose handler
#   authorizes its caller itself (the literal ``HANDLER_REFUSED``: harness-args
#   set/clear and the credential login/logout) is skipped by the dispatcher
#   and refuses the user agent in its handler, after that check,
#   so a caller refused for authorization on an ordinary agent is refused the
#   same way on the user agent.
# - ``agent.create`` and ``agent.start.preflight`` take
#   the reserved name as input: the name gate (``bootstrap.names``) refuses it
#   with INVALID_ARGUMENT, as for any other unusable name.
# - Rows of the status producer behind ``agent.list``/``agent.get`` carry two
#   dedicated fields on a user agent only: ``userAgent: true`` (the hint a
#   client uses to hide lifecycle buttons) and ``userActor: user:<owner>``,
#   the merge key every machine's instance shares.  The free-text ``role`` is
#   not used.
# Decision record (I7a, contract i7-0 v2 section 2.3, owner item A-1):
# - ``agent.cell.get`` and ``agent.cell.update`` are the literal
#   ``CELL_DOCUMENT_EXEMPT`` and are never refused here: they are the owner's
#   one write path for the user agent cell's adapter members.  The cell
#   service applies the allow-list on update and keeps the reader to the
#   local operator; a test pins the set to these two names.
"""Refuse agent administration on the user agent; mark its status rows."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from hyprial.kernel import canonical_user_uri

from .naming import is_user_agent, refuse_user_agent_change

__all__ = [
    "CELL_DOCUMENT_EXEMPT",
    "HANDLER_REFUSED",
    "LEGACY_TARGET_PARAMS",
    "NEVER_REFUSED",
    "refuse_legacy_agent_method",
    "refuse_user_agent_method",
    "refused_target_param",
    "user_agent_row_fields",
]

_FAMILY = "agent."
_TARGET_PARAMS = ("name", "actor", "agent")
_NOT_REFUSED = frozenset({"agent.create"})
_ALWAYS_REFUSED = frozenset({"agent.destroy.preview"})
CELL_DOCUMENT_EXEMPT = frozenset({"agent.cell.get", "agent.cell.update"})
"""The user agent cell's read and write path; the cell service guards both."""
NEVER_REFUSED = frozenset({
    "agent.provider.revoke",
    "agent.revoke",
    "agent.secret.revoke",
})
"""Revocations only remove, so they stay allowed on the user agent."""


HANDLER_REFUSED = frozenset({
    "agent.harness-args.clear",
    "agent.harness-args.set",
    "agent.provider.login",
    "agent.provider.logout",
})
"""Methods that authorize their caller in the handler and refuse the user agent there."""


LEGACY_TARGET_PARAMS: Mapping[str, str] = {
    "agent.config.rebind": "name",
    "agent.ensure-home": "name",
    "agent.grant": "actor",
    "agent.migrate.execute": "agent",
    "agent.migrate.preflight": "agent",
    "agent.migrate.rollback": "agent",
    "agent.restore-policy": "name",
    "agent.resume.restore": "name",
    "agent.runtime-context": "name",
    "agent.unblock": "name",
}
"""Legacy router methods refused for the user agent, and the param naming it."""


def refused_target_param(spec: Any) -> str | None:
    """The param naming the target agent when *spec* is refused for a user agent."""

    method = str(getattr(spec, "name", ""))
    if not method.startswith(_FAMILY) or method in _NOT_REFUSED or method in NEVER_REFUSED:
        return None
    if str(spec.name) in CELL_DOCUMENT_EXEMPT:
        return None
    if not (getattr(spec, "mutates", False) or method in _ALWAYS_REFUSED):
        return None
    properties = (getattr(spec, "params_schema", None) or {}).get("properties") or {}
    return next((key for key in _TARGET_PARAMS if key in properties), None)


def _refuse_named(app: Any, method: str, target: object) -> None:
    if not isinstance(target, str) or not target:
        return
    agents = getattr(app, "agents", None)
    if agents is None:
        return
    try:
        agent = agents.get(target)
    except Exception:  # noqa: BLE001 - the method's own path reports a bad name
        return
    refuse_user_agent_change(agent, method.removeprefix(_FAMILY))


def refuse_user_agent_method(app: Any, spec: Any, params: Mapping[str, Any]) -> None:
    """Raise USER_AGENT_IMMUTABLE when *spec*'s method targets the user agent."""

    if str(getattr(spec, "name", "")) in HANDLER_REFUSED:
        return
    key = refused_target_param(spec)
    if key is not None:
        _refuse_named(app, str(spec.name), params.get(key))


def refuse_legacy_agent_method(app: Any, method: str, params: Mapping[str, Any]) -> None:
    """Raise USER_AGENT_IMMUTABLE when legacy *method* targets the user agent."""

    key = LEGACY_TARGET_PARAMS.get(method)
    if key is None or not isinstance(params, Mapping):
        return
    if method == "agent.restore-policy" and params.get("list") is True:
        return
    _refuse_named(app, method, params.get(key))


def user_agent_row_fields(agent: Any) -> dict[str, Any]:
    """``userAgent``/``userActor`` for a user agent's status row; empty otherwise."""

    if not is_user_agent(agent):
        return {}
    return {"userAgent": True, "userActor": canonical_user_uri(str(agent.owner))}
