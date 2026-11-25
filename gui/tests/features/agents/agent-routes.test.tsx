import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DomainError, HttpTransport } from '../../../src/client';
import { createGeneratedClient } from '../../../src/client/generated';
import { createAdapterRoutesApi, type AdapterRoutesApi, type RouteSnapshot } from '../../../src/features/agents/impl/routes/api';
import { RouteSettings } from '../../../src/features/agents/impl/routes/RouteSettings';
import { expectUnavailableToast, renderWithNotifications } from '../../support/notify-harness';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.setPointerCapture ??= () => {};
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const empty: RouteSnapshot = { name: 'demo', appId: 'cli_demo', revision: 'a'.repeat(64), defaultRoute: null, routes: [] };
const saved: RouteSnapshot = { ...empty, revision: 'b'.repeat(64), routes: [{ name: 'main', type: 'direct', nativeId: 'oc_group' }] };
function fixture(): AdapterRoutesApi {
  return { list: vi.fn().mockResolvedValue(empty), candidates: vi.fn().mockResolvedValue([{ chatId: 'oc_group', chatType: 'group' }]),
    add: vi.fn().mockResolvedValue({ adapter: saved, configured: true, reloaded: true, nextAction: 'none' }),
    remove: vi.fn().mockResolvedValue({ adapter: empty, configured: true, reloaded: true, nextAction: 'none' }), reload: vi.fn() };
}

it('uses one declared request for each route action with no implicit retries', async () => {
  const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' });
  const invoke = vi.spyOn(transport, 'invoke').mockResolvedValue({ adapter: empty, candidates: [] });
  const api = createAdapterRoutesApi(createGeneratedClient(transport));
  await api.list('demo'); await api.candidates('demo');
  await api.add('demo', 'main', 'oc_group', false, empty.revision);
  await api.remove('demo', 'main', false, saved.revision);
  expect(invoke.mock.calls.map(call => call[0])).toEqual(['adapter.routes.list','adapter.routes.candidates','adapter.routes.add','adapter.routes.remove']);
  expect(invoke.mock.calls[2][2]).toEqual({ retry: 'never' });
  expect(invoke.mock.calls[3][2]).toEqual({ retry: 'never' });
});

it('reads candidates explicitly then saves and removes a route', async () => {
  const api = fixture(); const user = userEvent.setup();
  render(<RouteSettings name="demo" api={api} />);
  await screen.findByText('暂无消息路由');
  expect(api.candidates).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: '添加路由' }));
  await user.click(screen.getByRole('button', { name: '读取候选' }));
  await user.type(screen.getByLabelText('路由名称'), 'main');
  await user.click(screen.getByRole('combobox', { name: '目标聊天' }));
  await user.click(screen.getByRole('option', { name: '群聊 · oc_group' }));
  await user.click(screen.getByRole('button', { name: '保存路由' }));
  expect(await screen.findByRole('cell', { name: 'main' })).toBeVisible();
  expect(api.add).toHaveBeenCalledWith('demo','main','oc_group',false,empty.revision);
  await user.click(screen.getByRole('button', { name: '删除 main' }));
  await user.click(screen.getByRole('button', { name: '确认删除' }));
  expect(api.remove).toHaveBeenCalledWith('demo','main',false,saved.revision);
  await screen.findByText('暂无消息路由');
});

it('lost write response blocks replay until a successful configuration read', async () => {
  const api=fixture(); vi.mocked(api.add).mockRejectedValue(new Error('lost response'));
  const user=userEvent.setup(); render(<RouteSettings name="demo" api={api} />);
  await screen.findByText('暂无消息路由');
  await user.click(screen.getByRole('button', { name: '添加路由' }));
  await user.click(screen.getByRole('button',{name:'读取候选'}));
  await user.type(screen.getByLabelText('路由名称'),'main');
  await user.click(screen.getByRole('combobox', { name: '目标聊天' }));
  await user.click(screen.getByRole('option', { name: '群聊 · oc_group' }));
  await user.click(screen.getByRole('button',{name:'保存路由'}));
  await screen.findByText('保存结果未确认，请刷新路由。');
  expect(screen.getByRole('button',{name:'保存路由'})).toBeDisabled();
  vi.mocked(api.list).mockRejectedValueOnce(new Error('read failed'));
  await user.click(screen.getByRole('button',{name:'刷新路由'}));
  await waitFor(()=>expect(screen.getByRole('button',{name:'刷新路由'})).toBeEnabled());
  expect(screen.queryByRole('button',{name:'保存路由'})).toBeNull();
  expect(api.add).toHaveBeenCalledTimes(1);
});

