/*
 * Decision record (PR 1f/F1 verification fixes, 2026-11-14):
 * - Observed on the formal composition: a catalog that lacks one method
 *   must neither show a failure card whose retry cannot succeed, nor open a
 *   form or editor before the refusal is known, nor print the catalog
 *   sentence. Each case removes exactly one method from the served catalog.
 * - A read that was dispatched and failed keeps its failure state and retry.
 */
import { screen, waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import {
  bodyTextWithoutToasts,
  renderFormalAgents,
  toastTexts,
} from '../../support/formal-agents-app';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

const BANNED = /暂不可用|尚未开放|METHOD_NOT_|GUI_CAPABILITY_UNAVAILABLE|当前后端目录|读取失败/;

type Harness = ReturnType<typeof renderFormalAgents>;

async function agentEntry(): Promise<HTMLElement> {
  return await screen.findByRole('button', { name: 'Agent' });
}

async function openDetail(harness: Harness, id = 'contact-researcher'): Promise<HTMLElement> {
  await harness.user.click(await agentEntry());
  await harness.user.click(await screen.findByTestId(id));
  return await screen.findByTestId('agent-detail-panel');
}

async function openCreateForm(harness: Harness): Promise<void> {
  await harness.user.click(await agentEntry());
  await harness.user.click(await screen.findByRole('button', { name: '创建 Agent' }));
}

describe('agent.list', () => {
  it('present: the entry and the list render', async () => {
    const harness = renderFormalAgents();
    await harness.user.click(await agentEntry());
    expect(await screen.findByTestId('contact-researcher')).toBeInTheDocument();
  });

  it('absent from the catalog: no Agent entry and no failure card', async () => {
    renderFormalAgents({ omit: ['agent.list'] });
    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Agent' })).toBeNull();
    expect(screen.queryByText(/Agent 列表读取失败/)).toBeNull();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  });

  it('dispatched and failed: the failure card keeps its retry', async () => {
    const harness = renderFormalAgents({
      handlers: { 'agent.list': () => { throw new Error('boom'); } },
    });
    await harness.user.click(await agentEntry());
    expect(await screen.findByText(/Agent 列表读取失败/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeEnabled();
  });
});

describe('agent.secret.provider-list', () => {
  it('present: the create form offers the Key credential', async () => {
    const harness = renderFormalAgents();
    await openCreateForm(harness);
    expect(await screen.findByLabelText('凭据')).toBeInTheDocument();
  });

  it('absent: the create form offers no Key and prints no catalog sentence', async () => {
    const harness = renderFormalAgents({ omit: ['agent.secret.provider-list'] });
    await openCreateForm(harness);
    await screen.findByRole('heading', { name: '创建 Agent' });
    await waitFor(() => expect(harness.calls.some((call) => call.method === 'agent.list')).toBe(true));
    expect(harness.calls.some((call) => call.method === 'agent.secret.provider-list')).toBe(false);
    expect(document.getElementById('agent-credential')?.textContent ?? '').not.toContain('Key');
    expect(bodyTextWithoutToasts()).not.toMatch(BANNED);
    expect(document.querySelector('[role="alert"]')).toBeNull();
  });

  it('absent: the credentials tab offers no Key and prints no catalog sentence', async () => {
    const harness = renderFormalAgents({ omit: ['agent.secret.provider-list'] });
    const detail = await openDetail(harness);
    await harness.user.click(within(detail).getByRole('tab', { name: '凭据' }));
    await within(detail).findByRole('heading', { name: '凭据' });
    expect(harness.calls.some((call) => call.method === 'agent.secret.provider-list')).toBe(false);
    expect(bodyTextWithoutToasts()).not.toMatch(BANNED);
  });
});

describe('link.list removal', () => {
  it('never calls link.list or renders link connection records', async () => {
    const harness = renderFormalAgents({
      extraReads: ['link.list'],
      handlers: { 'link.list': () => { throw new Error('link.list must not be called'); } },
    });
    const detail = await openDetail(harness);
    await within(detail).findByTestId('overview-notices');
    expect(harness.calls.some((call) => call.method === 'link.list')).toBe(false);
    expect(within(detail).queryByTestId('agent-more-info-link-rows')).toBeNull();
    expect(within(detail).queryByText('连接记录')).toBeNull();
    expect(within(detail).queryByText(/link 读取失败/)).toBeNull();
  });
});

describe('agent.secret.list', () => {
  it('absent: the notices block makes no no-anomaly claim', async () => {
    const harness = renderFormalAgents({ omit: ['agent.secret.list'] });
    const detail = await openDetail(harness);
    await within(detail).findByTestId('overview-activity');
    expect(harness.calls.some((call) => call.method === 'agent.secret.list')).toBe(false);
    expect(within(detail).queryByTestId('overview-notices')).toBeNull();
    expect(detail.textContent).not.toMatch(/暂无异常/);
  });

  it('read and empty: the sentence stays', async () => {
    const harness = renderFormalAgents();
    const detail = await openDetail(harness);
    const notices = await within(detail).findByTestId('overview-notices');
    await waitFor(() => expect(notices.textContent).toMatch(/暂无异常/));
  });
});

describe('agent.create entered from the Start page', () => {
  it('refused: one toast, no form, the page does not change', async () => {
    const harness = renderFormalAgents({ omit: ['agent.create'] });
    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    await harness.user.click(await screen.findByRole('button', { name: /创建 Agent/ }));
    await waitFor(() => expect(toastTexts()).toEqual(['创建 Agent暂不可用。']));
    expect(screen.queryByRole('heading', { name: '创建 Agent' })).toBeNull();
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
  });

  it('allowed: the create form opens', async () => {
    const harness = renderFormalAgents();
    await harness.user.click(await screen.findByRole('button', { name: /创建 Agent/ }));
    expect(await screen.findByRole('heading', { name: '创建 Agent' })).toBeInTheDocument();
    expect(toastTexts()).toEqual([]);
  });

  it('Agent page hidden: the Start page has no create button', async () => {
    renderFormalAgents({ omit: ['agent.list'] });
    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /创建 Agent/ })).toBeNull();
  });
});

