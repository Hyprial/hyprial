"""PAC overview: one node's workflows and routines as a page-ready model.

`hyprial workflow overview show` prints the model; `publish` writes it into an
orgfs space as ``<path>/nodes/<node>.js`` next to a static ``index.html`` that
merges every node's file.  Members open ``index.html`` from their read-only
checkout (``hyprial fs checkout``), so the page works before the orgfs web
front is deployed.

Task lines are inferred until PAC can declare them, and are named after their
actor (no organisation's roster is built in): every graph joins the line of
the actor that sent it, and a routine's runs hang on the line of the
routine's owner as a heartbeat strip.  Relations are inferred from names: a
shared PR number, a shared slug bigram, or the same base name (a rerun).

Writes are content-addressed blobs, and only when the model changed: orgfs
keeps every write and replicates it to every member.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

DAY_MS = 24 * 3600 * 1000
DEFAULT_WINDOW_DAYS = 7
OUTPUT_TEXT_LIMIT = 600
NODE_SCRIPT_DIR = "nodes"

Request = Callable[[str, Mapping[str, Any]], Any]

# Tokens that must never reach a shared page, whatever an agent printed.
_SECRET = re.compile(
    r"(ghp_|github_pat_|gho_|sk-|tskey-|xox[bpas]-|AKIA|glpat-)[A-Za-z0-9_\-]{6,}"
)

_NAME_NOISE = {
    "review",
    "rereview",
    "rereview2",
    "rereview3",
    "cli",
    "dev",
    "routine",
    "final",
    "attempt",
    "fix",
    "r2",
    "r3",
}


def redact(text: object) -> str:
    return _SECRET.sub("[已隐去]", str(text or ""))


def short_actor(uri: object) -> str:
    """The last segment of an agent URI; ``user:<x>`` stays whole."""

    from hyprial.uri import short_actor_name

    return short_actor_name(str(uri or ""))


def collect(
    request: Request, *, now_ms: int, window_days: int = DEFAULT_WINDOW_DAYS
) -> dict[str, Any]:
    """Read this node's graphs, nodes and routines through the daemon.

    Graphs older than the window that are no longer running are left out, so
    the published file stays bounded however long the node has run.
    """

    since = now_ms - window_days * DAY_MS
    listed = request("workflow.list", {"limit": 500, "all": True})
    runs = [
        row
        for row in (listed.get("runs") or [])
        if isinstance(row, dict)
        and (row.get("state") == "running" or int(row.get("createdAtMs") or 0) >= since)
    ]
    detailed = []
    for row in runs:
        graph_id = str(row.get("graphId"))
        try:
            status = request("workflow.status", {"runId": graph_id})
        except Exception:  # noqa: BLE001 - one unreadable graph must not sink the page
            status = {}
        nodes = []
        for node in status.get("nodes") or []:
            node_id = str(node.get("nodeId") or "")
            if (
                not node_id
                or node_id.startswith("_actor.")
                or node.get("kind") == "actor"
            ):
                continue
            try:
                detail = request(
                    "workflow.node.inspect", {"runId": graph_id, "target": node_id}
                )
            except Exception:  # noqa: BLE001 - keep the graph with its listed fields
                detail = {}
            inner = detail.get("node") if isinstance(detail, dict) else None
            inner = inner if isinstance(inner, dict) else {}
            nodes.append(
                {
                    **node,
                    "outputText": inner.get("outputText"),
                    "flagSetAt": inner.get("flagSetAt"),
                }
            )
        detailed.append(
            {
                **row,
                "nodes": nodes,
                "edges": status.get("edges") or [],
                "roster": (status.get("roster") or {}).get("nodes") or [],
                "graphReason": status.get("reasonRef"),
            }
        )
    routines = request("routine.list", {"all": True}).get("routines") or []
    return {"observedAt": now_ms, "runs": detailed, "routines": routines}


def _signature(name: str) -> tuple[str, set[str], set[str]]:
    base = re.sub(r"-[0-9a-f]{12,}$", "", name)
    prs = set(re.findall(r"(?<![0-9])([1-9]\d\d)(?![0-9])", base))
    words = [
        word
        for word in re.split(r"[-_]", base.lower())
        if word
        and word not in _NAME_NOISE
        and not re.fullmatch(r"\d+|[a-z]{1,2}\d?", word)
    ]
    return base, prs, {f"{a}-{b}" for a, b in zip(words, words[1:])}


def build_model(snapshot: Mapping[str, Any], *, node: str) -> dict[str, Any]:
    """Turn a :func:`collect` snapshot into the page's data model."""

    routines = {
        str(item.get("name")): item
        for item in snapshot.get("routines") or []
        if isinstance(item, dict) and item.get("name")
    }

    def line_key(actor: object) -> str:
        return f"{node}|{actor}"

    runs: list[dict[str, Any]] = []
    for row in snapshot.get("runs") or []:
        routine_name = row.get("routineName")
        owner = (
            (routines.get(routine_name) or {}).get("owner") if routine_name else None
        )
        actor = owner or row.get("sender") or ""
        roster = {
            item.get("nodeId"): item
            for item in row.get("roster") or []
            if isinstance(item, dict)
        }
        runs.append(
            {
                "id": str(row.get("graphId")),
                "name": str(row.get("name") or ""),
                "actor": line_key(actor),
                "line": line_key(actor),
                "state": row.get("state"),
                "sender": short_actor(row.get("sender")),
                "created": row.get("createdAtMs"),
                "closed": row.get("closedAtMs"),
                "progress": row.get("lastProgressAtMs"),
                "current": row.get("currentNode"),
                "routine": routine_name,
                "reason": redact(row.get("graphReason")),
                "nodes": [
                    {
                        "id": node_row.get("nodeId"),
                        "kind": node_row.get("kind"),
                        "owner": short_actor(node_row.get("owner")),
                        "state": node_row.get("state"),
                        "flagAt": node_row.get("flagSetAt"),
                        "timeoutMs": node_row.get("timeoutMs"),
                        "reason": redact(node_row.get("reasonRef")),
                        "output": redact(node_row.get("outputText"))[
                            :OUTPUT_TEXT_LIMIT
                        ],
                        "role": (roster.get(node_row.get("nodeId")) or {}).get("role"),
                    }
                    for node_row in row.get("nodes") or []
                ],
                "edges": [
                    [edge.get("from"), edge.get("to")]
                    for edge in row.get("edges") or []
                    if isinstance(edge, dict)
                    and not str(edge.get("from", "")).startswith("_actor.")
                ],
            }
        )
    runs.sort(key=lambda item: (item["created"] or 0, item["id"]))

    # Each graph links to its most recent earlier relative per reason, so a
    # chain reads as a chain.  Routine runs never link to each other.
    signatures = {item["id"]: _signature(item["name"]) for item in runs}
    links: list[dict[str, str]] = []
    for index, later in enumerate(runs):
        if later["routine"]:
            continue
        base, prs, bigrams = signatures[later["id"]]
        found: dict[str, tuple[str, str]] = {}
        for earlier in reversed(runs[:index]):
            if earlier["routine"]:
                continue
            other_base, other_prs, other_bigrams = signatures[earlier["id"]]
            if "rerun" not in found and other_base == base:
                found["rerun"] = (earlier["id"], "重跑")
            if "pr" not in found and prs & other_prs:
                found["pr"] = (earlier["id"], "#" + sorted(prs & other_prs)[0])
            if "topic" not in found and bigrams & other_bigrams:
                found["topic"] = (earlier["id"], sorted(bigrams & other_bigrams)[0])
        seen: set[str] = set()
        for kind, (source, label) in found.items():
            if source in seen:
                continue
            seen.add(source)
            links.append(
                {"from": source, "to": later["id"], "kind": kind, "label": label}
            )

    lines = _task_lines(node, runs, links, routines, signatures)
    actors = []
    for key in sorted({line["actor"] for line in lines}):
        name = short_actor(key.split("|", 1)[1])
        actors.append(
            {"id": key, "node": node, "name": name, "human": name.startswith("user:")}
        )
    return {
        "node": node,
        "observedAt": snapshot.get("observedAt"),
        "actors": actors,
        "lines": lines,
        "runs": runs,
        "links": links,
        "visibleNodeGraphs": sum(1 for item in runs if item["nodes"]),
    }


