// Static browser entry for the App Core chat shell.
//
// What this file is allowed to trust: nothing about the user. A connection
// arrives from exactly two explicit places — a bridge object a native shell
// installed under one fixed global name, or the `?demo=1` simulation — and an
// injected bridge always wins, so a query parameter can never downgrade a real
// connection to simulated data. There is no implicit mock, no query-supplied
// principal, no credential in a URL, no arbitrary global lookup and no
// persistence: chat state stays in memory (see chat.mjs for the restart
// limitation on unresolved sends), and every value is written with
// textContent, so untrusted text can never become markup.
//
// Product text stays short and states the user-visible condition only; the
// machine-readable state lives in data attributes, and the internals are
// documented in README.md.
import { mountDiscovery } from './discovery.mjs';
import { createChatCore } from './chat.mjs';
import { assertKnownFields, assertNoIdentityFields, fail } from './connection.mjs';
import { createDemoConnection } from './demo.mjs';

/** The single global name a native shell may use to inject a connection. */
export const CORE_BRIDGE_GLOBAL = '__hyprialAppCore';
const BRIDGE_FIELDS = Object.freeze(['port', 'transport', 'powerPack']);

// Refused rather than ignored: a URL that carries these is trying to speak for
// the trusted side, and the identity decision does not belong to the browser.
const IDENTITY_QUERY_KEYS = Object.freeze([
  'principal', 'principalId', 'principal_id', 'principalID', 'user', 'username', 'trustedUser', 'trusted_user',
  'trustedPrincipal', 'trustedContext', 'identity', 'credentials', 'token', 'bearer', 'authorization',
  'authContext', 'authenticatedUser', 'sessionRef', 'actor',
]);

const PHASE_TEXT = Object.freeze({
  disconnected: '未连接',
  connecting: '连接中…',
  connected: '已连接',
  error: '连接失败',
});

const KIND_TEXT = Object.freeze({ 'local-ipc': '本机', tailnet: '私有网络', mock: '演示' });
const KIND_LABELS = Object.freeze({ user: '我', assistant: 'Agent', system: '系统', tool: '工具' });

