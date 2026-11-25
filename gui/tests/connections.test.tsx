import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';
import type { GuiCapabilities } from '../src/app/capabilities';
import { demoFixtureState, AUTHENTICATED_TEST_CAPABILITIES } from './support/demo-baseline';
import { createMockSharedCredentialsApi } from '../src/dev/mock/agent-settings';
import { createMockPlatformIntegrationsApi } from '../src/dev/mock/platform-integrations';

// Radix Select 在 jsdom 打开弹出层需要这些 API 存在（真实浏览器原生提供）。
beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const CONNECTION_PORTS: GuiCapabilities = {
  ...AUTHENTICATED_TEST_CAPABILITIES,
  sharedCredentials: createMockSharedCredentialsApi(),
  platformIntegrations: createMockPlatformIntegrationsApi(),
};

async function openConnections(capabilities = CONNECTION_PORTS) {
  const user = userEvent.setup();
  render(<App capabilities={capabilities} initialState={demoFixtureState()} />);
  await user.click(
    screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }),
  );
  await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));
  return { user };
}

describe('connections fixture page', () => {
  it('renders the credential and platform modules that have a port', async () => {
    await openConnections();

    expect(
      screen.getByRole('heading', { level: 2, name: '凭据设置' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 2, name: '平台接入' }),
    ).toBeInTheDocument();
    expect(await screen.findByText('feishu-main')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '模型服务' })).toBeNull();
    expect(screen.queryByRole('heading', { name: '共用渠道' })).toBeNull();
    expect(screen.queryByRole('heading', { name: '飞书渠道' })).toBeNull();
    expect(screen.queryByRole('button', { name: '登录服务商' })).toBeNull();
  });

  it('keeps one page h1 and one heading per module inside its surface', async () => {
    await openConnections();
    const page = document.querySelector('.page-connections') as HTMLElement;
    expect(page.querySelectorAll('h1')).toHaveLength(1);
    expect(page.querySelector('h1')?.textContent?.trim()).toBe('Agent 设置');
    const modules = [...page.querySelectorAll('[data-module]')];
    expect(modules.length).toBe(2);
    for (const module of modules) {
      const heading = module.querySelector('h2');
      expect(heading).not.toBeNull();
      expect(module.contains(heading)).toBe(true);
      expect(module.classList.contains('ui-card')).toBe(true);
      expect(module.querySelector('.ui-card')).toBeNull();
    }
    expect(
      [...page.querySelectorAll('h1, h2, h3')]
        .filter((node) => node.textContent?.trim() === '飞书渠道'),
    ).toHaveLength(0);
  });

  it('has no menu entry, no page and no control without any port', async () => {
    const user = userEvent.setup();
    render(<App capabilities={AUTHENTICATED_TEST_CAPABILITIES} initialState={demoFixtureState()} initialPage="connections" />);
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));

    expect(screen.getByRole('menuitem', { name: '用户设置' })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: 'Agent 设置' })).toBeNull();
    expect(document.querySelector('.page-connections')).toBeNull();
    expect(document.querySelector('[data-module]')).toBeNull();
    expect(screen.queryByRole('button', { name: '添加 Key' })).toBeNull();
    expect(screen.queryByRole('button', { name: '添加接入' })).toBeNull();
  });
});
