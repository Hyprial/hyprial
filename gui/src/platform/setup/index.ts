import { createWebHostTransport, HostSessionError, type WebHostTransport } from '../transport';

/** First-run host capability; no model key or grant value is returned. */
export interface SetupProgress { readonly stage: 'created' | 'granted' | 'started' }
export interface SetupResult {
  readonly ok: boolean;
  readonly code?: string;
  readonly saved?: boolean;
  readonly created?: boolean;
  /** Completed grant step/history; not a fresh effective-permission probe. */
  readonly granted?: boolean | null;
  readonly started?: boolean;
  readonly keySaved?: boolean | null;
  readonly compatible?: boolean;
  readonly resumeAvailable?: boolean;
  readonly pendingAction?: 'save' | 'create' | 'configure' | 'grant' | 'start' | null;
  readonly grantRecorded?: boolean | null;
  readonly grantVerified?: false;
  readonly modelVerified: false;
}
export interface SetupClient {
  status(): Promise<SetupResult>;
  saveModelKey(key: string): Promise<SetupResult>;
  prepareSquire(onProgress?: (event: SetupProgress) => void): Promise<SetupResult>;
  cancel(): Promise<void>;
}
class SetupError extends Error {
  constructor(readonly code: string) { super(code); }
}
export function createWebSetupClient(
  fetcher: typeof fetch = fetch,
  transport: WebHostTransport = createWebHostTransport(fetcher),
): SetupClient {
  async function request(action: string, body: Record<string, string> = {}): Promise<Response> {
    try { return await transport.request(action, body); }
    catch (error) {
      if (error instanceof HostSessionError) throw new SetupError('SETUP_UNAVAILABLE');
      throw error;
    }
  }
  async function result(response: Response): Promise<SetupResult> {
    const value = await response.json();
    if (!response.ok) throw new SetupError(value.code ?? 'SETUP_FAILED');
    return value as SetupResult;
  }
  return {
    async status() { return result(await request('setup-status')); },
    async saveModelKey(key) {
      const input = { key };
      try { return await result(await request('model-save', input)); }
      finally { input.key = ''; }
    },
    async prepareSquire(onProgress) {
      const response = await request('squire-prepare');
      if (!response.ok) return result(response);
      if (!response.body) throw new SetupError('SETUP_PROTOCOL_ERROR');
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      let size = 0;
      let final: SetupResult | undefined;
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          size += value.length;
          if (size > 65_536) throw new SetupError('SETUP_PROTOCOL_ERROR');
          buffer += decoder.decode(value, { stream: true });
          let newline;
          while ((newline = buffer.indexOf('\n')) !== -1) {
            const line = buffer.slice(0, newline); buffer = buffer.slice(newline + 1);
            if (!line) continue;
            const frame = JSON.parse(line);
            if (frame.type === 'progress' && !final && ['created', 'granted', 'started'].includes(frame.data?.stage)) onProgress?.({ stage: frame.data.stage });
            else if (frame.type === 'result' && !final) final = frame.data as SetupResult;
            else throw new SetupError('SETUP_PROTOCOL_ERROR');
          }
        }
        if (!final || buffer.trim()) throw new SetupError('SETUP_PROTOCOL_ERROR');
        return final;
      } catch (error) { await reader.cancel().catch(() => {}); throw error; }
      finally { reader.releaseLock(); }
    },
    async cancel() { await result(await request('cancel')); },
  };
}
