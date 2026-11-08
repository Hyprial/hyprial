"""Daemon-owned ``work.*`` commands over an org directory space."""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from hyprial.daemon.impl.ipc.params import JsonObject, _required_string
from hyprial.daemon.impl.mcp.channel.ownership import (
    _process_identities_comparable,
    _process_identities_match,
    _read_process_identity as process_birth_identity,
    _read_process_parent,
)
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.pac.views import work_items
from hyprial.daemon.impl.application.messaging.orgfs_bridge import (
    _targets_org_acl_space,
)
from hyprial.daemon.impl.application.messaging.work_ledger import (
    WorkLedgerError,
    WorkVerificationLedger,
)
from hyprial.daemon.impl.application.messaging.work.conflicts import (
    ITEM_ID_PATTERN,
    _WorkConflictMixin,
    allocate_item_ids,
    log_line as _log_line,
    work_path as _path,
)
from hyprial.kernel import DaemonRequestError, ipc_errors
from hyprial.kernel import parse_agent_uri, parse_user_uri

_WORKFLOW_ID = re.compile(r"wf-[0-9a-f]{32}")
_STALE_RETRY_ATTEMPTS = 3
_HISTORY_PAGE_SIZE = 50
_HISTORY_CACHE_SIZE = 512
_HISTORY_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class _WorkHistory:
    current: work_items.WorkItem | None
    current_version: str
    action_versions: tuple[str, ...]
    error: work_items.WorkItemError | None
    last_valid: work_items.WorkItem | None
    last_valid_version: str | None


