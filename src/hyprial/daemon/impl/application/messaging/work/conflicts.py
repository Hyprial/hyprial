"""Name-conflict reads and lossless ``work.resolve`` splitting."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from hyprial.daemon.impl.application.messaging.work_ledger import (
    WorkLedgerError,
    WorkVerificationLedger,
)
from hyprial.daemon.impl.ipc.params import JsonObject, _required_string
from hyprial.daemon.impl.orgfs.api import OrgFsError
from hyprial.daemon.impl.pac.views import work_items
from hyprial.kernel import (
    DaemonRequestError,
    canonical_user_uri,
    parse_agent_uri,
    parse_user_uri,
)

ITEM_ID_PATTERN = re.compile(r"M-[A-Za-z0-9._-]+-\d{4,}")


def work_path(item_id: str) -> str:
    if ITEM_ID_PATTERN.fullmatch(item_id) is None:
        raise DaemonRequestError(
            "WORK_ITEM_INVALID_ID", "work item id must match M-<owner>-NNNN"
        )
    return f"work/{item_id}.md"


def owner_slug(owner: str) -> str:
    parsed = parse_agent_uri(owner)
    raw = parsed[2] if parsed is not None else owner.removeprefix("user:")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.").lower()
    if not slug:
        raise DaemonRequestError(
            "WORK_ITEM_INVALID_OWNER", "owner cannot produce a work item id"
        )
    return slug


def _is_rw_member(fs: Any, space_id: str, caller: str) -> bool:
    """Whether ``caller`` (a user, or an agent through its owner) has rw."""

    agent = parse_agent_uri(caller)
    if agent is not None:
        try:
            principal = canonical_user_uri(agent[0])
        except ValueError:
            return False
    elif parse_user_uri(caller) is not None:
        principal = caller
    else:
        return False
    try:
        members = fs.members(space_id)
    except OrgFsError:
        return False
    return any(
        member.user == principal and member.mode == "rw" for member in members
    )



def _is_split_of(item: Any, item_id: str) -> bool:
    """Whether ``item`` is a copy an earlier resolve already split off ``item_id``."""

    return bool(item.log) and re.fullmatch(
        r"- \S+ .+: resolve split from "
        + re.escape(item_id)
        + r" \(conflict\)(?: \(unverified operator\))?",
        item.log[-1],
    ) is not None

def log_line(caller: str, text: str) -> str:
    return f"- {datetime.now(UTC).isoformat()} {caller}: {text}"


def allocate_item_ids(fs: Any, space_id: str, owners: list[str]) -> list[str]:
    """Allocate distinct owner-scoped ids from the current work directory."""

    rows = fs.listdir(space_id, "work")
    used = {
        row.name.removesuffix(".md")
        for row in rows
        if not row.deleted and row.name.endswith(".md")
    }
    allocated: list[str] = []
    for owner in owners:
        prefix = f"M-{owner_slug(owner)}-"
        numbers = [
            int(match.group(1))
            for value in used
            if (match := re.fullmatch(re.escape(prefix) + r"(\d{4,})", value))
            is not None
        ]
        item_id = f"{prefix}{max(numbers, default=0) + 1:04d}"
        used.add(item_id)
        allocated.append(item_id)
    return allocated


def _node_ref(value: object, name: str) -> str:
    ref = _required_string(value, name)
    if not ref.startswith("id:") or len(ref) == 3:
        raise DaemonRequestError(
            "WORK_ITEM_INVALID_NODE", f"{name} must be id:<nodeId>"
        )
    return ref


class _WorkConflictMixin:
    """Conflict methods composed into the daemon work-item application mixin."""

    def _work_nodes(self, space_id: str, item_id: str):  # type: ignore[no-untyped-def]
        fs = self._require_orgfs_runtime().facade
        try:
            return tuple(
                sorted(
                    fs.resolve(space_id, work_path(item_id)),
                    key=lambda node: node.node_id,
                )
            )
        except OrgFsError as error:
            self._work_raise(error)
        raise AssertionError("work item resolution returned without an outcome")

    def _work_assert_unambiguous(self, space_id: str, item_id: str) -> None:
        nodes = self._work_nodes(space_id, item_id)
        if len(nodes) > 1:
            raise DaemonRequestError(
                "WORK_ITEM_NAME_CONFLICT",
                f"work item {item_id} has multiple copies",
                {"itemId": item_id, "nodeIds": [node.node_id for node in nodes]},
            )

    def _work_show_node(self, params: JsonObject) -> JsonObject:
        fs = self._require_orgfs_runtime().facade
        space_id = _required_string(params.get("spaceId"), "spaceId")
        item_id = _required_string(params.get("itemId"), "itemId")
        node_ref = _node_ref(params.get("node"), "node")
        node_id = node_ref[3:]
        nodes = self._work_nodes(space_id, item_id)
        if node_id not in {node.node_id for node in nodes}:
            raise DaemonRequestError(
                "WORK_ITEM_CONFLICT_NODE_UNKNOWN",
                f"node {node_ref} is not a copy of {item_id}",
                {"itemId": item_id, "nodeIds": [node.node_id for node in nodes]},
            )
        try:
            snapshot = fs.read_text_snapshot(space_id, node_ref)
            item = work_items.parse_work_item(snapshot.content)
            if item.id != item_id:
                raise work_items.WorkItemError(
                    "WORK_ITEM_INVALID",
                    f"file id {item.id} does not match {item_id}",
                )
            work_items.validate_work_item_state(item)
        except work_items.WorkItemError as error:
            data = error.data if isinstance(error.data, dict) else {}
            result = self._work_invalid(
                item_id,
                str(error),
                snapshot.version,
                space_id,
                node_id=node_id,
                raw=snapshot.content,
                unverified_operator=data.get("unverifiedOperator") is True,
            )
            return result
        except OrgFsError as error:
            self._work_raise(error)
        return {
            **self._work_result(item, snapshot.version, space_id),
            "nodeId": node_id,
            "raw": snapshot.content,
        }

    def _work_move_copy(self, fs: Any, space_id: str, node_ref: str, new_id: str) -> None:
        """Move one conflicted copy to its new id; never onto an existing item."""

        try:
            occupied = fs.resolve(space_id, work_path(new_id))
        except OrgFsError:
            occupied = ()
        if occupied:
            raise DaemonRequestError(
                "WORK_ITEM_ALREADY_EXISTS",
                f"{new_id} appeared while resolving; run resolve again",
                {"itemId": new_id},
            )
        fs.move(space_id, node_ref, work_path(new_id))

    def _work_resolve(
        self,
        *,
        space_id: str,
        item_id: str,
        keep: object,
        caller: str,
        unverified_operator: bool,
    ) -> JsonObject:
        fs = self._require_orgfs_runtime().facade
        # Any read-write member may split a name conflict: resolve changes no
        # content and deletes nothing, it only gives each copy its own id and
        # records who did it.  Squire runs it for a user it is never the
        # operator of, so an owner-only guard would block the patrol.
        # ``repair`` changes content and stays owner-only.
        if not _is_rw_member(fs, space_id, caller):
            raise DaemonRequestError(
                "WORK_ITEM_MEMBER_REQUIRED",
                "only a read-write member of the org may resolve a name conflict",
                {"caller": caller},
            )
        keep_ref = _node_ref(keep, "keep")
        kept_node = keep_ref[3:]
        nodes = self._work_nodes(space_id, item_id)
        if len(nodes) < 2:
            raise DaemonRequestError(
                "WORK_ITEM_NOT_CONFLICTED", f"work item {item_id} is not conflicted"
            )
        node_ids = [node.node_id for node in nodes]
        if kept_node not in node_ids:
            raise DaemonRequestError(
                "WORK_ITEM_CONFLICT_NODE_UNKNOWN",
                f"node {keep_ref} is not a copy of {item_id}",
                {"itemId": item_id, "nodeIds": node_ids},
            )

        try:
            kept_snapshot = fs.read_text_snapshot(space_id, keep_ref)
            kept = work_items.parse_work_item(kept_snapshot.content)
            if kept.id != item_id:
                raise work_items.WorkItemError(
                    "WORK_ITEM_INVALID",
                    f"the kept copy's file id {kept.id} does not match {item_id}",
                )
            work_items.validate_work_item_state(kept)
        except (OrgFsError, work_items.WorkItemError) as error:
            self._work_raise(error)

        # Only the kept copy has to parse.  Every other copy is either split
        # (renamed in place, then moved), finished (an interrupted resolve
        # already renamed it: it carries its new id and the split line, so it
        # only needs its move), or moved as-is when it does not parse.
        to_split: list[tuple[Any, Any, Any]] = []
        unparsed: list[Any] = []
        finished: list[tuple[Any, str]] = []
        for node in nodes:
            if node.node_id == kept_node:
                continue
            try:
                snapshot = fs.read_text_snapshot(space_id, f"id:{node.node_id}")
            except OrgFsError as error:
                self._work_raise(error)
            try:
                item = work_items.parse_work_item(snapshot.content)
                work_items.validate_work_item_state(item)
            except work_items.WorkItemError:
                unparsed.append(node)
                continue
            if item.id == item_id:
                to_split.append((node, item, snapshot))
            elif _is_split_of(item, item_id):
                finished.append((node, item.id))
            else:
                raise DaemonRequestError(
                    "WORK_ITEM_INVALID",
                    f"copy id:{node.node_id} holds {item.id}, not a split of {item_id}",
                )
        new_ids = allocate_item_ids(
            fs,
            space_id,
            [item.owner for _node, item, _snapshot in to_split]
            + [kept.owner for _node in unparsed],
        )
        split_ids = new_ids[: len(to_split)]
        unparsed_ids = new_ids[len(to_split) :]
        suffix = " (unverified operator)" if unverified_operator else ""
        noted = {line.rsplit("-> ", 1)[-1] for line in kept.log if "-> " in line}
        # A retry after a failed write must not repeat a pointer the kept
        # copy already carries.
        kept_logs = [
            *kept.log,
            *[
                log_line(
                    caller,
                    f"resolve kept id:{kept_node}; other copy -> {new_id}{suffix}",
                )
                for new_id in [
                    *split_ids,
                    *unparsed_ids,
                    *[new_id for _node, new_id in finished],
                ]
                if f"{new_id}{suffix}" not in noted and new_id not in noted
            ],
        ]
        kept_after = work_items.replace_log(kept, kept_logs)
        try:
            kept_written = fs.write_text(
                space_id,
                keep_ref,
                work_items.serialize_work_item(kept_after),
                expect_version=kept_snapshot.version,
            )
            for (node, item, snapshot), new_id in zip(to_split, split_ids, strict=True):
                node_ref = f"id:{node.node_id}"
                fs.write_text(
                    space_id,
                    node_ref,
                    work_items.serialize_work_item(
                        work_items.rename_conflict_copy(
                            item,
                            new_id,
                            log_line(
                                caller,
                                f"resolve split from {item_id} (conflict){suffix}",
                            ),
                        )
                    ),
                    expect_version=snapshot.version,
                )
                self._work_move_copy(fs, space_id, node_ref, new_id)
            for node, new_id in finished:
                self._work_move_copy(fs, space_id, f"id:{node.node_id}", new_id)
            for node, new_id in zip(unparsed, unparsed_ids, strict=True):
                self._work_move_copy(fs, space_id, f"id:{node.node_id}", new_id)
        except OrgFsError as error:
            self._work_raise(error)
        moved_pairs = [
            *[(node, new_id) for (node, _item, _snap), new_id in zip(to_split, split_ids, strict=True)],
            *finished,
            *zip(unparsed, unparsed_ids, strict=True),
        ]
        produced_version = kept_written.version
        moved = [
            {"nodeId": node.node_id, "itemId": moved_id}
            for node, moved_id in moved_pairs
        ]
        self._work_record_resolution(
            space_id=space_id,
            item_id=item_id,
            kept_node=kept_node,
            moved=moved,
            caller=caller,
            caller_verified=not unverified_operator,
            produced_version=produced_version,
        )
        return {
            "spaceId": space_id,
            "itemId": item_id,
            "keptNode": kept_node,
            "moved": moved,
            "caller": caller,
            "producedVersion": produced_version,
        }

    def _work_record_resolution(
        self,
        *,
        space_id: str,
        item_id: str,
        kept_node: str,
        moved: list[JsonObject],
        caller: str,
        caller_verified: bool,
        produced_version: str,
    ) -> None:
        ledger = WorkVerificationLedger(
            self.state_dir / "work-verification-ledger.json"
        )
        for entry in moved:
            try:
                ledger.append(
                    {
                        "action": "resolve",
                        "itemId": item_id,
                        "spaceId": space_id,
                        "keptNode": kept_node,
                        "movedTo": entry["itemId"],
                        "caller": caller,
                        "callerVerified": caller_verified,
                        "producedVersion": produced_version,
                    }
                )
            except WorkLedgerError as error:
                self._work_record_ledger_corruption(error)
                return
