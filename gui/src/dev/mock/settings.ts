/**
 * Decision record (M2 fixture, 2026-11-12): the settings and device cards use
 * the same capability interfaces as the connected app, but this data lives in
 * the explicit dev composition only. Mutations are local to one preview mount
 * and never imply daemon or organization publication success.
 */
import type {
  AutoupdateStatusResult,
  DaemonStatusResult,
  LogPageResult,
} from '../../client/generated/types';
import type {
  DiagnosticApi,
  LocalManagementApi,
  LogsApi,
  UpgradeStatusApi,
} from '../../features/settings';
import type { AccountSnapshot } from '../../features/onboarding';

export interface DevSettingsApis {
  readonly localManagement: LocalManagementApi;
  readonly upgradeStatus: UpgradeStatusApi;
  readonly diagnostic: DiagnosticApi;
  readonly logs: LogsApi;
}

const now = () => Date.now();

export function createDevSettingsApis(options: {
  readonly getAccountState?: () => Promise<AccountSnapshot>;
  readonly actorCount?: () => number;
} = {}): DevSettingsApis {
  async function read<T>(value: () => T): Promise<T> {
    const state = await options.getAccountState?.();
    if (state && (state.account.status !== 'authenticated' || state.device.status !== 'ready')) {
      throw new Error('DEVICE_NOT_INITIALIZED');
    }
    return value();
  }
  const daemon: DaemonStatusResult = { phase: 'serving', running: true, version: 'preview-daemon' };
  const upgrade: AutoupdateStatusResult = {
    ok: true,
    active: false,
    autoUpgradeEnabled: false,
    environmentPathDigest: 'preview-environment',
    lastError: null,
    lastExitCode: null,
    lastRun: null,
    lastSkipReason: null,
    lastTrigger: null,
    lastTriggeredAt: null,
    nextRunAt: null,
    pending: false,
    running: false,
    schedule: [{ hour: 3, minute: 0 }],
    trigger: 'daemon',
  };
  const logs: LogPageResult[] = [{
    entries: [], nextCursor: null, order: 'file-offset', partial: false,
    partialReasons: [], skippedLines: 0, snapshotExpiresInSeconds: 300,
  }];
  return {
    localManagement: { status: () => read(() => daemon) },
    upgradeStatus: {
      status: () => read(() => upgrade),
      steps: () => read(() => ({ steps: [], legacyFileCount: 0, legacyProcessCount: 0 })),
    },
    diagnostic: { read: () => read(() => ({ observedAtMs: now(), actorCount: options.actorCount?.() ?? 0, strandedCount: 0 })) },
    logs: { read: () => read(() => logs[0]) },
  };
}
