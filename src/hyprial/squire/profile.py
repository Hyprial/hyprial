"""Versioned daemon-owned user profiles for Squire addressing."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Self


class UserProfileError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PreferredReceiver:
    machine: str
    machine_key: str

    def to_json(self) -> dict[str, str]:
        return {"machine": self.machine, "machineKey": self.machine_key}


@dataclass(frozen=True, slots=True)
class OwnerOpenId:
    channel: str
    open_id: str
    bound_at: str

    def to_json(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "openId": self.open_id,
            "boundAt": self.bound_at,
        }


RUNTIME_HARNESSES = ("claude", "codex", "pi")
RUNTIME_CAPABILITY_STATUSES = ("available", "unavailable")


@dataclass(frozen=True, slots=True)
class RuntimeCapability:
    """Observed availability of one harness + provider + model combination.

    This records facts only: whether the combination worked on this machine,
    why it failed when it did not, and when it was last probed.  Model
    tiering or dispatch judgement does not belong here.
    """

    harness: str
    model: str
    status: str
    probed_at: str
    provider: str | None = None
    reason: str | None = None
    detail: str | None = None
    retry_at: str | None = None

    @property
    def combo(self) -> tuple[str, str | None, str]:
        return (self.harness, self.provider, self.model)

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        harness = _string(record.get("harness"), f"{label}.harness")
        if harness not in RUNTIME_HARNESSES:
            raise UserProfileError(
                f"{label}.harness must be one of {', '.join(RUNTIME_HARNESSES)}"
            )
        provider = _optional_string(record.get("provider"), f"{label}.provider")
        if harness == "pi" and provider is None:
            raise UserProfileError(
                f"{label}.provider is required for the pi harness; omitting it "
                "resolves ambiguously"
            )
        status = _string(record.get("status"), f"{label}.status")
        if status not in RUNTIME_CAPABILITY_STATUSES:
            raise UserProfileError(
                f"{label}.status must be one of {', '.join(RUNTIME_CAPABILITY_STATUSES)}"
            )
        reason = _optional_string(record.get("reason"), f"{label}.reason")
        retry_at = _optional_string(record.get("retryAt"), f"{label}.retryAt")
        if status == "available" and (reason is not None or retry_at is not None):
            raise UserProfileError(
                f"{label}.reason and {label}.retryAt require status unavailable"
            )
        if status == "unavailable" and reason is None:
            raise UserProfileError(f"{label}.reason is required when unavailable")
        return cls(
            harness=harness,
            provider=provider,
            model=_string(record.get("model"), f"{label}.model"),
            status=status,
            reason=reason,
            detail=_optional_string(record.get("detail"), f"{label}.detail"),
            retry_at=retry_at,
            probed_at=_string(record.get("probedAt"), f"{label}.probedAt"),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "harness": self.harness,
            **({"provider": self.provider} if self.provider is not None else {}),
            "model": self.model,
            "status": self.status,
            **({"reason": self.reason} if self.reason is not None else {}),
            **({"detail": self.detail} if self.detail is not None else {}),
            **({"retryAt": self.retry_at} if self.retry_at is not None else {}),
            "probedAt": self.probed_at,
        }


@dataclass(frozen=True, slots=True)
class NotificationRule:
    id: str
    disposition: str
    enabled: bool = True
    sender: str | None = None
    intent: str | None = None
    keywords: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        disposition = _string(record.get("disposition"), f"{label}.disposition")
        if disposition not in {"immediate", "digest", "silent"}:
            raise UserProfileError(
                f"{label}.disposition must be immediate, digest, or silent"
            )
        enabled = record.get("enabled")
        if not isinstance(enabled, bool):
            raise UserProfileError(f"{label}.enabled must be a boolean")
        intent = _optional_string(record.get("intent"), f"{label}.intent")
        if intent not in {None, "request", "reply", "event"}:
            raise UserProfileError(f"{label}.intent must be request, reply, or event")
        raw_keywords = record.get("keywords", [])
        if not isinstance(raw_keywords, list):
            raise UserProfileError(f"{label}.keywords must be an array")
        keywords = tuple(
            _string(keyword, f"{label}.keywords[{index}]")
            for index, keyword in enumerate(raw_keywords)
        )
        return cls(
            id=_string(record.get("id"), f"{label}.id"),
            disposition=disposition,
            enabled=enabled,
            sender=_optional_string(record.get("sender"), f"{label}.sender"),
            intent=intent,
            keywords=keywords,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "disposition": self.disposition,
            "enabled": self.enabled,
            **({"sender": self.sender} if self.sender is not None else {}),
            **({"intent": self.intent} if self.intent is not None else {}),
            **({"keywords": list(self.keywords)} if self.keywords else {}),
        }


@dataclass(frozen=True, slots=True)
class UserProfile:
    owner: str
    owner_key: str
    login_name: str
    agents: tuple[str, ...]
    preferred_receiver: PreferredReceiver
    notification_rules: tuple[NotificationRule, ...] = ()
    squire_channel: str | None = None
    owner_open_id: OwnerOpenId | None = None
    delivery_agent: str | None = None
    runtime_capabilities: tuple[RuntimeCapability, ...] = ()

    def runtime_capability(
        self, harness: str, model: str, provider: str | None = None
    ) -> RuntimeCapability | None:
        """Exact-match lookup for one harness + provider + model combination."""

        combo = (harness, provider, model)
        return next(
            (
                capability
                for capability in self.runtime_capabilities
                if capability.combo == combo
            ),
            None,
        )

    def available_capabilities(
        self,
        *,
        harness: str | None = None,
        provider: str | None = None,
        model: str | None = None,
    ) -> tuple[RuntimeCapability, ...]:
        """Available combinations, optionally narrowed per dimension."""

        return tuple(
            capability
            for capability in self.runtime_capabilities
            if capability.status == "available"
            and (harness is None or capability.harness == harness)
            and (provider is None or capability.provider == provider)
            and (model is None or capability.model == model)
        )

    def is_available(
        self, harness: str, model: str, provider: str | None = None
    ) -> bool:
        capability = self.runtime_capability(harness, model, provider)
        return capability is not None and capability.status == "available"

    @property
    def squire_adapter(self) -> str | None:
        """External-platform adapter URI used by receiver-owned delivery.

        ``squireChannel`` remains the persisted v1 field for migration
        compatibility with the TypeScript profile.  New Python routing code
        uses adapter terminology because IRC channels are a different concept.
        """

        return self.squire_channel

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        raw_agents = record.get("agents")
        raw_rules = record.get("notificationRules")
        receiver = _record(
            record.get("preferredReceiver"), f"{label}.preferredReceiver"
        )
        if not isinstance(raw_agents, list):
            raise UserProfileError(f"{label}.agents must be an array")
        if not isinstance(raw_rules, list):
            raise UserProfileError(f"{label}.notificationRules must be an array")
        open_id: OwnerOpenId | None = None
        if record.get("ownerOpenId") is not None:
            binding = _record(record["ownerOpenId"], f"{label}.ownerOpenId")
            open_id = OwnerOpenId(
                channel=_channel(
                    binding.get("channel"), f"{label}.ownerOpenId.channel"
                ),
                open_id=_open_id(binding.get("openId"), f"{label}.ownerOpenId.openId"),
                bound_at=_string(
                    binding.get("boundAt"), f"{label}.ownerOpenId.boundAt"
                ),
            )
        channel = _optional_string(
            record.get("squireChannel"), f"{label}.squireChannel"
        )
        if channel is not None:
            channel = _channel(channel, f"{label}.squireChannel")
        delivery_agent = _optional_string(
            record.get("deliveryAgent"), f"{label}.deliveryAgent"
        )
        return cls(
            owner=_string(record.get("owner"), f"{label}.owner"),
            owner_key=_string(record.get("ownerKey"), f"{label}.ownerKey"),
            login_name=_string(record.get("loginName"), f"{label}.loginName"),
            agents=tuple(
                sorted(
                    {
                        _agent(agent, f"{label}.agents[{index}]")
                        for index, agent in enumerate(raw_agents)
                    }
                )
            ),
            squire_channel=channel,
            owner_open_id=open_id,
            delivery_agent=delivery_agent,
            preferred_receiver=PreferredReceiver(
                machine=_string(
                    receiver.get("machine"), f"{label}.preferredReceiver.machine"
                ),
                machine_key=_string(
                    receiver.get("machineKey"),
                    f"{label}.preferredReceiver.machineKey",
                ),
            ),
            notification_rules=tuple(
                NotificationRule.from_json(rule, f"{label}.notificationRules[{index}]")
                for index, rule in enumerate(raw_rules)
            ),
            runtime_capabilities=_runtime_capabilities(record, label),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "ownerKey": self.owner_key,
            "loginName": self.login_name,
            "agents": list(self.agents),
            **(
                {"squireChannel": self.squire_channel}
                if self.squire_channel is not None
                else {}
            ),
            **(
                {"ownerOpenId": self.owner_open_id.to_json()}
                if self.owner_open_id is not None
                else {}
            ),
            **(
                {"deliveryAgent": self.delivery_agent}
                if self.delivery_agent is not None
                else {}
            ),
            "preferredReceiver": self.preferred_receiver.to_json(),
            "notificationRules": [rule.to_json() for rule in self.notification_rules],
            **(
                {
                    "runtimeCapabilities": [
                        capability.to_json()
                        for capability in self.runtime_capabilities
                    ]
                }
                if self.runtime_capabilities
                else {}
            ),
        }


class UserProfileStore:
    """Serialized profile updates with atomic, no-op-aware persistence."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def list(self) -> tuple[UserProfile, ...]:
        with self._lock:
            return self._load()

    def get(self, owner_key: str) -> UserProfile | None:
        return next(
            (profile for profile in self.list() if profile.owner_key == owner_key),
            None,
        )

    def get_by_owner(self, owner: str) -> UserProfile | None:
        matches = tuple(profile for profile in self.list() if profile.owner == owner)
        if len(matches) > 1:
            raise UserProfileError(f"user owner {owner!r} is ambiguous")
        return matches[0] if matches else None

    def resolve(self, identifier: str) -> UserProfile | None:
        """Resolve a ``user:<identifier>`` address by owner key, then by owner.

        Callers hold identifiers from two different vocabularies and cannot
        tell them apart.  ``resolve_node_owner()`` yields the OS login name
        (``daemon.owner``), which is an **owner key**; a human typing
        ``user:<name>`` usually means the **owner**.  Looking up only one of
        them is what made the upgrade alert unable to find a squire on
        2026-09-03: ``daemon.owner`` was ``h2oslabs`` while ``profile.owner``
        was ``allenwoods``, so ``get_by_owner`` returned ``None`` and the
        restart refused fail-closed (``UPGRADE_NOTIFICATION_FAILED``).

        ⚠️ Owner key wins, and that is a real decision, not a tie-break of
        convenience: the key is the globally unique anchor, while ``owner`` is
        a display name.  The two vocabularies **share a value space** -- a
        person's login and their name can both be ``allenwoods`` -- so a value
        that is one profile's key and another's owner resolves to the key
        holder.  ``test_owner_key_wins_when_a_value_is_both`` pins that.

        The ambiguity guard in :meth:`get_by_owner` is deliberately still on
        the fallback path: two profiles sharing an ``owner`` remains an error
        rather than a silent first-match.
        """

        by_key = self.get(identifier)
        if by_key is not None:
            return by_key
        return self.get_by_owner(identifier)

    def ensure(
        self,
        *,
        owner: str,
        owner_key: str,
        login_name: str,
        machine: str,
        machine_key: str,
    ) -> tuple[UserProfile, tuple[str, ...]]:
        _validate_identity(owner, owner_key, login_name, machine, machine_key)
        with self._lock:
            profiles = list(self._load())
            existing = next(
                (profile for profile in profiles if profile.owner_key == owner_key),
                None,
            )
            if existing is not None:
                return existing, ()
            profile = UserProfile(
                owner=owner,
                owner_key=owner_key,
                login_name=login_name,
                agents=(),
                preferred_receiver=PreferredReceiver(machine, machine_key),
            )
            profiles.append(profile)
            self._save(tuple(profiles))
            return profile, ("users.profile",)

    def associate_agent(
        self, owner_key: str, agent: str
    ) -> tuple[UserProfile, tuple[str, ...]]:
        agent = _agent(agent, "agent")
        return self._replace(
            owner_key,
            lambda profile: (
                profile
                if agent in profile.agents
                else replace(profile, agents=tuple(sorted((*profile.agents, agent))))
            ),
            "users.agents",
        )

    def set_squire_channel(
        self, owner_key: str, channel: str
    ) -> tuple[UserProfile, tuple[str, ...]]:
        channel = _channel(channel, "squire channel")

        def update(profile: UserProfile) -> UserProfile:
            if profile.squire_channel == channel:
                return profile
            return replace(profile, squire_channel=channel, owner_open_id=None)

        return self._replace(owner_key, update, "users.squireChannel")

    def bind_owner_open_id(
        self,
        owner_key: str,
        *,
        channel: str,
        open_id: str,
        now: datetime | None = None,
    ) -> tuple[UserProfile, tuple[str, ...]]:
        channel = _channel(channel, "owner open_id channel")
        open_id = _open_id(open_id, "owner open_id")

        def update(profile: UserProfile) -> UserProfile:
            if profile.squire_channel != channel:
                raise UserProfileError(
                    "owner open_id channel must match the configured squire channel"
                )
            if (
                profile.owner_open_id is not None
                and profile.owner_open_id.channel == channel
                and profile.owner_open_id.open_id == open_id
            ):
                return profile
            timestamp = (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z")
            return replace(
                profile,
                owner_open_id=OwnerOpenId(channel, open_id, timestamp),
            )

        return self._replace(owner_key, update, "users.ownerOpenId")

    def set_delivery_agent(
        self, owner_key: str, agent: str
    ) -> tuple[UserProfile, tuple[str, ...]]:
        agent = _string(agent, "delivery agent")
        return self._replace(
            owner_key,
            lambda profile: (
                profile
                if profile.delivery_agent == agent
                else replace(profile, delivery_agent=agent)
            ),
            "users.deliveryAgent",
        )

    def set_runtime_capability(
        self, owner_key: str, capability: RuntimeCapability
    ) -> tuple[UserProfile, tuple[str, ...]]:
        """Upsert one probed combination, keyed by harness + provider + model."""

        # Round-trip through JSON so writer bugs surface as UserProfileError
        # instead of persisting an unreadable profile.
        checked = RuntimeCapability.from_json(
            capability.to_json(), "runtimeCapability"
        )

        def update(profile: UserProfile) -> UserProfile:
            combos = [existing.combo for existing in profile.runtime_capabilities]
            if checked.combo in combos:
                index = combos.index(checked.combo)
                if profile.runtime_capabilities[index] == checked:
                    return profile
                replaced = list(profile.runtime_capabilities)
                replaced[index] = checked
                return replace(profile, runtime_capabilities=tuple(replaced))
            return replace(
                profile,
                runtime_capabilities=(*profile.runtime_capabilities, checked),
            )

        return self._replace(owner_key, update, "users.runtimeCapabilities")

    def _replace(
        self,
        owner_key: str,
        operation: Callable[[UserProfile], UserProfile],
        changed_field: str,
    ) -> tuple[UserProfile, tuple[str, ...]]:
        with self._lock:
            profiles = list(self._load())
            for index, profile in enumerate(profiles):
                if profile.owner_key != owner_key:
                    continue
                updated = operation(profile)
                if updated == profile:
                    return profile, ()
                profiles[index] = updated
                self._save(tuple(profiles))
                return updated, (changed_field,)
        raise UserProfileError(f"user profile {owner_key!r} was not registered")

    def _load(self) -> tuple[UserProfile, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise UserProfileError(f"failed to read user profiles: {error}") from error
        record = _record(raw, "user profile data")
        if record.get("version") != 1 or not isinstance(record.get("users"), list):
            raise UserProfileError("unsupported user profile data; expected version 1")
        profiles = tuple(
            UserProfile.from_json(value, f"users[{index}]")
            for index, value in enumerate(record["users"])
        )
        keys = [profile.owner_key for profile in profiles]
        if len(keys) != len(set(keys)):
            raise UserProfileError("user profile data contains duplicate owner keys")
        return profiles

    def _save(self, profiles: tuple[UserProfile, ...]) -> None:
        ordered = sorted(
            profiles, key=lambda profile: (profile.owner, profile.owner_key)
        )
        payload = {
            "version": 1,
            "users": [profile.to_json() for profile in ordered],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_suffix(f".tmp.{os.getpid()}")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)


def _runtime_capabilities(
    record: dict[str, Any], label: str
) -> tuple[RuntimeCapability, ...]:
    raw = record.get("runtimeCapabilities")
    if raw is None:
        # Older profiles predate this section; absence means no probes yet.
        return ()
    if not isinstance(raw, list):
        raise UserProfileError(f"{label}.runtimeCapabilities must be an array")
    capabilities = tuple(
        RuntimeCapability.from_json(value, f"{label}.runtimeCapabilities[{index}]")
        for index, value in enumerate(raw)
    )
    combos = [capability.combo for capability in capabilities]
    if len(combos) != len(set(combos)):
        raise UserProfileError(
            f"{label}.runtimeCapabilities contains duplicate combinations"
        )
    return capabilities


def _validate_identity(*values: str) -> None:
    for value in values:
        _string(value, "identity component")


def _record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise UserProfileError(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise UserProfileError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _uri() -> Any:
    """Import the dependency-free address-grammar helpers."""

    from hyprial import uri

    return uri


def _channel(value: object, label: str) -> str:
    channel = _string(value, label)
    if _uri().parse_channel_uri(channel) is None:
        raise UserProfileError(
            f"{label} must be a channel:<owner>:<machine>:<channel> URI"
        )
    return channel


def _agent(value: object, label: str) -> str:
    agent = _string(value, label)
    if _uri().agent_uri_actor(agent) is None:
        raise UserProfileError(
            f"{label} must be an agent:<owner>:<machine>:<agent> URI"
        )
    return agent


def _open_id(value: object, label: str) -> str:
    open_id = _string(value, label)
    if not open_id.startswith("ou_"):
        raise UserProfileError(f"{label} must start with ou_")
    return open_id
