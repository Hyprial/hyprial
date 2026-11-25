import type { HttpTransport } from '../../../../client';
import { createGeneratedClient, type GeneratedClient } from '../../../../client/generated';

export interface DiagnosticSnapshot {
  readonly observedAtMs: number | null;
  readonly actorCount: number;
  readonly strandedCount: number;
}

export interface DiagnosticApi {
  read(signal?: AbortSignal): Promise<DiagnosticSnapshot>;
}

function isObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function createDiagnosticApi(transport: Pick<HttpTransport, 'invoke'>): DiagnosticApi {
  return createDiagnosticApiFromClient(createGeneratedClient(transport));
}

export function createDiagnosticApiFromClient(client: Pick<GeneratedClient, 'top'>): DiagnosticApi {
  return {
    async read(signal) {
      const result: unknown = await client.top.snapshot({}, { signal });
      if (!isObject(result) || !isObject(result.daemon) || !isObject(result.quota)
        || !Array.isArray(result.actors) || !result.actors.every(isObject)
        || !Array.isArray(result.stranded) || !result.stranded.every(isObject)
        || (result.nowMs !== undefined && (typeof result.nowMs !== 'number'
          || !Number.isSafeInteger(result.nowMs) || result.nowMs < 0 || result.nowMs > 8.64e15))) {
        throw new Error('DIAGNOSTIC_RESULT_INVALID');
      }
      return {
        observedAtMs: typeof result.nowMs === 'number' ? result.nowMs : null,
        actorCount: result.actors.length,
        strandedCount: result.stranded.length,
      };
    },
  };
}
