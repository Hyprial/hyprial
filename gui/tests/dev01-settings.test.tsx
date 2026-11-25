import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../src/App';
import { type FirstRunActions } from '../src/features/onboarding';
import { stubFirstRunActions } from './support/first-run-actions';

function actions(overrides: Partial<FirstRunActions> = {}): FirstRunActions {
  return { ...stubFirstRunActions(), getAccountState: vi.fn().mockResolvedValue({ account: { status: 'unauthenticated' }, device: { status: 'not-ready' } }),
    getSetupState: vi.fn().mockResolvedValue({ keySaved: false, pendingAction: null }), ...overrides };
}
async function openSettings(firstRun = actions()) {
  const user = userEvent.setup();  render(<App capabilities={{ firstRun }} />);
  await user.click(screen.getByRole('button', { name: '我的账号与 Agent 设置菜单' }));
  await user.click(screen.getByRole('menuitem', { name: '用户设置' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '刷新账号状态' })).toBeEnabled());
  return { user, firstRun };
}

describe('settings account and user capabilities', () => {
  it('reads public account state without starting or granting an agent', async () => {
    const saveModelKey = vi.fn();
    const firstRun = actions({ getAccountState: vi.fn().mockResolvedValue({ account: { status: 'authenticated', owner: 'person@example.invalid' }, device: { status: 'ready' } }), saveModelKey });
    await openSettings(firstRun);
    expect(screen.getByText('person@example.invalid')).toBeInTheDocument();
    expect(screen.queryByLabelText(/访问令牌/)).not.toBeInTheDocument();
    expect(saveModelKey).not.toHaveBeenCalled();
  });
  it('shows confirmed signed-out state without redundant account copy', async () => {
    await openSettings(actions({ getAccountState: vi.fn().mockResolvedValue({ account: { status: 'unauthenticated', message: '账号状态尚未确认。' }, device: { status: 'not-ready' } }) }));
    expect(screen.getByText('未登录')).toBeInTheDocument();
    expect(screen.queryByText('账号状态尚未确认。')).not.toBeInTheDocument();
  });
  it('logs in through official actions instead of collecting a token or invoking stub commands', async () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    const getAccountState = vi.fn().mockResolvedValueOnce({ account: { status: 'unauthenticated' }, device: { status: 'not-ready' } })
      .mockResolvedValue({ account: { status: 'authenticated', owner: 'signed-in' }, device: { status: 'not-ready' } });
    const authenticateAccount = vi.fn().mockResolvedValue({ status: 'authenticated' });
    const { user } = await openSettings(actions({ getAccountState, authenticateAccount }));
    await user.click(screen.getByRole('button', { name: '登录 Hyprial' }));
    expect(await screen.findByText('signed-in')).toBeInTheDocument();
    expect(authenticateAccount).toHaveBeenCalledWith({ account: '', token: '' }, 'login');
  });
  it('reports unavailable account capability without falling back to simulation', async () => {
    await openSettings(actions({ getAccountState: vi.fn().mockRejectedValue(new Error('offline')) }));
    expect(screen.getByText('账号服务不可用，请检查本机服务后刷新状态。')).toBeInTheDocument();
    expect(screen.queryByText(/隔离预览场景/)).not.toBeInTheDocument();
  });
});
