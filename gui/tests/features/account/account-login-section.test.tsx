import { act, render, renderHook, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { AccountLoginSection, AccountStatusField } from '../../../src/features/account/impl/AccountLoginSection';
import { useAccountSettings } from '../../../src/features/account/impl/useAccountSettings';
import { stubFirstRunActions } from '../../support/first-run-actions';

it.each([false, true])('reconciles a partial logout without retaining stale identity (read fails: %s)', async readFails => {
  const signedOut = { account: { status: 'unauthenticated' }, device: { status: 'not-ready' } };
  const getAccountState = vi.fn().mockResolvedValue(signedOut)
    .mockResolvedValueOnce({ account: { status: 'authenticated', owner: 'old@example.invalid' }, device: { status: 'ready' } });
  if (readFails) getAccountState.mockRejectedValueOnce(new Error('read unavailable'));
  const logoutAccount = vi.fn().mockResolvedValue({ status: 'failed', nextAction: 'inspect', removed: ['settings.owner'], message: '退出未完成，请重新打开 Hyprial。' });
  const authenticateAccount = vi.fn();
  const actions = { ...stubFirstRunActions(), getAccountState, logoutAccount, authenticateAccount };
  const { result } = renderHook(() => useAccountSettings(actions));
  await waitFor(() => expect(result.current.snapshot.account.status).toBe('authenticated'));
  await act(() => result.current.logout());
  expect(getAccountState).toHaveBeenCalledTimes(2);
  expect(result.current.snapshot.account.status).toBe(readFails ? 'unknown' : 'unauthenticated');
  expect(result.current.canLogout).toBe(false);
  await act(() => result.current.run('read'));
  expect(result.current.logoutState?.removed).toEqual(['settings.owner']);
  await act(() => result.current.retry());
  await act(() => result.current.logout());
  expect(logoutAccount).toHaveBeenCalledOnce();
  expect(authenticateAccount).not.toHaveBeenCalled();
  render(<AccountLoginSection state={result.current} />);
  expect(screen.queryByText('old@example.invalid')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeDisabled();
  expect(screen.getByText('退出未完成，请重新打开 Hyprial。')).toBeInTheDocument();
});

function sectionState(account: ReturnType<typeof useAccountSettings>['snapshot']['account']) {
  return {
    snapshot: { account, device: { status: 'not-ready' as const } },
    busy: null,
    statusLoaded: true,
    progress: null,
    run: vi.fn(),
    cancel: vi.fn(),
    retry: vi.fn(),
    logout: vi.fn(), logoutState: null, canLogout: true, hasLogout: true, hasLogin: true, hasDeviceInit: true,
  } satisfies ReturnType<typeof useAccountSettings>;
}

describe('AccountStatusField', () => {
  it('shows only the account address after login', () => {
    const { container } = render(<AccountStatusField value={{ status: 'authenticated', owner: 'owner@example.invalid', message: '已确认账号登录。' }} />);
    expect(container.textContent).toBe('owner@example.invalid');
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
  });

  it('prefers username over owner when displaying an authenticated account', () => {
    const { container } = render(<AccountStatusField value={{ status: 'authenticated', owner: 'legacy-owner', username: 'renamed-user', message: '已确认账号登录。' }} />);
    expect(container.textContent).toBe('renamed-user');
    expect(screen.queryByText('legacy-owner')).not.toBeInTheDocument();
  });

  it('shows loading instead of stale account content', () => {
    render(<AccountStatusField value={{ status: 'authenticated', owner: 'old@example.invalid' }} loading />);
    expect(screen.getByText('正在读取账号状态')).toBeInTheDocument();
    expect(screen.queryByText(/old@example.invalid/)).not.toBeInTheDocument();
  });

  it('shows the unauthenticated empty state', () => {
    render(<AccountStatusField value={{ status: 'unauthenticated' }} />);
    expect(screen.getByRole('status')).toHaveTextContent('未登录');
    expect(screen.getAllByText('未登录')).toHaveLength(1);
  });

  it('shows a failed status once', () => {
    render(<AccountStatusField value={{ status: 'failed', message: '授权失败' }} />);
    expect(screen.getByText('授权失败')).toBeInTheDocument();
  });
});

describe('AccountLoginSection', () => {
  it('shows loading while the account status action is pending', () => {
    const state = { ...sectionState({ status: 'unknown', message: '旧状态' }), busy: 'read' as const };
    render(<AccountLoginSection state={state} />);
    expect(screen.getByText('正在读取账号状态')).toBeInTheDocument();
    expect(screen.queryByText('旧状态')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新账号状态' })).toBeDisabled();
  });

  it('shows the logged-out empty state with an available login action', () => {
    render(<AccountLoginSection state={sectionState({ status: 'unauthenticated' })} />);
    expect(screen.getByRole('status')).toHaveTextContent('未登录');
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
  });

  it('does not duplicate an unavailable error message', () => {
    render(<AccountLoginSection state={sectionState({ status: 'unavailable', message: '账号服务不可用' })} />);
    expect(screen.getByText('账号服务不可用')).toBeInTheDocument();
  });

  it('keeps login and refresh operable and forwards the click', async () => {
    const state = sectionState({ status: 'unauthenticated' });
    const user = userEvent.setup();
    render(<AccountLoginSection state={state} />);
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '刷新账号状态' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '登录 Hyprial' }));
    expect(state.retry).toHaveBeenCalledTimes(1);
  });
});


