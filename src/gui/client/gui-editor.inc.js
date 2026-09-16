// Pure document edits. No Host calls, business commands, HTML, or executable input.
function guiEditorHelpers() {
  const containers = ['Stack', 'Grid', 'Split', 'Tabs'];
  let sequence = 0;
  const fail = message => { throw new Error(message); };
  function copy(value) {
    const seen = new Set();
    function check(item, depth) {
      if (depth > 28) fail('页面嵌套过深');
      if (item && typeof item === 'object') {
        if (seen.has(item)) fail('页面不能循环引用');
        seen.add(item);
        const prototype = Object.getPrototypeOf(item);
        if (!Array.isArray(item) && (Object.prototype.toString.call(item) !== '[object Object]' || prototype !== null && Object.getPrototypeOf(prototype) !== null)) fail('仅支持页面配置');
        if (Array.isArray(item) && prototype !== null && Object.hasOwn(prototype, 'toJSON')) fail('仅支持页面配置');
        if (Array.isArray(item) && item.length > 256) fail('页面内容过多');
        for (const key of Object.keys(item)) {
          if (['__proto__', 'constructor', 'prototype'].includes(key) || !Object.hasOwn(Object.getOwnPropertyDescriptor(item, key), 'value')) fail('不支持的配置');
          check(item[key], depth + 1);
        }
        seen.delete(item);
      } else if (item !== null && !['string', 'boolean', 'number'].includes(typeof item) || typeof item === 'number' && !Number.isFinite(item)) fail('配置必须是有效数据');
    }
    check(value, 0);
    const text = JSON.stringify(value);
    if (new TextEncoder().encode(text).length > 65536) fail('页面配置超过 64 KiB');
    return JSON.parse(text);
  }
  function walk(node, visit, parent = null, index = 0, depth = 0) {
    visit(node, parent, index, depth);
    (node.children || []).forEach((child, i) => walk(child, visit, node, i, depth + 1));
  }
  function inspect(document) {
    const doc = copy(document), pageIds = new Set(), instances = new Set(); let total = 0;
    if (![1, 2].includes(doc.schemaVersion) || !Array.isArray(doc.pages) || !doc.pages.length || doc.pages.length > 24) fail('页面数量应为 1 至 24');
    if (doc.kind === 'page' && doc.pages.length !== 1) fail('个人常用页只能包含一个页面');
    if (doc.theme !== undefined) guiValidateTheme(doc.theme);
    if (doc.layout?.objects !== undefined) {
      const objects = doc.layout.objects;
      if (!objects || typeof objects !== 'object' || Array.isArray(objects) || Object.keys(objects).some(key => !['mode', 'width'].includes(key)) || objects.mode !== undefined && !['inline', 'collapsible', 'drawer'].includes(objects.mode) || objects.width !== undefined && (!Number.isInteger(objects.width) || objects.width < 200 || objects.width > 480)) fail('对象列表设置无效');
    }
    if (doc.layout?.navigation !== undefined && !['top', 'left', 'native'].includes(doc.layout.navigation)) fail('导航布局不支持');
    if (doc.layout?.navigation === 'native' && doc.layout.nativeSidebar === false) fail('原生三栏导航必须保留原生侧栏');
    const slug = value => typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(value);
    for (const page of doc.pages) {
      if (!slug(page.id) || pageIds.has(page.id)) fail('页面标识重复或无效'); pageIds.add(page.id);
      if (page.appearance !== undefined) guiValidateAppearance(page.appearance);
      const styleScopes = ['light', 'dark'].map(mode => ({ root: guiModuleAppearanceVariables(guiStyleVariables(doc.theme || {}, mode), page.appearance || {}), nodes: new Map() }));
      const ids = new Set(); let native = 0;
      walk(page.layout, (node, parent, index, depth) => {
        if (node.appearance !== undefined) guiValidateAppearance(node.appearance);
        for (const scope of styleScopes) scope.nodes.set(node, guiModuleAppearanceVariables(parent ? scope.nodes.get(parent) : scope.root, node.appearance || {}));
        if (++total > 256 || depth > 12) fail('页面内容过多或嵌套过深');
        if (!slug(node.id) || ids.has(node.id)) fail('组件标识重复或无效'); ids.add(node.id);
        if (containers.includes(node.type)) {
          if (!Array.isArray(node.children) || !node.children.length || node.children.length > 24) fail('分组需要 1 至 24 个组件');
          if (node.type === 'Split' && node.children.length !== 2) fail('分栏需要两个组件');
          if (node.type === 'Tabs' && node.labels && node.labels.length !== node.children.length) fail('标签与组件数量不一致');
          if (node.gap !== undefined && (!Number.isInteger(node.gap) || node.gap < 0 || node.gap > 32)) fail('间距应为 0 至 32');
          if (node.columns !== undefined && (!Number.isInteger(node.columns) || node.columns < 1 || node.columns > 4)) fail('列数应为 1 至 4');
          if (node.ratio !== undefined && (!Number.isInteger(node.ratio) || node.ratio < 20 || node.ratio > 80)) fail('分栏比例应为 20 至 80');
        } else if (node.type === 'Text') {
          if (typeof node.text !== 'string' || !node.text.trim() || node.text.length > 4000) fail('文字应为 1 至 4000 字');
        } else if (node.type === 'Feature') {
          if (typeof node.feature !== 'string') fail('请选择功能模块');
          if ((node.view || 'default') === 'default' && (doc.schemaVersion === 1 || node.feature === 'dsh.conversation') && ++native > 1) fail('同一页面只能放置一个完整 Agent 会话；其他模块可用入口或列表');
          if (doc.schemaVersion === 2) {
            if (!slug(node.instanceId) || instances.has(node.instanceId)) fail('模块实例标识重复或无效'); instances.add(node.instanceId);
          }
        } else fail('不支持的组件类型');
      });
    }
    if (!Array.isArray(doc.navigation) || doc.navigation.length > 32) fail('导航项目过多');
    const navIds = new Set();
    for (const nav of doc.navigation) {
      if (!slug(nav.id) || navIds.has(nav.id)) fail('导航标识重复'); navIds.add(nav.id);
      if ((nav.pageId !== undefined) === (nav.feature !== undefined) || nav.pageId !== undefined && !pageIds.has(nav.pageId)) fail('导航目标不存在');
      if (typeof nav.label !== 'string' || !nav.label.trim() || nav.label.length > 160) fail('导航名称应为 1 至 160 字');
    }
    return doc;
  }
  function fresh(doc, prefix = 'node') {
    const used = new Set(doc.pages.flatMap(page => { const ids = [page.id]; walk(page.layout, node => ids.push(node.id, node.instanceId)); return ids; }).concat(doc.navigation.map(n => n.id)));
    let id; do { id = prefix + '-' + Date.now().toString(36) + '-' + (++sequence).toString(36); } while (used.has(id)); return id;
  }
  function locate(doc, pageId, nodeId) {
    const page = doc.pages.find(p => p.id === pageId); if (!page) fail('页面不存在');
    let found; walk(page.layout, (node, parent, index) => { if (node.id === nodeId) found = { page, node, parent, index }; });
    if (!found) fail('组件不存在'); return found;
  }
  function replace(found, node) { if (found.parent) found.parent.children[found.index] = node; else found.page.layout = node; }
  function textNode(doc) { return { type: 'Text', id: fresh(doc), text: '在这里添加内容' }; }
  function normalize(node) {
    if (!node.children) return node;
    node.children = node.children.map(normalize);
    if (node.type === 'Split' && node.children.length === 1) return node.children[0];
    return node;
  }
  function detach(doc, found) {
    if (!found.parent) fail('整页布局不能移动或删除，请删除页面或调整分组');
    found.parent.children.splice(found.index, 1);
    if (found.parent.type === 'Tabs' && found.parent.labels) found.parent.labels.splice(found.index, 1);
    if (!found.parent.children.length) {
      const parent = locate(doc, found.page.id, found.parent.id);
      replace(parent, { type: 'Text', id: found.parent.id, text: '在这里添加内容' });
    }
    found.page.layout = normalize(found.page.layout);
  }
  function insert(doc, target, node, index) {
    if (containers.includes(target.node.type)) {
      if (target.node.type === 'Split') fail('分栏已有两个区域，请添加到区域内');
      const at = Number.isInteger(index) ? Math.max(0, Math.min(index, target.node.children.length)) : target.node.children.length;
      target.node.children.splice(at, 0, node);
      if (target.node.type === 'Tabs' && target.node.labels) target.node.labels.splice(at, 0, '新标签');
    } else replace(target, { type: 'Stack', id: fresh(doc, 'group'), children: [target.node, node], gap: 12 });
  }
  function makeNode(doc, type, feature) {
    const node = { type, id: fresh(doc) };
    if (type === 'Text') node.text = '我的文字';
    else if (type === 'Feature') { node.feature = feature || 'dsh.conversation'; node.view = 'launcher'; if (doc.schemaVersion === 2) node.instanceId = fresh(doc, 'instance'); }
    else if (containers.includes(type)) { node.children = [textNode(doc)]; if (type === 'Split') { node.children.push(textNode(doc)); node.ratio = 50; } if (type === 'Grid') node.columns = 2; }
    else fail('不支持的组件类型');
    return node;
  }
  function apply(document, action) {
    const doc = inspect(document);
    if (!action || typeof action.type !== 'string') fail('请选择编辑操作');
    if (action.type === 'objectLayout') {
      if (doc.kind !== 'shell') fail('个人页面不能更改工作空间对象列表布局');
      doc.layout = { ...doc.layout, objects: { ...doc.layout?.objects, ...action.objects } };
    } else if (action.type === 'navigationLayout') {
      if (doc.kind !== 'shell') fail('个人页面不能更改工作空间导航布局');
      if (!['top', 'left', 'native'].includes(action.navigation)) fail('导航布局不支持');
      doc.layout = { ...doc.layout, navigation: action.navigation, nativeSidebar: action.navigation !== 'top' };
    } else if (action.type === 'addPage') {
      if (doc.kind === 'page') fail('个人常用页只能包含一个页面');
      const id = fresh(doc, 'page'); doc.pages.push({ id, title: '新页面', layout: textNode(doc) }); doc.navigation.push({ id: fresh(doc, 'nav'), label: '新页面', pageId: id });
    } else if (action.type === 'removePage') {
      if (doc.pages.length === 1) fail('至少保留一个页面');
      doc.pages = doc.pages.filter(p => p.id !== action.pageId); doc.navigation = doc.navigation.filter(n => n.pageId !== action.pageId);
    } else if (action.type === 'renamePage') {
      const page = doc.pages.find(p => p.id === action.pageId); if (!page || typeof action.title !== 'string' || !action.title.trim() || action.title.length > 160) fail('请填写页面名称'); page.title = action.title;
    } else if (action.type === 'pageAppearance') {
      const page = doc.pages.find(p => p.id === action.pageId); if (!page) fail('页面不存在');
      page.appearance = copy(action.appearance); guiValidateAppearance(page.appearance);
    } else if (action.type === 'addNav') {
      const target = action.pageId ? { pageId: action.pageId } : { feature: action.feature }; doc.navigation.push({ id: fresh(doc, 'nav'), label: action.label || '新入口', ...target });
    } else if (action.type === 'removeNav') doc.navigation = doc.navigation.filter(n => n.id !== action.id);
    else if (action.type === 'renameNav') { const nav = doc.navigation.find(n => n.id === action.id); if (!nav) fail('导航不存在'); nav.label = action.label; }
    else if (action.type === 'targetNav') {
      const nav = doc.navigation.find(n => n.id === action.id); if (!nav) fail('导航不存在');
      const target = action.target || {};
      if ((target.pageId !== undefined) === (target.feature !== undefined)) fail('请选择一个导航目标');
      if (target.feature !== undefined && !['dsh.conversation', 'h2b.directChat', 'h2b.contacts', 'h2b.workflow', 'h2b.kanban', 'h2b.routine', 'h2b.operations'].includes(target.feature)) fail('导航功能不存在');
      delete nav.pageId; delete nav.feature;
      if (target.pageId !== undefined) nav.pageId = target.pageId; else nav.feature = target.feature;
    }
    else if (action.type === 'moveNav') {
      const index = doc.navigation.findIndex(n => n.id === action.id), next = index + action.direction;
      if (index < 0 || ![-1, 1].includes(action.direction)) fail('无效移动');
      if (next >= 0 && next < doc.navigation.length) [doc.navigation[index], doc.navigation[next]] = [doc.navigation[next], doc.navigation[index]];
    } else {
      const found = locate(doc, action.pageId, action.nodeId);
      if (action.type === 'add') insert(doc, found, makeNode(doc, action.nodeType, action.feature));
      else if (action.type === 'remove') detach(doc, found);
      else if (action.type === 'move') {
        const target = locate(doc, action.pageId, action.targetId); let cycle = false;
        walk(found.node, node => { if (node.id === target.node.id) cycle = true; }); if (cycle) fail('不能将组件移动到自己内部');
        const moving = found.node; let index = action.index;
        if (found.parent?.id === target.node.id && Number.isInteger(index) && found.index < index) index--;
        detach(doc, found); insert(doc, locate(doc, action.pageId, action.targetId), moving, index);
      } else if (action.type === 'reorder') {
        if (!found.parent || ![-1, 1].includes(action.direction)) fail('该组件不能移动');
        const to = found.index + action.direction;
        if (to >= 0 && to < found.parent.children.length) {
          [found.parent.children[found.index], found.parent.children[to]] = [found.parent.children[to], found.parent.children[found.index]];
          if (found.parent.labels) [found.parent.labels[found.index], found.parent.labels[to]] = [found.parent.labels[to], found.parent.labels[found.index]];
        }
      } else if (action.type === 'duplicate') {
        if (!found.parent) fail('请选择分组内的组件进行复制');
        const duplicate = copy(found.node); walk(duplicate, node => { node.id = fresh(doc); if (node.instanceId) node.instanceId = fresh(doc, 'instance'); });
        insert(doc, locate(doc, action.pageId, found.parent.id), duplicate, found.index + 1);
      } else if (action.type === 'configure') {
        const values = copy(action.values), allowed = { Text: ['text'], Feature: ['feature', 'view', 'context'], Stack: ['gap'], Grid: ['gap', 'columns'], Split: ['gap', 'ratio'], Tabs: ['labels'] }[found.node.type];
        if (Object.keys(values).some(key => key !== 'appearance' && !allowed.includes(key))) fail('不支持的组件设置');
        if (found.node.type === 'Feature' && doc.schemaVersion === 2 && Object.keys(values).some(key => ['feature', 'view', 'context'].includes(key) && JSON.stringify(values[key]) !== JSON.stringify(found.node[key]))) {
          found.node.instanceId = fresh(doc, 'instance'); delete found.node.dataSource; delete found.node.actions;
        }
        Object.assign(found.node, values);
      } else if (action.type === 'type') {
        if (!containers.includes(action.nodeType) || !containers.includes(found.node.type)) fail('仅能转换分组布局');
        if (action.nodeType === 'Split' && found.node.children.length !== 2) fail('分栏需要恰好两个组件');
        const node = { type: action.nodeType, id: found.node.id, children: found.node.children, ...(found.node.appearance ? { appearance: found.node.appearance } : {}) };
        if (action.nodeType === 'Grid') node.columns = 2; if (action.nodeType === 'Split') node.ratio = 50; replace(found, node);
      } else fail('不支持的编辑操作');
    }
    return inspect(doc);
  }
  function history(document) { return { past: [], present: inspect(document), future: [] }; }
  function record(state, document) { const next = inspect(document); if (JSON.stringify(next) === JSON.stringify(state.present)) return state; return { past: [...state.past, state.present].slice(-30), present: next, future: [] }; }
  function undo(state) { if (!state.past.length) return state; return { past: state.past.slice(0, -1), present: state.past[state.past.length - 1], future: [state.present, ...state.future].slice(0, 30) }; }
  function redo(state) { if (!state.future.length) return state; return { past: [...state.past, state.present].slice(-30), present: state.future[0], future: state.future.slice(1) }; }
  return { apply, inspect, locate, walk, history, record, undo, redo };
}

