"""Validated persistent configuration shared by migration and daemon startup."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self


class PersistentConfigError(ValueError):
    pass


def _record(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PersistentConfigError(f"{label} must be an object")
    return value


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PersistentConfigError(f"{label} must be a non-empty string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


@dataclass(frozen=True, slots=True)
class ChannelRouteConfig:
    name: str
    type: str
    native_id: str | None = None
    nickname: str | None = None
    members: tuple[str, ...] = ()

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        route_type = record.get("type")
        if route_type not in {"direct", "fanout"}:
            raise PersistentConfigError(f"{label}.type must be direct or fanout")
        native_id = _optional_string(record.get("nativeId"), f"{label}.nativeId")
        raw_members = record.get("members", [])
        if not isinstance(raw_members, list) or any(
            not isinstance(item, str) or not item for item in raw_members
        ):
            raise PersistentConfigError(
                f"{label}.members must be an array of non-empty strings"
            )
        members = tuple(raw_members)
        if route_type == "direct" and native_id is None:
            raise PersistentConfigError(f"{label}.nativeId is required for direct routes")
        if route_type == "fanout" and (native_id is not None or not members):
            raise PersistentConfigError(
                f"{label} fanout routes require members and no nativeId"
            )
        return cls(
            name=_string(record.get("name"), f"{label}.name"),
            type=str(route_type),
            native_id=native_id,
            nickname=_optional_string(record.get("nickname"), f"{label}.nickname"),
            members=members,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "name": self.name,
            **({"nickname": self.nickname} if self.nickname is not None else {}),
            "type": self.type,
            **({"nativeId": self.native_id} if self.native_id is not None else {}),
            **({"members": list(self.members)} if self.members else {}),
        }


@dataclass(frozen=True, slots=True)
class LarkGatewayConfig:
    name: str
    app_id: str
    credential_ref: str
    routes: tuple[ChannelRouteConfig, ...]
    default_route: str | None = None

    @classmethod
    def from_json(cls, value: object, label: str) -> Self:
        record = _record(value, label)
        if record.get("provider") != "lark":
            raise PersistentConfigError(f"{label}.provider must be lark")
        raw_routes = record.get("routes")
        if not isinstance(raw_routes, list):
            raise PersistentConfigError(f"{label}.routes must be an array")
        routes = tuple(
            ChannelRouteConfig.from_json(item, f"{label}.routes[{index}]")
            for index, item in enumerate(raw_routes)
        )
        names = [item.name for item in routes]
        if len(set(names)) != len(names):
            raise PersistentConfigError(f"{label} contains duplicate route names")
        default_route = _optional_string(
            record.get("defaultRoute"), f"{label}.defaultRoute"
        )
        if default_route is not None and default_route not in names:
            raise PersistentConfigError(
                f"{label}.defaultRoute must name one of the gateway routes"
            )
        credential_ref = _string(
            record.get("credentialRef"), f"{label}.credentialRef"
        )
        if (
            not credential_ref.startswith("lark-")
            or Path(credential_ref).name != credential_ref
        ):
            raise PersistentConfigError(
                f"{label}.credentialRef must be a local lark-* name"
            )
        return cls(
            name=_string(record.get("name"), f"{label}.name"),
            app_id=_string(record.get("appId"), f"{label}.appId"),
            credential_ref=credential_ref,
            routes=routes,
            default_route=default_route,
        )

    def to_json(self) -> dict[str, object]:
        return {
            "provider": "lark",
            "name": self.name,
            "appId": self.app_id,
            "credentialRef": self.credential_ref,
            "routes": [item.to_json() for item in self.routes],
            **(
                {"defaultRoute": self.default_route}
                if self.default_route is not None
                else {}
            ),
        }


@dataclass(frozen=True, slots=True)
class ChannelConfiguration:
    gateways: tuple[LarkGatewayConfig, ...] = ()
    version: int = 1

    @classmethod
    def from_json(cls, value: object) -> Self:
        record = _record(value, "channels configuration")
        if record.get("version") != 1 or not isinstance(record.get("gateways"), list):
            raise PersistentConfigError(
                "unsupported channels configuration; expected version 1"
            )
        gateways = tuple(
            LarkGatewayConfig.from_json(item, f"gateways[{index}]")
            for index, item in enumerate(record["gateways"])
        )
        names = [item.name for item in gateways]
        refs = [item.credential_ref for item in gateways]
        if len(set(names)) != len(names):
            raise PersistentConfigError("channels configuration has duplicate gateways")
        if len(set(refs)) != len(refs):
            raise PersistentConfigError(
                "channels configuration has duplicate credential references"
            )
        return cls(gateways=gateways)

    def to_json(self) -> dict[str, object]:
        return {
            "version": self.version,
            "gateways": [item.to_json() for item in self.gateways],
        }


@dataclass(frozen=True, slots=True)
class UserProfiles:
    users: tuple[dict[str, object], ...] = ()
    version: int = 1

    @classmethod
    def from_json(cls, value: object) -> Self:
        record = _record(value, "user profiles")
        raw_users = record.get("users")
        if record.get("version") != 1 or not isinstance(raw_users, list):
            raise PersistentConfigError(
                "unsupported user profile data; expected version 1"
            )
        users = tuple(
            _validate_user_profile(item, f"users[{index}]")
            for index, item in enumerate(raw_users)
        )
        owner_keys = [str(item["ownerKey"]) for item in users]
        if len(set(owner_keys)) != len(owner_keys):
            raise PersistentConfigError("user profiles contain duplicate ownerKey values")
        return cls(users=users)

    def to_json(self) -> dict[str, object]:
        return {"version": self.version, "users": [dict(item) for item in self.users]}


@dataclass(frozen=True, slots=True)
class PersistentConfiguration:
    channels: ChannelConfiguration
    users: UserProfiles


class PersistentConfigStore:
    def __init__(self, hyprial_home: Path, state_dir: Path) -> None:
        self.hyprial_home = Path(hyprial_home)
        self.state_dir = Path(state_dir)

    def load(self) -> PersistentConfiguration:
        channels = ChannelConfiguration.from_json(
            self._read_json(self.hyprial_home / "channels.json", {"version": 1, "gateways": []})
        )
        for gateway in channels.gateways:
            secret_path = self.hyprial_home / "secrets" / f"{gateway.credential_ref}.json"
            secret = _record(self._read_json(secret_path), f"credential {gateway.credential_ref}")
            _string(secret.get("appSecret"), f"credential {gateway.credential_ref}.appSecret")
        users = UserProfiles.from_json(
            self._read_json(self.state_dir / "users.json", {"version": 1, "users": []})
        )
        return PersistentConfiguration(channels=channels, users=users)

    def lark_app_secret(self, credential_ref: str) -> str:
        """Return one validated secret without including its value in errors."""

        if (
            not credential_ref.startswith("lark-")
            or Path(credential_ref).name != credential_ref
        ):
            raise PersistentConfigError("invalid local Lark credential reference")
        secret = _record(
            self._read_json(self.hyprial_home / "secrets" / f"{credential_ref}.json"),
            f"credential {credential_ref}",
        )
        return _string(secret.get("appSecret"), f"credential {credential_ref}.appSecret")

    @staticmethod
    def _read_json(path: Path, missing: object | None = None) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            if missing is not None:
                return missing
            raise PersistentConfigError(f"required persistent config is missing: {path}")
        except (OSError, json.JSONDecodeError) as error:
            raise PersistentConfigError(f"cannot read persistent config {path}: {error}") from error


def _validate_user_profile(value: object, label: str) -> dict[str, object]:
    record = _record(value, label)
    result: dict[str, object] = {
        "owner": _string(record.get("owner"), f"{label}.owner"),
        "ownerKey": _string(record.get("ownerKey"), f"{label}.ownerKey"),
        "loginName": _string(record.get("loginName"), f"{label}.loginName"),
    }
    agents = record.get("agents")
    if not isinstance(agents, list) or any(
        not isinstance(item, str) or not item for item in agents
    ):
        raise PersistentConfigError(f"{label}.agents must be an array of strings")
    result["agents"] = list(agents)
    if "squireChannel" in record:
        result["squireChannel"] = _string(
            record["squireChannel"], f"{label}.squireChannel"
        )
    if "ownerOpenId" in record:
        binding = _record(record["ownerOpenId"], f"{label}.ownerOpenId")
        result["ownerOpenId"] = {
            "channel": _string(binding.get("channel"), f"{label}.ownerOpenId.channel"),
            "openId": _string(binding.get("openId"), f"{label}.ownerOpenId.openId"),
            "boundAt": _string(binding.get("boundAt"), f"{label}.ownerOpenId.boundAt"),
        }
    receiver = _record(record.get("preferredReceiver"), f"{label}.preferredReceiver")
    result["preferredReceiver"] = {
        "machine": _string(receiver.get("machine"), f"{label}.preferredReceiver.machine"),
        "machineKey": _string(
            receiver.get("machineKey"), f"{label}.preferredReceiver.machineKey"
        ),
    }
    rules = record.get("notificationRules")
    if not isinstance(rules, list):
        raise PersistentConfigError(f"{label}.notificationRules must be an array")
    result["notificationRules"] = [
        _validate_notification_rule(item, f"{label}.notificationRules[{index}]")
        for index, item in enumerate(rules)
    ]
    return result


def _validate_notification_rule(value: object, label: str) -> dict[str, object]:
    record = _record(value, label)
    disposition = record.get("disposition")
    if disposition not in {"immediate", "digest", "silent"}:
        raise PersistentConfigError(
            f"{label}.disposition must be immediate, digest, or silent"
        )
    enabled = record.get("enabled")
    if not isinstance(enabled, bool):
        raise PersistentConfigError(f"{label}.enabled must be a boolean")
    result: dict[str, object] = {
        "id": _string(record.get("id"), f"{label}.id"),
        "disposition": disposition,
        "enabled": enabled,
    }
    if "sender" in record:
        result["sender"] = _string(record["sender"], f"{label}.sender")
    if "intent" in record:
        intent = record["intent"]
        if intent not in {"request", "reply", "event"}:
            raise PersistentConfigError(
                f"{label}.intent must be request, reply, or event"
            )
        result["intent"] = intent
    if "keywords" in record:
        keywords = record["keywords"]
        if not isinstance(keywords, list) or any(
            not isinstance(item, str) or not item for item in keywords
        ):
            raise PersistentConfigError(f"{label}.keywords must be an array of strings")
        result["keywords"] = list(keywords)
    return result


def atomic_json_write(path: Path, value: object) -> None:
    """Write ``value`` as JSON to ``path`` atomically at mode 0600.

    The serialization (2-space indent, sorted keys, trailing newline) and file
    mode are shared by the TS migration and ``hyprial adapter add`` so migrated and
    freshly-added config are byte-identical. The parent directory is created at
    mode 0700 if missing.
    """

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
