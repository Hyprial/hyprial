import { createElement } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DevPreviewApp } from '../../src/dev/DevPreviewApp';
import { createInitialSession } from '../../src/dev/session/initial-session';
import { sessionReducer } from '../../src/dev/session/session-reducer';
import { selectChatTargets } from '../../src/dev/session/session-selectors';

describe('linked session boundaries', () => {
  it('V-11a renders and interacts without requests or storage writes', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const user = userEvent.setup({ delay: null });
    render(createElement(DevPreviewApp));

    await user.click(screen.getByTestId('dev-tools-fab'));
    await user.click(await screen.findByRole('button', { name: '已登录 · init' }));
    await user.click(screen.getByRole('button', { name: '未登录 · init' }));

    expect(fetchSpy).not.toHaveBeenCalled();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    vi.unstubAllGlobals();
  });

  it('V-10 V-11b starts every session and tab from the initial value', () => {
    const first = createInitialSession();
    const second = createInitialSession();
    expect(first.org).toBeNull();
    expect(first.firstRun.status).toBe('in_progress');
    expect(second).toEqual(first);
    expect(first).not.toBe(second);
  });

  it('seeds the generic baseline Squire so chat and the message connection are drivable', () => {
    const session = createInitialSession();
    expect(session.agents).toHaveLength(1);
    expect(session.agents[0]).toMatchObject({
      id: 'agent-squire',
      name: 'Squire',
      modelServiceId: 'model-1',
      state: 'idle',
    });
    expect(selectChatTargets(session)).toHaveLength(1);
    expect(selectChatTargets(session)[0]).toMatchObject({
      id: 'agent-squire',
      availability: 'needs-connection',
    });
  });

  it('keeps the baseline target unavailable until a model service connects', () => {
    const withoutService = createInitialSession();
    expect(withoutService.modelServices).toHaveLength(0);
    expect(selectChatTargets(withoutService)[0].availability).toBe('needs-connection');
    let state = sessionReducer(withoutService, { type: 'first-run-submit', step: 'account' });
    state = sessionReducer(state, { type: 'first-run-succeed', step: 'account' });
    state = sessionReducer(state, { type: 'first-run-submit', step: 'model' });
    state = sessionReducer(state, { type: 'first-run-succeed', step: 'model' });
    expect(state.firstRun.step).toBe(3);
    expect(selectChatTargets(state)[0].availability).toBe('available');
    expect(state.agents).toHaveLength(1);
    expect(state.agents[0].id).toBe('agent-squire');
    expect(state.agents[0].modelServiceId).toBe('model-1');
  });

  it('lets the message connection authorize the baseline agent without a first run', () => {
    const session = createInitialSession();
    const authorized = sessionReducer(session, { type: 'feishu-authorize', agentId: 'agent-squire' });
    expect(authorized.messageConnection).toBe('connected');
    expect(authorized.messageConnectionAgentId).toBe('agent-squire');
    const refused = sessionReducer(session, { type: 'feishu-authorize', agentId: 'agent-missing' });
    expect(refused.messageConnection).toBe('not-connected');
  });
});
