import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ModelPanel } from '../../../../src/features/agents/impl/model-configuration/Panel';
beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});
const prepared = { ok: true, agentId: 'id', member: 'actor', binding: null, cell: { cellId: 'cell', revision: 9, intentMatches: null }, cleanupPending: false, operation: { operationId: 'pending-1', phase: 'prepared', coordinationPhase: 'planned', expectedBindingRevision: 0, expectedCellRevision: 9 }, runtime: 'not-verified' };
it('preserves prepared work and cancels it by its durable operation ID', async () => {
  const api = { modelStatus: vi.fn(async () => prepared), configureModel: vi.fn(), cancelModel: vi.fn(async () => ({ ...prepared, operation: { ...prepared.operation, phase: 'cancelled' } })) };
  render(<ModelPanel actor="actor" api={api as never} onStatus={() => {}} />);
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: '取消未完成配置' }));
  expect(api.cancelModel).toHaveBeenCalledExactlyOnceWith({ name: 'actor', operationId: 'pending-1', expectedBindingRevision: 0 });
  expect(api.configureModel).not.toHaveBeenCalled();
  expect(await screen.findByText(/配置操作已取消/)).toBeInTheDocument();
});
it('does not turn failed status into legacy or empty and blocks edit', async () => {
  const onStatus = vi.fn();
  render(<ModelPanel actor="actor" api={{ modelStatus: async () => { throw new Error('down'); }, configureModel: vi.fn() } as never} onStatus={onStatus} />);
  expect(await screen.findByText(/模型配置读取失败/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '配置模型' })).toBeDisabled();
  expect(onStatus).not.toHaveBeenCalledWith(expect.objectContaining({ binding: null }));
});
it('does not retry unknown cancellation before successful readback', async () => {
  const api = { modelStatus: vi.fn().mockResolvedValue(prepared), cancelModel: vi.fn(async () => { throw { dispatch: 'unknown' }; }) };
  render(<ModelPanel actor="actor" api={api as never} onStatus={() => {}} />);
  const user = userEvent.setup(); await user.click(await screen.findByRole('button', { name: '取消未完成配置' }));
  expect(await screen.findByRole('button', { name: '取消未完成配置' })).toBeDisabled();
  api.modelStatus.mockRejectedValue(new Error('down'));
  await user.click(screen.getByRole('button', { name: '刷新模型配置' }));
  expect(await screen.findByText(/模型配置读取失败/)).toBeInTheDocument();
  expect(api.cancelModel).toHaveBeenCalledTimes(1);
});
it('rejects a cancelled receipt for a different operation and keeps writes blocked', async () => {
  const onStatus = vi.fn();
  const wrong = { ...prepared, operation: { ...prepared.operation, operationId: 'other-operation', phase: 'cancelled' } };
  const api = { modelStatus: vi.fn(async () => prepared), cancelModel: vi.fn(async () => wrong), configureModel: vi.fn() };
  render(<ModelPanel actor="actor" api={api as never} onStatus={onStatus} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: '取消未完成配置' }));
  await waitFor(() => expect(api.cancelModel).toHaveBeenCalledTimes(1));
  expect(screen.queryByText(/配置操作已取消/)).toBeNull();
  expect(onStatus).not.toHaveBeenCalledWith(wrong);
  expect(screen.getByRole('button', { name: '取消未完成配置' })).toBeDisabled();
});
it('ignores cancellation finishing after switching Agent', async () => {
  let finish!: (value: unknown) => void;
  const onStatus = vi.fn();
  const next = { ...prepared, agentId: 'other-id', member: 'other', operation: null };
  const api = { modelStatus: vi.fn(async (actor: string) => actor === 'actor' ? prepared : next), cancelModel: vi.fn(() => new Promise(resolve => { finish = resolve; })), configureModel: vi.fn() };
  const view = render(<ModelPanel actor="actor" api={api as never} onStatus={onStatus} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: '取消未完成配置' }));
  view.rerender(<ModelPanel actor="other" api={api as never} onStatus={onStatus} />);
  await waitFor(() => expect(onStatus).toHaveBeenCalledWith(next));
  onStatus.mockClear();
  await act(async () => finish({ ...prepared, operation: { ...prepared.operation, phase: 'cancelled' } }));
  expect(onStatus).not.toHaveBeenCalled();
  expect(screen.queryByText(/配置操作已取消/)).toBeNull();
});

