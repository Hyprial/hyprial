/** Device reads are admitted only by authenticated, fully initialized state. */
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expect, it, vi } from 'vitest';
import { createBackendConnection } from '../../src/app/backend';
import {
  GuiCapabilitiesProvider,
  createConnectedGuiCapabilities,
  createWebGuiCapabilities,
  type GuiCapabilities,
} from '../../src/app/capabilities';
import { DevicePage } from '../../src/pages/DevicePage';
import { expectUnavailableToast, renderWithNotifications } from '../support/notify-harness';
import { DomainError, HttpError } from '../../src/client';
import {
  createDiagnosticApiFromClient,
  createSettingsReadApis,
  createConnectedDeviceActions,
} from '../../src/features/settings';
import {
  type AccountSnapshot,
  type DeviceActionResult,
  type FirstRunActions,
} from '../../src/features/onboarding';
import { stubFirstRunActions } from '../support/first-run-actions';

function readClient() {
  return {
    daemon: {
      status: vi.fn(async () => ({ phase: 'serving', running: true as const, version: '9.9.9-test' })),
    },
    autoupdate: {
      status: vi.fn(async () => ({
        active: false,
        autoUpgradeEnabled: null,
        environmentPathDigest: 'digest',
        lastExitCode: null,
        lastRun: null,
        lastSkipReason: null,
        lastTrigger: null,
        lastTriggeredAt: null,
        nextRunAt: null,
        ok: true as const,
        pending: false,
        running: false,
        schedule: [],
        trigger: 'daemon' as const,
      })),
    },
    upgrade: {
      steps: {
        status: vi.fn(async () => ({ steps: [], legacyArgvFiles: [], legacyProcesses: [], pending: [] })),
      },
    },
    log: {
      page: vi.fn(async () => ({
        entries: [],
        nextCursor: null,
        order: 'file-offset' as const,
        partial: false,
        partialReasons: [],
        skippedLines: 0,
        snapshotExpiresInSeconds: 60,
      })),
    },
    identity: {
      users: { list: vi.fn(async () => ({ users: [] })) },
      bindings: { list: vi.fn(async () => ({ bindings: [] })) },
      resolve: vi.fn(),
    },
    top: {
      snapshot: vi.fn(async () => ({ daemon: {}, quota: {}, actors: [], stranded: [] })),
    },
  };
}

type ReadClient = ReturnType<typeof readClient>;

function firstRunForDevice(overrides: Partial<FirstRunActions> = {}): FirstRunActions {
  return {
    ...stubFirstRunActions(),
    getAccountState: vi.fn(async (): Promise<AccountSnapshot> => ({
      account: { status: 'authenticated' },
      device: { status: 'not-ready' },
    })),
    initializeDevice: vi.fn(async (): Promise<DeviceActionResult> => ({ status: 'ready' })),
    ...overrides,
  };
}

function deviceCapabilities(
  client: ReadClient,
  firstRun: FirstRunActions,
): GuiCapabilities {
  const reads = createSettingsReadApis(
    client as unknown as Parameters<typeof createSettingsReadApis>[0],
  );
  return {
    ...createWebGuiCapabilities(),
    firstRun,
    localManagement: reads.localManagement,
    upgradeStatus: reads.upgradeStatus,
    logs: reads.logs,
    diagnostic: createDiagnosticApiFromClient(
      client as unknown as Parameters<typeof createDiagnosticApiFromClient>[0],
    ),
  };
}

function renderDevicePage(capabilities: GuiCapabilities) {
  return render(
    <GuiCapabilitiesProvider value={capabilities}>
      <DevicePage />
    </GuiCapabilitiesProvider>,
  );
}

const ready = (): AccountSnapshot => ({ account: { status: 'authenticated' }, device: { status: 'ready' } });
const empty = (): AccountSnapshot => ({ account: { status: 'unauthenticated' }, device: { status: 'not-ready' } });
const prepared = (): AccountSnapshot => ({ account: { status: 'authenticated' }, device: { status: 'not-ready' } });
function expectNoReads(client: ReadClient) {
  for (const read of [client.daemon.status, client.autoupdate.status, client.upgrade.steps.status,
    client.top.snapshot, client.log.page]) expect(read).not.toHaveBeenCalled();
}

