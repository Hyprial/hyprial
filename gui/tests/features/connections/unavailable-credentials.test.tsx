/*
 * Decision record (PR 1f/F4): the credentials section never explains that a
 * function is unavailable. A read the catalog refuses before dispatch is an
 * absent port (nothing rendered); a refused operation keeps its control
 * operable and is reported once through the notification mechanism, with no
 * side effect (no dialog, no cleared secret, no stuck busy state).
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { MethodRefusedError, type MethodRefusal } from '../../../src/client';
import { CredentialsSection } from '../../../src/features/connections/CredentialsSection';
import {
  createSharedCredentialsApiFromClient,
  type CredentialEntry,
  type SharedCredentialWrite,
  type SharedCredentialsApi,
} from '../../../src/features/connections/shared-credentials-api';
import { expectUnavailableToast, renderWithNotifications } from '../../support/notify-harness';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const KEY: CredentialEntry = {
  keyId: 'pk.mfrggzdfmztwq2lknnwg23tpob',
  provider: 'deepseek',
  label: 'DeepSeek',
  state: 'active',
  createdAt: 1_760_000_000_000,
  lastUsedAt: null,
  grants: [{
    agent: '00000000-0000-0000-0000-000000000001',
    agentId: 'a.aaaaaaaaaaaaaaaaaaaaaaaaaa',
    grantId: 'grant-1',
    provider: 'deepseek',
    state: 'active',
  }],
};

const REFUSAL: MethodRefusal = { code: 'METHOD_NOT_FOUND', source: 'agent.secret.provider-write' };
const REFUSED: SharedCredentialWrite = { status: 'unavailable', refusal: REFUSAL };

function createApi(overrides: Partial<SharedCredentialsApi> = {}): SharedCredentialsApi {
  return {
    async listKeys() { return [KEY]; },
    async listSecretGrants() { return []; },
    async addKey() { return { status: 'saved', message: 'Key 已保存。' }; },
    async replaceKey() { return { status: 'saved', message: 'Key 已更新。' }; },
    async deleteKey() { return { status: 'saved', message: '凭据已删除。' }; },
    async revokeGrant() { return { status: 'saved', message: '授权已撤销。' }; },
    ...overrides,
  };
}

async function openDetail(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  const list = await screen.findByRole('list', { name: '凭据列表' });
  const item = within(list).getByText('DeepSeek').closest('li') as HTMLElement;
  await user.click(within(item).getByRole('button', { name: '查看详情' }));
  return item;
}

describe('credentials section: refused operations', () => {
  it('refuses missing connection writer before opening an editor', async () => {
    renderWithNotifications(<CredentialsSection api={createApi()} />);
    await screen.findByRole('list', { name: '凭据列表' });
    await userEvent.setup().click(screen.getByRole('button', { name: '添加连接' }));
    expectUnavailableToast('添加连接暂不可用。');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('reports a refusal known before the form opens and opens nothing', async () => {
    const user = userEvent.setup();
    const addKey = vi.fn();
    const api = createApi({ addKey, refusal: action => (action === 'add' ? REFUSAL : null) });
    renderWithNotifications(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });

    await user.click(screen.getByRole('button', { name: '添加连接' }));

    expectUnavailableToast('添加连接暂不可用。');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('button', { name: '添加连接' })).toBeEnabled();
    expect(addKey).not.toHaveBeenCalled();
  });

  it('reports a refused save once, keeps the form editable and keeps the typed key', async () => {
    const user = userEvent.setup();
    const api = createApi({ configureKey: vi.fn(async () => REFUSED) });
    renderWithNotifications(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });
    await user.click(screen.getByRole('button', { name: '添加连接' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('名称'), 'gateway');
    await user.type(within(dialog).getByLabelText('服务地址'), 'https://gateway.example/v1');
    await user.type(within(dialog).getByLabelText('Key'), 'synthetic-key');

    await user.click(within(dialog).getByRole('button', { name: '保存' }));

    expectUnavailableToast('保存连接暂不可用。');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeEnabled();
    expect(within(dialog).getByLabelText('Key')).toHaveValue('synthetic-key');
    expect(document.querySelector('.ui-notice')).toBeNull();
    expect(within(dialog).getByLabelText('名称')).toBeEnabled();
  });

  it.each([
    ['更换 Key', 'replace', '更换 Key暂不可用。'],
    ['删除凭据', 'delete', '删除凭据暂不可用。'],
  ] as const)('reports %s as refused before its dialog opens', async (label, action, text) => {
    const user = userEvent.setup();
    const api = createApi({ refusal: requested => (requested === action ? REFUSAL : null) });
    renderWithNotifications(<CredentialsSection api={api} />);
    const item = await openDetail(user);

    await user.click(within(item).getByRole('button', { name: label }));

    expectUnavailableToast(text);
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(within(item).getByRole('button', { name: label })).toBeEnabled();
  });

  it('keeps the replacement dialog and value when the save result is a refusal', async () => {
    const user = userEvent.setup();
    const api = createApi({ replaceKey: vi.fn(async () => REFUSED) });
    renderWithNotifications(<CredentialsSection api={api} />);
    const item = await openDetail(user);
    await user.click(within(item).getByRole('button', { name: '更换 Key' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('新 Key'), 'replacement-key');

    await user.click(within(dialog).getByRole('button', { name: '保存' }));

    expectUnavailableToast('保存暂不可用。');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('新 Key')).toHaveValue('replacement-key');
    expect(within(dialog).getByRole('button', { name: '保存' })).toBeEnabled();
  });

  it('keeps the delete confirmation open when the delete result is a refusal', async () => {
    const user = userEvent.setup();
    const api = createApi({ deleteKey: vi.fn(async () => REFUSED) });
    renderWithNotifications(<CredentialsSection api={api} />);
    const item = await openDetail(user);
    await user.click(within(item).getByRole('button', { name: '删除凭据' }));
    const dialog = await screen.findByRole('dialog');

    await user.click(within(dialog).getByRole('button', { name: '确认删除' }));

    expectUnavailableToast('确认删除暂不可用。');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '确认删除' })).toBeEnabled();
  });

  it('reports a refused grant revocation without an inline notice', async () => {
    const user = userEvent.setup();
    const api = createApi({ revokeGrant: vi.fn(async () => REFUSED) });
    renderWithNotifications(<CredentialsSection api={api} />);
    const item = await openDetail(user);
    const revoke = within(item).getByRole('button', { name: '撤销 a.aaaaaaaaaaaaaaaaaaaaaaaaaa 的授权' });

    await user.click(revoke);

    expectUnavailableToast('撤销授权暂不可用。');
    expect(revoke).toBeEnabled();
    expect(document.querySelector('.ui-notice')).toBeNull();
  });
});

describe('credentials section: refused reads', () => {
  it('renders nothing when the key list is refused before dispatch', async () => {
    const api = createApi({
      listKeys: async () => { throw new MethodRefusedError('user.provider.list', 'METHOD_NOT_FOUND'); },
    });
    const { container } = renderWithNotifications(<CredentialsSection api={api} />);
    await vi.waitFor(() => expect(screen.queryByText('正在读取凭据')).toBeNull());

    expect(container.querySelector('.credentials-section')).toBeNull();
    expect(container.querySelector('h2, h3')).toBeNull();
    expect(document.body.textContent ?? '').not.toMatch(/暂不|不可用|不支持|尚未/);
  });

  it('hides only the service-grant block when that read is refused', async () => {
    const api = createApi({
      listSecretGrants: async () => { throw new MethodRefusedError('agent.secret.list', 'METHOD_NOT_FOUND'); },
    });
    renderWithNotifications(<CredentialsSection api={api} />);

    expect(await screen.findByText('DeepSeek')).toBeInTheDocument();
    expect(screen.queryByText(/服务密钥授权读取失败/)).toBeNull();
    expect(screen.queryByText('正在读取服务密钥授权')).toBeNull();
  });

  it('raises only a notification for a refused refresh and leaves the list untouched', async () => {
    const user = userEvent.setup();
    let reads = 0;
    const api = createApi({
      async listKeys() {
        reads += 1;
        if (reads > 1) throw new MethodRefusedError('user.provider.list', 'METHOD_NOT_FOUND');
        return [KEY];
      },
    });
    renderWithNotifications(<CredentialsSection api={api} />);
    await screen.findByText('DeepSeek');

    await user.click(screen.getByRole('button', { name: '刷新' }));

    expectUnavailableToast('刷新暂不可用。');
    expect(screen.getByText('DeepSeek')).toBeInTheDocument();
    expect(screen.queryByText('正在读取凭据')).toBeNull();
    expect(screen.queryByText('暂无 Key')).toBeNull();
    expect(screen.queryByText(/凭据读取失败/)).toBeNull();
    expect(screen.getByRole('button', { name: '刷新' })).toBeEnabled();
  });

  it('does not present a refused read as an empty list', async () => {
    const api = createApi({
      listKeys: async () => { throw new MethodRefusedError('user.provider.list', 'METHOD_NOT_FOUND'); },
    });
    renderWithNotifications(<CredentialsSection api={api} />);
    await vi.waitFor(() => expect(screen.queryByText('正在读取凭据')).toBeNull());
    expect(screen.queryByText('暂无 Key')).toBeNull();
    expect(screen.queryByRole('button', { name: '添加连接' })).toBeNull();
  });
});

describe('credentials section: absent capabilities are not rendered', () => {
  it('has no test button or test-model field without a test port', async () => {
    const user = userEvent.setup();
    renderWithNotifications(<CredentialsSection api={createApi({ configureKey: vi.fn(async () => REFUSED) })} />);
    await screen.findByRole('list', { name: '凭据列表' });
    await user.click(screen.getByRole('button', { name: '添加连接' }));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).queryByRole('button', { name: /测试/ })).toBeNull();
    expect(within(dialog).queryByLabelText('测试模型 ID')).toBeNull();
    expect(within(dialog).queryByText(/尚未接入/)).toBeNull();
  });

  it('offers the test controls when a test port exists', async () => {
    const user = userEvent.setup();
    const api = createApi({ configureKey: vi.fn(async () => REFUSED), async testKey() { return { kind: 'model', status: 200, ok: true, latencyMs: 1 }; } });
    renderWithNotifications(<CredentialsSection api={api} />);
    await screen.findByRole('list', { name: '凭据列表' });
    await user.click(screen.getByRole('button', { name: '添加连接' }));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).getByRole('button', { name: '测试' })).toBeInTheDocument();
    expect(within(dialog).getByLabelText('测试模型 ID')).toBeInTheDocument();
  });

  it('offers custom supported protocol without claiming unavailable official presets', async () => {
    const user=userEvent.setup();renderWithNotifications(<CredentialsSection api={createApi({ configureKey: vi.fn(async () => REFUSED) })}/>);
    await screen.findByRole('list',{name:'凭据列表'});await user.click(screen.getByRole('button',{name:'添加连接'}));
    const dialog=screen.getByRole('dialog');
    expect(within(dialog).queryByLabelText('模型服务商')).toBeNull();
    expect(within(dialog).getByLabelText('服务地址')).toBeVisible();
    expect(within(dialog).getByLabelText('兼容协议')).toHaveTextContent('OpenAI Chat Completions');
    expect(within(dialog).getByRole('button',{name:'保存'})).toBeDisabled();
    expect(within(dialog).getByText('官方地址预设读取失败，可填写自定义地址。')).toBeVisible();
  });

  it('renders a fixture model without any operation control', async () => {
    renderWithNotifications(<CredentialsSection model={{ entries: [KEY], authGrants: [] }} />);
    const list = await screen.findByRole('list', { name: '凭据列表' });
    const item = within(list).getByText('DeepSeek').closest('li') as HTMLElement;
    await userEvent.setup().click(within(item).getByRole('button', { name: '查看详情' }));

    expect(within(item).queryByRole('button', { name: '更换 Key' })).toBeNull();
    expect(within(item).queryByRole('button', { name: '删除凭据' })).toBeNull();
    expect(within(item).queryByRole('button', { name: /撤销 .* 的授权/ })).toBeNull();
    expect(screen.queryByRole('button', { name: '添加连接' })).toBeNull();
  });

  it('renders nothing without a port and without a model', () => {
    const { container } = render(<CredentialsSection />);
    expect(container.querySelector('.credentials-section')).toBeNull();
    expect(container.textContent).toBe('');
  });
});

describe('credentials connected port', () => {
  it('lets a refused read reach the handler as a classifiable refusal', async () => {
    const access = () => ({ available: false, code: 'METHOD_NOT_FOUND', reason: 'catalog sentence' }) as const;
    const api = createSharedCredentialsApiFromClient({ user: { provider: {} }, agent: {} } as never, access);

    await expect(api.listKeys()).rejects.toBeInstanceOf(MethodRefusedError);
    await expect(api.listSecretGrants()).rejects.toMatchObject({ code: 'METHOD_NOT_FOUND', method: 'agent.secret.list' });
    expect(api.refusal?.('add')).toEqual({ code: 'METHOD_NOT_FOUND', source: 'agent.secret.provider-configure' });
    expect(api.refusal?.('replace')).toEqual({ code: 'METHOD_NOT_FOUND', source: 'agent.secret.provider-write' });
    expect(api.deleteKey).toBeUndefined();
    expect(api.refusal?.('revoke')).toEqual({ code: 'METHOD_NOT_FOUND', source: 'agent.secret.revoke' });
  });

  it('answers a refused write with the refusal and never with a sentence', async () => {
    const access = () => ({ available: false, code: 'WRITE_METHOD_UNVERIFIED', reason: 'catalog sentence' }) as const;
    const login = vi.fn();
    const api = createSharedCredentialsApiFromClient({ user: { provider: { login } } } as never, access);

    const result = await api.addKey('deepseek', 'DeepSeek', 'synthetic');

    expect(result).toEqual({ status: 'unavailable', refusal: { code: 'WRITE_METHOD_UNVERIFIED', source: 'agent.secret.provider-write' } });
    expect(login).not.toHaveBeenCalled();
  });

  it('keeps the dispatched store failure an ordinary read error', async () => {
    const error = Object.assign(new Error('store down'), { code: 'PROVIDER_STORE_UNAVAILABLE', dispatch: 'dispatched' });
    const list = vi.fn().mockRejectedValue(error);
    const api = createSharedCredentialsApiFromClient({ agent: { secret: { providerList: list } } } as never);

    await expect(api.listKeys()).rejects.toBe(error);
  });
});
