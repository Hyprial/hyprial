import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { GuiCapabilitiesProvider } from '../src/app/capabilities';
import { StartPage } from '../src/pages/StartPage';
import { appReducer, createEmptyState } from '../src/state/appReducer';
import type { AccountSnapshot, FirstRunActions, FirstRunAttemptView, FirstRunReceiptView } from '../src/features/onboarding';
import { stubFirstRunActions } from './support/first-run-actions';

const RECORDED_BOTH: FirstRunReceiptView = {
  state: 'recorded',
  revision: 1,
  confirmed: ['configuration', 'reply'],
  updatedAt: '2026-11-18T00:00:00Z',
};

function accountActions(snapshot: AccountSnapshot): FirstRunActions {
  return stubFirstRunActions({ getAccountState: async () => snapshot });
}

function pendingSnapshot(overrides: Partial<AccountSnapshot> = {}): AccountSnapshot {
  return {
    account: { status: 'authenticated', owner: 'alice' },
    device: { status: 'ready' },
    firstRun: { state: 'none', revision: 0, confirmed: [], updatedAt: null },
    ...overrides,
  };
}

describe('the Start-page first-run entry', () => {
  it('returns to Start when the legacy flow is already complete', () => {
    const state = { ...createEmptyState('complete'), page: 'first-run' as const };
    const completed = appReducer(state, { type: 'first-run-complete' });
    expect(completed.page).toBe('start');
    expect(completed.firstRun.status).toBe('complete');
    expect(appReducer(completed, { type: 'first-run-complete' })).toBe(completed);
  });

  it('returns to Start and reads the durable receipt after confirming a resumed reply', async () => {
    const bound: FirstRunAttemptView = {
      attemptId: 'attempt-1', operationKey: 'operation-1', sendKey: 'send-1',
      revision: 2, state: 'bound', receiptRevision: 0, loginEpoch: 1,
      agentUri: 'agent:alice:node:first-agent', agentCreatedAtMs: 1760000000000,
      provider: 'deepseek', model: 'deepseek-flash', harness: 'pi',
      configRevision: null, credentialKind: 'key', requestMessageId: 'request-1',
      conversationId: 'conversation-1', replyMessageId: null,
    };
    const confirmed = { ...bound, state: 'confirmed' as const, revision: 3, replyMessageId: 'reply-1' };
    let snapshot = pendingSnapshot({ firstRunAttempt: bound });
    const getAccountState = vi.fn(async () => snapshot);
    render(<App capabilities={{ firstRunWizardEnabled: true, firstRun: {
      getAccountState,
      replyConfirmation: {
        begin: vi.fn(), send: vi.fn(), cancel: vi.fn(),
        history: async () => [{ id: 'reply-1', author: 'agent', text: 'Current reply' }],
        confirm: async () => {
          snapshot = pendingSnapshot({ firstRun: RECORDED_BOTH, firstRunAttempt: confirmed });
          return { status: 'confirmed', attempt: confirmed, firstRun: RECORDED_BOTH };
        },
      },
    }}} />);
    fireEvent.click(await screen.findByRole('button', { name: '继续首次设置' }));
    const confirm = await screen.findByRole('button', { name: '我已收到真实回复，确认完成' });
    const reads = getAccountState.mock.calls.length;
    fireEvent.click(confirm);
    await screen.findByRole('heading', { level: 1, name: '开始' });
    await waitFor(() => expect(getAccountState.mock.calls.length).toBeGreaterThan(reads));
    await waitFor(() => expect(document.querySelector('[data-start-block="first-run"]')).toBeNull());
  });

  it('renders the resume control only when the composition marks the setup pending', () => {
    const onResume = vi.fn();
    const view = render(
      <GuiCapabilitiesProvider value={{ firstRun: stubFirstRunActions() }}>
        <StartPage state={createEmptyState()} dispatch={vi.fn()} />
      </GuiCapabilitiesProvider>,
    );
    expect(screen.queryByRole('button', { name: '继续首次设置' })).not.toBeInTheDocument();
    view.rerender(
      <GuiCapabilitiesProvider value={{ firstRun: stubFirstRunActions() }}>
        <StartPage state={createEmptyState()} dispatch={vi.fn()}
          firstRunPending onResumeFirstRun={onResume} />
      </GuiCapabilitiesProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: '继续首次设置' }));
    expect(onResume).toHaveBeenCalledTimes(1);
    expect(document.querySelector('[data-start-block="first-run"]')).toBeInTheDocument();
  });

  it('navigates into the wizard from the live Start page while the receipt is incomplete', async () => {
    render(<App capabilities={{
      firstRun: accountActions(pendingSnapshot()),
      firstRunWizardEnabled: true,
    }} />);
    const resume = await screen.findByRole('button', { name: '继续首次设置' });
    fireEvent.click(resume);
    await waitFor(() => expect(document.querySelector('[data-page="first-run"]')).not.toBeNull());
    expect(screen.queryByRole('button', { name: '继续首次设置' })).not.toBeInTheDocument();
  });

  it('keeps the entry for a recorded receipt that only confirms configuration', async () => {
    render(<App capabilities={{
      firstRun: accountActions(pendingSnapshot({
        firstRun: { ...RECORDED_BOTH, confirmed: ['configuration'] },
      })),
      firstRunWizardEnabled: true,
    }} />);
    expect(await screen.findByRole('button', { name: '继续首次设置' })).toBeInTheDocument();
  });

  it('drops the entry once the receipt records both configuration and reply', async () => {
    render(<App capabilities={{
      firstRun: accountActions(pendingSnapshot({ firstRun: RECORDED_BOTH })),
      firstRunWizardEnabled: true,
    }} />);
    await screen.findByRole('heading', { level: 1, name: '开始' });
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '继续首次设置' })).not.toBeInTheDocument());
  });

  it('does not offer the entry when the wizard switch is off', async () => {
    render(<App capabilities={{
      firstRun: accountActions(pendingSnapshot()),
      firstRunWizardEnabled: false,
    }} />);
    await screen.findByRole('heading', { level: 1, name: '开始' });
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: '继续首次设置' })).not.toBeInTheDocument());
  });
});
