import type { LogPageResult } from '../views/contracts';

export interface LogsApi { read(cursor?: string, signal?: AbortSignal): Promise<LogPageResult> }
