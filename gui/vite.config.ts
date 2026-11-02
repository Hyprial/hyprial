import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { defineConfig, loadEnv, type Plugin } from 'vite';
import tailwindcss from '@tailwindcss/vite';
import { liveBackendPlugin } from './server/live/index.mjs';
import { referenceHttpPlugin } from './server/reference/index';

const guiDir = fileURLToPath(new URL('.', import.meta.url));
const LOOPBACK_HOSTS = new Set(['127.0.0.1', 'localhost', '::1', '[::1]']);

function parseBackendEndpoint(
  raw: string | undefined,
): string | null {
  const value = raw?.trim();
  if (!value) {
    return null;
  }

  let parsed: URL;
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

function guiHealthPlugin(
  mode: string,
  backendEndpoint: string | null,
  liveEnabled: boolean,
): Plugin {
  const handler = (
    request: { method?: string; url?: string },
    response: {
      statusCode: number;
      setHeader: (name: string, value: string) => void;
      end: (body: string) => void;
    },
    next: () => void,
  ): void => {
    if (request.method !== 'GET' || request.url !== '/__gui/healthz') {
      next();
      return;
    }
    response.statusCode = 200;
    response.setHeader('Content-Type', 'application/json; charset=utf-8');
    response.setHeader('Cache-Control', 'no-store');
    response.end(
      JSON.stringify({
        ok: true,
        service: 'hyprial-gui',
        mode,
        dataMode: liveEnabled ? 'live' : 'fixture',
        backendConfigured: liveEnabled || backendEndpoint !== null,
        backendEndpoint,
      }),
    );
  };

  return {
    name: 'hyprial-gui-health',
    configureServer(server) {
      server.middlewares.use(handler);
    },
    configurePreviewServer(server) {
      server.middlewares.use(handler);
    },
  };
}

export default defineConfig(({ mode }) => {
  const fileEnv = loadEnv(mode, guiDir, '');
  const devModuleLabelsRequested =
    process.env.HYPRIAL_DEV_MODULE_LABELS ??
    fileEnv.HYPRIAL_DEV_MODULE_LABELS ??
    '1';
  const devModuleLabels =
    mode === 'development' && devModuleLabelsRequested !== '0';
  const backendEndpoint = parseBackendEndpoint(
    process.env.HYPRIAL_BACKEND_ENDPOINT ?? fileEnv.HYPRIAL_BACKEND_ENDPOINT,
  );
  const liveEnabled = process.env.HYPRIAL_GUI_BACKEND_DRIVER === 'daemon';
  if (liveEnabled && backendEndpoint) {
    throw new Error('Choose the owned daemon backend or HYPRIAL_BACKEND_ENDPOINT, not both');
  }
  const proxy = backendEndpoint
    ? {
        '/api/gui/v1': {
          target: backendEndpoint,
          changeOrigin: false,
          ws: false,
        },
      }
    : undefined;

  return {
    base: './',
    resolve: { alias: { '@': resolve(guiDir, 'src') } },
    server: {
      host: '127.0.0.1',
      port: 4173,
      strictPort: true,
      proxy,
    },
    preview: {
      host: '127.0.0.1',
      port: 4173,
      strictPort: true,
      proxy,
    },
    define: {
      __HYPRIAL_LIVE_ENABLED__: JSON.stringify(liveEnabled),
      __HYPRIAL_REFERENCE_HTTP__: JSON.stringify(Boolean(process.env.HYPRIAL_GUI_REFERENCE_CONFIG)),
      __HYPRIAL_BACKEND_ENDPOINT__: JSON.stringify(backendEndpoint ?? ''),
      __HYPRIAL_DEV_MODULE_LABELS__: JSON.stringify(devModuleLabels),
      __HYPRIAL_DEV_PREVIEW__: JSON.stringify(mode === 'development'),
    },
    plugins: [tailwindcss(), liveBackendPlugin(liveEnabled), referenceHttpPlugin(process.env.HYPRIAL_GUI_REFERENCE_CONFIG, mode === 'development'), guiHealthPlugin(mode, backendEndpoint, liveEnabled)],
    build: {
      outDir: 'dist',
      emptyOutDir: true,
      rollupOptions: {
        input: {
          main: resolve(guiDir, 'index.html'),
        },
      },
    },
  };
});
