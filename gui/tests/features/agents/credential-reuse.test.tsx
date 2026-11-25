import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, describe, expect, it, vi } from 'vitest';
import { createAgentJourney } from '../../../src/dev/journey';
import { CredentialsSection } from '../../../src/features/connections';
import { CredentialsTab } from '../../../src/features/agents/impl/detail/CredentialsTab';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

async function setup() {
  const journey = createAgentJourney();
  await journey.sharedCredentials.addKey('deepseek', '模型 Key', 'synthetic');
  await journey.agents.create({ name: 'auth-agent', preferredHarness: 'claude', provider: null, model: null });
  const agent = (await journey.agents.list())[0];
  const files = [{ filename: '.credentials.json', contentBase64: 'e30=' }];
  await journey.agents.loginNative(agent.uri, 'claude', files);
  return { journey, agent, files };
}

async function choose(user: ReturnType<typeof userEvent.setup>, label: string, value: string) {
  await user.click(await screen.findByRole('combobox', { name: label }));
  await user.click(screen.getByRole('option', { name: value }));
}

function editor(data: Awaited<ReturnType<typeof setup>>) {
  const picker = vi.fn(async () => data.files);
  render(<CredentialsTab actor={data.agent.uri} preferredHarness="claude" agentsApi={data.journey.agents}
    credentialsApi={data.journey.sharedCredentials} nativeLoginFiles={picker}
    providerStatus={{ view: { status: 'ready', rows: [] }, reload: () => undefined }} onNavigateConnections={() => undefined} />);
  return picker;
}

