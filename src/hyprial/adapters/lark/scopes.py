"""Lark App scope inventory, authorization requests, and safe recovery hints.

The v6 ``scopes/apply`` endpoint requests tenant-admin authorization only for
scopes already declared on the App.  It cannot add undeclared scopes, publish
an App version, or replace the administrator's approval.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

from .endpoint import lark_base_url

JsonObject = dict[str, Any]
_SCOPE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_LARKSUITE_CONSOLE_HOST = "open.larksuite.com"
_OFFICIAL_LINK_HOSTS = frozenset({"open.feishu.cn", _LARKSUITE_CONSOLE_HOST})
_FEISHU_CONSOLE_ORIGIN = "https://open.feishu.cn"
_FEISHU_CONSOLE_HOST = urllib.parse.urlparse(_FEISHU_CONSOLE_ORIGIN).hostname
_LARKSUITE_CONSOLE_URL_MISSING_REASON = "larksuite_console_url_missing"
_MAX_HTTP_RESPONSE_BYTES = 64 * 1024
_BUSINESS_HTTP_CODES = frozenset({212001, 212002, 212003, 212004})
_THROTTLE_LOCKS: dict[Path, RLock] = {}
_THROTTLE_LOCKS_GUARD = Lock()
_MAX_CLOCK_SKEW_SECONDS = 300


@dataclass(frozen=True, slots=True)
class LarkCapability:
    id: str
    requirement: str
    any_of: tuple[str, ...]
    all_of_any: tuple[tuple[str, ...], ...] = ()

    @property
    def scope_groups(self) -> tuple[tuple[str, ...], ...]:
        return (self.any_of, *self.all_of_any)


LARK_GATEWAY_CAPABILITIES = (
    LarkCapability(
        "message-send", "required", ("im:message", "im:message:send_as_bot")
    ),
    LarkCapability(
        "receive-p2p", "required", ("im:message", "im:message.p2p_msg:readonly")
    ),
    LarkCapability(
        "receive-group-mention",
        "required",
        ("im:message.group_at_msg:readonly", "im:message.group_at_msg"),
    ),
    LarkCapability(
        "group-history",
        "optional",
        ("im:message.group_msg",),
        (("im:message", "im:message:readonly"),),
    ),
    LarkCapability("quote-fetch", "optional", ("im:message", "im:message:readonly")),
    LarkCapability("message-resources", "optional", ("im:resource",)),
    LarkCapability("reaction-write", "optional", ("im:message.reactions:write_only",)),
    LarkCapability("chat-read", "optional", ("im:chat:readonly",)),
)

CAPABILITY_IDS: tuple[str, ...] = tuple(
    capability.id for capability in LARK_GATEWAY_CAPABILITIES
)
OPTIONAL_CAPABILITY_IDS: tuple[str, ...] = tuple(
    capability.id
    for capability in LARK_GATEWAY_CAPABILITIES
    if capability.requirement != "required"
)

# Capabilities whose scopes widen what the App can *see*, as opposed to what it
# can do on request.  Naming one here forces the CLI to say so out loud before a
# human is asked to approve it; none of them may ever be requested by default.
SENSITIVE_CAPABILITY_NOTES: dict[str, str] = {
    "group-history": (
        "im:message.group_msg lets this App read every message in the groups it "
        "belongs to, not only the messages that @-mention it."
    ),
}


def capability_scopes(capabilities: Iterable[str] = ()) -> tuple[str, ...]:
    """Tenant scopes covering every required capability plus the named ones.

    Each capability is satisfied group by group: a group already covered by an
    earlier selection contributes nothing, otherwise its first (broadest)
    member is taken.  Required capabilities are always included -- a gateway
    that cannot send or receive is not a gateway -- so the argument only ever
    *adds* optional capability, never removes a required one.

    The result is ordered by :data:`LARK_GATEWAY_CAPABILITIES`, so the same
    request always produces the same declaration and the same authorization URL.
    """

    requested = tuple(dict.fromkeys(capabilities))
    unknown = [item for item in requested if item not in CAPABILITY_IDS]
    if unknown:
        raise ValueError(
            f"unknown Lark gateway capability: {', '.join(sorted(unknown))}; "
            f"known capabilities: {', '.join(CAPABILITY_IDS)}"
        )
    selected: list[str] = []
    for capability in LARK_GATEWAY_CAPABILITIES:
        if capability.requirement != "required" and capability.id not in requested:
            continue
        for group in capability.scope_groups:
            if any(scope in selected for scope in group):
                continue
            selected.append(group[0])
    return tuple(selected)


def developer_console_permission_url(app_id: str) -> str:
    return (
        _FEISHU_CONSOLE_ORIGIN
        + "/app/"
        + urllib.parse.quote(app_id, safe="")
        + "/permission"
    )


def scope_apply_url(app_id: str, scopes: Iterable[str]) -> str:
    """Build the preselected permission handoff, or the console fallback.

    Callers must supply scopes they actually know are missing.  An empty list
    cannot produce an honest preselection, so it deliberately retains the
    existing developer-console destination instead of guessing.
    """

    selected = tuple(dict.fromkeys(scopes))
    if not selected:
        return developer_console_permission_url(app_id)
    return _FEISHU_CONSOLE_ORIGIN + "/page/scope-apply?" + urllib.parse.urlencode(
        {"clientID": app_id, "scopes": ",".join(selected)}
    )


def valid_scope_name(value: str) -> bool:
    return bool(_SCOPE.fullmatch(value))


def _official_https_url(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = urllib.parse.urlparse(value)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in _OFFICIAL_LINK_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        return None
    return urllib.parse.urlunsplit(
        ("https", parsed.hostname, parsed.path or "/", parsed.query, "")
    )


def runtime_permission_url(
    app_id: str, scopes: Iterable[str], console_url: object = None
) -> tuple[str | None, str | None]:
    """Choose a safe runtime permission handoff for the error's tenant.

    A validated non-Feishu vendor link may carry tenant-specific routing that
    the Feishu-only scope-apply constructor cannot reproduce. Preserve it;
    otherwise prefer the preselected Feishu link built from verified scopes.
    """

    official_url = _official_https_url(console_url)
    if (
        official_url is not None
        and urllib.parse.urlparse(official_url).hostname != _FEISHU_CONSOLE_HOST
    ):
        return official_url, None
    configured_host = urllib.parse.urlparse(lark_base_url()).hostname
    if configured_host == _LARKSUITE_CONSOLE_HOST:
        if official_url is not None:
            return official_url, None
        return None, _LARKSUITE_CONSOLE_URL_MISSING_REASON
    # Intentional default: every non-Larksuite base is treated as Feishu for
    # human permission handoffs, including local stand-ins. Revisit this
    # branch when a third production service family is introduced.
    return scope_apply_url(app_id, scopes), None


def parse_permission_violation(error: object) -> JsonObject | None:
    """Extract only validated scope evidence from a Lark permission error.

    230027 is deliberately not enough by itself: Feishu documents several
    unrelated causes for that code.  Automatic authorization is allowed only
    when the platform also supplies structured ``permission_violations``.
    """

    direct_scopes = getattr(error, "missing_scopes", ())
    if (
        isinstance(direct_scopes, tuple)
        and direct_scopes
        and all(
            isinstance(item, str) and _SCOPE.fullmatch(item) for item in direct_scopes
        )
    ):
        result: JsonObject = {"scopes": list(dict.fromkeys(direct_scopes))}
        if link := _official_https_url(getattr(error, "authorization_url", None)):
            result["consoleUrl"] = link
        return result
    queue = [error]
    seen: set[int] = set()
    candidates: list[Mapping[str, object]] = []
    while queue:
        item = queue.pop(0)
        if id(item) in seen:
            continue
        seen.add(id(item))
        if not isinstance(item, Mapping):
            violations = getattr(item, "permission_violations", None)
            helps = getattr(item, "helps", None)
            if violations is None and helps is None:
                continue
            item = {
                "permission_violations": [
                    {"subject": getattr(value, "subject", None)}
                    for value in violations or ()
                ],
                "helps": [
                    {"url": getattr(value, "url", None)} for value in helps or ()
                ],
            }
        candidates.append(item)
        for key in ("response", "data", "error", "cause"):
            queue.append(item.get(key))
    envelope = next(
        (item for item in candidates if item.get("code") in {99991679, 230027}),
        None,
    )
    if envelope is None:
        return None
    nested = envelope.get("error")
    if isinstance(nested, Mapping):
        permission = nested
    elif nested is not None:
        permission = {
            "permission_violations": [
                {"subject": getattr(value, "subject", None)}
                for value in getattr(nested, "permission_violations", None) or ()
            ],
            "helps": [
                {"url": getattr(value, "url", None)}
                for value in getattr(nested, "helps", None) or ()
            ],
        }
    else:
        permission = envelope
    violations = permission.get("permission_violations")
    if not isinstance(violations, list):
        return None
    scopes: list[str] = []
    for violation in violations:
        subject = violation.get("subject") if isinstance(violation, Mapping) else None
        if (
            isinstance(subject, str)
            and _SCOPE.fullmatch(subject)
            and subject not in scopes
        ):
            scopes.append(subject)
    if not scopes:
        return None
    result: JsonObject = {"scopes": scopes}
    links = [permission.get("console_url"), envelope.get("console_url")]
    helps = permission.get("helps")
    if isinstance(helps, list):
        links.extend(
            item.get("url") for item in helps if isinstance(item, Mapping)
        )
    links.extend(item.get("console_url") for item in candidates)
    link = next((url for raw in links if (url := _official_https_url(raw))), None)
    if link:
        result["consoleUrl"] = link
    return result


def diagnose_scopes(
    *, adapter: str, app_id: str, response: Mapping[str, Any]
) -> JsonObject:
    if response.get("code") != 0:
        raise RuntimeError(
            f"Lark scope query failed ({response.get('code', 'unknown')}): "
            f"{response.get('msg', 'unknown error')}"
        )
    data = response.get("data")
    raw_scopes = data.get("scopes", []) if isinstance(data, Mapping) else []
    tenant: dict[str, int] = {}
    if isinstance(raw_scopes, list):
        for row in raw_scopes:
            if not isinstance(row, Mapping) or row.get("scope_type") not in {
                None,
                "tenant",
            }:
                continue
            name, status = row.get("scope_name"), row.get("grant_status")
            if isinstance(name, str) and isinstance(status, int):
                tenant[name] = status
    rows: list[JsonObject] = []
    summary = {"granted": 0, "declared_ungranted": 0, "undeclared": 0}
    for capability in LARK_GATEWAY_CAPABILITIES:
        groups = capability.scope_groups
        row: JsonObject = {
            "id": capability.id,
            "requirement": capability.requirement,
            "anyOf": list(capability.any_of),
            **(
                {"scopeGroups": [list(group) for group in groups]}
                if len(groups) > 1
                else {}
            ),
        }
        matched = [
            next((scope for scope in group if tenant.get(scope) == 1), None)
            for group in groups
        ]
        missing_groups = []
        for group, match in zip(groups, matched, strict=True):
            if match is not None:
                continue
            declared = [scope for scope in group if scope in tenant]
            missing_groups.append(
                {
                    "anyOf": list(group),
                    "status": "declared_ungranted" if declared else "undeclared",
                    **({"declaredScopes": declared} if declared else {}),
                }
            )
        declared = [scope for group in groups for scope in group if scope in tenant]
        if not missing_groups:
            row.update(status="granted", matchedScope=matched[0])
            if len(matched) > 1:
                row["matchedScopes"] = matched
        elif all(group["status"] == "declared_ungranted" for group in missing_groups):
            row.update(
                status="declared_ungranted",
                declaredScopes=declared,
                **({"missingGroups": missing_groups} if len(groups) > 1 else {}),
                action={
                    "type": "request_admin_authorization",
                    "command": f"hyprial adapter authorize {adapter}",
                },
            )
        else:
            undeclared_scopes = [
                scope
                for group in missing_groups
                if group["status"] == "undeclared"
                for scope in group["anyOf"]
            ]
            row.update(
                status="undeclared",
                **({"missingGroups": missing_groups} if len(groups) > 1 else {}),
                action={
                    "type": "declare_in_developer_console",
                    "scopes": undeclared_scopes,
                    "url": scope_apply_url(app_id, undeclared_scopes),
                },
            )
        summary[str(row["status"])] += 1
        rows.append(row)
    required_ok = all(
        row["status"] == "granted" for row in rows if row["requirement"] == "required"
    )
    return {
        "ok": required_ok,
        "adapter": adapter,
        "appId": app_id,
        "summary": summary,
        "capabilities": rows,
    }


def request_scope_authorization(response: Mapping[str, Any]) -> JsonObject:
    code = response.get("code")
    statuses = {
        0: (True, "requested"),
        212001: (False, "super_sensitive_only"),
        212002: (False, "nothing_to_request"),
        212003: (False, "request_limit_exceeded"),
        212004: (True, "already_requested"),
    }
    ok, status = statuses.get(code, (False, "failed"))
    return {
        "ok": ok,
        "code": code if isinstance(code, int) else -1,
        "status": status,
        "message": str(response.get("msg", "unknown error")),
    }


class LarkScopeClient:
    """Small synchronous v6 client; injectable origin keeps tests offline."""

    def __init__(
        self, app_id: str, app_secret: str, *, origin: str | None = None
    ) -> None:
        self.app_id = app_id
        self._app_secret = app_secret
        # ``origin`` was injectable before this line and no production caller
        # ever passed it, so only the default was ever in effect.  Resolving
        # the default through the shared lookup is what actually moves this
        # client; keeping the parameter leaves the existing offline tests --
        # which do pass it -- untouched.
        self._origin = (origin or lark_base_url()).rstrip("/")

    def _json(
        self,
        path: str,
        *,
        method: str,
        token: str | None = None,
        body: JsonObject | None = None,
    ) -> JsonObject:
        headers = {"content-type": "application/json; charset=utf-8"}
        if token:
            headers["authorization"] = f"Bearer {token}"
        encoded = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self._origin + path, data=encoded, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                value = self._decode_bounded(response)
        except urllib.error.HTTPError as error:
            try:
                value = self._decode_bounded(error)
            except (RuntimeError, json.JSONDecodeError) as decode_error:
                raise RuntimeError(
                    f"Lark scope request failed: {type(decode_error).__name__}"
                ) from decode_error
            code = value.get("code") if isinstance(value, dict) else None
            if code not in _BUSINESS_HTTP_CODES:
                raise RuntimeError(
                    f"Lark scope request failed: HTTPError status={error.code}"
                ) from error
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            raise RuntimeError(
                f"Lark scope request failed: {type(error).__name__}"
            ) from error
        if not isinstance(value, dict):
            raise TypeError("Lark scope request returned a non-object response")
        return value

    @staticmethod
    def _decode_bounded(response: Any) -> object:
        payload = response.read(_MAX_HTTP_RESPONSE_BYTES + 1)
        if len(payload) > _MAX_HTTP_RESPONSE_BYTES:
            raise RuntimeError("Lark scope response is too large")
        return json.loads(payload)

    def _tenant_token(self) -> str:
        value = self._json(
            "/open-apis/auth/v3/tenant_access_token/internal",
            method="POST",
            body={"app_id": self.app_id, "app_secret": self._app_secret},
        )
        token = value.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise RuntimeError(
                f"cannot obtain Lark tenant token ({value.get('code', 'unknown')}): "
                f"{value.get('msg', 'unknown error')}"
            )
        return token

    def list_scopes(self) -> JsonObject:
        return self._json(
            "/open-apis/application/v6/scopes", method="GET", token=self._tenant_token()
        )

    def apply_scopes(self) -> JsonObject:
        # Feishu's API contract requires an empty POST, not an empty JSON object.
        return self._json(
            "/open-apis/application/v6/scopes/apply",
            method="POST",
            token=self._tenant_token(),
        )


def _throttle_path_lock(path: Path) -> RLock:
    canonical = path.resolve()
    with _THROTTLE_LOCKS_GUARD:
        return _THROTTLE_LOCKS.setdefault(canonical, RLock())


class LarkScopeThrottleStore:
    """Crash-safe App-level apply lease shared by daemon and worker processes."""

    def __init__(self, path: Path, *, now: Callable[[], float] = time.time) -> None:
        self.path = path
        self._now = now
        self._thread_lock = _throttle_path_lock(path)

    def claim(self, app_id: str, scopes: tuple[str, ...], cooldown: float) -> bool:
        key = hashlib.sha256(app_id.encode("utf-8")).hexdigest()
        with self._thread_lock, self._file_lock():
            state = self._load()
            now = self._now()
            previous = state["apps"].get(key)
            if isinstance(previous, dict):
                attempted = previous.get("attemptedAt")
                if (
                    isinstance(attempted, (int, float))
                    and math.isfinite(attempted)
                    and attempted <= now + _MAX_CLOCK_SKEW_SECONDS
                    and now - attempted < cooldown
                ):
                    return False
            state["apps"][key] = {
                "attemptedAt": now,
                "status": "pending",
                "scopes": list(scopes),
            }
            self._save(state)
            return True

    def record(self, app_id: str, evidence: Mapping[str, object]) -> None:
        key = hashlib.sha256(app_id.encode("utf-8")).hexdigest()
        with self._thread_lock, self._file_lock():
            state = self._load()
            current = state["apps"].get(key)
            if not isinstance(current, dict):
                current = {"attemptedAt": self._now(), "scopes": []}
            state["apps"][key] = {**current, **evidence}
            self._save(state)

    @contextmanager
    def _file_lock(self):  # type: ignore[no-untyped-def]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            os.chmod(lock_path, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _load(self) -> JsonObject:
        if not self.path.exists():
            return {"version": 1, "apps": {}}
        try:
            value = json.loads(
                self.path.read_text(encoding="utf-8"),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
            if not isinstance(value, dict) or value.get("version") != 1:
                raise ValueError
            apps = value.get("apps")
            if not isinstance(apps, dict):
                raise ValueError
            for record in apps.values():
                if not isinstance(record, dict):
                    raise ValueError
                attempted = record.get("attemptedAt")
                if not isinstance(attempted, (int, float)) or not math.isfinite(
                    attempted
                ):
                    raise ValueError
            return value
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self._quarantine()
            return {"version": 1, "apps": {}}

    def _quarantine(self) -> None:
        if not self.path.exists():
            return
        quarantine = self.path.with_name(
            f"{self.path.name}.corrupt.{time.time_ns()}.{uuid4().hex}"
        )
        os.replace(self.path, quarantine)
        os.chmod(quarantine, 0o600)

    def _save(self, value: Mapping[str, object]) -> None:
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid4().hex}.tmp"
        )
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, separators=(",", ":"), sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            temporary.unlink(missing_ok=True)


class LarkScopeRecovery:
    def __init__(
        self,
        *,
        app_id: str,
        apply: Callable[[], Mapping[str, Any]],
        notify: Callable[[str], None] | None = None,
        now: Callable[[], float] = time.monotonic,
        cooldown_seconds: float = 3600,
        throttle: LarkScopeThrottleStore | None = None,
    ) -> None:
        self._app_id = app_id
        self._apply = apply
        self._now = now
        self._notify = notify
        self._cooldown = cooldown_seconds
        self._last_attempt: dict[str, float] = {}
        self._throttle = throttle

    def _notify_admin(self, scopes: list[str], result: Mapping[str, Any]) -> None:
        """Send one best-effort, recovery-free, sanitized administrator hint."""

        if self._notify is None:
            return
        scopes_text = " 或 ".join(scopes)
        link = result.get("authorizationUrl")
        if isinstance(link, str) and link:
            message = (
                f"缺少 {scopes_text} 权限，已发起管理员授权申请，亦可点击链接处理：{link}"
                if result.get("ok") is True
                else f"缺少 {scopes_text} 权限，自动发起管理员授权申请失败"
                f"（{result['status']}），请点击链接处理：{link}"
            )
        else:
            reason = result.get("authorizationUrlReason")
            message = (
                f"缺少 {scopes_text} 权限；未生成预选权限页"
                f"（authorizationUrlReason={reason}）"
            )
        try:
            self._notify(message)
        except Exception:  # noqa: BLE001, S110 - recovery-free best effort
            pass

    def handle(self, error: object) -> JsonObject | None:
        violation = parse_permission_violation(error)
        if violation is None:
            return None
        scopes = list(violation["scopes"])
        now = self._now()
        persistence_warning: str | None = None
        durable_claim = False
        if self._throttle is not None:
            try:
                eligible = self._throttle.claim(
                    self._app_id, tuple(scopes), self._cooldown
                )
                durable_claim = eligible
            except Exception as persistence_error:  # noqa: BLE001
                eligible = True
                persistence_warning = f"claim:{type(persistence_error).__name__}"
        else:
            eligible_scopes = [
                scope
                for scope in scopes
                if now - self._last_attempt.get(scope, float("-inf")) >= self._cooldown
            ]
            eligible = bool(eligible_scopes)
        if not eligible:
            return {"handled": True, "status": "throttled", "scopes": scopes}
        for scope in scopes:
            self._last_attempt[scope] = now
        authorization_url, authorization_reason = runtime_permission_url(
            self._app_id, scopes, violation.get("consoleUrl")
        )
        permission_handoff: JsonObject = (
            {"authorizationUrl": authorization_url}
            if authorization_url is not None
            else {"authorizationUrlReason": authorization_reason}
        )
        try:
            result = request_scope_authorization(self._apply())
        except Exception as error:  # noqa: BLE001 - preserve original SDK error
            result = {
                "handled": True,
                "status": "failed",
                "scopes": scopes,
                **permission_handoff,
                "errorType": type(error).__name__,
            }
            if persistence_warning is not None:
                result.update(
                    persistenceWarning=persistence_warning,
                    duplicateApplyRisk=True,
                )
            if self._throttle is not None and durable_claim:
                try:
                    self._throttle.record(
                        self._app_id,
                        {"status": "failed", "errorType": type(error).__name__},
                    )
                except Exception as persistence_error:  # noqa: BLE001
                    result.update(
                        persistenceWarning=(
                            f"record:{type(persistence_error).__name__}"
                        ),
                        duplicateApplyRisk=True,
                    )
            self._notify_admin(scopes, result)
            return result
        result.update(
            handled=True,
            scopes=scopes,
            **permission_handoff,
        )
        if persistence_warning is not None:
            result.update(
                persistenceWarning=persistence_warning,
                duplicateApplyRisk=True,
            )
        if self._throttle is not None and durable_claim:
            try:
                self._throttle.record(
                    self._app_id,
                    {
                        "status": str(result["status"]),
                        "code": int(result["code"]),
                    },
                )
            except Exception as persistence_error:  # noqa: BLE001
                result.update(
                    persistenceWarning=f"record:{type(persistence_error).__name__}",
                    duplicateApplyRisk=True,
                )
        self._notify_admin(scopes, result)
        return result


def _configured_gateway(
    hyprial_home: Path, state_dir: Path, adapter: str
) -> tuple[Any, Any]:
    from hyprial.persistent_config import PersistentConfigError, PersistentConfigStore

    store = PersistentConfigStore(hyprial_home, state_dir)
    configuration = store.load()
    gateway = next(
        (item for item in configuration.channels.gateways if item.name == adapter), None
    )
    if gateway is None:
        raise PersistentConfigError(f"adapter {adapter!r} is not configured")
    return store, gateway


def configured_app_id(hyprial_home: Path, state_dir: Path, adapter: str) -> str:
    """Resolve one local adapter's app_id, and only its app_id.

    The device-authorization path authenticates the *human*, not the App, so it
    has no use for the app secret and never obtains one. (The shared config
    store still validates every credential file while loading, which is why a
    broken credential surfaces here too -- but no secret value leaves this
    call.)
    """

    _, gateway = _configured_gateway(hyprial_home, state_dir, adapter)
    return gateway.app_id


def configured_scope_client(
    hyprial_home: Path, state_dir: Path, adapter: str
) -> tuple[str, LarkScopeClient]:
    """Resolve one local adapter without ever returning/logging its secret."""

    store, gateway = _configured_gateway(hyprial_home, state_dir, adapter)
    secret = store.lark_app_secret(gateway.credential_ref)
    return gateway.app_id, LarkScopeClient(gateway.app_id, secret)