it('shows only the initialize action after login and does not issue maintenance reads', async () => {
  const client = readClient();
  renderDevicePage(deviceCapabilities(client, firstRunForDevice()));
  expect(await screen.findByRole('button', { name: '初始化当前设备' })).toBeEnabled();
  expect(screen.getAllByRole('button')).toHaveLength(1);
  expect(screen.queryByRole('heading', { name: '本机状态' })).not.toBeInTheDocument();
  expectNoReads(client);
});

it('shows a read-only recovery state when serving does not expose device initialization', async () => {
  const client = readClient();
  renderDevicePage(deviceCapabilities(client, firstRunForDevice({
    initializeDevice: undefined,
    getAccountState: async () => ({
      account: { status: 'authenticated' },
      device: { status: 'not-ready', message: '设备已登录，但初始化状态尚未完成。' },
    }),
  })));
  expect(await screen.findByRole('region', { name: '设备状态' })).toHaveTextContent('设备已登录，但初始化状态尚未完成。');
  expect(screen.queryByRole('button', { name: '初始化当前设备' })).not.toBeInTheDocument();
});

it('shows the authoritative readouts only after initialized state and omits hosts', async () => {
  const client = readClient();
  renderDevicePage(deviceCapabilities(client, firstRunForDevice({ getAccountState: async () => ready() })));
  expect(await screen.findByText('9.9.9-test')).toBeInTheDocument();
  expect(screen.getByText('已完成')).toBeInTheDocument();
  for (const name of ['本机状态', '服务更新状态', '运行诊断', '日志事件摘要']) {
    const section = screen.getByRole('region', { name });
    expect(within(section).getByRole('heading', { name })).toBeInTheDocument();
    expect(within(section).getByRole('button', { name: `刷新${name}` })).toHaveTextContent('刷新');
  }
  await waitFor(() => expect(client.log.page).toHaveBeenCalledTimes(1));
  expect(client.upgrade.steps.status).toHaveBeenCalledTimes(1);
  for (const name of ['主机列表', '用户与平台身份绑定', '网络状态', '模型服务']) {
    expect(screen.queryByText(name)).not.toBeInTheDocument();
  }
});

it('keeps logged-out devices empty and issues no reads even if a device key already exists', async () => {
  const client = readClient();
  const getAccountState = vi.fn(async () => ({ ...empty(), device: { status: 'ready' as const } }));
  renderDevicePage(deviceCapabilities(client, firstRunForDevice({ getAccountState })));
  await userEvent.setup().click(await screen.findByRole('button', { name: '初始化当前设备' }));
  expect(await screen.findByText('请先登录')).toBeInTheDocument();
  expectNoReads(client);
});

it('reveals reads only after the initialization operation completes and is confirmed', async () => {
  const client = readClient();
  let initialized = false;
  const initializeDevice = vi.fn(async (): Promise<DeviceActionResult> => { initialized = true; return { status: 'ready' }; });
  renderDevicePage(deviceCapabilities(client, firstRunForDevice({
    getAccountState: async () => initialized ? ready() : prepared(), initializeDevice,
  })));
  await userEvent.setup().click(await screen.findByRole('button', { name: '初始化当前设备' }));
  expect(await screen.findByText('9.9.9-test')).toBeInTheDocument();
  expect(initializeDevice).toHaveBeenCalledTimes(1);
  expect(screen.queryByRole('button', { name: '初始化当前设备' })).not.toBeInTheDocument();
});