describe('agent.rename', () => {
  it('refused: one toast and the editor does not open', async () => {
    const harness = renderFormalAgents({ omit: ['agent.rename'] });
    const detail = await openDetail(harness);
    await harness.user.click(within(detail).getByRole('button', { name: '重命名' }));
    await waitFor(() => expect(toastTexts()).toEqual(['重命名 Agent暂不可用。']));
    expect(within(detail).queryByLabelText('Agent 名称')).toBeNull();
    expect(harness.calls.some((call) => call.method === 'agent.rename')).toBe(false);
  });

  it('allowed: the editor opens', async () => {
    const harness = renderFormalAgents();
    const detail = await openDetail(harness);
    await harness.user.click(within(detail).getByRole('button', { name: '重命名' }));
    expect(await within(detail).findByLabelText('Agent 名称')).toBeInTheDocument();
    expect(toastTexts()).toEqual([]);
  });
});

describe('credentials', () => {
  async function openCredentialsEditor(harness: Harness, id = 'contact-squire'): Promise<HTMLElement> {
    const detail = await openDetail(harness, id);
    await harness.user.click(within(detail).getByRole('tab', { name: '凭据' }));
    await harness.user.click(await within(detail).findByRole('button', { name: '编辑凭据' }));
    return detail;
  }

  it('both writes refused: 编辑凭据 notifies once and opens nothing', async () => {
    const harness = renderFormalAgents({ omit: ['agent.secret.grant', 'agent.provider.login'] });
    const detail = await openDetail(harness);
    await harness.user.click(within(detail).getByRole('tab', { name: '凭据' }));
    await harness.user.click(await within(detail).findByRole('button', { name: '编辑凭据' }));
    await waitFor(() => expect(toastTexts()).toEqual(['编辑凭据暂不可用。']));
    expect(screen.queryByRole('dialog', { name: '编辑凭据' })).toBeNull();
  });

  it('Key write refused: the dialog offers only Auth, with no kind selector', async () => {
    const harness = renderFormalAgents({ omit: ['agent.secret.grant'] });
    await openCredentialsEditor(harness);
    const dialog = await screen.findByRole('dialog', { name: '编辑凭据' });
    expect(within(dialog).getByLabelText('harness')).toBeInTheDocument();
    expect(within(dialog).queryByRole('combobox', { name: '凭据' })).toBeNull();
    expect(within(dialog).queryByRole('combobox', { name: 'Key' })).toBeNull();
    expect(toastTexts()).toEqual([]);
  });

  it('Auth write refused with a compatible Key: the dialog offers only Key', async () => {
    const harness = renderFormalAgents({ omit: ['agent.provider.login'] });
    await openCredentialsEditor(harness);
    const dialog = await screen.findByRole('dialog', { name: '编辑凭据' });
    expect(within(dialog).getByRole('combobox', { name: 'Key' })).toBeInTheDocument();
    expect(within(dialog).queryByRole('combobox', { name: '凭据' })).toBeNull();
    expect(within(dialog).queryByLabelText('harness')).toBeNull();
    expect(within(dialog).queryByRole('button', { name: /选择.*登录文件/ })).toBeNull();
    expect(toastTexts()).toEqual([]);
  });

  it('Auth write refused and no compatible Key: one toast, no dialog', async () => {
    const harness = renderFormalAgents({
      omit: ['agent.provider.login'],
      handlers: { 'agent.secret.provider-list': () => ({ ok: true, keys: [] }) },
    });
    await openCredentialsEditor(harness);
    await waitFor(() => expect(toastTexts()).toEqual(['编辑凭据暂不可用。']));
    expect(screen.queryByRole('dialog', { name: '编辑凭据' })).toBeNull();
    expect(harness.calls.some((call) => call.method === 'agent.provider.login')).toBe(false);
  });

  it('Auth write refused, no Key, a stored Auth exists: the dialog offers only the existing Auth', async () => {
    const harness = renderFormalAgents({
      omit: ['agent.provider.login'],
      handlers: {
        'agent.secret.provider-list': () => ({ ok: true, keys: [] }),
        'agent.provider.auth.list': () => ({
          ok: true,
          records: [{
            actor: 'agent:owner:node:a.squire', displayName: 'squire', harness: 'pi',
            files: ['auth.json'], state: 'stored', missingFiles: [], errorCode: null,
            importedAt: null, startupConfirmedAt: null,
          }],
        }),
      },
    });
    await openCredentialsEditor(harness);
    const dialog = await screen.findByRole('dialog', { name: '编辑凭据' });
    expect(await within(dialog).findByText('auth.json')).toBeInTheDocument();
    expect(within(dialog).queryByRole('button', { name: /选择.*登录文件/ })).toBeNull();
    expect(within(dialog).queryByRole('combobox')).toBeNull();
    expect(toastTexts()).toEqual([]);
  });

  it('Auth list not callable: no Auth heading and no empty section', async () => {
    const harness = renderFormalAgents({ omit: ['agent.provider.auth.list'] });
    const detail = await openDetail(harness);
    await harness.user.click(within(detail).getByRole('tab', { name: '凭据' }));
    await within(detail).findByRole('button', { name: '编辑凭据' });
    expect(within(detail).queryByRole('heading', { name: 'Auth' })).toBeNull();
  });

  it('Auth list callable: the Auth heading renders', async () => {
    const harness = renderFormalAgents();
    const detail = await openDetail(harness);
    await harness.user.click(within(detail).getByRole('tab', { name: '凭据' }));
    expect(await within(detail).findByRole('heading', { name: 'Auth' })).toBeInTheDocument();
  });

  it('removal refused: one toast and no confirmation dialog', async () => {
    const harness = renderFormalAgents({
      omit: ['agent.provider.logout'],
      handlers: {
        'agent.provider.auth.list': () => ({
          ok: true,
          records: [{
            actor: 'agent:owner:lab-2:a.researcher', displayName: 'researcher', harness: 'claude',
            files: ['credential.json'], state: 'stored', missingFiles: [], errorCode: null,
            importedAt: null, startupConfirmedAt: null,
          }],
        }),
      },
    });
    const detail = await openDetail(harness);
    await harness.user.click(within(detail).getByRole('tab', { name: '凭据' }));
    await harness.user.click(await within(detail).findByRole('button', { name: '移除 Auth' }));
    await waitFor(() => expect(toastTexts()).toEqual(['移除登录文件暂不可用。']));
    expect(screen.queryByRole('dialog', { name: '移除 Auth' })).toBeNull();
    expect(harness.calls.some((call) => call.method === 'agent.provider.logout')).toBe(false);
  });
});

