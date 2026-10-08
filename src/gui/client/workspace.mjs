import React from 'react';
import { guiAppearanceStyle, guiModuleAppearanceVariables, guiStyleAliases, guiStyleVariables } from '../presentation/index.mjs';

// Static ESM extraction of the D0 gui-workspace.inc.js closure. Its registry,
// compiler helpers, and React components remain private to this factory.
function createGuiFeatureRegistry(features) {
  const entries = new Map();
  for (const feature of features) {
    if (!feature || typeof feature.id !== 'string' || entries.has(feature.id)) throw new Error('Invalid or duplicate GUI feature');
    entries.set(feature.id, Object.freeze({ ...feature, views: Object.freeze([...(feature.views || ['default'])]), singleton: feature.singleton ?? false }));
  }
  return Object.freeze({ list: () => [...entries.values()], get: id => entries.get(id), supports: (id, view = 'default') => Boolean(entries.get(id)?.views.includes(view)) });
}
function guiWorkspacePage(document, pageId) {
  if (!document || !Array.isArray(document.pages) || !document.pages.length) throw new Error('GUI has no pages');
  return document.pages.find(page => page.id === pageId) || document.pages[0];
}
function guiWorkspaceNodeStyle(node) {
  const gap = Number.isInteger(node.gap) && node.gap >= 0 && node.gap <= 32 ? node.gap : 12;
  const base = { minWidth: 0, minHeight: 0, gap };
  if (node.type === 'Grid') {
    const columns = Number.isInteger(node.columns) && node.columns >= 1 && node.columns <= 4 ? node.columns : 2;
    return { ...base, ...(node.appearance ? guiAppearanceStyle(node.appearance) : {}), display: 'grid', gridTemplateColumns: `repeat(${columns}, minmax(0, 1fr))` };
  }
  if (node.type === 'Split') {
    const ratio = Number.isInteger(node.ratio) && node.ratio >= 20 && node.ratio <= 80 ? node.ratio : 50;
    return { ...base, ...(node.appearance ? guiAppearanceStyle(node.appearance) : {}), display: 'grid', gridTemplateColumns: `minmax(0, ${ratio}fr) minmax(0, ${100 - ratio}fr)` };
  }
  return { ...base, ...(node.appearance ? guiAppearanceStyle(node.appearance) : {}), display: 'flex', flexDirection: 'column' };
}

