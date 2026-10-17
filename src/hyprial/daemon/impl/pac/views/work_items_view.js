// Work-item views for the PAC overview page (design-work-items §4).
//
// A sibling asset of overview_page.html, not part of the page itself: the
// page's own file is nearly at the structural 1000-line limit, so the
// publisher (overview.page_html / overview.publish) writes this file next to
// index.html and the page loads it with <script src> before its inline
// script -- the same mechanism the per-node data files use, and one that
// works from a file:// checkout.
//
// It registers window.PAC_WORK_ITEMS with the two entry points the page
// dispatches to for view.level "work" / "workItem":
//
//   renderList(el, ctx)      the 工作项 list
//   renderItem(el, ctx, id)  one item's detail
//
// ctx = { data, runs, esc, full, go } carries the page's merged DATA, its
// RUNS map (graph id -> run), its escaper, its full() time formatter and its
// go() navigator.  listHtml/itemHtml are pure builders (run directly by the
// projection tests in node); wiring is one delegated click handler.  Every
// field goes through ctx.esc: work-item text is user-written and crosses
// node boundaries.
(() => {
  const STATUS_ORDER = ["doing", "blocked", "todo", "done", "dropped"];
  const STATUS_ZH = { doing: "进行中", blocked: "受阻", todo: "待办", done: "已完成", dropped: "已放弃" };
  const EMPTY = "还没有工作项数据：发布端较旧（没有 workItems）或 org 空间的 work/ 目录为空。在 work/ 目录登记工作项后，一分钟内这里会自动出现。";
  let showArchived = false;  // the 已完成 archive filter; resets on reload like showOlder
  let mounted = null;        // { el, ctx, id } so the filter toggle can re-render

  const str = v => (v == null ? "" : String(v));
  const listOf = v => (Array.isArray(v) ? v : []);
  const num = v => (typeof v === "number" && Number.isFinite(v) ? v : null);

  // Every node reads the same org space, so a merged DATA can carry one item
  // several times.  Dedupe by id (first publisher wins) and normalise; a
  // status outside the known set keeps its own bucket at the end.
  function itemsOf(data) {
    const seen = new Set();
    const out = [];
    for (const raw of listOf(data && data.workItems)) {
      const id = str(raw && raw.id);
      if (!id || seen.has(id)) continue;
      seen.add(id);
      out.push({
        id,
        title: str(raw.title) || id,
        status: str(raw.status || "todo"),
        owner: str(raw.owner),
        assignee: raw.assignee == null ? null : str(raw.assignee),
        checklist: raw.checklist && typeof raw.checklist === "object"
          ? { done: num(raw.checklist.done) ?? 0, total: num(raw.checklist.total) ?? 0 }
          : { done: 0, total: 0 },
        depends: listOf(raw.depends).map(str),
        lastLog: raw.lastLog && typeof raw.lastLog === "object"
          ? { text: str(raw.lastLog.text), line: str(raw.lastLog.line) }
          : null,
        pacs: listOf(raw.pacs).map(str),
        pacsInWindow: new Set(listOf(raw.pacsInWindow).map(str)),
        acceptance: str(raw.acceptance),
        checklistItems: listOf(raw.checklistItems).map(c =>
          c && typeof c === "object" ? { text: str(c.text), done: c.done === true } : { text: "", done: false }),
        log: listOf(raw.log).map(str),
        opened: num(raw.opened),
        closed: num(raw.closed),
        unverifiedOperator: raw.unverifiedOperator === true,
      });
    }
    return out;
  }

  // 归档 rule: done/dropped items closed before the run window's start
  // (workItemsArchiveCutoffMs) stay out of the list until the filter shows
  // them.  A null cutoff (older publishers) or a null closed date archives
  // nothing.
  function archivedOf(item, cutoff) {
    return (item.status === "done" || item.status === "dropped")
      && cutoff != null && item.closed != null && item.closed < cutoff;
  }

  // One PAC id: runs inside the window link to the PAC view (the run's name
  // is the label); ids outside the window were not published with this DATA
  // and stay plain text.
  function pacPart(ctx, item, id) {
    const esc = ctx.esc;
    const run = ctx.runs && ctx.runs.get ? ctx.runs.get(id) : null;
    return item.pacsInWindow.has(id) && run
      ? `<button type="button" class="wi-pac" data-run="${esc(id)}" title="打开 PAC ${esc(id)}">${esc(str(run.name) || id)}</button>`
      : `<span class="wi-pac-plain mono" title="在统计窗口之外，未随这批数据发布">${esc(id)}</span>`;
  }

  // One dependency id: a button when the item is in this DATA, plain text
  // when it is not (archived away or from another batch).
  function depPart(esc, id, ids) {
    return ids.has(id)
      ? `<button type="button" class="wi-dep" data-dep="${esc(id)}">${esc(id)}</button>`
      : `<span class="wi-pac-plain mono" title="不在这批数据里">${esc(id)}</span>`;
  }

  function rowHtml(ctx, item, ids) {
    const esc = ctx.esc;
    const cl = item.checklist.total ? `${item.checklist.done}/${item.checklist.total}` : "—";
    return `<div class="wi-row" data-wi="${esc(item.id)}" role="button" tabindex="0" aria-label="打开工作项 ${esc(item.id)}">
      <span class="wi-id mono">${esc(item.id)}</span>
      <span class="wi-title">${esc(item.title)}</span>
      <span class="wi-cl mono" title="清单完成 ${item.checklist.done}/${item.checklist.total}">${esc(cl)}</span>
      ${item.depends.length ? `<span class="wi-cell"><span class="wi-cell-k">依赖</span>${item.depends.map(id => depPart(esc, id, ids)).join("")}</span>` : ""}
      ${item.lastLog ? `<span class="wi-cell wi-log" title="${esc(item.lastLog.line)}"><span class="wi-cell-k">最近</span>${esc(item.lastLog.text)}</span>` : ""}
      ${item.pacs.length ? `<span class="wi-cell"><span class="wi-cell-k">PAC</span>${item.pacs.map(id => pacPart(ctx, item, id)).join("")}</span>` : ""}
      ${item.unverifiedOperator ? `<span class="tag" title="owner action 是未经验证的 operator 写入">未验证 operator</span>` : ""}
    </div>`;
  }

  // Within one status, rows group under the person carrying the item: the
  // assignee when there is one, else the owner.
  function groupsHtml(ctx, rows, ids) {
    const esc = ctx.esc;
    const byWho = new Map();
    for (const item of rows) {
      const who = item.assignee || item.owner || "（没有负责人）";
      if (!byWho.has(who)) byWho.set(who, []);
      byWho.get(who).push(item);
    }
    return [...byWho.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([who, items]) =>
      `<div class="wi-group"><div class="wi-who"><b>${esc(who)}</b>${items.length > 1 ? `<span class="lane-meta">${items.length} 项</span>` : ""}</div>${items.map(item => rowHtml(ctx, item, ids)).join("")}</div>`
    ).join("");
  }

  function listHtml(ctx, withArchived) {
    const esc = ctx.esc;
    const items = itemsOf(ctx.data);
    if (!items.length) return `<div class="empty">${EMPTY}</div>`;
    const ids = new Set(items.map(item => item.id));
    const cutoff = num(ctx.data && ctx.data.workItemsArchiveCutoffMs);
    const buckets = new Map();
    for (const item of items) {
      if (!buckets.has(item.status)) buckets.set(item.status, []);
      buckets.get(item.status).push(item);
    }
    const ordered = [
      ...STATUS_ORDER.filter(status => buckets.has(status)),
      ...[...buckets.keys()].filter(status => !STATUS_ORDER.includes(status)).sort(),
    ];
    return ordered.map(status => {
      const rows = buckets.get(status);
      const archived = rows.filter(item => archivedOf(item, cutoff));
      const shown = withArchived ? rows : rows.filter(item => !archivedOf(item, cutoff));
      const fold = archived.length ? `<button type="button" class="fold" data-arch="1" aria-pressed="${withArchived}"><span class="caret" aria-hidden="true">${withArchived ? "▾" : "▸"}</span>${withArchived ? "收起" : "另有"} ${archived.length} 个已归档</button>` : "";
      return `<div class="section-label wi-label">${esc(STATUS_ZH[status] || status)}<span>${shown.length} 个</span>${fold}</div>
        ${shown.length ? groupsHtml(ctx, shown, ids) : `<div class="wi-none">这一组的工作项都已归档。</div>`}`;
    }).join("");
  }

  function itemHtml(ctx, id) {
    const esc = ctx.esc;
    const full = ctx.full;
    const items = itemsOf(ctx.data);
    const item = items.find(w => w.id === id);
    if (!item) return `<div class="empty">找不到工作项 <span class="mono">${esc(id)}</span>：它不在这批数据里（可能已归档、被删除，或来自另一批发布）。</div>`;
    const ids = new Set(items.map(w => w.id));
    const cl = item.checklist.total ? `${item.checklist.done}/${item.checklist.total}` : "—";
    return `<div class="linehead">
      <div>
        <div class="who" style="margin-bottom:6px"><span class="tag">工作项</span><span class="tag">${esc(STATUS_ZH[item.status] || item.status)}</span><span class="mono">${esc(item.id)}</span>${item.unverifiedOperator ? `<span class="tag" title="owner action 是未经验证的 operator 写入">未验证 operator</span>` : ""}</div>
        <h2>${esc(item.title)}</h2>
        <p>${esc(item.owner)}${item.assignee ? " → " + esc(item.assignee) : ""}${item.opened != null ? " · 打开 " + esc(full(item.opened)) : ""}${item.closed != null ? " · 关闭 " + esc(full(item.closed)) : ""}</p>
      </div>
      <div class="box"><dl class="kv">
        <dt>状态</dt><dd>${esc(STATUS_ZH[item.status] || item.status)}</dd>
        <dt>负责人</dt><dd>${esc(item.owner)}</dd>
        <dt>指派</dt><dd>${item.assignee ? esc(item.assignee) : "—"}</dd>
        <dt>清单</dt><dd class="mono">${esc(cl)}</dd>
        <dt>打开</dt><dd class="mono">${item.opened != null ? esc(full(item.opened)) : "—"}</dd>
        <dt>关闭</dt><dd class="mono">${item.closed != null ? esc(full(item.closed)) : "—"}</dd>
      </dl></div>
    </div>
    <div class="section-label">清单<span>${item.checklist.done}/${item.checklist.total}</span></div>
    ${item.checklistItems.length
      ? `<ul class="wi-check">${item.checklistItems.map(c => `<li class="${c.done ? "done" : ""}"><span class="wi-mark" aria-hidden="true">${c.done ? "✓" : "○"}</span>${esc(c.text)}</li>`).join("")}</ul>`
      : `<div class="wi-none">清单为空。</div>`}
    <div class="section-label">验收标准</div>
    ${item.acceptance ? `<p class="wi-accept">${esc(item.acceptance)}</p>` : `<div class="wi-none">没有写验收标准。</div>`}
    <div class="section-label">依赖</div>
    ${item.depends.length ? `<div class="wi-cell">${item.depends.map(dep => depPart(esc, dep, ids)).join("")}</div>` : `<div class="wi-none">没有依赖。</div>`}
    <div class="section-label">关联的 PAC<span>${item.pacs.length} 个</span></div>
    ${item.pacs.length ? `<div class="wi-cell">${item.pacs.map(pac => pacPart(ctx, item, pac)).join("")}</div>` : `<div class="wi-none">还没有关联的 PAC。</div>`}
    <div class="section-label">日志<span>最新在前</span></div>
    ${item.log.length ? `<div class="wi-logbox">${[...item.log].reverse().map(line => `<div class="wi-log-line">${esc(line)}</div>`).join("")}</div>` : `<div class="wi-none">还没有日志。</div>`}`;
  }

  // Styles ride with the asset (the page's own file is at its line limit).
  // They only use the page's existing CSS variables and classes.
  const CSS = `
.wi-label { margin-top: 14px; }
.wi-group { margin-bottom: 4px; }
.wi-who { display: flex; gap: 8px; align-items: baseline; font: 600 13px/1.3 var(--display); color: var(--muted); margin: 8px 0 2px; }
.wi-row { display: flex; flex-wrap: wrap; gap: 4px 16px; align-items: baseline; padding: 7px 10px; border-bottom: 1px solid var(--line); cursor: pointer; }
.wi-row:hover, .wi-row:focus-visible { background: var(--accent-soft); }
.wi-row .wi-id { color: var(--accent); font-weight: 600; font-size: 12px; }
.wi-title { font-weight: 500; min-width: 14em; flex: 1 1 14em; overflow-wrap: anywhere; }
.wi-cl { color: var(--muted); font-size: 12px; }
.wi-cell { display: inline-flex; flex-wrap: wrap; gap: 4px 8px; align-items: baseline; font-size: 12px; color: var(--muted); min-width: 0; overflow-wrap: anywhere; }
.wi-cell-k { color: var(--faint); }
.wi-log { max-width: 46ch; flex: 2 2 20em; }
.wi-pac, .wi-dep { border: 0; background: none; padding: 0; color: var(--accent); cursor: pointer; font: 12px var(--mono); text-decoration: underline dotted; }
.wi-pac:hover, .wi-dep:hover { color: var(--ink); }
.wi-pac-plain { color: var(--faint); font-size: 12px; }
.wi-none { font-size: 12.5px; color: var(--faint); padding: 4px 0 10px; }
.wi-check { list-style: none; margin: 0 0 14px; padding: 0; display: grid; gap: 5px; }
.wi-check li { display: flex; gap: 8px; align-items: baseline; }
.wi-check li.done { color: var(--muted); }
.wi-check li.done .wi-mark { color: var(--done); }
.wi-mark { flex: none; font-family: var(--mono); }
.wi-accept { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0 0 14px; max-width: 90ch; }
.wi-logbox { display: grid; gap: 3px; margin-bottom: 10px; }
.wi-log-line { font: 12px/1.5 var(--mono); overflow-wrap: anywhere; }
`;
  let styled = false;
  function ensureStyle() {
    if (styled || typeof document === "undefined" || !document.head) return;
    const tag = document.createElement("style");
    tag.id = "pac-work-items-style";
    tag.textContent = CSS;
    document.head.appendChild(tag);
    styled = true;
  }

  // One delegated handler for both views.  Buttons first (run links, dep
  // links, the archive filter), then the row itself; rows are divs because
  // they contain buttons, so they carry role=button and a keydown fallback.
  // A run link keeps the run's line as its from-fallback but sets back to the
  // work view it was opened from, so Escape/up returns there (not to the
  // line): the list for a row's link, the item detail for a detail's link.
  function wire(el, ctx) {
    el.onclick = e => {
      const b = e.target.closest("button");
      if (b) {
        if (b.dataset.run) {
          const run = ctx.runs.get(b.dataset.run);
          ctx.go({
            level: "pac",
            id: b.dataset.run,
            from: run ? run.line : null,
            back: mounted.id == null ? { level: "work" } : { level: "workItem", id: mounted.id },
          });
          return;
        }
        if (b.dataset.dep) { ctx.go({ level: "workItem", id: b.dataset.dep }); return; }
        if (b.dataset.arch !== undefined) { showArchived = !showArchived; rerender(); return; }
      }
      const row = e.target.closest("[data-wi]");
      if (row) ctx.go({ level: "workItem", id: row.dataset.wi });
    };
    el.onkeydown = e => {
      if (e.key !== "Enter" && e.key !== " ") return;
      if (e.target.closest("button")) return;
      const row = e.target.closest("[data-wi]");
      if (row) { e.preventDefault(); ctx.go({ level: "workItem", id: row.dataset.wi }); }
    };
  }
  function rerender() {
    if (!mounted) return;
    if (mounted.id == null) renderList(mounted.el, mounted.ctx);
    else renderItem(mounted.el, mounted.ctx, mounted.id);
  }
  function renderList(el, ctx) {
    mounted = { el, ctx, id: null };
    ensureStyle();
    el.innerHTML = listHtml(ctx, showArchived);
    wire(el, ctx);
  }
  function renderItem(el, ctx, id) {
    mounted = { el, ctx, id };
    ensureStyle();
    el.innerHTML = itemHtml(ctx, id);
    wire(el, ctx);
  }

  const api = { renderList, renderItem, listHtml, itemHtml, itemsOf, archivedOf };
  if (typeof window !== "undefined") window.PAC_WORK_ITEMS = api;
  if (typeof globalThis !== "undefined") globalThis.PAC_WORK_ITEMS = api;
})();
