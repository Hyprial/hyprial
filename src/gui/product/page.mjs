import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createWorkspaceRenderer } from '../client/index.mjs';

// Server-side page composition for the product shell. All identity and data
// arrive as arguments from the trusted composition root; the page never reads
// credentials, and React escaping owns every text interpolation.
const h = React.createElement;
const renderWorkspace = createWorkspaceRenderer();

function sessionRows(sessions) {
  return sessions.map(row => ({ ...row, id: row.sessionId ?? row.id, displayTitle: row.title ?? row.displayTitle ?? row.sessionId }));
}

function historyCard(event) {
  if (event.kind === 'tool' && event.toolCard) {
    return h('div', { key: event.eventId, className: 'gui-event gui-event-tool' },
      renderWorkspace(null, { previewBlock: event.toolCard }));
  }
  const label = { user: '用户', assistant: 'Agent', system: '系统' }[event.kind] ?? event.kind;
  return h('article', { key: event.eventId, className: `gui-event gui-event-${event.kind}` },
    h('strong', { className: 'gui-event-kind' }, label),
    h('p', null, String(event.text ?? '')));
}

function capabilityBadges(capabilities) {
  if (!capabilities) return null;
  const flags = [
    ['流式', capabilities.streaming], ['工具卡', capabilities.toolCards],
    ['附件', capabilities.attachments], ['子Agent', capabilities.subagents],
  ];
  return h('span', { className: 'gui-capabilities' },
    flags.map(([label, value]) => h('span', {
      key: label, className: 'gui-capability', 'data-supported': value === true ? 'true' : 'false',
    }, `${label}: ${value === true ? '支持' : typeof value === 'string' ? value : '不支持'}`)),
    Array.isArray(capabilities.unknown) && capabilities.unknown.length
      ? h('span', { className: 'gui-capability gui-capability-unknown', title: capabilities.unknown.join('、') }, `未确认 ${capabilities.unknown.length} 项`)
      : null);
}

/**
 * Render the workspace page. `data` is assembled by the composition root from
 * host port responses; `token` is the per-boot local credential that keeps
 * navigation links inside the authenticated origin.
 */
export function renderWorkspacePage({ sessions = [], currentSessionId = null, history = null, terminal = null, capabilities = null, draftCount = 0, token }) {
  const link = suffix => `/?token=${encodeURIComponent(token)}${suffix}`;
  const rows = sessionRows(sessions);
  const sessionNav = h('nav', { className: 'gui-session-nav', 'aria-label': '会话导航' },
    h('h2', null, '会话'),
    rows.map(row => h('a', {
      key: row.id, className: 'gui-session-link', href: link(`&session=${encodeURIComponent(row.id)}`),
      'data-session-id': row.id, 'aria-current': row.id === currentSessionId ? 'page' : undefined,
    }, row.displayTitle)),
    rows.length ? null : h('p', { className: 'gui-empty' }, '没有会话'));
  const conversation = !currentSessionId
    ? h('p', { className: 'gui-empty' }, '选择左侧会话查看内容。')
    : h(React.Fragment, null,
        (history ?? []).map(historyCard),
        terminal ? h('p', { className: 'gui-terminal', 'data-terminal': terminal.terminal }, `会话已${terminal.terminal === 'completed' ? '完成' : '结束'}。`) : null,
        h('form', { className: 'gui-composer', method: 'post', action: '/send', 'data-gui-composer': '1' },
          h('input', { type: 'hidden', name: 'token', value: token }),
          h('input', { type: 'hidden', name: 'sessionId', value: currentSessionId }),
          h('textarea', { name: 'text', required: true, maxLength: 8000, rows: 3, placeholder: '发送到当前会话…' }),
          h('button', { type: 'submit' }, '发送')));
  const shellDocument = {
    schemaVersion: 1, id: 'hyprial-product-shell', name: 'Hyprial', kind: 'shell',
    navigation: [{ id: 'sessions', label: '会话', pageId: 'sessions' }],
    pages: [{
      id: 'sessions', title: '会话',
      layout: {
        type: 'Split', id: 'root', ratio: 28, children: [
          { type: 'Feature', id: 'library', feature: 'agent.conversation', view: 'list' },
          { type: 'Feature', id: 'conversation', feature: 'agent.conversation', view: 'default' },
        ],
      },
    }],
  };
  const shell = renderWorkspace(shellDocument, {
    shellNavigationExternal: true,
    renderFeature: (feature, view) => (view === 'list' ? sessionNav : conversation),
  });
  const body = h('body', { 'data-hyprial-gui': '1' },
    h('header', { className: 'gui-header' },
      h('strong', null, 'Hyprial'),
      h('span', { className: 'gui-header-meta' }, `Studio 草稿 ${draftCount} 个`),
      capabilityBadges(capabilities)),
    h('div', { className: 'gui-body' }, shell),
    h('script', { defer: true, src: '/assets/client.js' }));
  return '<!doctype html>\n' + renderToStaticMarkup(
    h('html', { lang: 'zh' },
      h('head', null,
        h('meta', { charSet: 'utf-8' }),
        h('meta', { name: 'viewport', content: 'width=device-width, initial-scale=1' }),
        h('title', null, 'Hyprial'),
        h('link', { rel: 'stylesheet', href: '/assets/style.css' })),
      body));
}