def _task_lines(
    node: str,
    runs: list[dict[str, Any]],
    links: list[dict[str, str]],
    routines: Mapping[str, Mapping[str, Any]],
    signatures: Mapping[str, tuple[str, set[str], set[str]]],
) -> list[dict[str, Any]]:
    """Infer task lines (what is being built) under actors (who builds it).

    PAC cannot declare a task line yet, so related graphs (the inferred links:
    same PR, shared topic, rerun) form one line, owned by the actor with the
    most graphs in it; graphs other actors ran for it stay on that line.
    Unrelated one-off graphs collect in their actor's "其他" line, and each
    routine is a line of its own.  Every graph's ``line`` is set here.
    """

    by_id = {item["id"]: item for item in runs}
    parent = {item["id"]: item["id"] for item in runs if not item["routine"]}

    def find(key: str) -> str:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for link in links:
        if link["from"] in parent and link["to"] in parent:
            parent[find(link["from"])] = find(link["to"])
    groups: dict[str, list[str]] = {}
    for key in parent:
        groups.setdefault(find(key), []).append(key)

    lines: list[dict[str, Any]] = []
    misc: dict[str, list[str]] = {}
    for members in groups.values():
        members.sort(key=lambda key: (by_id[key]["created"] or 0, key))
        # Owned by the actor with the most graphs; on a tie, the one who
        # started the line.
        owners: dict[str, int] = {}
        first_seen: dict[str, int] = {}
        for index, key in enumerate(members):
            owner = by_id[key]["actor"]
            owners[owner] = owners.get(owner, 0) + 1
            first_seen.setdefault(owner, index)
        actor = max(owners, key=lambda owner: (owners[owner], -first_seen[owner]))
        if len(members) == 1:
            misc.setdefault(actor, []).append(members[0])
            continue
        line_id = f"{actor}|t:{members[0]}"
        for key in members:
            by_id[key]["line"] = line_id
        lines.append(
            _line(
                node,
                line_id,
                actor,
                _topic_title(members, by_id, signatures),
                "task",
                members,
                [],
            )
        )
    for actor, members in misc.items():
        line_id = f"{actor}|misc"
        for key in members:
            by_id[key]["line"] = line_id
        lines.append(
            _line(
                node, line_id, actor, "其他（没有关联的零散任务）", "misc", members, []
            )
        )

    routine_runs: dict[str, list[str]] = {}
    for item in runs:
        if item["routine"]:
            routine_runs.setdefault(item["routine"], []).append(item["id"])
    for name in sorted(set(routine_runs) | set(routines)):
        info = routines.get(name) or {}
        ids = routine_runs.get(name, [])
        actor = (
            f"{node}|{info.get('owner')}"
            if info.get("owner")
            else (by_id[ids[0]]["actor"] if ids else f"{node}|")
        )
        line_id = f"{actor}|r:{name}"
        for key in ids:
            by_id[key]["line"] = line_id
        strip = {
            "name": name,
            "mode": info.get("mode"),
            "enabled": info.get("enabled"),
            "nextDue": info.get("nextDueMs"),
            "inFlight": len(info.get("inFlight") or []),
            "runs": ids,
        }
        lines.append(_line(node, line_id, actor, f"↻ {name}", "routine", [], [strip]))
    lines.sort(key=lambda line: line["id"])
    return lines


