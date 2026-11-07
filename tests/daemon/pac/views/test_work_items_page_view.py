"""Work-item views on the PAC overview page (design-work-items §4).

The views live in a sibling asset (``work_items_view.js``) the publisher
ships next to the page; the page itself only merges ``workItems`` /
``workItemsArchiveCutoffMs`` from the node files and dispatches to
``window.PAC_WORK_ITEMS``.  These tests run the asset's pure builders in
node against a fixture DATA shaped exactly like the work-items page model
contract (``test_work_items_page_model.py``); the page-side merge is pinned
by substring checks the way the other page-behaviour tests do.
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

_ASSET = (
    resources.files("hyprial.daemon.impl.pac.views")
    .joinpath("work_items_view.js")
    .read_text("utf-8")
)
_PAGE = (
    resources.files("hyprial.daemon.impl.pac.views")
    .joinpath("overview_page.html")
    .read_text("utf-8")
)

HOSTILE = "<img src=x onerror=alert(1)>"
# Review 1218 (3): the attribute-breaking payload — quote, angle brackets, a
# closing </script>, an SVG onload — plus the JS line separators U+2028/2029,
# carried in item ids, depends, assignees and PAC ids.
HOSTILE_ATTR = '"\'><img src=x onerror=alert(1)></script><svg/onload=alert(2)>\u2028\u2029'


def _item(item_id: str, status: str, **over: Any) -> dict[str, Any]:
    item = {
        "id": item_id,
        "title": item_id,
        "status": status,
        "owner": "alice",
        "assignee": None,
        "checklist": {"done": 0, "total": 0},
        "depends": [],
        "lastLog": None,
        "pacs": [],
        "pacsInWindow": [],
        "acceptance": "",
        "checklistItems": [],
        "log": [],
        "opened": 100,
        "closed": None,
        "unverifiedOperator": False,
    }
    item.update(over)
    return item


def _fixture() -> dict[str, Any]:
    return {
        "workItemsArchiveCutoffMs": 800,
        "workItems": [
            _item("M-1", "todo", title="前置项 " + HOSTILE,
                  checklist={"done": 0, "total": 1},
                  checklistItems=[{"text": "先做 <b>这步</b>", "done": False}]),
            _item(
                "M-2",
                "doing",
                title="登录提示 " + HOSTILE,
                assignee="bob",
                checklist={"done": 2, "total": 3},
                depends=["M-1", "M-x"],
                lastLog={"text": "最近一条 <script>日志</script>", "line": "- 2026-10-08 bob: 最近一条"},
                pacs=["wf-in", "wf-out"],
                pacsInWindow=["wf-in"],
                acceptance="验收 <b>标准</b>：页面能打开",
                checklistItems=[
                    {"text": "步骤甲", "done": True},
                    {"text": "步骤乙", "done": True},
                    {"text": "步骤丙", "done": False},
                ],
                log=["- 早的一条", "- 晚的一条"],
                unverifiedOperator=True,
            ),
            _item("M-3", "blocked", owner="carol", title="受阻-卡罗尔",
                  lastLog={"text": "等待上游", "line": "- 2026-10-08 carol: 等待上游"}),
            _item("M-7", "blocked", owner="alice", title="受阻-艾丽丝"),
            _item("M-4", "done", title="新近完成", closed=900,
                  checklist={"done": 1, "total": 1},
                  checklistItems=[{"text": "做完了", "done": True}]),
            _item("M-5", "done", title="归档完成", closed=700),
            _item("M-6", "dropped", owner="carol", title="归档放弃", closed=700),
        ],
    }


# wf-in is inside the window and published with this DATA; wf-out is outside
# the window, so it never arrives as a run and must stay a plain id.  The
# hostile PAC id is a linked run, so it exercises the data-run attribute.
_RUNS_JS = (
    "new Map([['wf-in', {id: 'wf-in', name: '窗口内的运行', line: 'hq|user:owner|m:M-2'}], "
    f"[{json.dumps(HOSTILE_ATTR)}, {{id: 'wf-x', name: '运行的名称', line: 'l'}}]])"
)
_ESC_JS = (
    "const esc = s => String(s ?? '').replace(/[&<>\"']/g,"
    " c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '\"': '&quot;', \"'\": '&#39;'}[c]));"
)


def _render(call: str, data: dict[str, Any] | None = None) -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    payload = json.dumps(_fixture() if data is None else data, ensure_ascii=False)
    script = (
        _ASSET
        + _ESC_JS
        + f"\nconst ctx = {{ data: {payload}, runs: {_RUNS_JS}, esc, full: t => 'T' + t, go: () => {{}} }};\n"
        + f"process.stdout.write({call});\n"
    )
    done = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _list(data: dict[str, Any] | None = None, *, archived: bool = False) -> str:
    return _render(f"globalThis.PAC_WORK_ITEMS.listHtml(ctx, {json.dumps(archived)})", data)


def _detail(item_id: str, data: dict[str, Any] | None = None) -> str:
    return _render(f"globalThis.PAC_WORK_ITEMS.itemHtml(ctx, {json.dumps(item_id)})", data)


def test_list_groups_by_status_then_by_owner_or_assignee() -> None:
    html = _list()
    # Status sections: doing, blocked, todo, then done, dropped.
    assert (
        html.index("进行中")
        < html.index("受阻")
        < html.index("待办")
        < html.index("已完成")
        < html.index("已放弃")
    )
    # M-2 doing, then blocked grouped by owner (alice before carol), then todo.
    # Row positions, not bare ids: ids also appear in depends and PAC links.
    assert (
        html.index('data-wi="M-2"')
        < html.index('data-wi="M-7"')
        < html.index('data-wi="M-3"')
        < html.index('data-wi="M-1"')
    )
    # The assignee carries a doing item: bob heads M-2's group.
    assert "<b>bob</b>" in html


def test_list_shows_checklist_progress() -> None:
    html = _list()
    assert ">2/3<" in html  # M-2
    assert ">0/1<" in html  # M-1
    assert ">—<" in html  # no checklist items at all


def test_list_links_dependencies_to_those_items() -> None:
    html = _list()
    assert 'data-dep="M-1"' in html  # a known dependency opens its item
    assert "M-x" in html  # an unknown one still shows its id
    assert 'data-dep="M-x"' not in html


def test_list_shows_the_last_log_line_and_nothing_without_one() -> None:
    html = _list()
    assert "最近一条 &lt;script&gt;日志&lt;/script&gt;" in html
    assert "等待上游" in html
    # Only M-2 and M-3 carry lastLog; rows without one render no log cell.
    assert html.count('class="wi-cell wi-log"') == 2


def test_list_links_window_runs_and_keeps_outside_runs_as_plain_ids() -> None:
    html = _list()
    assert '<button type="button" class="wi-pac" data-run="wf-in"' in html
    assert "窗口内的运行" in html  # the link shows the run's name
    assert "wf-out" in html
    assert 'data-run="wf-out"' not in html
    assert "在统计窗口之外，未随这批数据发布" in html


def test_list_marks_unverified_operator_items() -> None:
    html = _list()
    assert html.count("未验证 operator") == 1  # M-2 only


def test_archive_filter_hides_done_dropped_closed_before_the_cutoff() -> None:
    default = _list()
    assert "M-4" in default  # closed 900 >= cutoff 800: still listed
    assert "M-5" not in default  # done, closed 700 < 800: archived
    assert "M-6" not in default  # dropped, closed 700 < 800: archived
    assert default.count('data-arch="1"') == 2  # one toggle per section
    assert 'aria-pressed="false"' in default
    shown = _list(archived=True)
    assert "M-5" in shown and "M-6" in shown
    assert 'aria-pressed="true"' in shown


def test_detail_renders_checklist_acceptance_log_runs_and_depends() -> None:
    html = _detail("M-2")
    # Checklist items with their done state.
    assert 'class="done"' in html and "✓" in html and "○" in html
    assert "步骤丙" in html
    # Acceptance, dependencies, unverified marker, opened time.
    assert "验收 &lt;b&gt;标准&lt;/b&gt;：页面能打开" in html
    assert 'data-dep="M-1"' in html
    assert "未验证 operator" in html
    assert "T100" in html
    # Log latest first.
    assert html.index("晚的一条") < html.index("早的一条")
    # Linked runs behave exactly like the list.
    assert '<button type="button" class="wi-pac" data-run="wf-in"' in html
    assert "wf-out" in html and 'data-run="wf-out"' not in html


def test_every_rendered_field_is_escaped() -> None:
    for html in (_list(), _detail("M-2")):
        assert HOSTILE not in html
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        assert "<script>" not in html
        assert "<b>标准</b>" not in html
    assert "<b>这步</b>" not in _detail("M-1")


def test_empty_states_for_no_items_and_older_publishers() -> None:
    assert "还没有工作项数据" in _list(data={})  # older publisher: no key at all
    assert "还没有工作项数据" in _list(data={"workItems": []})
    missing = _detail("M-absent")
    assert "找不到工作项" in missing and "M-absent" in missing


def test_page_merges_work_item_keys_from_every_part_and_dispatches() -> None:
    # The DATA merge carries both keys like it carries missions.
    assert "workItems: PARTS.flatMap(p => p.workItems || [])," in _PAGE
    assert "workItemsArchiveCutoffMs: PARTS.map(p => p.workItemsArchiveCutoffMs)" in _PAGE
    # The render dispatch and the saved-view restore reach the asset.
    assert "window.PAC_WORK_ITEMS?.renderList(el, { data: DATA, runs: RUNS, esc, full, go })" in _PAGE
    assert "window.PAC_WORK_ITEMS?.renderItem(el, { data: DATA, runs: RUNS, esc, full, go }, view.id)" in _PAGE
    assert 'saved.view.level === "workItem" && DATA.workItems.some' in _PAGE
    # Navigation: crumbs reach the list, up() walks back.
    assert 'b.dataset.go === "work" ? { level: "work" }' in _PAGE
    assert 'view.level === "workItem") go({ level: "work" })' in _PAGE


def test_asset_loads_without_a_dom_and_registers_itself() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    done = subprocess.run(
        [node, "-e", _ASSET + "\nprocess.stdout.write(typeof globalThis.PAC_WORK_ITEMS.renderList);"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout == "function"
    assert "renderItem" in _ASSET and re.search(r"level: \"pac\"", _ASSET)


# ---- the assembled page boots: publisher output + node file + asset ----


def _page_model(work_items: list[dict[str, Any]] | None) -> dict[str, Any]:
    model: dict[str, Any] = {
        "node": "hq-node",
        "observedAt": 9_000,
        "actors": [],
        "lines": [],
        "runs": [],
        "links": [],
        "nodeLinks": [],
        "missions": [],
    }
    if work_items is not None:
        model["workItems"] = work_items
        model["workItemsArchiveCutoffMs"] = 800
    return model


def _boot_page(
    saved_view: dict[str, Any] | None, model: dict[str, Any], epilogue: str = ""
) -> str:
    """Run the page exactly as a checkout loads it: node file, asset, inline.

    A minimal DOM stub stands in for the browser; the assertions are on what
    the page put into #content, so the page glue (DATA merge, saved-view
    restore, dispatch) is exercised for real, not as substrings.  The stub
    captures every document.addEventListener handler in ``docHandlers`` and
    the epilogue (same scope, run after the boot) may drive the page's own
    handlers — crumbs/content onclick, the keydown listeners — before its
    stdout replaces the default content dump the caller asserts on.
    """

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    page = overview.page_html(["hq-node.js"])
    inline = re.search(r"<script>\n(.*?)</script>", page, re.S)
    assert inline, "the page lost its inline script"
    saved = json.dumps({"view": saved_view} if saved_view else None)
    stub = (
        """