function createGuiWorkspaceRenderer(ReactRuntime) {
  const h = ReactRuntime.createElement;
  function Tabs({ node, renderNode, ...common }) {
    const [selected, setSelected] = ReactRuntime.useState(node.children[0]?.id);
    const active = node.children.some(child => child.id === selected) ? selected : node.children[0]?.id;
    const prefix = ReactRuntime.useId();
    return h('section', { ...common, className: 'hyprialgui-tabs', style: { ...guiWorkspaceNodeStyle(node), ...common.style } },
      h('div', { role: 'tablist', 'aria-label': '页面分组', style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
        node.children.map((child, index) => h('button', {
          key: child.id, type: 'button', role: 'tab', id: prefix + '-tab-' + index,
          'aria-selected': child.id === active, 'aria-controls': prefix + '-panel-' + index,
          tabIndex: child.id === active ? 0 : -1,
          onClick: () => setSelected(child.id),
          onKeyDown: event => {
            const offset = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
            if (!offset && event.key !== 'Home' && event.key !== 'End') return;
            event.preventDefault();
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? node.children.length - 1 : (index + offset + node.children.length) % node.children.length;
            setSelected(node.children[next].id);
            event.currentTarget.parentElement.children[next]?.focus();
          },
        }, node.labels?.[index] || '分组 ' + (index + 1)))),
      node.children.map((child, index) => h('div', {
        key: child.id, role: 'tabpanel', id: prefix + '-panel-' + index,
        'aria-labelledby': prefix + '-tab-' + index, hidden: child.id !== active,
        style: { minWidth: 0 },
      }, renderNode(child))));
  }
  return function GuiWorkspace({ document, pageId, registry, renderFeature, onNavigate, shellNavigationExternal = false }) {
    const page = guiWorkspacePage(document, pageId);
    const [mode, setMode] = ReactRuntime.useState(() => typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    ReactRuntime.useEffect(() => {
      const media = typeof window !== 'undefined' ? window.matchMedia?.('(prefers-color-scheme: dark)') : null;
      const change = () => setMode(media?.matches ? 'dark' : 'light');
      media?.addEventListener('change', change); return () => media?.removeEventListener('change', change);
    }, []);
    const pageAppearance = page.appearance && Object.keys(page.appearance).length ? page.appearance : null;
    let count = 0, hasAppearance = Boolean(pageAppearance);
    function checkComplexity(node, depth = 0) {
      if (!node || depth > 12 || ++count > 256) throw new Error('GUI layout exceeds supported complexity');
      if (node.appearance && Object.keys(node.appearance).length) hasAppearance = true;
      if (Array.isArray(node.children)) for (const child of node.children) checkComplexity(child, depth + 1);
    }
    checkComplexity(page.layout);
    const styled = hasAppearance || document.theme && ['preset', 'colors', 'typography', 'spacing', 'radius', 'borderWidth', 'shadow'].some(key => document.theme[key] !== undefined);
    const resolvedMode = !document.theme?.mode || document.theme.mode === 'system' ? mode : document.theme.mode;
    let variables = styled ? guiStyleVariables(document.theme || {}, resolvedMode) : {};
    if (pageAppearance) variables = guiModuleAppearanceVariables(variables, pageAppearance);
    const pageStyle = { ...variables, ...(styled ? guiStyleAliases(variables) : {}), ...(pageAppearance ? guiAppearanceStyle(pageAppearance) : {}) };
    function renderNode(node, inherited = variables) {
      const appearance = node.appearance && Object.keys(node.appearance).length ? node.appearance : null;
      const local = appearance ? guiModuleAppearanceVariables(inherited, appearance) : inherited;
      const common = { key: node.id, style: { ...(appearance ? { ...local, ...guiStyleAliases(local), ...guiAppearanceStyle(appearance) } : {}), ...(styled && node.gap === undefined && ['Stack', 'Grid', 'Split'].includes(node.type) ? { gap: 'var(--gui-style-spacing,12px)' } : {}) }, 'data-gui-node': node.id, 'data-gui-appearance': appearance ? JSON.stringify(appearance) : undefined, className: 'hyprialgui-node hyprialgui-node-' + node.type };
      if (node.type === 'Text') return h('p', common, String(node.text || ''));
      if (node.type === 'Feature') {
        if (!registry.supports(node.feature, node.view || 'default')) return h('div', { ...common, role: 'status' }, '此功能视图当前不可用');
        return h('section', common, renderFeature(node.feature, node.view || 'default', node.id, node));
      }
      if (!['Stack', 'Grid', 'Split', 'Tabs'].includes(node.type) || !Array.isArray(node.children)) throw new Error('Unsupported GUI layout node');
      const childRenderer = child => renderNode(child, local);
      if (node.type === 'Tabs') return h(Tabs, { ...common, node, renderNode: childRenderer });
      return h('div', { ...common, style: { ...guiWorkspaceNodeStyle(node), ...common.style } }, node.children.map(childRenderer));
    }
    return h('section', { className: 'hyprialgui-workspace', 'aria-label': document.name, 'data-gui-styled': styled ? 'true' : undefined, 'data-gui-theme': styled ? JSON.stringify(document.theme || {}) : undefined, 'data-gui-style-mode': resolvedMode, 'data-gui-appearance': pageAppearance ? JSON.stringify(pageAppearance) : undefined, style: pageStyle },
      document.kind === 'shell' && shellNavigationExternal ? null : h('nav', { 'aria-label': '自定义工作空间', style: { display: 'flex', gap: 8, flexWrap: 'wrap' } },
        (document.navigation || []).map(item => h('button', { key: item.id, type: 'button', 'aria-current': item.pageId === page.id ? 'page' : undefined, onClick: () => onNavigate?.(item) }, item.label))),
      document.kind === 'shell' ? null : h('h2', null, page.title), renderNode(page.layout));
  };
}

// Static ESM extraction of the D0 gui-business.inc.js session-list component.
function createGuiSessionLibrary(ReactRuntime, dependencies) {
  return function GuiSessionLibrary(props) {
    const [, update] = ReactRuntime.useState(0);
    const [query, setQuery] = ReactRuntime.useState('');
    const [error, setError] = ReactRuntime.useState('');
    ReactRuntime.useEffect(() => dependencies.subscribe(() => update(n => n + 1)), []);
    const snapshot = dependencies.read();
    const archived = new Set(snapshot.archived || []);
    const rows = snapshot.sessions.filter(row => !archived.has(row.id) && dependencies.eligible(row) && dependencies.direct(row.id) === Boolean(props.direct));
    const matched = rows.filter(row => !query || String(row.displayTitle || row.id).toLowerCase().includes(query.toLowerCase()));
    const h = ReactRuntime.createElement;
    return h('section', { className: 'gui-session-library', 'aria-label': props.direct ? '直聊会话列表' : 'Agent 会话列表' },
      h('header', null, h('strong', null, props.direct ? 'Hyprial 直聊' : 'Agent 会话'), h('span', null, rows.length + ' 个会话')),
      props.view === 'summary' ? h('p', null, '当前会话：' + (rows.find(row => row.id === snapshot.current)?.displayTitle || '未选择')) : null,
      h('label', null, '查找会话', h('input', { value: query, onChange: event => setQuery(event.target.value) })),
      error ? h('p', { role: 'alert' }, error) : null,
      matched.map(row => h('button', { key: row.id, 'aria-current': row.id === snapshot.current ? 'page' : undefined,
        onClick: () => { setError(''); Promise.resolve().then(() => props.onSelect ? props.onSelect(row.id) : dependencies.open(row.id)).catch(err => setError(err.message || String(err))); } }, row.displayTitle || row.id)),
      !matched.length ? h('p', null, '没有符合条件的会话') : null);
  };
}

class GuiBoundary extends React.Component {
  constructor(props) { super(props); this.state = { error: null }; }
  static getDerivedStateFromError(error) { return { error }; }
  render() { return this.state.error ? React.createElement('p', { role: 'alert' }, '渲染降级：' + this.state.error.message) : this.props.children; }
}

export function createWorkspaceRenderer() {
  const featureRegistry = createGuiFeatureRegistry([
    { id: 'agent.conversation', views: ['default', 'launcher', 'list', 'summary'] },
    { id: 'hyprial.kanban', views: ['default', 'launcher', 'readOnly', 'tasks', 'detail'] },
  ]);
  const GuiWorkspace = createGuiWorkspaceRenderer(React);
  function GuiConversationPreview({ block, onOpenDraft }) {
    if (!block || block.kind !== 'tool-result') return React.createElement('p', null, '正在准备 GUI 预览…');
    let value;
    try { value = JSON.parse((block.content || []).filter(part => typeof part.text === 'string').map(part => part.text).join('\n')); } catch { /* malformed result */ }
    const draft = value && value.draft;
    if (!draft || !draft.document) return React.createElement('p', null, 'GUI 预览不可用，请查看工具返回并修正设计。');
    return React.createElement('article', { className: 'gui-conversation-preview' },
      React.createElement('strong', null, draft.document.name + ' · v' + draft.revision),
      React.createElement('p', null, '本次设计快照 · 尚未应用'),
      React.createElement('details', null, React.createElement('summary', null, '查看界面设计'),
        React.createElement(GuiBoundary, null, React.createElement(GuiWorkspace, { document: draft.document, registry: featureRegistry, renderFeature: feature => React.createElement('div', { className: 'gui-feature-reference' }, String(feature)) }))),
      React.createElement('button', { type: 'button', onClick: () => onOpenDraft?.(draft.id) }, '打开当前草稿'));
  }

  return function renderWorkspace(document, options = {}) {
    if (options.previewBlock) return React.createElement(GuiConversationPreview, { block: options.previewBlock, onOpenDraft: options.onOpenDraft });
    if (options.sessionSnapshot) {
      const dependencies = { read: () => options.sessionSnapshot, subscribe: () => () => {}, eligible: row => options.eligible?.(row) ?? true,
        direct: id => options.directFor?.(id) ?? false, open: id => options.onSelect?.(id) };
      const Library = createGuiSessionLibrary(React, dependencies);
      return React.createElement(Library, { direct: options.direct, view: options.view, onSelect: options.onSelect });
    }
    const registry = options.features ? createGuiFeatureRegistry(options.features) : featureRegistry;
    return React.createElement(GuiWorkspace, { ...options, document, registry, renderFeature: options.renderFeature || (feature => feature) });
  };
}
