/*
 * Decision record (PR 1f/F4): the platform integration section never explains
 * that a function is unavailable. Controls whose port the composition does
 * not have are not rendered; a refused read before dispatch hides its block
 * (never shown as empty); a refused operation keeps its control operable and
 * is reported once through the notification mechanism with no side effect.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { DomainError, MethodRefusedError } from '../../../src/client';
import { createMockPlatformIntegrationsApi } from '../../../src/dev/mock/platform-integrations';
import { PlatformIntegrationsSection } from '../../../src/features/connections/PlatformIntegrationsSection';
import {
  PLATFORM_PROVIDERS,
  createPlatformIntegrationsApiFromClient,
  type PlatformIntegrationsApi,
} from '../../../src/features/connections/platform-integrations-api';
import { expectUnavailableToast, renderWithNotifications } from '../../support/notify-harness';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const REFUSED = (method: string) => new MethodRefusedError(method, 'METHOD_NOT_FOUND', 'catalog sentence');

function renderSection(api: PlatformIntegrationsApi = createMockPlatformIntegrationsApi()) {
  const user = userEvent.setup();
  const view = renderWithNotifications(<PlatformIntegrationsSection api={api} />);
  return { user, api, ...view };
}

async function openDetail(user: ReturnType<typeof userEvent.setup>): Promise<HTMLElement> {
  const list = await screen.findByRole('list', { name: '平台接入列表' });
  const row = within(list).getByText('feishu-main').closest('li') as HTMLElement;
  await user.click(within(row).getByRole('button', { name: '详情' }));
  return row;
}

describe('platform integrations: absent ports are not rendered', () => {
  it('offers only platforms that can be configured', () => {
    expect(PLATFORM_PROVIDERS.map(item => item.id)).toEqual(['lark']);
    expect(PLATFORM_PROVIDERS.every(item => !('available' in item) && !('reason' in item))).toBe(true);
  });

  it('has no delete control when the port has no remove', async () => {
    const base = createMockPlatformIntegrationsApi();
    const api: PlatformIntegrationsApi = { ...base, remove: undefined, removePreview: undefined };
    const { user } = renderSection(api);
    const row = await openDetail(user);

    expect(within(row).queryByRole('button', { name: '删除接入' })).toBeNull();
    expect(row.textContent ?? '').not.toMatch(/尚未接入/);
  });

  it('has no add control without register and without onboarding', async () => {
    const base = createMockPlatformIntegrationsApi();
    renderSection({ ...base, register: undefined, beginAdd: undefined });
    await screen.findByRole('list', { name: '平台接入列表' });

    expect(screen.queryByRole('button', { name: '添加接入' })).toBeNull();
  });

  it('offers only the add mode whose port exists and no mode choice for a single mode', async () => {
    const base = createMockPlatformIntegrationsApi();
    const { user } = renderSection({ ...base, register: undefined });
    await screen.findByRole('list', { name: '平台接入列表' });
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).queryByLabelText('创建新应用')).toBeNull();
    expect(within(dialog).queryByLabelText('使用已有应用')).toBeNull();
    expect(within(dialog).queryByLabelText('App ID')).toBeNull();
    expect(within(dialog).getByLabelText('名称')).toBeInTheDocument();
    expect(dialog.textContent ?? '').not.toMatch(/尚未接入/);
  });

  it('starts in the existing-app mode when only registration has a port', async () => {
    const base = createMockPlatformIntegrationsApi();
    const { user } = renderSection({ ...base, beginAdd: undefined });
    await screen.findByRole('list', { name: '平台接入列表' });
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const dialog = await screen.findByRole('dialog');

    expect(within(dialog).queryByLabelText('创建新应用')).toBeNull();
    expect(within(dialog).getByLabelText('App ID')).toBeInTheDocument();
    expect(within(dialog).getByLabelText('App Secret')).toBeInTheDocument();
  });

  it('has no identities block when the port cannot list identities', async () => {
    const base = createMockPlatformIntegrationsApi();
    const { user } = renderSection({ ...base, listIdentities: undefined, syncIdentities: undefined });
    const row = await openDetail(user);

    expect(within(row).queryByRole('heading', { name: '平台身份' })).toBeNull();
    expect(within(row).queryByRole('button', { name: '同步平台身份' })).toBeNull();
    expect(row.textContent ?? '').not.toMatch(/未接入|暂不|不可用/);
  });

  it('hides the identities block when its read is refused and does not show it as empty', async () => {
    const base = createMockPlatformIntegrationsApi();
    const api: PlatformIntegrationsApi = { ...base, listIdentities: async () => { throw REFUSED('adapter.identities.list'); } };
    const { user } = renderSection(api);
    const row = await openDetail(user);

    await waitFor(() => expect(within(row).queryByText('正在读取平台身份…')).toBeNull());
    expect(within(row).queryByRole('heading', { name: '平台身份' })).toBeNull();
    expect(within(row).queryByText(/没有可显示的平台身份|尚未同步平台身份/)).toBeNull();
  });

  it('renders nothing without a port', () => {
    const { container } = render(<PlatformIntegrationsSection />);
    expect(container.textContent).toBe('');
    expect(container.querySelector('.platform-integrations')).toBeNull();
  });

  it('renders nothing when the list read is refused before dispatch', async () => {
    const base = createMockPlatformIntegrationsApi();
    const { container } = renderSection({ ...base, list: async () => { throw REFUSED('adapter.list'); } });
    await waitFor(() => expect(screen.queryByText('正在读取平台接入')).toBeNull());

    expect(container.querySelector('.platform-integrations')).toBeNull();
    expect(screen.queryByText('暂无平台接入')).toBeNull();
    expect(screen.queryByRole('button', { name: '添加接入' })).toBeNull();
  });
});

describe('platform integrations: refused operations', () => {
  it('reports a refused add before the dialog opens', async () => {
    const base = createMockPlatformIntegrationsApi();
    const api: PlatformIntegrationsApi = {
      ...base,
      capabilities: { ...base.capabilities, add: { available: false, code: 'METHOD_NOT_FOUND', source: 'adapter.register' } },
    };
    const { user } = renderSection(api);
    await screen.findByRole('list', { name: '平台接入列表' });

    await user.click(screen.getByRole('button', { name: '添加接入' }));

    expectUnavailableToast('添加接入暂不可用。');
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByRole('button', { name: '添加接入' })).toBeEnabled();
    expect(document.querySelector('.platform-integrations__hint')).toBeNull();
  });

  it('reports a refused submit once and keeps the form editable', async () => {
    const base = createMockPlatformIntegrationsApi();
    const api: PlatformIntegrationsApi = { ...base, beginAdd: vi.fn(async () => { throw REFUSED('adapter.onboard.begin'); }) };
    const { user } = renderSection(api);
    await screen.findByRole('list', { name: '平台接入列表' });
    await user.click(screen.getByRole('button', { name: '添加接入' }));
    const dialog = await screen.findByRole('dialog');
    await user.type(within(dialog).getByLabelText('名称'), 'feishu-new');

    await user.click(within(dialog).getByRole('button', { name: '开始添加' }));

    expectUnavailableToast('开始添加暂不可用。');
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: '开始添加' })).toBeEnabled();
    expect(within(dialog).getByLabelText('名称')).toHaveValue('feishu-new');
    expect(within(dialog).queryByRole('alert')).toBeNull();
  });

  it.each([
    ['启动', 'adapter.start'],
    ['授权', 'adapter.authorization.begin'],
  ] as const)('reports a refused %s without any inline notice', async (label, method) => {
    const base = createMockPlatformIntegrationsApi();
    const api: PlatformIntegrationsApi = {
      ...base,
      start: async () => { throw REFUSED(method); },
      beginAuthorization: async () => { throw REFUSED(method); },
    };
    const { user } = renderSection(api);
    const row = await openDetail(user);
    const control = within(row).getByRole('button', { name: label });

    await user.click(control);

    expectUnavailableToast(`${label}暂不可用。`);
    expect(control).toBeEnabled();
    expect(document.querySelector('.ui-notice')).toBeNull();
    expect(within(row).queryByText(/尚未确认|结果未确认/)).toBeNull();
  });

  it('reports a refused identity sync and does not re-read the identities', async () => {
    const base = createMockPlatformIntegrationsApi();
    const listIdentities = vi.spyOn(base, 'listIdentities');
    const api: PlatformIntegrationsApi = { ...base, syncIdentities: async () => { throw REFUSED('adapter.identities.sync'); } };
    const { user } = renderSection(api);
    const row = await openDetail(user);
    await within(row).findByText('尚未同步平台身份。');
    const reads = listIdentities.mock.calls.length;

    await user.click(within(row).getByRole('button', { name: '同步平台身份' }));

    expectUnavailableToast('同步平台身份暂不可用。');
    expect(within(row).getByRole('button', { name: '同步平台身份' })).toBeEnabled();
    expect(listIdentities.mock.calls.length).toBe(reads);
    expect(document.querySelector('.ui-notice')).toBeNull();
  });

  it('reports a refused capability before an identity sync is dispatched', async () => {
    const base = createMockPlatformIntegrationsApi();
    const sync = vi.fn();
    const api: PlatformIntegrationsApi = {
      ...base,
      syncIdentities: sync,
      capabilities: { ...base.capabilities, identities: { available: false, code: 'METHOD_NOT_FOUND', source: 'adapter.identities.sync' } },
    };
    const { user } = renderSection(api);
    const row = await openDetail(user);

    await user.click(within(row).getByRole('button', { name: '同步平台身份' }));

    expectUnavailableToast('同步平台身份暂不可用。');
    expect(sync).not.toHaveBeenCalled();
  });

  it('reports a refused configuration reload once', async () => {
    const base = createMockPlatformIntegrationsApi();
    const { user } = renderSection({ ...base, reload: async () => { throw REFUSED('adapter.reload'); } });
    await screen.findByRole('list', { name: '平台接入列表' });

    await user.click(screen.getByRole('button', { name: '重新加载全部配置' }));

    expectUnavailableToast('重新加载全部配置暂不可用。');
    expect(screen.getByRole('button', { name: '重新加载全部配置' })).toBeEnabled();
    expect(document.querySelector('.ui-notice')).toBeNull();
  });

  it('raises only a notification for a refused refresh and keeps the list', async () => {
    const base = createMockPlatformIntegrationsApi();
    let reads = 0;
    const api: PlatformIntegrationsApi = {
      ...base,
      async list() {
        reads += 1;
        if (reads > 1) throw REFUSED('adapter.list');
        return base.list();
      },
    };
    const { user } = renderSection(api);
    await screen.findByRole('list', { name: '平台接入列表' });

    await user.click(screen.getByRole('button', { name: /^刷新$/ }));

    expectUnavailableToast('刷新暂不可用。');
    expect(screen.getByText('feishu-main')).toBeInTheDocument();
    expect(screen.queryByText('正在读取平台接入')).toBeNull();
    expect(screen.queryByText('暂无平台接入')).toBeNull();
    expect(screen.queryByText(/平台接入读取失败/)).toBeNull();
  });
});

describe('platform connected port', () => {
  const refuse = (code: string) => () => ({ available: false, code, reason: 'catalog sentence' }) as const;

  it('lets refusals reach the handler as MethodRefusedError and never as a sentence', async () => {
    const api = createPlatformIntegrationsApiFromClient({ adapter: {} } as never, refuse('METHOD_NOT_FOUND'));

    await expect(api.list()).rejects.toBeInstanceOf(MethodRefusedError);
    await expect(api.start('a')).rejects.toMatchObject({ method: 'adapter.start', code: 'METHOD_NOT_FOUND' });
    await expect(api.beginAuthorization!('a')).rejects.toMatchObject({ method: 'adapter.authorization.begin' });
    await expect(api.syncIdentities!('a')).rejects.toMatchObject({ method: 'adapter.identities.sync' });
    await expect(api.listIdentities!('a')).rejects.toMatchObject({ method: 'adapter.identities.list' });
  });

  it('states capability refusals as machine tokens only', () => {
    const api = createPlatformIntegrationsApiFromClient({ adapter: {} } as never, refuse('METHOD_NOT_FOUND'));

    expect(api.capabilities.authorization).toEqual({ available: false, code: 'METHOD_NOT_FOUND', source: 'adapter.authorization.begin' });
    expect(api.capabilities.identities).toEqual({ available: false, code: 'METHOD_NOT_FOUND', source: 'adapter.identities.sync' });
    expect(JSON.stringify(api.capabilities)).not.toContain('catalog sentence');
  });

  it('defines no removal when the injected client omits its methods', () => {
    const api = createPlatformIntegrationsApiFromClient({ adapter: {} } as never);

    expect(api.remove).toBeUndefined();
    expect(api.removePreview).toBeUndefined();
    expect('remove' in api.capabilities).toBe(false);
  });
});

describe('platform integrations: notices after a settled call', () => {
  const NOT_READY = () => new DomainError('DAEMON_NOT_READY', 'daemon not ready', null, {
    method: 'adapter.authorization.begin', requestId: 'r', status: 503, catalogDigest: null,
    attempts: 1, startedAt: 0, durationMs: 0, dispatch: 'not-dispatched',
  });
  const UNCONFIRMED = '授权结果未确认，请刷新授权状态。';

  it('clears an earlier authorization warning once a later attempt succeeds', async () => {
    const base = createMockPlatformIntegrationsApi();
    let calls = 0;
    const api: PlatformIntegrationsApi = {
      ...base,
      beginAuthorization: async name => {
        calls += 1;
        if (calls === 1) throw NOT_READY();
        return base.beginAuthorization!(name);
      },
    };
    const { user } = renderSection(api);
    const row = await openDetail(user);

    await user.click(within(row).getByRole('button', { name: '授权' }));
    expect(await screen.findByText(UNCONFIRMED)).toBeInTheDocument();
    await user.click(within(row).getByRole('button', { name: '授权' }));

    await waitFor(() => expect(calls).toBe(2));
    await waitFor(() => expect(screen.queryByText(UNCONFIRMED)).toBeNull());
  });

  it('keeps an earlier notice when a later authorization is refused', async () => {
    const base = createMockPlatformIntegrationsApi();
    let calls = 0;
    const api: PlatformIntegrationsApi = {
      ...base,
      beginAuthorization: async () => {
        calls += 1;
        if (calls === 1) throw NOT_READY();
        throw REFUSED('adapter.authorization.begin');
      },
    };
    const { user } = renderSection(api);
    const row = await openDetail(user);

    await user.click(within(row).getByRole('button', { name: '授权' }));
    expect(await screen.findByText(UNCONFIRMED)).toBeInTheDocument();
    await user.click(within(row).getByRole('button', { name: '授权' }));

    expectUnavailableToast('授权暂不可用。');
    expect(screen.getByText(UNCONFIRMED)).toBeInTheDocument();
  });

  it('clears an earlier warning once a later authorization refresh succeeds', async () => {
    const base = createMockPlatformIntegrationsApi();
    let reads = 0;
    const api: PlatformIntegrationsApi = {
      ...base,
      authorizationOperation: async id => {
        reads += 1;
        if (reads === 1) throw NOT_READY();
        return base.authorizationOperation!(id);
      },
    };
    const { user } = renderSection(api);
    const row = await openDetail(user);
    await user.click(within(row).getByRole('button', { name: '授权' }));
    const refresh = await within(row).findByRole('button', { name: /^刷新.*状态$/ });

    await user.click(refresh);
    expect(await screen.findByText('授权记录读取失败。')).toBeInTheDocument();
    await user.click(within(row).getByRole('button', { name: /^刷新.*状态$/ }));

    await waitFor(() => expect(reads).toBe(2));
    await waitFor(() => expect(screen.queryByText('授权记录读取失败。')).toBeNull());
  });

  it('clears an earlier warning once a later removal preview succeeds', async () => {
    const base = createMockPlatformIntegrationsApi();
    let calls = 0;
    const api: PlatformIntegrationsApi = {
      ...base,
      removePreview: async name => {
        calls += 1;
        if (calls === 1) throw NOT_READY();
        return base.removePreview!(name);
      },
    };
    const { user } = renderSection(api);
    const row = await openDetail(user);

    await user.click(within(row).getByRole('button', { name: '删除接入' }));
    expect(await screen.findByText('删除预览尚未确认。')).toBeInTheDocument();
    await user.click(within(row).getByRole('button', { name: '删除接入' }));

    await waitFor(() => expect(calls).toBe(2));
    await waitFor(() => expect(screen.queryByText('删除预览尚未确认。')).toBeNull());
  });

  it('keeps an earlier removal warning when a later preview is refused', async () => {
    const base = createMockPlatformIntegrationsApi();
    let calls = 0;
    const api: PlatformIntegrationsApi = {
      ...base,
      removePreview: async () => {
        calls += 1;
        if (calls === 1) throw NOT_READY();
        throw REFUSED('adapter.remove.preview');
      },
    };
    const { user } = renderSection(api);
    const row = await openDetail(user);

    await user.click(within(row).getByRole('button', { name: '删除接入' }));
    expect(await screen.findByText('删除预览尚未确认。')).toBeInTheDocument();
    await user.click(within(row).getByRole('button', { name: '删除接入' }));

    expectUnavailableToast('删除接入暂不可用。');
    expect(screen.getByText('删除预览尚未确认。')).toBeInTheDocument();
  });

  it('clears an earlier warning when a later refresh fails without a refusal', async () => {
    const base = createMockPlatformIntegrationsApi();
    let reads = 0;
    let calls = 0;
    const api: PlatformIntegrationsApi = {
      ...base,
      beginAuthorization: async () => { calls += 1; throw NOT_READY(); },
      async list() {
        reads += 1;
        if (reads > 1) throw NOT_READY();
        return base.list();
      },
    };
    const { user } = renderSection(api);
    const row = await openDetail(user);
    await user.click(within(row).getByRole('button', { name: '授权' }));
    expect(await screen.findByText(UNCONFIRMED)).toBeInTheDocument();
    expect(calls).toBe(1);

    await user.click(screen.getByRole('button', { name: /^刷新$/ }));

    await waitFor(() => expect(screen.queryByText(UNCONFIRMED)).toBeNull());
  });
});
