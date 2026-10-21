// Static browser entry for the App Core shell.
//
// What this file is allowed to trust: nothing about the user. A connection
// arrives from exactly one explicit place — a bridge object a native shell
// installed under one fixed global name. There is no query-selected
// simulation in production (createDemoConnection remains an explicit test
// fixture only), no implicit mock, no query-supplied principal, no credential
// in a URL, no arbitrary global lookup and no persistence: chat state stays in
// memory (see chat.mjs for the restart limitation on unresolved sends), and
// every value is written with textContent, so untrusted text can never become
// markup.
//
// Product text stays short and states the user-visible condition only; the
// machine-readable state lives in data attributes, and the internals are
// documented in README.md.
import { mountDiscovery } from './discovery.mjs';
import { createChatCore } from './chat.mjs';
import { assertKnownFields, assertNoIdentityFields, fail } from './connection.mjs';
import { createChatView } from './views/chat.mjs';
import { createContactsView } from './views/contacts.mjs';
import { button as makeButton, element as makeElement, focusNode, setHidden } from './views/dom.mjs';
import { createRail } from './views/rail.mjs';
import { bindViewport } from './views/viewport.mjs';

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
const PAGE_TEXT = Object.freeze({ chat: '聊天', contacts: '通讯录' });
const MOBILE_PHASES = Object.freeze(['disconnected', 'connecting', 'connected', 'error']);
const MOBILE_TRANSPORTS = Object.freeze(['direct-test', 'tailnet']);
const DIRECT_TEST_TEXT = '直连测试（未认证，非生产）';

/**
 * Event the mobile configuration panel publishes after every connection-state
 * change. The panel owns the native transport, so its status is authoritative
 * for the header: the injected bridge's static `tailnet` metadata must never
 * label a direct test as a private network, and a failed tailnet attempt must
 * clear a stale "connected" left in the Core state.
 */
export const MOBILE_NETWORK_STATUS_EVENT = 'hyprial:mobile-network-status';

function normalizeMobileStatus(detail) {
  if (!detail || typeof detail !== 'object') return null;
  const state = detail.connectionError === true ? 'error' : detail.state;
  if (!MOBILE_PHASES.includes(state)) return null;
  const transport = MOBILE_TRANSPORTS.includes(detail.transport) ? detail.transport : null;
  const unauthenticated = detail.unauthenticated === true || transport === 'direct-test';
  // Absent means "the producer did not say": only an explicit false may turn
  // on the first-run guide, and the guide is never inferred from an error.
  const configured = detail.configured === true ? true : detail.configured === false ? false : null;
  return { state, transport, unauthenticated, configured };
}

function mobilePhase(state, mobileStatus) {
  const corePhase = state?.phase ?? 'disconnected';
  if (!mobileStatus) return corePhase;
  if (mobileStatus.state === 'connected') {
    // The native transport is up, but only Core's own successful connection
    // proves the page can read history and send. Never show "已连接" while the
    // Core request path is still failing or has not completed.
    if (corePhase === 'connected') return 'connected';
    if (corePhase === 'error') return 'error';
    if (corePhase === 'connecting') return 'connecting';
    return 'disconnected';
  }
  return mobileStatus.state;
}

function mobileTransportText(mobileStatus) {
  if (!mobileStatus) return null;
  if (mobileStatus.unauthenticated) return DIRECT_TEST_TEXT;
  if (mobileStatus.transport === 'tailnet') return 'Tailnet';
  return null;
}

