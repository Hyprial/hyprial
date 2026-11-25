/*
 * Decision record (integration 2026-11-15): a destination with nothing to show is
 * absent from the nav, its route renders the start page, and no link anywhere points
 * at it. The device page is empty for a ready, authenticated account when the catalog
 * has none of its four cards; the Agent detail offers 开始对话 only while 聊天 is shown.
 */
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { App } from '../../src/App';
import { createBackendConnection } from '../../src/app/backend';
import { createConnectedGuiCapabilities } from '../../src/app/capabilities';
import { AGENTS_LIST_ROWS_DATA } from '../fixtures/agents/agent-list';

const DESCRIPTOR = {
  interfaceVersion: 1,
  driver: 'contract-mock',
  phase: 'ready',
  generation: 'generation-1',
  catalogDigest: 'sha256:catalog',
} as const;

const entry = (name: string) => ({ name, callable: true, state: 'available', mutates: false }) as const;

function resultFor(method: string, authenticated: boolean): unknown {
  switch (method) {
    case 'onboarding.status': return authenticated
      ? { operations: [], nextActions: [], ok: true, phase: 'serving', authenticated: true, owner: 'test@example.invalid', deviceReady: true, initialized: true }
      : { operations: [], nextActions: [], ok: true, phase: 'serving', authenticated: false, deviceReady: false, initialized: false };
    case 'agent.list': return { ok: true, agents: AGENTS_LIST_ROWS_DATA, agentsWithoutRoutine: [] };
    default: throw new Error(`unexpected method ${method}`);
  }
}

function mount(methods: readonly string[], initialPage: 'start' | 'device' | 'contacts', authenticated = true) {
  const connection = createBackendConnection({
    async invoke<TResult>(method: string): Promise<TResult> {
      return resultFor(method, authenticated) as TResult;
    },
  }, { ...DESCRIPTOR, methods: methods.map(entry) });
  render(<App initialPage={initialPage} backend={{ status: 'connected', connection }}
    capabilities={createConnectedGuiCapabilities(connection)} />);
}

function mountRecovery(message?: string) {
  const connection = createBackendConnection({
    async invoke<TResult>(): Promise<TResult> {
      return { operations: [], nextActions: [], ok: true, phase: 'serving', authenticated: true,
        owner: 'test@example.invalid', deviceReady: true, initialized: false } as TResult;
    },
  }, { ...DESCRIPTOR, methods: [entry('onboarding.status')] });
  const capabilities = createConnectedGuiCapabilities(connection);
  render(<App initialPage="device" backend={{ status: 'connected', connection }}
    capabilities={{ ...capabilities, firstRun: {
      ...capabilities.firstRun,
      getAccountState: async () => ({ account: { status: 'authenticated' as const, owner: 'test@example.invalid' },
        device: { status: 'not-ready' as const, ...(message === undefined ? {} : { message }) } }),
    } }} />);
}

describe('device page with no card ports', () => {
  it('has no nav entry and its route lands on Start', async () => {
    mount(['onboarding.status'], 'device');

    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(document.querySelector('[data-page="device"]')).toBeNull();
    expect(screen.queryByRole('button', { name: '设备' })).toBeNull();
  });

  it('keeps the device destination visible for an authenticated recovery message', async () => {
    mountRecovery('设备已登录，但初始化状态尚未完成。');

    expect(await screen.findByRole('button', { name: '设备' })).toBeInTheDocument();
    expect(screen.getByRole('region', { name: '设备状态' })).toHaveTextContent('设备已登录，但初始化状态尚未完成。');
  });

  it('still hides the device destination when the recovery message is empty', async () => {
    mountRecovery();

    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '设备' })).toBeNull();
  });
});

describe('Agent detail without a chat destination', () => {
  it('offers no 开始对话 when 聊天 is hidden', async () => {
    mount(['onboarding.status', 'agent.list'], 'contacts');

    const user = userEvent.setup();
    await user.click(await screen.findByTestId('contact-squire'));
    expect(await screen.findByTestId('agent-detail-panel')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '聊天' })).toBeNull();
    expect(screen.queryByRole('button', { name: '开始对话' })).toBeNull();
  });
});

describe('signed-out Start links follow the entries shown in that state', () => {
  const SIGNED_OUT_PORTS = ['onboarding.status', 'agent.list', 'agent.secret.provider-list', 'adapter.list'];

  it('shows both links when their destinations are shown, and asks for login on use', async () => {
    mount(SIGNED_OUT_PORTS, 'start', false);

    const user = userEvent.setup();
    expect(await screen.findByRole('button', { name: '创建 Agent' })).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: '查看 Agent 设置' }));
    expect(await screen.findByText('请先登录')).toBeInTheDocument();
  });

  it('omits the settings link when the connections entry is hidden, keeping the create card', async () => {
    mount(['onboarding.status', 'agent.list'], 'start', false);

    expect(await screen.findByRole('button', { name: '创建 Agent' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '查看 Agent 设置' })).toBeNull();
  });

  it('omits the create card when the Agent entry is hidden', async () => {
    mount(['onboarding.status', 'agent.secret.provider-list'], 'start', false);

    expect(await screen.findByRole('button', { name: '查看 Agent 设置' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '创建 Agent' })).toBeNull();
  });
});

describe('Start summary rows follow their sources', () => {
  it('leaves out Agent 状态 and 平台绑定 when their sources are absent', async () => {
    mount(['onboarding.status'], 'start');

    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(document.querySelector('[data-summary="agents"]')).toBeNull();
    expect(document.querySelector('[data-summary="platform-bindings"]')).toBeNull();
  });

  it('keeps 未确认 for a present source that has not confirmed', async () => {
    mount(['onboarding.status', 'agent.list', 'adapter.list'], 'start', false);

    expect(await screen.findByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(document.querySelector('[data-summary="agents"]')).toHaveTextContent('未确认');
    expect(document.querySelector('[data-summary="platform-bindings"]')).toHaveTextContent('未确认');
  });
});
