/**
 * Front-end development without a backend: `connectContractMock`.
 *
 * Decision record (1e-2, 2026-11-15)
 * - The generated client, the transport and the bootstrap are the product's own;
 *   only `fetch` is the in-browser Contract Mock. Reads and refusals therefore
 *   arrive as the typed results and errors a page sees from the real host.
 * - This runs in jsdom on purpose: it proves `Response`, `Headers` and
 *   `crypto` are available where component tests and the dev preview run.
 */

import { describe, expect, it } from 'vitest';
import { DomainError } from '../../src/client';
import { connectContractMock, contractExamples } from '../../src/dev/contract-mock';

describe('connectContractMock', () => {
  it('opens a contract-mock session and serves the declared example', async () => {
    const bootstrap = await connectContractMock({ scenario: 'success' });
    expect(bootstrap.session.driver).toBe('contract-mock');
    expect(bootstrap.session.phase).toBe('ready');
    const declared = contractExamples('agent.list').find((example) => example.kind === 'success');
    await expect(bootstrap.connection.client.agent.list({})).resolves.toEqual(declared?.result);
  });

  it('serves the empty scenario', async () => {
    const bootstrap = await connectContractMock({ scenario: 'empty' });
    const declared = contractExamples('agent.list').find((example) => example.kind === 'empty');
    await expect(bootstrap.connection.client.agent.list({})).resolves.toEqual(declared?.result);
  });

  it('pins a declared error case for one method', async () => {
    const declared = contractExamples('agent.list').find((example) => example.kind === 'domain-error');
    const bootstrap = await connectContractMock({ scenario: 'success', cases: { 'agent.list': declared!.name } });
    const refusal = await bootstrap.connection.client.agent.list({}).catch((error: unknown) => error);
    expect(refusal).toBeInstanceOf(DomainError);
    expect(refusal).toMatchObject({ code: declared!.error!.code, dispatch: 'dispatched' });
  });

  it('refuses a method without a case instead of inventing a payload', async () => {
    const bootstrap = await connectContractMock({ scenario: 'unconfigured' });
    const refusal = await bootstrap.connection.client.agent.list({}).catch((error: unknown) => error);
    expect(refusal).toMatchObject({ code: 'MOCK_SCENARIO_MISSING', dispatch: 'not-dispatched' });
  });

  it('rejects an unknown preset before any request', async () => {
    await expect(connectContractMock({ preset: 'no-such-preset' })).rejects.toThrow(/unknown preset/);
  });
});

describe('contractExamples', () => {
  it('lists the declared cases of a method and nothing for an unknown one', () => {
    expect(contractExamples('agent.list').map((example) => example.name)).toContain('one-agent');
    expect(contractExamples('no.such.method')).toEqual([]);
  });
});
