import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { resolve } from 'node:path';
import { defineConfig, type Plugin } from 'vite';
import tailwindcss from '@tailwindcss/vite';

/*
 * WP2 decisions (gui-dev-product-parity-2026-11-09, third integration round):
 * - CSS runs one transformer (lightningcss) with one explicit target set for
 *   dev serving and both production artifacts, so -webkit-backdrop-filter and
 *   the ordering of the fixed ≤640 nav rules cannot drift between dev and
 *   build output (targets keep 100dvh native via safari/ios_saf 15.4+).
 * - HYPRIAL_GUI_PARITY=1 (production mode only) defines __HYPRIAL_PARITY__=true
 *   and emits dist-parity for the browser parity gate; the normal build emits
 *   dist with the token false. Dev preview logic is unchanged.
 * - Developer tool layer (PR 4b-G, 2026-11-15): a chunk whose modules all live
 *   under src/dev/, and a stylesheet that originates there, are emitted under
 *   dist/dev-tools/ instead of dist/assets/. The only entry that loads that
 *   chunk is the dynamic import in src/main.tsx, so index.html never
 *   references it and the host can gate the prefix as one unit.
 * - Swagger UI (PR 4b-G): the release build copies four files of the exact
 *   devDependency swagger-ui-dist (bundle, stylesheet, LICENSE, NOTICE) to
 *   dist/dev-tools/docs/ at build time. The plugin is build-only and defines
 *   no dev or preview server hook; the dist-parity test artifact does not
 *   carry it. The artifact gate pins the file set and the hashes.
 * - One launch path (2026-11-16): this configuration serves no backend. The
 *   Python session host (src/hyprial/shell/impl/gui/host) owns /daemon/* and
 *   serves gui/dist, or relays to this dev server for `hyprial gui dev`, so the
 *   Node daemon-request plugin is gone and /__gui/healthz here reports only the
 *   dataless dev server (dataMode fixture or unavailable, no driver).
 */
const guiDir = fileURLToPath(new URL('.', import.meta.url));

function toolLayerOnly(moduleIds: readonly string[]): boolean {
  return moduleIds.length > 0 && moduleIds.every((id) => id.includes('/src/dev/'));
}

const SWAGGER_UI_FILES = ['swagger-ui-bundle.js', 'swagger-ui.css', 'LICENSE', 'NOTICE'] as const;

function swaggerUiAssetsPlugin(): Plugin {
  return {
    name: 'hyprial-swagger-ui-assets',
    apply: 'build',
    generateBundle() {
      for (const name of SWAGGER_UI_FILES) {
        this.emitFile({
          type: 'asset',
          fileName: `dev-tools/docs/${name}`,
          source: new Uint8Array(readFileSync(resolve(guiDir, 'node_modules/swagger-ui-dist', name))),
        });
      }
    },
  };
}

function guiHealthPlugin(mode: string, dataMode: 'fixture' | 'unavailable'): Plugin {
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
    response.end(JSON.stringify({
      ok: true,
      service: 'hyprial-gui',
      mode,
      dataMode,
      driver: null,
      backendConfigured: false,
      session: 'unavailable',
    }));
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
  const devPreview = mode === 'development' && process.env.HYPRIAL_GUI_DEV_PREVIEW === '1';
  const parityRequested = process.env.HYPRIAL_GUI_PARITY === '1';
  if (parityRequested && mode !== 'production') {
    throw new Error('HYPRIAL_GUI_PARITY requires the production build mode');
  }
  const parityBuild = parityRequested;
  const dataMode = devPreview ? 'fixture' : 'unavailable';
  const cssTargets = {
    chrome: 107 << 16,
    edge: 107 << 16,
    firefox: 106 << 16,
    safari: (15 << 16) | (4 << 8),
    ios_saf: (15 << 16) | (4 << 8),
    opera: 93 << 16,
  };

  return {
    base: './',
    resolve: { alias: { '@': resolve(guiDir, 'src') } },
    css: {
      transformer: 'lightningcss',
      lightningcss: {
        targets: cssTargets,
      },
    },
    server: {
      host: '127.0.0.1',
      port: 4173,
      strictPort: true,
    },
    preview: {
      host: '127.0.0.1',
      port: 4173,
      strictPort: true,
    },
    define: {
      __HYPRIAL_DEV_PREVIEW__: JSON.stringify(devPreview),
      __HYPRIAL_PARITY__: JSON.stringify(parityBuild),
    },
    plugins: [
      tailwindcss(),
      guiHealthPlugin(mode, dataMode),
      ...(parityBuild ? [] : [swaggerUiAssetsPlugin()]),
    ],
    build: {
      outDir: parityBuild ? 'dist-parity' : 'dist',
      emptyOutDir: true,
      cssMinify: 'lightningcss',
      rollupOptions: {
        input: {
          main: resolve(guiDir, 'index.html'),
        },
        output: {
          chunkFileNames: (chunk: { moduleIds: readonly string[] }) => (
            toolLayerOnly(chunk.moduleIds) ? 'dev-tools/[name]-[hash].js' : 'assets/[name]-[hash].js'
          ),
          assetFileNames: (asset: { originalFileNames?: readonly string[] }) => (
            (asset.originalFileNames ?? []).some((name) => name.includes('src/dev/'))
              ? 'dev-tools/[name]-[hash][extname]'
              : 'assets/[name]-[hash][extname]'
          ),
        },
      },
    },
  };
});
