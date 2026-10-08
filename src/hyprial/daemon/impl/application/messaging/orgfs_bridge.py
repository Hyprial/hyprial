"""OrgFs runtime bridge: the orgfs.* IPC family, owner notices and the org-context bridge."""

from __future__ import annotations

from __future__ import annotations
import json
import time
from collections.abc import Callable
from dataclasses import asdict, is_dataclass
from hyprial.daemon.impl.path_authz import PathPolicy, PathRefused, read_authorized_file
from hyprial.daemon.impl.path_authz.write import (
    authorize_write_path,
    write_authorized_file,
)
from typing import Any, TYPE_CHECKING
from uuid import NAMESPACE_URL, uuid5
from hyprial.kernel import ipc_errors
from hyprial.kernel import DaemonRequestError
from hyprial.daemon.impl.orgfs.composition.vocabulary import ORGFS_CONTENT_WAIT_S
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.daemon.impl.squire import (
    UserDeliveryRequest,
)
from hyprial.kernel import (
    canonical_agent_uri,
    canonical_user_uri,
)
from hyprial.identity import is_org_acl_space
if TYPE_CHECKING:
    pass

from hyprial.daemon.impl.ipc.params import (
    JsonObject,
    _required_string,
)


_ORGFS_NOTICE_NAMESPACE = "orgfs"

_ORG_CONTEXT_READ_METHODS = frozenset(
    {
        "orgfs.export",
        "orgfs.history",
        "orgfs.ls",
        "orgfs.read",
        "orgfs.read_at",
        "orgfs.stat",
        "orgfs.stat_at",
    }
)

_ORGFS_MUTATING_METHODS = frozenset(
    {
        "orgfs.checkout",
        "orgfs.create",
        "orgfs.import",
        "orgfs.invite",
        "orgfs.join",
        "orgfs.mkdir",
        "orgfs.move",
        "orgfs.purge",
        "orgfs.purge_plan",
        "orgfs.remove",
        "orgfs.remove_member",
        "orgfs.restore",
        "orgfs.serve",
        "orgfs.unban",
        "orgfs.write",
    }
)

_ORGFS_READ_METHODS = frozenset(
    {
        "orgfs.export",
        "orgfs.history",
        "orgfs.ls",
        "orgfs.members",
        "orgfs.purge_status",
        "orgfs.read",
        "orgfs.read_at",
        "orgfs.resolve",
        "orgfs.spaces",
        "orgfs.stat",
        "orgfs.stat_at",
        "orgfs.status",
        "orgfs.trash",
        "orgfs.watch",
    }
)


def _targets_org_acl_space(fs: Any, method: str, params: JsonObject) -> bool:
    if method == "orgfs.create":
        return is_org_acl_space(params.get("name"))
    space_id = params.get("spaceId")
    if not isinstance(space_id, str):
        return False
    return any(
        space.space_id == space_id and is_org_acl_space(space.name)
        for space in fs.spaces()
    )



#: Largest local file ``orgfs.import`` copies into a space in one call.
ORGFS_IMPORT_MAX_BYTES = 64 * 1024 * 1024
#: Export uses the same registered one-call transfer bound as import.
ORGFS_EXPORT_MAX_BYTES = ORGFS_IMPORT_MAX_BYTES


def _authorized_or_refused(action: Callable[[], Any]) -> Any:
    try:
        return action()
    except PathRefused as refused:
        data: dict[str, Any] = {"reason": refused.reason}
        if refused.path is not None:
            data["path"] = refused.path
        if refused.size_bytes is not None:
            data["sizeBytes"] = refused.size_bytes
        raise DaemonRequestError(
            ipc_errors.ATTACHMENT_PATH_REFUSED, str(refused), data
        ) from refused