export function mountChat({ document, root, core = null, reason = null, notice = null, localDiscovery } = {}) {
  if (!document || typeof document.createElement !== 'function') throw new TypeError('mountChat requires a document');
  const host = root ?? document.body;
  if (!host || typeof host.replaceChildren !== 'function') throw new TypeError('mountChat requires a root element');

  const element = (tagName, className, text = '') => {
    const node = document.createElement(tagName);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  };

  const title = element('h1', 'core-title', 'Hyprial 聊天');
  const status = element('p', 'core-status');
  const badge = element('p', 'core-demo-badge', '演示模式（Demo）· 仅模拟聊天');
  const routeLine = element('p', 'core-route');
  const powerPackLine = element('p', 'core-powerpack', '本地 Agent 管理：可用');
  const header = element('header', 'core-header');
  const noticeLine = element('p', 'core-notice');
  const error = element('p', 'core-error');
  const refreshButton = element('button', 'core-refresh', '刷新');
  const sessionHeading = element('h2', 'core-sessions-title', '会话');
  const sessions = element('div', 'core-sessions');
  const moreButton = element('button', 'core-more', '加载更多');
  const messageHeading = element('h2', 'core-messages-title', '消息');
  const messages = element('div', 'core-messages');
  const terminal = element('p', 'core-terminal');
  const input = element('textarea', 'core-input');
  const sendButton = element('button', 'core-send', '发送');
  const retryButton = element('button', 'core-retry', '重试同一条消息');
  const dismissButton = element('button', 'core-dismiss', '不再重试');
  const sendNotice = element('p', 'core-send-notice');
  const composer = element('div', 'core-composer');
  const conversation = element('section', 'core-conversation');
  const container = element('main', 'core-app');

  status.setAttribute('role', 'status');
  noticeLine.setAttribute('role', 'status');
  error.setAttribute('role', 'alert');
  sendNotice.setAttribute('role', 'status');
  input.setAttribute('aria-label', '消息内容');
  input.setAttribute('placeholder', '输入消息…');
  input.setAttribute('maxlength', '8000');
  input.setAttribute('rows', '3');
  for (const button of [refreshButton, sendButton, retryButton, dismissButton, moreButton]) button.setAttribute('type', 'button');

  composer.replaceChildren(input, sendButton, retryButton, dismissButton, sendNotice);
  conversation.replaceChildren(messageHeading, messages, terminal, composer);
  container.replaceChildren(header, noticeLine, error, refreshButton, sessionHeading, sessions, moreButton, conversation);
  host.replaceChildren(container);
  const discovery = mountDiscovery({ document, root: container, port: localDiscovery });

  let actionError = null;

  const actions = {
    connect: () => (core ? core.connect() : Promise.resolve(null)),
    refresh: () => {
      if (!core) return Promise.resolve(null);
      return core.refresh().then(state => {
        // A refresh that reached the node clears the failure it may have been
        // retrying: a reconnected UI must not keep showing the dead
        // connection's error beside "已连接" (the send action already does the
        // same).
        actionError = null;
        render();
        return state;
      });
    },
    select: sessionId => (core ? core.selectSession(sessionId) : Promise.resolve(null)),
    more: () => (core ? core.loadMoreHistory() : Promise.resolve(null)),
    send: () => {
      if (!core) return Promise.resolve(null);
      const sentText = input.value;
      return core.send(sentText).then(() => {
        if (core.state().lastSend?.state === 'accepted' && input.value === sentText) input.value = '';
        actionError = null;
        render();
      });
    },
    retry: () => (core ? core.retrySend() : Promise.resolve(null)),
    dismiss: () => {
      actionError = null;
      return core ? core.dismissUncertain() : Promise.resolve(null);
    },
  };

  // Every control is guarded: a synchronous or asynchronous rejection is shown
  // in the error line instead of escaping as an unhandled rejection.
  function noteFailure(error) {
    actionError = { code: error?.code ?? 'CORE_ERROR', message: error?.message ?? String(error) };
    render();
    return null;
  }

  const guard = run => () => {
    let result;
    try {
      result = run();
    } catch (error) {
      return Promise.resolve(noteFailure(error));
    }
    return Promise.resolve(result).catch(noteFailure);
  };

  function sessionRows(state) {
    const selected = state?.selectedSessionId ?? null;
    return (state?.sessions ?? []).map(row => {
      const button = element('button', 'core-session', row.title);
      button.setAttribute('type', 'button');
      button.setAttribute('data-session-id', row.sessionId);
      if (row.sessionId === selected) button.setAttribute('aria-current', 'page');
      if (row.simulation === true) button.setAttribute('data-simulation', 'true');
      button.addEventListener('click', guard(() => actions.select(row.sessionId)));
      return button;
    });
  }

  function messageRows(state) {
    return (state?.messages ?? []).map(message => {
      const article = element('article', `core-message core-message-${message.kind}`);
      article.setAttribute('data-event-id', message.eventId ?? '');
      if (message.simulation === true) article.setAttribute('data-simulation', 'true');
      const kind = element('strong', 'core-message-kind', KIND_LABELS[message.kind] ?? message.kind);
      const body = element('p', 'core-message-text', message.text);
      article.replaceChildren(kind, body);
      return article;
    });
  }

  function terminalText(state) {
    const value = state?.progress?.terminal ?? null;
    if (!value) return '';
    if (value.simulation === true) return '本轮为模拟结果，不代表真实完成';
    if (value.terminal === 'completed') return '本轮已完成';
    return `本轮未完成：${value.terminal}`;
  }

  function sendStateText(state) {
    const send = state?.lastSend ?? null;
    if (!send) return '';
    if (send.state === 'sending') return '发送中…';
    if (send.state === 'accepted') {
      return `已受理，等待回复${send.deduplicated ? '（重复请求）' : ''}${send.simulation ? '（模拟）' : ''}`;
    }
    if (send.state === 'uncertain') {
      return `发送结果未知，请重试同一条消息${send.error ? `（${send.error.code}）` : ''}`;
    }
    return `发送失败：${send.error?.message ?? '未知原因'}${send.error ? `（${send.error.code}）` : ''}`;
  }

  function render() {
    const state = core ? core.state() : null;
    const phase = state?.phase ?? 'disconnected';
    const kind = state?.transport?.kind ?? null;
    const disconnected = !state || phase !== 'connected';
    status.textContent = state
      ? `${PHASE_TEXT[phase] ?? phase}${state.phase === 'connected' && kind ? ` · ${KIND_TEXT[kind] ?? kind}` : ''}`
      : `未连接${reason ? ` · ${reason}` : ''}`;
    container.setAttribute('data-connection', phase);
    container.setAttribute('data-demo', state?.demo === true ? 'true' : 'false');
    header.replaceChildren(title, status, ...(state?.demo ? [badge] : []), ...(state?.powerPack?.available ? [powerPackLine] : []), routeLine);
    routeLine.textContent = `连接方式：${kind ? KIND_TEXT[kind] ?? kind : '无'}`;
    noticeLine.textContent = notice ?? '';
    // Set(): the same failure can be reported by both the core state and the
    // last guarded action, and it is stated once.
    error.textContent = [...new Set([
      reason,
      state?.lastError ? `错误（${state.lastError.code}）：${state.lastError.message}` : null,
      actionError ? `错误（${actionError.code}）：${actionError.message}` : null,
    ].filter(Boolean))].join('\n');
    sessions.replaceChildren(...sessionRows(state));
    messages.replaceChildren(...messageRows(state));
    terminal.textContent = terminalText(state);
    sendNotice.textContent = sendStateText(state);
    sendNotice.setAttribute('data-send-state', state?.lastSend?.state ?? 'idle');

    const send = state?.lastSend ?? null;
    sendButton.disabled = disconnected || !state.selectedSessionId || send?.state === 'sending' || send?.state === 'uncertain';
    input.disabled = disconnected;
    refreshButton.disabled = !state || phase === 'connecting';
    refreshButton.textContent = disconnected ? '重新连接' : '刷新';
    moreButton.disabled = !state?.history?.hasMore || state.history.loading === true;
    retryButton.disabled = disconnected || send?.state !== 'uncertain';
    dismissButton.disabled = send?.state !== 'uncertain';
  }

  sendButton.addEventListener('click', guard(actions.send));
  retryButton.addEventListener('click', guard(actions.retry));
  dismissButton.addEventListener('click', guard(actions.dismiss));
  refreshButton.addEventListener('click', guard(actions.refresh));
  moreButton.addEventListener('click', guard(actions.more));

  const unsubscribe = core ? core.subscribe(() => render()) : () => {};
  render();
  // Guarded too: even a synchronous throw from connect() is shown, never left
  // as an unhandled rejection.
  if (core) void guard(actions.connect)();

  return {
    core,
    elements: {
      container, status, badge, routeLine, powerPackLine, noticeLine, error, refreshButton,
      sessions, moreButton, messages, terminal, input, sendButton, retryButton, dismissButton, sendNotice,
    },
    actions,
    render,
    setNotice(text) {
      notice = text || null;
      render();
    },
    destroy() {
      unsubscribe();
      discovery.destroy();
      host.replaceChildren();
    },
  };
}