it('requires explicit confirmation before removing the default route', async () => {
  const api = fixture();
  vi.mocked(api.list).mockResolvedValue({ ...saved, defaultRoute: 'main' });
  const user = userEvent.setup(); render(<RouteSettings name="demo" api={api} />);
  await user.click(await screen.findByRole('button', { name: '删除 main' }));
  expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled();
  expect(api.remove).not.toHaveBeenCalled();
  await user.click(screen.getByLabelText('同时清除默认路由'));
  await user.click(screen.getByRole('button', { name: '确认删除' }));
  expect(api.remove).toHaveBeenCalledWith('demo', 'main', true, saved.revision);
});

it('does not install a late write receipt into a different platform', async () => {
  const api = fixture();
  let complete!: (value: Awaited<ReturnType<AdapterRoutesApi['add']>>) => void;
  vi.mocked(api.add).mockReturnValue(new Promise(resolve => { complete = resolve; }));
  const user = userEvent.setup(); const view = render(<RouteSettings name="demo" api={api} />);
  await screen.findByText('暂无消息路由');
  await user.click(screen.getByRole('button', { name: '添加路由' }));
  await user.click(screen.getByRole('button', { name: '读取候选' }));
  await user.type(screen.getByLabelText('路由名称'), 'main');
  await user.click(screen.getByRole('combobox', { name: '目标聊天' }));
  await user.click(screen.getByRole('option', { name: '群聊 · oc_group' }));
  await user.click(screen.getByRole('button', { name: '保存路由' }));
  const next = fixture();
  view.rerender(<RouteSettings name="next" api={next} />);
  await waitFor(() => expect(next.list).toHaveBeenCalledWith('next'));
  await act(async () => complete({ adapter: saved, configured: true, reloaded: true, nextAction: 'none' }));
  expect(screen.queryByRole('cell', { name: 'main' })).toBeNull();
  expect(screen.getByText('暂无消息路由')).toBeVisible();
});

it.each([
  ['ROUTE_EXISTS', '路由名称已存在。', false],
  ['ADAPTER_CONFIG_CONFLICT', '配置已变化，请刷新路由。', true],
  ['SUBMIT_OUTCOME_UNKNOWN', '保存结果未确认，请刷新路由。', true],
  ['DAEMON_ERROR', '保存结果未确认，请刷新路由。', true],
])('projects %s without replaying the write', async (code, message, blocked) => {
  const api = fixture();
  vi.mocked(api.add).mockRejectedValue(new DomainError(code, 'opaque backend diagnostic', undefined, {
    method: 'adapter.routes.add', requestId: 'test', status: 500, catalogDigest: null,
    attempts: 1, startedAt: 0, durationMs: 1, dispatch: 'dispatched',
  }));
  const user = userEvent.setup(); render(<RouteSettings name="demo" api={api} />);
  await screen.findByText('暂无消息路由');
  await user.click(screen.getByRole('button', { name: '添加路由' }));
  await user.click(screen.getByRole('button', { name: '读取候选' }));
  await user.type(screen.getByLabelText('路由名称'), 'main');
  await user.click(screen.getByRole('combobox', { name: '目标聊天' }));
  await user.click(screen.getByRole('option', { name: '群聊 · oc_group' }));
  await user.click(screen.getByRole('button', { name: '保存路由' }));
  expect(await screen.findByText(message)).toBeVisible();
  expect(screen.getByRole('button', { name: '保存路由' }).hasAttribute('disabled')).toBe(blocked);
  expect(screen.queryByText('opaque backend diagnostic')).toBeNull();
  expect(api.add).toHaveBeenCalledTimes(1);
});

function toastTexts(): (string | null)[] {
  return Array.from(document.querySelectorAll('.toast.toast-warning .toast-text')).map(node => node.textContent);
}

it('keeps every route control enabled when the catalog refuses an action and notifies on use without a request', async () => {
  const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' });
  const invoke = vi.spyOn(transport, 'invoke').mockResolvedValue({ adapter: saved });
  const access = vi.fn((method: string) => (['adapter.routes.list', 'adapter.routes.add'].includes(method)
    ? { available: true }
    : { available: false, code: 'METHOD_NOT_CALLABLE', reason: '当前会话只读。' }));
  const api = createAdapterRoutesApi(createGeneratedClient(transport), access);
  const user = userEvent.setup();
  renderWithNotifications(<RouteSettings name="demo" api={api} />);
  await screen.findByRole('cell', { name: 'main' });
  await user.click(screen.getByRole('button', { name: '添加路由' }));
  expect(invoke).toHaveBeenCalledTimes(1);
  for (const name of ['刷新路由', '读取候选', '删除 main']) {
    expect(screen.getByRole('button', { name })).toBeEnabled();
    expect(screen.getByRole('button', { name })).not.toHaveAttribute('title');
  }
  await user.click(screen.getByRole('button', { name: '读取候选' }));
  expectUnavailableToast('读取候选暂不可用。');
  await user.click(screen.getByRole('button', { name: '删除 main' }));
  await waitFor(() => expect(toastTexts()).toEqual(['读取候选暂不可用。', '删除路由暂不可用。']));
  expect(screen.queryByRole('dialog', { name: '删除路由' })).toBeNull();
  expect(screen.getByRole('button', { name: '删除 main' })).toBeEnabled();
  expect(invoke).toHaveBeenCalledTimes(1);
  expect(document.body.textContent).not.toMatch(/当前会话只读|METHOD_NOT_CALLABLE/);
});