// Shared confirmation UI: safe default focus, contained keyboard navigation, Escape cancellation.
function createGuiConfirmationDialog(React) {
  const h = React.createElement;
  return function GuiConfirmationDialog({ title, message, onConfirm, onCancel, confirmLabel = '确认删除' }) {
    const cancelRef = React.useRef(null);
    React.useEffect(() => {
      const previous = globalThis.document?.activeElement;
      cancelRef.current?.focus();
      return () => { if (previous?.isConnected) previous.focus(); };
    }, []);
    return h('div', { className: 'gui-editor-confirm-backdrop', onClick: event => event.stopPropagation() },
      h('section', { className: 'gui-editor-confirm', role: 'alertdialog', 'aria-modal': true, 'aria-label': title,
        onKeyDown: event => { event.stopPropagation(); if (event.key === 'Escape') { event.preventDefault(); onCancel(); } if (event.key === 'Tab') { const buttons = event.currentTarget.querySelectorAll('button'); const first = buttons[0], last = buttons[buttons.length - 1]; if (event.shiftKey && event.target === first) { event.preventDefault(); last.focus(); } else if (!event.shiftKey && event.target === last) { event.preventDefault(); first.focus(); } } } },
        h('h3', null, title), h('p', null, message),
        h('button', { type: 'button', ref: cancelRef, onClick: onCancel }, '取消'),
        h('button', { type: 'button', onClick: onConfirm }, confirmLabel)));
  };
}