def _line(
    node: str,
    line_id: str,
    actor: str,
    title: str,
    kind: str,
    workflows: list[str],
    strips: list[dict[str, Any]],
) -> dict[str, Any]:
    owner = short_actor(actor.split("|", 1)[1])
    return {
        "id": line_id,
        "node": node,
        "actor": actor,
        "kind": kind,
        "title": title,
        "owner": owner,
        "background": {
            "task": "由互相关联的 PAC 推断出的任务线（同一个 PR、同一主题或重跑）",
            "misc": f"{owner} 没有与其他 PAC 关联的零散任务",
            "routine": f"{owner} 负责的例行，每次运行产生一个 PAC",
        }[kind],
        "goal": "—（任务线尚未声明）",
        "runs": workflows,
        "routines": strips,
    }


def _topic_title(
    members: list[str],
    by_id: Mapping[str, Mapping[str, Any]],
    signatures: Mapping[str, tuple[str, set[str], set[str]]],
) -> str:
    """The words most of the line's graphs share, else its first graph's name."""

    counts: dict[str, int] = {}
    prs: dict[str, int] = {}
    for key in members:
        _, pr_numbers, bigrams = signatures[key]
        for bigram in bigrams:
            counts[bigram] = counts.get(bigram, 0) + 1
        for number in pr_numbers:
            prs[number] = prs.get(number, 0) + 1
    best = max(counts.items(), key=lambda item: (item[1], item[0]), default=None)
    pr = max(prs.items(), key=lambda item: (item[1], item[0]), default=None)
    if best and best[1] >= 2:
        return best[0] + (f" · #{pr[0]}" if pr and pr[1] >= 2 else "")
    if pr and pr[1] >= 2:
        return f"#{pr[0]}"
    return str(by_id[members[0]]["name"])


