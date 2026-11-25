import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TransportError, type RequestDiagnostics } from '../../../../src/client';
import { DiagnosticSection } from '../../../../src/features/settings/impl/diagnostic/DiagnosticSection';
import { createDiagnosticApi } from '../../../../src/features/settings/impl/diagnostic/api';

const wire = { daemon: { owner: 'private-owner', socket: '/private/socket' }, actors: [{ actor: 'private-actor' }], stranded: [], quota: {}, nowMs: 1000 };
const snapshot = { observedAtMs: 1000, actorCount: 1, strandedCount: 0 };

it('uses generated safe top.snapshot and projects only approved fields', async () => {
  const invoke = vi.fn().mockResolvedValue(wire);
  const signal = new AbortController().signal;
  expect(await createDiagnosticApi({ invoke }).read(signal)).toEqual(snapshot);
  expect(invoke).toHaveBeenCalledWith('top.snapshot', {}, { signal, retry: 'safe' });
});

it.each([{}, { ...wire, actors: [null] }, { ...wire, quota: null }, { ...wire, nowMs: -1 }, { ...wire, nowMs: Infinity }])('refuses invalid result %j', async value => {
  await expect(createDiagnosticApi({ invoke: vi.fn().mockResolvedValue(value) }).read()).rejects.toThrow('DIAGNOSTIC_RESULT_INVALID');
});

it('keeps omitted observation time unknown', async () => {
  const { nowMs: _time, ...withoutTime } = wire;
  expect(await createDiagnosticApi({ invoke: vi.fn().mockResolvedValue(withoutTime) }).read()).toEqual({ ...snapshot, observedAtMs: null });
});

it('shows a bounded summary without raw identity or path information', async () => {
  render(<DiagnosticSection api={createDiagnosticApi({ invoke: vi.fn().mockResolvedValue(wire) })} />);
  expect((await screen.findByText('Agent 观测条目')).parentElement).toHaveTextContent('Agent 观测条目1');
  expect(screen.getByText('滞留收件人条目').parentElement).toHaveTextContent('滞留收件人条目0');
  expect(document.body.textContent).not.toMatch(/private-owner|private-actor|private\/socket/);
  expect(screen.queryByText(/当前实例的在线观测快照/)).not.toBeInTheDocument();
});

it('supports empty observations and manual retry after failure', async () => {
  const read = vi.fn().mockRejectedValueOnce(new Error('private error')).mockResolvedValue({ observedAtMs: null, actorCount: 0, strandedCount: 0 });
  render(<DiagnosticSection api={{ read }} />);
  expect(await screen.findByText('运行诊断查询失败')).toBeInTheDocument();
  expect(screen.queryByText('private error')).not.toBeInTheDocument();
  await userEvent.setup().click(screen.getByRole('button', { name: '刷新运行诊断' }));
  expect((await screen.findByText('Agent 观测条目')).parentElement).toHaveTextContent('Agent 观测条目0');
  expect(screen.getByText('观测时间').parentElement).toHaveTextContent('—');
  expect(read).toHaveBeenCalledTimes(2);
});

it('shows unknown dispatch without an automatic component retry', async () => {
  const diagnostics: RequestDiagnostics = { method: 'top.snapshot', requestId: null, status: null, dispatch: 'unknown', catalogDigest: null, attempts: 1, startedAt: 0, durationMs: 1 };
  const read = vi.fn().mockRejectedValue(new TransportError('network', 'REQUEST_FAILED', 'private detail', diagnostics));
  render(<DiagnosticSection api={{ read }} />);
  expect(await screen.findByText('运行诊断结果未知')).toBeInTheDocument();
  expect(read).toHaveBeenCalledTimes(1);
});

it('aborts and discards a superseded capability result', async () => {
  let resolve!: (value: typeof snapshot) => void;
  const first = { read: vi.fn((_signal?: AbortSignal) => new Promise<typeof snapshot>(done => { resolve = done; })) };
  const second = { read: vi.fn().mockResolvedValue({ ...snapshot, actorCount: 2 }) };
  const view = render(<DiagnosticSection api={first} />);
  expect(screen.getByText('正在读取运行诊断…')).toBeInTheDocument();
  view.rerender(<DiagnosticSection api={second} />);
  expect(first.read.mock.calls[0][0]?.aborted).toBe(true);
  resolve(snapshot);
  await waitFor(() => expect(screen.getByText('Agent 观测条目').parentElement).toHaveTextContent('Agent 观测条目2'));
  expect(screen.getByText('Agent 观测条目').parentElement).not.toHaveTextContent('Agent 观测条目1');
  view.unmount();
  expect(second.read.mock.calls[0][0]?.aborted).toBe(true);
});