def _one_line(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise DaemonRequestError(
            "WORK_ITEM_INVALID", f"{name} must be one non-empty line"
        )
    return value.strip()


def _strings(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise DaemonRequestError(
            "WORK_ITEM_INVALID", f"{name} must be a list of strings"
        )
    return list(value)


class _WorkItemsMixin(_WorkConflictMixin):
    """Application mixin that keeps work-item guards inside the daemon."""

    def _work_caller(
        self, params: JsonObject, *, owner_only: bool = True
    ) -> tuple[str, bool]:
        """Return the verified agent or the explicitly session-less operator.

        LAX(identity-step-up): the private local socket still cannot prove the
        human operator. Any presented session is fenced first and is therefore
        always an agent, never an operator fallback.
        """

        if "sessionRef" in params:
            return self._workflow_caller(params), False
        try:
            ancestor = self._work_agent_ancestor()
        except DaemonRequestError as error:
            if owner_only or error.code != ipc_errors.CALLER_NOT_AUTHORIZED:
                raise
            return self._workflow_caller(params), True
        if ancestor is not None:
            return ancestor, False
        return self._workflow_caller(params), True

    def _work_known_agent_processes(self) -> dict[int, list[tuple[str, str]]]:
        known: dict[int, list[tuple[str, str]]] = {}
        for session in self._agent_session_domains.session.read_sessions():
            if session.process_pid is None or session.process_identity is None:
                continue
            known.setdefault(session.process_pid, []).append(
                (session.actor, session.process_identity)
            )
        harnesses = self._harnesses
        read_identities = getattr(
            harnesses, "projected_worker_process_identities", None
        )
        identities = read_identities() if callable(read_identities) else {}
        specs = self.desired_state.load().harnesses
        for spec in specs:
            identity = identities.get((spec.harness, spec.name))
            if identity is None:
                continue
            pid, marker = identity
            actor = self._canonical_harness_uri(spec.name, spec)
            known.setdefault(pid, []).append((actor, marker))
        return known

    def _work_agent_ancestor(self) -> str | None:
        """Resolve a session-less socket caller from kernel process ancestry."""

        peer_pid = getattr(self._ipc_peer, "pid", None)
        if not isinstance(peer_pid, int) or peer_pid <= 0:
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "operator-only work action refused: IPC process ancestry is unavailable",
            )
        known = self._work_known_agent_processes()
        seen: set[int] = set()
        current = peer_pid
        while current > 1 and current not in seen and len(seen) < 128:
            seen.add(current)
            candidates = known.get(current, ())
            if candidates:
                observed = process_birth_identity(current)
                if observed is None:
                    raise DaemonRequestError(
                        ipc_errors.CALLER_NOT_AUTHORIZED,
                        "operator-only work action refused: IPC process ancestry identity is unreadable",
                    )
                incomparable = False
                for actor, expected in candidates:
                    if _process_identities_match(expected, observed):
                        return actor
                    if not _process_identities_comparable(expected, observed):
                        incomparable = True
                if incomparable:
                    raise DaemonRequestError(
                        ipc_errors.CALLER_NOT_AUTHORIZED,
                        "operator-only work action refused: IPC process ancestry identity is not comparable",
                    )
            parent = _read_process_parent(current)
            if parent is None or parent < 0:
                raise DaemonRequestError(
                    ipc_errors.CALLER_NOT_AUTHORIZED,
                    "operator-only work action refused: IPC process ancestry is unreadable",
                )
            current = parent
        if current in seen or current > 1:
            raise DaemonRequestError(
                ipc_errors.CALLER_NOT_AUTHORIZED,
                "operator-only work action refused: IPC process ancestry did not terminate",
            )
        return None

    @staticmethod
    def _work_raise(error: Exception) -> None:
        if isinstance(error, work_items.WorkItemError):
            raise DaemonRequestError(error.code, str(error), error.data) from error
        if isinstance(error, OrgFsError):
            raise DaemonRequestError(error.code, str(error), error.details) from error
        raise error

    def _work_read_snapshot(
        self, space_id: str, item_id: str
    ) -> tuple[work_items.WorkItem, str, tuple[str, ...]]:
        inspected = self._work_inspect_history(space_id, item_id)
        if inspected.current is None:
            assert inspected.error is not None
            raise inspected.error
        return inspected.current, inspected.current_version, inspected.action_versions

    def _work_inspect_history(self, space_id: str, item_id: str) -> _WorkHistory:
        fs = self._require_orgfs_runtime().facade
        snapshot = fs.read_text_snapshot(space_id, _path(item_id))
        # A purge replaces the physical document while it may retain the
        # logical head version. Include that replacement fence so cached
        # validation can never survive a history retirement.
        cache_key = (space_id, item_id, snapshot.node.doc_id, snapshot.version)
        with _HISTORY_CACHE_LOCK:
            cache = getattr(self, "_work_history_cache", None)
            if cache is None:
                cache = OrderedDict()
                self._work_history_cache = cache
            cached = cache.get(cache_key)
            if cached is not None:
                cache.move_to_end(cache_key)
                return cached
        node = f"id:{snapshot.node.node_id}"
        history: list[tuple[str, str]] = []
        entries = []
        before = None
        while True:
            page = fs.history(
                space_id,
                node,
                limit=_HISTORY_PAGE_SIZE,
                before=before,
            )
            if not page:
                break
            for entry in page:
                entries.append(entry)
                if entry.node.version == snapshot.version:
                    continue
                try:
                    previous_text = fs.read_at(
                        space_id, node, entry.node.version
                    ).decode("utf-8")
                except OrgFsError as error:
                    if error.code not in {"purged", "unknown-doc"}:
                        raise
                    return self._work_incomplete_history(
                        space_id, item_id, snapshot.version
                    )
                except UnicodeError as error:
                    raise work_items.WorkItemError(
                        "WORK_ITEM_INVALID",
                        "previous work item version is not UTF-8",
                    ) from error
                history.append((entry.node.version, previous_text))
            before = page[-1].node.version
        if (
            not entries
            or entries[0].node.version != snapshot.version
            or entries[-1].changed != "created"
        ):
            return self._work_incomplete_history(
                space_id, item_id, snapshot.version
            )
        chain = [*reversed(history), (snapshot.version, snapshot.content)]
        last_valid: work_items.WorkItem | None = None
        last_valid_version: str | None = None
        action_versions: list[str] = []
        invalid_version: str | None = None
        latest_error: work_items.WorkItemError | None = None
        valid = False
        for version, text in chain:
            try:
                candidate = work_items.parse_work_item(text)
                if last_valid is None:
                    work_items.validate_work_item_state(candidate)
                    if invalid_version is not None:
                        raise work_items.WorkItemError(
                            "WORK_ITEM_INVALID", "no validating version precedes repair"
                        )
                    new_action_versions = [version] * len(
                        candidate.head["ownerActions"]
                    )
                elif valid:
                    work_items.validate_work_item_history(last_valid, candidate)
                    added = len(candidate.head["ownerActions"]) - len(
                        last_valid.head["ownerActions"]
                    )
                    new_action_versions = [*action_versions, *([version] * added)]
                else:
                    assert invalid_version is not None
                    assert last_valid_version is not None
                    if not work_items.is_exact_repair(
                        last_valid,
                        candidate,
                        invalid_version=invalid_version,
                        restored_version=last_valid_version,
                    ):
                        raise work_items.WorkItemError(
                            "WORK_ITEM_INVALID",
                            "invalid history was not restored by an exact repair",
                        )
                    new_action_versions = [*action_versions, version]
            except work_items.WorkItemError as error:
                if valid or latest_error is None:
                    latest_error = error
                valid = False
                invalid_version = version
                continue
            last_valid = candidate
            last_valid_version = version
            action_versions = new_action_versions
            valid = True
            invalid_version = None
            latest_error = None
        if valid and last_valid is not None and last_valid.id != item_id:
            latest_error = work_items.WorkItemError(
                "WORK_ITEM_INVALID",
                f"file id {last_valid.id} does not match {item_id}",
            )
            valid = False
        inspected = _WorkHistory(
            current=last_valid if valid else None,
            current_version=snapshot.version,
            action_versions=tuple(action_versions),
            error=latest_error,
            last_valid=last_valid,
            last_valid_version=last_valid_version,
        )
        with _HISTORY_CACHE_LOCK:
            cache[cache_key] = inspected
            cache.move_to_end(cache_key)
            while len(cache) > _HISTORY_CACHE_SIZE:
                cache.popitem(last=False)
        return inspected

    def _work_incomplete_history(
        self, space_id: str, item_id: str, version: str
    ) -> _WorkHistory:
        self._log(
            "warn",
            "daemon",
            "daemon.work.history_incomplete",
            spaceId=space_id,
            itemId=item_id,
        )
        return _WorkHistory(
            current=None,
            current_version=version,
            action_versions=(),
            error=work_items.WorkItemError(
                "WORK_ITEM_INVALID",
                "work item history is incomplete after purge",
                {"unverifiedOperator": True, "historyIncomplete": True},
            ),
            last_valid=None,
            last_valid_version=None,
        )

    def _work_read(
        self, space_id: str, item_id: str
    ) -> tuple[work_items.WorkItem, str, tuple[str, ...]]:
        try:
            return self._work_read_snapshot(space_id, item_id)
        except (OrgFsError, work_items.WorkItemError) as error:
            self._work_raise(error)
        raise AssertionError("work item read returned without an outcome")

    def _work_result(
        self,
        item: work_items.WorkItem,
        version: str,
        space_id: str,
        action_versions: tuple[str, ...] = (),
    ) -> JsonObject:
        try:
            rows = WorkVerificationLedger(
                self.state_dir / "work-verification-ledger.json"
            ).rows()
        except WorkLedgerError as error:
            self._work_record_ledger_corruption(error)
            rows = ()
        states: list[str] = []
        for action, action_version in zip(
            item.head["ownerActions"], action_versions, strict=False
        ):
            proof = next(
                (
                    row
                    for row in reversed(rows)
                    if row.get("itemId") == item.id
                    and row.get("spaceId") == space_id
                    and row.get("action") == action["action"]
                    and row.get("caller") == action["actor"]
                    and row.get("producedVersion") == action_version
                ),
                None,
            )
            states.append(
                "verified"
                if proof is not None and proof.get("callerVerified") is True
                else "unverified"
                if proof is not None
                else "unverified (remote)"
            )
        projected = work_items.project_owner_actions(item, states)
        return {
            "spaceId": space_id,
            "version": version,
            "item": item.to_dict(owner_actions=projected),
        }

    @staticmethod
    def _work_invalid(
        item_id: str,
        reason: str,
        version: str,
        space_id: str,
        *,
        node_id: str | None = None,
        raw: str | None = None,
        unverified_operator: bool = False,
    ) -> JsonObject:
        return {
            "spaceId": space_id,
            "version": version,
            **({"nodeId": node_id} if node_id is not None else {}),
            **({"raw": raw} if raw is not None else {}),
            "item": {
                "id": item_id,
                "path": _path(item_id),
                **({"nodeId": node_id} if node_id is not None else {}),
                "invalid": True,
                "reason": reason,
                **(
                    {"unverifiedOperator": True}
                    if unverified_operator
                    else {}
                ),
            },
        }

    @staticmethod
    def _work_owner_action(
        item: work_items.WorkItem,
        *,
        action: str,
        caller: str,
        unverified_operator: bool,
    ) -> work_items.WorkItem:
        recorded = work_items.append_owner_action(
            item,
            action=action,
            actor=caller,
        )
        suffix = " (unverified operator)" if unverified_operator else ""
        label = "acceptance set" if action == "acceptance" else "done"
        return work_items.replace_log(
            recorded,
            [*recorded.log, _log_line(caller, f"{label}{suffix}")],
        )

    def _work_mutate(
        self,
        *,
        space_id: str,
        item_id: str,
        caller: str,
        unverified_operator: bool,
        change: Callable[[work_items.WorkItem], work_items.WorkItem | None],
    ) -> JsonObject:
        fs = self._require_orgfs_runtime().facade
        for attempt in range(1, _STALE_RETRY_ATTEMPTS + 1):
            before, version, action_versions = self._work_read(space_id, item_id)
            try:
                after = change(before)
                if after is None:
                    return self._work_result(
                        before, version, space_id, action_versions
                    )
                work_items.validate_work_item_update(before, after, caller=caller)
                work_items.validate_work_item_state(after)
                text = work_items.serialize_work_item(after)
                written = fs.write_text(
                    space_id,
                    _path(item_id),
                    text,
                    expect_version=version,
                )
            except OrgFsError as error:
                if error.code != "stale-write":
                    self._work_raise(error)
                if attempt == _STALE_RETRY_ATTEMPTS:
                    raise DaemonRequestError(
                        "WORK_ITEM_VERSION_CONFLICT",
                        "work item kept changing while this update was retried",
                        {"attempts": _STALE_RETRY_ATTEMPTS, "itemId": item_id},
                    ) from error
                continue
            except work_items.WorkItemError as error:
                self._work_raise(error)
            data = self._orgfs_json(written)
            produced_version = str(data.get("version") or version)
            self._work_record_owner_actions(
                before,
                after,
                space_id=space_id,
                produced_version=produced_version,
                caller_verified=not unverified_operator,
            )
            current, current_version, current_actions = self._work_read_snapshot(
                space_id, item_id
            )
            return self._work_result(
                current, current_version, space_id, current_actions
            )
        raise AssertionError("work item retry loop exhausted without an outcome")

    def _work_record_owner_actions(
        self,
        before: work_items.WorkItem | None,
        after: work_items.WorkItem,
        *,
        space_id: str,
        produced_version: str,
        caller_verified: bool,
        invalid_version: str | None = None,
        restored_version: str | None = None,
    ) -> None:
        offset = len(before.head["ownerActions"]) if before is not None else 0
        ledger = WorkVerificationLedger(
            self.state_dir / "work-verification-ledger.json"
        )
        for action in after.head["ownerActions"][offset:]:
            row: JsonObject = {
                "action": str(action["action"]),
                "itemId": after.id,
                "spaceId": space_id,
                "caller": str(action["actor"]),
                "producedVersion": produced_version,
                "callerVerified": caller_verified,
            }
            if action["action"] == "repair":
                row.update(
                    {
                        "invalidVersion": invalid_version,
                        "restoredVersion": restored_version,
                    }
                )
            try:
                ledger.append(row)
            except WorkLedgerError as error:
                self._work_record_ledger_corruption(error)
                return

    def _work_record_ledger_corruption(self, error: WorkLedgerError) -> None:
        self._log(
            "warn",
            "daemon",
            "daemon.work.ledger_corrupt",
            errorType=type(error).__name__,
        )

    def _work_add(
        self, params: JsonObject, caller: str, unverified_operator: bool
    ) -> JsonObject:
        fs = self._require_orgfs_runtime().facade
        space_id = _required_string(params.get("spaceId"), "spaceId")
        title = _one_line(params.get("title"), "title")
        owner = params.get("owner", caller)
        if not isinstance(owner, str) or (
            parse_agent_uri(owner) is None and parse_user_uri(owner) is None
        ):
            raise DaemonRequestError(
                "WORK_ITEM_INVALID_OWNER", "owner must be a canonical user or agent URI"
            )
        acceptance = params.get("acceptance", "")
        if not isinstance(acceptance, str):
            raise DaemonRequestError("WORK_ITEM_INVALID", "acceptance must be text")
        if acceptance and owner != caller:
            raise DaemonRequestError(
                "WORK_ITEM_OWNER_REQUIRED",
                "only the work item owner may set acceptance",
            )
        checklist = _strings(params.get("checklist", []), "checklist")
        depends = _strings(params.get("depends", []), "depends")
        assignee = params.get("assignee")
        if assignee is not None and not isinstance(assignee, str):
            raise DaemonRequestError(
                "WORK_ITEM_INVALID", "assignee must be a string or null"
            )
        try:
            try:
                fs.listdir(space_id, "work")
            except OrgFsError as error:
                if error.code != "unknown-doc":
                    raise
                try:
                    fs.mkdir(space_id, "work")
                except OrgFsError as mkdir_error:
                    if mkdir_error.code != "invalid-argument":
                        raise
                    fs.listdir(space_id, "work")
            item_id = allocate_item_ids(fs, space_id, [owner])[0]
            item = work_items.new_work_item(
                item_id=item_id,
                title=title,
                owner=owner,
                assignee=assignee if isinstance(assignee, str) else None,
                checklist=checklist,
                depends=depends,
                acceptance=acceptance,
                opened=int(time.time() * 1000),
            )
            if acceptance:
                item = self._work_owner_action(
                    item,
                    action="acceptance",
                    caller=caller,
                    unverified_operator=unverified_operator,
                )
            written = fs.write_text(
                space_id,
                _path(item_id),
                work_items.serialize_work_item(item),
                create_only=True,
            )
        except OrgFsError as error:
            if error.code == "stale-write":
                raise DaemonRequestError(
                    "WORK_ITEM_ALREADY_EXISTS",
                    f"work item {item_id} was created concurrently",
                    {"itemId": item_id},
                ) from error
            self._work_raise(error)
        except work_items.WorkItemError as error:
            self._work_raise(error)
        data = self._orgfs_json(written)
        produced_version = str(data.get("version") or "")
        self._work_record_owner_actions(
            None,
            item,
            space_id=space_id,
            produced_version=produced_version,
            caller_verified=not unverified_operator,
        )
        current, version, action_versions = self._work_read_snapshot(
            space_id, item_id
        )
        return self._work_result(current, version, space_id, action_versions)

    def _work_show(self, params: JsonObject) -> JsonObject:
        space_id = _required_string(params.get("spaceId"), "spaceId")
        item_id = _required_string(params.get("itemId"), "itemId")
        if "node" in params:
            return self._work_show_node(params)
        self._work_assert_unambiguous(space_id, item_id)
        try:
            item, version, action_versions = self._work_read_snapshot(
                space_id, item_id
            )
        except work_items.WorkItemError as error:
            data = error.data if isinstance(error.data, dict) else {}
            return self._work_invalid(
                item_id,
                str(error),
                locals().get("version", ""),
                space_id,
                unverified_operator=data.get("unverifiedOperator") is True,
            )
        except OrgFsError as error:
            self._work_raise(error)
        return self._work_result(item, version, space_id, action_versions)

    def _work_list(self, params: JsonObject) -> JsonObject:
        fs = self._require_orgfs_runtime().facade
        space_id = _required_string(params.get("spaceId"), "spaceId")
        status = params.get("status")
        owner = params.get("owner")
        assignee = params.get("assignee")
        invalid_only = params.get("invalid") is True
        items: list[JsonObject] = []
        try:
            try:
                rows = fs.listdir(space_id, "work")
            except OrgFsError as error:
                if error.code == "unknown-doc":
                    rows = []
                else:
                    raise
            for row in rows:
                item_id = row.name.removesuffix(".md")
                if row.deleted or ITEM_ID_PATTERN.fullmatch(item_id) is None:
                    continue
                if row.name_conflict:
                    items.append(
                        self._work_invalid(
                            item_id,
                            "path has a name conflict",
                            row.version,
                            space_id,
                            node_id=row.node_id,
                        )["item"]
                    )
                    continue
                try:
                    item, version, action_versions = self._work_read_snapshot(
                        space_id, item_id
                    )
                except work_items.WorkItemError as error:
                    data = error.data if isinstance(error.data, dict) else {}
                    items.append(
                        self._work_invalid(
                            item_id,
                            str(error),
                            row.version,
                            space_id,
                            unverified_operator=(
                                data.get("unverifiedOperator") is True
                            ),
                        )["item"]
                    )
                    continue
                except OrgFsError as error:
                    items.append(
                        self._work_invalid(
                            item_id, f"unreadable: {error}", row.version, space_id
                        )["item"]
                    )
                    continue
                value = self._work_result(
                    item, version, space_id, action_versions
                )["item"]
                if invalid_only:
                    continue
                if status is not None and value["status"] != status:
                    continue
                if owner is not None and value["owner"] != owner:
                    continue
                if assignee is not None and value["assignee"] != assignee:
                    continue
                items.append(value)
        except OrgFsError as error:
            self._work_raise(error)
        items.sort(key=lambda value: str(value["id"]))
        return {"spaceId": space_id, "items": items}

    def _work_repair(
        self,
        *,
        space_id: str,
        item_id: str,
        caller: str,
        unverified_operator: bool,
    ) -> JsonObject:
        fs = self._require_orgfs_runtime().facade
        for attempt in range(1, _STALE_RETRY_ATTEMPTS + 1):
            try:
                history = self._work_inspect_history(space_id, item_id)
            except (OrgFsError, work_items.WorkItemError) as error:
                self._work_raise(error)
            if history.current is not None:
                raise DaemonRequestError(
                    "WORK_ITEM_NOT_INVALID",
                    f"work item {item_id} already passes validation",
                )
            restored = history.last_valid
            restored_version = history.last_valid_version
            if restored is None or restored_version is None:
                # OrgFS history retirement is an explicit owner-reviewed purge,
                # never routine retention. With no surviving validating version,
                # repair must fail closed instead of blessing the replacement head.
                raise DaemonRequestError(
                    "WORK_ITEM_NO_VALID_VERSION",
                    f"work item {item_id} has no validating version to restore",
                )
            if caller != restored.owner:
                raise DaemonRequestError(
                    "WORK_ITEM_OWNER_REQUIRED",
                    "only the work item owner may repair it",
                )
            invalid_version = history.current_version
            repaired = work_items.append_owner_action(
                restored,
                action="repair",
                actor=caller,
                invalid_version=invalid_version,
                restored_version=restored_version,
            )
            repaired = work_items.replace_log(
                repaired,
                [
                    *restored.log,
                    _log_line(
                        caller,
                        f"repair {invalid_version} -> {restored_version}"
                        + (
                            " (unverified operator)"
                            if unverified_operator
                            else ""
                        ),
                    ),
                ],
            )
            try:
                written = fs.write_text(
                    space_id,
                    _path(item_id),
                    work_items.serialize_work_item(repaired),
                    expect_version=invalid_version,
                )
            except OrgFsError as error:
                if error.code != "stale-write":
                    self._work_raise(error)
                if attempt == _STALE_RETRY_ATTEMPTS:
                    raise DaemonRequestError(
                        "WORK_ITEM_VERSION_CONFLICT",
                        "work item kept changing while repair was retried",
                        {"attempts": _STALE_RETRY_ATTEMPTS, "itemId": item_id},
                    ) from error
                continue
            data = self._orgfs_json(written)
            produced_version = str(data.get("version") or invalid_version)
            self._work_record_owner_actions(
                restored,
                repaired,
                space_id=space_id,
                produced_version=produced_version,
                caller_verified=not unverified_operator,
                invalid_version=invalid_version,
                restored_version=restored_version,
            )
            current, version, action_versions = self._work_read_snapshot(
                space_id, item_id
            )
            return self._work_result(current, version, space_id, action_versions)
        raise AssertionError("work item repair retry loop exhausted without an outcome")

    def _handle_work(self, method: str, params: JsonObject) -> JsonObject:
        space_id = _required_string(params.get("spaceId"), "spaceId")
        fs = self._require_orgfs_runtime().facade
        if method not in {"work.show", "work.ls"} and _targets_org_acl_space(
            fs, "orgfs.write", params
        ):
            raise DaemonRequestError(
                ipc_errors.ORGFS_ACL_SPACE_EXTERNAL_MUTATION,
                "ACL spaces can be changed only by the daemon's internal org lifecycle",
                {"method": method},
            )
        if method == "work.show":
            self._enforce_org_context(self._capability_caller(params))
            return self._work_show(params)
        if method == "work.ls":
            self._enforce_org_context(self._capability_caller(params))
            return self._work_list(params)
        acceptance = params.get("acceptance")
        owner_only = (
            method in {"work.done", "work.repair", "work.resolve"}
            or (method == "work.status" and params.get("status") == "done")
            or (
                method == "work.add"
                and isinstance(acceptance, str)
                and bool(acceptance)
            )
        )
        caller, unverified_operator = self._work_caller(
            params, owner_only=owner_only
        )
        if method == "work.add":
            return self._work_add(params, caller, unverified_operator)

        item_id = _required_string(params.get("itemId"), "itemId")
        if method == "work.repair":
            return self._work_repair(
                space_id=space_id,
                item_id=item_id,
                caller=caller,
                unverified_operator=unverified_operator,
            )
        if method == "work.resolve":
            return self._work_resolve(
                space_id=space_id,
                item_id=item_id,
                keep=params.get("keep"),
                caller=caller,
                unverified_operator=unverified_operator,
            )

        def change(item: work_items.WorkItem) -> work_items.WorkItem | None:
            if method == "work.check":
                number = params.get("number")
                checklist = [dict(entry) for entry in item.head["checklist"]]
                if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                    raise DaemonRequestError(
                        "WORK_ITEM_CHECKLIST_INDEX", "checklist number must be positive"
                    )
                if number > len(checklist):
                    raise DaemonRequestError(
                        "WORK_ITEM_CHECKLIST_INDEX",
                        f"checklist item {number} does not exist",
                        {"size": len(checklist)},
                    )
                checklist[number - 1]["done"] = True
                return work_items.replace_head(item, checklist=checklist)
            if method == "work.note":
                note = _one_line(params.get("text"), "note")
                if unverified_operator:
                    note += " (unverified operator)"
                return work_items.replace_log(
                    item, [*item.log, _log_line(caller, note)]
                )
            if method in {"work.status", "work.done"}:
                status = "done" if method == "work.done" else params.get("status")
                if status not in work_items.WORK_STATUSES:
                    raise DaemonRequestError(
                        "WORK_ITEM_INVALID_STATUS",
                        "status must be one of " + ", ".join(work_items.WORK_STATUSES),
                    )
                closed = int(time.time() * 1000) if status in {"done", "dropped"} else None
                changed = work_items.replace_head(item, status=status, closed=closed)
                if status == "done":
                    if caller != item.owner:
                        raise DaemonRequestError(
                            "WORK_ITEM_OWNER_REQUIRED",
                            "only the work item owner may mark it done",
                        )
                    changed = self._work_owner_action(
                        changed,
                        action="done",
                        caller=caller,
                        unverified_operator=unverified_operator,
                    )
                return changed
            if method == "work.link":
                graph_id = _required_string(params.get("graphId"), "graphId")
                if _WORKFLOW_ID.fullmatch(graph_id) is None:
                    raise DaemonRequestError(
                        "WORK_ITEM_INVALID_GRAPH_ID",
                        "graph id must be a canonical wf-<32 lowercase hex> id",
                    )
                pacs = list(item.head["pacs"])
                if graph_id in pacs:
                    return None
                label = f"linked {graph_id}"
                if unverified_operator:
                    label += " (unverified operator)"
                return work_items.replace_log(
                    work_items.replace_head(item, pacs=[*pacs, graph_id]),
                    [*item.log, _log_line(caller, label)],
                )
            if method == "work.keywords":
                additions = _strings(params.get("add", []), "add")
                removals = _strings(params.get("remove", []), "remove")
                if any(not value or "\n" in value or "\r" in value for value in additions + removals):
                    raise DaemonRequestError(
                        "WORK_ITEM_INVALID", "keywords must be non-empty one-line strings"
                    )
                remove = set(removals)
                keywords = [value for value in item.head["keywords"] if value not in remove]
                keywords.extend(value for value in additions if value not in keywords)
                summary = (
                    f"keywords add={','.join(additions) or '-'} "
                    f"remove={','.join(removals) or '-'}"
                )
                if unverified_operator:
                    summary += " (unverified operator)"
                return work_items.replace_log(
                    work_items.replace_head(item, keywords=keywords),
                    [*item.log, _log_line(caller, summary)],
                )
            raise DaemonRequestError(
                ipc_errors.METHOD_NOT_FOUND, f"unknown work method {method}"
            )

        return self._work_mutate(
            space_id=space_id,
            item_id=item_id,
            caller=caller,
            unverified_operator=unverified_operator,
            change=change,
        )