/**
 * Boot the static chat page.
 *
 * Only `?demo=1` selects the simulation, and only when no bridge is injected: a
 * global property is an injection point, not a trust boundary, but it is also
 * never silently replaced by fake data. Anything else without a bridge stays
 * visibly disconnected with a disabled composer.
 */
export function startBrowserChat({ document, location, globalObject = globalThis, root } = {}) {
  const mount = options => mountChat({ ...options, localDiscovery: globalObject?.__hyprialPowerPack });
  const params = new URLSearchParams(typeof location?.search === 'string' ? location.search : '');
  const supplied = IDENTITY_QUERY_KEYS.filter(key => params.has(key));
  if (supplied.length) {
    return mount({
      document, root, core: null,
      reason: `已拒绝启动：链接不能携带身份信息（${supplied.join(', ')}）`,
    });
  }
  const demoRequested = params.get('demo') === '1';
  const injected = globalObject ? globalObject[CORE_BRIDGE_GLOBAL] : undefined;
  if (injected !== undefined && injected !== null) {
    let core;
    try {
      if (typeof injected !== 'object' || Array.isArray(injected)) {
        fail('CORE_CONNECTION_REQUIRED', '注入的连接桥必须是对象');
      }
      assertNoIdentityFields(injected, 'The injected connection bridge');
      assertKnownFields(injected, BRIDGE_FIELDS, 'The injected connection bridge');
      core = createChatCore({ port: injected.port, transport: injected.transport, powerPack: injected.powerPack });
    } catch (error) {
      return mount({
        document, root, core: null,
        reason: `已拒绝注入的连接（${error?.code ?? 'CORE_ERROR'}）：${error?.message ?? String(error)}；不会改用演示数据`,
      });
    }
    return mount({
      document, root, core,
      notice: demoRequested ? '已忽略链接中的演示参数：注入的连接优先。' : null,
    });
  }
  if (!demoRequested) {
    return mount({
      document, root, core: null,
      reason: '本页没有可用的连接',
    });
  }
  return mount({ document, root, core: createChatCore({ demo: createDemoConnection() }) });
}
