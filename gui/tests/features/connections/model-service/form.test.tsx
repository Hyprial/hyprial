import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { CredentialsSection } from '../../../../src/features/connections/CredentialsSection';
import type { SharedCredentialsApi, CredentialProbe } from '../../../../src/features/connections/shared-credentials-api';
import { keyCompatibleWithHarness } from '../../../../src/features/agents/impl/AgentForm';

beforeAll(() => {
  window.Element.prototype.hasPointerCapture ??= () => false;
  window.Element.prototype.releasePointerCapture ??= () => {};
  window.Element.prototype.setPointerCapture ??= () => {};
  window.HTMLElement.prototype.scrollIntoView ??= () => {};
});

function setup() {
  const api: SharedCredentialsApi = {
    listKeys: vi.fn(async () => []), listSecretGrants: async () => [],
    addKey: vi.fn(async () => ({ status: 'saved' as const })),
    configureKey: vi.fn(async () => ({ status: 'saved' as const })),
    replaceKey: vi.fn(async () => ({ status: 'saved' as const })),
    revokeGrant: vi.fn(async () => ({ status: 'saved' as const })),
    testKey: vi.fn(async () => ({ kind: 'model' as const, status: 200, ok: true, latencyMs: 10 })),
  };
  return api;
}

async function openCustom() {
  const user = userEvent.setup();
  await screen.findByText('暂无 Key');
  await user.click(screen.getByRole('button', { name: '添加 Key' }));
  await user.click(screen.getByLabelText('服务地址来源'));
  await user.click(screen.getByRole('option', { name: '自定义地址' }));
  await user.type(screen.getByLabelText('服务地址'), 'http://127.0.0.1:45678/v1');
  await user.type(screen.getByLabelText('Key'), 'synthetic');
  return user;
}

it('uses configure for custom save without a hidden probe or ordinary write', async () => {
  const api = setup(); render(<CredentialsSection api={api} />);
  const user = await openCustom();
  expect(screen.getByText(/仅支持 Pi.*deepseek-flash/)).toBeVisible();
  await user.click(screen.getByRole('button', { name: '保存' }));
  expect(api.configureKey).toHaveBeenCalledExactlyOnceWith({ provider: 'deepseek', label: 'DeepSeek', value: 'synthetic', endpoint: 'http://127.0.0.1:45678/v1' });
  expect(api.addKey).not.toHaveBeenCalled();
  expect(api.testKey).not.toHaveBeenCalled();
});

it('discards a late probe result after closing and reopening the form', async () => {
  const api = setup(); let finish!: (value: CredentialProbe) => void;
  api.testKey = vi.fn(() => new Promise<CredentialProbe>(resolve => { finish = resolve; }));
  render(<CredentialsSection api={api} />);
  const user = await openCustom();
  await user.type(screen.getByLabelText('测试模型'), 'deepseek-flash');
  await user.click(screen.getByRole('button', { name: '测试' }));
  expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: '取消' }));
  await user.click(screen.getByRole('button', { name: '添加 Key' }));
  await act(async () => finish({ kind: 'model', status: 200, ok: true, latencyMs: 10 }));
  expect(screen.queryByText(/测试通过/)).toBeNull();
  expect(screen.getByLabelText('Key')).toHaveValue('');
  expect(api.configureKey).not.toHaveBeenCalled();
});

it('admits only an explicit supported Pi route, while retaining ordinary compatibility', () => {
  expect(keyCompatibleWithHarness('pi', 'deepseek', 'https://example.invalid/v1', 'openai-completions')).toBe(true);
  expect(keyCompatibleWithHarness('pi', 'deepseek', 'https://example.invalid/v1')).toBe(false);
  expect(keyCompatibleWithHarness('codex', 'deepseek', 'https://example.invalid/v1', 'openai-completions')).toBe(false);
  expect(keyCompatibleWithHarness('pi', 'deepseek', 'https://example.invalid/v1', 'anthropic-messages')).toBe(false);
  expect(keyCompatibleWithHarness('pi', 'deepseek')).toBe(true);
});

it('keeps ordinary saving available without advertising an unsupported provider test', async () => {
  const api = { ...setup(), testProviders: ['deepseek', 'openai', 'anthropic', 'hyprial'] };
  render(<CredentialsSection api={api} />);
  const user = userEvent.setup();
  await screen.findByText('暂无 Key');
  await user.click(screen.getByRole('button', { name: '添加 Key' }));
  await user.click(screen.getByLabelText('模型服务商'));
  await user.click(screen.getByRole('option', { name: 'Kimi Code' }));
  await user.type(screen.getByLabelText('Key'), 'synthetic');
  expect(screen.queryByRole('button', { name: '测试' })).toBeNull();
  expect(screen.queryByLabelText('测试模型')).toBeNull();
  await user.click(screen.getByRole('button', { name: '保存' }));
  expect(api.addKey).toHaveBeenCalledExactlyOnceWith('kimi-coding', 'Kimi-Code', 'synthetic');
  expect(api.testKey).not.toHaveBeenCalled();
});
