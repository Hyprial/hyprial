import type { DaemonStatusResult } from './views/contracts';

export interface LocalManagementApi {
  status(signal?: AbortSignal): Promise<DaemonStatusResult>;
}
