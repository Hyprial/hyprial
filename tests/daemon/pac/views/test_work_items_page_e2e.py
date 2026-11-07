"""One end-to-end pass: model → node file → published page → rendered views.

This is the work-items page's cross-branch contract test (model node ↔ view
node): it builds the model exactly the way the publisher's model side does
(``build_model`` over a collect-style snapshot plus loaded work items), assembles
the page exactly the way ``publish`` writes it (``page_html`` + ``node_script``),
boots that assembly in node with a minimal DOM stub — the same order a checkout
loads it: node file, work-item asset, inline script — and asserts on what the
page put into ``#content`` and into its merged ``PAC_NODES`` data.

It goes red when either side breaks the contract: a model key or field renamed
in ``build_model`` (``workItems`` / ``workItemsArchiveCutoffMs`` / entry fields
like ``pacsInWindow``), or the page/asset reading a different key.  The pure
per-side fixtures live in ``test_work_items_page_model.py`` and
``test_work_items_page_view.py``; this file only carries the joined pipeline.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib import resources
from typing import Any

import pytest

from hyprial.daemon.impl.pac.views import overview
from hyprial.daemon.impl.pac.views.work_items import (
    new_work_item,
    replace_head,
    replace_log,
)

NODE = "e2e-node"
DAY_MS = 24 * 3600 * 1000
NOW = 100 * DAY_MS  # observedAt; window_days=7 → archive cutoff at 93 * DAY_MS

OWNER = "user:owner@example.test"  # a person: short_actor keeps it whole
WORKER = "agent:owner@example.test:hq-node:worker-b"  # the assignee of M-eve-0002
ALICE = "agent:alice@example.test:hq-node:alice-bot"
CAROL = "agent:carol@example.test:hq-node:carol-bot"

HOSTILE = "<img src=x onerror=alert(1)>"

_ASSET = (
    resources.files("hyprial.daemon.impl.pac.views")
    .joinpath("work_items_view.js")
    .read_text("utf-8")
)


def _item(
    item_id: str,
    *,
    title: str = "A task",
    status: str = "todo",
    owner: str = OWNER,
    assignee: str | None = None,
    checklist: tuple[tuple[str, bool], ...] = (),
    depends: tuple[str, ...] = (),
    acceptance: str = "",
    pacs: tuple[str, ...] = (),
    log: tuple[str, ...] = (),
    opened: int = 50 * DAY_MS,
    closed: int | None = None,
) -> dict[str, Any]:
    """One loaded work item in the exact form load_missions hands build_model."""

    item = new_work_item(
        item_id=item_id,
        title=title,
        owner=owner,
        assignee=assignee,
        checklist=[text for text, _ in checklist],
        depends=list(depends),
        acceptance=acceptance,
        opened=opened,
    )
    for index, (_, done) in enumerate(checklist):
        if done:
            entries = [dict(entry) for entry in item.head["checklist"]]
            entries[index]["done"] = True
            item = replace_head(item, checklist=entries)
    if status != "todo":
        item = replace_head(item, status=status)
    if closed is not None:
        item = replace_head(item, closed=closed)
    if pacs:
        item = replace_head(item, pacs=list(pacs))
    if log:
        item = replace_log(item, list(log))
    projected = item.to_dict()
    return {
        **projected,
        "pacs": [str(graph).strip() for graph in projected["pacs"] if str(graph).strip()],
    }


def _work_items() -> list[dict[str, Any]]:
    """All statuses, archived done/dropped beyond the window, depends, empty
    log, in/out-of-window pacs, HTML in a title — the page's whole backlog."""

    return [
        _item(
            "M-eve-0001",
            title="前置项 " + HOSTILE,
            checklist=(("先做 <b>这步</b>", False),),
            depends=("M-eve-0007", "M-eve-0000"),  # one present, one absent
        ),
        _item(
            "M-eve-0002",
            title="登录提示",
            status="doing",
            assignee=WORKER,
            checklist=(("步骤甲", True), ("步骤乙", True), ("步骤丙", False)),
            acceptance="验收 <b>标准</b>：页面能打开",
            pacs=("wf-live", "wf-gone"),  # one in window, one outside
            log=(
                "- 2026-10-08T09:00:00+00:00 user:owner: 早的一条",
                "- 2026-10-08T10:00:00+00:00 user:owner: 最近一条 <script>日志</script>",
            ),
        ),
        _item(
            "M-eve-0003",
            title="受阻项",
            status="blocked",
            owner=CAROL,
            log=("- 2026-10-08T08:00:00+00:00 user:carol: 等待上游",),
        ),
        _item("M-eve-0007", title="受阻无日志", status="blocked", owner=ALICE),
        # Closed inside the window: done, but not archived.
        _item(
            "M-eve-0004",
            title="新近完成",
            status="done",
            closed=99 * DAY_MS,
            checklist=(("做完了", True),),
        ),
        # Closed far before the window start (cutoff 93 * DAY_MS): archived.
        _item("M-eve-0005", title="归档完成", status="done", closed=1_000),
        _item("M-eve-0006", title="归档放弃", status="dropped", closed=2_000),
    ]