it('notifies when the route list or the save is refused, without an inline message or a request', async () => {
  const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:1', token: 'synthetic' });
  const invoke = vi.spyOn(transport, 'invoke').mockResolvedValue({ adapter: saved, candidates: [{ chatId: 'oc_group', chatType: 'group' }] });
  let refused: readonly string[] = [];
  const access = vi.fn((method: string) => (refused.includes(method)
    ? { available: false, code: 'METHOD_NOT_AVAILABLE' }
    : { available: true }));
  const api = createAdapterRoutesApi(createGeneratedClient(transport), access);
  const user = userEvent.setup();
  renderWithNotifications(<RouteSettings name="demo" api={api} />);
  await screen.findByRole('cell', { name: 'main' });
  await user.click(screen.getByRole('button', { name: '添加路由' }));
  await user.click(screen.getByRole('button', { name: '读取候选' }));
  await user.type(screen.getByLabelText('路由名称'), 'next');
  await user.click(screen.getByRole('combobox', { name: '目标聊天' }));
  await user.click(screen.getByRole('option', { name: /oc_group/ }));
  const calls = invoke.mock.calls.length;
  refused = ['adapter.routes.add'];
  await user.click(screen.getByRole('button', { name: '保存路由' }));
  expectUnavailableToast('保存路由暂不可用。');
  expect(screen.getByRole('button', { name: '保存路由' })).toBeEnabled();
  expect(invoke).toHaveBeenCalledTimes(calls);
  refused = ['adapter.routes.list'];
  await user.click(screen.getByRole('button', { name: '刷新路由' }));
  await waitFor(() => expect(document.querySelectorAll('.toast.toast-warning')).toHaveLength(2));
  expect(Array.from(document.querySelectorAll('.toast-text')).map(node => node.textContent))
    .toEqual(['保存路由暂不可用。', '读取路由暂不可用。']);
  expect(screen.getByRole('cell', { name: 'main' })).toBeVisible();
  expect(invoke).toHaveBeenCalledTimes(calls);
  expect(document.body.textContent).not.toMatch(/METHOD_NOT_AVAILABLE|路由读取暂不可用/);
});

it('a refused reload notifies and keeps the reload control enabled', async () => {
  const api = fixture();
  vi.mocked(api.add).mockResolvedValue({ adapter: saved, configured: true, reloaded: false, nextAction: 'reload' });
  api.availability = (action) => (action === 'reload' ? { available: false, code: 'METHOD_NOT_AVAILABLE' } : { available: true });
  const user = userEvent.setup();
  renderWithNotifications(<RouteSettings name="demo" api={api} />);
  await screen.findByText('暂无消息路由');
  await user.click(screen.getByRole('button', { name: '添加路由' }));
  await user.click(screen.getByRole('button', { name: '读取候选' }));
  await user.type(screen.getByLabelText('路由名称'), 'main');
  await user.click(screen.getByRole('combobox', { name: '目标聊天' }));
  await user.click(screen.getByRole('option', { name: /oc_group/ }));
  await user.click(screen.getByRole('button', { name: '保存路由' }));
  const reload = await screen.findByRole('button', { name: '重新加载全部配置' });
  expect(reload).toBeEnabled();
  await user.click(reload);
  expectUnavailableToast('重新加载配置暂不可用。');
  expect(api.reload).not.toHaveBeenCalled();
  expect(screen.getByRole('button', { name: '重新加载全部配置' })).toBeEnabled();
});


it('separates adding from the route list and shows names before IDs', async () => {
  const api = fixture();
  vi.mocked(api.candidates).mockResolvedValue([{ chatId: 'oc_group', chatType: 'group', chatName: '验收测试群' }]);
  const user = userEvent.setup();
  render(<RouteSettings name="demo" api={api} />);
  await screen.findByText('暂无消息路由');
  expect(screen.queryByLabelText('路由名称')).toBeNull();
  await user.click(screen.getByRole('button', { name: '添加路由' }));
  await user.click(screen.getByRole('button', { name: '读取候选' }));
  expect(await screen.findByText('已读取 1 个聊天。')).toBeVisible();
  await user.click(screen.getByRole('combobox', { name: '目标聊天' }));
  expect(screen.getByRole('option', { name: '验收测试群 · 群聊 · oc_group' })).toBeVisible();
});
