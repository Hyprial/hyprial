import { expect, it, vi } from 'vitest';
import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { OperationProgress } from '../../../src/features/connections/impl/platform/OperationProgress';
import { DomainError, HttpTransport } from '../../../src/client';
import { createGeneratedClient } from '../../../src/client/generated';
import { PlatformIntegrationsSection } from '../../../src/features/connections/PlatformIntegrationsSection';
import { createPlatformIntegrationsApiFromClient } from '../../../src/features/connections/platform-integrations-api';

function setup() {
  const transport = new HttpTransport({ baseUrl: 'http://127.0.0.1:1', token: 'test-only' });
  let exists = false;
  let lost = false;
  let recorded = true;
  let saved = false;
  const operation = { ok: true, operationId: 'new-app', adapter: 'demo', provider: 'lark', kind: 'add', status: 'awaiting_user',
    configured: false, reloaded: false, remoteOutcomeUnknown: true, nextAction: 'wait',
    verificationUrl: 'https://accounts.feishu.cn/authorize', expiresAt: Date.now() + 600000 };
  const invoke = vi.spyOn(transport, 'invoke').mockImplementation(async method => {
    if (method === 'adapter.list') return { ok: true, adapters: saved ? [{ name: 'demo', provider: 'lark', configured: true, desired: false, processRunning: false, status: 'stopped', online: false, agent: null }] : [] };
    if (method === 'adapter.onboard.begin') { exists = recorded; if (lost) throw new Error('response lost'); return operation; }
    if (method === 'adapter.onboard.operation') {
      if (!exists) throw new DomainError('ADAPTER_OPERATION_NOT_FOUND', 'not found', null, { method, requestId: null, status: 400, dispatch: 'dispatched', catalogDigest: null, attempts: 1, startedAt: 0, durationMs: 1 });
      return saved ? { ...operation, status: 'succeeded', configured: true, reloaded: false, nextAction: 'reload' } : operation;
    }
    if (method === 'adapter.onboard.cancel') return { ...operation, status: 'cancelled', verificationUrl: undefined, expiresAt: undefined };
    throw new Error('unexpected ' + method);
  });
  return { api: createPlatformIntegrationsApiFromClient(createGeneratedClient(transport)), invoke,
    restore: () => { exists = true; }, loseReply: (writeRecorded = true) => { lost = true; recorded = writeRecorded; }, save: () => { saved = true; } };
}

async function openForm(api: ReturnType<typeof setup>['api']) {
  const user = userEvent.setup();
  render(<PlatformIntegrationsSection api={api} />);
  await user.click(await screen.findByRole('button', { name: '添加接入' }));
  await user.type(screen.getByLabelText('名称'), 'demo');
  return user;
}

it('routes new app begin/read/cancel through declared methods', async () => {
  const { api, invoke } = setup();
  expect(api.capabilities.onboard?.available).toBe(true);
  await api.beginAdd!({ name: 'demo', provider: 'lark', mode: 'create' });
  await api.addOperation!('new-app');
  await api.cancelAdd!('new-app');
  expect(invoke.mock.calls.map(call => call[0])).toEqual(['adapter.onboard.begin', 'adapter.onboard.operation', 'adapter.onboard.cancel']);
});

it('recovers an existing creation without submitting again', async () => {
  const { api, invoke, restore } = setup();
  restore();
  await openForm(api);
  expect(await screen.findByRole('link', { name: '打开平台授权页面' })).toBeVisible();
  expect(invoke.mock.calls.filter(call => call[0] === 'adapter.onboard.begin')).toHaveLength(0);
});

it('lost begin response blocks replay and saved but unloaded configuration requests reload', async () => {
  const { api, invoke, loseReply, save } = setup();
  loseReply();
  const user = await openForm(api);
  await waitFor(() => expect(screen.getByRole('button', { name: '开始添加' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: '开始添加' }));
  expect(await screen.findByText('结果未确认')).toBeVisible();
  save();
  await user.click(screen.getByRole('button', { name: '刷新添加状态' }));
  expect(await screen.findByText('接入已保存，请重新加载配置。')).toBeVisible();
  expect(invoke.mock.calls.filter(call => call[0] === 'adapter.onboard.begin')).toHaveLength(1);
});

it('restores saved configuration with a reload action even when the adapter list is empty', async () => {
  const { api, invoke } = setup();
  vi.spyOn(api, 'currentAdd').mockResolvedValue({ operationId: 'saved', adapter: 'demo', provider: 'lark', kind: 'add',
    status: 'succeeded', configured: true, reloaded: false, nextAction: 'reload' });
  await openForm(api);
  expect(await screen.findByText('接入已保存，请重新加载配置。')).toBeVisible();
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '重新加载全部配置' })).toBeEnabled();
  expect(invoke.mock.calls.filter(call => call[0] === 'adapter.onboard.begin')).toHaveLength(0);
});

