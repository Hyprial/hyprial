"""Proactive ``route:<adapter>:<route>`` outbound addressing.

A route target posts to a Lark chat (its configured ``nativeId``) through a
locally configured adapter.  Resolution is pure sender-local configuration:
no zenoh hop, no receiver profile.  Whether the post lands is decided by the
platform — the app must be a member of the target chat — so every failure
here names the adapter, the route, and the chat involved.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hyprial.adapters.lark import LarkApiError
from hyprial.persistent_config import ChannelConfiguration, LarkGatewayConfig
from hyprial.contracts import ipc_errors

# Lark OpenAPI im/v1/messages create error codes (open.feishu.cn document
# "Send message", error-code table).  230002 was additionally confirmed live
# against the dedicated E2E app; the others come from the same table.
LARK_CODE_APP_NOT_IN_CHAT = 230002
LARK_CODE_BOT_ABILITY_DISABLED = 230006
LARK_CODE_OPERATOR_NOT_IN_CHAT = 232011
LARK_CODES_PERMISSION_REQUIRED = frozenset({99991679, 230027})
# Official IM upload limits:
# https://open.feishu.cn/document/server-docs/im-v1/file/create
# https://open.feishu.cn/document/server-docs/im-v1/image/create
LARK_FILE_MAX_BYTES = 30 * 1024 * 1024
LARK_IMAGE_MAX_BYTES = 10 * 1024 * 1024
LARK_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".ico", ".tiff", ".heic"}
)


class RouteDeliveryError(RuntimeError):
    def __init__(self, code: str, message: str, data: Any | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(frozen=True, slots=True)
class RouteTarget:
    adapter: str
    route: str

    @property
    def uri(self) -> str:
        return f"route:{self.adapter}:{self.route}"

    @classmethod
    def parse(cls, value: str) -> RouteTarget:
        parts = value.split(":")
        if len(parts) != 3 or parts[0] != "route":
            raise ValueError(
                "route target must be route:<adapter>:<route> "
                "(for example route:mac-studio-feishu:hyprial-all-member)"
            )
        _component(parts[1], "adapter")
        _component(parts[2], "route")
        return cls(adapter=parts[1], route=parts[2])


def is_route_target(value: str) -> bool:
    return value.startswith("route:")


@dataclass(frozen=True, slots=True)
class ResolvedRoute:
    """One concrete chat post after fanout expansion."""

    route: str
    chat_id: str


@dataclass(frozen=True, slots=True)
class RouteResource:
    """One validated local resource for a proactive Lark route send."""

    path: Path
    kind: str
    media_type: str


def parse_route_resources(value: object) -> tuple[RouteResource, ...]:
    """Validate the CLI's local resource manifest before any native send.

    Feishu's IM upload APIs reject empty payloads, files over 30 MiB, images
    over 10 MiB, and image formats outside their documented allowlist.  These
    deterministic failures are caught locally so a text post cannot land while
    its attachment is silently omitted.
    """

    if value is None:
        return ()
    if not isinstance(value, list):
        raise RouteDeliveryError(
            "INVALID_RESOURCE_PATHS", "resourcePaths must be an array"
        )
    resources: list[RouteResource] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise RouteDeliveryError(
                "INVALID_RESOURCE_PATHS",
                f"resourcePaths[{index}] must be an object",
            )
        raw_path = item.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise RouteDeliveryError(
                "INVALID_RESOURCE_PATHS",
                f"resourcePaths[{index}].path must be a non-empty string",
            )
        kind = item.get("kind")
        if kind not in {"file", "image"}:
            raise RouteDeliveryError(
                "RESOURCE_KIND_UNSUPPORTED",
                f"resourcePaths[{index}].kind must be file or image, got {kind!r}",
                {"index": index, "kind": kind},
            )
        path = Path(raw_path).expanduser()
        try:
            stat = path.stat()
        except FileNotFoundError as error:
            raise RouteDeliveryError(
                "RESOURCE_NOT_FOUND",
                f"resourcePaths[{index}] does not exist: {path}",
                {"index": index, "path": str(path)},
            ) from error
        except OSError as error:
            raise RouteDeliveryError(
                "RESOURCE_UNREADABLE",
                f"resourcePaths[{index}] cannot be inspected: {path}: {error}",
                {"index": index, "path": str(path)},
            ) from error
        if not path.is_file():
            raise RouteDeliveryError(
                "RESOURCE_NOT_FILE",
                f"resourcePaths[{index}] is not a regular file: {path}",
                {"index": index, "path": str(path)},
            )
        if stat.st_size == 0:
            raise RouteDeliveryError(
                "RESOURCE_EMPTY",
                f"resourcePaths[{index}] is empty; Lark rejects zero-byte uploads",
                {"index": index, "path": str(path)},
            )
        limit = LARK_FILE_MAX_BYTES if kind == "file" else LARK_IMAGE_MAX_BYTES
        if stat.st_size > limit:
            raise RouteDeliveryError(
                "RESOURCE_TOO_LARGE",
                f"resourcePaths[{index}] is {stat.st_size} bytes; Lark {kind} "
                f"uploads are limited to {limit} bytes",
                {
                    "index": index,
                    "path": str(path),
                    "kind": kind,
                    "sizeBytes": stat.st_size,
                    "maxBytes": limit,
                },
            )
        suffix = path.suffix.lower()
        if kind == "file" and not suffix:
            raise RouteDeliveryError(
                "RESOURCE_FILE_NAME_INVALID",
                f"resourcePaths[{index}] must have a filename extension for Lark",
                {"index": index, "path": str(path)},
            )
        if kind == "image" and suffix not in LARK_IMAGE_SUFFIXES:
            raise RouteDeliveryError(
                "RESOURCE_IMAGE_TYPE_UNSUPPORTED",
                f"resourcePaths[{index}] image extension {suffix or '(none)'} is "
                "not supported by Lark",
                {
                    "index": index,
                    "path": str(path),
                    "supportedExtensions": sorted(LARK_IMAGE_SUFFIXES),
                },
            )
        raw_media_type = item.get("mediaType")
        if raw_media_type is not None and (
            not isinstance(raw_media_type, str) or not raw_media_type.strip()
        ):
            raise RouteDeliveryError(
                "INVALID_RESOURCE_PATHS",
                f"resourcePaths[{index}].mediaType must be a non-empty string",
            )
        media_type = (
            raw_media_type
            if isinstance(raw_media_type, str)
            else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        resources.append(
            RouteResource(
                path=path,
                kind=kind,
                media_type=media_type,
            )
        )
    return tuple(resources)


def find_gateway(
    configuration: ChannelConfiguration, adapter: str
) -> LarkGatewayConfig:
    gateway = next(
        (item for item in configuration.gateways if item.name == adapter), None
    )
    if gateway is None:
        configured = sorted(item.name for item in configuration.gateways)
        raise RouteDeliveryError(
            ipc_errors.ROUTE_ADAPTER_UNCONFIGURED,
            f"route target names adapter {adapter!r}, which is not configured "
            f"in channels.json (configured: {', '.join(configured) or 'none'})",
            {"adapter": adapter, "configuredAdapters": configured},
        )
    return gateway


def resolve_gateway_routes(
    gateway: LarkGatewayConfig, route_name: str
) -> tuple[ResolvedRoute, ...]:
    """Resolve a route name to concrete chat posts, expanding fanout."""

    routes = {item.name: item for item in gateway.routes}
    route = routes.get(route_name)
    if route is None:
        available = sorted(routes)
        raise RouteDeliveryError(
            "ROUTE_NOT_CONFIGURED",
            f"adapter {gateway.name!r} has no route {route_name!r} "
            f"(configured routes: {', '.join(available) or 'none'})",
            {
                "adapter": gateway.name,
                "route": route_name,
                "configuredRoutes": available,
            },
        )
    if route.type == "direct":
        assert route.native_id is not None
        return (ResolvedRoute(route=route.name, chat_id=route.native_id),)
    resolved: list[ResolvedRoute] = []
    for member in route.members:
        member_route = routes.get(member)
        if member_route is None or member_route.type != "direct" or (
            member_route.native_id is None
        ):
            raise RouteDeliveryError(
                "ROUTE_FANOUT_MEMBER_INVALID",
                f"fanout route {route.name!r} on adapter {gateway.name!r} "
                f"references {member!r}, which is not a configured direct route",
                {"adapter": gateway.name, "route": route.name, "member": member},
            )
        resolved.append(ResolvedRoute(route=member, chat_id=member_route.native_id))
    return tuple(resolved)


def map_lark_send_error(
    error: LarkApiError, *, adapter: str, route: str, chat_id: str
) -> RouteDeliveryError:
    """Translate a platform rejection into a named, actionable failure."""

    if error.code in LARK_CODES_PERMISSION_REQUIRED and error.missing_scopes:
        data = {
            "adapter": adapter,
            "route": route,
            "chatId": chat_id,
            "missingScopes": list(error.missing_scopes),
        }
        if error.authorization_url:
            data["authorizationUrl"] = error.authorization_url
        if error.authorization_url_reason:
            data["authorizationUrlReason"] = error.authorization_url_reason
        action = (
            f": {error.authorization_url}"
            if error.authorization_url
            else (
                "；未生成授权链接"
                f"（authorizationUrlReason={error.authorization_url_reason}）"
                if error.authorization_url_reason
                else ""
            )
        )
        return RouteDeliveryError(
            "ROUTE_APP_SCOPE_REQUIRED",
            f"adapter {adapter!r} 缺少飞书 App 权限 "
            f"({', '.join(error.missing_scopes)})；请由管理员授权后重试"
            + action,
            data,
        )
    if error.code == LARK_CODE_APP_NOT_IN_CHAT:
        return RouteDeliveryError(
            "ROUTE_APP_NOT_IN_CHAT",
            f"adapter {adapter!r} 的 app 不在 route {route!r} 对应的群 "
            f"(chat_id={chat_id}) 里；请把该 app 拉进群后重试 "
            f"(lark code {error.code})",
            {"adapter": adapter, "route": route, "chatId": chat_id},
        )
    if error.code == LARK_CODE_BOT_ABILITY_DISABLED:
        return RouteDeliveryError(
            "ROUTE_APP_BOT_ABILITY_DISABLED",
            f"adapter {adapter!r} 的 app 未开启机器人能力；请在飞书开放平台的"
            f"应用能力页启用后重试 (lark code {error.code})",
            {"adapter": adapter, "route": route, "chatId": chat_id},
        )
    if error.code == LARK_CODE_OPERATOR_NOT_IN_CHAT:
        return RouteDeliveryError(
            "ROUTE_OPERATOR_NOT_IN_CHAT",
            f"操作者不在 route {route!r} 对应的群 (chat_id={chat_id}) 里 "
            f"(lark code {error.code})",
            {"adapter": adapter, "route": route, "chatId": chat_id},
        )
    return RouteDeliveryError(
        ipc_errors.ROUTE_SEND_FAILED,
        f"route {route!r} (chat_id={chat_id}) send via adapter {adapter!r} "
        f"failed: {error}",
        {"adapter": adapter, "route": route, "chatId": chat_id,
         "larkCode": error.code},
    )


def _component(value: str, label: str) -> None:
    if not value or value.strip() != value:
        raise ValueError(
            f"route target {label} must be a non-empty component of "
            "route:<adapter>:<route>"
        )
