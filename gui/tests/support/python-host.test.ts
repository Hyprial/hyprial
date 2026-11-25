// @vitest-environment node

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  HOST_TEST_TIMEOUT_MS,
  hostArguments,
  hostEnvironment,
  startPythonHost,
  type PythonHost,
  type PythonHostOptions,
} from './python-host';

vi.setConfig({ testTimeout: HOST_TEST_TIMEOUT_MS });

const PRINCIPAL = { owner: 'owner', node: 'node', actor: 'user:owner' } as const;
const BASE: PythonHostOptions = {
  driver: 'daemon',
  endpoint: 'http://127.0.0.1:9',
  upstreamToken: 'upstream-secret-never-for-argv',
  principal: PRINCIPAL,
};
const started: PythonHost[] = [];

afterEach(async () => {
  for (const host of started.splice(0)) await host.close();
});

async function start(options: Partial<PythonHostOptions> = {}): Promise<PythonHost> {
  const host = await startPythonHost({ ...BASE, ...options });
  started.push(host);
  return host;
}

describe('the host environment and arguments', () => {
  it('carries the secrets only in the environment and drops every HYPRIAL_ variable of the caller', () => {
    const env = hostEnvironment(
      { ...BASE, handoffPath: '/tmp/launch.url', launchTtlMs: 5000 },
      'grant-value',
      { HYPRIAL_GUI_PRINCIPAL: 'user:leaked', HYPRIAL_HOME: '/home/leaked', PATH: '/bin' },
    );
    expect(env).toMatchObject({
      PATH: '/bin',
      HYPRIAL_GUI_BACKEND_DRIVER: 'daemon',
      HYPRIAL_GUI_UPSTREAM_TOKEN: 'upstream-secret-never-for-argv',
      HYPRIAL_GUI_LAUNCH_GRANT: 'grant-value',
      HYPRIAL_GUI_PRINCIPAL: 'user:owner',
      HYPRIAL_NODE_ID: 'node',
      HYPRIAL_GUI_HANDOFF_PATH: '/tmp/launch.url',
      HYPRIAL_GUI_LAUNCH_TTL_MS: '5000',
    });
    expect(env.HYPRIAL_HOME).toBeUndefined();
    expect(hostArguments(BASE, '/dist').join(' ')).not.toMatch(/secret|grant-value/);
  });

  it('leaves the principal out for an anonymous bootstrap and refuses a root together with a live upstream', () => {
    const anonymous = { kind: 'anonymous-bootstrap', owner: null, node: null, actor: null } as const;
    const env = hostEnvironment({ ...BASE, principal: anonymous }, 'g', {});
    expect(env.HYPRIAL_GUI_PRINCIPAL).toBeUndefined();
    expect(env.HYPRIAL_NODE_ID).toBeUndefined();
    expect(hostArguments({ ...BASE, liveUpstream: 'http://127.0.0.1:5173' }, undefined)).toEqual([
      '-m', 'hyprial.shell.impl.gui.host', 'serve', '--port', '0', '--live-upstream', 'http://127.0.0.1:5173',
    ]);
    expect(() => hostArguments({ ...BASE, root: '/dist', liveUpstream: 'http://127.0.0.1:5173' }, '/dist')).toThrow(/not both/);
    expect(hostArguments(BASE, '/dist')).not.toContain('--dev-tools');
    expect(hostArguments({ ...BASE, devTools: false }, '/dist')).not.toContain('--dev-tools');
    expect(hostArguments({ ...BASE, devTools: true }, '/dist').at(-1)).toBe('--dev-tools');
  });
});

describe('a started Python host', () => {
  it('serves the built root on its own port, says production, and builds a one-time launch URL', async () => {
    const host = await start();
    const health = await (await fetch(`${host.origin}/__gui/healthz`)).json() as Record<string, unknown>;
    expect(health).toMatchObject({ ok: true, mode: 'production', dataMode: 'hosted', driver: 'daemon' });
    expect(host.url).toBe(host.origin);
    expect(host.launchUrl('/x')).toBe(`${host.origin}/x#grant=${host.launchGrant}`);
    expect(host.launchGrant).toMatch(/^[0-9a-f]{64}$/);
    expect(host.backendConfigured).toBe(true);
    expect(host.listenAddress).toBe('127.0.0.1');
  });

  it('stops on close and frees its port', async () => {
    const host = await startPythonHost(BASE);
    await host.close();
    await expect(fetch(`${host.origin}/__gui/healthz`)).rejects.toThrow();
  });

  it('relays to a live upstream and says development', async () => {
    const dev = await start();
    const host = await start({ liveUpstream: dev.origin });
    const health = await (await fetch(`${host.origin}/__gui/healthz`)).json() as Record<string, unknown>;
    expect(health.mode).toBe('development');
    expect(await (await fetch(`${host.origin}/`)).text()).toBe('<!doctype html><title>Hyprial</title>');
    expect((await fetch(`${host.origin}/missing.js`)).status).toBe(404);
  });

  it('rejects with the child output when the host refuses its configuration', async () => {
    await expect(startPythonHost({ ...BASE, endpoint: 'not-a-url' })).rejects.toThrow(/exited with 2 before it was ready.*INVALID_CONFIG/s);
  });
});