def content_key(model: Mapping[str, Any]) -> str:
    """A hash that ignores when the model was built, only what it says."""

    stable = {key: value for key, value in model.items() if key != "observedAt"}
    stable["lines"] = [
        {
            **line,
            "routines": [
                {k: v for k, v in strip.items() if k not in {"nextDue", "inFlight"}}
                for strip in line["routines"]
            ],
        }
        for line in model.get("lines") or []
    ]
    encoded = json.dumps(
        stable, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def node_file_name(node: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", node).strip("._") or "node"
    return f"{safe}.js"


def node_script(model: Mapping[str, Any]) -> str:
    """One node's model as a script the page loads with ``<script src>``.

    A script (not JSON) because a page opened from ``file://`` may not fetch
    its sibling files, but it may include them.
    """

    payload = json.dumps(model, ensure_ascii=False, separators=(",", ":")).replace(
        "</", "<\\/"
    )
    key = json.dumps(model.get("node"), ensure_ascii=False)
    return f"(window.PAC_NODES = window.PAC_NODES || {{}})[{key}] = {payload};\n"


_NODE_FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.js")


def page_html(node_files: list[str]) -> str:
    """The page, including every node file :func:`node_file_name` could have named.

    Names come from the space listing, which any writer controls; anything
    outside that shape is left out rather than escaped into the page.
    """

    template = (
        resources.files("hyprial.pac").joinpath("overview_page.html").read_text("utf-8")
    )
    safe = sorted({name for name in node_files if _NODE_FILE.fullmatch(name)})
    tags = "\n".join(
        f'<script src="{NODE_SCRIPT_DIR}/{name}"></script>' for name in safe
    )
    return template.replace("<!--NODE_SCRIPTS-->", tags)


def publish(
    request: Request,
    *,
    space_id: str,
    base: str,
    model: Mapping[str, Any],
    staging: Path,
    last: Mapping[str, str],
    force: bool = False,
) -> dict[str, Any]:
    """Write this node's file, and the page when the set of nodes changed.

    ``last`` holds the keys of the previous publish from this node (the
    caller persists the returned ``keys``).  Nothing is written when neither
    changed, so an idle node adds nothing to the space's history.
    """

    base = base.strip("/")
    node_file = node_file_name(str(model.get("node")))
    key = content_key(model)
    written: list[str] = []
    _ensure_dir(request, space_id, base)
    _ensure_dir(request, space_id, f"{base}/{NODE_SCRIPT_DIR}")
    if force or last.get("node") != key:
        staged = staging / node_file
        staged.write_text(node_script(model), encoding="utf-8")
        request(
            "orgfs.import",
            {
                "spaceId": space_id,
                "path": f"{base}/{NODE_SCRIPT_DIR}/{node_file}",
                "source": str(staged),
            },
        )
        written.append(f"{base}/{NODE_SCRIPT_DIR}/{node_file}")
    listing = request(
        "orgfs.ls", {"spaceId": space_id, "path": f"{base}/{NODE_SCRIPT_DIR}"}
    )
    files = sorted(
        {
            str(item.get("name"))
            for item in listing.get("nodes") or []
            if isinstance(item, dict)
            and not item.get("deleted")
            and _NODE_FILE.fullmatch(str(item.get("name", "")))
        }
        | {node_file}
    )
    page = page_html(files)
    page_key = hashlib.sha256(page.encode("utf-8")).hexdigest()
    if force or last.get("page") != page_key:
        staged = staging / "index.html"
        staged.write_text(page, encoding="utf-8")
        request(
            "orgfs.import",
            {"spaceId": space_id, "path": f"{base}/index.html", "source": str(staged)},
        )
        written.append(f"{base}/index.html")
    # A checkout leaves out blobs whose bytes have not reached this node and
    # does not revisit them when they arrive.  Pull the other nodes' files,
    # then refresh the checkout so the local page shows every node.
    pending = []
    for item in listing.get("nodes") or []:
        if (
            not isinstance(item, dict)
            or item.get("deleted")
            or item.get("name") == node_file
        ):
            continue
        if item.get("contentState") not in (None, "arrived"):
            try:
                request(
                    "orgfs.export",
                    {
                        "spaceId": space_id,
                        "node": f"{base}/{NODE_SCRIPT_DIR}/{item.get('name')}",
                        "destination": str(staging / f"peer-{item.get('name')}"),
                        "waitSeconds": 20,
                    },
                )
            except Exception:  # noqa: BLE001 - that node is offline; its file arrives later
                pending.append(str(item.get("name")))
    checkout = request("orgfs.checkout", {"spaceId": space_id, "enabled": True})
    checkout_path = checkout.get("path") if isinstance(checkout, dict) else None
    return {
        "spaceId": space_id,
        "node": model.get("node"),
        "written": written,
        "nodes": files,
        "unavailable": pending,
        "page": str(Path(checkout_path) / base / "index.html")
        if checkout_path
        else None,
        "keys": {"node": key, "page": page_key},
    }


def _ensure_dir(request: Request, space_id: str, path: str) -> None:
    from hyprial.cli import CliError

    try:
        request("orgfs.ls", {"spaceId": space_id, "path": path})
    except CliError:
        # Missing (the usual first-publish case).  If it exists after all,
        # mkdir's own error is the one worth reporting.
        request("orgfs.mkdir", {"spaceId": space_id, "path": path})
