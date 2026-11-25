/*
 * Decision record (unavailable copy to notifications, PR 1f/F6, 2026-11-15):
 * the first-run wizard renders a control only when its port exists. A
 * composition without a port shows no control and no explanation; a login
 * refused before dispatch raises one toast, opens no window and leaves the
 * wizard as it was. Choices where the plan was silent: a step whose only
 * control is absent renders its title and status cards only; the model step
 * without a save port shows no form; the conversation step without a chat
 * runtime renders its title only.
 */
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FirstRunOnboarding, firstRunWizardOffered, type FirstRunActions, type FirstRunStepId } from '../../../src/features/onboarding';
import { expectUnavailableToast, renderWithNotifications } from '../../support/notify-harness';
import { stubFirstRunActions } from '../../support/first-run-actions';

const WORDING = /暂不|暂未|不可用|不支持|未接[通线入]|尚未(?:开放|接入|接通)/;
const REFUSAL = { code: 'METHOD_NOT_FOUND', source: 'auth.login.begin' } as const;

function region(id: FirstRunStepId): HTMLElement {
  return document.querySelector<HTMLElement>(`[data-step="${id}"]`)!;
}

const authenticated: FirstRunActions = {
  getAccountState: async () => ({ account: { status: 'authenticated', owner: 'o@example.invalid' }, device: { status: 'ready' } }),
};

async function toModel(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: '继续到设备' }));
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
}

describe('first-run wizard without ports', () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it('renders no login control and no wording when the composition has no actions', () => {
    render(<FirstRunOnboarding />);
    expect(screen.queryByRole('button', { name: '登录 Hyprial' })).toBeNull();
    expect(document.body.textContent).not.toMatch(WORDING);
  });

  it('uses the explicit skip shortcut without claiming the four steps were confirmed', async () => {
    const user = userEvent.setup();
    const onComplete = vi.fn();
    const skipFirstRun = vi.fn(async () => ({
      status: 'skipped' as const,
      firstRun: {
        state: 'recorded' as const,
        revision: 1,
        confirmed: [],
        updatedAt: '2026-11-19T00:00:00Z',
        skipped: true,
      },
    }));
    render(<FirstRunOnboarding actions={{ skipFirstRun }} onComplete={onComplete} />);
    await user.click(screen.getByTestId('first-run-skip'));
    expect(skipFirstRun).toHaveBeenCalledWith(0);
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: '跳过首次设置' })).toBeEnabled();
  });

  it('offers no device initialization without its port', async () => {
    const actions = stubFirstRunActions({
      getAccountState: async () => ({ account: { status: 'authenticated' }, device: { status: 'not-ready' } }),
    });
    render(<FirstRunOnboarding actions={actions} />);
    await screen.findByText('账号已登录');
    expect(screen.queryByRole('button', { name: '初始化当前设备' })).toBeNull();
    expect(document.body.textContent).not.toMatch(WORDING);
  });

  it('shows no save form on the model step without a save port', async () => {
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={stubFirstRunActions(authenticated)} />);
    await toModel(user);
    expect(await screen.findByRole('heading', { name: '模型服务' })).toBeInTheDocument();
    expect(screen.queryByLabelText(/DeepSeek API Key/)).toBeNull();
    expect(screen.queryByRole('button', { name: '保存并继续' })).toBeNull();
    expect(document.body.textContent).not.toMatch(WORDING);
  });

  it('requires only account, device, and a runtime port for firstRunWizardOffered', () => {
    const auth = vi.fn();
    const device = vi.fn();
    expect(firstRunWizardOffered(undefined)).toBe(false);
    expect(firstRunWizardOffered({ authenticateAccount: auth, initializeDevice: device })).toBe(false);
    expect(firstRunWizardOffered({ authenticateAccount: auth, saveModelKey: vi.fn() })).toBe(false);
    expect(firstRunWizardOffered({ initializeDevice: device, saveModelKey: vi.fn() })).toBe(false);
    expect(firstRunWizardOffered({
      authenticateAccount: auth,
      initializeDevice: device,
      saveModelKey: vi.fn(),
    })).toBe(true);
    expect(firstRunWizardOffered({
      authenticateAccount: auth,
      initializeDevice: device,
      configureAgent: vi.fn(),
    })).toBe(true);
  });

  it('opens no window and raises one toast when the login is refused before dispatch', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    const authenticateAccount = vi.fn();
    const actions = stubFirstRunActions({ subscribeAccountProgress: () => () => undefined, loginRefusal: () => REFUSAL, authenticateAccount });
    renderWithNotifications(<FirstRunOnboarding actions={actions} />);
    await userEvent.setup().click(screen.getByRole('button', { name: '登录 Hyprial' }));
    expect(open).not.toHaveBeenCalled();
    expect(authenticateAccount).not.toHaveBeenCalled();
    expectUnavailableToast('登录 Hyprial暂不可用。');
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
    expect(region('account').querySelectorAll('.ui-notice')).toHaveLength(0);
  });

  it('raises one toast and restores the idle state when the login answer is a refusal', async () => {
    const actions = stubFirstRunActions({
      authenticateAccount: vi.fn(async () => ({ status: 'unavailable' as const, refusal: REFUSAL })),
    });
    renderWithNotifications(<FirstRunOnboarding actions={actions} />);
    await userEvent.setup().click(screen.getByRole('button', { name: '登录 Hyprial' }));
    await waitFor(() => expectUnavailableToast('登录 Hyprial暂不可用。'));
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
    expect(region('account').textContent).not.toMatch(WORDING);
  });

  it('raises one toast when device initialization answers with a refusal', async () => {
    const user = userEvent.setup();
    const actions = stubFirstRunActions({
      getAccountState: async () => ({ account: { status: 'authenticated' }, device: { status: 'not-ready' } }),
      initializeDevice: vi.fn(async () => ({ status: 'unavailable' as const, refusal: { code: 'METHOD_NOT_FOUND', source: 'onboarding.initialize' } })),
    });
    renderWithNotifications(<FirstRunOnboarding actions={actions} />);
    await user.click(await screen.findByRole('button', { name: '继续到设备' }));
    await user.click(await screen.findByRole('button', { name: '初始化当前设备' }));
    await waitFor(() => expectUnavailableToast('初始化当前设备暂不可用。'));
    expect(screen.getByRole('button', { name: '初始化当前设备' })).toBeEnabled();
  });

  it('renders no notice or card for an unavailable chat and no control without its handler', () => {
    const { rerender } = render(<FirstRunOnboarding actions={stubFirstRunActions()} chatStatus="unavailable" />);
    expect(document.body.textContent).not.toMatch(WORDING);
    rerender(<FirstRunOnboarding actions={stubFirstRunActions()} chatStatus="ready" />);
    expect(screen.queryByRole('button', { name: '打开聊天' })).toBeNull();
    rerender(<FirstRunOnboarding actions={stubFirstRunActions()} chatStatus="completed" />);
    expect(screen.queryByRole('button', { name: '进入开始页' })).toBeNull();
  });
});
