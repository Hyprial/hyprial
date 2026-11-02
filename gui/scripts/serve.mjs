#!/usr/bin/env node

import { existsSync, realpathSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer, loadEnv, preview } from 'vite';
import { createAuthMiddleware } from '../server/auth/middleware.mjs';
import { createSetupService } from '../server/setup/service.mjs';

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);
const guiDir = realpathSync(fileURLToPath(new URL('..', import.meta.url)));

function usage() {
  return [
    'Usage: node scripts/serve.mjs --mode production|development [options]',
    '',
    'Options:',
    '  --host HOST          loopback host (default 127.0.0.1)',
    '  --port PORT          TCP port, 0 for ephemeral (default 4173)',
    '  --strict-port        fail instead of choosing another port (default)',
    '  --no-strict-port     allow Vite to choose another port',
    '  --mode MODE          production (preview dist) or development',
    '  --help               show this help',
    '',
    'Environment: HYPRIAL_GUI_HOME (this GUI package or its worktree root),',
    '             HYPRIAL_BACKEND_ENDPOINT (http(s) loopback origin with port).',
    '             HYPRIAL_GUI_AUTH=local enables local account authorization.',
    '             HYPRIAL_GUI_AUTH_PYTHON and HYPRIAL_HOME must then be absolute.',
    '             HYPRIAL_GUI_SETUP=daemon enables Pi first-run through the public daemon client.',
  ].join('\n');
}

export function parseArgs(argv) {
  let mode = 'production';
  let host = '127.0.0.1';
  let port = 4173;
  let strictPort = true;

  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === '--help' || argument === '-h') {
      return { help: true, mode, host, port, strictPort };
    }
    if (argument === '--mode') {
      mode = argv[index + 1] ?? '';
      index += 1;
      continue;
    }
    if (argument.startsWith('--mode=')) {
      mode = argument.slice('--mode='.length);
      continue;
    }
    if (argument === '--host') {
      host = argv[index + 1] ?? '';
      index += 1;
      continue;
    }
    if (argument.startsWith('--host=')) {
      host = argument.slice('--host='.length);
      continue;
    }
    if (argument === '--port') {
      port = Number(argv[index + 1]);
      index += 1;
      continue;
    }
    if (argument.startsWith('--port=')) {
      port = Number(argument.slice('--port='.length));
      continue;
    }
    if (argument === '--strict-port') {
      strictPort = true;
      continue;
    }
    if (argument === '--no-strict-port') {
      strictPort = false;
      continue;
    }
    throw new Error(`unknown argument: ${argument}`);
  }

  if (mode !== 'production' && mode !== 'development') {
    throw new Error('--mode must be production or development');
  }
  if (!LOOPBACK_HOSTS.has(host)) {
    throw new Error(
      '--host must be a loopback host (127.0.0.1, localhost or ::1)',
    );
  }
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new Error('--port must be an integer between 0 and 65535');
  }

  return { help: false, mode, host, port, strictPort };
}

function validateGuiHome(configured) {
  if (!configured) {
    return guiDir;
  }
  if (!isAbsolute(configured)) {
    throw new Error('HYPRIAL_GUI_HOME must be an absolute path');
  }

  let candidate;
  try {
    candidate = realpathSync(configured);
  } catch {
    throw new Error('HYPRIAL_GUI_HOME does not exist');
  }

  if (candidate === guiDir) {
    return guiDir;
  }

  const worktreeGui = resolve(candidate, 'gui');
  if (existsSync(worktreeGui) && realpathSync(worktreeGui) === guiDir) {
    return guiDir;
  }

  throw new Error(
    'HYPRIAL_GUI_HOME must point to this GUI package or its worktree root',
  );
}

function parseBackendEndpoint(raw) {
  const value = raw?.trim();
  if (!value) {
    return null;
  }

  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(
      'HYPRIAL_BACKEND_ENDPOINT must be an absolute http(s) loopback URL',
    );
  }

  if (
    (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') ||
    !LOOPBACK_HOSTS.has(parsed.hostname) ||
    parsed.username !== '' ||
    parsed.password !== '' ||
    parsed.pathname !== '/' ||
    parsed.search !== '' ||
    parsed.hash !== '' ||
    parsed.port === ''
  ) {
    throw new Error(
      'HYPRIAL_BACKEND_ENDPOINT must be http(s)://<loopback-host>:<port> with no path, query, fragment or credentials',
    );
  }

  return parsed.origin;
}

function endpointFor(host, port) {
  const hostPart = host.includes(':') ? `[${host}]` : host;
  return `http://${hostPart}:${port}`;
}