class _OrgFsBridgeMixin:
    """Application cluster mixin; the state owner is DaemonApplication."""

    @classmethod
    def _orgfs_json(cls, value: Any) -> Any:
        if is_dataclass(value):
            return {
                ("contentState" if key == "content_state" else key): (
                    cls._orgfs_json(item)
                )
                for key, item in asdict(value).items()
            }
        if isinstance(value, tuple):
            return [cls._orgfs_json(item) for item in value]
        if isinstance(value, list):
            return [cls._orgfs_json(item) for item in value]
        if isinstance(value, dict):
            return {
                str(key): cls._orgfs_json(item)
                for key, item in value.items()
            }
        return value

    def _require_orgfs_runtime(self) -> OrgFsRuntime:
        if self._orgfs_runtime is None:
            self._orgfs_runtime = OrgFsRuntime(
                self.state_dir,
                node_id=self.node_id,
                author=(
                    self.owner
                    if self.owner.startswith("user:")
                    else canonical_user_uri(self.owner)
                ),
                logger=lambda level, event, **fields: self._log(
                    level, "orgfs", event, **fields
                ),
                owner_notifier=self._notify_orgfs_owner,
            )
        return self._orgfs_runtime

    def _notify_orgfs_owner(
        self, owner: str, event: str, details: dict[str, object]
    ) -> None:
        """Deliver resident-replica integrity alarms to the space owner."""

        owner_name = owner.removeprefix("user:")
        if self._user_delivery is None:
            self._log(
                "warn",
                "orgfs",
                "orgfs.owner-notice.unavailable",
                owner=owner,
                notice=event,
                **details,
            )
            return
        stable = json.dumps(details, sort_keys=True, separators=(",", ":"))
        message_id = str(
            uuid5(NAMESPACE_URL, f"hyprial:orgfs:{owner}:{event}:{stable}")
        )
        try:
            outcome = self._user_delivery.deliver(
                UserDeliveryRequest(
                    message_id=message_id,
                    idempotency_key=f"{_ORGFS_NOTICE_NAMESPACE}:{event}:{message_id}",
                    owner=owner_name,
                    sender=canonical_agent_uri(self.owner, self.node_id, "squire"),
                    message=(
                        f"orgfs resident replica reported {event}: "
                        f"{stable}"
                    ),
                    conversation_id=f"{_ORGFS_NOTICE_NAMESPACE}:{details.get('spaceId', 'unknown')}",
                )
            )
        except Exception as exc:  # noqa: BLE001 - integrity path must still reject
            self._log(
                "warn",
                "orgfs",
                "orgfs.owner-notice.failed",
                owner=owner,
                notice=event,
                detail=str(exc),
            )
            return
        self._log(
            "info" if outcome.accepted else "warn",
            "orgfs",
            (
                "orgfs.owner-notice.delivered"
                if outcome.accepted
                else "orgfs.owner-notice.failed"
            ),
            owner=owner,
            notice=event,
            messageId=outcome.message_id,
        )

    def _require_org_context_bridge(self) -> Any:
        if self._org_context_bridge is None:
            from hyprial.daemon.impl.org.orgfs_migration import OrgContextOrgFsBridge

            self._org_context_bridge = OrgContextOrgFsBridge(
                self.hyprial_home,
                self._require_orgfs_runtime(),
                logger=lambda level, event, **fields: self._log(
                    level, "org", event, **fields
                ),
            )
        return self._org_context_bridge

    def _handle_orgfs(
        self, method: str, params: JsonObject, *, trusted_origin: str | None = None
    ) -> Any:
        from hyprial.daemon.impl.orgfs.api import OrgFsError

        runtime = self._require_orgfs_runtime()
        fs = runtime.facade
        if method in _ORGFS_MUTATING_METHODS and _targets_org_acl_space(
            fs, method, params
        ):
            raise DaemonRequestError(
                ipc_errors.ORGFS_ACL_SPACE_EXTERNAL_MUTATION,
                "ACL spaces can be changed only by the daemon's internal org lifecycle",
                {"method": method},
            )

        if method in _ORG_CONTEXT_READ_METHODS:
            self._enforce_org_context(self._capability_caller(params))

        def required(name: str) -> str:
            return _required_string(params.get(name), name)

        def content_wait_seconds() -> float:
            raw = params.get("waitSeconds", ORGFS_CONTENT_WAIT_S)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise OrgFsError(
                    "invalid-argument",
                    {"message": "waitSeconds must be a number from 0 to 120"},
                )
            value = float(raw)
            if value != value or value < 0 or value > 120:
                raise OrgFsError(
                    "invalid-argument",
                    {"message": "waitSeconds must be a number from 0 to 120"},
                )
            return value

        def with_content_wait(
            space_id: str, operation: Callable[[], Any]
        ) -> Any:
            wait_seconds = content_wait_seconds()
            started = time.monotonic()
            deadline = started + wait_seconds
            while True:
                try:
                    return operation()
                except OrgFsError as exc:
                    if exc.code != ipc_errors.ORGFS_CONTENT_PENDING:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        details = dict(exc.details)
                        details["waitedSeconds"] = max(
                            0.0, time.monotonic() - started
                        )
                        raise OrgFsError(exc.code, details) from exc
                    node = str(exc.details.get("node", ""))
                    node_id = node[3:] if node.startswith("id:") else node
                    runtime.await_content(
                        space_id,
                        node_id,
                        deadline_monotonic=deadline,
                    )
                    remaining = deadline - time.monotonic()
                    if remaining > 0:
                        time.sleep(min(0.25, remaining))

        try:
            if method == "orgfs.spaces":
                return {"spaces": self._orgfs_json(fs.spaces())}
            if method == "orgfs.create":
                info = fs.create_space(required("name"))
                runtime.broadcast_pending(info.space_id)
                from hyprial.daemon.impl.org.orgfs_migration import ORG_CONTEXT_SPACE_NAME

                if info.name == ORG_CONTEXT_SPACE_NAME:
                    self._require_org_context_bridge().publish_accepted()
                return self._orgfs_json(info)
            if method == "orgfs.resolve":
                return {"nodes": self._orgfs_json(fs.resolve(required("spaceId"), required("path")))}
            if method == "orgfs.ls":
                node = str(params.get("path", params.get("node", "")))
                return {"nodes": self._orgfs_json(fs.listdir(required("spaceId"), node))}
            if method == "orgfs.stat":
                node = str(params.get("node", params.get("path", "")))
                return self._orgfs_json(fs.stat(required("spaceId"), node))
            if method == "orgfs.read":
                space_id = required("spaceId")
                node = str(params.get("node", params.get("path", "")))

                def read() -> Any:
                    info = fs.stat(space_id, node)
                    if info.kind == "doc":
                        text, version = fs.read_text(space_id, node)
                        if len(text.encode()) > 2 * 1024 * 1024:
                            raise OrgFsError("too-large", {"use": "export"})
                        return {"node": self._orgfs_json(info), "text": text, "version": version}
                    content = fs.read_bytes(space_id, node)
                    if len(content) > 2 * 1024 * 1024:
                        raise OrgFsError("too-large", {"use": "export"})
                    import base64

                    return {"node": self._orgfs_json(info), "contentB64": base64.b64encode(content).decode("ascii")}

                return with_content_wait(space_id, read)
            if method == "orgfs.write":
                space_id = required("spaceId")
                node = str(params.get("node", params.get("path", "")))
                has_text = "text" in params
                has_bytes = "contentB64" in params
                if has_text == has_bytes:
                    raise OrgFsError("invalid-argument", {"message": "provide exactly one of text or contentB64"})
                if has_text:
                    text = params.get("text")
                    if not isinstance(text, str):
                        raise OrgFsError("invalid-argument", {"message": "text must be a string"})
                    result = fs.write_text(
                        space_id,
                        node,
                        text,
                        base_version=params.get("baseVersion"),
                        expect_version=params.get("expectVersion"),
                    )
                else:
                    import base64

                    try:
                        content = base64.b64decode(required("contentB64"), validate=True)
                    except ValueError as exc:
                        raise OrgFsError("invalid-argument", {"message": "contentB64 is invalid"}) from exc
                    result = fs.write_bytes(
                        space_id, node, content, expect_version=params.get("expectVersion")
                    )
                return self._orgfs_json(result)
            if method == "orgfs.export":
                space_id = required("spaceId")
                node = str(params.get("node", params.get("path", "")))
                destination = required("destination")
                policy = self._orgfs_path_policy(params, method, trusted_origin)
                _authorized_or_refused(
                    lambda: authorize_write_path(destination, policy)
                )

                def export() -> Any:
                    info = fs.stat(space_id, node)
                    if info.size is not None and info.size > ORGFS_EXPORT_MAX_BYTES:
                        raise OrgFsError(
                            "too-large",
                            {"maxBytes": ORGFS_EXPORT_MAX_BYTES, "use": "export"},
                        )
                    data = fs.read_bytes(space_id, node)
                    if len(data) > ORGFS_EXPORT_MAX_BYTES:
                        raise OrgFsError(
                            "too-large",
                            {"maxBytes": ORGFS_EXPORT_MAX_BYTES, "use": "export"},
                        )
                    _authorized_or_refused(
                        lambda: write_authorized_file(
                            destination,
                            data,
                            policy,
                            overwrite=params.get("overwrite") is True,
                        )
                    )
                    return self._orgfs_json(info)

                return with_content_wait(space_id, export)
            if method == "orgfs.import":
                policy = self._orgfs_path_policy(params, method, trusted_origin)
                source = _authorized_or_refused(
                    lambda: read_authorized_file(
                        required("source"), policy, max_bytes=ORGFS_IMPORT_MAX_BYTES
                    )
                )
                return self._orgfs_json(
                    fs.write_bytes(
                        required("spaceId"),
                        str(params.get("node", params.get("path", ""))),
                        source.data,
                    )
                )
            if method == "orgfs.mkdir":
                return self._orgfs_json(fs.mkdir(required("spaceId"), required("path")))
            if method == "orgfs.move":
                return self._orgfs_json(
                    fs.move(
                        required("spaceId"),
                        str(params.get("sourcePath", params.get("source", ""))),
                        str(params.get("destinationPath", params.get("destination", ""))),
                    )
                )
            if method == "orgfs.remove":
                fs.remove(required("spaceId"), str(params.get("node", params.get("path", ""))))
                return {"ok": True}
            if method == "orgfs.history":
                before = params.get("before")
                return {
                    "history": self._orgfs_json(
                        fs.history(
                            required("spaceId"),
                            str(params.get("node", params.get("path", ""))),
                            int(params.get("limit", 50)),
                            str(before) if before is not None else None,
                        )
                    )
                }
            if method == "orgfs.read_at":
                import base64

                content = fs.read_at(required("spaceId"), required("node"), required("version"))
                return {"contentB64": base64.b64encode(content).decode("ascii")}
            if method == "orgfs.stat_at":
                return self._orgfs_json(
                    fs.stat_at(required("spaceId"), required("node"), required("version"))
                )
            if method == "orgfs.trash":
                return {"nodes": self._orgfs_json(fs.trash(required("spaceId"), int(params.get("limit", 100))))}
            if method == "orgfs.restore":
                return self._orgfs_json(
                    fs.restore(
                        required("spaceId"),
                        required("node"),
                        required("version"),
                        recursive=bool(params.get("recursive", True)),
                    )
                )
            if method == "orgfs.status":
                return self._orgfs_json(runtime.status(required("spaceId")))
            if method == "orgfs.purge_plan":
                targets = params.get("targets")
                if not isinstance(targets, list) or not all(
                    isinstance(target, dict) for target in targets
                ):
                    raise OrgFsError(
                        "invalid-argument",
                        {"message": "targets must be an array of objects"},
                    )
                return self._orgfs_json(
                    runtime.purge_plan(required("spaceId"), targets)
                )
            if method == "orgfs.purge":
                return self._orgfs_json(
                    runtime.purge(required("spaceId"), required("planId"))
                )
            if method == "orgfs.purge_status":
                return self._orgfs_json(
                    runtime.purge_status(required("spaceId"), required("planId"))
                )
            if method == "orgfs.unban":
                runtime.unban(required("spaceId"), required("sha"))
                return {"ok": True}
            if method == "orgfs.serve":
                return self._orgfs_json(
                    runtime.serve(
                        required("spaceId"), str(params.get("backend", "fs"))
                    )
                )
            if method == "orgfs.checkout":
                enabled = params.get("enabled")
                if type(enabled) is not bool:
                    raise OrgFsError(
                        "invalid-argument", {"message": "enabled must be a boolean"}
                    )
                return self._orgfs_json(
                    runtime.checkout(required("spaceId"), enabled)
                )
            if method == "orgfs.watch":
                return {
                    "events": self._orgfs_json(
                        runtime.watch_events(
                            required("spaceId"),
                            str(params.get("glob", "*")),
                            str(params["sinceVersion"]) if params.get("sinceVersion") is not None else None,
                        )
                    )
                }
            if method == "orgfs.invite":
                return self._orgfs_json(
                    fs.invite(
                        required("spaceId"), required("user"), str(params.get("mode", "rw"))
                    )
                )
            if method == "orgfs.remove_member":
                fs.remove_member(required("spaceId"), required("user"))
                return {"ok": True}
            if method == "orgfs.members":
                return {"members": self._orgfs_json(fs.members(required("spaceId")))}
            if method == "orgfs.join":
                info = fs.join(required("spaceId"))
                from hyprial.daemon.impl.org.orgfs_migration import ORG_CONTEXT_SPACE_NAME

                if info.name == ORG_CONTEXT_SPACE_NAME:
                    self._require_org_context_bridge().publish_accepted()
                return self._orgfs_json(info)
        except OrgFsError as exc:
            raise DaemonRequestError(exc.code, str(exc), exc.details) from exc
        except (TypeError, ValueError) as exc:
            raise DaemonRequestError(ipc_errors.INVALID_ARGUMENT, str(exc)) from exc
        raise DaemonRequestError(ipc_errors.METHOD_NOT_FOUND, f"unknown orgfs method: {method}")

    def _orgfs_path_policy(
        self, params: JsonObject, method: str, trusted_origin: str | None
    ) -> PathPolicy:
        """The caller's local-file authorization for orgfs import/export (#1169).

        The same rule as message attachments (path_authz): a session may use
        its own workspace and registered cwd, the operator anywhere outside
        the hyprial home and state dir, and nobody else any local file.
        """

        caller = self._authenticated_message_caller(
            params, method=method, trusted_origin=trusted_origin
        )
        policy = self._attachment_policy(caller, trusted_origin)
        if policy is None:
            raise DaemonRequestError(
                ipc_errors.ATTACHMENT_PATH_REFUSED,
                f"{method} needs an authenticated agent session or the local "
                "operator identity to touch a local file",
                {"reason": "caller-not-authenticated"},
            )
        return policy

    def _ipc_orgfs(
        self, method, params, _trusted_message_origin: str | None = None
    ) -> Any:
        return self._handle_orgfs(
            method, params, trusted_origin=_trusted_message_origin
        )
