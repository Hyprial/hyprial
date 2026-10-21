// v11 chat layout: session list, conversation, and an honest info panel.
//
// This module owns presentation and keyboard/pane behavior only. Sessions,
// history, replies, progress and send outcomes still come from the chat core
// (chat.mjs); the view never invents a title, a reply, a model or a role. The
// info panel states "接口未提供" for every field the session projection does not
// carry, and its management button is disabled rather than faked.
import { button as makeButton, element as makeElement, focusNode, setHidden } from './dom.mjs';

const KIND_LABELS = Object.freeze({ user: '我', assistant: 'Agent', system: '系统', tool: '工具' });
const EMPTY_NO_BINDINGS = '暂无会话：节点尚未配置会话绑定';
const EMPTY_SELECT = '选择一个会话查看真实消息';
const EMPTY_MESSAGES = '该会话暂无可读消息';
const EMPTY_LOADING = '正在加载消息…';
const HISTORY_ERROR_PREFIX = '读取消息失败';
const INFO_UNAVAILABLE = '角色、模型与在线状态：当前接口未提供';

function label(value, fallback) {
  const text = typeof value === 'string' ? value.trim() : '';
  return text || fallback;
}

function initial(value) {
  const text = label(value, '?');
  return Array.from(text)[0] ?? '?';
}

function preview(state) {
  const messages = state?.messages ?? [];
  const last = messages[messages.length - 1];
  return last && typeof last.text === 'string' ? last.text.slice(0, 120) : '';
}

