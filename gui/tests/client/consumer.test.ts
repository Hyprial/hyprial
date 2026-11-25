import { describe, expect, it } from 'vitest';

import { DomainError, TransportError } from '@/client/errors';
import { HttpTransport } from '@/client/transport';

import {
  createAgentProviderFeature,
  createLiveAgentProviderPort,
} from './agent-provider-feature';
import { createGeneratedClient } from './fixtures/generated';

interface Call {
  readonly method: string;
  readonly body: unknown;
  readonly authorization: string | null;
}

function envelope(method: string, result: unknown): Record<string, unknown> {
  return {
    schemaVersion: 1,
    ok: true,
    method,
    requestId: `req-${method}`,
    dispatch: 'dispatched',
    result,
  };
}

function successBody(method: string): unknown {
  switch (method) {
    case 'agent.list':
      return envelope(method, {
        agents: [
          { actor: 'agent:alpha', uri: 'agent:alpha', name: 'Alpha', provider: 'claude' },
          { actor: 'agent:beta', uri: 'agent:beta', name: null },
        ],
      });
    case 'agent.provider.default':
      return envelope(method, { actor: 'agent:alpha', provider: 'claude' });
    case 'agent.secret.list':
      return envelope(method, {
        grants: [{ grantId: 'grant-1', source: 'claude', fieldName: 'token', environmentNames: ['dev'] }],
      });
    case 'agent.provider.logout':
      return envelope(method, { actor: 'agent:alpha', provider: 'claude', revoked: true });
    default:
      throw new Error(`unexpected method ${method}`);
  }
}

function makeConsumer(onRequest?: (call: Call) => Response | Promise<Response> | undefined) {
  const calls: Call[] = [];
  const fetchImpl = (async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = url.slice(url.lastIndexOf('/') + 1);
    const headers = new Headers(init?.headers);
    const call = {
      method,
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
      authorization: headers.get('authorization'),
    };
    calls.push(call);
    return onRequest?.(call) ?? new Response(JSON.stringify(successBody(method)), {
      status: 200,
      headers: { 'content-type': 'application/json', 'x-hyprial-catalog-digest': 'sha256:catalog' },
    });
  }) as typeof fetch;
  const transport = new HttpTransport({
    baseUrl: 'http://127.0.0.1:47832',
    token: 'isolated-test-token',
    fetch: fetchImpl,
    requestBudgetMs: 50,
    expectedCatalogDigest: 'sha256:catalog',
  });
  return { client: createGeneratedClient(transport), transport, calls };
}

describe('generated client consumer (no React, no CLI text)', () => {
  it('calls nested namespaces through the canonical HTTP method path and returns business results', async () => {
    const { client, calls } = makeConsumer();

    const result = await client.agent.provider.logout({ actor: 'agent:alpha', provider: 'claude' });

    expect(result.revoked).toBe(true);
    expect(calls).toEqual([
      {
        method: 'agent.provider.logout',
        body: { actor: 'agent:alpha', provider: 'claude' },
        authorization: 'Bearer isolated-test-token',
      },
    ]);
  });

  it('loads the feature view through the generated client with the feature adapter injected', async () => {
    const { client, transport } = makeConsumer();
    const feature = createAgentProviderFeature({
      mode: 'live',
      client,
      mock: undefined,
    });

    const view = await feature.load('agent:alpha');

    expect(view).toEqual({
      agents: [
        { actor: 'agent:alpha', label: 'Alpha' },
        { actor: 'agent:beta', label: 'agent:beta' },
      ],
      defaultProvider: 'claude',
      grants: [{ id: 'grant-1', source: 'claude', field: 'token', environments: ['dev'] }],
    });
    expect(transport.lastDiagnostics?.attempts).toBe(1);
  });

  it('switches to the mock port without changing the feature contract', async () => {
    const feature = createAgentProviderFeature({
      mode: 'mock',
      mock: {
        agents: [{ actor: 'agent:mock', label: 'Mock' }],
        defaultProvider: 'codex',
        grants: [{ id: 'g', source: 'codex', field: 'apiKey', environments: ['test'] }],
      },
    });

    const view = await feature.load('agent:mock');
    await feature.logout('agent:mock', 'codex');

    expect(view.agents).toEqual([{ actor: 'agent:mock', label: 'Mock' }]);
    expect(view.defaultProvider).toBe('codex');
  });

  it('keeps dispatch=unknown on write timeout, makes exactly one attempt and never claims rollback', async () => {
    const { transport, calls } = makeConsumer(
      (call) =>
        call.method === 'agent.provider.logout'
          ? new Promise<Response>((_resolve, reject) => {
              setTimeout(() => {
                const error = new Error('timeout');
                error.name = 'AbortError';
                reject(error);
              }, 30);
            })
          : undefined,
    );
    const port = createLiveAgentProviderPort(createGeneratedClient(transport));

    const error = (await port
      .logout('agent:alpha', 'claude')
      .catch((value: unknown) => value)) as TransportError;

    expect(error).toBeInstanceOf(TransportError);
    expect(error.diagnostics.dispatch).toBe('unknown');
    expect(calls.filter((call) => call.method === 'agent.provider.logout')).toHaveLength(1);
    expect(Object.keys(error)).not.toContain('rolledBack');
  });

  it('surfaces a post-dispatch domain error without retrying or hiding the dispatch fact', async () => {
    const { transport, calls } = makeConsumer((call) =>
      call.method === 'agent.provider.logout'
        ? new Response(
            JSON.stringify({
              schemaVersion: 1,
              ok: false,
              requestId: 'req-write-1',
              code: 'TARGET_NOT_AUTHORIZED',
              message: 'the principal may not modify this agent',
              dispatch: 'dispatched',
              method: 'agent.provider.logout',
              data: { actor: 'agent:alpha' },
            }),
            { status: 403, headers: { 'content-type': 'application/json' } },
          )
        : undefined,
    );

    const error = (await transport
      .invoke('agent.provider.logout', { actor: 'agent:alpha', provider: 'claude' })
      .catch((value: unknown) => value)) as DomainError;

    expect(error).toBeInstanceOf(DomainError);
    expect(error.diagnostics.dispatch).toBe('dispatched');
    expect(error.code).toBe('TARGET_NOT_AUTHORIZED');
    expect(calls).toHaveLength(1);
  });
});