describe('create form credential kinds follow their write method', () => {
  it('agent.provider.allow absent: no Key option, create submits without a credential section for Key', async () => {
    const harness = renderFormalAgents({ omit: ['agent.secret.grant'] });
    await openCreateForm(harness);
    await screen.findByRole('heading', { name: '创建 Agent' });
    await waitFor(() => expect(harness.calls.some((call) => call.method === 'agent.list')).toBe(true));
    expect(harness.calls.some((call) => call.method === 'agent.secret.provider-list')).toBe(false);
  });

  it('both kinds unavailable: no 凭据 field at all', async () => {
    const harness = renderFormalAgents({ omit: ['agent.secret.grant', 'agent.provider.login'] });
    await openCreateForm(harness);
    await screen.findByRole('heading', { name: '创建 Agent' });
    expect(document.getElementById('agent-credential')).toBeNull();
    expect(screen.queryByText('凭据')).toBeNull();
  });
});

describe('user.provider.list refused while agent.provider.auth.list is admitted', () => {
  const STORED_AUTH = {
    'agent.provider.auth.list': () => ({
      ok: true,
      records: [{
        actor: 'agent:owner:node:a.squire', displayName: 'squire', harness: 'pi',
        files: ['auth.json'], state: 'stored', missingFiles: [], errorCode: null,
        importedAt: null, startupConfirmedAt: null,
      }],
    }),
  };

  async function openCredentials(harness: Harness): Promise<HTMLElement> {
    const detail = await openDetail(harness, 'contact-squire');
    await harness.user.click(within(detail).getByRole('tab', { name: '凭据' }));
    return detail;
  }

  it('lists the Auth files under their heading and reads no Key list', async () => {
    const harness = renderFormalAgents({ omit: ['agent.secret.provider-list'], handlers: STORED_AUTH });
    const detail = await openCredentials(harness);
    expect(await within(detail).findByRole('heading', { name: 'Auth' })).toBeInTheDocument();
    expect(await within(detail).findByRole('button', { name: '移除 Auth' })).toBeInTheDocument();
    expect(harness.calls.some((call) => call.method === 'agent.provider.auth.list')).toBe(true);
    expect(harness.calls.some((call) => call.method === 'agent.secret.provider-list')).toBe(false);
    expect(bodyTextWithoutToasts()).not.toMatch(BANNED);
    expect(bodyTextWithoutToasts()).not.toMatch(/Key 列表|Key 数据源|Key 管理/);
  });

  it('offers the stored Auth as the existing choice and saves it without a login call', async () => {
    const harness = renderFormalAgents({ omit: ['agent.secret.provider-list'], handlers: STORED_AUTH });
    const detail = await openCredentials(harness);
    await harness.user.click(await within(detail).findByRole('button', { name: '编辑凭据' }));
    const dialog = await screen.findByRole('dialog', { name: '编辑凭据' });
    expect(await within(dialog).findByText('auth.json')).toBeInTheDocument();
    expect(within(dialog).getByRole('combobox', { name: '登录文件' })).toHaveTextContent('使用已有 Auth');
    expect(within(dialog).queryByRole('combobox', { name: 'Key' })).toBeNull();
    expect(within(dialog).queryByRole('combobox', { name: '凭据' })).toBeNull();
    expect(within(dialog).queryByLabelText('替换已有 Auth')).toBeNull();
    const reads = harness.calls.filter((call) => call.method === 'agent.provider.auth.list').length;
    await harness.user.click(within(dialog).getByRole('button', { name: '完成' }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '编辑凭据' })).toBeNull());
    expect(harness.calls.filter((call) => call.method === 'agent.provider.auth.list').length).toBeGreaterThan(reads);
    expect(harness.calls.some((call) => call.method === 'agent.provider.login')).toBe(false);
    expect(toastTexts()).toEqual([]);
  });

  it('with the Auth write also refused the stored Auth still opens the editor', async () => {
    const harness = renderFormalAgents({
      omit: ['agent.secret.provider-list', 'agent.provider.login'], handlers: STORED_AUTH,
    });
    const detail = await openCredentials(harness);
    await harness.user.click(await within(detail).findByRole('button', { name: '编辑凭据' }));
    const dialog = await screen.findByRole('dialog', { name: '编辑凭据' });
    expect(await within(dialog).findByText('auth.json')).toBeInTheDocument();
    expect(within(dialog).queryByRole('combobox')).toBeNull();
    expect(toastTexts()).toEqual([]);
  });
});
