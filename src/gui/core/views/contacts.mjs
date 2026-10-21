// v11 contacts: the node's authoritative network directory, its configured or
// ensured sessions, and the optional native-owned local discovery list.
//
// Contacts are not an address book the browser may invent. Nodes and agents
// are exactly the rows `targets.list` returned (the daemon's `hosts` and
// `targets` answers); an agent click asks the node to create or reuse a stable
// session binding, and only then is the session selected. Sessions are exactly
// `sessions.list`. Status strings are shown as the daemon returned them; role
// and model are not part of the port, so the card states that. Search and the
// "current session" chip are local view operations and make no port calls.
import { button as makeButton, element as makeElement, focusNode, setHidden } from './dom.mjs';

const EMPTY_NO_BINDINGS = '节点尚未配置会话绑定';
const EMPTY_CLICK_AGENT = '尚未建立会话；点击上方 Agent 开始聊天';
const EMPTY_FILTERED = '没有匹配的会话';
const DIRECTORY_NOTE = '在线状态来自本机 daemon 返回的 status；角色与模型暂未提供。';

function initial(value) {
  const text = typeof value === 'string' && value.trim() ? value.trim() : '?';
  return Array.from(text)[0] ?? '?';
}

function matches(row, query) {
  if (!query) return true;
  const text = `${row.title ?? ''} ${row.sessionId ?? ''} ${row.targetUri ?? ''}`.toLowerCase();
  return text.includes(query);
}

function matchesAgent(row, query) {
  if (!query) return true;
  return `${row.targetUri ?? ''} ${row.status ?? ''} ${row.hostedBy ?? ''}`.toLowerCase().includes(query);
}

// The daemon's own status vocabulary, translated for display only. Any other
// string is shown verbatim: the view never upgrades it to "online".
function statusText(status) {
  if (status === 'online') return '在线';
  if (status === 'offline') return '离线';
  return typeof status === 'string' && status ? `状态：${status}` : '状态：未知';
}

function directorySummary(targets, phase) {
  if (phase !== 'connected') return '未连接，无法读取网络目录。';
  if (phase === 'connected' && targets.phase === 'idle') return '正在准备网络目录…';
  if (targets.phase === 'loading') return '正在读取网络目录…';
  if (targets.phase === 'error') {
    const code = targets.error?.code ?? 'CORE_ERROR';
    const message = targets.error?.message;
    return `网络目录读取失败：${code}${message ? ` · ${message}` : ''}`;
  }
  if (targets.phase !== 'ready') return '网络目录尚未读取。';
  const counts = targets.counts ?? { nodes: 0, targets: 0 };
  const parts = [];
  if (!targets.nodes.length) parts.push('未发现在线节点');
  if (!targets.agents.length) parts.push('未发现可用 Agent');
  if (!parts.length) parts.push(`节点 ${counts.nodes ?? targets.nodes.length} · Agent ${counts.targets ?? targets.agents.length}`);
  if (targets.truncated) parts.push('仅显示前 200 项');
  return parts.join('；');
}

