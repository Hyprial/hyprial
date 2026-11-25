import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { createBackendConnection } from '../src/app/backend';
import type { AccountSnapshot, FirstRunActions } from '../src/features/onboarding';

const receipt = { state: 'none' as const, revision: 0, confirmed: [], updatedAt: null };
const ready: AccountSnapshot = {
  account: { status: 'authenticated', owner: 'alice' }, device: { status: 'ready' }, firstRun: receipt,
};
const connection = createBackendConnection({ invoke: vi.fn() }, {
  interfaceVersion: 1, driver: 'daemon', phase: 'ready', generation: 'g1', catalogDigest: 'catalog', methods: [],
});
const backend = { status: 'connected' as const, connection };
function actions(snapshot: AccountSnapshot): FirstRunActions {
  return { getAccountState: vi.fn(async () => snapshot), configureAgent: vi.fn(), initializeDevice: vi.fn() };
}
const currentStep = () => document.querySelector('[aria-label="首次设置步骤"] [aria-current="step"]')?.getAttribute('data-step-nav');

describe('connected first-run routing from authoritative status', () => {
  it('waits for status instead of showing a completed home while the read is pending', async () => {
    let resolve!: (value: AccountSnapshot) => void;
    render(<App backend={backend} capabilities={{ firstRunWizardEnabled: true, firstRun: {
      getAccountState: () => new Promise<AccountSnapshot>(done => { resolve = done; }),
    } }} />);
    expect(await screen.findByText('正在读取首次设置状态')).toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 1, name: '开始' })).not.toBeInTheDocument();
    resolve({ ...ready, firstRun: { state: 'recorded', revision: 1, confirmed: ['configuration', 'reply'], updatedAt: '2026-11-18T00:00:00Z' } });
    await screen.findByRole('heading', { level: 1, name: '开始' });
  });

  it('keeps a failed status read in recovery rather than treating it as a fresh account', async () => {
    render(<App backend={backend} capabilities={{ firstRunWizardEnabled: true, firstRun: {
      getAccountState: async () => { throw new Error('offline'); },
    } }} />);
    await screen.findByRole('heading', { level: 1, name: '首次设置' });
    expect(screen.queryByRole('heading', { level: 1, name: '开始' })).not.toBeInTheDocument();
    await waitFor(() => expect(currentStep()).toBe('account'));
    expect(screen.queryByRole('button', { name: '创建 Agent' })).not.toBeInTheDocument();
  });

  it.each([
    ['signed out', { ...ready, account: { status: 'unauthenticated' as const } }, 'account'],
    ['new device', { ...ready, device: { status: 'not-ready' as const } }, 'device'],
    ['initialized legacy device without a receipt', ready, 'runtime'],
  ])('automatically resumes %s without submitting a write', async (_name, snapshot, step) => {
    const ports = actions(snapshot);
    render(<App backend={backend} capabilities={{ firstRunWizardEnabled: true, firstRun: ports }} />);
    await screen.findByRole('heading', { level: 1, name: '首次设置' });
    await waitFor(() => expect(currentStep()).toBe(step));
    expect(ports.initializeDevice).not.toHaveBeenCalled();
    expect(ports.configureAgent).not.toHaveBeenCalled();
  });

  it('returns a completed account and device to Start, including a remembered wizard route', async () => {
    render(<App backend={backend} initialPage="first-run" capabilities={{ firstRunWizardEnabled: true, firstRun: actions({
      ...ready, firstRun: { state: 'recorded', revision: 2, confirmed: ['configuration', 'reply'], updatedAt: '2026-11-18T00:00:00Z' },
    }) }} />);
    await screen.findByRole('heading', { level: 1, name: '开始' });
    expect(screen.queryByRole('heading', { name: '首次设置' })).not.toBeInTheDocument();
  });

  it.each(['corrupt', 'unsupported-version', 'unavailable'] as const)('retains %s as recovery, never starts configuration', async state => {
    const ports = actions({ ...ready, firstRun: { ...receipt, state } });
    render(<App backend={backend} capabilities={{ firstRunWizardEnabled: true, firstRun: ports }} />);
    await screen.findByText('首次设置记录需要核对');
    expect(screen.queryByRole('heading', { level: 1, name: '开始' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '创建 Agent' })).not.toBeInTheDocument();
    expect(ports.configureAgent).not.toHaveBeenCalled();
  });

  it('does not trap an explicit return to settings and resets progress for a different account', async () => {
    const view = render(<App backend={backend} capabilities={{ firstRunWizardEnabled: true, firstRun: actions(ready) }} />);
    await screen.findByRole('heading', { level: 1, name: '首次设置' });
    fireEvent.click(screen.getByRole('button', { name: '返回设置' }));
    await waitFor(() => expect(screen.queryByRole('heading', { level: 1, name: '首次设置' })).not.toBeInTheDocument());
    view.rerender(<App backend={backend} capabilities={{ firstRunWizardEnabled: true, firstRun: actions({ ...ready,
      account: { status: 'authenticated', owner: 'bob' }, device: { status: 'not-ready' },
    }) }} />);
    await screen.findByRole('heading', { level: 1, name: '首次设置' });
    await waitFor(() => expect(currentStep()).toBe('device'));
  });
});
