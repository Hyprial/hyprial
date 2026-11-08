"""Extended mission files used as durable work items.

The YAML head is the machine-owned shape. Unknown head keys and all prose
outside the YAML block survive a parse/serialize cycle. Update validation is
centralized here so every writer applies the same append/tick/owner rules.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import yaml

from hyprial.kernel import canonical_user_uri, parse_agent_uri, parse_user_uri

WORK_STATUSES = ("todo", "doing", "blocked", "done", "dropped")
LEGACY_STATUSES = {"active": "doing", "paused": "todo", "done": "done"}

_YAML_BLOCK = re.compile(r"^```ya?ml[ \t]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_HEADING = re.compile(r"^##[ \t]+(\S+)[ \t]+(.+?)[ \t]*$", re.MULTILINE)
_LOG_SECTION = re.compile(
    r"(^##[ \t]+Log[ \t]*\n)(.*?)(?=^##[ \t]+|\Z)",
    re.MULTILINE | re.DOTALL,
)


class WorkItemError(ValueError):
    """A typed work-item parse or write-rule refusal."""

    def __init__(self, code: str, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


@dataclass(frozen=True)
class WorkItem:
    """One parsed mission file, including prose that is not schema-owned."""

    head: dict[str, Any]
    prefix: str
    suffix: str
    title: str
    log: tuple[str, ...]

    @property
    def id(self) -> str:
        return str(self.head["id"])

    @property
    def owner(self) -> str:
        return str(self.head["owner"])

    def to_dict(
        self, *, owner_actions: list[dict[str, object]] | None = None
    ) -> dict[str, Any]:
        projected = (
            project_owner_actions(self)
            if owner_actions is None
            else copy.deepcopy(owner_actions)
        )
        required_actions: set[str] = set()
        if self.head["acceptance"]:
            required_actions.add("acceptance")
        if self.head["status"] == "done":
            required_actions.add("done")
        verified_actions = {
            action["action"]
            for action in projected
            if action["actor"] == self.owner and action["verified"] is True
        }
        return {
            "title": self.title,
            **copy.deepcopy(self.head),
            "ownerActions": projected,
            "log": list(self.log),
            "unverifiedOperator": (
                any(action["verified"] is not True for action in projected)
                or not required_actions <= verified_actions
            ),
        }


def _invalid(message: str, *, unverified: bool = False) -> WorkItemError:
    data = {"unverifiedOperator": True} if unverified else None
    return WorkItemError("WORK_ITEM_INVALID", message, data)


def _string_list(value: object, name: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise _invalid(f"{name} must be a list of strings")
    return list(value)


def _checklist(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise _invalid("checklist must be a list")
    result: list[dict[str, object]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise _invalid(f"checklist[{index}] must be an object")
        text = raw.get("text")
        done = raw.get("done")
        if not isinstance(text, str) or not text.strip() or not isinstance(done, bool):
            raise _invalid(f"checklist[{index}] requires non-empty text and boolean done")
        result.append({"text": text, "done": done})
    return result


def _owner_actions(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise _invalid("ownerActions must be a list", unverified=True)
    result: list[dict[str, object]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise _invalid(f"ownerActions[{index}] must be an object", unverified=True)
        trusted = {
            key: copy.deepcopy(item)
            for key, item in raw.items()
            if isinstance(key, str)
            and "verified" not in key.lower()
            and "verification" not in key.lower()
            and key != "lax"
        }
        if not trusted:
            continue
        action = trusted.get("action")
        actor = trusted.get("actor")
        if action not in {"acceptance", "done", "repair"}:
            raise _invalid(f"ownerActions[{index}].action is unknown", unverified=True)
        if not isinstance(actor, str) or (
            parse_agent_uri(actor) is None and parse_user_uri(actor) is None
        ):
            raise _invalid(
                f"ownerActions[{index}].actor must be a canonical principal URI",
                unverified=True,
            )
        normalized: dict[str, object] = {"action": action, "actor": actor}
        if action == "repair":
            for name in ("invalidVersion", "restoredVersion"):
                version = trusted.get(name)
                if not isinstance(version, str) or not version:
                    raise _invalid(
                        f"ownerActions[{index}].{name} must be a non-empty string",
                        unverified=True,
                    )
                normalized[name] = version
        result.append(normalized)
    return result


def _normalize_head(raw: object) -> dict[str, Any]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise _invalid("yaml block must be a mapping with string keys")
    head = copy.deepcopy(raw)
    for key in tuple(head):
        lowered = key.lower()
        if "verified" in lowered or "verification" in lowered:
            head.pop(key, None)
    item_id = head.get("id")
    if not isinstance(item_id, str) or not item_id.startswith("M-"):
        raise _invalid("id must be a string starting with M-")
    status = head.get("status")
    if status in LEGACY_STATUSES:
        status = LEGACY_STATUSES[str(status)]
    if status not in WORK_STATUSES:
        raise _invalid(f"status must be one of {', '.join(WORK_STATUSES)}")
    head["status"] = status
    owner = head.get("owner")
    if isinstance(owner, str) and owner and ":" not in owner:
        owner = canonical_user_uri(owner)
        head["owner"] = owner
    # Legacy missions used an empty owner. ``work.add`` remains strict.
    if not isinstance(owner, str) or (
        owner
        and parse_agent_uri(owner) is None
        and parse_user_uri(owner) is None
    ):
        raise _invalid("owner must be a canonical user or agent URI")
    assignee = head.setdefault("assignee", None)
    if assignee is not None and (not isinstance(assignee, str) or not assignee):
        raise _invalid("assignee must be null or a non-empty principal URI")
    head["checklist"] = _checklist(head.setdefault("checklist", []))
    head["depends"] = _string_list(head.setdefault("depends", []), "depends")
    acceptance = head.setdefault("acceptance", "")
    if not isinstance(acceptance, str):
        raise _invalid("acceptance must be text")
    for name in ("opened", "closed"):
        value = head.setdefault(name, None)
        if isinstance(value, str):
            try:
                parsed_datetime = datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                )
            except ValueError:
                pass
            else:
                if parsed_datetime.tzinfo is None:
                    parsed_datetime = parsed_datetime.replace(tzinfo=UTC)
                value = int(parsed_datetime.timestamp() * 1000)
                head[name] = value
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            value = int(value.timestamp() * 1000)
            head[name] = value
        elif isinstance(value, date):
            value = int(
                datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp()
                * 1000
            )
            head[name] = value
        if value is not None and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
            raise _invalid(f"{name} must be a non-negative epoch millisecond integer or null")
    head["keywords"] = _string_list(head.setdefault("keywords", []), "keywords")
    head["pacs"] = _string_list(head.setdefault("pacs", []), "pacs")
    head["ownerActions"] = _owner_actions(head.setdefault("ownerActions", []))
    return head


def _log_lines(suffix: str) -> tuple[str, ...]:
    match = _LOG_SECTION.search(suffix)
    if match is None:
        return ()
    lines = tuple(line for line in match.group(2).splitlines() if line.strip())
    if any(not line.startswith("- ") for line in lines):
        raise _invalid("every non-empty ## Log line must start with '- '")
    return lines


def parse_work_item(text: str) -> WorkItem:
    """Parse and normalize one work-item Markdown file."""

    block = _YAML_BLOCK.search(text)
    if block is None:
        raise _invalid("no ```yaml block")
    try:
        raw = yaml.safe_load(block.group(1))
    except yaml.YAMLError as error:
        raise _invalid(f"yaml: {error}".splitlines()[0]) from error
    head = _normalize_head(raw)
    prefix = text[: block.start()]
    suffix = text[block.end() :]
    heading = next(
        (match.group(2) for match in _HEADING.finditer(prefix) if match.group(1) == head["id"]),
        None,
    )
    return WorkItem(
        head=head,
        prefix=prefix,
        suffix=suffix,
        title=heading or str(head["id"]),
        log=_log_lines(suffix),
    )


def serialize_work_item(item: WorkItem) -> str:
    """Serialize deterministically while retaining unknown keys and prose."""

    head = _normalize_head(item.head)
    block = yaml.safe_dump(
        head,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return f"{item.prefix}```yaml\n{block}```{item.suffix}"


def replace_head(item: WorkItem, **changes: object) -> WorkItem:
    head = copy.deepcopy(item.head)
    head.update(changes)
    normalized = _normalize_head(head)
    return WorkItem(normalized, item.prefix, item.suffix, item.title, item.log)


def replace_log(item: WorkItem, lines: list[str]) -> WorkItem:
    """Return an item with exactly ``lines`` in its Log section."""

    if any(not isinstance(line, str) or not line.startswith("- ") for line in lines):
        raise _invalid("every ## Log line must start with '- '")
    content = "\n".join(lines)
    if content:
        content += "\n"
    match = _LOG_SECTION.search(item.suffix)
    if match is None:
        separator = "" if not item.suffix or item.suffix.endswith("\n\n") else "\n"
        suffix = f"{item.suffix}{separator}## Log\n{content}"
    else:
        suffix = f"{item.suffix[:match.start(2)]}{content}{item.suffix[match.end(2):]}"
    return WorkItem(copy.deepcopy(item.head), item.prefix, suffix, item.title, tuple(lines))


def rename_conflict_copy(item: WorkItem, item_id: str, line: str) -> WorkItem:
    """Rename one conflicted copy and append the exact split provenance line."""

    prefix, count = re.subn(
        rf"(^##[ \t]+){re.escape(item.id)}(?=[ \t])",
        rf"\g<1>{item_id}",
        item.prefix,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise _invalid("work item heading cannot be renamed")
    renamed = replace_head(item, id=item_id)
    renamed = WorkItem(
        renamed.head,
        prefix,
        renamed.suffix,
        renamed.title,
        renamed.log,
    )
    return replace_log(renamed, [*renamed.log, line])


def is_conflict_split(before: WorkItem, after: WorkItem) -> bool:
    """Recognize the sole history transition allowed to change a work-item id."""

    if before.id == after.id or len(after.log) != len(before.log) + 1:
        return False
    line = after.log[-1]
    if re.fullmatch(
        r"- \S+ .+: resolve split from "
        + re.escape(before.id)
        + r" \(conflict\)(?: \(unverified operator\))?",
        line,
    ) is None:
        return False
    try:
        return rename_conflict_copy(before, after.id, line) == after
    except WorkItemError:
        return False


def append_owner_action(
    item: WorkItem,
    *,
    action: str,
    actor: str,
    invalid_version: str | None = None,
    restored_version: str | None = None,
) -> WorkItem:
    actions = [*item.head["ownerActions"]]
    actions.append(
        {
            "action": action,
            "actor": actor,
            **(
                {
                    "invalidVersion": invalid_version,
                    "restoredVersion": restored_version,
                }
                if action == "repair"
                else {}
            ),
        }
    )
    return replace_head(item, ownerActions=actions)


def project_owner_actions(
    item: WorkItem, states: list[str] | None = None
) -> list[dict[str, object]]:
    """Return output-only verification fields; file claims never supply them."""

    actions = [copy.deepcopy(action) for action in item.head["ownerActions"]]
    recorded = {str(action["action"]) for action in actions}
    if item.head["acceptance"] and "acceptance" not in recorded:
        actions.append({"action": "acceptance", "actor": item.owner})
    if item.head["status"] == "done" and "done" not in recorded:
        actions.append({"action": "done", "actor": item.owner})
    explicit = len(item.head["ownerActions"])
    resolved = states or ["unverified (remote)"] * explicit
    result: list[dict[str, object]] = []
    for index, action in enumerate(actions):
        state = resolved[index] if index < len(resolved) else "unverified (legacy)"
        display_state = (
            "verified (LAX same-uid)" if state == "verified" else state
        )
        projected = {
            **action,
            "verified": state == "verified",
            "verification": display_state,
        }
        if state == "unverified (remote)":
            projected["lax"] = "signed-updates"
        result.append(projected)
    return result


def is_exact_repair(
    restored: WorkItem,
    candidate: WorkItem,
    *,
    invalid_version: str,
    restored_version: str,
) -> bool:
    """Recognize the deterministic content written by ``work.repair``."""

    if len(candidate.log) != len(restored.log) + 1:
        return False
    repair = candidate.log[-1]
    match = re.fullmatch(
        r"- \S+ (.+): repair "
        + re.escape(invalid_version)
        + r" -> "
        + re.escape(restored_version)
        + r"(?: \(unverified operator\))?",
        repair,
    )
    if match is None:
        return False
    caller = match.group(1)
    try:
        expected = append_owner_action(
            restored,
            action="repair",
            actor=caller,
            invalid_version=invalid_version,
            restored_version=restored_version,
        )
        expected = replace_log(expected, [*restored.log, repair])
        validate_work_item_state(expected)
    except WorkItemError:
        return False
    return expected == candidate


def validate_work_item_update(before: WorkItem, after: WorkItem, *, caller: str) -> None:
    """Enforce append/tick/owner gates for one complete-file replacement."""

    if before.id != after.id:
        raise WorkItemError("WORK_ITEM_ID_IMMUTABLE", "work item id cannot change")
    old = before.head["checklist"]
    new = after.head["checklist"]
    assert isinstance(old, list) and isinstance(new, list)
    if len(new) < len(old):
        raise WorkItemError(
            "WORK_ITEM_CHECKLIST_REWRITE", "checklist items may only be appended or ticked"
        )
    for index, previous in enumerate(old):
        current = new[index]
        if previous["text"] != current["text"] or (
            previous["done"] is True and current["done"] is not True
        ):
            raise WorkItemError(
                "WORK_ITEM_CHECKLIST_REWRITE",
                "checklist items may not be reordered, edited, removed, or unticked",
                {"index": index + 1},
            )
    if after.log[: len(before.log)] != before.log or len(after.log) < len(before.log):
        raise WorkItemError("WORK_ITEM_LOG_REWRITE", "work item log is append-only")
    old_actions = before.head["ownerActions"]
    new_actions = after.head["ownerActions"]
    if new_actions[: len(old_actions)] != old_actions or len(new_actions) < len(old_actions):
        raise WorkItemError(
            "WORK_ITEM_OWNER_ACTION_REWRITE",
            "owner action records are append-only",
            {"unverifiedOperator": True},
        )
    if before.head["acceptance"] != after.head["acceptance"] and caller != before.owner:
        raise WorkItemError(
            "WORK_ITEM_OWNER_REQUIRED", "only the work item owner may change acceptance"
        )
    if before.head["status"] != "done" and after.head["status"] == "done":
        if any(not bool(entry["done"]) for entry in new):
            raise WorkItemError(
                "WORK_ITEM_CHECKLIST_INCOMPLETE",
                "work item cannot be done while checklist items remain unticked",
            )
        if caller != before.owner:
            raise WorkItemError(
                "WORK_ITEM_OWNER_REQUIRED", "only the work item owner may mark it done"
            )


def validate_work_item_state(item: WorkItem) -> None:
    """Reject an intrinsically invalid raw-written work-item snapshot."""

    checklist = item.head["checklist"]
    assert isinstance(checklist, list)
    if item.head["status"] == "done" and any(
        not bool(entry["done"]) for entry in checklist
    ):
        raise _invalid("done work item has unticked checklist items")


def validate_work_item_history(before: WorkItem, after: WorkItem) -> None:
    """Detect raw rewrites by comparing adjacent OrgFS versions."""

    if before.id != after.id and not is_conflict_split(before, after):
        raise _invalid("work item id was rewritten in history")

    old_checklist = before.head["checklist"]
    new_checklist = after.head["checklist"]
    if len(new_checklist) < len(old_checklist):
        raise _invalid("checklist items were deleted from history")
    for index, previous in enumerate(old_checklist):
        current = new_checklist[index]
        if previous["text"] != current["text"] or (
            previous["done"] is True and current["done"] is not True
        ):
            raise _invalid(f"checklist item {index + 1} was rewritten in history")
    if after.log[: len(before.log)] != before.log or len(after.log) < len(before.log):
        raise _invalid("work item log was rewritten in history")
    old_actions = before.head["ownerActions"]
    new_actions = after.head["ownerActions"]
    if new_actions[: len(old_actions)] != old_actions or len(new_actions) < len(old_actions):
        raise _invalid("owner action record was rewritten in history", unverified=True)
    validate_work_item_state(after)


def new_work_item(
    *,
    item_id: str,
    title: str,
    owner: str,
    assignee: str | None,
    checklist: list[str],
    depends: list[str],
    acceptance: str,
    opened: int,
) -> WorkItem:
    """Build the canonical initial file for ``org work add``."""

    return parse_work_item(
        f"## {item_id} {title}\n\n"
        "```yaml\n"
        + yaml.safe_dump(
            {
                "id": item_id,
                "status": "todo",
                "owner": owner,
                "assignee": assignee,
                "checklist": [{"text": text, "done": False} for text in checklist],
                "depends": depends,
                "acceptance": acceptance,
                "opened": opened,
                "closed": None,
                "keywords": [],
                "pacs": [],
                "ownerActions": [],
            },
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "```\n\n## Log\n"
    )