def _model() -> dict[str, Any]:
    snapshot = {
        "observedAt": NOW,
        "runs": [
            {
                "graphId": "wf-live",
                "name": "fix-login-970",
                "sender": WORKER,
                "state": "completed",
                "createdAtMs": 99 * DAY_MS,
                "closedAtMs": 99 * DAY_MS + 1_000,
                "lastProgressAtMs": 99 * DAY_MS + 1_000,
                "routineName": None,
                "nodes": [],
                "edges": [],
                "roster": [],
            }
        ],
        "routines": [],
    }
    return overview.build_model(
        snapshot, node=NODE, missions=_work_items(), window_days=7
    )


_STUB = """
const elements = new Map();
const element = (id = "") => ({
  id, innerHTML: "", textContent: "", hidden: false, disabled: false, dataset: {},
  clientHeight: 200, clientWidth: 100,
  addEventListener() {}, setAttribute() {}, appendChild() {}, focus() {}, scrollIntoView() {},
  querySelector: () => element(), querySelectorAll: () => [], insertAdjacentHTML() {},
  getBoundingClientRect: () => ({left: 0, top: 0, width: 100, height: 50}),
});
const content = element("content");
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.setInterval = () => 0;
globalThis.sessionStorage = { getItem: () => (SAVED), setItem() {} };
globalThis.document = {
  head: element("head"),
  createElement: () => element("created"),
  getElementById: id => (id === "content" ? content : elements.get(id) || (elements.set(id, element(id)), elements.get(id))),
  querySelector: () => element(), querySelectorAll: () => [], addEventListener() {},
};
"""


def _boot(saved_view: dict[str, Any], model: dict[str, Any], epilogue: str) -> str:
    """Run the page exactly as a checkout loads it; return the epilogue's stdout."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = overview.page_html([f"{NODE}.js"])
    inline = re.search(r"<script>\n(.*?)</script>", page, re.S)
    assert inline, "the page lost its inline script"
    stub = _STUB.replace("SAVED", json.dumps(json.dumps({"view": saved_view})))
    boot = stub + overview.node_script(model) + _ASSET + inline.group(1)
    done = subprocess.run(
        [node, "-e", boot + "\n" + epilogue],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _list_page(model: dict[str, Any], *, toggle_archive: bool = False) -> str:
    """The booted page's #content for the work list, optionally after the
    已归档 toggle is clicked through the page's own delegated handler."""

    toggle = ""
    if toggle_archive:
        toggle = (
            "\ncontent.onclick({target: {closest: sel => "
            "sel === 'button' ? {dataset: {arch: '1'}} : null}});"
        )
    return _boot(
        {"level": "work"},
        model,
        toggle + "\nprocess.stdout.write(content.innerHTML);",
    )


def _detail_page(model: dict[str, Any], item_id: str) -> str:
    return _boot(
        {"level": "workItem", "id": item_id},
        model,
        "\nprocess.stdout.write(content.innerHTML);",
    )


