import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import type { FirstRunMode } from '../src/state/types';
import { useChatContractRuntime } from '../src/dev/mock/use-contract-runtime';
import { createFixtureFirstRunActions } from '../src/dev/first-run-capabilities';
import { loginPreview, preparePreview } from './support/first-run-journey';
import { demoFixtureCapabilities, demoFixtureState } from './support/demo-baseline';

/**
 * Mirrors the fixture composition root: it owns the first-run chat runtime
 * selection (conversation-failure maps to the failure contract), builds the
 * fixture capability set around this app's dispatch and injects the fixture
 * seed built by the shared createFixtureState. A live or product composition
 * injects none of these.
 */
function FirstRunJ1App({ mode, chatScenario }: {
  readonly mode: FirstRunMode;
  readonly chatScenario?: 'success' | 'failure';
}) {
  const effective = chatScenario ?? (mode === 'conversation-failure' ? 'failure' : undefined);
  const firstRunChat = useChatContractRuntime(effective);
  return (
    <App
      initialState={demoFixtureState(mode)}
      firstRunChat={firstRunChat}
      capabilitiesFactory={(dispatch) => demoFixtureCapabilities(dispatch, { firstRunMode: mode })}
    />
  );
}

function renderFirstRun(mode: FirstRunMode = 'simulate', chatScenario?: 'success' | 'failure') {
  render(<FirstRunJ1App mode={mode} chatScenario={chatScenario} />);
}
afterEach(() => vi.unstubAllGlobals());

describe('one first-run view through the App entry', () => {
  it('sends the first message over the chat contract and auto-enters the start page without host calls', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    const open = vi.spyOn(window, 'open');
    const user = userEvent.setup();
    renderFirstRun('simulate', 'success');
    expect(screen.getByRole('heading', { name: '登录' })).toBeInTheDocument();
    expect(document.querySelector('.dev01-v17-onboarding')).not.toBeNull();
    expect(screen.queryByLabelText('访问令牌')).toBeNull();
    expect(screen.queryByLabelText('账号')).toBeNull();
    expect(screen.queryByRole('navigation', { name: '主导航' })).toBeNull();
    await preparePreview(user);
    expect(screen.getByText('隔离数据来源')).toBeInTheDocument();
    await user.type(screen.getByRole('textbox', { name: '给 Fixture Squire 的消息' }), '第一条真实消息');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByRole('navigation', { name: '主导航' });
    expect(screen.getByRole('heading', { name: '开始', level: 1 })).toBeInTheDocument();
    expect(screen.queryByText(/本地首次设置流程已完成/)).not.toBeInTheDocument();
    expect(fetch).not.toHaveBeenCalled(); expect(open).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
  });
  it('keeps failure/retry in the shared account view', async () => {
    const user = userEvent.setup(); renderFirstRun('account-failure');
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('模拟：账号授权失败');
    await user.click(screen.getByRole('button', { name: '重试登录' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('模拟：账号授权失败');
    expect(screen.queryByLabelText('访问令牌')).toBeNull();
  });
  it.each(['model-failure', 'model-unavailable'] as const)('preserves %s without entering conversation', async mode => {
    const user = userEvent.setup(); renderFirstRun(mode); await loginPreview(user);
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'preview-key');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    await screen.findByText('Key 保存未确认');
    expect(screen.getByRole('heading', { name: '模型服务' })).toBeInTheDocument();
    expect(screen.getByLabelText(/DeepSeek API Key/)).toHaveValue('');
  });
  it('keeps the draft on a failed send without completing first-run', async () => {
    const user = userEvent.setup(); renderFirstRun('conversation-failure'); await preparePreview(user);
    const composer = screen.getByRole('textbox', { name: '给 Fixture Squire 的消息' });
    await user.type(composer, 'keep draft');
    await user.click(screen.getByRole('button', { name: '发送' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('发送失败');
    expect(composer).toHaveValue('keep draft');
    expect(screen.queryByRole('navigation', { name: '主导航' })).toBeNull();
  });
  it('does not replace the shared view when the service is unavailable', async () => {
    const user = userEvent.setup(); renderFirstRun('unavailable');
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    await screen.findAllByText('账号服务不可用');
    expect(screen.getByRole('heading', { name: '登录' })).toBeInTheDocument();
  });
  it('cancels an in-flight mock request without accepting a late success', async () => {
    const dispatch = vi.fn(); const actions = createFixtureFirstRunActions('simulate', dispatch);
    const pending = actions.authenticateAccount!({ account: '', token: '' }, 'login');
    const rejected = expect(pending).rejects.toThrow('cancelled');
    expect(await actions.cancelAccount?.()).toEqual({ ok: true, cancelled: true });
    await rejected;
    expect((await actions.getAccountState?.())?.account.status).toBe('unauthenticated');
    expect(dispatch).not.toHaveBeenCalled();
  });
});
