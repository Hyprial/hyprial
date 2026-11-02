/** Shared same-origin transport for the explicitly enabled local GUI host.
 * Domain clients own their operation names, response decoding and errors.
 * Session state is memory-only and scoped to this transport instance.
 */
export interface WebHostTransport {
  request(action: string, body?: Readonly<Record<string, string>>): Promise<Response>;
}

export class HostSessionError extends Error {
  constructor(readonly code?: string) { super('GUI host session unavailable'); }
}

const PREFIX = '/__gui/auth/';

export function createWebHostTransport(fetcher: typeof fetch = fetch): WebHostTransport {
  let nonce: string | null = null;
  let bootstrap: Promise<string> | null = null;

  async function session(): Promise<string> {
    if (nonce) return nonce;
    bootstrap ??= (async () => {
      const response = await fetcher(`${PREFIX}session`, {
        credentials: 'same-origin', cache: 'no-store',
      });
      const value = await response.json();
      if (!response.ok || typeof value?.nonce !== 'string' || !/^[a-f0-9]{64}$/.test(value.nonce)) {
        throw new HostSessionError(typeof value?.code === 'string' ? value.code : undefined);
      }
      nonce = value.nonce;
      return value.nonce as string;
    })().finally(() => { bootstrap = null; });
    return bootstrap;
  }

  async function request(
    action: string,
    body: Readonly<Record<string, string>> = {},
    refreshSession = true,
  ): Promise<Response> {
    // Callers select fixed local operations, never arbitrary URLs or paths.
    if (!/^[a-z][a-z0-9-]*$/.test(action)) throw new Error('Invalid GUI host operation');
    const sentNonce = await session();
    const response = await fetcher(`${PREFIX}${action}`, {
      method: 'POST', credentials: 'same-origin', cache: 'no-store',
      headers: { 'Content-Type': 'application/json', 'X-Hyprial-Auth-Session': sentNonce },
      body: JSON.stringify(body),
    });
    // The host guarantees this rejection happens before execution. Do not
    // replay mutations for network failures, ownership failures or other codes.
    if (refreshSession && response.status === 403) {
      const rejected = await response.clone().json().catch(() => null);
      if (rejected?.code === 'AUTH_REQUEST_REJECTED') {
        if (nonce === sentNonce) nonce = null;
        return request(action, body, false);
      }
    }
    return response;
  }

  return { request: (action, body) => request(action, body) };
}