export function createContactsView({
  document,
  onOpen = () => {},
  onConnectAgent = () => {},
  onRefreshTargets = () => {},
} = {}) {
  const element = (tagName, className = '', text = '') => makeElement(document, tagName, className, text);
  const button = (className, text) => makeButton(document, className, text);
  const root = element('section', 'core-contacts core-page');
  root.setAttribute('data-page', 'contacts');
  root.setAttribute('aria-label', '通讯录');

  const heading = element('h2', 'core-page-title', '通讯录');
  heading.setAttribute('tabindex', '-1');
  const subtitle = element('p', 'core-page-subtitle', '节点与 Agent 来自本机 daemon 的权威目录；点击 Agent 即可建立或打开会话。');
  const createButton = button('core-contacts-create', '创建 Agent');
  createButton.disabled = true;
  createButton.setAttribute('aria-disabled', 'true');
  createButton.setAttribute('title', '创建 Agent 仍请在本机管理窗口操作；此处可直接点击网络 Agent 建立会话');
  const head = element('header', 'core-contacts-head');
  head.replaceChildren(heading, subtitle, createButton);

  const searchLabel = element('label', 'core-search');
  const searchText = element('span', 'core-search-label', '搜索会话或 Agent');
  const search = element('input', 'core-search-input');
  search.setAttribute('type', 'search');
  search.setAttribute('placeholder', '按名称、会话 ID、Agent 或状态搜索');
  search.setAttribute('aria-label', '搜索会话或 Agent');
  searchLabel.replaceChildren(searchText, search);

  const filters = element('div', 'core-filters');
  filters.setAttribute('role', 'group');
  filters.setAttribute('aria-label', '会话筛选');
  const filterButtons = new Map();
  for (const [id, text] of [['all', '全部'], ['current', '当前会话']]) {
    const node = button('core-filter', text);
    node.setAttribute('data-filter', id);
    node.setAttribute('aria-pressed', id === 'all' ? 'true' : 'false');
    node.addEventListener('click', () => {
      filter = id;
      paint();
    });
    filterButtons.set(id, node);
    filters.appendChild(node);
  }
  const unavailableFilter = button('core-filter core-filter-disabled', '可用状态');
  unavailableFilter.disabled = true;
  unavailableFilter.setAttribute('aria-disabled', 'true');
  unavailableFilter.setAttribute('title', '当前目录没有可用的状态筛选');
  filters.appendChild(unavailableFilter);

  const clear = button('core-search-clear', '清除');
  clear.setAttribute('aria-label', '清除搜索与筛选');
  const tools = element('div', 'core-contacts-tools');
  tools.replaceChildren(searchLabel, filters, clear);

  const directory = element('section', 'core-directory');
  directory.setAttribute('aria-label', '网络节点与 Agent');
  const directoryHead = element('div', 'core-directory-head');
  const directoryTitle = element('h3', 'core-directory-title', '网络节点与 Agent');
  const directoryRefresh = button('core-directory-refresh', '刷新目录');
  directoryRefresh.addEventListener('click', () => onRefreshTargets());
  directoryHead.replaceChildren(directoryTitle, directoryRefresh);
  const directoryStatus = element('p', 'core-directory-status');
  directoryStatus.setAttribute('role', 'status');
  const nodesTitle = element('h4', 'core-directory-subtitle', '节点');
  const nodesList = element('ul', 'core-directory-list core-directory-nodes');
  const agentsTitle = element('h4', 'core-directory-subtitle', 'Agent');
  const agentsList = element('ul', 'core-directory-list core-directory-agents');
  directory.replaceChildren(directoryHead, directoryStatus, nodesTitle, nodesList, agentsTitle, agentsList);

  const sessionsTitle = element('h3', 'core-contacts-section-title', '会话');
  const grid = element('div', 'core-contact-grid');
  grid.setAttribute('aria-label', '会话联系人');
  const empty = element('p', 'core-empty');
  const note = element('p', 'core-contacts-note', DIRECTORY_NOTE);
  const discovery = element('div', 'core-contacts-discovery');
  root.replaceChildren(head, tools, directory, sessionsTitle, empty, grid, note, discovery);

  let query = '';
  let filter = 'all';
  let latest = {
    sessions: [],
    selectedSessionId: null,
    phase: 'disconnected',
    targets: { phase: 'idle', nodes: [], agents: [], truncated: false, counts: { nodes: 0, targets: 0 }, error: null },
  };

  function resetView() {
    query = '';
    filter = 'all';
    search.value = '';
    paint();
    focusNode(search);
  }

  function visibleRows() {
    return (latest.sessions ?? []).filter(row => {
      if (filter === 'current' && row.sessionId !== latest.selectedSessionId) return false;
      return matches(row, query);
    });
  }

  function visibleAgents() {
    return (latest.targets?.agents ?? []).filter(row => matchesAgent(row, query));
  }

  function card(row) {
    const node = button('core-contact-card', '');
    node.setAttribute('data-session-id', row.sessionId);
    if (row.targetUri) node.setAttribute('data-target-uri', row.targetUri);
    if (row.simulation === true) node.setAttribute('data-simulation', 'true');
    node.setAttribute('aria-label', '打开会话');
    const avatar = element('span', 'core-contact-avatar', initial(row.title || row.sessionId));
    avatar.setAttribute('aria-hidden', 'true');
    const body = element('span', 'core-contact-body');
    const name = element('strong', 'core-contact-name', row.title || row.sessionId);
    const meta = element('span', 'core-contact-meta', `会话 ${row.sessionId}`);
    const role = element('span', 'core-contact-role', '角色：接口未提供');
    const state = element('span', 'core-contact-state', typeof row.state === 'string' && row.state
      ? `会话状态：${row.state}`
      : (row.targetUri ? `Agent：${row.targetUri}` : '在线状态：接口未提供'));
    const badges = element('span', 'core-contact-badges');
    if (row.sessionId === latest.selectedSessionId) badges.appendChild(element('span', 'core-badge', '当前会话'));
    if (row.simulation === true) badges.appendChild(element('span', 'core-badge core-badge-demo', '模拟'));
    body.replaceChildren(name, meta, role, state, badges);
    node.replaceChildren(avatar, body);
    node.addEventListener('click', () => onOpen(row.sessionId));
    return node;
  }

  function nodeRow(row) {
    const item = element('li', 'core-directory-row core-directory-node');
    item.setAttribute('data-node-id', row.nodeId);
    const name = element('span', 'core-directory-name', row.nodeId);
    const state = element('span', 'core-directory-state', statusText(row.status));
    item.replaceChildren(name, state);
    return item;
  }

  function agentRow(row) {
    const known = (latest.sessions ?? []).some(session => session.targetUri === row.targetUri);
    const item = button('core-directory-row core-directory-agent', '');
    item.setAttribute('data-target-uri', row.targetUri);
    item.setAttribute('aria-label', `${known ? '打开' : '建立'}与 ${row.targetUri} 的会话`);
    item.disabled = latest.targets?.phase !== 'ready';
    const name = element('span', 'core-directory-name', row.targetUri);
    const meta = element('span', 'core-directory-meta');
    const state = element('span', 'core-directory-state', statusText(row.status));
    meta.appendChild(state);
    if (typeof row.hostedBy === 'string' && row.hostedBy) {
      meta.appendChild(element('span', 'core-directory-hosted', `宿主机 ${row.hostedBy}`));
    }
    item.replaceChildren(name, meta);
    item.addEventListener('click', () => {
      if (latest.targets?.phase === 'ready') onConnectAgent(row.targetUri);
    });
    return item;
  }

  function paint() {
    const sessions = latest.sessions ?? [];
    const targets = latest.targets ?? {};
    const agents = visibleAgents();
    const rows = visibleRows();
    directoryStatus.textContent = directorySummary(targets, latest.phase);
    directoryRefresh.disabled = latest.phase !== 'connected' || targets.phase === 'loading';
    nodesList.replaceChildren(...(targets.nodes ?? []).map(nodeRow));
    agentsList.replaceChildren(...agents.map(agentRow));
    empty.textContent = !sessions.length
      ? (latest.phase === 'connected'
        ? ((targets.agents ?? []).length ? EMPTY_CLICK_AGENT : EMPTY_NO_BINDINGS)
        : '未连接，暂无会话')
      : rows.length ? '' : EMPTY_FILTERED;
    grid.replaceChildren(...rows.map(card));
    for (const [id, node] of filterButtons) node.setAttribute('aria-pressed', id === filter ? 'true' : 'false');
    setHidden(clear, !query && filter === 'all');
    search.disabled = !sessions.length && !(targets.agents ?? []).length;
  }

  function render(state) {
    latest = {
      sessions: state?.sessions ?? [],
      selectedSessionId: state?.selectedSessionId ?? null,
      phase: state?.phase ?? 'disconnected',
      targets: state?.targets ?? {
        phase: 'idle', nodes: [], agents: [], truncated: false,
        counts: { nodes: 0, targets: 0 }, error: null,
      },
    };
    paint();
  }

  search.addEventListener('input', () => {
    query = String(search.value ?? '').trim().toLowerCase();
    paint();
  });
  search.addEventListener('keydown', event => {
    if (event.key !== 'Escape' || (!query && filter === 'all')) return;
    event.preventDefault?.();
    resetView();
  });
  clear.addEventListener('click', resetView);

  render(latest);
  return {
    element: root,
    discovery,
    elements: {
      root, heading, grid, empty, search, clear, createButton, filterButtons, note,
      directory, directoryStatus, directoryNodes: nodesList, directoryAgents: agentsList, directoryRefresh,
    },
    render,
    reset: resetView,
  };
}
