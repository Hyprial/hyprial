// @vitest-environment node
/*
 * Decision record (dev tools chunk, PR 4b-G of the single GUI launch path
 * plan, 2026-11-15): builds the release with the real vite config (in
 * memory, nothing written) and checks where the tool layer lands.
 * - Everything that names the tool layer is emitted under `dev-tools/`; the
 *   product closure (index.html and every other file) carries none of those
 *   names, and index.html neither references nor preloads `dev-tools/`.
 * - The layer is reached only through a dynamic import from the entry chunk,
 *   so a launch without the host capability never requests it.
 * - The Swagger UI files travel with the build under `dev-tools/docs/`.
 */
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { build } from 'vite';
import { beforeAll, describe, expect, it } from 'vitest';

const TOOL_LAYER_NAMES = [
  'dev-01',
  '样例',
  'dev-module-index',
  'dev-module-dock',
  'dev-module-entry',
  'dev-module-scroll-hint',
  'dev-data-source',
  'dev-scenario',
];

const SWAGGER_UI_DIST_5_17_14 = {
  'swagger-ui-bundle.js': 'c2e4a9ef08144839ff47c14202063ecfe4e59e70a4e7154a26bd50d880c88ba1',
  'swagger-ui.css': '40170f0ee859d17f92131ba707329a88a070e4f66874d11365e9a77d232f6117',
  LICENSE: 'cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30',
  NOTICE: '0d20d1adef18aee3f40dd258172155521ce702ac445cb5f7b7d60ed32dad2fb2',
};

let files;

function text(item) {
  return item.type === 'chunk' ? item.code : typeof item.source === 'string' ? item.source : '';
}

beforeAll(async () => {
  const result = await build({
    root: process.cwd(),
    logLevel: 'silent',
    mode: 'production',
    build: { write: false, outDir: 'dist-layout-probe' },
  });
  files = (Array.isArray(result) ? result : [result]).flatMap((entry) => entry.output);
}, 120_000);

describe('release build layout of the developer tool layer', () => {
  it('emits the tool layer chunk and its stylesheet under dev-tools/', () => {
    const names = files.map((item) => item.fileName);
    expect(names.filter((name) => /^dev-tools\/[^/]+\.js$/.test(name))).toHaveLength(1);
    expect(names.filter((name) => /^dev-tools\/[^/]+\.css$/.test(name))).toHaveLength(1);
  });

  it('keeps every file that names the tool layer under dev-tools/', () => {
    const marked = files
      .filter((item) => /\.(js|css|html)$/.test(item.fileName))
      .filter((item) => TOOL_LAYER_NAMES.some((name) => text(item).includes(name)))
      .map((item) => item.fileName);
    expect(marked.length).toBeGreaterThan(0);
    expect(marked.filter((name) => !name.startsWith('dev-tools/'))).toEqual([]);
  });

  it('does not reference or preload dev-tools/ from index.html', () => {
    const html = files.find((item) => item.fileName === 'index.html');
    expect(html).toBeDefined();
    expect(text(html)).not.toContain('dev-tools');
  });

  it('reaches the tool chunk only through a dynamic import', () => {
    const entry = files.find((item) => item.type === 'chunk' && item.isEntry);
    expect(entry.imports.filter((name) => name.startsWith('dev-tools/'))).toEqual([]);
    expect(entry.dynamicImports.filter((name) => name.startsWith('dev-tools/'))).toHaveLength(1);
  });

  it('ships exactly the four Swagger UI files, byte for byte, under dev-tools/docs/', () => {
    const docs = files.filter((item) => item.fileName.startsWith('dev-tools/docs/'));
    expect(docs.map((item) => item.fileName).sort()).toEqual(
      Object.keys(SWAGGER_UI_DIST_5_17_14).map((name) => `dev-tools/docs/${name}`).sort(),
    );
    for (const item of docs) {
      const name = item.fileName.slice('dev-tools/docs/'.length);
      const bytes = Buffer.from(item.source);
      expect(createHash('sha256').update(bytes).digest('hex'), name).toBe(SWAGGER_UI_DIST_5_17_14[name]);
    }
  });

  it('pins swagger-ui-dist exactly as a development dependency', () => {
    const manifest = JSON.parse(readFileSync('package.json', 'utf8'));
    expect(manifest.devDependencies['swagger-ui-dist']).toBe('5.17.14');
    expect(manifest.dependencies?.['swagger-ui-dist']).toBeUndefined();
    const lock = JSON.parse(readFileSync('package-lock.json', 'utf8'));
    expect(lock.packages['node_modules/swagger-ui-dist']).toMatchObject({ version: '5.17.14', license: 'Apache-2.0' });
  });
});
