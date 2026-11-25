import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { createGuiCapabilities, createNativeGuiCapabilities } from '../src/app/capabilities';
import { FirstRunOnboarding } from '../src/features/onboarding';
import { AccountProgressPanel } from '../src/features/onboarding/impl/AccountProgressPanel';
import type { AuthClient, AuthProgress, AuthState } from '../src/platform/auth';
import type { SetupClient } from '../src/platform/setup';

const signedOut: AuthState = {
  authenticated: false,
  owner: null,
  deviceReady: false,
  sidecarError: null,
  ready: false,
};
const authenticatedPartial: AuthState = {
  authenticated: true,
  owner: null,
  deviceReady: false,
  sidecarError: null,
  ready: false,
};
const authenticatedReady: AuthState = {
  ...authenticatedPartial,
  owner: 'ready@example.invalid',
  deviceReady: true,
  ready: true,
};

afterEach(() => vi.restoreAllMocks());

function host(overrides: Partial<AuthClient> = {}): AuthClient {
  return {
    status: vi.fn(async () => signedOut),
    login: vi.fn(async () => ({ ok: true, state: signedOut })),
    device: vi.fn(async () => ({ ok: false, state: signedOut })),
    cancel: vi.fn(async () => ({ ok: true, cancelled: true })),
    ...overrides,
  };
}

