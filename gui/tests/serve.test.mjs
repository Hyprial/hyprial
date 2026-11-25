// @vitest-environment node

import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { parseArgs, startGuiServer } from '../scripts/serve.mjs';
const previousHome = process.env.HYPRIAL_GUI_HOME;
const previousDriver = process.env.HYPRIAL_GUI_BACKEND_DRIVER;
const previousBackend = process.env.HYPRIAL_BACKEND_ENDPOINT;
const previousPrincipal = process.env.HYPRIAL_GUI_PRINCIPAL;
const previousToken = process.env.HYPRIAL_GUI_UPSTREAM_TOKEN;
const previousGrant = process.env.HYPRIAL_GUI_LAUNCH_GRANT;
const previousPreview = process.env.HYPRIAL_GUI_DEV_PREVIEW;
const previousParity = process.env.HYPRIAL_GUI_PARITY;
const worktreeRoot = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
);
let gui;

beforeEach(() => {
  delete process.env.HYPRIAL_GUI_HOME;
  delete process.env.HYPRIAL_GUI_BACKEND_DRIVER;
  delete process.env.HYPRIAL_BACKEND_ENDPOINT;
  delete process.env.HYPRIAL_GUI_PRINCIPAL;
  delete process.env.HYPRIAL_GUI_UPSTREAM_TOKEN;
  delete process.env.HYPRIAL_GUI_LAUNCH_GRANT;
  delete process.env.HYPRIAL_GUI_DEV_PREVIEW;
  delete process.env.HYPRIAL_GUI_PARITY;
});

afterEach(async () => {
  if (gui) {
    await gui.close();
    gui = undefined;
  }
  for (const [name, value] of Object.entries({
    HYPRIAL_GUI_HOME: previousHome,
    HYPRIAL_GUI_BACKEND_DRIVER: previousDriver,
    HYPRIAL_BACKEND_ENDPOINT: previousBackend,
    HYPRIAL_GUI_PRINCIPAL: previousPrincipal,
    HYPRIAL_GUI_UPSTREAM_TOKEN: previousToken,
    HYPRIAL_GUI_LAUNCH_GRANT: previousGrant,
    HYPRIAL_GUI_DEV_PREVIEW: previousPreview,
    HYPRIAL_GUI_PARITY: previousParity,
  })) {
    if (value === undefined) delete process.env[name];
    else process.env[name] = value;
  }
});

describe('launcher arguments', () => {
  it('accepts the documented npm dev interface', () => {
    expect(
      parseArgs([
        '--mode',
        'development',
        '--host',
        '127.0.0.1',
        '--port',
        '0',
        '--strict-port',
      ]),
    ).toEqual({
      help: false,
      mode: 'development',
      host: '127.0.0.1',
      port: 0,
      strictPort: true,
    });
  });

  it('accepts the dist-parity mode for the browser parity gate', () => {
    expect(parseArgs(['--mode', 'dist-parity', '--port', '0']).mode).toBe('dist-parity');
    expect(parseArgs(['--mode=dist-parity']).mode).toBe('dist-parity');
  });

  it('refuses non-loopback hosts, non-numeric ports and unknown modes', () => {
    expect(() => parseArgs(['--host', '0.0.0.0'])).toThrow(/loopback/);
    expect(() => parseArgs(['--port', 'not-a-port'])).toThrow(/port/);
    expect(() => parseArgs(['--mode', 'staging'])).toThrow(/--mode must be/);
  });
});

describe('dist-parity serving', () => {
  const parityDir = resolve(dirname(fileURLToPath(import.meta.url)), '..', 'dist-parity');

  it('requires HYPRIAL_GUI_PARITY=1 before anything else', async () => {
    rmSync(parityDir, { recursive: true, force: true });
    await expect(startGuiServer({
      mode: 'dist-parity',
      host: '127.0.0.1',
      port: 0,
      strictPort: true,
    })).rejects.toThrow(/HYPRIAL_GUI_PARITY=1/);
  });

  it('fails closed when the dist-parity artifact is missing', async () => {
    process.env.HYPRIAL_GUI_PARITY = '1';
    rmSync(parityDir, { recursive: true, force: true });
    await expect(startGuiServer({
      mode: 'dist-parity',
      host: '127.0.0.1',
      port: 0,
      strictPort: true,
    })).rejects.toThrow(/dist-parity assets are missing/);
  });

  it('serves the dist-parity artifact over loopback', async () => {
    process.env.HYPRIAL_GUI_PARITY = '1';
    mkdirSync(parityDir, { recursive: true });
    writeFileSync(resolve(parityDir, 'index.html'), '<!doctype html><title>dist-parity</title>', 'utf8');
    try {
      gui = await startGuiServer({
        mode: 'dist-parity',
        host: '127.0.0.1',
        port: 0,
        strictPort: true,
      });
      expect(gui.mode).toBe('dist-parity');
      expect(gui.port).toBeGreaterThan(0);
      const response = await fetch(`${gui.url}/`);
      expect(response.status).toBe(200);
      expect(await response.text()).toContain('dist-parity');
    } finally {
      rmSync(parityDir, { recursive: true, force: true });
    }
  });
});

describe('GUI development server', () => {
  it('refuses a backend driver in the environment: the Vite server no longer hosts data', async () => {
    process.env.HYPRIAL_GUI_HOME = worktreeRoot;
    for (const driver of ['daemon', 'contract-mock']) {
      process.env.HYPRIAL_GUI_BACKEND_DRIVER = driver;
      await expect(startGuiServer({
        mode: 'development',
        host: '127.0.0.1',
        port: 0,
        strictPort: true,
      })).rejects.toThrow(/HYPRIAL_GUI_BACKEND_DRIVER is not accepted/);
    }
  });

  it('reports a dataless server: no driver, no backend, no session', async () => {
    process.env.HYPRIAL_GUI_HOME = worktreeRoot;

    gui = await startGuiServer({
      mode: 'development',
      host: '127.0.0.1',
      port: 0,
      strictPort: true,
    });

    expect(gui.port).toBeGreaterThan(0);
    expect(gui.url).toBe(`http://127.0.0.1:${gui.port}`);
    expect(gui.backendConfigured).toBe(false);
    expect(gui.driver).toBeNull();
    expect(gui.dataMode).toBe('unavailable');
    expect(gui.session).toBe('unavailable');
    expect(gui.guiHome).toBe(
      realpathSync(dirname(dirname(fileURLToPath(import.meta.url)))),
    );

    const health = await fetch(`${gui.url}/__gui/healthz`);
    expect(health.status).toBe(200);
    expect(await health.json()).toEqual({
      ok: true,
      service: 'hyprial-gui',
      mode: 'development',
      dataMode: 'unavailable',
      driver: null,
      backendConfigured: false,
      session: 'unavailable',
    });
  });

  it('says fixture only for the explicit dev preview', async () => {
    process.env.HYPRIAL_GUI_HOME = worktreeRoot;
    process.env.HYPRIAL_GUI_DEV_PREVIEW = '1';

    gui = await startGuiServer({
      mode: 'development',
      host: '127.0.0.1',
      port: 0,
      strictPort: true,
    });

    expect(gui.dataMode).toBe('fixture');
    expect((await (await fetch(`${gui.url}/__gui/healthz`)).json()).dataMode).toBe('fixture');
  });

  it('has no production mode: the Python session host serves gui/dist', () => {
    expect(() => parseArgs(['--mode', 'production'])).toThrow(/--mode must be development or dist-parity/);
  });
});
