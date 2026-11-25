import type { GeneratedClient } from '../../../../client/generated';
import type { LocalManagementApi } from '../api';
import type { LogsApi } from '../logs/api';
import type { UpgradeStatusApi, UpgradeStepsView } from '../upgradeApi';

export interface SettingsReadApis {
  readonly localManagement: LocalManagementApi;
  readonly upgradeStatus: UpgradeStatusApi;
  readonly logs: LogsApi;
}

export function createSettingsReadApis(
  client: Pick<GeneratedClient, 'daemon' | 'autoupdate' | 'upgrade' | 'log'>,
): SettingsReadApis {
  return {
    localManagement: {
      async status(signal) {
        const result = await client.daemon.status({}, { signal });
        if (!result || result.running !== true || typeof result.version !== 'string'
          || !['serving', 'reconciled'].includes(result.phase)) {
          throw new Error('DAEMON_STATUS_RESULT_INVALID');
        }
        return result;
      },
    },
    upgradeStatus: {
      status: signal => client.autoupdate.status({}, { signal }),
      async steps(signal) {
        const result = await client.upgrade.steps.status({}, { signal });
        if (!result || !Array.isArray(result.steps) || !Array.isArray(result.legacyArgvFiles)
          || !Array.isArray(result.legacyProcesses)) throw new Error('UPGRADE_STEPS_RESULT_INVALID');
        const steps: UpgradeStepsView['steps'] = result.steps.map(step => {
          if (!step || !Number.isSafeInteger(step.attempts) || step.attempts < 0
            || (step.updatedAt !== null && typeof step.updatedAt !== 'string')
            || (step.detail !== null && typeof step.detail !== 'string')) {
            throw new Error('UPGRADE_STEPS_RESULT_INVALID');
          }
          return {
            id: step.id === 'mcp-entry-v1' || step.id === 'autoupdate-timer-v1' ? step.id : 'other',
            state: step.state === 'pending' || step.state === 'done' || step.state === 'failed' ? step.state : 'unknown',
            attempts: step.attempts, updatedAt: step.updatedAt, hasDetail: step.detail !== null,
          };
        });
        return { steps, legacyFileCount: result.legacyArgvFiles.length, legacyProcessCount: result.legacyProcesses.length };
      },
    },
    logs: {
      read: (cursor, signal) => client.log.page({ limit: 20, ...(cursor === undefined ? {} : { cursor }) }, { signal }),
    },
  };
}