describe('credential cards and saved Auth reuse', () => {
  it.each(['settings', 'agent'] as const)('uses the same empty state for Key and Auth in %s', async page => {
    const journey = createAgentJourney();
    if (page === 'settings') render(<CredentialsSection api={journey.sharedCredentials} />);
    else render(<CredentialsTab actor="agent:preview@example.invalid:dev-local:empty" preferredHarness="claude"
      agentsApi={journey.agents} credentialsApi={journey.sharedCredentials}
      providerStatus={{ view: { status: 'ready', rows: [] }, reload: () => undefined }}
      onNavigateConnections={() => undefined} />);
    const key = await screen.findByText(page === 'settings' ? '暂无 Key' : '无 Key 授权。');
    const auth = await screen.findByText('暂无 Auth');
    expect(key.closest('.ui-state')).not.toBeNull();
    expect(auth.closest('.ui-state')).not.toBeNull();
    expect(key.closest('.ui-state')?.className).toBe(auth.closest('.ui-state')?.className);
    expect(key.tagName).toBe(auth.tagName);
  });

  it('uses the same expandable list component for global Key and Auth', async () => {
    const user = userEvent.setup();
    const { journey } = await setup();
    render(<CredentialsSection api={journey.sharedCredentials} />);
    const keys = await screen.findByRole('list', { name: '凭据列表' });
    const auth = await screen.findByRole('list', { name: 'Auth 清单' });
    expect(keys.className).toBe(auth.className);
    expect(keys.querySelector('li')?.className).toBe(auth.querySelector('li')?.className);
    await user.click(within(auth).getByRole('button', { name: '查看详情' }));
    expect(within(auth).getByText('.credentials.json')).toBeVisible();
    expect(within(auth).queryByRole('button', { name: '移除 Auth' })).toBeNull();
    await user.click(within(keys).getByRole('button', { name: '查看详情' }));
    expect(within(keys).getByRole('button', { name: '更换 Key' })).toBeVisible();
  });

  it('returns to saved Auth after selecting Key without uploading or mutating either credential', async () => {
    const user = userEvent.setup();
    const data = await setup();
    const login = vi.spyOn(data.journey.agents, 'loginNative');
    const allow = vi.spyOn(data.journey.agents, 'allowProvider');
    const logout = vi.spyOn(data.journey.agents, 'logoutNative');
    const picker = editor(data);
    await user.click(screen.getByRole('button', { name: '编辑凭据' }));
    await screen.findByRole('combobox', { name: '登录文件' });
    await choose(user, '凭据', 'Key');
    await choose(user, '凭据', 'Auth');
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByRole('combobox', { name: '登录文件' })).toHaveTextContent('使用已有 Auth');
    expect(within(dialog).getByText('.credentials.json')).toBeVisible();
    expect(within(dialog).queryByRole('button', { name: '选择登录文件' })).toBeNull();
    await user.click(within(dialog).getByRole('button', { name: '完成' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(picker).not.toHaveBeenCalled();
    expect(login).not.toHaveBeenCalled();
    expect(allow).not.toHaveBeenCalled();
    expect(logout).not.toHaveBeenCalled();
  });

  it('uses matching expandable cards for Key and Auth inside Agent configuration', async () => {
    const user = userEvent.setup();
    const { journey, agent } = await setup();
    const key = (await journey.sharedCredentials.listKeys())[0];
    await journey.agents.allowProvider(agent.uri, key.provider, { keyId: key.keyId });
    const rows = await journey.agents.providerStatus(agent.uri);
    render(<CredentialsTab actor={agent.uri} preferredHarness="claude" agentsApi={journey.agents}
      credentialsApi={journey.sharedCredentials} providerStatus={{ view: { status: 'ready', rows }, reload: () => undefined }}
      onNavigateConnections={() => undefined} />);
    const keys = await screen.findByRole('list', { name: 'Key 授权' });
    const auth = await screen.findByRole('list', { name: 'Auth 清单' });
    expect(keys.className).toBe(auth.className);
    expect(within(keys).getByRole('listitem').className).toBe(within(auth).getByRole('listitem').className);
    expect(within(keys).getByText('已授权')).toBeVisible();
    expect(within(auth).getByText('已保存')).toBeVisible();
    expect(within(keys).queryByText('到期')).toBeNull();
    expect(within(auth).getByText('登录文件')).toBeVisible();
    for (const list of [keys, auth]) {
      await user.click(within(list).getByRole('button', { name: '收起' }));
      expect(within(list).queryByRole('definition')).toBeNull();
      await user.click(within(list).getByRole('button', { name: '查看详情' }));
    }
    expect(within(keys).getByText('模型 Key（deepseek）')).toBeVisible();
    expect(within(auth).getByText('.credentials.json')).toBeVisible();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('uploads only on explicit replacement and keeps selected files across type changes', async () => {
    const user = userEvent.setup();
    const data = await setup();
    await data.journey.agents.start(data.agent.actor, { harness: 'claude' });
    const login = vi.spyOn(data.journey.agents, 'loginNative');
    const picker = editor(data);
    await user.click(screen.getByRole('button', { name: '编辑凭据' }));
    await choose(user, '登录文件', '上传替换');
    await user.click(screen.getByRole('button', { name: '选择登录文件' }));
    await choose(user, '凭据', 'Key');
    await choose(user, '凭据', 'Auth');
    const dialog = screen.getByRole('dialog');
    expect(within(dialog).getByText('已选择：.credentials.json')).toBeVisible();
    await user.click(within(dialog).getByRole('button', { name: '保存' }));
    expect(picker).toHaveBeenCalledTimes(1);
    expect(login).toHaveBeenCalledWith(data.agent.uri, 'claude', data.files, { replace: true });
    expect((await data.journey.sharedCredentials.nativeAuth.list())[0].startupConfirmedAt).toBeNull();
  });

  it('keeps saved Auth available after saving a Key authorization and reopening the editor', async () => {
    const user = userEvent.setup();
    const data = await setup();
    const before = await data.journey.sharedCredentials.nativeAuth.list(data.agent.uri);
    const login = vi.spyOn(data.journey.agents, 'loginNative');
    const allow = vi.spyOn(data.journey.agents, 'allowProvider');
    editor(data);
    await user.click(screen.getByRole('button', { name: '编辑凭据' }));
    await screen.findByRole('combobox', { name: '登录文件' });
    await choose(user, '凭据', 'Key');
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '保存' }));
    expect(allow).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole('button', { name: '编辑凭据' }));
    await screen.findByRole('combobox', { name: '登录文件' });
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: '完成' }));
    expect(await data.journey.sharedCredentials.nativeAuth.list(data.agent.uri)).toEqual(before);
    expect(login).not.toHaveBeenCalled();
    expect(allow).toHaveBeenCalledTimes(1);
  });

  it('rechecks the existing record and refuses reuse after it was removed', async () => {
    const user = userEvent.setup();
    const data = await setup();
    editor(data);
    await user.click(screen.getByRole('button', { name: '编辑凭据' }));
    const finish = await screen.findByRole('button', { name: '完成' });
    await data.journey.agents.logoutNative(data.agent.uri, 'claude');
    await user.click(finish);
    expect(screen.getByRole('dialog')).toBeVisible();
    expect(within(screen.getByRole('dialog')).getByText('已有 Auth 已移除，请选择登录文件。')).toBeVisible();
    expect(screen.getByRole('button', { name: '选择登录文件' })).toBeVisible();
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  });
});