it.each(['failed', 'unavailable', 'unknown'] as const)('preserves a %s result when status still says not initialized', async status => {
  const client = readClient();
  const initializeDevice = vi.fn(async (): Promise<DeviceActionResult> => ({ status, message: 'TEST_INIT_RESULT' }));
  renderDevicePage(deviceCapabilities(client, firstRunForDevice({ initializeDevice })));
  await userEvent.setup().click(await screen.findByRole('button', { name: '初始化当前设备' }));
  expect(await screen.findByText('TEST_INIT_RESULT')).toBeInTheDocument();
  expectNoReads(client);
  const button = screen.getByRole('button', { name: '初始化当前设备' });
  if (status === 'unknown') expect(button).toBeDisabled();
  expect(screen.getByRole('button', { name: '刷新初始化状态' })).toBeEnabled();
});

it('disables initialization after a dispatched device outcome-unknown domain error', async () => {
  const initialize = vi.fn().mockRejectedValue(new DomainError('SUBMIT_OUTCOME_UNKNOWN', 'unknown', { retryable: false }, {
    method: 'onboarding.initialize', requestId: 'unknown-device', status: 500,
    dispatch: 'dispatched', catalogDigest: 'test', attempts: 1, startedAt: 1, durationMs: 1,
  }));
  const ensure = vi.fn();
  const client = { onboarding: { status: vi.fn().mockResolvedValue({ authenticated: true, deviceReady: false,
    initialized: false, phase: 'bootstrap', operations: [] }), device: { ensure }, initialize } };
  const actions = createConnectedDeviceActions(client as unknown as Parameters<typeof createConnectedDeviceActions>[0]);
  renderDevicePage(deviceCapabilities(readClient(), firstRunForDevice(actions)));
  await userEvent.setup().click(await screen.findByRole('button', { name: '初始化当前设备' }));
  await waitFor(() => expect(screen.getByRole('button', { name: '初始化当前设备' })).toBeDisabled());
  expect(ensure).not.toHaveBeenCalled();
  expect(initialize).toHaveBeenCalledTimes(1);
});

it('aborts old readouts after logout and never displays a late response', async () => {
  const client = readClient();
  let finish!: (value: Awaited<ReturnType<typeof client.daemon.status>>) => void;
  client.daemon.status.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const initial = deviceCapabilities(client, firstRunForDevice({ getAccountState: async () => ready() }));
  const view = renderDevicePage(initial);
  await waitFor(() => expect(client.daemon.status).toHaveBeenCalledTimes(1));
  const after = deviceCapabilities(client, firstRunForDevice({ getAccountState: async () => empty() }));
  view.rerender(<GuiCapabilitiesProvider value={after}><DevicePage /></GuiCapabilitiesProvider>);
  await waitFor(() => expect(screen.queryByRole('region')).not.toBeInTheDocument());
  await act(async () => finish({ phase: 'serving', running: true, version: 'STALE_VERSION' }));
  expect(screen.queryByText('STALE_VERSION')).not.toBeInTheDocument();
  expect(client.daemon.status).toHaveBeenCalledTimes(1);
});

const CARDS = [
  ['localManagement', '本机状态'],
  ['upgradeStatus', '服务更新状态'],
  ['diagnostic', '运行诊断'],
  ['logs', '日志事件摘要'],
] as const;

function readyCapabilities(omit: readonly (typeof CARDS[number][0])[] = []): GuiCapabilities {
  const full = deviceCapabilities(readClient(), firstRunForDevice({ getAccountState: async () => ready() }));
  const next: Record<string, unknown> = { ...full };
  for (const key of omit) delete next[key];
  return next as unknown as GuiCapabilities;
}

it.each(CARDS)('does not render the %s card when its port is absent', async (key, name) => {
  renderDevicePage(readyCapabilities([key]));
  const others = CARDS.filter(([other]) => other !== key);
  for (const [, other] of others) expect(await screen.findByRole('region', { name: other })).toBeInTheDocument();
  expect(screen.queryByRole('region', { name })).not.toBeInTheDocument();
  expect(screen.queryByRole('heading', { name })).not.toBeInTheDocument();
});

