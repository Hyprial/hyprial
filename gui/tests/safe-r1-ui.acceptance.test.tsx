import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, vi } from 'vitest';
import { FixtureApp } from '../src/dev/FixtureApp';
import { DevDataSourceNotice } from '../src/dev';

const PRODUCT_ONLY_COPY = /演示|样例|fixture|占位服务/i;

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderApp(): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(() => new Promise(() => undefined)),
  );
  render(
    <FixtureApp
      dataMode="fixture"
      agentSeed={{ agents: [], connections: [], organizations: [] }}
    />,
  );
}

function expectProductSurfaceHasNoPreviewCopy() {
  const productSurface = document.querySelector('.window');
  expect(productSurface).not.toBeNull();
  expect(productSurface?.textContent ?? '').not.toMatch(PRODUCT_ONLY_COPY);
}

describe('safe R1 product surface', () => {
  it('does not present local task, decision, or board fixtures as real state', () => {
    renderApp();

    expect(screen.queryByText('进行中任务')).not.toBeInTheDocument();
    expect(screen.queryByText('待你决定')).not.toBeInTheDocument();
    expect(
      screen.queryByRole('heading', { name: '当前看板' }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText('还没有安排')).not.toBeInTheDocument();
    expect(screen.queryByText('暂无进行中的事项')).not.toBeInTheDocument();
    expect(screen.queryByText('1 个连接等待处理。')).not.toBeInTheDocument();
  });

  it('does not present local test agents or messages as real product state', async () => {
    const user = userEvent.setup();
    renderApp();

    expect(screen.queryByText('可用 Agent')).not.toBeInTheDocument();
    expect(screen.queryByTestId('recent-conversation')).not.toBeInTheDocument();
    expect(screen.queryByText('下午好，佳佳。有什么需要处理的？')).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '聊天' }));

    const productSurface = document.querySelector('.window');
    expect(productSurface).not.toBeNull();
    expect(productSurface).not.toHaveTextContent('可用 Agent');
    expect(productSurface).not.toHaveTextContent('可用状态');
    expect(productSurface).not.toHaveTextContent('下午好，佳佳。有什么需要处理的？');
    expect(screen.getByText('尚未连接，可以先保存草稿。')).toBeInTheDocument();
    expect(screen.getByRole('note')).toHaveTextContent('需要连接模型服务');
  });

  it('keeps the development data-source notice outside the product frame', () => {
    render(
      <>
        <FixtureApp dataMode="fixture" />
        <DevDataSourceNotice />
      </>,
    );

    const notice = screen.getByRole('note', { name: '开发预览数据来源' });
    expect(notice).toHaveTextContent('本地测试数据');
    expect(notice.closest('.window')).toBeNull();
  });

  it('moves settings out of the primary navigation and into the account menu', async () => {
    const user = userEvent.setup();
    renderApp();

    const primaryNavigation = screen.getByRole('navigation', {
      name: '主导航',
    });
    expect(
      within(primaryNavigation).queryByRole('button', { name: '用户设置' }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }),
    );

    const accountMenu = screen.getByRole('menu', { name: '账号菜单' });
    const items = within(accountMenu).getAllByRole('menuitem');
    expect(items.map((item) => item.textContent?.trim())).toEqual([
      '用户设置',
      '退出本机登录',
    ]);

    await user.click(within(accountMenu).getByRole('menuitem', { name: '用户设置' }));
    expect(
      screen.getByRole('heading', { level: 1, name: '用户设置' }),
    ).toBeInTheDocument();
  });

  it('keeps preview-only wording outside every product page', async () => {
    const user = userEvent.setup();
    renderApp();

    expectProductSurfaceHasNoPreviewCopy();

    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
    for (const page of ['聊天', 'Agent', '设备'] as const) {
      await user.click(screen.getByRole('button', { name: page }));
      expectProductSurfaceHasNoPreviewCopy();
    }

    await user.click(
      screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }),
    );
    await user.click(screen.getByRole('menuitem', { name: '用户设置' }));
    expectProductSurfaceHasNoPreviewCopy();

    expect(
      screen.queryByRole('button', { name: '打开首次设置' }),
    ).not.toBeInTheDocument();
    expectProductSurfaceHasNoPreviewCopy();
  });

  it('keeps the chat page inside the full main workspace', async () => {
    const user = userEvent.setup();
    renderApp();

    await user.click(screen.getByRole('button', { name: '聊天' }));

    const chat = document.querySelector('[data-page="chat"]');
    expect(chat).not.toBeNull();
    expect(chat?.parentElement).toHaveClass('content-wide');
    expect(chat?.closest('.main')).not.toBeNull();
    expect(chat?.closest('.card')).toBeNull();
  });
});