it('retries a failed receipt read explicitly without allowing a new creation', async () => {
  const { api, invoke } = setup();
  const read = vi.spyOn(api, 'currentAdd').mockRejectedValueOnce(new Error('read failed')).mockResolvedValue(null);
  const user = await openForm(api);
  const retry = await screen.findByRole('button', { name: '重新读取添加记录' });
  expect(screen.getByRole('button', { name: '开始添加' })).toBeDisabled();
  await user.click(retry);
  await waitFor(() => expect(screen.getByRole('button', { name: '开始添加' })).toBeEnabled());
  expect(read).toHaveBeenCalledTimes(2);
  expect(screen.queryByText('添加记录读取失败，请重试。')).not.toBeInTheDocument();
  expect(invoke.mock.calls.filter(call => call[0] === 'adapter.onboard.begin')).toHaveLength(0);
});

it('rejects an invalid platform name before any operation request', async () => {
  const { api, invoke } = setup();
  const user = await openForm(api);
  await user.clear(screen.getByLabelText('名称'));
  await user.type(screen.getByLabelText('名称'), 'gui测试');
  await user.click(screen.getByRole('button', { name: '开始添加' }));
  expect(await screen.findByText('名称须以小写字母或数字开头，仅含小写字母、数字、点、下划线或连字符。')).toBeVisible();
  expect(invoke.mock.calls.filter(call => call[0] === 'adapter.onboard.begin')).toHaveLength(0);
});

it('updates pending creation and closes the dialog without a refresh click or another begin', async () => {
  const { api, invoke, save } = setup();
  const user = await openForm(api);
  await waitFor(() => expect(screen.getByRole('button', { name: '开始添加' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: '开始添加' }));
  expect(await screen.findByRole('link', { name: '打开平台授权页面' })).toBeVisible();
  save();
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument(), { timeout: 5000 });
  expect(invoke.mock.calls.filter(call => call[0] === 'adapter.onboard.begin')).toHaveLength(1);
  expect(screen.getByText('接入已保存，请重新加载配置。')).toBeVisible();
});


it('pauses progress reads on expiry and on unmount without replaying an operation', async () => {
  vi.useFakeTimers();
  try {
    const refresh = vi.fn();
    const view = render(<OperationProgress busy={false} onRefresh={refresh} operation={{
      operationId: 'expiring', adapter: 'demo', provider: 'lark', kind: 'add', status: 'awaiting_user', expiresAt: Date.now() + 1000,
    }} />);
    await act(async () => vi.advanceTimersByTime(2100));
    expect(refresh).not.toHaveBeenCalled();
    expect(screen.getByText('自动更新已暂停，请刷新确认最终状态。')).toBeVisible();
    view.unmount();
    await act(async () => vi.advanceTimersByTime(20000));
    expect(refresh).not.toHaveBeenCalled();
  } finally { vi.useRealTimers(); }
});


it('submits a unique operation key when randomUUID is unavailable on HTTP', async () => {
  const { api, invoke } = setup();
  const getRandomValues = crypto.getRandomValues.bind(crypto);
  vi.stubGlobal('crypto', { getRandomValues });
  try {
    await api.beginAdd!({ name: 'demo', provider: 'lark', mode: 'create' });
    await api.beginAdd!({ name: 'demo', provider: 'lark', mode: 'create' });
    const keys = invoke.mock.calls.map(call => (call[1] as { operationKey: string }).operationKey);
    expect(keys).toHaveLength(2);
    const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
    expect(keys[0]).toMatch(uuidV4);
    expect(keys[1]).toMatch(uuidV4);
    expect(keys[0]).not.toBe(keys[1]);
  } finally {
    vi.unstubAllGlobals();
  }
});

it('uses the native UUID when the browser provides randomUUID', async () => {
  const { api, invoke } = setup();
  const operationKey = '23fcac67-4682-4b65-8dec-21f84e4b1581';
  const randomUUID = vi.fn(() => operationKey);
  vi.stubGlobal('crypto', { randomUUID });
  try {
    await api.beginAdd!({ name: 'demo', provider: 'lark', mode: 'create' });
    expect(randomUUID).toHaveBeenCalledOnce();
    expect(invoke).toHaveBeenCalledWith('adapter.onboard.begin', expect.objectContaining({ operationKey }), expect.anything());
  } finally {
    vi.unstubAllGlobals();
  }
});

it('explains a missing receipt after automatic status refresh without replaying creation', async () => {
  const { api, invoke, loseReply } = setup();
  loseReply(false);
  const user = await openForm(api);
  await waitFor(() => expect(screen.getByRole('button', { name: '开始添加' })).toBeEnabled());
  await user.click(screen.getByRole('button', { name: '开始添加' }));
  expect(await screen.findByText('结果未确认')).toBeVisible();
  expect(await screen.findByText('未找到这次添加的记录。请重新开始添加。', {}, { timeout: 5000 })).toBeVisible();
  expect(screen.getByRole('dialog')).toBeVisible();
  expect(screen.getByLabelText('名称')).toHaveValue('demo');
  expect(invoke.mock.calls.filter(call => call[0] === 'adapter.onboard.begin')).toHaveLength(1);
});
