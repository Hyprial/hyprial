import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { createSharedCredentialsApiFromClient } from '../../../../src/features/connections';
import { CredentialsSection } from '../../../../src/features/connections/CredentialsSection';
import { createAgentsApi } from '../../../../src/features/agents/impl/api';
import { AgentRequestError } from '../../../../src/features/agents/impl/api/agent-writes';
import { createMockSharedCredentialsApi } from '../../../../src/dev/mock/agent-settings';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

function catalog() {
  const entries = new Map<string, Record<string, string>>();
  const providerList = vi.fn(async () => ({ ok: true, entries: [...entries].map(([entryId, fields]) => ({ entryId, fieldNames: Object.keys(fields) })) }));
  const providerWrite = vi.fn(async ({ entryId, fieldName, value }: { entryId: string; fieldName: string; value: string }) => {
    entries.set(entryId, { [fieldName]: value });
    return { ok: true, entryId, fieldNames: [fieldName] };
  });
  const revoke = vi.fn(async () => ({ ok: true, revoked: false }));
  const list = vi.fn(async () => ({ ok: true, grants: [] }));
  const client = { agent: { secret: { providerList, providerWrite, revoke, list } } };
  return { entries, providerList, providerWrite, revoke, client, api: createSharedCredentialsApiFromClient(client as never) };
}

describe('Secret review regressions', () => {
  it.each(['deepseek', 'openai'])('rejects a second add with the same name for %s without replacing any fields', async provider => {
    const { api, entries, providerWrite, providerList } = catalog();
    expect((await api.addKey('deepseek', 'team', 'synthetic-original')).status).toBe('saved');
    expect((await api.addKey(provider, ' team ', 'synthetic-new')).status).toBe('failed');
    expect(providerWrite).toHaveBeenCalledTimes(1);
    expect(providerList).toHaveBeenCalledTimes(2);
    expect(entries.get('team')).toEqual({ DEEPSEEK_API_KEY: 'synthetic-original' });
  });

  it('rechecks the catalog at submission and preserves an entry added after the displayed list', async () => {
    const { api, entries, providerWrite } = catalog();
    expect(await api.listKeys()).toEqual([]);
    entries.set('team', { OPENAI_API_KEY: 'synthetic-openai', OTHER_FIELD: 'synthetic-other' });
    expect(await api.addKey('deepseek', 'team', 'synthetic-new')).toMatchObject({ status: 'failed', message: expect.stringContaining('已存在') });
    expect(providerWrite).not.toHaveBeenCalled();
    expect(entries.get('team')).toEqual({ OPENAI_API_KEY: 'synthetic-openai', OTHER_FIELD: 'synthetic-other' });
  });

  it('does not write or claim an unknown write outcome when the catalog read fails', async () => {
    const { api, providerList, providerWrite } = catalog();
    providerList.mockRejectedValue(new Error('private-path-sentinel'));
    const result = await api.addKey('deepseek', 'team', 'synthetic');
    expect(result).toMatchObject({ status: 'failed', message: expect.stringContaining('未写入') });
    expect(JSON.stringify(result)).not.toContain('private-path-sentinel');
    expect(providerWrite).not.toHaveBeenCalled();
  });

  it('propagates a catalog refusal without dispatching a write', async () => {
    const { client, providerWrite } = catalog();
    const api = createSharedCredentialsApiFromClient(client as never, method => method === 'agent.secret.provider-list'
      ? { available: false, code: 'METHOD_NOT_FOUND', reason: 'catalog' } : { available: true });
    expect(await api.addKey('deepseek', 'team', 'synthetic')).toMatchObject({ status: 'unavailable', refusal: { source: 'agent.secret.provider-list', code: 'METHOD_NOT_FOUND' } });
    expect(providerWrite).not.toHaveBeenCalled();
  });

  it('does not report a successful revocation for a missing grant', async () => {
    const { api, revoke } = catalog();
    expect(await api.revokeGrant('ds-agent1', 'missing')).toMatchObject({ status: 'failed', message: expect.stringContaining('未找到') });
    expect(revoke).toHaveBeenCalledExactlyOnceWith({ actor: 'ds-agent1', grantId: 'missing' });
  });

  it.each(['providerStatus', 'revokeProvider'] as const)('normalizes %s admission refusals into AgentRequestError', async action => {
    const { client } = catalog();
    const api = createAgentsApi(client as never, () => ({ available: false, code: 'METHOD_NOT_FOUND', reason: 'catalog' }));
    const failure = await api[action]('ds-agent1', undefined as never).catch(error => error);
    expect(failure).toBeInstanceOf(AgentRequestError);
    expect(failure).toMatchObject({ code: 'METHOD_NOT_FOUND', dispatch: 'not-dispatched', domain: false });
  });

  it('offers no delete capability in preview when the connected port cannot delete', () => {
    expect(createMockSharedCredentialsApi().deleteKey).toBeUndefined();
  });

  it('keeps the duplicate-name explanation inside the open form and clears the secret', async () => {
    const user = userEvent.setup();
    const { api, entries, providerWrite } = catalog();
    entries.set('DeepSeek', { DEEPSEEK_API_KEY: 'synthetic-original' });
    render(<CredentialsSection api={api} />);
    await screen.findByText('DeepSeek');
    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    const dialog = screen.getByRole('dialog', { name: '添加 Key' });
    await user.type(within(dialog).getByLabelText('Key'), 'synthetic-new');
    await user.click(within(dialog).getByRole('button', { name: '保存' }));
    expect(await within(dialog).findByText(/该名称已存在/)).toBeVisible();
    expect(within(dialog).getByLabelText('Key')).toHaveValue('');
    expect(providerWrite).not.toHaveBeenCalled();
  });

  it.each([['Kimi Code', 'Kimi-Code', 'KIMI_API_KEY'], ['Moonshot AI', 'Moonshot-AI', 'MOONSHOT_API_KEY']])('can save %s using its default name', async (provider, entryId, fieldName) => {
    const user = userEvent.setup();
    const { api, providerWrite } = catalog();
    render(<CredentialsSection api={api} />);
    await screen.findByText('暂无 Key');
    await user.click(screen.getByRole('button', { name: '添加 Key' }));
    const form = screen.getByRole('dialog', { name: '添加 Key' });
    await user.click(within(form).getByRole('combobox'));
    await user.click(screen.getByRole('option', { name: provider }));
    expect(within(form).getByRole('textbox', { name: '名称' })).toHaveValue(entryId);
    await user.type(within(form).getByLabelText('Key'), 'synthetic');
    await user.click(within(form).getByRole('button', { name: '保存' }));
    expect(providerWrite).toHaveBeenCalledExactlyOnceWith({ entryId, fieldName, value: 'synthetic' });
  });
});