def test_published_page_renders_the_work_items_model_end_to_end() -> None:
    model = _model()
    cutoff = model["workItemsArchiveCutoffMs"]
    assert cutoff == NOW - 7 * DAY_MS

    # ---- (a) the list: grouped by status then owner/assignee, with the
    # checklist n/m, depends, last log line and linked PAC runs ----
    html = _list_page(model)
    assert (
        html.index("进行中")
        < html.index("受阻")
        < html.index("待办")
        < html.index("已完成")
        < html.index("已放弃")
    )
    # Blocked groups by owner: alice-bot before carol-bot; the doing item
    # groups under its assignee worker-b, not its owner.
    assert (
        html.index('data-wi="M-eve-0002"')
        < html.index('data-wi="M-eve-0007"')
        < html.index('data-wi="M-eve-0003"')
        < html.index('data-wi="M-eve-0001"')
    )
    assert "<b>worker-b</b>" in html
    assert "<b>alice-bot</b>" in html and "<b>carol-bot</b>" in html
    # Checklist progress.
    assert ">2/3<" in html  # M-eve-0002
    assert ">0/1<" in html  # M-eve-0001
    assert ">—<" in html  # items without a checklist
    # Depends: the present one links, the absent one stays plain text.
    assert 'data-dep="M-eve-0007"' in html
    assert "M-eve-0000" in html and 'data-dep="M-eve-0000"' not in html
    # Last log line (text only), and no log cell for items without a log.
    assert "最近一条 &lt;script&gt;日志&lt;/script&gt;" in html
    assert "等待上游" in html
    assert html.count('class="wi-cell wi-log"') == 2
    # PAC runs: in-window links by run name; out-of-window stays a plain id.
    assert '<button type="button" class="wi-pac" data-run="wf-live"' in html
    assert "fix-login-970" in html
    assert "wf-gone" in html and 'data-run="wf-gone"' not in html
    assert "在统计窗口之外，未随这批数据发布" in html

    # ---- (c) the archive filter: done/dropped closed before the cutoff ----
    assert "M-eve-0004" in html  # done inside the window: still listed
    assert "M-eve-0005" not in html  # done, closed before the cutoff
    assert "M-eve-0006" not in html  # dropped, closed before the cutoff
    assert html.count('data-arch="1"') == 2  # one toggle per archived section
    assert html.count("另有 1 个已归档") == 2
    shown = _list_page(model, toggle_archive=True)
    assert 'data-wi="M-eve-0005"' in shown and 'data-wi="M-eve-0006"' in shown
    assert 'aria-pressed="true"' in shown
    assert shown.count("收起 1 个已归档") == 2

    # ---- (b) one item's detail ----
    detail = _detail_page(model, "M-eve-0002")
    assert "步骤丙" in detail and "✓" in detail and "○" in detail
    assert "验收 &lt;b&gt;标准&lt;/b&gt;：页面能打开" in detail
    assert "没有依赖。" in detail  # M-eve-0002 declares none
    assert "未验证 operator" in detail
    assert "打开" in detail  # the opened timestamp renders
    assert detail.index("最近一条") < detail.index("早的一条")  # log newest first
    assert '<button type="button" class="wi-pac" data-run="wf-live"' in detail
    assert "wf-gone" in detail and 'data-run="wf-gone"' not in detail
    quiet = _detail_page(model, "M-eve-0001")
    assert "还没有日志。" in quiet  # the empty-log state
    assert "没有写验收标准。" in quiet
    assert 'data-dep="M-eve-0007"' in quiet  # present dependency links
    assert "M-eve-0000" in quiet and 'data-dep="M-eve-0000"' not in quiet
    assert "先做 &lt;b&gt;这步&lt;/b&gt;" in quiet

    # ---- escaping: hostile markup crosses the whole pipeline safely ----
    for rendered in (html, shown, detail, quiet):
        assert HOSTILE not in rendered
        assert "<script>" not in rendered
    for carries_title in (html, shown, quiet):
        assert "&lt;img src=x onerror=alert(1)&gt;" in carries_title

    # ---- (d) the work-item data rides the page's own DATA merge ----
    data = _boot(
        {"level": "work"},
        model,
        "\nconst part = window.PAC_NODES['" + NODE + "'];\n"
        "process.stdout.write(JSON.stringify({"
        "cutoff: part.workItemsArchiveCutoffMs,"
        "items: part.workItems.map(w => ({id: w.id, status: w.status,"
        " closed: w.closed, pacs: w.pacs, pacsInWindow: w.pacsInWindow}))}));",
    )
    payload = json.loads(data)
    assert payload["cutoff"] == NOW - 7 * DAY_MS
    assert [entry["id"] for entry in payload["items"]] == [
        "M-eve-0001",
        "M-eve-0002",
        "M-eve-0003",
        "M-eve-0004",
        "M-eve-0005",
        "M-eve-0006",
        "M-eve-0007",
    ]
    by_id = {entry["id"]: entry for entry in payload["items"]}
    assert by_id["M-eve-0005"]["closed"] < payload["cutoff"]
    assert by_id["M-eve-0006"]["closed"] < payload["cutoff"]
    assert by_id["M-eve-0004"]["closed"] >= payload["cutoff"]
    assert by_id["M-eve-0002"]["pacs"] == ["wf-gone", "wf-live"]
    assert by_id["M-eve-0002"]["pacsInWindow"] == ["wf-live"]