it('renders no card and no explanation when no device port is present', async () => {
  const { container } = renderDevicePage(readyCapabilities(CARDS.map(([key]) => key)));
  await waitFor(() => expect(screen.queryByText('正在读取…')).not.toBeInTheDocument());
  expect(screen.queryByRole('region')).not.toBeInTheDocument();
  expect(container.textContent).not.toMatch(/不可用|未接入|不支持|尚未/);
});

it('omits the migration record block when the upgrade api has no steps reader', async () => {
  const full = readyCapabilities();
  const upgradeStatus = { status: full.upgradeStatus!.status };
  renderDevicePage({ ...full, upgradeStatus });
  expect(await screen.findByRole('region', { name: '服务更新状态' })).toBeInTheDocument();
  expect(screen.queryByRole('region', { name: '升级迁移记录' })).not.toBeInTheDocument();
  expect(screen.queryByText('升级迁移记录')).not.toBeInTheDocument();
});

const missingRoute = () => new HttpError('METHOD_NOT_FOUND', 'missing', 404, {
  method: 'autoupdate.status', requestId: 'missing-route', status: 404,
  dispatch: 'not-dispatched', catalogDigest: 'test', attempts: 1, startedAt: 1, durationMs: 1,
});

it('shows a missing upgrade status route as the ordinary failed read', async () => {
  const full = readyCapabilities();
  renderDevicePage({ ...full, upgradeStatus: { status: vi.fn().mockRejectedValue(missingRoute()) } });
  expect(await screen.findByText('升级状态查询失败')).toBeInTheDocument();
  expect(screen.getByText('请检查本机服务后刷新。')).toBeInTheDocument();
  expect(screen.queryByText(/暂不支持|当前服务未提供/)).not.toBeInTheDocument();
});

it('shows a missing daemon status route as the ordinary failed read', async () => {
  const full = readyCapabilities();
  renderDevicePage({ ...full, localManagement: { status: vi.fn().mockRejectedValue(missingRoute()) } });
  expect(await screen.findByText('状态读取失败')).toBeInTheDocument();
  expect(screen.getByText(/未能读取服务状态/)).toBeInTheDocument();
  expect(screen.queryByText(/未提供服务状态查询接口/)).not.toBeInTheDocument();
});

it('titles a refused or failed initialization answer the same way', async () => {
  const initializeDevice = vi.fn(async (): Promise<DeviceActionResult> => ({ status: 'unavailable', message: 'BACKEND_ANSWER' }));
  renderDevicePage(deviceCapabilities(readClient(), firstRunForDevice({ initializeDevice })));
  await userEvent.setup().click(await screen.findByRole('button', { name: '初始化当前设备' }));
  expect(await screen.findByText('BACKEND_ANSWER')).toBeInTheDocument();
  expect(screen.getByText('初始化未完成')).toBeInTheDocument();
  expect(screen.queryByText('初始化暂不可用')).not.toBeInTheDocument();
});

it('keeps initialization operable when onboarding.initialize is refused and notifies on use', async () => {
  const { createConnectedDeviceActions: connected } = await import('../../src/features/settings');
  const initialize = vi.fn();
  const client = { onboarding: { initialize } };
  const access = () => ({ available: false as const, code: 'METHOD_NOT_FOUND' });
  const actions = connected(client as unknown as Parameters<typeof connected>[0], access);
  const notify = vi.fn();
  const { renderHook } = await import('@testing-library/react');
  const { useAccountSettings } = await import('../../src/features/account');
  const full = firstRunForDevice({ ...actions });
  const { result } = renderHook(() => useAccountSettings(full, notify));
  await act(async () => {});
  await act(async () => { await result.current.run('device'); });
  expect(initialize).not.toHaveBeenCalled();
  expect(notify).toHaveBeenCalledExactlyOnceWith(expect.objectContaining({
    text: '初始化当前设备暂不可用。', source: 'onboarding.initialize · METHOD_NOT_FOUND',
  }));
  expect(result.current.snapshot.device.status).toBe('not-ready');
});