export async function startGuiServer(options) {
  const { mode, host, port, strictPort } = options;
  const guiHome = validateGuiHome(process.env.HYPRIAL_GUI_HOME);
  const fileEnv = loadEnv(mode, guiDir, '');
  const backendEndpoint = parseBackendEndpoint(
    process.env.HYPRIAL_BACKEND_ENDPOINT ?? fileEnv.HYPRIAL_BACKEND_ENDPOINT,
  );

  if (mode === 'production') {
    const indexFile = resolve(guiDir, 'dist', 'index.html');
    if (!existsSync(indexFile)) {
      throw Object.assign(
        new Error('production assets are missing; run npm run build first'),
        { code: 'BUILD_MISSING' },
      );
    }
  }

  // Authentication is independent of the placeholder business backend. Only
  // trusted launcher environment selects the local runtime and instance home.
  const authEnabled = process.env.HYPRIAL_GUI_AUTH === 'local';
  if (process.env.HYPRIAL_GUI_AUTH && !authEnabled) {
    throw new Error('HYPRIAL_GUI_AUTH must be local or unset');
  }
  if (authEnabled && (
    !isAbsolute(process.env.HYPRIAL_GUI_AUTH_PYTHON ?? '') ||
    !isAbsolute(process.env.HYPRIAL_HOME ?? '')
  )) {
    throw new Error('Local authentication requires absolute HYPRIAL_GUI_AUTH_PYTHON and HYPRIAL_HOME');
  }
  let authOrigin = '';
  const setupMode = process.env.HYPRIAL_GUI_SETUP;
  if (process.env.HYPRIAL_GUI_SETUP_CLI) {
    throw new Error('HYPRIAL_GUI_SETUP_CLI is unsupported; use HYPRIAL_GUI_SETUP=daemon');
  }
  if (setupMode && (setupMode !== 'daemon' || !authEnabled)) {
    throw new Error('Pi first-run requires HYPRIAL_GUI_SETUP=daemon and local authentication');
  }
  const setupRunner = options.setupRunner ?? (setupMode ? createSetupService({
    pythonExecutable: process.env.HYPRIAL_GUI_AUTH_PYTHON,
    hyprialHome: process.env.HYPRIAL_HOME,
  }) : null);
  const auth = createAuthMiddleware({
    enabled: authEnabled,
    origin: () => authOrigin,
    pythonExecutable: process.env.HYPRIAL_GUI_AUTH_PYTHON,
    hyprialHome: process.env.HYPRIAL_HOME,
    runner: options.authRunner,
    setupRunner,
  });
  const installAuth = (viteServer) => {
    viteServer.middlewares.use(auth.middleware);
  };
  const common = {
    root: guiDir,
    configFile: resolve(guiDir, 'vite.config.ts'),
    mode,
    logLevel: 'warn',
    plugins: [{
      name: 'hyprial-local-auth',
      configureServer: installAuth,
      configurePreviewServer: installAuth,
    }],
  };

  let server;
  let close;
  if (mode === 'development') {
    server = await createServer({
      ...common,
      server: { host, port, strictPort },
    });
    await server.listen();
    close = () => server.close();
  } else {
    server = await preview({
      ...common,
      preview: { host, port, strictPort },
    });
    close = async () => {
      const httpServer = server.httpServer;
      if (typeof httpServer.closeAllConnections === 'function') {
        httpServer.closeAllConnections();
      }
      await new Promise((resolveClose, rejectClose) => {
        httpServer.close((error) => {
          if (error) {
            rejectClose(error);
            return;
          }
          resolveClose();
        });
      });
    };
  }

  const address = server.httpServer.address();
  if (typeof address !== 'object' || address === null) {
    await close();
    throw new Error('GUI server did not bind a TCP port');
  }
  authOrigin = endpointFor(address.address, address.port);
  const closeServer = close;
  close = async () => {
    await auth.close();
    await closeServer();
  };

  return {
    guiHome,
    mode,
    host: address.address,
    port: address.port,
    url: endpointFor(address.address, address.port),
    backendEndpoint,
    dataMode: process.env.HYPRIAL_GUI_BACKEND_DRIVER === 'daemon' ? 'live' : 'fixture',
    backendConfigured: process.env.HYPRIAL_GUI_BACKEND_DRIVER === 'daemon' || backendEndpoint !== null,
    authConfigured: authEnabled,
    setupConfigured: setupRunner !== null,
    close,
  };
}

function readyLine(started) {
  return `HYPRIAL_GUI_READY ${JSON.stringify({
    service: 'hyprial-gui',
    mode: started.mode,
    dataMode: started.dataMode,
    host: started.host,
    port: started.port,
    url: started.url,
    backendConfigured: started.backendConfigured,
    authConfigured: started.authConfigured,
    setupConfigured: started.setupConfigured,
    backendEndpoint: started.backendEndpoint,
    guiHome: started.guiHome,
  })}`;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    console.log(usage());
    return;
  }

  const started = await startGuiServer(args);
  console.log(readyLine(started));

  // Vite's dev and preview servers install their own SIGTERM listener that
  // exits without reporting the launcher lifecycle. This process owns the
  // server, so replace that listener with the contract below.
  process.removeAllListeners('SIGTERM');

  let closing = false;
  const shutdown = async (signal) => {
    if (closing) {
      return;
    }
    closing = true;
    try {
      await started.close();
    } catch {
      // The process is stopping; the stopped line records the signal only.
    } finally {
      console.log(`HYPRIAL_GUI_STOPPED ${JSON.stringify({ signal })}`);
    }
  };

  process.on('SIGTERM', () => {
    void shutdown('SIGTERM');
  });
  process.on('SIGINT', () => {
    void shutdown('SIGINT');
  });
}

const invokedDirectly =
  process.argv[1] !== undefined &&
  resolve(process.argv[1]) === fileURLToPath(import.meta.url);

if (invokedDirectly) {
  main().catch((error) => {
    console.error(
      `HYPRIAL_GUI_ERROR ${JSON.stringify({
        code: error && error.code ? error.code : 'GUI_ERROR',
        message: error instanceof Error ? error.message : 'unknown error',
      })}`,
    );
    process.exitCode = 1;
  });
}
