"""Missions: which PAC graphs belong to which task line (docs/design-missions.md).

A mission is one file ``missions/M-<owner>-NNNN.md`` in a dedicated orgfs
space, written only by squires.  Its first ```yaml block is machine-readable
(``id``, ``status``, ``owner``, ``keywords``, ``pacs``); the rest is prose.

This module is the deterministic half of the association.  A squire's
scheduled routine runs, in one ``work`` node:

1. :func:`collect` - read the missions, read the previous round's state
   (``asked``/``declined``) from that round's ``outputText``, pick this node's
   new graphs, pre-filter at most :data:`TOP_CANDIDATES` missions for each,
   and build one jev payload per graph that has candidates;
2. the squire sends each payload to the jev with ``hyprial send`` and reads
   the replies from its inbox;
3. :func:`finish` - fold the replies into one result per graph and the state
   the squire completes the node with (``--output-text``).

Nothing here writes to orgfs: every write stays with the squire.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

from hyprial.pac.overview import DAY_MS, DEFAULT_WINDOW_DAYS, Request, _signature

MISSIONS_DIR = "missions"
WORK_NODE = "work"
TOP_CANDIDATES = 5
GRAPHS_PER_ROUND = 20
LIST_LIMIT = 500  # workflow.list returns at most this many, newest first
PAYLOAD_LIMIT = 32 * 1024
STATE_LIMIT = 60 * 1024  # under the 64 KiB workflow output cap
STATUSES = ("active", "done", "paused")
_TERMINAL = {"completed", "failed", "cancelled"}
SKIPPED_STATE = {"skipped": True}  # the output of a round that did nothing
_MISSION_FILE = re.compile(r"M-[A-Za-z0-9._-]+-\d{4,}\.md")
_YAML_BLOCK = re.compile(r"^```ya?ml[ \t]*\n(.*?)^```", re.MULTILINE | re.DOTALL)
_HEADING = re.compile(r"^##[ \t]+(\S+)[ \t]+(.+?)[ \t]*$", re.MULTILINE)


class MissionParseError(ValueError):
    """A mission file whose YAML block cannot be used; the squire repairs it."""


class PreviousRoundUnreadable(RuntimeError):
    """The previous round exists but cannot be read (e.g. the routine changed actor).

    Treating it as "no previous round" would ask the owner again about every
    graph already asked, so the squire must fail its node instead.
    """


def parse_mission(text: str) -> dict[str, Any]:
    """The YAML block of one mission file, validated; the title comes from its heading."""

    block = _YAML_BLOCK.search(text)
    if block is None:
        raise MissionParseError("no ```yaml block")
    try:
        data = yaml.safe_load(block.group(1))
    except yaml.YAMLError as error:
        raise MissionParseError(f"yaml: {error}".splitlines()[0]) from error
    if not isinstance(data, dict):
        raise MissionParseError("yaml block is not a mapping")
    mission_id = data.get("id")
    if not isinstance(mission_id, str) or not mission_id.startswith("M-"):
        raise MissionParseError("id must be a string starting with M-")
    status = data.get("status")
    if status not in STATUSES:
        raise MissionParseError(f"status must be one of {', '.join(STATUSES)}")
    keywords = data.get("keywords") or []
    pacs = data.get("pacs") or []
    if not isinstance(keywords, list) or not isinstance(pacs, list):
        raise MissionParseError("keywords and pacs must be lists")
    heading = next(
        (m.group(2) for m in _HEADING.finditer(text) if m.group(1) == mission_id), None
    )
    return {
        "id": mission_id,
        "title": heading or mission_id,
        "status": status,
        "owner": str(data.get("owner") or ""),
        "keywords": [str(word) for word in keywords],
        "pacs": [str(graph).strip() for graph in pacs if str(graph).strip()],
    }


def load_missions(
    request: Request, space_id: str, *, base: str = MISSIONS_DIR
) -> dict[str, Any]:
    """Every mission file in the space, with those that need a squire's repair.

    ``unparseable`` and ``conflicts`` are never skipped silently: a merge that
    garbled a line, or two files created under one name, is the squire's to fix.
    """

    listing = request("orgfs.ls", {"spaceId": space_id, "path": base})
    missions: list[dict[str, Any]] = []
    unparseable: list[dict[str, str]] = []
    conflicts: list[str] = []
    for item in listing.get("nodes") or []:
        if not isinstance(item, dict) or item.get("deleted"):
            continue
        name = str(item.get("name") or "")
        if not _MISSION_FILE.fullmatch(name):
            continue
        path = f"{base}/{name}"
        if item.get("name_conflict"):
            conflicts.append(path)
            continue
        try:
            read = request(
                "orgfs.read",
                {"spaceId": space_id, "path": f"id:{item.get('node_id')}", "waitSeconds": 10},
            )
            mission = parse_mission(str(read.get("text") or ""))
        except MissionParseError as error:
            unparseable.append({"path": path, "reason": str(error)})
            continue
        except Exception as error:  # noqa: BLE001 - one unreadable file is reported, not fatal
            unparseable.append({"path": path, "reason": f"unreadable: {error}"})
            continue
        missions.append(
            {
                **mission,
                "path": path,
                "version": read.get("version"),
                "modifiedBy": item.get("modified_by"),
            }
        )
    missions.sort(key=lambda mission: mission["id"])
    return {"missions": missions, "unparseable": unparseable, "conflicts": conflicts}


def previous_state(
    request: Request, runs: Sequence[Mapping[str, Any]], routine: str
) -> dict[str, Any]:
    """The ``asked``/``declined`` state the last completed round of ``routine`` wrote.

    Rounds that failed or were cancelled wrote nothing, and rounds that
    skipped (see :data:`SKIPPED_STATE`) carried nothing: both are passed
    over for the round before them, since every round writes the whole set.
    A completed round without readable state stops the search instead.
    """

    rounds = sorted(
        (
            run
            for run in runs
            if run.get("routineName") == routine and run.get("state") == "completed"
        ),
        key=lambda run: (int(run.get("createdAtMs") or 0), str(run.get("graphId"))),
        reverse=True,
    )
    for run in rounds:
        graph_id = str(run.get("graphId"))
        try:
            detail = request("workflow.node.inspect", {"runId": graph_id, "target": WORK_NODE})
        except Exception as error:  # noqa: BLE001 - CALLER_NOT_AUTHORIZED among others
            raise PreviousRoundUnreadable(f"{graph_id}: {error}") from error
        node = detail.get("node") if isinstance(detail, dict) else None
        text = node.get("outputText") if isinstance(node, dict) else None
        try:
            state = json.loads(text) if isinstance(text, str) else None
        except json.JSONDecodeError:
            state = None
        if not isinstance(state, dict):
            raise PreviousRoundUnreadable(f"{graph_id}: outputText is not a state object")
        if state.get("skipped"):
            continue
        return {
            "from": graph_id,
            "asked": [str(g) for g in state.get("asked") or []],
            "declined": [str(g) for g in state.get("declined") or []],
        }
    return {"asked": [], "declined": []}


def new_graphs(
    runs: Sequence[Mapping[str, Any]],
    missions: Sequence[Mapping[str, Any]],
    state: Mapping[str, Any],
    *,
    now_ms: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
    limit: int = GRAPHS_PER_ROUND,
) -> tuple[Sequence[Mapping[str, Any]], int]:
    """Graphs created here in the window that no mission, question or refusal covers.

    Routine graphs (this routine's rounds included) are never candidates.
    Oldest first, at most ``limit``; the rest wait for the next round.
    """

    since = now_ms - window_days * DAY_MS
    known = {graph for mission in missions for graph in mission.get("pacs") or []}
    known |= set(state.get("asked") or []) | set(state.get("declined") or [])
    fresh = sorted(
        (
            run
            for run in runs
            if not run.get("routineName")
            and int(run.get("createdAtMs") or 0) >= since
            and str(run.get("graphId")) not in known
        ),
        key=lambda run: (int(run.get("createdAtMs") or 0), str(run.get("graphId"))),
    )
    return fresh[:limit], max(0, len(fresh) - limit)


def candidates(
    run: Mapping[str, Any],
    missions: Sequence[Mapping[str, Any]],
    runs_by_id: Mapping[str, Mapping[str, Any]],
    *,
    top: int = TOP_CANDIDATES,
) -> list[dict[str, Any]]:
    """At most ``top`` missions this graph may belong to, strongest evidence first.

    The same relations the overview infers task lines from (a shared PR
    number, a slug bigram, a rerun), plus the mission's keywords and whether
    the graph's sender already has graphs in that mission.
    """

    base, prs, bigrams = _signature(str(run.get("name") or ""))
    name = str(run.get("name") or "").lower()
    sender = run.get("sender")
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for mission in missions:
        if mission.get("status") == "done":
            continue
        reasons: list[str] = []
        score = 0
        words = {str(word).lower() for word in mission.get("keywords") or []}
        for number in sorted(prs):
            if f"#{number}" in words or number in words:
                score += 8
                reasons.append(f"pr #{number}")
        hits = sorted(word for word in words if word.lstrip("#") and word in name)
        if hits:
            score += 4 * len(hits)
            reasons.append("keywords " + ", ".join(hits))
        members = [runs_by_id[g] for g in mission.get("pacs") or [] if g in runs_by_id]
        for member in members:
            other_base, other_prs, other_bigrams = _signature(str(member.get("name") or ""))
            if other_base == base:
                score += 6
                reasons.append(f"rerun of {member.get('graphId')}")
                break
            if prs & other_prs or bigrams & other_bigrams:
                score += 3
                reasons.append(f"related to {member.get('graphId')}")
                break
        if score and sender and any(m.get("sender") == sender for m in members):
            score += 1
            reasons.append("same sender")
        if score:
            scored.append((score, str(mission["id"]), {"id": mission["id"], "score": score, "reasons": reasons}))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [entry for _, _, entry in scored[:top]]


def _question_name(index: int) -> str:
    return f"c{index}"


def jev_payload(
    run: Mapping[str, Any],
    found: Sequence[Mapping[str, Any]],
    missions_by_id: Mapping[str, Mapping[str, Any]],
    *,
    limit: int = PAYLOAD_LIMIT,
) -> dict[str, Any] | None:
    """The {state, questions} the squire sends to the jev, or None when too large.

    One ``noul`` question per candidate ("does this graph belong to it?").
    Missions carry only id, title, keywords and status.  A payload over
    ``limit`` is never replaced by a reference, which the jev cannot follow:
    the caller treats that graph as low confidence and asks the owner.
    """

    state = {
        "pac": {
            "graphId": run.get("graphId"),
            "name": run.get("name"),
            "sender": run.get("sender"),
        },
        "candidates": [
            {
                key: missions_by_id[str(entry["id"])].get(key)
                for key in ("id", "title", "keywords", "status")
            }
            for entry in found
        ],
    }
    questions = {
        _question_name(index): {
            "type": "noul",
            "instructions": (
                f"Probability that PAC graph {run.get('graphId')} ({run.get('name')}) "
                f"is work toward mission {entry['id']}, given the mission's title and keywords."
            ),
        }
        for index, entry in enumerate(found)
    }
    payload = {"state": state, "questions": questions}
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > limit:
        return None
    return payload


def collect(
    request: Request,
    space_id: str,
    *,
    routine: str,
    current: str,
    now_ms: int,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict[str, Any]:
    """Step 1 of a round: what the squire must ask the jev, and what is already decided.

    ``current`` is this round's own graph.  While another round of the same
    routine is still running, its ``asked`` set is not written yet, so this
    round would ask the same graphs again: it skips instead.
    """

    runs = [
        row
        for row in request("workflow.list", {"limit": LIST_LIMIT, "all": True}).get("runs") or []
        if isinstance(row, dict)
    ]
    # A full page means older graphs were cut off.  Each graph is first seen
    # by the round after it was created, long before it can fall off, so
    # this only matters after a long pause or backlog; it is reported.
    covered_since = (
        min(int(run.get("createdAtMs") or 0) for run in runs)
        if len(runs) >= LIST_LIMIT
        else None
    )
    overlapping = sorted(
        str(run.get("graphId"))
        for run in runs
        if run.get("routineName") == routine
        and run.get("state") not in _TERMINAL
        and str(run.get("graphId")) != current
    )
    if overlapping:
        return {
            "routine": routine,
            "skipped": {"running": overlapping},
            "graphs": [],
            "state": SKIPPED_STATE,
        }
    loaded = load_missions(request, space_id)
    state = previous_state(request, runs, routine)
    fresh, deferred = new_graphs(
        runs, loaded["missions"], state, now_ms=now_ms, window_days=window_days
    )
    # Carry forward only what still matters: graphs in the window that no
    # mission has taken since (the owner answered and the squire linked it).
    since = now_ms - window_days * DAY_MS
    linked = {graph for mission in loaded["missions"] for graph in mission["pacs"]}
    live = {
        str(run.get("graphId"))
        for run in runs
        if int(run.get("createdAtMs") or 0) >= since
    } - linked
    state = {
        **state,
        "asked": [graph for graph in state["asked"] if graph in live],
        "declined": [graph for graph in state["declined"] if graph in live],
    }
    by_id = {str(run.get("graphId")): run for run in runs}
    missions_by_id = {str(m["id"]): m for m in loaded["missions"]}
    graphs = []
    for run in fresh:
        found = candidates(run, loaded["missions"], by_id)
        payload = jev_payload(run, found, missions_by_id) if found else None
        graphs.append(
            {
                "graphId": run.get("graphId"),
                "name": run.get("name"),
                "sender": run.get("sender"),
                "candidates": found,
                "jev": payload,
                **({"tooLarge": True} if found and payload is None else {}),
            }
        )
    return {
        "spaceId": space_id,
        "routine": routine,
        "observedAt": now_ms,
        "previous": state,
        "graphs": graphs,
        "deferred": deferred,
        **({"coveredSinceMs": covered_since} if covered_since else {}),
        "unparseable": loaded["unparseable"],
        "conflicts": loaded["conflicts"],
    }


def _answers(reply: object) -> dict[str, float] | None:
    """Candidate-question probabilities from one jev reply, or None if it is not one."""

    if isinstance(reply, str):
        try:
            reply = json.loads(reply)
        except json.JSONDecodeError:
            return None
    if isinstance(reply, dict) and isinstance(reply.get("output"), dict):
        reply = reply["output"]
    answers = reply.get("answers") if isinstance(reply, dict) else None
    if not isinstance(answers, dict):
        return None
    result: dict[str, float] = {}
    for name, answer in answers.items():
        value = answer.get("noul") if isinstance(answer, dict) else None
        if isinstance(value, (int, float)) and 0 <= value <= 1:
            result[str(name)] = float(value)
    return result


def finish(
    collected: Mapping[str, Any],
    replies: Mapping[str, Any],
    *,
    declined: Sequence[str] = (),
) -> dict[str, Any]:
    """Step 3: one result per graph, and the ``state`` for ``--output-text``.

    ``replies`` maps graphId to the jev's reply (its JSON text or object).
    A graph with candidates but no usable reply is ``failed`` and comes back
    next round; the auto-link decision itself stays with the squire.
    Every graph with a result joins ``asked``: the squire either links it now
    (and the mission file covers it from then on) or asks its owner.
    ``declined`` are graphs the owner said belong to no mission.
    """

    results = []
    failed = []
    for graph in collected.get("graphs") or []:
        graph_id = str(graph.get("graphId"))
        found = graph.get("candidates") or []
        result: dict[str, Any] = {
            "graphId": graph_id,
            "match": None,
            "confidence": 0.0,
            "candidates": [entry["id"] for entry in found],
        }
        if not found:
            result["reason"] = "no candidate"
        elif graph.get("tooLarge"):
            result["reason"] = "payload over limit; ask the owner"
        else:
            answers = _answers(replies.get(graph_id))
            if answers is None:
                failed.append(graph_id)
                continue
            scored = [
                (answers[_question_name(index)], str(entry["id"]))
                for index, entry in enumerate(found)
                if _question_name(index) in answers
            ]
            if not scored:
                failed.append(graph_id)
                continue
            confidence, match = max(scored, key=lambda item: (item[0], item[1]))
            result.update(match=match, confidence=round(confidence, 4), reason="jev")
        results.append(result)
    previous = collected.get("previous") or {}
    return {
        "results": results,
        "state": bounded_state(
            asked=[
                graph
                for graph in [*(previous.get("asked") or []), *(r["graphId"] for r in results)]
                if graph not in set(declined)
            ],
            declined=[*(previous.get("declined") or []), *declined],
            failed=failed,
        ),
    }


def bounded_state(*, asked: list[str], declined: list[str], failed: list[str]) -> dict[str, Any]:
    """The round's output state, oldest entries dropped first to fit the output cap.

    A dropped graph re-enters the candidates next round; at worst the owner
    is asked about it once more.
    """

    asked, declined = list(dict.fromkeys(asked)), list(dict.fromkeys(declined))
    while True:
        state = {"asked": asked, "declined": declined, "failed": failed}
        if len(json.dumps(state).encode("utf-8")) <= STATE_LIMIT or not (asked or declined):
            return state
        if len(asked) >= len(declined):
            asked = asked[1:]
        else:
            declined = declined[1:]
