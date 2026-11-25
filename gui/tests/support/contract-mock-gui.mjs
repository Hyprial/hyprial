/**
 * Decision record (1e-3, 2026-11-15)
 * - Browser scripts that need daemon data get it from the one Contract Mock
 *   and nowhere else: startContractMock() starts the Node form of the mock
 *   (the in-process core behind gui/contract-mock/serve.ts) with a Selection
 *   (scenario, methodScenarios, cases), and openContractMockView() puts the
 *   PRODUCT entry behind a real session host in front of it. No test builds a
 *   transport, a page entry or an HTTP server of its own.
 * - Callers receive only origin, launchUrl() and close(). The upstream
 *   endpoint, its bearer and the launch grant stay inside this file. The only
 *   host paths a script may assert are /daemon/session and
 *   /daemon/request/<method>.
 * - One view is one host instance: the host has one one-time launch grant.
 *   A page opens the view at launchUrl() and the product bootstrap consumes the
 *   grant from the URL fragment.
 * - The host is the Python session host (2026-11-16, one launch path), started
 *   by support/python-host.ts over the built gui/dist. The Node host this used
 *   to start in production or development mode is gone, so there is no mode
 *   to choose and no process environment to set around the call.
 * - A canned browser reply is a second data source and is not offered here.
 *   A state is reached by naming a declared example (`cases`) or an error
 *   scenario (`methodScenarios`) when the mock is started; a state no declared
 *   case produces is a gap to report, not something to fabricate.
 */
import { resolve } from 'node:path';
import { startContractMockServer } from '../../contract-mock/serve.ts';
import { startPythonHost } from './python-host.ts';

const DIST = resolve(import.meta.dirname, '../../dist');
const upstreams = new WeakMap();

export async function startContractMock({ scenario = 'success', methodScenarios = {}, cases = {} } = {}) {
  const server = await startContractMockServer({ scenario, methodScenarios, cases, port: 0 });
  const mock = { scenario, methodScenarios, cases, close: () => server.close() };
  upstreams.set(mock, { endpoint: server.endpoint, token: server.token });
  return mock;
}

export async function openContractMockView(mock) {
  const upstream = upstreams.get(mock);
  if (upstream === undefined) throw new Error('openContractMockView needs a mock from startContractMock()');
  const host = await startPythonHost({
    driver: 'contract-mock',
    endpoint: upstream.endpoint,
    upstreamToken: upstream.token,
    principal: { owner: 'contract-mock', node: 'contract-mock-node', actor: 'user:contract-mock' },
    root: DIST,
  });
  return { origin: host.origin, launchUrl: host.launchUrl, close: host.close };
}

export async function withContractMockView(selection, body) {
  const mock = await startContractMock(selection);
  let view;
  try {
    view = await openContractMockView(mock);
    return await body(view);
  } finally {
    await view?.close();
    await mock.close();
  }
}
