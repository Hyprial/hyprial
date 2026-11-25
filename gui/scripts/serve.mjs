#!/usr/bin/env node

// Decisions (gui dev/product parity, docs/design/gui-dev-product-parity-2026-11-09.md):
// - `--mode dist-parity` serves the separate dist-parity test artifact (PROD
//   build + DCE with the composition-root parity input, no dev tools). It is
//   the production side of the browser parity gate and is never a release
//   artifact: the release artifact gate scans only `dist`.
// - Serving dist-parity requires HYPRIAL_GUI_PARITY=1 (the WP2 build/serving
//   convention) and a built dist-parity/index.html; both fail closed so the
//   gate can never serve a stale or missing parity artifact.
// - The regular production/development modes are unchanged and keep serving
//   `dist` / the vite dev server respectively.
// - 2026-11-16 (PR 1e-4): `startGuiServer` takes no extra Vite plugins; the
//   development server mounts only what gui/vite.config.ts declares. Browser
//   tests reach data through the Python session host or the Contract Mock.
// - 2026-11-16 (one launch path, PR 3): this script no longer hosts data. The
//   Python session host serves gui/dist and /daemon/*, and relays to the
//   development server started here for `hyprial gui dev`. The production mode
//   and `npm start` are gone, and a backend driver in the environment is
//   refused instead of being ignored: a Vite process that held a driver, an
//   endpoint or a bearer would be a second host. The ready line therefore
//   always reports driver null; the launcher refuses one that does not.
import { existsSync, realpathSync } from 'node:fs';
import { isAbsolute, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createServer, preview } from 'vite';

const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1']);
const guiDir = realpathSync(fileURLToPath(new URL('..', import.meta.url)));
const SERVE_MODES = new Set(['development', 'dist-parity']);

function usage() {
  return [
    'Usage: node scripts/serve.mjs --mode development|dist-parity [options]',
    '',
    'Options:',
    '  --host HOST          loopback host (default 127.0.0.1)',
    '  --port PORT          TCP port, 0 for ephemeral (default 4173)',
    '  --strict-port        fail instead of choosing another port (default)',
    '  --no-strict-port     allow Vite to choose another port',
    '  --mode MODE          development (the Vite dev server, default) or',
    '                       dist-parity (preview the dist-parity test artifact;',
    '                       requires HYPRIAL_GUI_PARITY=1 and a built artifact)',
    '  --help               show this help',
    '',
    'Environment: HYPRIAL_GUI_HOME (this GUI package or its worktree root),',
    '             HYPRIAL_GUI_PARITY=1 (dist-parity serving only).',
    '             This server holds no backend: it refuses HYPRIAL_GUI_BACKEND_DRIVER.',
    '             `hyprial gui` serves the built GUI and /daemon/* through the Python host.',
  ].join('\n');
}

export function parseArgs(argv) {
  let mode = 'development';
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

  if (!SERVE_MODES.has(mode)) {
    throw new Error('--mode must be development or dist-parity');
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

function endpointFor(host, port) {
  const hostPart = host.includes(':') ? `[${host}]` : host;
  return `http://${hostPart}:${port}`;
}

export async function startGuiServer(options) {
  const { mode, host, port, strictPort } = options;
  const guiHome = validateGuiHome(process.env.HYPRIAL_GUI_HOME);
  if (process.env.HYPRIAL_GUI_BACKEND_DRIVER?.trim()) {
    throw new Error(
      'HYPRIAL_GUI_BACKEND_DRIVER is not accepted: the Vite server hosts no backend; use `hyprial gui` (the Python session host serves /daemon/*)',
    );
  }
  if (process.env.HYPRIAL_GUI_AUTH || process.env.HYPRIAL_GUI_SETUP || process.env.HYPRIAL_GUI_SETUP_CLI) {
    throw new Error('account authentication and first-run setup are not implemented in the M1 GUI');
  }

  if (mode === 'dist-parity') {
    if (process.env.HYPRIAL_GUI_PARITY !== '1') {
      throw new Error(
        'dist-parity serving requires HYPRIAL_GUI_PARITY=1 (build with npm run build:dist-parity first)',
      );
    }
    const parityIndexFile = resolve(guiDir, 'dist-parity', 'index.html');
    if (!existsSync(parityIndexFile)) {
      throw Object.assign(
        new Error('dist-parity assets are missing; run npm run build:dist-parity first'),
        { code: 'BUILD_MISSING' },
      );
    }
  }

  const common = {
    root: guiDir,
    configFile: resolve(guiDir, 'vite.config.ts'),
    mode: mode === 'dist-parity' ? 'production' : mode,
    logLevel: 'warn',
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
    const previewConfig = {};
    if (mode === 'dist-parity') {
      previewConfig.outDir = resolve(guiDir, 'dist-parity');
    }
    server = await preview({
      ...common,
      build: Object.keys(previewConfig).length > 0 ? previewConfig : undefined,
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
  const closeServer = close;
  close = async () => {
    await closeServer();
  };

  return {
    guiHome,
    mode,
    host: address.address,
    port: address.port,
    url: endpointFor(address.address, address.port),
    driver: null,
    dataMode: process.env.HYPRIAL_GUI_DEV_PREVIEW === '1' && mode === 'development' ? 'fixture' : 'unavailable',
    backendConfigured: false,
    session: 'unavailable',
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
    driver: started.driver,
    backendConfigured: started.backendConfigured,
    session: started.session,
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
