import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';
import {
  type AccountProgress,
  type FirstRunActions,
} from '../src/features/onboarding';
import { AppShell } from '../src/app/AppShell';
import { GuiCapabilitiesProvider } from '../src/app/capabilities';
import { createEmptyState } from '../src/state/appReducer';
import { stubFirstRunActions } from './support/first-run-actions';

async function settings(overrides: Partial<FirstRunActions> = {}) {
  const actions = { ...stubFirstRunActions(), ...overrides };
  const user = userEvent.setup();  render(<App capabilities={{ firstRun: actions }} />);
  await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
  await user.click(screen.getByRole('menuitem', { name: '用户设置' }));
  return { user, actions };
}

async function firstRun(overrides: Partial<FirstRunActions> = {}) {
  const actions = { ...stubFirstRunActions(), ...overrides };
  const user = userEvent.setup();  const state = { ...createEmptyState(), page: 'first-run' as const };
  render(<GuiCapabilitiesProvider value={{ firstRun: actions }}>
    <AppShell state={state} dispatch={vi.fn()} firstRunWizardEnabled />
  </GuiCapabilitiesProvider>);
  return { user, actions };
}

describe('product first-run composition', () => {
  it('restores confirmed account state on the first-run route', async () => {
    const getAccountState = vi.fn().mockResolvedValue({ account: { status: 'authenticated', owner: 'restored' }, device: { status: 'ready' } });
    const { user } = await firstRun({ getAccountState, saveModelKey: async () => ({ status: 'saved' }) });
    expect(await screen.findByText('当前账号：restored')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '继续到设备' }));
    await user.click(screen.getByRole('button', { name: '继续到运行配置' }));
    expect(screen.getByLabelText(/DeepSeek API Key/)).toBeInTheDocument();
    expect(getAccountState).toHaveBeenCalledTimes(2);
  });
  it('does not advance or simulate success when official login capability is unavailable', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    const { user } = await firstRun({ authenticateAccount: async () => ({ status: 'unavailable', message: '账号服务不可用。' }) });
    await user.click(screen.getByRole('button', { name: '登录 Hyprial' }));
    expect((await screen.findAllByText('账号服务不可用')).length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: '继续到设备' })).not.toBeInTheDocument();
  });
  it('shares the real-event login progress panel in Settings', async () => {
    let progress!: (event: AccountProgress) => void;
    let finish!: (result: { status: 'authenticated'; owner: string }) => void;
    const getAccountState = vi.fn()
      .mockResolvedValueOnce({
        account: { status: 'unauthenticated' },
        device: { status: 'not-ready' },
      })
      .mockResolvedValueOnce({
        account: { status: 'authenticated', owner: 'settings@example.invalid' },
        device: { status: 'ready' },
      });
    const { user } = await settings({
      getAccountState,
      authenticateAccount: () => new Promise((resolve) => { finish = resolve; }),
      subscribeAccountProgress: (listener) => {
        progress = listener;
        return () => {};
      },
    });
    expect(screen.queryByLabelText('DeepSeek API Key')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '登录 Hyprial' }));

    act(() => progress({
      event: 'device',
      verificationUri: 'https://auth.example.invalid/device',
      userCode: 'SETTINGS',
      expiresIn: 300,
      interval: 5,
    }));
    expect(screen.getByText(/请在浏览器完成登录或注册/)).toBeInTheDocument();
    expect(screen.getByText('授权码：SETTINGS')).toBeInTheDocument();
    expect(screen.queryByText(/轮询间隔/)).not.toBeInTheDocument();

    await act(async () => finish({
      status: 'authenticated',
      owner: 'settings@example.invalid',
    }));
    expect(
      await screen.findByText(
        'settings@example.invalid',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/设备状态：/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText('DeepSeek API Key')).not.toBeInTheDocument();
  });

  it('key saving on product entry uses shared capability and does not fake a response', async () => {
    const saveModelKey = vi.fn().mockResolvedValue({ status: 'failed', message: '保存失败，未推进。' });
    const { user } = await firstRun({ getAccountState: vi.fn().mockResolvedValue({ account: { status: 'authenticated' }, device: { status: 'ready' } }), saveModelKey });
    await user.click(await screen.findByRole('button', { name: '继续到设备' }));
    await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'isolated-only');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText('保存失败，未推进。')).toBeInTheDocument();
    expect(saveModelKey).toHaveBeenCalledWith('isolated-only');
    expect(screen.queryByRole('heading', { name: '首次对话' })).not.toBeInTheDocument();
  });
});

describe('A7 hidden first-run wizard', () => {
  it('hides the settings entry and renders no wizard when disabled', async () => {
    const user = userEvent.setup();    render(<App capabilities={{ firstRun: stubFirstRunActions(), firstRunWizardEnabled: false }} />);
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '用户设置' }));
    expect(screen.queryByRole('button', { name: '打开首次设置' })).toBeNull();
    expect(screen.queryByRole('button', { name: '继续到设备' })).toBeNull();
  });

  it('shows an unknown account receipt as unknown, never as completed', async () => {
    const getAccountState = vi.fn().mockResolvedValue({
      account: { status: 'unknown', message: '授权操作 op-9 仍在进行；请刷新以读取服务端回执。' },
      device: { status: 'not-ready' },
    });
    const { user } = await settings({ getAccountState });
    expect(await screen.findByText('授权操作 op-9 仍在进行；请刷新以读取服务端回执。')).toBeInTheDocument();
    expect(screen.queryByText('结果未确认，不视为已登录。')).not.toBeInTheDocument();
    expect(screen.queryByText('账号状态')).not.toBeInTheDocument();
    expect(screen.queryByText(/当前账号：/)).toBeNull();
    expect(getAccountState).toHaveBeenCalled();
    void user;
  });
});

describe('A7 route-level wizard refusal', () => {
  it('renders the start page, not the wizard, when the route is forced', () => {
    const state = { ...createEmptyState(), page: 'first-run' as const };
    const { container } = render(<AppShell state={state} dispatch={vi.fn()} firstRunWizardEnabled={false} />);
    // The wizard route is refused at render time: no wizard root, start page instead.
    expect(container.querySelector('.dev01-v17-onboarding')).toBeNull();
    expect(container.querySelector('[data-page="start"]')).not.toBeNull();
  });
});
