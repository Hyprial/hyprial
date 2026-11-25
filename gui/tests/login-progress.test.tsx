import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  FirstRunOnboarding,
  type AccountProgress,
  type FirstRunActions,
} from '../src/features/onboarding';
import {
  AccountProgressPanel,
  LONG_WAIT_SECONDS,
  WAIT_TIMEOUT_SECONDS,
  accountProgressCopy,
  waitNotice,
} from '../src/features/onboarding/impl/AccountProgressPanel';

type User = ReturnType<typeof userEvent.setup>;

function makeActions(
  overrides: Partial<FirstRunActions> = {},
): FirstRunActions {
  return {
    saveModelKey: async () => ({ status: 'saved' }),
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    ...overrides,
  };
}

function accountStep(): HTMLElement {
  return document.querySelector<HTMLElement>('[data-step="account"]')!;
}

function deviceStep(): HTMLElement {
  return document.querySelector<HTMLElement>('[data-step="device"]')!;
}

async function startLogin(user: User): Promise<void> {
  await user.click(
    await within(accountStep()).findByRole('button', { name: '登录 Hyprial' }),
  );
}

beforeEach(() => {
  vi.spyOn(window, 'open').mockReturnValue(null);
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('login progress copy', () => {
  it('maps real events without inventing saving or success', () => {
    const events: AccountProgress['event'][] = [
      'device',
      'authenticated',
      'committed',
      'unknown',
    ];
    for (const event of events) {
      const copy = accountProgressCopy({ event }, true);
      const text = `${copy.title} ${copy.detail}`;
      expect(text).not.toMatch(/正在保存|保存登录|已成功登录|账号已登录/);
    }
    expect(accountProgressCopy({ event: 'device' }, true).title)
      .toContain('请在浏览器完成登录或注册');
    expect(accountProgressCopy({ event: 'unknown' }, true).detail)
      .toContain('不视为登录成功');
  });

  it('describes long waits and timeouts without claiming success', () => {
    expect(waitNotice(LONG_WAIT_SECONDS - 1)).toBeNull();
    expect(waitNotice(LONG_WAIT_SECONDS)).toContain('仍在等待登录结果');
    expect(waitNotice(WAIT_TIMEOUT_SECONDS)).toContain('仍未确认');
    expect(waitNotice(WAIT_TIMEOUT_SECONDS)).toContain('可取消或重试');
  });
});

describe('first-run login progress', () => {
  it('shows one authorization entry without transport details or duplicate instructions', () => {
    render(<AccountProgressPanel active authorizing onCancel={vi.fn()} onRetry={vi.fn()}
      progress={{ event: 'device', verificationUri: 'https://auth.example.invalid/device',
        userCode: 'SHORT', expiresIn: 300, interval: 5 }} />);
    expect(screen.getAllByRole('link')).toHaveLength(1);
    expect(screen.queryByRole('button', { name: '打开授权页' })).not.toBeInTheDocument();
    expect(screen.queryByText(/轮询间隔|弹窗被拦截|授权后还需等待/)).not.toBeInTheDocument();
    expect(screen.getByText('授权码：SHORT')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '取消登录' })).toBeEnabled();
  });

  it('completes from a single authorization and shows real account/device/network status', async () => {
    let progress!: (event: AccountProgress) => void;
    let finish!: (result: { status: 'authenticated'; owner: string }) => void;
    const authenticateAccount = vi.fn(
      () => new Promise<{ status: 'authenticated'; owner: string }>((resolve) => {
        progress({
          event: 'device',
          verificationUri: 'https://auth.example.invalid/device',
          userCode: 'ABCD',
          expiresIn: 300,
        });
        finish = resolve;
      }),
    );
    const actions = makeActions({
      authenticateAccount,
      subscribeAccountProgress: (listener) => {
        progress = listener;
        return () => {};
      },
    });
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions} />);
    expect(screen.queryByText('未检测 · 待检测')).not.toBeInTheDocument();
    await startLogin(user);

    expect(await screen.findByText('授权码：ABCD')).toBeInTheDocument();
    expect(screen.getByText(/授权剩余约 5 分 0 秒/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '打开授权页' }))
      .toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByLabelText(/^账号：.*账号已登录$/)).not.toBeInTheDocument();

    await act(async () => finish({
      status: 'authenticated',
      owner: 'person@example.invalid',
    }));
    expect(
      await screen.findByLabelText('账号：账号已登录'),
    ).toBeInTheDocument();

    await user.click(
      within(accountStep()).getByRole('button', { name: '继续到设备' }),
    );
    await user.click(
      await within(deviceStep()).findByRole('button', { name: '初始化当前设备' }),
    );
    expect(
      await within(deviceStep()).findByLabelText(
        '当前设备：设备已就绪',
      ),
    ).toBeInTheDocument();
    expect(screen.getByText('未检测 · 待检测')).toBeInTheDocument();
    expect(screen.queryByText(/在线/)).not.toBeInTheDocument();
  });

  it('shows the authorization with validity and does not report success early', async () => {
    let progress!: (event: AccountProgress) => void;
    let finish!: (result: { status: 'authenticated' }) => void;
    const actions = makeActions({
      authenticateAccount: () => new Promise((resolve) => { finish = resolve; }),
      subscribeAccountProgress: (listener) => {
        progress = listener;
        return () => {};
      },
    });
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions} />);
    await startLogin(user);

    act(() => progress({
      event: 'device',
      verificationUri: 'https://auth.example.invalid/device',
      userCode: 'BIND',
      expiresIn: 600,
      interval: 5,
    }));
    expect(screen.getByText(/请在浏览器完成登录或注册/))
      .toBeInTheDocument();
    expect(screen.getByText('授权码：BIND')).toBeInTheDocument();
    expect(screen.getByText(/授权剩余约 10 分 0 秒/)).toBeInTheDocument();
    expect(screen.queryByText(/轮询间隔/)).not.toBeInTheDocument();

    act(() => progress({ event: 'authenticated' }));
    expect(screen.getByText(/正在确认登录结果/))
      .toBeInTheDocument();
    expect(screen.queryByLabelText(/^账号：.*账号已登录$/)).not.toBeInTheDocument();

    act(() => progress({ event: 'committed' }));
    expect(screen.getByText(/正在确认登录结果/))
      .toBeInTheDocument();
    expect(screen.queryByLabelText(/^账号：.*账号已登录$/)).not.toBeInTheDocument();

    await act(async () => finish({ status: 'authenticated' }));
    expect(
      await screen.findByLabelText('账号：账号已登录'),
    ).toBeInTheDocument();
  });

  it('cancels the authorization and ignores its late result', async () => {
    let progress!: (event: AccountProgress) => void;
    let finish!: (result: { status: 'authenticated' }) => void;
    const cancelAccount = vi.fn(async () => ({ ok: true, cancelled: true }));
    const actions = makeActions({
      cancelAccount,
      getAccountState: async () => ({
        account: { status: 'unauthenticated' },
        device: { status: 'not-ready' },
      }),
      authenticateAccount: () => new Promise((resolve) => { finish = resolve; }),
      subscribeAccountProgress: (listener) => {
        progress = listener;
        return () => {};
      },
    });
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions} />);
    await startLogin(user);

    act(() => progress({
      event: 'device',
      verificationUri: 'https://auth.example.invalid/device',
      userCode: 'BIND',
    }));
    await user.click(screen.getByRole('button', { name: '取消登录' }));
    expect(cancelAccount).toHaveBeenCalledTimes(1);
    expect(screen.queryByText('授权码：BIND')).not.toBeInTheDocument();

    await act(async () => finish({ status: 'authenticated' }));
    expect(screen.queryByLabelText(/^账号：.*账号已登录$/)).not.toBeInTheDocument();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
  });

  it('keeps an unknown event in a safe degraded state and then reports failure', async () => {
    let progress!: (event: AccountProgress) => void;
    let finish!: (result: { status: 'failed'; message: string }) => void;
    const actions = makeActions({
      authenticateAccount: () => new Promise((resolve) => { finish = resolve; }),
      subscribeAccountProgress: (listener) => {
        progress = listener;
        return () => {};
      },
    });
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions} />);
    await startLogin(user);

    act(() => progress({
      event: 'unknown',
      token: 'SECRET-TOKEN',
    } as AccountProgress));
    expect(screen.getByText(/收到未识别的登录进度/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/^账号：.*账号已登录$/)).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain('SECRET-TOKEN');

    await act(async () => finish({
      status: 'failed',
      message: '授权未完成。',
    }));
    expect((await screen.findAllByText('登录失败')).length).toBeGreaterThan(0);
    expect(await screen.findByText('登录进度未识别')).toBeInTheDocument();
    expect(screen.queryByLabelText(/^账号：.*账号已登录$/)).not.toBeInTheDocument();
  });

  it('offers a single safe link for authorization without a scripted popup', () => {
    render(<AccountProgressPanel active authorizing onCancel={vi.fn()} onRetry={vi.fn()}
      progress={{ event: 'device', verificationUri: 'https://auth.example.invalid/device',
        userCode: 'BIND', expiresIn: 600 }} />);
    const link = screen.getByRole('link', { name: '打开授权页' });
    expect(link).toHaveAttribute('href', 'https://auth.example.invalid/device');
    expect(link).toHaveAttribute('target', '_blank');
    expect(link).toHaveAttribute('rel', 'noopener noreferrer');
    expect(screen.queryByRole('button', { name: '打开授权页' })).not.toBeInTheDocument();
    expect(window.open).not.toHaveBeenCalled();
  });

  it('shows elapsed waiting, preserves authorization, and offers retry only after timeout', async () => {
    vi.useFakeTimers();
    const onCancel = vi.fn();
    const onRetry = vi.fn();
    render(
      <AccountProgressPanel
        progress={{
          event: 'device',
          verificationUri: 'https://auth.example.invalid/device',
          userCode: 'WAIT',
          expiresIn: 300,
        }}
        active
        authorizing
        onCancel={onCancel}
        onRetry={onRetry}
      />,
    );

    expect(screen.getByText('请在浏览器完成登录或注册。')).toBeInTheDocument();
    expect(screen.queryByText(/已等待 0 秒/)).not.toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LONG_WAIT_SECONDS * 1000);
    });
    expect(screen.getByText(/仍在等待登录结果/)).toBeInTheDocument();
    expect(screen.getByText('授权码：WAIT')).toBeInTheDocument();
    expect(screen.queryByText(/正在保存|登录成功|账号已登录/)).not.toBeInTheDocument();
    expect(onRetry).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(
        (WAIT_TIMEOUT_SECONDS - LONG_WAIT_SECONDS) * 1000,
      );
    });
    expect(screen.getByText(/等待已超过 90 秒/)).toBeInTheDocument();
    expect(screen.getByText('授权码：WAIT')).toBeInTheDocument();
    expect(onRetry).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '重试登录' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: '取消登录' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
