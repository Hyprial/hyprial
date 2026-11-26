import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelStartDialog } from '../../../../src/features/agents/impl/model-configuration/StartDialog';
const status = { binding: { configuration: { harness: 'pi', authKind: 'connection', model: 'hy4-flash' } }, cell: { intentMatches: true }, cleanupPending: false, operation: null };
it('starts the saved binding with trust and cwd only, without provider/model/harness overrides', async () => {
  const start = vi.fn(async () => ({ tone: 'status' as const, text: 'accepted', dispatched: true }));
  const user = userEvent.setup();
  render(<ModelStartDialog actor="actor" status={status as never} api={{ modelStatus: async () => status } as never} cwd="/workspace" onStart={start} onClose={() => {}} onOutcome={() => {}} />);
  expect(screen.queryByLabelText('服务商')).toBeNull();
  expect(screen.getByText(/hy4-flash/)).toBeInTheDocument();
  await user.click(screen.getByRole('checkbox'));
  await user.click(screen.getByRole('button', { name: '启动' }));
  expect(start).toHaveBeenCalledExactlyOnceWith({ cwd: '/workspace', approve: true });
});
it('rechecks and refuses when configuration changes while the dialog is open', async () => {
  const start = vi.fn(); const user = userEvent.setup();
  render(<ModelStartDialog actor="actor" status={status as never} api={{ modelStatus: async () => ({ ...status, cleanupPending: true }) } as never} cwd="/workspace" onStart={start} onClose={() => {}} onOutcome={() => {}} />);
  await user.click(screen.getByRole('checkbox')); await user.click(screen.getByRole('button', { name: '启动' }));
  expect(start).not.toHaveBeenCalled();
  expect(await screen.findByRole('alert')).toHaveTextContent('配置');
});
it('does not dispatch start when the user leaves during preflight readback', async () => {
  let finish!: (value: unknown) => void;
  const read = vi.fn(() => new Promise(resolve => { finish = resolve; }));
  const start = vi.fn();
  const view = render(<ModelStartDialog actor="actor" status={status as never} api={{ modelStatus: read } as never} cwd="/workspace" onStart={start} onClose={() => {}} onOutcome={() => {}} />);
  const user = userEvent.setup(); await user.click(screen.getByRole('checkbox')); await user.click(screen.getByRole('button', { name: '启动' }));
  await waitFor(() => expect(read).toHaveBeenCalledTimes(1));
  view.unmount();
  await act(async () => finish(status));
  expect(start).not.toHaveBeenCalled();
});

it.each([true, false])('keeps explicit stop available only for a confirmed running receipt (%s), without inventing observed state', async running => {
  const { AgentDetail } = await import('../../../../src/features/agents/impl/AgentDetail');
  const { AGENTS_DETAIL_AVAILABLE } = await import('../../../fixtures/agents/agent-detail');
  const action = vi.fn(async (kind: string) => ({ tone: 'status' as const, text: kind, dispatched: true,
    awaitingObservation: kind === 'start', running: kind === 'start' && running }));
  const user = userEvent.setup();
  render(<AgentDetail agent={AGENTS_DETAIL_AVAILABLE} extension={{ runtime: 'unknown', providerGrants: [], capabilityGrants: [], preset: false }}
    organizations={[]} onNavigateConnections={() => {}} onRuntimeAction={action} onRefreshRuntime={() => {}}
    agentsApi={{ modelStatus: async () => status } as never} />);
  await screen.findByText('pi · 连接 / Key · hy4-flash · 后台运行');
  await user.click(screen.getByRole('button', { name: '启动' }));
  await user.type(screen.getByLabelText('工作目录', { exact: true }), '/workspace');
  await user.click(screen.getByRole('checkbox'));
  const { within } = await import('@testing-library/react');
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '启动' }));
  await screen.findByRole('button', { name: '刷新状态' });
  expect(screen.getByTestId('agent-detail-runstate')).toHaveTextContent('状态未确认');
  if (running) {
    await user.click(screen.getByRole('button', { name: '停止' }));
    expect(action.mock.calls.map(call => call[0])).toEqual(['start', 'stop']);
  } else expect(screen.queryByRole('button', { name: '停止' })).toBeNull();
});
