/**
 * The browser keeps the anonymous bootstrap explicit.
 *
 * These cases pin the browser side of the bootstrap contract: it accepts a
 * session whose principal is explicit `null`s for an ownerless home, refuses a
 * synthesised or lost principal, and keeps the generation guard unchanged. The
 * host side (principal parsing, the daemon receipts, session generations,
 * revocation and expiry) is tested in tests/shell/gui/host.
 */

import { describe, expect, it, vi } from 'vitest';

import { connectBrowserBackend } from '../../src/app/backend';

const ORIGIN = 'http://127.0.0.1:4173';
const VIEW = 'view-proof';
const GRANT = 'launch-grant-value-that-is-long-enough';

class MemoryStorage {
  readonly values = new Map<string, string>();
  getItem(key: string): string | null { return this.values.get(key) ?? null; }
  setItem(key: string, value: string): void { this.values.set(key, value); }
  removeItem(key: string): void { this.values.delete(key); }
}

function bootstrapSessionResponse(overrides: Record<string, unknown> = {}): Response {
  return new Response(JSON.stringify({
    schemaVersion: 1,
    ok: true,
    requestId: 'session-request',
    driver: 'daemon',
    phase: 'bootstrap',
    view: VIEW,
    generation: 'generation-1',
    csrf: 'csrf-1',
    expiresAt: Date.now() + 60_000,
    principal: { kind: 'anonymous-bootstrap', owner: null, node: null, actor: null },
    catalog: {
      digest: 'sha256:catalog',
      declarationDigest: 'sha256:declaration',
      backendEpoch: 'epoch-1',
      httpContractVersion: 1,
      catalogVersion: '1',
      methodCount: 1,
      methods: [{ name: 'onboarding.status', callable: true, state: 'available', mutates: false }],
    },
    ...overrides,
  }), { status: 200, headers: { 'content-type': 'application/json' } });
}

function browserOptions(response: () => Response) {
  const storage = new MemoryStorage();
  return {
    storage,
    options: {
      fetch: vi.fn(async () => response()),
      storage,
      location: { hash: `#grant=${GRANT}`, pathname: '/', search: '', origin: ORIGIN },
      history: { replaceState: vi.fn() },
    },
  };
}

describe('the browser keeps the anonymous bootstrap explicit', () => {
  it('connects a bootstrap session with null principals and its phase', async () => {
    const { options } = browserOptions(() => bootstrapSessionResponse());

    const bootstrap = await connectBrowserBackend(options);

    expect(bootstrap.session.phase).toBe('bootstrap');
    expect(bootstrap.session.principal).toEqual({ owner: null, node: null, actor: null });
    expect(bootstrap.connection.descriptor.phase).toBe('bootstrap');
    expect(bootstrap.connection.descriptor.principalActor).toBeUndefined();
  });

  it('refuses a bootstrap session that synthesises a principal', async () => {
    const { options } = browserOptions(() => bootstrapSessionResponse({
      principal: { owner: '', node: '', actor: 'local-operator' },
    }));

    await expect(connectBrowserBackend(options)).rejects.toMatchObject({ code: 'BOOTSTRAP_INVALID_RESPONSE' });
  });

  it('refuses a ready session that lost its principal', async () => {
    const { options } = browserOptions(() => bootstrapSessionResponse({
      phase: 'ready',
      principal: { owner: null, node: null, actor: null },
    }));

    await expect(connectBrowserBackend(options)).rejects.toMatchObject({ code: 'BOOTSTRAP_INVALID_RESPONSE' });
  });

  it('keeps rejecting a stale generation after refresh', async () => {
    const { options } = browserOptions(() => bootstrapSessionResponse({
      phase: 'ready',
      principal: { owner: 'owner', node: 'node', actor: 'agent:owner:node:helper' },
    }));

    const session = await connectBrowserBackend(options);

    expect(session.session.phase).toBe('ready');
    expect(session.session.principal).toEqual({ owner: 'owner', node: 'node', actor: 'agent:owner:node:helper' });
    // The generation guard is unchanged: one session pins exactly one generation.
    expect(session.session.generation).toBe('generation-1');
  });

  it('rejects an expired session response outright', async () => {
    const { options } = browserOptions(() => bootstrapSessionResponse({ expiresAt: 'soon' }));

    await expect(connectBrowserBackend(options)).rejects.toMatchObject({ code: 'BOOTSTRAP_INVALID_RESPONSE' });
  });
});
