/*
 * Decision record (unavailable copy to notifications, PR 1f/F5, 2026-11-14):
 * the settings account section keeps its controls when a call is refused by the
 * served catalog. Operating the control raises one `${subject}暂不可用。` warning
 * toast whose notification-center source is `method · CODE`; the snapshot,
 * busy gate and logout state do not move, and the page writes no explanation.
 * A client without a logout action does not render the logout control.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../../../src/App';
import { type MethodAccess } from '../../../src/client';
import type { GeneratedClient } from '../../../src/client/generated';
import type { OnboardingStatusResult } from '../../../src/client/generated/types';
import { createConnectedAccountPort, useAccountSettings } from '../../../src/features/account';
import { type AccountSnapshot, type FirstRunActions } from '../../../src/features/onboarding';
import { createEmptyState } from '../../../src/state/appReducer';
import { expectUnavailableToast } from '../../support/notify-harness';
import { stubFirstRunActions } from '../../support/first-run-actions';

type AccountClient = Pick<GeneratedClient, 'auth' | 'onboarding'>;

function status(overrides: Partial<OnboardingStatusResult> = {}) {
  return {
    ok: true, authenticated: false, deviceReady: true, initialized: true,
    nextActions: [], operations: [], owner: null, phase: 'serving', ...overrides,
  } as unknown as OnboardingStatusResult;
}

function fakeClient(account: OnboardingStatusResult) {
  return {
    onboarding: { status: vi.fn().mockResolvedValue(account) },
    auth: {
      login: { begin: vi.fn() },
      operation: { get: vi.fn(), cancel: vi.fn() },
      relogin: vi.fn(),
      logout: vi.fn(),
    },
  };
}

function refusing(...methods: string[]): MethodAccess {
  return method => methods.includes(method)
    ? { available: false, code: 'METHOD_NOT_FOUND', reason: `${method} 当前后端目录未声明。` }
    : { available: true };
}

function actionsOf(client: ReturnType<typeof fakeClient>, access: MethodAccess): FirstRunActions {
  const port = createConnectedAccountPort(client as unknown as AccountClient, access);
  return { ...stubFirstRunActions(), ...port.actions };
}

function Shell({ actions }: { readonly actions: FirstRunActions }) {
  return <App capabilities={{ firstRun: actions, firstRunWizardEnabled: false }}
    initialState={createEmptyState('complete')} initialPage="settings" />;
}

async function sourceInCenter(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: /通知中心/ }));
  return within(screen.getByRole('dialog', { name: '通知中心' }));
}

describe('refused account calls notify on use', () => {
  it('keeps login enabled and notifies when auth.login.begin is refused', async () => {
    const client = fakeClient(status());
    const user = userEvent.setup();
    render(<Shell actions={actionsOf(client, refusing('auth.login.begin'))} />);
    const login = await screen.findByRole('button', { name: '登录 Hyprial' });
    expect(login).toBeEnabled();
    expect(screen.getByRole('button', { name: '刷新账号状态' })).toBeEnabled();

    await user.click(login);

    expectUnavailableToast('登录暂不可用。');
    expect(client.auth.login.begin).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
    expect(screen.getByText('未登录')).toBeInTheDocument();
    expect(document.querySelector('.settings-card')!.textContent).not.toMatch(/暂不可用|不可用/);
    const center = await sourceInCenter(user);
    expect(center.getByText('登录暂不可用。')).toBeInTheDocument();
    expect(center.getByText('来源 · auth.login.begin · METHOD_NOT_FOUND')).toBeInTheDocument();
  });

  it('notifies for a refused status refresh and keeps the displayed account', async () => {
    const client = fakeClient(status({ authenticated: true, owner: 'owner@example.invalid' } as never));
    let refused = false;
    const access: MethodAccess = method => refused && method === 'onboarding.status'
      ? { available: false, code: 'METHOD_NOT_FOUND' } : { available: true };
    const user = userEvent.setup();
    render(<Shell actions={actionsOf(client, access)} />);
    expect(await screen.findByText('owner@example.invalid')).toBeInTheDocument();
    refused = true;

    await user.click(screen.getByRole('button', { name: '刷新账号状态' }));

    expectUnavailableToast('刷新账号状态暂不可用。');
    expect(screen.getByText('owner@example.invalid')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新账号状态' })).toBeEnabled();
    expect(client.onboarding.status).toHaveBeenCalledTimes(1);
  });

  it('keeps logout enabled when auth.logout is refused and leaves the state untouched', async () => {
    const client = fakeClient(status({ authenticated: true, owner: 'owner@example.invalid' } as never));
    const user = userEvent.setup();
    render(<Shell actions={actionsOf(client, refusing('auth.logout'))} />);
    const logout = await screen.findByRole('button', { name: '退出本机登录' });
    expect(logout).toBeEnabled();
    expect(screen.queryByText(/暂不支持/)).not.toBeInTheDocument();

    await user.click(logout);
    await user.click(await screen.findByRole('button', { name: '确认退出' }));

    expectUnavailableToast('退出本机登录暂不可用。');
    expect(client.auth.logout).not.toHaveBeenCalled();
    expect(screen.getByText('owner@example.invalid')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '退出本机登录' })).toBeEnabled();
    expect(screen.queryByText('账号退出')).not.toBeInTheDocument();
    const center = await sourceInCenter(user);
    expect(center.getByText('来源 · auth.logout · METHOD_NOT_FOUND')).toBeInTheDocument();
  });

  it('does not render the logout control for a client without a logout action', async () => {
    const client = fakeClient(status({ authenticated: true, owner: 'owner@example.invalid' } as never));
    const { logoutAccount: _logout, ...withoutLogout } = actionsOf(client, refusing());
    render(<Shell actions={withoutLogout} />);
    expect(await screen.findByText('owner@example.invalid')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '退出本机登录' })).not.toBeInTheDocument();
    expect(screen.queryByText(/暂不支持|不支持退出/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新账号状态' })).toBeEnabled();
  });
});

function fakePopup() {
  return { closed: false, opener: {}, close: vi.fn(), location: { href: 'about:blank', replace: vi.fn() } };
}

describe('a refused login starts nothing', () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it('opens no window, keeps the button enabled and not busy, and notifies', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => fakePopup() as unknown as Window);
    const client = fakeClient(status());
    const user = userEvent.setup();
    render(<Shell actions={actionsOf(client, refusing('auth.login.begin'))} />);
    const login = await screen.findByRole('button', { name: '登录 Hyprial' });

    await user.click(login);

    expectUnavailableToast('登录暂不可用。');
    expect(open).not.toHaveBeenCalled();
    expect(client.auth.login.begin).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
    expect(screen.queryByText('正在等待授权完成，请在授权页继续。')).not.toBeInTheDocument();
  });

  it('refuses before the window when only the receipt read is refused', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => fakePopup() as unknown as Window);
    const client = fakeClient(status());
    const user = userEvent.setup();
    render(<Shell actions={actionsOf(client, refusing('auth.operation.get'))} />);
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    expectUnavailableToast('登录暂不可用。');
    expect(open).not.toHaveBeenCalled();
    const center = await sourceInCenter(user);
    expect(center.getByText('来源 · auth.operation.get · METHOD_NOT_FOUND')).toBeInTheDocument();
  });

  it('still opens the window inside the click call stack when login is allowed', async () => {
    const popup = fakePopup();
    const open = vi.spyOn(window, 'open').mockImplementation(() => popup as unknown as Window);
    const client = fakeClient(status());
    client.auth.login.begin.mockReturnValue(new Promise(() => undefined));
    render(<Shell actions={actionsOf(client, refusing())} />);
    const login = await screen.findByRole('button', { name: '登录 Hyprial' });

    fireEvent.click(login);

    expect(open).toHaveBeenCalledTimes(1);
    expect(open).toHaveBeenCalledWith('about:blank', '_blank');
    expect(popup.close).not.toHaveBeenCalled();
  });

  it('refuses retry() and run(login) before any window, cancellation or busy state', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => fakePopup() as unknown as Window);
    const client = fakeClient(status());
    const notify = vi.fn();
    const actions = actionsOf(client, refusing('auth.login.begin'));
    const { result } = renderHook(() => useAccountSettings(actions, notify));
    await waitFor(() => expect(result.current.busy).toBeNull());
    await act(async () => { await result.current.retry(); });
    await act(async () => { await result.current.run('login'); });
    expect(open).not.toHaveBeenCalled();
    expect(notify).toHaveBeenCalledTimes(2);
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({
      text: '登录暂不可用。', source: 'auth.login.begin · METHOD_NOT_FOUND',
    }));
    expect(result.current.busy).toBeNull();
  });
});

describe('a refused cancellation leaves the running login alone', () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it('notifies once, closes no window and lets the login finish', async () => {
    const popup = fakePopup();
    vi.spyOn(window, 'open').mockImplementation(() => popup as unknown as Window);
    const client = fakeClient(status());
    let release: (value: { operationId: string }) => void = () => undefined;
    client.auth.login.begin.mockReturnValue(new Promise(resolve => { release = resolve; }));
    client.auth.operation.get.mockResolvedValue({ operation: {
      kind: 'login', operationId: 'op-1', state: 'authenticated', owner: 'owner@example.invalid',
    } });
    client.onboarding.status
      .mockResolvedValueOnce(status())
      .mockResolvedValue(status({ authenticated: true, owner: 'owner@example.invalid' } as never));
    const notify = vi.fn();
    const actions = actionsOf(client, refusing('auth.operation.cancel'));
    const { result } = renderHook(() => useAccountSettings(actions, notify));
    await waitFor(() => expect(result.current.busy).toBeNull());
    let login: Promise<void> = Promise.resolve();
    act(() => { login = result.current.run('login'); });
    await waitFor(() => expect(result.current.busy).toBe('login'));

    await act(async () => { await result.current.cancel(); });

    expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
      text: '取消授权暂不可用。', source: 'auth.operation.cancel · METHOD_NOT_FOUND',
    }));
    expect(popup.close).not.toHaveBeenCalled();
    expect(result.current.busy).toBe('login');
    await act(async () => { release({ operationId: 'op-1' }); await login; });
    expect(result.current.snapshot.account.status).toBe('authenticated');
    expect(result.current.busy).toBeNull();
  });
});

function scripted() {
  let authenticated = true;
  let refuseStatus = false;
  let failStatus = false;
  let owner = 'owner@example.invalid';
  const signedIn = (): AccountSnapshot => ({ account: { status: 'authenticated', owner }, device: { status: 'ready' } });
  const actions: FirstRunActions = {
    ...stubFirstRunActions(),
    getAccountState: async () => failStatus ? Promise.reject(new Error('boom')) : refuseStatus
      ? { account: { status: 'unavailable', refusal: { code: 'METHOD_NOT_FOUND', source: 'onboarding.status' } }, device: { status: 'unknown', message: 'x' } }
      : authenticated ? signedIn() : { account: { status: 'unauthenticated' }, device: { status: 'not-ready' } },
    logoutAccount: async () => {
      authenticated = false;
      return { status: 'logged-out', snapshot: { account: { status: 'unauthenticated' }, device: { status: 'not-ready' } } };
    },
    authenticateAccount: async () => ({ status: 'unauthenticated' }),
    loginRefusal: () => ({ code: 'METHOD_NOT_FOUND', source: 'auth.login.begin' }),
  };
  return {
    actions,
    refuse: (value: boolean) => { refuseStatus = value; },
    fail: (value: boolean) => { failStatus = value; },
    relogin: (next: string) => { owner = next; },
    signOut: () => { authenticated = false; },
  };
}


describe('the notification center follows the signed-in session', () => {
  it('drops entries of a signed-in session at logout and lists entries raised while signed out', async () => {
    const { actions, refuse } = scripted();
    const user = userEvent.setup();
    render(<Shell actions={actions} />);
    expect(await screen.findByText('owner@example.invalid')).toBeInTheDocument();
    refuse(true);
    await user.click(screen.getByRole('button', { name: '刷新账号状态' }));
    expectUnavailableToast('刷新账号状态暂不可用。');
    refuse(false);
    let center = await sourceInCenter(user);
    expect(center.getByText('来源 · onboarding.status · METHOD_NOT_FOUND')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /通知中心/ }));

    await user.click(screen.getByRole('button', { name: '退出本机登录' }));
    await user.click(await screen.findByRole('button', { name: '确认退出' }));
    const login = await screen.findByRole('button', { name: '登录 Hyprial' });
    center = await sourceInCenter(user);
    expect(center.getByText('暂无通知')).toBeInTheDocument();
    expect(center.queryByText(/onboarding\.status/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /通知中心/ }));

    await user.click(login);
    center = await sourceInCenter(user);
    expect(center.getByText('登录暂不可用。')).toBeInTheDocument();
    expect(center.getByText('来源 · auth.login.begin · METHOD_NOT_FOUND')).toBeInTheDocument();
    expect(center.queryByText(/onboarding\.status/)).not.toBeInTheDocument();
  });
});

describe('the notification center clears only on a confirmed identity change', () => {
  async function raised() {
    const harness = scripted();
    const user = userEvent.setup();
    render(<Shell actions={harness.actions} />);
    expect(await screen.findByText('owner@example.invalid')).toBeInTheDocument();
    harness.refuse(true);
    await user.click(screen.getByRole('button', { name: '刷新账号状态' }));
    expectUnavailableToast('刷新账号状态暂不可用。');
    harness.refuse(false);
    return { harness, user };
  }
  const entryCount = async (user: ReturnType<typeof userEvent.setup>) => {
    const center = await sourceInCenter(user);
    const found = center.queryAllByText('来源 · onboarding.status · METHOD_NOT_FOUND').length;
    await user.click(screen.getByRole('button', { name: /通知中心/ }));
    return found;
  };

  it('keeps center and toasts when a dispatched status read fails', async () => {
    const { harness, user } = await raised();
    harness.fail(true);
    await user.click(screen.getByRole('button', { name: '刷新账号状态' }));
    expect(await screen.findByText('账号服务不可用，请检查本机服务后刷新状态。')).toBeInTheDocument();
    expect(screen.getByText('刷新账号状态暂不可用。')).toBeInTheDocument();
    expect(await entryCount(user)).toBe(1);
  });

  it('clears center and toasts when the status is confirmed unauthenticated', async () => {
    const { harness, user } = await raised();
    harness.signOut();
    await user.click(screen.getByRole('button', { name: '刷新账号状态' }));
    await screen.findByRole('button', { name: '登录 Hyprial' });
    expect(screen.queryByText('刷新账号状态暂不可用。')).not.toBeInTheDocument();
    expect(await entryCount(user)).toBe(0);
  });

  it('clears center and toasts when another owner is confirmed without a signed-out state', async () => {
    const { harness, user } = await raised();
    harness.relogin('other@example.invalid');
    await user.click(screen.getByRole('button', { name: '刷新账号状态' }));
    expect(await screen.findByText('other@example.invalid')).toBeInTheDocument();
    expect(screen.queryByText('刷新账号状态暂不可用。')).not.toBeInTheDocument();
    expect(await entryCount(user)).toBe(0);
  });

  it('keeps entries when the same owner is confirmed again', async () => {
    const { user } = await raised();
    await user.click(screen.getByRole('button', { name: '刷新账号状态' }));
    expect(await screen.findByText('owner@example.invalid')).toBeInTheDocument();
    expect(await entryCount(user)).toBe(1);
  });
});

describe('the device destination without any content', () => {
  it('is hidden and its route redirects when status is refused and no card port exists', async () => {
    const client = fakeClient(status());
    render(<App capabilities={{ firstRun: actionsOf(client, refusing('onboarding.status')), firstRunWizardEnabled: false }}
      initialState={createEmptyState('complete')} initialPage="device" />);
    await screen.findByRole('button', { name: /通知中心/ });
    await waitFor(() => expect(document.querySelector('[data-page="device"]')).toBeNull());
    expect(screen.queryByRole('button', { name: /^设备$/ })).not.toBeInTheDocument();
  });

  it('stays visible while the status read is served', async () => {
    const client = fakeClient(status());
    const firstRun = { ...actionsOf(client, refusing()), initializeDevice: async () => ({ status: 'failed' as const, message: '未完成' }) };
    render(<App capabilities={{ firstRun, firstRunWizardEnabled: false }}
      initialState={createEmptyState('complete')} initialPage="device" />);
    expect(await screen.findByRole('button', { name: '初始化当前设备' })).toBeInTheDocument();
    expect(document.querySelector('[data-page="device"]')).not.toBeNull();
  });
});

describe('the status read the page starts by itself', () => {
  it('renders no status field and raises no notification when onboarding.status is refused before dispatch', async () => {
    const client = fakeClient(status());
    render(<Shell actions={actionsOf(client, refusing('onboarding.status'))} />);
    const login = await screen.findByRole('button', { name: '登录 Hyprial' });
    const card = document.querySelector('.settings-card')!;
    expect(client.onboarding.status).not.toHaveBeenCalled();
    expect(card.querySelector('[role="status"]')).toBeNull();
    expect(card.querySelector('.state-error, [role="alert"]')).toBeNull();
    expect(card.textContent).not.toMatch(/账号状态不可用|未登录|暂时无法读取/);
    expect(login).toBeEnabled();
    expect(document.querySelector('.toast-host .toast')).toBeNull();
  });

  it('keeps the failed state for a read that was dispatched and failed', async () => {
    const client = fakeClient(status());
    client.onboarding.status.mockRejectedValue(new Error('boom'));
    render(<Shell actions={actionsOf(client, refusing())} />);
    expect(await screen.findByText('暂时无法读取账号信息，请刷新重试。')).toBeInTheDocument();
    expect(client.onboarding.status).toHaveBeenCalledTimes(1);
    expect(document.querySelector('.toast-host .toast')).toBeNull();
  });
});