function createGuiVisualEditor(React) {
  const h = React.createElement, helpers = guiEditorHelpers(), ConfirmationDialog = createGuiConfirmationDialog(React);
  const labels = { Stack: '纵向排列', Grid: '网格', Split: '左右分栏', Tabs: '标签页', Text: '文字', Feature: '功能模块' };
  function decodeSelection(value) {
    if (!value || value === '@workspace') return { kind: 'workspace' };
    const [scope, id] = value.split('/');
    if (scope === '@page') return { kind: 'page', pageId: id };
    if (scope === '@navigation') return { kind: 'navigation', id: id || undefined };
    return { kind: id ? 'node' : 'page', pageId: scope, id };
  }
  return function GuiVisualEditor({ document, onChange, selected, onSelect, catalog = [], inspectorExtras, onShowInspector, locateSelection }) {
    const [systemDark, setSystemDark] = React.useState(() => typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches || false);
    React.useEffect(() => { const media = typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)'); if (!media) return; const changed = () => setSystemDark(media.matches); media.addEventListener('change', changed); return () => media.removeEventListener('change', changed); }, []);
    const inspectorRef = React.useRef(null);
    const leftRef = React.useRef(null);
    const canvasRef = React.useRef(null);
    const [leftPanel, setLeftPanel] = React.useState('structure');
    const [mobilePanel, setMobilePanel] = React.useState('canvas');
    const [pageId, setPageId] = React.useState(document.pages[0].id);
    const [selection, setSelection] = React.useState(() => decodeSelection(selected));
    const [confirmation, setConfirmation] = React.useState(null);
    React.useEffect(() => { if (selected) setSelection(decodeSelection(selected)); }, [selected]);
    React.useEffect(() => { if (confirmation && confirmation.snapshot !== JSON.stringify(document)) { setConfirmation(null); setError('设计已更新，删除确认已取消。请重新选择对象。'); } }, [document, confirmation]);
    const [error, setError] = React.useState('');
    const [state, setState] = React.useState(() => helpers.history(document));
    const last = React.useRef(document);
    React.useEffect(() => {
      if (JSON.stringify(document) !== JSON.stringify(last.current)) {
        setState(previous => previous.present.id === document.id ? helpers.record(previous, document) : helpers.history(document));
        last.current = document;
      }
    }, [document]);
    const page = document.pages.find(p => p.id === (selection.pageId || pageId)) || document.pages[0];
    const nodeId = selection.kind === 'node' ? selection.id : page.layout.id;
    let current; try { current = helpers.locate(document, page.id, nodeId).node; } catch { current = page.layout; }
    const featureLabels = { 'dsh.conversation': 'Agent 会话', 'h2b.directChat': '直聊', 'h2b.contacts': '通讯录', 'h2b.workflow': '工作流', 'h2b.kanban': '任务看板', 'h2b.operations': '运行状态', 'h2b.routine': '定时任务' };
    const features = catalog.map(entry => { const item = typeof entry === 'string' ? { id: entry, views: ['default', 'launcher'] } : entry; return { ...item, label: item.label || featureLabels[item.id] || item.id }; });
    function select(id) { setSelection({ kind: 'node', pageId: page.id, id }); onSelect?.(page.id + '/' + id); }
    function selectScope(kind, id) { setSelection({ kind, ...(kind === 'page' ? { pageId: id || page.id } : {}), ...(kind === 'navigation' ? { id } : {}) }); if (kind === 'page') setPageId(id || page.id); onSelect?.(kind === 'workspace' ? '@workspace' : kind === 'page' ? '@page/' + (id || page.id) : '@navigation/' + (id || '')); }
    const selectedNav = document.navigation.find(nav => nav.id === selection.id);
    const nodeAncestors = [];
    function ancestry(node, trail = []) { if (node.id === current.id) nodeAncestors.push(...trail, node); else (node.children || []).forEach(child => ancestry(child, [...trail, node])); }
    if (selection.kind === 'node') ancestry(page.layout);
    const scopeLabel = { workspace: '工作空间', page: '页面 · ' + page.title, node: '组件 · ' + labels[current.type], navigation: '导航入口 · ' + (selectedNav?.label || '导航菜单') }[selection.kind];
    const selectionKey = [document.id, selection.kind, page.id, selection.id || ''].join('/');
    React.useEffect(() => {
      if (inspectorRef.current) inspectorRef.current.scrollTop = 0;
    }, [selectionKey]);
    React.useEffect(() => {
      const panel = leftRef.current;
      const active = panel?.querySelector('.gui-editor-tree [aria-pressed=true], .gui-editor-pages [aria-pressed=true]');
      if (active && panel) {
        const target = active.getBoundingClientRect(), bounds = panel.getBoundingClientRect();
        if (target.top < bounds.top + 48) panel.scrollTop += target.top - bounds.top - 48;
        else if (target.bottom > bounds.bottom) panel.scrollTop += target.bottom - bounds.bottom;
      }
    }, [selectionKey, leftPanel]);
    const located = React.useRef(0);
    React.useEffect(() => {
      if (!locateSelection || located.current === locateSelection) return;
      const requested = decodeSelection(selected);
      if (requested.kind !== selection.kind || requested.id !== selection.id || requested.pageId !== selection.pageId) return;
      located.current = locateSelection;
      setMobilePanel('canvas');
      const canvas = canvasRef.current;
      const target = selection.kind === 'node' ? Array.from(canvas?.querySelectorAll('[data-node-id]') || []).find(element => element.dataset.nodeId === current.id) : canvas?.querySelector('.gui-editor-frame-preview');
      if (target && canvas) canvas.scrollTop += target.getBoundingClientRect().top - canvas.getBoundingClientRect().top - 16;
      else if (canvas) canvas.scrollTop = 0;
    }, [locateSelection, selectionKey, selected]);
    function showInspector() { setMobilePanel('inspector'); onShowInspector?.(); }
    function publish(next) { last.current = next.present; setState(next); onChange(next.present); setError(''); }
    function act(action) {
      if (['remove', 'removePage', 'removeNav'].includes(action.type)) {
        const target = { pageId: page.id, nodeId: current.id, ...action };
        let title, impact;
        if (target.type === 'remove') {
          const node = helpers.locate(document, target.pageId, target.nodeId).node;
          if (node.id === page.layout.id) { setError('页面根布局不能删除，可调整排列方式或删除其子组件。'); return; }
          let count = -1; helpers.walk(node, () => count++);
          title = '删除组件：' + (node.type === 'Text' ? node.text.slice(0, 40) : node.type === 'Feature' ? features.find(f => f.id === node.feature)?.label || node.feature : labels[node.type]);
          impact = '将移除该组件' + (count ? '及其 ' + count + ' 个子组件' : '') + '。不会删除会话、任务等业务数据。';
        } else if (target.type === 'removePage') {
          const targetPage = document.pages.find(item => item.id === target.pageId);
          if (document.pages.length === 1) return;
          title = '删除页面：' + targetPage.title;
          impact = '将删除页面布局及 ' + document.navigation.filter(nav => nav.pageId === target.pageId).length + ' 个导航引用。发布并启用后，指向该页的启动页和收藏可能调整；业务数据不会删除。';
        } else {
          const nav = document.navigation.find(item => item.id === target.id); if (!nav) return;
          title = '移除导航入口：' + nav.label; impact = '只移除此入口，不删除目标页面或业务模块。';
        }
        setConfirmation({ action: target, snapshot: JSON.stringify(document), title, impact }); return;
      }
      try { const next = helpers.apply(document, { pageId: page.id, nodeId: current.id, ...action }); publish(helpers.record(state, next)); }
      catch (e) { setError(e.message); }
    }
    function control(label, callback, disabled = false) { return h('button', { type: 'button', disabled, onClick: callback }, label); }
    function configure(values) { act({ type: 'configure', values }); }
    function field(label, value, onValue, props = {}) { return h('label', { className: 'gui-editor-field' }, h('span', null, label), h('input', { 'aria-label': label, value, onChange: e => onValue(e.target.value), ...props })); }
    function drop(event, targetId) {
      event.preventDefault(); event.stopPropagation();
      try {
        const raw = event.dataTransfer.getData('application/x-dsh-gui-node');
        if (!raw || raw.length > 4096) return;
        const value = JSON.parse(raw);
        if (value.palette) act({ type: 'add', nodeId: targetId, nodeType: value.palette, feature: value.feature });
        else if (value.pageId === page.id && typeof value.id === 'string') act({ type: 'move', nodeId: value.id, targetId });
        else setError('请在同一页面内移动组件');
      } catch { setError('无法拖放此组件'); }
    }
    const canvasVariables = guiModuleAppearanceVariables(guiStyleVariables(document.theme || {}, document.theme?.mode === 'dark' || (!document.theme?.mode || document.theme.mode === 'system') && systemDark ? 'dark' : 'light'), page.appearance || {});
    function nodeCard(node, inherited = canvasVariables) {
      const localVariables = guiModuleAppearanceVariables(inherited, node.appearance || {});
      const active = selection.kind === 'node' && current.id === node.id;
      const style = node.type === 'Grid' ? { display: 'grid', gridTemplateColumns: 'repeat(' + (node.columns || 2) + ', minmax(0,1fr))' } : node.type === 'Split' ? { display: 'grid', gridTemplateColumns: (node.ratio || 50) + 'fr ' + (100 - (node.ratio || 50)) + 'fr' } : { display: 'flex', flexDirection: 'column' };
      style.gap = node.gap ?? 'var(--gui-style-spacing)';
      return h('div', { key: node.id, className: 'gui-editor-node' + (active ? ' is-selected' : ''), 'data-node-id': node.id, style: Object.assign({}, localVariables, guiAppearanceStyle(node.appearance || {})), draggable: true, tabIndex: 0, role: 'group', 'aria-label': labels[node.type] + ' ' + node.id,
        onClick: event => { event.stopPropagation(); select(node.id); },
        onDragStart: event => { event.stopPropagation(); event.dataTransfer.setData('application/x-dsh-gui-node', JSON.stringify({ pageId: page.id, id: node.id })); event.dataTransfer.effectAllowed = 'move'; },
        onDragOver: event => { event.preventDefault(); event.stopPropagation(); }, onDrop: event => drop(event, node.id),
        onKeyDown: event => {
          if (event.target !== event.currentTarget) return;
          if (event.key === 'Delete' || event.key === 'Backspace') { event.preventDefault(); act({ type: 'remove', nodeId: node.id }); }
          if (event.altKey && ['ArrowUp', 'ArrowDown'].includes(event.key)) { event.preventDefault(); act({ type: 'reorder', nodeId: node.id, direction: event.key === 'ArrowUp' ? -1 : 1 }); }
        }
      }, h('div', { className: 'gui-editor-node-title' }, labels[node.type]),
      node.type === 'Text' ? h('p', null, node.text) : node.type === 'Feature' ? h('div', { className: 'gui-editor-module' }, features.find(f => f.id === node.feature)?.label || node.feature, h('small', null, '编辑占位 · 点击仅选择组件，交互预览中使用完整功能')) : h('div', { className: 'gui-editor-children', style }, node.children.map(child => nodeCard(child, localVariables))));
    }
    function structure(node) {
      return h('li', { key: node.id }, h('button', { type: 'button', 'aria-pressed': selection.kind === 'node' && current.id === node.id, onClick: () => select(node.id) }, node.type === 'Feature' ? features.find(f => f.id === node.feature)?.label || '功能模块' : labels[node.type]), node.children ? h('ul', null, node.children.map(structure)) : null);
    }
    function paletteButton(type, label, feature) {
      return h('button', { type: 'button', key: feature || type, draggable: true, onDragStart: e => e.dataTransfer.setData('application/x-dsh-gui-node', JSON.stringify({ palette: type, feature })), onClick: () => act({ type: 'add', nodeType: type, feature }) }, label);
    }
    const navigationMode = document.layout?.navigation || 'left';
    const navigationLabel = { top: '顶部导航', left: '左侧导航', native: '原生三栏导航' }[navigationMode];
    const navigationDescription = { top: '顶部主导航 → 内容区；原生侧栏收起，可从工作空间菜单临时显示。', left: '左侧自定义主导航 → 功能列表 → 内容区；入口名称与顺序由下方菜单定义。', native: '一级应用 → 二级功能 → 三级列表：保留原生导航结构。自定义入口不会替换原生应用，已有设计菜单保留，切回顶部或左侧导航后继续使用。' }[navigationMode];
    const selectedFeature = features.find(f => f.id === current.feature);
    const views = selectedFeature?.views || ['default', 'launcher'];
    const viewLabel = value => ({ default: '完整功能', launcher: '打开入口', readOnly: '只读看板', list: '列表', summary: '概览', detail: '详情', runs: '运行记录', tasks: '任务列表' }[value] || value);

    function navigationInspector() { return h('details', { className: 'gui-editor-navigation', open: true }, h('summary', null, document.kind === 'shell' ? navigationMode === 'native' ? '已保留的自定义菜单（原生模式不使用）' : '主导航菜单' : '页面内导航'),
        h('p', null, document.kind === 'shell' ? navigationMode === 'native' ? '以下自定义入口在原生模式下不显示。切换为顶部或左侧导航后恢复使用；编辑或删除这些入口不会改动原生三级导航。' : '这是应用后使用的主导航。可修改入口名称、顺序与目标；位置在“工作空间设置”的“导航布局”中设置。移除入口不会删除对应业务模块，完整功能仍可从工作空间菜单打开。' : '这里只定义个人页面内部的导航，不替换当前工作空间的主导航。'),
        document.navigation.length ? null : h('p', null, '尚无导航入口，可从下方添加。'),
        document.navigation.filter(nav => selection.kind !== 'navigation' || !selectedNav || nav.id === selectedNav.id).map((nav) => h('div', { key: nav.id, className: 'gui-editor-nav-row' }, field('入口名称', nav.label, label => act({ type: 'renameNav', id: nav.id, label })), h('label', { className: 'gui-editor-field' }, '打开内容', h('select', { 'aria-label': '打开内容 · ' + nav.label, value: nav.pageId ? 'page/' + nav.pageId : 'feature/' + nav.feature, onChange: event => { const [kind, id] = event.target.value.split('/'); act({ type: 'targetNav', id: nav.id, target: kind === 'page' ? { pageId: id } : { feature: id } }); } }, h('optgroup', { label: '功能模块' }, features.map(f => h('option', { key: f.id, value: 'feature/' + f.id }, f.label || f.id))), h('optgroup', { label: '自定义页面' }, document.pages.map(p => h('option', { key: p.id, value: 'page/' + p.id }, p.title + '（' + p.id + '）'))))), control('前移', () => act({ type: 'moveNav', id: nav.id, direction: -1 }), document.navigation.indexOf(nav) === 0), control('后移', () => act({ type: 'moveNav', id: nav.id, direction: 1 }), document.navigation.indexOf(nav) === document.navigation.length - 1), control('移除入口', () => act({ type: 'removeNav', id: nav.id })))),
        h('select', { 'aria-label': '添加导航入口', value: '', onChange: e => { const [kind, id] = e.target.value.split('/'); if (!id) return; const label = kind === 'page' ? document.pages.find(p => p.id === id).title : features.find(f => f.id === id)?.label || id; act({ type: 'addNav', ...(kind === 'page' ? { pageId: id } : { feature: id }), label }); } }, h('option', { value: '' }, '添加导航入口…'), document.pages.map(p => h('option', { key: p.id, value: 'page/' + p.id }, p.title)), features.map(f => h('option', { key: f.id, value: 'feature/' + f.id }, f.label || f.id)))); }
    function workspaceInspector() { return h(React.Fragment, null, document.kind === 'shell' ? h('section', { className: 'gui-editor-navigation-layout', 'aria-label': '主导航布局' },
        h('h3', null, '主导航布局'),
        h('label', { className: 'gui-editor-field' }, '导航布局', h('select', { 'aria-label': '导航布局', value: navigationMode, onChange: event => act({ type: 'navigationLayout', navigation: event.target.value }) }, ['top', 'left', 'native'].map(value => h('option', { key: value, value }, { top: '顶部导航', left: '左侧导航', native: '原生三栏导航' }[value])))),
        h('p', null, navigationDescription),
        h('label', { className: 'gui-editor-field' }, '业务对象列表', h('select', { 'aria-label': '业务对象列表', value: document.layout?.objects?.mode || 'inline', onChange: event => act({ type: 'objectLayout', objects: { mode: event.target.value } }) }, [['inline', '并排显示'], ['collapsible', '可折叠 · 默认收起'], ['drawer', '抽屉显示']].map(([value, label]) => h('option', { key: value, value }, label)))),
        field('对象列表宽度', document.layout?.objects?.width || 280, value => act({ type: 'objectLayout', objects: { width: Number(value) } }), { type: 'number', min: 200, max: 480 }),
        h('p', null, '隐藏全局导航后，会话、通讯录等业务页仍保留对象切换。窄屏自动使用抽屉；完整嵌入模块保留自身列表。')) : null, h('h3', null, '导航入口'), document.navigation.map(nav => control('编辑入口 · ' + nav.label, () => selectScope('navigation', nav.id))), control('添加或管理导航', () => selectScope('navigation')), inspectorExtras || null); }
    function nodeInspector() { return h(React.Fragment, null,
      h('div', { className: 'gui-editor-controls' }, control('上移', () => act({ type: 'reorder', direction: -1 })), control('下移', () => act({ type: 'reorder', direction: 1 })), control('复制', () => act({ type: 'duplicate' }), current.id === page.layout.id), h('details', null, h('summary', null, '更多操作'), control('删除组件', () => act({ type: 'remove' }), current.id === page.layout.id))),           current.type === 'Text' ? h('label', { className: 'gui-editor-field' }, '文字内容', h('textarea', { 'aria-label': '文字内容', value: current.text, maxLength: 4000, onChange: e => configure({ text: e.target.value }) })) : null,
          ['Stack', 'Grid', 'Split', 'Tabs'].includes(current.type) ? h('label', { className: 'gui-editor-field' }, '排列方式', h('select', { 'aria-label': '排列方式', value: current.type, onChange: e => act({ type: 'type', nodeType: e.target.value }) }, ['Stack', 'Grid', 'Split', 'Tabs'].map(type => h('option', { key: type, value: type }, labels[type])))) : null,
          ['Stack', 'Grid', 'Split'].includes(current.type) ? field('组件间距', current.gap ?? 12, value => configure({ gap: Number(value) }), { type: 'range', min: 0, max: 32 }) : null,
          current.type === 'Split' ? field('左侧宽度比例', current.ratio ?? 50, value => configure({ ratio: Number(value) }), { type: 'range', min: 20, max: 80 }) : null,
          current.type === 'Grid' ? field('网格列数', current.columns ?? 2, value => configure({ columns: Number(value) }), { type: 'number', min: 1, max: 4 }) : null,
          current.type === 'Tabs' ? current.children.map((child, index) => field('标签 ' + (index + 1), current.labels?.[index] || '标签 ' + (index + 1), value => configure({ labels: current.children.map((n, i) => i === index ? value : current.labels?.[i] || '标签 ' + (i + 1)) }), { key: child.id })) : null,
          current.type === 'Feature' ? h(React.Fragment, null,
            h('label', { className: 'gui-editor-field' }, '功能模块', h('select', { 'aria-label': '功能模块', value: current.feature, onChange: e => configure({ feature: e.target.value, view: 'launcher', ...(document.schemaVersion === 2 ? { context: {} } : {}) }) }, features.map(f => h('option', { key: f.id, value: f.id }, f.label || f.id)))),
            h('label', { className: 'gui-editor-field' }, '显示方式', h('select', { 'aria-label': '显示方式', value: current.view || 'default', onChange: e => configure({ view: e.target.value }) }, views.map(view => h('option', { key: view, value: view }, viewLabel(view))))),
            document.schemaVersion === 2 ? h('details', null, h('summary', null, '关联已有内容'), ['sessionId', 'workflowId', 'runId', 'taskId', 'targetUri'].map(key => field(({ sessionId: '会话标识', workflowId: '工作流标识', runId: '运行标识', taskId: '任务标识', targetUri: '联系人地址' })[key], current.context?.[key] || '', value => { const context = { ...current.context }; if (value) context[key] = value; else delete context[key]; configure({ context }); }, { key, maxLength: key === 'targetUri' ? 1024 : 256 }))) : null) : null,
          guiAppearanceControls(React, '选区外观', current.appearance || {}, appearance => configure({ appearance }))); }
    return h('section', { className: 'gui-visual-editor', 'data-mobile-panel': mobilePanel, 'data-selection-kind': selection.kind, 'aria-label': '可视化界面编辑器', onKeyDown: event => { if (event.defaultPrevented || confirmation || !['Delete', 'Backspace'].includes(event.key) || event.target.closest?.('input, textarea, select, [contenteditable=true]')) return; event.preventDefault(); event.stopPropagation(); if (selection.kind === 'node') act({ type: 'remove' }); else if (selection.kind === 'page') act({ type: 'removePage' }); else if (selection.kind === 'navigation' && selectedNav) act({ type: 'removeNav', id: selectedNav.id }); } },
      h('div', { className: 'gui-editor-toolbar' },
        control('撤销', () => publish(helpers.undo(state)), !state.past.length), control('重做', () => publish(helpers.redo(state)), !state.future.length),
        h('span', null, '当前选区：' + scopeLabel), control('查看选区属性', showInspector)),
      h('nav', { className: 'gui-editor-breadcrumbs', 'aria-label': '选区层级' }, control('工作空间', () => selectScope('workspace')), selection.kind === 'navigation' ? control('导航 · ' + (selectedNav?.label || '菜单'), () => selectScope('navigation', selection.id)) : selection.kind !== 'workspace' ? h(React.Fragment, null, control('页面 · ' + page.title, () => selectScope('page')), nodeAncestors.map(node => h('button', { type: 'button', key: node.id, onClick: () => select(node.id) }, labels[node.type] + ' · ' + node.id))) : null),
      error ? h('div', { role: 'alert', className: 'gui-editor-error' }, error) : null,
      h('nav', { className: 'gui-editor-panel-switch', 'aria-label': '编辑面板' }, [['left', '页面与添加'], ['canvas', '画布'], ['inspector', '属性']].map(([id, label]) => h('button', { type: 'button', key: id, 'aria-pressed': mobilePanel === id, onClick: () => { if (id === 'inspector') showInspector(); else setMobilePanel(id); } }, label))),
      h('div', { className: 'gui-editor-body' },
        h('aside', { ref: leftRef, className: 'gui-editor-left', 'aria-label': '页面与组件' },
          h('div', { className: 'gui-editor-left-tabs' }, [['structure', '页面与结构'], ['add', '添加']].map(([id, label]) => h('button', { type: 'button', key: id, 'aria-pressed': leftPanel === id, onClick: () => setLeftPanel(id) }, label))),
          h('div', { hidden: leftPanel !== 'structure' }, control('工作空间设置', () => selectScope('workspace')), control('导航菜单设置', () => selectScope('navigation')), h('ul', { className: 'gui-editor-tree', 'aria-label': '导航结构' }, document.navigation.map(nav => h('li', { key: nav.id }, h('button', { type: 'button', 'aria-pressed': selection.kind === 'navigation' && selection.id === nav.id, onClick: () => selectScope('navigation', nav.id) }, nav.label)))), h('strong', null, '页面'),
          h('div', { className: 'gui-editor-pages' }, document.pages.map(p => h('button', { type: 'button', key: p.id, 'aria-pressed': selection.kind === 'page' && page.id === p.id, onClick: () => selectScope('page', p.id) }, p.title)), control('添加页面', () => act({ type: 'addPage' }), document.kind === 'page' || document.pages.length >= 24)), h('strong', null, '当前页面结构'), h('ul', { className: 'gui-editor-tree', 'aria-label': '节点结构' }, structure(page.layout))),
          h('div', { className: 'gui-editor-palette', 'aria-label': '组件库', hidden: leftPanel !== 'add' }, h('p', null, '点击或拖入画布，添加到选中区域。'),
            h('section', { 'aria-label': '布局容器' }, h('h4', null, '布局 · 容器'), ['Stack', 'Grid', 'Split', 'Tabs'].map(type => paletteButton(type, labels[type]))),
            h('section', { 'aria-label': '业务模块' }, h('h4', null, '业务模块 · 完整功能'), features.map(feature => paletteButton('Feature', feature.label, feature.id))),
            h('section', { 'aria-label': '基础内容' }, h('h4', null, '基础内容'), paletteButton('Text', '文字')))),
        h('div', { ref: canvasRef, className: 'gui-editor-canvas', 'aria-label': '页面布局', onClick: () => selectScope('page'), style: Object.assign({}, canvasVariables, guiAppearanceStyle(page.appearance || {})) },
          document.kind === 'shell' ? h('div', { className: 'gui-editor-frame-preview', 'aria-label': '整体界面外框预览', onClick: event => { event.stopPropagation(); selectScope('workspace'); } },
            h('small', null, navigationLabel + ' · ' + navigationDescription),
            h('div', { className: 'gui-editor-frame-structure', 'data-navigation': navigationMode, 'aria-label': navigationLabel + '结构示意' },
              navigationMode === 'native' ? [
                h('div', { key: 'apps', className: 'gui-editor-frame-column' }, h('strong', null, '一级应用'), ['消息', '通讯录', '任务', '运维'].map(label => h('span', { key: label }, label))),
                h('div', { key: 'features', className: 'gui-editor-frame-column' }, h('strong', null, '二级功能'), h('span', null, 'Agent 会话'), h('span', null, 'Workflow'), h('span', null, '任务看板')),
                h('div', { key: 'items', className: 'gui-editor-frame-column' }, h('strong', null, '三级列表'), h('span', null, '会话 / 任务 / 联系人'), h('small', null, '按选中功能显示'))
              ] : [
                h('div', { key: 'navigation', className: 'gui-editor-frame-navigation' }, document.navigation.map(item => h('button', { type: 'button', key: item.id, 'aria-pressed': selection.kind === 'navigation' && selection.id === item.id, onClick: event => { event.stopPropagation(); selectScope('navigation', item.id); } }, item.label))),
                h('div', { key: 'content', className: 'gui-editor-frame-content' }, '内容区 · 使用下方页面布局')
              ])) : h('small', null, '个人页面 · 放入当前工作空间，保留其导航'),
          nodeCard(page.layout)),
        h('aside', { ref: inspectorRef, className: 'gui-editor-inspector', 'aria-label': '选区属性', 'data-selection-kind': selection.kind }, h('strong', null, scopeLabel),
          selection.kind === 'workspace' ? workspaceInspector() : selection.kind === 'navigation' ? navigationInspector() : selection.kind === 'page' ? h(React.Fragment, null, field('页面名称', page.title, title => act({ type: 'renamePage', title })), guiAppearanceControls(React, '页面外观', page.appearance || {}, appearance => act({ type: 'pageAppearance', appearance })), h('details', null, h('summary', null, '更多操作'), control('删除页面', () => act({ type: 'removePage' }), document.pages.length === 1))) : nodeInspector())),
      confirmation ? h(ConfirmationDialog, { title: confirmation.title, message: confirmation.impact + ' 此操作先修改草稿，确认后可撤销。', onCancel: () => setConfirmation(null), onConfirm: () => { if (confirmation.snapshot !== JSON.stringify(document)) { setConfirmation(null); setError('设计已更新，请重新选择对象。'); return; } try { const next = helpers.apply(document, confirmation.action); publish(helpers.record(state, next)); selectScope(confirmation.action.type === 'removePage' ? 'workspace' : confirmation.action.type === 'removeNav' ? 'navigation' : 'page'); setConfirmation(null); } catch (error) { setError(error.message); setConfirmation(null); } } }) : null);


  };
}

// Controls emit only the shared declarative style contract, never arbitrary CSS.
function guiStyleControl(React, label, value, change, options) {
  const h = React.createElement;
  return h('label', { className: 'gui-editor-field', key: label }, label,
    Array.isArray(options) ? h('select', { 'aria-label': label, value, onChange: event => change(event.target.value) }, options.map(([id, name]) => h('option', { key: id, value: id }, name))) :
      h('input', { 'aria-label': label, value, ...options, onChange: event => { if (!event.target.checkValidity()) return; change(options.type === 'number' || options.type === 'range' ? Number(event.target.value) : event.target.value); } }));
}
function guiAppearanceControls(React, title, appearance, onChange) {
  const h = React.createElement, set = (key, value) => onChange({ ...appearance, [key]: value });
  const field = (label, key, fallback, options) => guiStyleControl(React, title + ' · ' + label, appearance[key] ?? fallback, value => set(key, value), options);
  return h('details', { className: 'gui-editor-style-controls' }, h('summary', null, title),
    field('背景', 'surface', 'transparent', [['transparent', '透明'], ['base', '页面底色'], ['surface', '卡片底色'], ['primary', '主题主色']]),
    field('内边距', 'padding', 0, { type: 'range', min: 0, max: 32 }),
    field('圆角', 'radius', 0, { type: 'range', min: 0, max: 24 }),
    h('label', { className: 'gui-editor-check' }, h('input', { type: 'checkbox', checked: appearance.border || false, onChange: event => set('border', event.target.checked) }), title + ' · 显示边框'),
    field('阴影', 'shadow', 'none', [['none', '无'], ['soft', '轻柔'], ['medium', '明显']]),
    field('文字颜色', 'textTone', 'default', [['default', '正文色'], ['muted', '次要文字'], ['primary', '主题主色']]),
    field('字号', 'fontSize', 14, { type: 'number', min: 12, max: 40 }),
    guiStyleControl(React, title + ' · 字重', String(appearance.fontWeight || 400), value => set('fontWeight', Number(value)), [['400', '常规'], ['500', '适中'], ['600', '加粗'], ['700', '粗体']]),
    field('对齐', 'align', 'left', [['left', '左对齐'], ['center', '居中'], ['right', '右对齐']]),
    h('button', { type: 'button', onClick: () => onChange({}) }, '重置' + title));
}
function guiThemeControls(React, theme, onChange) {
  const h = React.createElement, set = (key, value) => onChange({ ...theme, [key]: value });
  const field = (label, key, fallback, options) => guiStyleControl(React, label, theme[key] ?? fallback, value => set(key, value), options);
  const typography = theme.typography || {};
  const fontField = (label, key, fallback, options) => guiStyleControl(React, label, typography[key] ?? fallback, value => set('typography', { ...typography, [key]: value }), options);
  return h('section', { className: 'gui-editor-style-controls', 'aria-label': '全局样式' },
    guiStyleControl(React, '风格预设', theme.preset || 'default', preset => { const next = { ...theme, preset }; delete next.colors; delete next.accent; onChange(next); }, [['default', '默认'], ['ocean', '海洋'], ['forest', '森林'], ['warm', '暖色'], ['mono', '黑白']]),
    field('主题', 'mode', 'system', [['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']]),
    field('强调色', 'accent', '#4263eb', { type: 'color' }),
    field('界面密度', 'density', 'comfortable', [['comfortable', '舒适'], ['compact', '紧凑']]),
    fontField('字体', 'font', 'system', [['system', '系统字体'], ['sans', '无衬线'], ['serif', '衬线'], ['mono', '等宽']]),
    fontField('全局字号', 'size', 14, { type: 'number', min: 12, max: 20 }),
    fontField('行高', 'lineHeight', 1.5, { type: 'number', min: 1.2, max: 2, step: 0.1 }),
    fontField('标题比例', 'headingScale', 1.3, { type: 'number', min: 1.1, max: 1.8, step: 0.1 }),
    field('全局间距', 'spacing', 12, { type: 'range', min: 0, max: 32 }),
    field('全局圆角', 'radius', 8, { type: 'range', min: 0, max: 24 }),
    field('边框宽度', 'borderWidth', 1, { type: 'range', min: 0, max: 3 }),
    field('全局阴影', 'shadow', 'none', [['none', '无'], ['soft', '轻柔'], ['medium', '明显']]),
    h('details', null, h('summary', null, '自定义配色'), ['light', 'dark'].map(mode => h('fieldset', { key: mode }, h('legend', null, mode === 'light' ? '浅色主题配色' : '深色主题配色'),
      [['background', '页面背景'], ['surface', '卡片背景'], ['text', '正文'], ['muted', '次要文字'], ['border', '边框'], ['primary', '主色'], ['onPrimary', '主色上的文字']].map(([key, label]) => guiStyleControl(React, (mode === 'light' ? '浅色' : '深色') + ' · ' + label, guiStyleVariables(theme, mode)['--gui-style-' + (key === 'onPrimary' ? 'on-primary' : key)], value => set('colors', { ...theme.colors, [mode]: { ...theme.colors?.[mode], [key]: value } }), { type: 'color' }))))),
    h('button', { type: 'button', onClick: () => onChange({}) }, '重置全局样式'));
}
