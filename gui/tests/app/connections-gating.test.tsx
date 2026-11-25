/*
 * Decision record (PR 1f/F4): the formal composition hands a connections
 * surface down only when the catalog admits the method that surface reads.
 * A catalog without that method leaves the surface absent (never an empty
 * container or an explanation); a catalog that has it renders the surface.
 * Integration (2026-11-15): with neither surface the page would be a title only,
 * so the account-menu entry, the route and the start-page link are absent.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { App } from '../../src/App';
import { createBackendConnection } from '../../src/app/backend';
import { createConnectedGuiCapabilities } from '../../src/app/capabilities';

const DESCRIPTOR = {
  interfaceVersion: 1,
  driver: 'contract-mock',
  phase: 'ready',
  generation: 'generation-1',
  catalogDigest: 'sha256:catalog',
} as const;

type Entry = { name: string; callable: true; state: 'available'; mutates: boolean };
const entry = (name: string, mutates = false): Entry => ({ name, callable: true, state: 'available', mutates });

const BASE_METHODS = [
  entry('onboarding.status'),
  entry('agent.list'),
  entry('org.list'),
  entry('targets'),
  entry('message.query'),
];

const CONNECTION_METHODS = [
  entry('agent.secret.provider-list'),
  entry('agent.secret.list'),
  entry('agent.provider.auth.list'),
  entry('adapter.list'),
];

function resultFor(method: string): unknown {
  switch (method) {
    case 'onboarding.status': return { operations: [], nextActions: [], ok: true, phase: 'serving', authenticated: true, owner: 'test@example.invalid', deviceReady: true, initialized: true };
    case 'agent.list': return { agents: [] };
    case 'org.list': return { orgs: [], leftOrgs: [] };
    case 'targets': return { targets: [] };
    case 'message.query': return { ok: true, entries: [] };
    case 'agent.secret.provider-list': return { entries: [] };
    case 'agent.secret.list': return { grants: [] };
    case 'agent.provider.auth.list': return { records: [] };
    case 'adapter.list': return { ok: true, adapters: [{
      name: 'lark-main', provider: 'lark', status: 'stopped', configured: true, desired: false,
      processRunning: false, online: false, agent: null,
    }] };
    default: throw new Error(`unexpected method ${method}`);
  }
}

function formal(missing: readonly string[]) {
  const calls: string[] = [];
  const methods = [...BASE_METHODS, ...CONNECTION_METHODS].filter(item => !missing.includes(item.name));
  const connection = createBackendConnection({
    async invoke<TResult>(method: string): Promise<TResult> {
      calls.push(method);
      return resultFor(method) as TResult;
    },
  }, { ...DESCRIPTOR, methods });
  return { connection, calls, capabilities: createConnectedGuiCapabilities(connection) };
}

function mount(missing: readonly string[]) {
  const { connection, calls, capabilities } = formal(missing);
  render(<App initialPage="connections" backend={{ status: 'connected', connection }} capabilities={capabilities} />);
  return { calls, capabilities };
}

describe('formal connections page', () => {
  it('renders both surfaces when the catalog has their list methods', async () => {
    mount([]);

    expect(await screen.findByRole('heading', { name: '凭据设置' })).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: '平台接入' })).toBeInTheDocument();
    expect(await screen.findByText('lark-main')).toBeInTheDocument();
  });

  it('leaves the credentials surface absent and never dispatches its read', async () => {
    const { calls, capabilities } = mount(['agent.secret.provider-list']);

    expect(await screen.findByRole('heading', { name: '平台接入' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '凭据设置' })).toBeNull();
    expect(capabilities.sharedCredentials).toBeUndefined();
    expect(calls).not.toContain('agent.secret.provider-list');
    expect(document.body.textContent ?? '').not.toMatch(/暂不|不可用|不支持|尚未|未接入/);
  });

  it('leaves the platform surface absent and never dispatches its read', async () => {
    const { calls, capabilities } = mount(['adapter.list']);

    expect(await screen.findByRole('heading', { name: '凭据设置' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '平台接入' })).toBeNull();
    expect(capabilities.platformIntegrations).toBeUndefined();
    expect(calls).not.toContain('adapter.list');
    expect(document.body.textContent ?? '').not.toMatch(/暂不|不可用|不支持|尚未|未接入/);
  });

  it('leaves no empty section container when a sub-read is absent', async () => {
    mount(['agent.secret.list', 'agent.provider.auth.list']);

    const title = await screen.findByRole('heading', { name: '凭据设置' });
    await waitFor(() => expect(screen.queryByText('正在读取凭据')).toBeNull());
    const section = title.closest('section') as HTMLElement;
    expect(within(section).getByText('暂无 Key')).toBeInTheDocument();
    expect(within(section).queryByText(/读取失败|暂不|不可用/)).toBeNull();
    expect(within(section).queryByRole('heading', { name: 'Auth' })).toBeNull();
    expect(within(section).queryByText(/Auth/)).toBeNull();
  });
});

describe('Agent 设置 entry follows the page sections', () => {
  async function openAccountMenu(): Promise<void> {
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: '我的账号与 Agent 设置菜单' }));
    expect(await screen.findByRole('menuitem', { name: '用户设置' })).toBeInTheDocument();
  }

  it('has no menu entry, no route and no start-page link when neither section has a port', async () => {
    const { calls } = mount(['agent.secret.provider-list', 'adapter.list']);

    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(document.querySelector('[data-page="connections"]')).toBeNull();
    expect(screen.queryByRole('button', { name: '查看 Agent 设置' })).toBeNull();
    await openAccountMenu();
    expect(screen.queryByRole('menuitem', { name: 'Agent 设置' })).toBeNull();
    expect(calls).not.toContain('agent.secret.provider-list');
    expect(calls).not.toContain('adapter.list');
  });

  it.each([
    ['only the platform section', ['agent.secret.provider-list']],
    ['only the credentials section', ['adapter.list']],
  ])('keeps the menu entry with %s', async (_name, missing) => {
    mount(missing);

    expect(document.querySelector('[data-page="connections"]')).not.toBeNull();
    await openAccountMenu();
    expect(screen.getByRole('menuitem', { name: 'Agent 设置' })).toBeInTheDocument();
  });
});