describe('shared GUI with a native account capability', () => {
  it('selects native capabilities only when the host provides an account bridge', async () => {
    const account = host();
    const native = createGuiCapabilities(account);

    expect(native.firstRun.getSetupState).toBeUndefined();
    expect(await native.firstRun.getAccountState?.()).toEqual({
      account: { status: 'unauthenticated' },
      device: { status: 'not-ready', message: undefined },
    });
    expect(native.firstRun.saveModelKey).toBeUndefined();
    expect(account.status).toHaveBeenCalledTimes(1);
    expect(account.login).not.toHaveBeenCalled();

    const web = createGuiCapabilities();
    expect(web.firstRun.getSetupState).toBeUndefined();
  });

  it('renders signed-out settings, device status and all four steps without claiming readiness', async () => {
    const account = host();
    const user = userEvent.setup();

    const view = render(
      <App
        capabilities={createNativeGuiCapabilities(account)}
      />,
    );
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '用户设置' }));

    expect(await screen.findByText('未登录')).toBeInTheDocument();
    expect(screen.queryByLabelText('设备状态：尚未初始化')).not.toBeInTheDocument();
    expect(screen.queryByLabelText('DeepSeek API Key')).not.toBeInTheDocument();
    expect(account.login).not.toHaveBeenCalled();
    expect(account.device).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: '设备' }));
    await user.click(screen.getByRole('button', { name: '初始化当前设备' }));
    expect(screen.getByText('请先登录')).toBeInTheDocument();
    expect(account.device).not.toHaveBeenCalled();
    expect(screen.queryByLabelText('DeepSeek API Key')).not.toBeInTheDocument();
    view.unmount();

    const setup = {
      status: vi.fn(), saveModelKey: vi.fn(),
    } as unknown as SetupClient;
    render(
      <App
        initialPage="first-run"
        capabilities={createNativeGuiCapabilities(account, setup)}
      />,
    );
    expect(screen.getByRole('navigation', { name: '首次设置步骤' })).toBeInTheDocument();
    for (const label of ['1 · 登录', '2 · 设备', '3 · 运行配置', '4 · 对话']) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(await screen.findByLabelText('账号：尚未登录')).toBeInTheDocument();
    expect(screen.queryByLabelText('当前设备：尚未初始化')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeInTheDocument();
    expect(screen.queryByText(/已就绪|已确认/)).not.toBeInTheDocument();
    expect(account.login).not.toHaveBeenCalled();
    expect(account.device).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('offers no first-run wizard for a native host without a setup bridge', () => {
    expect(createNativeGuiCapabilities(host()).firstRunWizardEnabled).toBe(false);
    const setup = { status: vi.fn(), saveModelKey: vi.fn() } as unknown as SetupClient;
    expect(createNativeGuiCapabilities(host(), setup).firstRunWizardEnabled).toBe(true);
  });

  it('only marks the device ready when every host readiness field is confirmed', async () => {
    const cases: ReadonlyArray<readonly [AuthState, string]> = [
      [{ ...authenticatedPartial, deviceReady: true, ready: false }, 'not-ready'],
      [{ ...authenticatedPartial, deviceReady: false, ready: true }, 'not-ready'],
      [{ ...authenticatedPartial, deviceReady: true, sidecarError: 'SIDECAR_MISSING', ready: true }, 'failed'],
      [authenticatedReady, 'ready'],
    ];

    for (const [state, expected] of cases) {
      const actions = createNativeGuiCapabilities(
        host({ status: async () => state }),
      ).firstRun;
      const snapshot = await actions.getAccountState?.();
      expect(snapshot?.account.status).toBe('authenticated');
      expect(snapshot?.device.status).toBe(expected);
    }
  });

  it('refreshes authoritative host status when a native operation omits AuthState', async () => {
    const status = vi.fn(async () => authenticatedPartial);
    const login = vi.fn(async () => ({ ok: true }));
    const actions = createNativeGuiCapabilities(host({ status, login })).firstRun;

    const result = await actions.authenticateAccount!({ account: '', token: '' }, 'login');
    expect(result).toMatchObject({ status: 'authenticated' });
    if (result.status === 'authenticated') expect(result.owner).toBeUndefined();
    expect(status).toHaveBeenCalledTimes(1);

    const readyStatus = vi.fn(async () => authenticatedReady);
    const device = vi.fn(async () => ({ ok: true }));
    const deviceActions = createNativeGuiCapabilities(
      host({ status: readyStatus, device }),
    ).firstRun;

    expect(await deviceActions.initializeDevice!()).toMatchObject({
      status: 'ready',
    });
    expect(readyStatus).toHaveBeenCalledTimes(1);
  });

  it('does not claim success when a state-less login cannot refresh status', async () => {
    const result = await createNativeGuiCapabilities(
      host({
        status: async () => {
          throw new Error('offline');
        },
        login: async () => ({ ok: true }),
      }),
    ).firstRun.authenticateAccount!({ account: '', token: '' }, 'login');

    expect(result).toMatchObject({ status: 'unknown' });
    expect(result.message).not.toContain('offline');
  });

  it('does not claim login success when a failed native login carries an authenticated state', async () => {
    const status = vi.fn(async () => signedOut);
    const login = vi.fn(async () => ({ ok: false, state: authenticatedPartial }));
    const result = await createNativeGuiCapabilities(host({ status, login })).firstRun.authenticateAccount!(
      { account: '', token: '' },
      'login',
    );

    expect(result).toMatchObject({ status: 'failed', retryable: true });
    expect(status).not.toHaveBeenCalled();
  });

  it('does not claim device readiness when a failed native device carries a ready state', async () => {
    const device = vi.fn(async () => ({ ok: false, state: authenticatedReady }));
    const result = await createNativeGuiCapabilities(host({ device })).firstRun.initializeDevice!();

    expect(result).toMatchObject({ status: 'failed', retryable: true });
  });

  it('initializes a partial device without repeating login', async () => {
    let current = authenticatedPartial;
    const status = vi.fn(async () => current);
    const device = vi.fn(async () => {
      current = authenticatedReady;
      return { ok: true, state: authenticatedReady };
    });
    const login = vi.fn();
    const actions = createNativeGuiCapabilities(
      host({ status, device, login }),
    ).firstRun;
    const user = userEvent.setup();

    render(<FirstRunOnboarding actions={actions} />);
    await user.click(await screen.findByRole('button', { name: '继续到设备' }));
    expect(
      await screen.findByText('账号已登录，但当前设备尚未就绪。'),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^当前账号：/)).not.toBeInTheDocument();
    expect(screen.getByLabelText('当前设备：尚未初始化')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '初始化当前设备' }));
    expect(
      await screen.findByRole('button', { name: '继续到运行配置' }),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText(
        '当前设备：设备已就绪',
      ),
    ).toBeInTheDocument();
    expect(login).not.toHaveBeenCalled();
    expect(device).toHaveBeenCalledTimes(1);
  });

  it('keeps a confirmed login when the device refresh fails', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    let reads = 0;
    const status = vi.fn(async () => {
      reads += 1;
      if (reads === 1) return signedOut;
      throw new Error('offline');
    });
    const login = vi.fn(async () => ({ ok: true, state: authenticatedPartial }));
    const actions = createNativeGuiCapabilities(host({ status, login })).firstRun;
    const user = userEvent.setup();

    render(<FirstRunOnboarding actions={actions} />);
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    await user.click(await screen.findByRole('button', { name: '继续到设备' }));

    expect(
      await screen.findByText(
        '账号已登录，但当前设备状态未能确认。请刷新状态或重试设备初始化。',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^当前账号：/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: '登录 Hyprial' }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: '重试设备初始化' }),
    ).toBeInTheDocument();
  });

  it('forwards the native progress whitelist without a user code or host payloads', async () => {
    const progress = vi.fn();
    const login = vi.fn(async (notify?: (event: AuthProgress) => void) => {
      notify?.({
        event: 'device',
        verificationUri: 'https://auth.example.invalid/device',
        expiresIn: 300,
        interval: 5,
      });
      return { ok: true, state: authenticatedPartial };
    });
    const actions = createNativeGuiCapabilities(host({ login })).firstRun;
    const unsubscribe = actions.subscribeAccountProgress?.(progress);

    await actions.authenticateAccount!({ account: '', token: '' }, 'login');
    expect(login).toHaveBeenCalledWith(expect.any(Function));
    expect(progress).toHaveBeenCalledWith({
      event: 'device',
      verificationUri: 'https://auth.example.invalid/device',
      expiresIn: 300,
      interval: 5,
    });
    expect(JSON.stringify(progress.mock.calls)).not.toMatch(
      /token|secret|accessToken|refreshToken|userCode/i,
    );
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    unsubscribe?.();
  });

  it('forwards an explicit native cancel acknowledgement and fails closed otherwise', async () => {
    const notCancelled = vi.fn(async () => ({ ok: true, cancelled: false }));
    const direct = createNativeGuiCapabilities(host({ cancel: notCancelled })).firstRun;
    expect(await direct.cancelAccount?.()).toEqual({ ok: true, cancelled: false });

    const malformed = vi.fn(async () => ({ ok: true })) as unknown as AuthClient['cancel'];
    const missing = createNativeGuiCapabilities(host({ cancel: malformed })).firstRun;
    expect(await missing.cancelAccount?.()).toEqual({ ok: false, cancelled: false });

    const thrown = vi.fn(async () => { throw new Error('connection lost'); });
    const unavailable = createNativeGuiCapabilities(host({ cancel: thrown })).firstRun;
    expect(await unavailable.cancelAccount?.()).toEqual({ ok: false, cancelled: false });
  });

  it('exposes a controlled open action only when the native host supplies one', async () => {
    const open = vi.fn(async () => ({ ok: true, opened: true }));
    const native = createNativeGuiCapabilities(host({ open })).firstRun;
    expect(native.openAuthorization).toBeDefined();
    expect(await native.openAuthorization?.()).toEqual({ ok: true, opened: true });
    expect(open).toHaveBeenCalledOnce();
    expect(createNativeGuiCapabilities(host()).firstRun.openAuthorization).toBeUndefined();
    expect(createGuiCapabilities().firstRun.openAuthorization).toBeUndefined();
  });

  it('keeps a native login in the system browser without reserving a WebView popup', async () => {
    const windowOpen = vi.spyOn(window, 'open').mockReturnValue(null);
    let progress!: (event: AuthProgress) => void;
    const login = vi.fn((notify?: (event: AuthProgress) => void) => new Promise<never>(() => {
      if (notify) progress = notify;
    }));
    const open = vi.fn(async () => ({ ok: true, opened: true }));
    const actions = createNativeGuiCapabilities(host({ login, open })).firstRun;
    const user = userEvent.setup();

    render(<FirstRunOnboarding actions={actions} />);
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    expect(windowOpen).not.toHaveBeenCalled();

    act(() => progress({
      event: 'device',
      verificationUri: 'https://auth.hyprial.com/device?code=1',
      expiresIn: 300,
      openAvailable: true,
      openOpened: true,
    }));
    expect(windowOpen).not.toHaveBeenCalled();
    expect(screen.getByText('请在浏览器完成登录或注册。')).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: '打开授权页' })).not.toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: '打开授权页' }));
    expect(open).toHaveBeenCalledOnce();
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it('routes the Settings login through the native opener without a WebView popup', async () => {
    const windowOpen = vi.spyOn(window, 'open').mockReturnValue(null);
    let progress!: (event: AuthProgress) => void;
    const login = vi.fn((notify?: (event: AuthProgress) => void) => new Promise<never>(() => {
      if (notify) progress = notify;
    }));
    const open = vi.fn(async () => ({ ok: true, opened: true }));
    const user = userEvent.setup();
    render(
      <App
        capabilities={createNativeGuiCapabilities(host({ login, open }))}
      />,
    );
    await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    await user.click(screen.getByRole('menuitem', { name: '用户设置' }));
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    expect(windowOpen).not.toHaveBeenCalled();

    act(() => progress({
      event: 'device',
      verificationUri: 'https://auth.hyprial.com/device?code=1',
      openAvailable: true,
    }));
    await user.click(screen.getByRole('button', { name: '打开授权页' }));
    expect(open).toHaveBeenCalledOnce();
    expect(windowOpen).not.toHaveBeenCalled();
  });

  it('shows a retryable native open failure without a WebView popup or link', async () => {
    const windowOpen = vi.spyOn(window, 'open').mockReturnValue(null);
    const openAuthorization = vi.fn(async () => ({
      ok: false,
      opened: false,
      code: 'DESKTOP_ACCOUNT_OPEN_FAILED',
      retryable: true,
    }));
    const user = userEvent.setup();
    render(
      <AccountProgressPanel
        progress={{
          event: 'device',
          verificationUri: 'https://auth.hyprial.com/device?code=1',
          openAvailable: true,
          openError: 'DESKTOP_ACCOUNT_OPEN_FAILED',
          openRetryable: true,
        }}
        active
        authorizing
        onCancel={vi.fn()}
        onRetry={vi.fn()}
        onOpenAuthorization={openAuthorization}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('无法自动打开系统浏览器');
    expect(screen.queryByRole('link', { name: '打开授权页' })).not.toBeInTheDocument();
    expect(windowOpen).not.toHaveBeenCalled();

    const retry = screen.getByRole('button', { name: '打开授权页' });
    await user.click(retry);
    expect(openAuthorization).toHaveBeenCalledOnce();
    expect(await screen.findByRole('alert')).toHaveTextContent('无法打开系统浏览器');
    expect(windowOpen).not.toHaveBeenCalled();
    await user.click(retry);
    expect(openAuthorization).toHaveBeenCalledTimes(2);
  });

  it('guides device-code entry from the safe URL without a bridge user code', () => {
    render(
      <AccountProgressPanel
        progress={{
          event: 'device',
          verificationUri: 'https://auth.example.invalid/device',
          expiresIn: 600,
        }}
        active
        authorizing
        onCancel={vi.fn()}
        onRetry={vi.fn()}
      />,
    );

    expect(
      screen.getByText(
        '请在浏览器完成登录或注册。',
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/^授权码：/)).not.toBeInTheDocument();
    expect(
      screen.getByRole('link', { name: '打开授权页' }),
    ).toHaveAttribute('href', 'https://auth.example.invalid/device');
    expect(document.body.textContent).not.toMatch(/token|secret/i);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});
