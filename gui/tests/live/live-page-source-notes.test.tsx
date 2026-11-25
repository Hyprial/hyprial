import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { LiveChatPage, type ChatPageRuntime } from '../../src/pages/live/LiveChatPage';

const SOURCE_NOTES = { loading: '正在加载隔离聊天会话…', ready: '以下为隔离契约会话数据。' };
const ISOLATED_LABEL = '隔离契约 · Agent chat host · 不连接真实服务';

function stubChat(): ChatPageRuntime['chat'] {
  const snapshot = { status: 'ready' as const, targets: [], activeTargetId: null, drafts: {}, entries: {} };
  return { subscribe: () => () => {}, getSnapshot: () => snapshot } as unknown as ChatPageRuntime['chat'];
}

function renderPage(status: 'loading' | 'ready', withNotes: boolean): string {
  const { container } = render(
    <LiveChatPage
      live={{
        status,
        chat: stubChat(),
        ...(withNotes ? { sourceLabel: ISOLATED_LABEL, sourceNote: SOURCE_NOTES } : {}),
      }}
    />,
  );
  const text = container.textContent ?? '';
  return text;
}

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('live chat page status copy is source input driven', () => {
  it('renders the injected loading note in production mode', () => {
    vi.stubEnv('DEV', false);
    renderPage('loading', true);
    expect(screen.getByText('正在加载隔离聊天会话…')).toBeInTheDocument();
  });

  it('renders the injected ready note in production mode', () => {
    vi.stubEnv('DEV', false);
    renderPage('ready', true);
    expect(screen.getByText('以下为隔离契约会话数据。')).toBeInTheDocument();
  });

  it('keeps the real-service copy when no source note is injected in production mode', () => {
    vi.stubEnv('DEV', false);
    renderPage('loading', false);
    expect(screen.getByText('正在连接聊天服务…')).toBeInTheDocument();
    expect(screen.queryByText(SOURCE_NOTES.loading)).not.toBeInTheDocument();
  });

  it('renders identical status copy for identical inputs in dev and production mode', () => {
    vi.stubEnv('DEV', false);
    const production = renderPage('loading', true);
    vi.unstubAllEnvs();
    vi.stubEnv('DEV', true);
    const development = renderPage('loading', true);
    expect(development).toBe(production);
  });
});
