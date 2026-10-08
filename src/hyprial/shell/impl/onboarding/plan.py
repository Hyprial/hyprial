"""Read-only planning for the first-run onboarding flow.

The desktop app owns the phase scheduler and the rendering.  This module owns
the product rule that answers "what is still missing on this machine?" and the
read-only snapshot collector that backs it.  It deliberately has no mutation
path: onboarding actions are a separate, later interface.

This module is the only implementation of that rule: the desktop shell renders
``hyprial onboarding plan``/``apply`` instead of keeping its own step list
(desktop contract section 11), so there is no second copy to drift.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.kernel import agent_uri_actor


class OnboardingPlanError(ValueError):
    """The planner was given something that is not a snapshot object."""


@dataclass(frozen=True, slots=True)
class OnboardingStep:
    """One ordered step in the first-run graph."""

    id: str
    owner: str
    requires: tuple[str, ...]
    interactive: bool
    title: str
    action: str


STEPS: tuple[OnboardingStep, ...] = (
    OnboardingStep(
        "runtime-ready",
        "runtime",
        (),
        False,
        "运行时可用",
        "等待 daemon 与 GUI 就绪",
    ),
    OnboardingStep(
        "default-agent",
        "first-run",
        ("runtime-ready",),
        False,
        "默认 agent 会话",
        "hyprial agent create --kind default",
    ),
    OnboardingStep(
        "worker-agent",
        "first-run",
        ("default-agent",),
        False,
        "worker/developer agent",
        "hyprial agent create --kind worker",
    ),
    OnboardingStep(
        "squire",
        "first-run",
        ("runtime-ready",),
        False,
        "Squire 个人助手",
        "hyprial squire setup",
    ),
    OnboardingStep(
        "lark-app",
        "first-run",
        ("squire",),
        True,
        "创建/接入飞书 App",
        "hyprial adapter onboard <name>",
    ),
    OnboardingStep(
        "lark-authorize",
        "first-run",
        ("lark-app",),
        True,
        "租户授权 scopes",
        "hyprial adapter authorize <name> --interactive",
    ),
    OnboardingStep(
        "lark-route",
        "first-run",
        ("lark-app",),
        False,
        "出站 route",
        "hyprial adapter route add <name> <route>=<native_chat_id>",
    ),
    OnboardingStep(
        "lark-pin",
        "first-run",
        ("lark-authorize", "squire"),
        False,
        "入站绑定到 Squire",
        "hyprial adapter pin <name> <squire actor>",
    ),
    OnboardingStep(
        "lark-roundtrip",
        "first-run",
        ("lark-authorize", "lark-pin", "lark-route"),
        False,
        "双向消息冒烟",
        "hyprial send … / 等待回执",
    ),
)

LARK_STEPS = frozenset(
    {"lark-app", "lark-authorize", "lark-route", "lark-pin", "lark-roundtrip"}
)
_STEP_BY_ID = {step.id: step for step in STEPS}


def _agents_of(snapshot: Mapping[str, object], kind: str) -> list[Mapping[str, Any]]:
    agents = snapshot.get("agents")
    if not isinstance(agents, Sequence) or isinstance(agents, (str, bytes, bytearray)):
        return []
    return [
        agent
        for agent in agents
        if isinstance(agent, Mapping) and agent.get("kind") == kind
    ]


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def is_satisfied(step_id: str, snapshot: Mapping[str, object]) -> bool:
    """Whether a step is already observable in the supplied snapshot."""

    if step_id not in _STEP_BY_ID:
        raise OnboardingPlanError(f"unknown onboarding step: {step_id}")
    lark = _mapping(snapshot.get("lark"))
    squire = _mapping(snapshot.get("squire"))
    if step_id == "runtime-ready":
        return snapshot.get("runtimeReady") is True
    if step_id == "default-agent":
        return bool(_agents_of(snapshot, "default"))
    if step_id == "worker-agent":
        return bool(_agents_of(snapshot, "worker"))
    if step_id == "squire":
        return squire.get("configured") is True
    if step_id == "lark-app":
        return isinstance(lark.get("adapters"), int) and lark["adapters"] > 0
    if step_id == "lark-authorize":
        return lark.get("authorized") is True
    if step_id == "lark-route":
        return isinstance(lark.get("routes"), int) and lark["routes"] > 0
    if step_id == "lark-pin":
        return lark.get("pinned") is True
    if step_id == "lark-roundtrip":
        return lark.get("roundTripVerified") is True
    raise AssertionError(f"unhandled onboarding step: {step_id}")


def _lark_applies(snapshot: Mapping[str, object]) -> bool:
    return _mapping(snapshot.get("lark")).get("available") is not False


def plan_first_run(snapshot: Mapping[str, object]) -> dict[str, object]:
    """Return the ordered, read-only first-run plan for one snapshot.

    The function is pure: it neither mutates ``snapshot`` nor reads state.
    Callers that need machine truth build the snapshot with
    :class:`OnboardingStateReader` first.
    """

    if not isinstance(snapshot, Mapping):
        raise OnboardingPlanError("first-run needs a snapshot object")

    results: dict[str, str] = {}
    for step in STEPS:
        if not _lark_applies(snapshot) and step.id in LARK_STEPS:
            results[step.id] = "skipped"
        else:
            results[step.id] = (
                "done" if is_satisfied(step.id, snapshot) else "outstanding"
            )

    steps: list[dict[str, object]] = []
    runnable: list[dict[str, object]] = []
    for step in STEPS:
        state = results[step.id]
        if state == "skipped":
            steps.append(
                {
                    "id": step.id,
                    "title": step.title,
                    "state": "skipped",
                    "interactive": step.interactive,
                    "action": step.action,
                    "reason": "本机未检测到该消息平台",
                }
            )
            continue
        if state == "done":
            steps.append(
                {
                    "id": step.id,
                    "title": step.title,
                    "state": "done",
                    "interactive": step.interactive,
                    "action": step.action,
                }
            )
            continue

        waiting_on = [
            requirement
            for requirement in step.requires
            if results.get(requirement) == "outstanding"
        ]
        entry: dict[str, object] = {
            "id": step.id,
            "title": step.title,
            "interactive": step.interactive,
            "action": step.action,
            "state": "blocked" if waiting_on else "ready",
            "reason": (
                f"等待前置：{'、'.join(waiting_on)}"
                if waiting_on
                else "尚未完成"
            ),
        }
        steps.append(entry)
        if entry["state"] == "ready":
            runnable.append(entry)

    blocked = [step for step in steps if step["state"] == "blocked"]
    return {
        "complete": not runnable and not blocked,
        "steps": steps,
        "next": runnable[0] if runnable else None,
        "remaining": len(runnable) + len(blocked),
        "needsHuman": any(step["interactive"] is True for step in runnable),
        "blockedCount": len(blocked),
    }


def _agent_role(agent: Mapping[str, object]) -> str | None:
    """Return the current first-run role for an ``agent list`` row.

    The agent entity has no role field yet.  The explicit ``kind`` field is
    honoured as soon as the CLI grows one; until then the collector recognizes
    the deterministic actor names the first-run apply path will create.  An
    unrelated agent is never promoted to a role merely because it exists.
    """

    kind = agent.get("kind")
    if isinstance(kind, str) and kind in {"default", "worker"}:
        return kind
    actor = agent.get("actor")
    if not isinstance(actor, str) or not actor:
        uri = agent.get("uri")
        actor = agent_uri_actor(uri) if isinstance(uri, str) else None
    if actor == "default":
        return "default"
    if actor in {"worker", "developer"}:
        return "worker"
    return None


def _json_object(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _json_list(value: object) -> list[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return list(value)


def _adapter_replied_chats(db_path: Path, name: str) -> tuple[str, ...]:
    """Default round-trip evidence: one configured adapter's own state.

    The configured name is mapped to the adapter's state namespace the same way
    the adapter worker maps it, so the query looks where the writes landed.
    """

    from hyprial.daemon import adapter_namespace
    from hyprial.daemon import replied_chats

    return replied_chats(db_path, adapter=adapter_namespace(name))


class OnboardingStateReader:
    """Build an onboarding snapshot from the CLI's existing read surfaces."""

    def __init__(
        self,
        *,
        hyprial_home: Path,
        state_dir: Path,
        daemon_request: Callable[..., object],
        scope_authorized: Callable[[str], object] | None = None,
        adapters_reader: Callable[[Path], object] | None = None,
        messaging_available: bool | None = None,
        lark_cli_reader: Callable[[], object] | None = None,
        round_trip_reader: Callable[[Path, str], object] | None = None,
    ) -> None:
        self.hyprial_home = Path(hyprial_home)
        self.state_dir = Path(state_dir)
        self._daemon_request = daemon_request
        self._scope_authorized = scope_authorized
        self._adapters_reader = adapters_reader
        self._messaging_available = messaging_available
        self._lark_cli_reader = lark_cli_reader
        self._round_trip_reader = round_trip_reader

    def read(self) -> dict[str, object]:
        runtime_ready = self._runtime_ready()
        agents = self._agents()
        adapters, routes, adapter_names = self._adapters()
        pinned = self._pinned(agents)
        authorized = False
        if adapter_names and self._scope_authorized is not None:
            for name in adapter_names:
                try:
                    if self._scope_authorized(name) is True:
                        authorized = True
                        break
                except Exception:  # noqa: BLE001 - an unavailable read is not a plan failure
                    continue
        return {
            "runtimeReady": runtime_ready,
            "agents": agents,
            "squire": {"configured": self._squire_configured()},
            "lark": {
                "available": self._messaging_platform_available(adapter_names),
                "adapters": adapters,
                "authorized": authorized,
                "routes": routes,
                "pinned": pinned,
                "roundTripVerified": bool(self._round_trip_chats(adapter_names)),
            },
        }

    def _round_trip_chats(self, adapter_names: Sequence[str]) -> tuple[str, ...]:
        """Chats where a real inbound message and its native reply are both recorded.

        The round-trip step asks whether two-way messaging works, and the honest
        answer is the adapter's own state, never a marker this machine could
        write for itself: ``request_correlations`` is written only after a real
        inbound event was forwarded, and ``reply_routes`` only after the platform
        returned a native id.  A chat holding both is a completed exchange.  An
        unreadable or absent store is absence of evidence -- it never verifies.
        """

        if not adapter_names:
            return ()
        path = self.state_dir / "adapters.sqlite3"
        reader = self._round_trip_reader or _adapter_replied_chats
        try:
            chats = [
                chat
                for name in adapter_names
                for chat in _json_list(reader(path, name))
                if isinstance(chat, str) and chat
            ]
        except Exception:  # noqa: BLE001 - an unreadable store is absence, not failure
            return ()
        return tuple(sorted(set(chats)))

    def _daemon_json(self, method: str) -> Mapping[str, Any]:
        try:
            value = self._daemon_request(method, {}, restore_wait=0.0)
        except Exception:  # noqa: BLE001 - the plan is best effort and read-only
            return {}
        return _json_object(value)

    def _runtime_ready(self) -> bool:
        value = self._daemon_json("ps")
        return bool(value)

    def _agents(self) -> list[dict[str, object]]:
        value = self._daemon_json("agent.list")
        agents = _json_list(value.get("agents"))
        result: list[dict[str, object]] = []
        for agent in agents:
            if not isinstance(agent, Mapping):
                continue
            row = dict(agent)
            role = _agent_role(agent)
            if role is not None:
                row["kind"] = role
            result.append(row)
        return result

    def _adapters(self) -> tuple[int, int, list[str]]:
        try:
            if self._adapters_reader is not None:
                value = self._adapters_reader(self.hyprial_home)
            else:
                from hyprial.daemon import list_gateway_routes

                value = list_gateway_routes(hyprial_home=self.hyprial_home)
        except Exception:  # noqa: BLE001 - absent or malformed local config is empty state
            return 0, 0, []
        payload = _json_object(value)
        raw_adapters = _json_list(payload.get("adapters"))
        names: list[str] = []
        routes = 0
        for adapter in raw_adapters:
            if not isinstance(adapter, Mapping):
                continue
            name = adapter.get("name")
            if isinstance(name, str) and name:
                names.append(name)
            routes += len(_json_list(adapter.get("routes")))
        return len(names), routes, sorted(names)

    def _messaging_platform_available(self, adapter_names: Sequence[str]) -> bool:
        if self._messaging_available is not None:
            return self._messaging_available
        if adapter_names:
            return True
        try:
            if self._lark_cli_reader is not None:
                return bool(self._lark_cli_reader())
            from hyprial.daemon import find_lark_cli

            return find_lark_cli() is not None
        except Exception:  # noqa: BLE001 - an unavailable probe is absence, not failure
            return False

    def _pinned(self, agents: Sequence[Mapping[str, object]]) -> bool:
        value = self._daemon_json("adapter.pins")
        pins = value.get("pins")
        if isinstance(pins, Mapping) and bool(pins):
            return True
        if isinstance(pins, Sequence) and not isinstance(
            pins, (str, bytes, bytearray)
        ) and bool(pins):
            return True
        for agent in agents:
            pinned = agent.get("pinnedAdapters")
            if isinstance(pinned, Sequence) and not isinstance(
                pinned, (str, bytes, bytearray)
            ) and bool(pinned):
                return True
        return False

    def _squire_configured(self) -> bool:
        try:
            from hyprial.identity import UserProfileStore

            profiles = UserProfileStore(self.state_dir / "users.json").list()
        except Exception:  # noqa: BLE001 - malformed state is an unconfigured state
            return False
        for profile in profiles:
            for agent in profile.agents:
                if isinstance(agent, str) and agent_uri_actor(agent) == "squire":
                    return True
        return False


__all__ = [
    "LARK_STEPS",
    "OnboardingPlanError",
    "OnboardingStateReader",
    "OnboardingStep",
    "STEPS",
    "is_satisfied",
    "plan_first_run",
]