const unbound = { ...prepared, operation: null };
const targets = [{ actor: 'actor', harness: 'pi', state: 'stored', targets: [
  { nativeAuthRef: 'auth-first', target: 'first', kind: 'oauth' },
  { nativeAuthRef: 'auth-second', target: 'second', kind: 'api_key' },
] }];
async function openNative() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: '配置模型' }));
  await user.click(screen.getByLabelText('凭据', { exact: true }));
  await user.click(screen.getByRole('option', { name: '原生 Auth' }));
  return user;
}
it('requires explicit native target and preserves the manually entered model and exact CAS', async () => {
  const api = { modelStatus: vi.fn(async () => unbound), nativeTargets: vi.fn(async () => targets), configureModel: vi.fn(async (input: { configuration: unknown; operationId: string; expectedBindingRevision: number; expectedCellRevision: number }) => ({ ...unbound, cell: { ...unbound.cell, intentMatches: true }, binding: { configuration: input.configuration }, operation: { ...input, phase: 'finalized' } })) };
  render(<ModelPanel actor="actor" api={api as never} initialHarness="pi" initialModel="hy4-flash" onStatus={() => {}} />);
  const user = await openNative();
  await user.click(screen.getByRole('button', { name: '保存配置' }));
  expect(api.configureModel).not.toHaveBeenCalled();
  await user.click(screen.getByLabelText('登录目标', { exact: true }));
  await user.click(await screen.findByRole('option', { name: 'second · api_key' }));
  await user.click(screen.getByRole('button', { name: '保存配置' }));
  await waitFor(() => expect(api.configureModel).toHaveBeenCalledTimes(1));
  expect(api.configureModel).toHaveBeenCalledWith({ name: 'actor', configuration: { harness: 'pi', mode: 'headless', model: 'hy4-flash', authKind: 'native', nativeAuthRef: 'auth-second' }, operationId: expect.any(String), expectedBindingRevision: 0, expectedCellRevision: 9 });
});
it('blocks resubmission after unknown native configuration until status is read successfully', async () => {
  const api = { modelStatus: vi.fn(async () => unbound), nativeTargets: vi.fn(async () => targets), configureModel: vi.fn(async () => { throw { dispatch: 'unknown' }; }) };
  render(<ModelPanel actor="actor" api={api as never} initialHarness="pi" initialModel="hy4-flash" onStatus={() => {}} />);
  const user = await openNative();
  await user.click(screen.getByLabelText('登录目标', { exact: true }));
  await user.click(await screen.findByRole('option', { name: 'first · oauth' }));
  await user.click(screen.getByRole('button', { name: '保存配置' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '保存配置' })).toBeDisabled());
  expect(api.configureModel).toHaveBeenCalledTimes(1);
  await user.keyboard('{Escape}');
  api.modelStatus.mockRejectedValueOnce(new Error('down'));
  await user.click(screen.getByRole('button', { name: '刷新模型配置' }));
  expect(await screen.findByText(/模型配置读取失败/)).toBeInTheDocument();
  expect(api.configureModel).toHaveBeenCalledTimes(1);
});
it('imports into the current Agent without selecting the first native target', async () => {
  const files = [{ path: 'auth.json', content: 'synthetic' }];
  const api = { modelStatus: vi.fn(async () => unbound), nativeTargets: vi.fn().mockResolvedValueOnce([]).mockResolvedValue(targets), loginNative: vi.fn(), configureModel: vi.fn() };
  const picker = vi.fn(async () => files);
  render(<ModelPanel actor="actor" api={api as never} nativeLoginFiles={picker as never} initialHarness="pi" initialModel="hy4-flash" onStatus={() => {}} />);
  const user = await openNative();
  await user.click(screen.getByRole('button', { name: '导入或替换登录文件' }));
  await waitFor(() => expect(api.nativeTargets).toHaveBeenCalledTimes(2));
  expect(picker).toHaveBeenCalledWith('actor', 'pi');
  expect(api.loginNative).toHaveBeenCalledExactlyOnceWith('actor', 'pi', files, { replace: true });
  await user.click(screen.getByRole('button', { name: '保存配置' }));
  expect(api.configureModel).not.toHaveBeenCalled();
});
it('does not publish an unexpected finalized cancel receipt as start-ready', async () => {
  const onStatus = vi.fn();
  const unexpected = { ...prepared, binding: { configuration: { harness: 'pi', model: 'wrong' } }, cell: { ...prepared.cell, intentMatches: true }, operation: { ...prepared.operation, phase: 'finalized' } };
  const api = { modelStatus: vi.fn(async () => prepared), cancelModel: vi.fn(async () => unexpected) };
  render(<ModelPanel actor="actor" api={api as never} onStatus={onStatus} />);
  await userEvent.setup().click(await screen.findByRole('button', { name: '取消未完成配置' }));
  await waitFor(() => expect(api.cancelModel).toHaveBeenCalledTimes(1));
  expect(onStatus).not.toHaveBeenCalledWith(unexpected);
});