describe('account card de-duplication', () => {
  it.each([
    { status: 'unauthenticated' as const, message: '账号状态尚未确认。' },
    { status: 'unknown' as const, message: '无法读取当前登录状态，请重试登录。' },
  ])('keeps one status card and no duplicate notice for $status', account => {
    const { container } = render(<AccountLoginSection state={sectionState(account)} />);
    const card = container.querySelector('section.settings-card[aria-label="账号登录"]');
    expect(card).not.toBeNull();
    expect(card!.querySelectorAll('.ui-status-card')).toHaveLength(0);
    expect(card!.querySelectorAll('.ui-notice.ui-notice--warning')).toHaveLength(0);
    expect(card!.querySelector('.ui-notice')).toBeNull();
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '刷新账号状态' })).toBeInTheDocument();
  });

  it('shows the actual reason once without a generic unknown-state card', () => {
    render(<AccountLoginSection state={sectionState({ status: 'unknown', message: '正在等待授权完成，请在授权页继续。' })} />);
    expect(screen.getByRole('status')).toHaveTextContent('正在等待授权完成，请在授权页继续。');
    expect(screen.queryByText('账号状态')).not.toBeInTheDocument();
  });

  it('still shows the logout result notice for an authenticated account', () => {
    const state = { ...sectionState({ status: 'authenticated', owner: 'owner@example.invalid' }),
      logoutState: { status: 'logged-out' as const, message: '本机退出已确认。' } };
    const { container } = render(<AccountLoginSection state={state} />);
    const card = container.querySelector('section.settings-card[aria-label="账号登录"]');
    expect(card!.querySelectorAll('.ui-notice')).toHaveLength(1);
    expect(screen.getByText('本机退出已确认。')).toBeInTheDocument();
  });
});

describe('A7 account section gating', () => {
  it('offers no register control and no fabricated completion', () => {
    render(<AccountLoginSection state={sectionState({ status: 'unknown', message: '授权操作 op-9 仍在进行；请刷新以读取服务端回执。' })} />);
    expect(screen.queryByRole('button', { name: /注册/ })).toBeNull();
    // An unknown receipt is never projected as an authenticated account.
    expect(screen.queryByText(/当前账号：/)).toBeNull();
    expect(screen.getByRole('region', { name: '账号登录' })).toBeInTheDocument();
  });

  it('renders no logout control and no explanation when the client has no logout action', () => {
    const state = sectionState({ status: 'authenticated', owner: 'a7@example.invalid' });
    const { container } = render(<AccountLoginSection state={{ ...state, hasLogout: false }} />);
    expect(screen.queryByRole('button', { name: '退出本机登录' })).not.toBeInTheDocument();
    expect(container.textContent).not.toMatch(/暂不支持|不支持退出/);
  });

  it('locks logout while the previous result is unknown', () => {
    const state = sectionState({ status: 'authenticated', owner: 'a7@example.invalid' });
    render(<AccountLoginSection state={{ ...state, canLogout: false }} />);
    expect(screen.getByRole('button', { name: '退出本机登录' })).toBeDisabled();
  });
});