const FORMAL_READS = [
  ['daemon.status', '本机状态'],
  ['autoupdate.status', '服务更新状态'],
  ['upgrade.steps.status', '升级迁移记录'],
  ['top.snapshot', '运行诊断'],
  ['log.page', '日志事件摘要'],
] as const;

function formalDevicePage(refused: readonly string[]) {
  const client = readClient();
  const answer: Record<string, () => unknown> = {
    'onboarding.status': () => ({ ok: true, authenticated: true, deviceReady: true, initialized: true,
      nextActions: [], operations: [], owner: 'owner@example.invalid', phase: 'serving' }),
    'daemon.status': () => client.daemon.status(),
    'autoupdate.status': () => client.autoupdate.status(),
    'upgrade.steps.status': () => client.upgrade.steps.status(),
    'top.snapshot': () => client.top.snapshot(),
    'log.page': () => client.log.page(),
  };
  const connection = createBackendConnection({
    async invoke<TResult>(method: string): Promise<TResult> { return answer[method]!() as TResult; },
  }, {
    interfaceVersion: 1, driver: 'contract-mock', phase: 'ready', generation: 'generation-1', catalogDigest: 'sha256:catalog',
    methods: Object.keys(answer).filter(name => !refused.includes(name))
      .map(name => ({ name, callable: true, state: 'available', mutates: false })),
  } as never);
  renderDevicePage(createConnectedGuiCapabilities(connection));
  return client;
}

it('renders every device card in the formal composition when the catalog serves its read', async () => {
  formalDevicePage([]);
  for (const [, name] of FORMAL_READS) expect(await screen.findByRole('region', { name })).toBeInTheDocument();
});

it.each(FORMAL_READS)('does not render the card whose read %s the catalog refuses', async (method, name) => {
  const client = formalDevicePage([method]);
  for (const [other, label] of FORMAL_READS.filter(([m]) => m !== method)) {
    if (other === 'upgrade.steps.status' && method === 'autoupdate.status') continue;
    expect(await screen.findByRole('region', { name: label })).toBeInTheDocument();
  }
  expect(screen.queryByRole('region', { name })).not.toBeInTheDocument();
  const read = { 'daemon.status': client.daemon.status, 'autoupdate.status': client.autoupdate.status,
    'upgrade.steps.status': client.upgrade.steps.status, 'top.snapshot': client.top.snapshot, 'log.page': client.log.page }[method];
  expect(read).not.toHaveBeenCalled();
});

it('renders the served cards and no status block when onboarding.status is refused before dispatch', async () => {
  const client = formalDevicePage(['onboarding.status']);
  for (const [, name] of FORMAL_READS) expect(await screen.findByRole('region', { name })).toBeInTheDocument();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
  expect(screen.queryByText(/账号状态/)).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: '初始化当前设备' })).not.toBeInTheDocument();
  expect(client.daemon.status).toHaveBeenCalled();
});

it('names the refresh control it was operated by when the initialization refresh is refused', async () => {
  const refusal = { code: 'METHOD_NOT_FOUND', source: 'onboarding.status' };
  const getAccountState = vi.fn()
    .mockResolvedValueOnce({ account: { status: 'authenticated' }, device: { status: 'unknown', message: 'WAITING' } })
    .mockResolvedValue({ account: { status: 'unavailable', refusal }, device: { status: 'unknown', message: 'WAITING' } });
  renderWithNotifications(
    <GuiCapabilitiesProvider value={deviceCapabilities(readClient(), firstRunForDevice({ getAccountState }))}>
      <DevicePage />
    </GuiCapabilitiesProvider>,
  );
  const refresh = await screen.findByRole('button', { name: '刷新初始化状态' });
  await userEvent.setup().click(refresh);
  expectUnavailableToast('刷新初始化状态暂不可用。');
  expect(screen.getByRole('button', { name: '刷新初始化状态' })).toBeEnabled();
  expect(screen.getByText('WAITING')).toBeInTheDocument();
});