export function mountChat({ document, root, core = null, reason = null, notice = null, localDiscovery } = {}) {
  if (!document || typeof document.createElement !== 'function') throw new TypeError('mountChat requires a document');
  const host = root ?? document.body;
  if (!host || typeof host.replaceChildren !== 'function') throw new TypeError('mountChat requires a root element');

  const element = (tagName, className = '', text = '') => makeElement(document, tagName, className, text);
  const button = (className, text) => makeButton(document, className, text);

  const container = element('main', 'core-app');
  const brand = element('div', 'core-brand');
  brand.replaceChildren(
    element('strong', 'core-brand-name', 'Hyprial'),
    element('span', 'core-brand-sub', 'App Core'),
  );
  // Rail navigation keeps focus in the rail (a menu, not a link out): the page
  // switch must not steal focus or the next arrow key would leave the menu.
  const rail = createRail({ document, onNavigate: page => showPage(page, { focus: false }) });
  const routeLine = element('p', 'core-route');
  const powerPackLine = element('p', 'core-powerpack', '本地 Agent 管理：可用');
  setHidden(powerPackLine, true);
  const railFoot = element('div', 'core-rail-foot');
  railFoot.replaceChildren(routeLine, powerPackLine);
  const railAside = element('aside', 'core-rail');
  railAside.replaceChildren(brand, rail.element, railFoot);

  const pageTitle = element('h1', 'core-title', '聊天');
  const status = element('p', 'core-status');
  status.setAttribute('role', 'status');
  const badge = element('p', 'core-demo-badge', '演示模式（Demo）· 仅模拟聊天');
  const refreshButton = button('core-refresh', '刷新');
  const topbar = element('header', 'core-topbar');

  const noticeLine = element('p', 'core-notice');
  noticeLine.setAttribute('role', 'status');
  // Unconfigured first run: one actionable setup guide replaces the raw error
  // wall. The raw failures stay reachable, collapsed, under diagnostics.
  const setupGuide = element('section', 'core-setup-guide');
  setupGuide.replaceChildren(
    element('h2', 'core-setup-title', '先配置移动网络连接'),
    element('p', 'core-setup-body', '打开本机的移动网络配置，填写 Core 地址与端口，并选择「保存并连接」。模拟器请使用本平台提示的地址；真机不能照搬模拟器地址。'),
  );
  setHidden(setupGuide, true);
  const diagnosticsText = element('p', 'core-diagnostics-text');
  const diagnostics = element('details', 'core-diagnostics');
  diagnostics.replaceChildren(
    element('summary', 'core-diagnostics-summary', '诊断信息（配置前 Core 的错误）'),
    diagnosticsText,
  );
  setHidden(diagnostics, true);
  const error = element('p', 'core-error');
  error.setAttribute('role', 'alert');

  let actionError = null;
  let activePage = 'chat';
  let mobileStatus = null;
  // On the mobile shell the injected bridge always declares `tailnet`, even
  // when the panel is configured for a direct test. Until the panel publishes
  // its real status, withhold Core's transport label rather than showing a
  // private-network claim that has not been verified.
  const mobileShell = /Android|iPhone|iPad|iPod/i.test(document.defaultView?.navigator?.userAgent ?? '');

  const onMobileStatus = event => {
    const next = normalizeMobileStatus(event?.detail);
    if (!next) return;
    mobileStatus = next;
    render();
  };
  document.addEventListener?.(MOBILE_NETWORK_STATUS_EVENT, onMobileStatus);

  const guard = run => () => {
    let result;
    try {
      result = run();
    } catch (error) {
      return Promise.resolve(noteFailure(error));
    }
    return Promise.resolve(result).catch(noteFailure);
  };

  const chatView = createChatView({
    document,
    onSelect: sessionId => guard(() => actions.select(sessionId))(),
    onMore: () => guard(actions.more)(),
    onHistoryRetry: () => guard(actions.historyRetry)(),
    onSend: () => guard(actions.send)(),
    onRetry: () => guard(actions.retry)(),
    onDismiss: () => guard(actions.dismiss)(),
  });
  const contactsView = createContactsView({
    document,
    onOpen: sessionId => guard(async () => {
      await actions.select(sessionId);
      showPage('chat');
      chatView.setPane('thread');
    })(),
    onConnectAgent: targetUri => guard(async () => {
      // The node creates or reuses the binding; the browser only named the
      // target it was shown. A successful ensure selects the session.
      await actions.ensureSession(targetUri);
      showPage('chat');
      chatView.setPane('thread');
    })(),
    onRefreshTargets: () => guard(actions.refreshTargets)(),
  });
  const pageHost = element('div', 'core-pages');
  pageHost.replaceChildren(chatView.element, contactsView.element);

  const main = element('div', 'core-main');
  main.replaceChildren(topbar, setupGuide, diagnostics, noticeLine, error, pageHost);
  container.replaceChildren(railAside, main);
  host.replaceChildren(container);
  const discovery = mountDiscovery({ document, root: contactsView.discovery, port: localDiscovery });
  const releaseViewport = bindViewport({ document });

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
    ensureSession: targetUri => (core && typeof core.ensureSession === 'function'
      ? core.ensureSession(targetUri)
      : Promise.resolve(null)),
    refreshTargets: () => (core && typeof core.refreshTargets === 'function'
      ? core.refreshTargets()
      : Promise.resolve(null)),
    more: () => (core ? core.loadMoreHistory() : Promise.resolve(null)),
    historyRetry: () => (core && typeof core.retryHistory === 'function'
      ? core.retryHistory()
      : Promise.resolve(null)),
    send: () => {
      if (!core) return Promise.resolve(null);
      const input = chatView.elements.input;
      const sentText = input.value;
      // The view disables the button, but the action is part of the public
      // API and must enforce the same contract: no whitespace-only send and
      // no send while the visible connection state is not connected.
      if (!sentText.trim() || mobilePhase(core.state(), mobileStatus) !== 'connected') {
        return Promise.resolve(null);
      }
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
  function noteFailure(failure) {
    actionError = { code: failure?.code ?? 'CORE_ERROR', message: failure?.message ?? String(failure) };
    render();
    return null;
  }

  function showPage(page, { focus = true } = {}) {
    if (!Object.hasOwn(PAGE_TEXT, page)) return;
    activePage = page;
    rail.setActive(page);
    pageTitle.textContent = PAGE_TEXT[page];
    container.setAttribute('data-page', page);
    setHidden(chatView.element, page !== 'chat');
    setHidden(contactsView.element, page !== 'contacts');
    if (focus) focusNode(page === 'chat' ? chatView.elements.threadTitle : contactsView.elements.heading);
  }

  function render() {
    const state = core ? core.state() : null;
    const phase = mobilePhase(state, mobileStatus);
    const kind = state?.transport?.kind ?? null;
    const disconnected = !state || phase !== 'connected';
    // The mobile panel's transport is stated in every phase so a direct test
    // stays explicitly unauthenticated even while it is connecting or failed;
    // Core's own transport suffix is still only claimed when connected.
    const surfaceText = mobileStatus
      ? mobileTransportText(mobileStatus)
      : phase === 'connected' && kind && !mobileShell ? KIND_TEXT[kind] ?? kind : null;
    status.textContent = state
      ? `${PHASE_TEXT[phase] ?? phase}${surfaceText ? ` · ${surfaceText}` : ''}`
      : `未连接${reason ? ` · ${reason}` : ''}`;
    container.setAttribute('data-connection', phase);
    container.setAttribute('data-demo', state?.demo === true ? 'true' : 'false');
    container.setAttribute('data-page', activePage);
    topbar.replaceChildren(pageTitle, status, ...(state?.demo === true ? [badge] : []), refreshButton);
    const routeText = mobileStatus
      ? mobileStatus.unauthenticated ? DIRECT_TEST_TEXT : mobileStatus.transport === 'tailnet' ? 'Tailnet' : '无'
      : mobileShell ? '未确认' : kind ? KIND_TEXT[kind] ?? kind : '无';
    routeLine.textContent = `连接方式：${routeText}`;
    setHidden(powerPackLine, state?.powerPack?.available !== true);
    noticeLine.textContent = notice ?? '';
    // Set(): the same failure can be reported by both the core state and the
    // last guarded action, and it is stated once.
    const rawErrorText = [...new Set([
      reason,
      state?.lastError ? `错误（${state.lastError.code}）：${state.lastError.message}` : null,
      actionError ? `错误（${actionError.code}）：${actionError.message}` : null,
    ].filter(Boolean))].join('\n');
    // The panel owns "has this device been configured?". Only its explicit
    // false shows the guide; a configured failure is a real error, not
    // onboarding, and an older panel that says nothing changes no behavior.
    const firstRunSetup = mobileStatus?.configured === false;
    setHidden(setupGuide, !firstRunSetup);
    setHidden(diagnostics, !firstRunSetup);
    diagnosticsText.textContent = rawErrorText;
    error.textContent = firstRunSetup ? '' : rawErrorText;
    setHidden(error, firstRunSetup || rawErrorText.length === 0);
    // The panel's transport state can be stricter than Core's last snapshot
    // (for example a tailnet fail-closed attempt). Project the effective phase
    // into the views too, so the composer and empty states cannot look
    // connected when the header says the connection failed.
    const viewState = state && phase !== state.phase ? { ...state, phase } : state;
    chatView.render(viewState);
    contactsView.render(viewState);
    refreshButton.disabled = !state || phase === 'connecting';
    refreshButton.textContent = disconnected ? '重新连接' : '刷新';
  }

  refreshButton.addEventListener('click', guard(actions.refresh));

  const unsubscribe = core ? core.subscribe(() => render()) : () => {};
  showPage('chat', { focus: false });
  render();
  // Guarded too: even a synchronous throw from connect() is shown, never left
  // as an unhandled rejection.
  if (core) void guard(actions.connect)();

  return {
    core,
    elements: {
      container, status, badge, routeLine, powerPackLine, noticeLine, error, refreshButton,
      setupGuide, diagnostics, diagnosticsText,
      rail: rail.element,
      pages: { chat: chatView.element, contacts: contactsView.element },
      chat: chatView.elements,
      contacts: contactsView.elements,
      sessions: chatView.elements.sessions,
      moreButton: chatView.elements.moreButton,
      historyErrorLine: chatView.elements.historyErrorLine,
      historyRetryButton: chatView.elements.historyRetryButton,
      messages: chatView.elements.messages,
      terminal: chatView.elements.terminal,
      input: chatView.elements.input,
      sendButton: chatView.elements.sendButton,
      retryButton: chatView.elements.retryButton,
      dismissButton: chatView.elements.dismissButton,
      sendNotice: chatView.elements.sendNotice,
    },
    actions,
    showPage,
    render,
    setNotice(text) {
      notice = text || null;
      render();
    },
    destroy() {
      unsubscribe();
      document.removeEventListener?.(MOBILE_NETWORK_STATUS_EVENT, onMobileStatus);
      discovery.destroy();
      releaseViewport();
      host.replaceChildren();
    },
  };
}

/**
 * Boot the static chat page.
 *
 * Only an injected `__hyprialAppCore` bridge can produce a connected page.
 * `?demo=1` is no longer a production data source: it is stated as disabled
 * and the page stays visibly disconnected with a disabled composer. A bridge
 * (or a broken bridge) is never silently replaced by simulated data.
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
        reason: `已拒绝注入的连接（${error?.code ?? 'CORE_ERROR'}）：${error?.message ?? String(error)}；不会显示演示数据`,
      });
    }
    return mount({
      document, root, core,
      notice: demoRequested ? '链接中的演示参数已停用：只使用注入的真实连接。' : null,
    });
  }
  if (demoRequested) {
    return mount({
      document, root, core: null,
      reason: '演示模式已停用：本页只显示真实连接',
    });
  }
  return mount({
    document, root, core: null,
    reason: '本页没有可用的连接',
  });
}
