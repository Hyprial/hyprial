import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { createAgentJourney } from '../../../src/dev/journey';
import { NativeAuthList } from '../../../src/features/agent-provider';
import { CredentialsSection } from '../../../src/features/connections';
import { createSharedCredentialsApiFromClient } from '../../../src/features/connections/shared-credentials-api';
import { CredentialsTab } from '../../../src/features/agents/impl/detail/CredentialsTab';

async function imported(harness = 'claude') {
  const journey = createAgentJourney();
  await journey.agents.create({ name: 'assistant', preferredHarness: harness, provider: null, model: null });
  const agent = (await journey.agents.list())[0];
  const files = (harness === 'pi' ? ['auth.json', 'models-store.json'] : ['.credentials.json'])
    .map(filename => ({ filename, contentBase64: 'e30=' }));
  await journey.agents.loginNative(agent.uri, harness, files);
  return { journey, agent, files };
}

describe('native Auth inventory', () => {
  it('refreshes current-import evidence without calling an absent record a failed login', async () => {
    const user = userEvent.setup();
    const row = { actor: 'worker', displayName: 'worker', harness: 'codex', state: 'stored' as const,
      files: ['auth.json'], missingFiles: [], errorCode: null, importedAt: 1000, startupConfirmedAt: null as number | null };
    const list = vi.fn(async () => [{ ...row }]);
    render(<NativeAuthList actor="worker" api={{ list }} />);
    expect(await screen.findByText('暂无本版本成功回复记录')).toBeVisible();
    row.startupConfirmedAt = 2000;
    await user.click(screen.getByRole('button', { name: '刷新 Auth' }));
    expect(await screen.findByText('本版本成功回复')).toBeVisible();
    expect(document.querySelector('time')?.dateTime).toBe('1970-01-01T00:00:02.000Z');
    row.startupConfirmedAt = null;
    await user.click(screen.getByRole('button', { name: '刷新 Auth' }));
    expect(await screen.findByText('暂无本版本成功回复记录')).toBeVisible();
    expect(document.querySelector('time')).toBeNull();
  });
  it('reads the real inventory in one request independently from Key availability', async () => {
    const list = vi.fn(async () => ({ ok: true, records: [{ actor: 'agent:owner:node:worker', displayName: 'worker', harness: 'pi', state: 'stored', files: ['auth.json', 'models-store.json'], missingFiles: [], errorCode: null, importedAt: null, startupConfirmedAt: null }] }));
    const api = createSharedCredentialsApiFromClient({ agent: { provider: { auth: { list } } } } as never);
    expect(await api.nativeAuth!.list('agent:owner:node:worker')).toHaveLength(1);
    expect(list).toHaveBeenCalledExactlyOnceWith({ actor: 'agent:owner:node:worker' });
  });

  it('shows incomplete and unreadable Auth without claiming import or startup times', async () => {
    render(<NativeAuthList actor="worker" api={{ list: async () => [
      { actor: 'worker', displayName: 'worker', harness: 'pi', files: ['auth.json'], state: 'partial', missingFiles: ['models-store.json'], errorCode: null, importedAt: null, startupConfirmedAt: null },
      { actor: 'worker', displayName: 'worker', harness: 'claude', files: [], state: 'unavailable', missingFiles: [], errorCode: 'PROVIDER_UNSAFE_PATH', importedAt: null, startupConfirmedAt: null },
    ] }} />);
    expect(await screen.findByText('文件不完整')).toBeVisible();
    expect(screen.getByText('读取失败')).toBeVisible();
    expect(screen.queryByText('已导入')).toBeNull();
    expect(screen.queryByText('暂无 Auth')).toBeNull();
    expect(screen.queryByText('导入时间')).toBeNull();
    expect(screen.queryByText('最近启动')).toBeNull();
  });

  it('records import immediately and keeps startup evidence separate, resetting it on replacement', async () => {
    const { journey, agent, files } = await imported();
    const read = journey.sharedCredentials.nativeAuth!.list;
    expect(await read()).toEqual([expect.objectContaining({ actor: agent.uri, displayName: 'assistant', harness: 'claude', files: ['.credentials.json'], startupConfirmedAt: null })]);
    await journey.agents.start(agent.actor, { harness: 'claude' });
    expect((await read(agent.uri))[0].startupConfirmedAt).toBeNull();
    await journey.agents.stop(agent.actor);
    expect((await read())[0].startupConfirmedAt).toBeNull();
    await journey.agents.loginNative(agent.uri, 'claude', files, { replace: true });
    expect((await read())[0].startupConfirmedAt).toBeNull();
    await journey.agents.rename(agent.actor, '我的助手');
    expect((await read())[0].displayName).toBe('我的助手');
    expect(await read('absent')).toEqual([]);
    await journey.agentDelete(agent.uri);
    expect(await read()).toEqual([]);
  });

  it('keeps the Pi pair in one record and never attributes a failed or Key startup to Auth', async () => {
    const { journey, agent } = await imported('pi');
    const read = journey.sharedCredentials.nativeAuth!.list;
    expect((await read())[0].files).toEqual(['auth.json', 'models-store.json']);
    await expect(journey.agents.start(agent.actor, { harness: 'pi', provider: 'deepseek' })).rejects.toThrow();
    expect((await read())[0].startupConfirmedAt).toBeNull();
    await journey.agents.start(agent.actor, { harness: 'pi', provider: 'deepseek', model: 'deepseek-flash' });
    expect((await read())[0].startupConfirmedAt).toBeNull();
  });

  it('shows a read-only global list independently of Key failures', async () => {
    const { journey } = await imported();
    render(<CredentialsSection api={{ ...journey.sharedCredentials, listKeys: async () => { throw new Error('Key unavailable'); } }} />);
    const table = await screen.findByRole('list', { name: 'Auth 清单' });
    expect(within(table).getByText('assistant')).toBeVisible();
    expect(within(table).getByText('已保存')).toBeVisible();
    expect(within(table).queryByRole('button', { name: '移除 Auth' })).toBeNull();
    expect(within(table).getByRole('button', { name: '查看详情' })).toBeVisible();
  });

  it('removes Auth from Agent detail, preserving the Agent and Key, and updates the global list', async () => {
    const user = userEvent.setup();
    const { journey, agent } = await imported();
    await journey.sharedCredentials.addKey('deepseek', '保留 Key', 'synthetic');
    const { unmount } = render(<CredentialsTab actor={agent.uri} preferredHarness="claude" agentsApi={journey.agents} credentialsApi={journey.sharedCredentials} onNavigateConnections={() => undefined} />);
    await user.click(await screen.findByRole('button', { name: '移除 Auth' }));
    const dialog = screen.getByRole('dialog', { name: '移除 Auth' });
    await user.click(within(dialog).getByRole('button', { name: '移除' }));
    await screen.findByText('暂无 Auth');
    expect(await journey.agents.list()).toHaveLength(1);
    expect((await journey.sharedCredentials.listKeys())).toHaveLength(1);
    unmount();
    render(<CredentialsSection api={journey.sharedCredentials} />);
    expect(await screen.findByText('暂无 Auth')).toBeVisible();
    expect(screen.queryByText('.credentials.json')).toBeNull();
  });

  it('blocks duplicate and unknown removals until a read confirms absence', async () => {
    const user = userEvent.setup();
    const { journey, agent } = await imported();
    let reject!: (reason: unknown) => void;
    const remove = vi.fn(() => new Promise<void>((_resolve, rejectPromise) => { reject = rejectPromise; }));
    render(<NativeAuthList api={journey.sharedCredentials.nativeAuth} actor={agent.uri} onRemove={remove} />);
    await user.click(await screen.findByRole('button', { name: '移除 Auth' }));
    const button = within(screen.getByRole('dialog')).getByRole('button', { name: '移除' });
    await user.dblClick(button);
    expect(remove).toHaveBeenCalledTimes(1);
    await act(async () => reject(new Error('response lost')));
    expect(await screen.findByRole('alert')).toHaveTextContent('移除结果待确认');
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: '移除' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: '重新读取' }));
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: '移除' })).toBeDisabled();
    const record = (await journey.sharedCredentials.nativeAuth!.list())[0];
    await journey.agents.logoutNative(record.actor, record.harness);
    await user.click(screen.getByRole('button', { name: '重新读取' }));
    await screen.findByText('暂无 Auth');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(remove).toHaveBeenCalledTimes(1);
  });

  it('discards a late read from a previous data source', async () => {
    const { journey } = await imported();
    const rows = await journey.sharedCredentials.nativeAuth!.list();
    let resolve!: (value: typeof rows) => void;
    const old = { list: () => new Promise<typeof rows>(r => { resolve = r; }) };
    const { rerender } = render(<NativeAuthList api={old} />);
    rerender(<NativeAuthList api={{ list: async () => [] }} />);
    await screen.findByText('暂无 Auth');
    await act(async () => resolve(rows));
    expect(screen.queryByText('assistant')).toBeNull();
  });
});