export function createChatView({
  document,
  onSelect = () => {},
  onMore = () => {},
  onHistoryRetry = () => {},
  onSend = () => {},
  onRetry = () => {},
  onDismiss = () => {},
} = {}) {
  const element = (tagName, className = '', text = '') => makeElement(document, tagName, className, text);
  const button = (className, text) => makeButton(document, className, text);
  const root = element('section', 'core-chat core-page');
  root.setAttribute('data-page', 'chat');
  root.setAttribute('aria-label', '聊天');
  // Land on the session list: at phone width the thread pane hides the list,
  // and an empty thread on first open gives the user nothing to act on. On
  // desktop every column is visible, so the pane value only matters narrow.
  root.setAttribute('data-pane', 'list');

  // Left column: the node's configured sessions.
  const listHeading = element('h2', 'core-panel-title', '会话');
  listHeading.setAttribute('tabindex', '-1');
  const listEmpty = element('p', 'core-empty');
  const sessions = element('div', 'core-sessions');
  const listPanel = element('aside', 'core-chat-list');
  listPanel.setAttribute('aria-label', '会话列表');
  listPanel.replaceChildren(listHeading, listEmpty, sessions);

  // Center column: header, message log, composer.
  const backButton = button('core-back', '返回会话列表');
  const threadTitle = element('h2', 'core-thread-title', '未选择会话');
  threadTitle.setAttribute('tabindex', '-1');
  const threadMeta = element('p', 'core-thread-meta', '');
  const threadHeading = element('div', 'core-thread-heading');
  threadHeading.replaceChildren(threadTitle, threadMeta);
  const infoToggle = button('core-info-toggle', '信息');
  infoToggle.setAttribute('aria-controls', 'core-thread-info');
  infoToggle.setAttribute('aria-expanded', 'false');
  const threadHead = element('header', 'core-thread-head');
  threadHead.replaceChildren(backButton, threadHeading, infoToggle);

  const moreButton = button('core-more', '加载更多');
  const historyErrorLine = element('p', 'core-history-error');
  historyErrorLine.setAttribute('role', 'alert');
  const historyRetryButton = button('core-history-retry', '重试加载历史');
  const historyStatus = element('div', 'core-history-status');
  historyStatus.replaceChildren(historyErrorLine, historyRetryButton);
  setHidden(historyErrorLine, true);
  setHidden(historyRetryButton, true);
  const moreRow = element('div', 'core-more-row');
  moreRow.replaceChildren(moreButton, historyStatus);
  const messages = element('div', 'core-messages');
  messages.setAttribute('role', 'log');
  messages.setAttribute('aria-live', 'polite');
  messages.setAttribute('aria-label', '消息');
  const terminal = element('p', 'core-terminal');
  const messageScroll = element('div', 'core-messages-scroll');
  messageScroll.replaceChildren(moreRow, messages, terminal);

  const input = element('textarea', 'core-input');
  input.setAttribute('aria-label', '消息内容');
  input.setAttribute('placeholder', '输入消息…');
  input.setAttribute('maxlength', '8000');
  input.setAttribute('rows', '2');
  const sendButton = button('core-send', '发送');
  const retryButton = button('core-retry', '重试同一条消息');
  const dismissButton = button('core-dismiss', '不再重试');
  const sendNotice = element('p', 'core-send-notice');
  sendNotice.setAttribute('role', 'status');
  const composerHint = element('p', 'core-composer-hint', 'Ctrl/⌘ + Enter 发送');
  const composerRow = element('div', 'core-composer-row');
  composerRow.replaceChildren(input, sendButton);
  const composerActions = element('div', 'core-composer-actions');
  composerActions.replaceChildren(retryButton, dismissButton, sendNotice);
  const composer = element('div', 'core-composer');
  composer.replaceChildren(composerRow, composerActions, composerHint);

  const thread = element('section', 'core-thread');
  thread.setAttribute('aria-label', '当前会话');
  thread.replaceChildren(threadHead, messageScroll, composer);

  // Right column: only fields the port really carries, everything else named
  // as unavailable instead of guessed.
  const infoTitle = element('h2', 'core-info-title', '会话信息');
  infoTitle.setAttribute('tabindex', '-1');
  const infoName = element('p', 'core-info-name', '未选择会话');
  const infoRole = element('p', 'core-info-role', '角色：接口未提供');
  const infoState = element('p', 'core-info-state', '在线状态：接口未提供');
  const infoCount = element('p', 'core-info-count', '已加载 0 条消息');
  const infoModel = element('p', 'core-info-model', '模型服务：接口未提供');
  const infoNote = element('p', 'core-info-note', INFO_UNAVAILABLE);
  const manageButton = button('core-info-manage', '管理 Agent');
  manageButton.disabled = true;
  manageButton.setAttribute('aria-disabled', 'true');
  manageButton.setAttribute('title', '聊天接口不提供 Agent 管理操作；请在桌面本机管理窗口操作');
  const infoBack = button('core-back-info', '返回会话');
  const infoPanel = element('aside', 'core-chat-info');
  infoPanel.setAttribute('id', 'core-thread-info');
  infoPanel.setAttribute('aria-label', '会话信息');
  infoPanel.replaceChildren(infoTitle, infoName, infoRole, infoState, infoCount, infoModel, infoNote, manageButton, infoBack);

  root.replaceChildren(listPanel, thread, infoPanel);

  let pane = 'list';
  let latest = null;
  let lastSelected = null;
  let lastCount = 0;
  let followPending = false;

  // Keep the newest message in view without fighting a reader who scrolled up.
  // `nearBottom` is measured from the pre-render layout: once the DOM has been
  // replaced the old distance is already polluted by the new content, which is
  // why the previous version never followed a message sent while at the
  // bottom. A selection change waits until its history has actually arrived
  // (the selection patch carries an empty message list), so the first render
  // cannot consume the jump on an empty thread.
  function followLatest(state, { nearBottom = false } = {}) {
    const selected = state?.selectedSessionId ?? null;
    const count = (state?.messages ?? []).length;
    const loading = state?.history?.loading === true;
    const selectionChanged = selected !== lastSelected;
    const grew = count > lastCount;
    lastSelected = selected;
    lastCount = count;
    if (selectionChanged) followPending = true;
    if (!followPending && !grew) return;
    if (loading || count === 0) return;
    const node = messageScroll;
    if (typeof node.scrollHeight !== 'number' || typeof node.clientHeight !== 'number'
        || typeof node.scrollTop !== 'number') return;
    if (!followPending && !nearBottom) return;
    node.scrollTop = node.scrollHeight;
    followPending = false;
  }

  function setPane(next) {
    if (!['list', 'thread', 'info'].includes(next)) return;
    pane = next;
    root.setAttribute('data-pane', pane);
    infoToggle.setAttribute('aria-expanded', pane === 'info' ? 'true' : 'false');
    if (pane === 'list') focusNode(listHeading);
    else if (pane === 'info') focusNode(infoTitle);
    else focusNode(threadTitle);
  }

  function setThreadTitle(title, meta) {
    threadTitle.textContent = title;
    threadMeta.textContent = meta;
  }

  function sessionRows(state) {
    const selected = state?.selectedSessionId ?? null;
    return (state?.sessions ?? []).map(row => {
      const node = button('core-session', '');
      node.setAttribute('data-session-id', row.sessionId);
      if (row.sessionId === selected) node.setAttribute('aria-current', 'page');
      if (row.simulation === true) node.setAttribute('data-simulation', 'true');
      const avatar = element('span', 'core-avatar', initial(row.title || row.sessionId));
      avatar.setAttribute('aria-hidden', 'true');
      const text = element('span', 'core-session-text');
      const name = element('span', 'core-session-name', label(row.title, row.sessionId));
      const summaryText = row.sessionId === selected ? (preview(state) || '暂无消息') : '选择后加载真实消息';
      const summary = element('span', 'core-session-summary', summaryText);
      text.replaceChildren(name, summary);
      node.replaceChildren(avatar, text);
      node.addEventListener('click', () => {
        setPane('thread');
        return onSelect(row.sessionId);
      });
      return node;
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
      const marks = `${send.deduplicated ? '（重复请求）' : ''}${send.simulation ? '（模拟）' : ''}`;
      const reply = send.reply ?? { state: 'waiting' };
      if (reply.state === 'completed') return `本轮已完成${marks}`;
      if (reply.state === 'simulated') return '已收到模拟回复（仅模拟，不代表真实完成）';
      if (reply.state === 'failed') {
        return `本轮未完成：${reply.error?.message ?? '未知原因'}${reply.error ? `（${reply.error.code}）` : ''}；可刷新后重试`;
      }
      if (reply.state === 'replied') return `已收到回复${marks}`;
      if (reply.timedOut === true) return '已受理，本次等待未收到回复；可刷新查看，不会自动重发';
      return `已受理，等待回复${marks}`;
    }
    if (send.state === 'uncertain') {
      return `发送结果未知，请重试同一条消息${send.error ? `（${send.error.code}）` : ''}`;
    }
    return `发送失败：${send.error?.message ?? '未知原因'}${send.error ? `（${send.error.code}）` : ''}`;
  }

  function draftHasText(value) {
    return typeof value === 'string' && value.trim().length > 0;
  }

  // Read the live textarea value rather than trusting the last rendered
  // `disabled` flag: a programmatic value change or a keyboard submit after
  // typing must still be gated by the text that is really there.
  function canSendNow(state) {
    const phase = state?.phase ?? 'disconnected';
    const send = state?.lastSend ?? null;
    const blocked = send?.state === 'sending' || send?.state === 'uncertain';
    return phase === 'connected'
      && Boolean(state?.selectedSessionId)
      && !blocked
      && draftHasText(input.value);
  }

  function updateComposer(state) {
    const connected = (state?.phase ?? 'disconnected') === 'connected';
    const send = state?.lastSend ?? null;
    const uncertain = send?.state === 'uncertain';
    sendButton.disabled = !canSendNow(state);
    input.disabled = !connected;
    retryButton.disabled = !connected || !uncertain;
    dismissButton.disabled = !uncertain;
  }

  function render(state) {
    latest = state;
    const node = messageScroll;
    const measured = typeof node.scrollHeight === 'number' && typeof node.clientHeight === 'number'
      && typeof node.scrollTop === 'number';
    const nearBottom = measured && node.scrollHeight - node.scrollTop - node.clientHeight <= 80;
    const phase = state?.phase ?? 'disconnected';
    const connected = phase === 'connected';
    const selected = state?.selectedSessionId ?? null;
    const selectedRow = (state?.sessions ?? []).find(row => row.sessionId === selected) ?? null;

    sessions.replaceChildren(...sessionRows(state));
    listEmpty.textContent = (state?.sessions ?? []).length
      ? ''
      : connected ? EMPTY_NO_BINDINGS : '未连接，暂无会话';
    if (selectedRow) setThreadTitle(label(selectedRow.title, selectedRow.sessionId), selectedRow.sessionId);
    else setThreadTitle('未选择会话', connected ? EMPTY_SELECT : '未连接');

    const history = state?.history ?? {};
    const historyLoading = history.loading === true;
    const historyError = history.error ?? null;
    const validCursor = history.hasMore === true
      && typeof history.nextCursor === 'string' && history.nextCursor.length > 0;
    const rows = messageRows(state);
    if (rows.length) messages.replaceChildren(...rows);
    else {
      // Only a successful, settled history request may claim the conversation
      // is empty. A failed or still-pending request keeps its own label so the
      // page never reports "no messages" when it simply has not read them.
      let emptyText = '';
      if (!connected) emptyText = '未连接，不显示消息';
      else if (!selected) emptyText = EMPTY_SELECT;
      else if (historyLoading) emptyText = EMPTY_LOADING;
      else if (!historyError) emptyText = EMPTY_MESSAGES;
      if (emptyText) messages.replaceChildren(element('p', 'core-empty', emptyText));
      else messages.replaceChildren();
    }
    terminal.textContent = terminalText(state);
    sendNotice.textContent = sendStateText(state);
    sendNotice.setAttribute('data-send-state', state?.lastSend?.state ?? 'idle');

    infoName.textContent = selectedRow ? label(selectedRow.title, selectedRow.sessionId) : '未选择会话';
    infoRole.textContent = '角色：接口未提供';
    infoState.textContent = typeof selectedRow?.state === 'string' && selectedRow.state
      ? `会话状态：${selectedRow.state}`
      : '在线状态：接口未提供';
    infoCount.textContent = selected ? `已加载 ${(state?.messages ?? []).length} 条消息` : '已加载 0 条消息';
    infoModel.textContent = '模型服务：接口未提供';

    historyErrorLine.textContent = historyError
      ? `${HISTORY_ERROR_PREFIX}：${historyError.message ?? '未知原因'}（${historyError.code ?? 'CORE_HISTORY_ERROR'}）`
      : '';
    setHidden(historyErrorLine, !historyError);
    setHidden(historyRetryButton, !historyError);
    setHidden(moreButton, Boolean(historyError));
    historyRetryButton.disabled = !(connected && selected && historyError && !historyLoading);
    // Load-more requires a usable server cursor; a bare `hasMore` true with no
    // cursor cannot be requested, and a failed page needs an explicit retry.
    moreButton.disabled = !connected || !selected || !validCursor || historyLoading || Boolean(historyError);

    updateComposer(state);
    infoToggle.disabled = !selectedRow;
    followLatest(state, { nearBottom });
  }

  function requestSend() {
    if (!canSendNow(latest)) return undefined;
    return onSend();
  }

  sendButton.addEventListener('click', event => {
    event.preventDefault?.();
    return requestSend();
  });
  retryButton.addEventListener('click', () => onRetry());
  dismissButton.addEventListener('click', () => onDismiss());
  moreButton.addEventListener('click', () => onMore());
  historyRetryButton.addEventListener('click', () => {
    if (historyRetryButton.disabled) return undefined;
    return onHistoryRetry();
  });
  backButton.addEventListener('click', () => setPane('list'));
  infoBack.addEventListener('click', () => setPane('thread'));
  infoToggle.addEventListener('click', () => setPane(pane === 'info' ? 'thread' : 'info'));
  input.addEventListener('input', () => updateComposer(latest));
  input.addEventListener('keydown', event => {
    if (event.key !== 'Enter' || !(event.ctrlKey || event.metaKey)) return undefined;
    event.preventDefault?.();
    return requestSend();
  });
  root.addEventListener('keydown', event => {
    if (event.key !== 'Escape') return;
    if (pane === 'info') {
      event.preventDefault?.();
      setPane('thread');
    } else if (pane === 'thread') {
      event.preventDefault?.();
      setPane('list');
    }
  });

  render(latest);
  return {
    element: root,
    elements: {
      root, listPanel, thread, infoPanel, sessions, listEmpty, messages, messageScroll, moreButton,
      moreRow, historyStatus, historyErrorLine, historyRetryButton, terminal,
      input, sendButton, retryButton, dismissButton, sendNotice, threadTitle, threadMeta,
      infoName, infoRole, infoState, infoCount, infoModel, infoToggle, backButton, infoBack, manageButton,
    },
    render,
    setPane,
    getPane: () => pane,
  };
}