const elements = new Map();
const element = (id = "") => ({
  id, innerHTML: "", textContent: "", hidden: false, disabled: false, dataset: {},
  clientHeight: 200, clientWidth: 100,
  addEventListener() {}, setAttribute() {}, appendChild() {}, focus() {}, scrollIntoView() {},
  querySelector: () => element(), querySelectorAll: () => [], insertAdjacentHTML() {},
  getBoundingClientRect: () => ({left: 0, top: 0, width: 100, height: 50}),
});
const content = element("content");
const docHandlers = {};
globalThis.window = globalThis;
globalThis.addEventListener = () => {};
globalThis.setInterval = () => 0;
globalThis.sessionStorage = { getItem: () => (SAVED), setItem() {} };
globalThis.document = {
  head: element("head"),
  createElement: () => element("created"),
  getElementById: id => (id === "content" ? content : elements.get(id) || (elements.set(id, element(id)), elements.get(id))),
  querySelector: () => element(), querySelectorAll: () => [],
  addEventListener: (type, fn) => { (docHandlers[type] = docHandlers[type] || []).push(fn); },
};
""".replace("SAVED", json.dumps(saved))
    )
    boot = stub + overview.node_script(model) + _ASSET + inline.group(1)
    tail = epilogue or "process.stdout.write(content.innerHTML);"
    done = subprocess.run(
        [node, "-e", boot + "\n" + tail],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_published_page_boots_the_work_list_through_the_real_assembly() -> None:
    items = [it for it in _fixture()["workItems"] if it["id"] in {"M-2", "M-5"}]
    html = _boot_page({"level": "work"}, _page_model(items))
    assert 'data-wi="M-2"' in html
    assert "进行中" in html and "已完成" in html
    assert "M-5" not in html  # archived by default on the booted page
    assert "另有 1 个已归档" in html


def test_published_page_without_work_items_boots_to_the_empty_state() -> None:
    html = _boot_page({"level": "work"}, _page_model(None))
    assert "还没有工作项数据" in html


# ---- review 1218: the page's own entries into and out of the work views ----


_PAC = "wf-in"
_LINE = "hq-node|user:owner@example.test|m:M-2"


def _nav_model() -> dict[str, Any]:
    """One work item with one in-window PAC on its own (renderable) line.

    The line is complete enough that the line view renders too: the back-
    navigation test must fail on a wrong landing page, not on a crash.
    """

    item = _item(
        "M-2",
        "doing",
        title="登录提示",
        assignee="bob",
        pacs=[_PAC],
        pacsInWindow=[_PAC],
        acceptance="验收标准文本",
        checklist={"done": 1, "total": 1},
        checklistItems=[{"text": "步骤甲", "done": True}],
        log=["- 早的一条"],
    )
    return {
        "node": "hq-node",
        "observedAt": 9_000,
        "actors": [
            {"id": _LINE, "node": "hq-node", "name": "user:owner@example.test", "human": True}
        ],
        "lines": [
            {
                "id": _LINE,
                "kind": "mission",
                "title": "登录提示",
                "owner": "user:owner@example.test",
                "background": "背景",
                "goal": "目标",
                "actor": _LINE,
                "node": "hq-node",
                "mission": {"id": "M-2", "status": "doing"},
                "runs": [_PAC],
                "routines": [],
            }
        ],
        "runs": [
            {
                "id": _PAC,
                "name": "fix-login-970",
                "actor": _LINE,
                "line": _LINE,
                "state": "completed",
                "sender": "worker-b",
                "created": 8_000,
                "closed": 8_500,
                "progress": 8_500,
                "current": None,
                "routine": None,
                "nodes": [],
                "edges": [],
            }
        ],
        "links": [],
        "nodeLinks": [],
        "missions": [],
        "workItems": [item],
        "workItemsArchiveCutoffMs": 800,
    }


def test_lines_level_opens_the_work_item_views_through_the_page_handlers() -> None:
    """Review 1218 (1): the lines level must link into the work-item views.

    Every earlier test seeded the work/workItem level into sessionStorage; a
    viewer landing on {"level":"lines"} — the default — had no way in.
    """

    epilogue = r'''
const crumbs = document.getElementById("crumbs");
const entry = /<button data-go="work">[^<]*<\/button>/.exec(crumbs.innerHTML);
if (!entry) process.stdout.write("NO-ENTRY crumbs=" + crumbs.innerHTML);
else {
  crumbs.onclick({target: {closest: s => (s === "button[data-go]" ? {dataset: {go: "work"}} : null)}});
  const row = /data-wi="([^"]+)"/.exec(content.innerHTML);
  if (!row) process.stdout.write("NO-LIST " + content.innerHTML);
  else {
    const clicked = row[1];
    const listHtml = content.innerHTML;
    content.onclick({target: {closest: s => (s === "[data-wi]" ? {dataset: {wi: clicked}} : null)}});
    process.stdout.write("LIST " + clicked + "\n@@LIST@@\n" + listHtml + "\n@@DETAIL@@\n" + content.innerHTML);
  }
}
'''
    out = _boot_page({"level": "lines"}, _nav_model(), epilogue=epilogue)
    assert "NO-ENTRY" not in out  # the lines level carries the 工作项 entry
    assert "NO-LIST" not in out
    stages = out.split("\n@@LIST@@\n")
    assert stages[0] == "LIST M-2"  # the crumbs click rendered the list
    listing, detail = stages[1].split("\n@@DETAIL@@\n")
    assert 'data-wi="M-2"' in listing and "进行中" in listing
    assert "fix-login-970" in listing  # the linked PAC is on the row
    # The row click rendered the item's detail.
    assert "<h2>登录提示</h2>" in detail
    assert "验收标准" in detail and "关联的 PAC" in detail


def test_pac_link_back_returns_to_the_work_view_it_was_opened_from() -> None:
    """Review 1218 (4): PAC links from the work views return there on Escape.

    The link used to pass only ``from: run.line``, so back/up landed on the
    mission line; it must return to the item detail (or the list) instead.
    """

    epilogue = r'''
content.onclick({target: {closest: s => (s === "button" ? {dataset: {run: "PACID"}} : null)}});
const pacHtml = content.innerHTML;
docHandlers.keydown.at(-1)({key: "Escape", target: {closest: () => null}});
process.stdout.write(pacHtml + "\n@@BACK@@\n" + content.innerHTML);
'''.replace("PACID", _PAC)
    # Opened from the item detail: Escape returns to that detail.
    out = _boot_page({"level": "workItem", "id": "M-2"}, _nav_model(), epilogue=epilogue)
    pac, back = out.split("\n@@BACK@@\n")
    assert "fix-login-970" in pac  # the PAC view opened
    assert "<h2>登录提示</h2>" in back and "验收标准" in back
    assert 'class="gv"' not in back  # not the PAC view anymore
    # Opened from the list: Escape returns to the list.
    out = _boot_page({"level": "work"}, _nav_model(), epilogue=epilogue)
    pac, back = out.split("\n@@BACK@@\n")
    assert "fix-login-970" in pac
    assert 'data-wi="M-2"' in back and "进行中" in back


def test_hostile_ids_depends_assignees_and_pacs_inject_nothing() -> None:
    """Review 1218 (3): attribute-context escaping is load-bearing.

    The hostile payload rides the id, a dep id, the assignee and a PAC id —
    fields whose escaped forms land inside quoted attributes — plus the JS
    line separators U+2028/2029.  No tag or attribute may be injected into
    the rendered list or detail (this is the test that goes red when esc()
    is removed from data-dep or the detail assignee).
    """

    hostile_item = _item(
        HOSTILE_ATTR,
        "doing",
        title="标题 " + HOSTILE_ATTR,
        assignee=HOSTILE_ATTR,
        depends=[HOSTILE_ATTR],  # self-dependency: a known id, so it links
        pacs=[HOSTILE_ATTR],
        pacsInWindow=[HOSTILE_ATTR],
        lastLog={"text": "日志 " + HOSTILE_ATTR, "line": "- 2026-10-08 bob: 最近"},
        acceptance="验收 " + HOSTILE_ATTR,
        log=["- 日志 " + HOSTILE_ATTR],
    )
    watcher = _item("M-watch", "todo", depends=[HOSTILE_ATTR])
    data = {"workItemsArchiveCutoffMs": 800, "workItems": [hostile_item, watcher]}

    for html in (_list(data=data), _detail(HOSTILE_ATTR, data=data)):
        assert HOSTILE_ATTR not in html  # never raw
        for tag in ("<img", "<svg", "</script"):
            assert tag not in html
        # Attribute values are escaped: the payload's leading "'> stays inside.
        assert 'data-dep="&quot;&#39;&gt;' in html
        # Text contexts survive as inert escaped text, separators included.
        assert "&lt;img src=x onerror=alert(1)&gt;" in html
        assert "\u2028" in html and "\u2029" in html
    # The row and the PAC link escape the id inside their attributes too.
    listing = _list(data=data)
    assert 'data-wi="&quot;' in listing
    assert 'data-run="&quot;' in listing
