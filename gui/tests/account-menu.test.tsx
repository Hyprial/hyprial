import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';
import { demoFixtureState, demoFixtureCapabilities } from './support/demo-baseline';
import { createMockSharedCredentialsApi } from '../src/dev/mock/agent-settings';

async function openAccountMenu(withConnectionPort = true) {
  const user = userEvent.setup();
  render(<App
    capabilitiesFactory={dispatch => withConnectionPort
      ? { ...demoFixtureCapabilities(dispatch), sharedCredentials: createMockSharedCredentialsApi() }
      : demoFixtureCapabilities(dispatch)}
    initialState={demoFixtureState()} />);
  const trigger = screen.getByRole('button', {
    name: '我的账号与 Agent 设置菜单',
  });
  await user.click(trigger);
  return { user, trigger };
}

describe('account menu', () => {
  it('opens as a menu, moves focus and closes with Escape', async () => {
    const { user, trigger } = await openAccountMenu();

    expect(screen.getByRole('menu', { name: '账号菜单' })).toBeInTheDocument();
    expect(
      screen.getByRole('menuitem', { name: '用户设置' }),
    ).toHaveFocus();

    await user.keyboard('{ArrowDown}');
    expect(screen.getByRole('menuitem', { name: 'Agent 设置' })).toHaveFocus();

    await user.keyboard('{Escape}');

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
    expect(trigger).toHaveFocus();
  });

  it('navigates to settings from the first account-menu item', async () => {
    const { user } = await openAccountMenu();

    const items = screen.getAllByRole('menuitem');
    expect(items.map((item) => item.textContent)).toEqual([
      '用户设置',
      'Agent 设置',
      '退出本机登录',
    ]);

    await user.click(screen.getByRole('menuitem', { name: '用户设置' }));

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 1, name: '用户设置' }),
    ).toBeInTheDocument();
  });

  it('omits Agent 设置 when the composition has no port for either of its sections', async () => {
    await openAccountMenu(false);

    expect(screen.getAllByRole('menuitem').map((item) => item.textContent)).toEqual([
      '用户设置',
      '退出本机登录',
    ]);
  });

  it('navigates to connections from the menu', async () => {
    const { user } = await openAccountMenu();

    await user.click(screen.getByRole('menuitem', { name: 'Agent 设置' }));

    expect(screen.queryByRole('menu')).not.toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 1, name: 'Agent 设置' }),
    ).toBeInTheDocument();
  });

  it('routes logout through the shared account settings capability', async () => {
    const { user } = await openAccountMenu();
    await user.click(screen.getByRole('menuitem', { name: '退出本机登录' }));
    expect(screen.getByRole('heading', { level: 1, name: '用户设置' })).toBeInTheDocument();
    expect(screen.queryByRole('dialog', { name: '登出此账号？' })).toBeNull();
    await user.click(await screen.findByRole('button', { name: '退出本机登录' }));
    await user.click(screen.getByRole('button', { name: '确认退出' }));
    expect(await screen.findByRole('button', { name: '登录 Hyprial' })).toBeInTheDocument();
    expect(screen.queryByText(/已清空当前会话/)).toBeNull();
  });
});
