import type { AutoupdateStatusResult } from './views/contracts';

export interface UpgradeStepsView {
  readonly steps: readonly { readonly id: 'mcp-entry-v1' | 'autoupdate-timer-v1' | 'other';
    readonly state: 'pending' | 'done' | 'failed' | 'unknown'; readonly attempts: number;
    readonly updatedAt: string | null; readonly hasDetail: boolean }[];
  readonly legacyFileCount: number; readonly legacyProcessCount: number;
}
export interface UpgradeStatusApi {
  steps?(signal?: AbortSignal): Promise<UpgradeStepsView>;
  status(signal?: AbortSignal): Promise<AutoupdateStatusResult>;
}
