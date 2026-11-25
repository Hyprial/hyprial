// KP-038/T2（PR #2579 handoff §2.3，docs/design/gui-testing-architecture-handoff-2026-11-08.md）。
// 决策：生产产物“缺失/空/损坏/含禁止内容”必须让真实 verify-production-copy.mjs 失败，合法产物通过。
// - 直接 spawn 真实脚本，在 mkdtemp 隔离目录构造最小 dist；不 mock 脚本、不改脚本、
//   不读生产配置/凭据、不注入远端 CI、不删除或放宽 gui/tests/production-copy-gate.test.mjs 的既有规则。
// - 既有 gate 测试在 dist/assets 读取失败时直接 return 被记成功（假绿）；本文件
//   “缺 dist/assets / 空 assets 必须失败”两例即暴露该缺口。
// - verify 脚本当前只做文本 marker 扫描：空 assets、缺/空入口、空 JS 产物、
//   F-FIX-006 的 demo / 隔离预览 模块样例都会假通过。这些用例在脚本补齐产物完整性
//   校验前为预期红（WIP 红测试），不得用 skip 或放宽断言掩盖。
import { spawnSync } from 'node:child_process';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';

const TEST_TIMEOUT = 30_000;

function verifyScriptPath() {
  const testPath = expect.getState().testPath;
  return resolve(dirname(testPath), '../scripts/verify-production-copy.mjs');
}

const VALID_ENTRY_HTML = [
  '<!doctype html>',
  '<html lang="zh-CN">',
  '<head>',
  '<meta charset="utf-8">',
  '<title>Hyprial</title>',
  '<link rel="stylesheet" href="./assets/index-C0FFEE1.css">',
  '</head>',
  '<body>',
  '<div id="root"></div>',
  '<script type="module" src="./assets/index-C0FFEE1.js"></script>',
  '</body>',
  '</html>',
].join('\n');
const VALID_ENTRY_JS =
  'const root=document.querySelector("#root");if(root){root.textContent="Hyprial GUI ready";}';
const VALID_ENTRY_CSS =
  ':root{color-scheme:light}body{margin:0;font-family:system-ui,sans-serif}';

const tempRoots = [];

async function newTempRoot(caseName) {
  const root = await mkdtemp(join(tmpdir(), `hyprial-gui-artifact-${caseName}-`));
  tempRoots.push(root);
  return root;
}

async function buildDist(
  caseName,
  { files = {}, dirs = [] } = {},
) {
  const root = await newTempRoot(caseName);
  const distDir = join(root, 'dist');
  for (const rel of dirs) {
    await mkdir(join(distDir, rel), { recursive: true });
  }
  for (const [rel, content] of Object.entries(files)) {
    if (content === null) continue;
    const path = join(distDir, rel);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, content);
  }
  return distDir;
}

function validDistFiles(overrides = {}) {
  return {
    'index.html': VALID_ENTRY_HTML,
    'assets/index-C0FFEE1.js': VALID_ENTRY_JS,
    'assets/index-C0FFEE1.css': VALID_ENTRY_CSS,
    ...overrides,
  };
}

function runVerify(distDir) {
  const result = spawnSync(process.execPath, [verifyScriptPath(), distDir], {
    encoding: 'utf8',
    timeout: 20_000,
  });
  return {
    status: result.status,
    output: `${result.stdout ?? ''}\n${result.stderr ?? ''}`,
  };
}

function expectRejected(result, context) {
  expect(
    result.status,
    `${context}: verify-production-copy unexpectedly passed\n${result.output}`,
  ).not.toBe(0);
  return result.output;
}

afterAll(async () => {
  await Promise.all(
    tempRoots.map((root) => rm(root, { recursive: true, force: true })),
  );
});

describe('production artifact contract: valid package', () => {
  it(
    'accepts a minimal valid dist with entry html and non-empty assets',
    { timeout: TEST_TIMEOUT },
    async () => {
      const distDir = await buildDist('valid', {
        files: validDistFiles(),
      });
      const result = runVerify(distDir);
      expect(
        result.status,
        `valid dist was rejected\n${result.output}`,
      ).toBe(0);
      expect(result.output).toContain('Production copy gate passed');
    },
  );
});

describe('production artifact contract: missing or empty artifacts must fail', () => {
  it(
    'fails when the dist directory itself is missing and names the missing path',
    { timeout: TEST_TIMEOUT },
    async () => {
      const root = await newTempRoot('missing-dist');
      const distDir = join(root, 'dist');
      const result = runVerify(distDir);
      const output = expectRejected(result, 'missing dist directory');
      expect(output).toContain(distDir);
    },
  );

  it(
    'fails when dist exists but the assets directory is missing',
    { timeout: TEST_TIMEOUT },
    async () => {
      const distDir = await buildDist('no-assets', {
        files: { 'index.html': VALID_ENTRY_HTML },
      });
      const result = runVerify(distDir);
      const output = expectRejected(result, 'missing dist/assets');
      expect(output).toContain('assets');
    },
  );

  it(
    'fails when dist/assets exists but is empty',
    { timeout: TEST_TIMEOUT },
    async () => {
      const distDir = await buildDist('empty-assets', {
        files: { 'index.html': VALID_ENTRY_HTML },
        dirs: ['assets'],
      });
      const result = runVerify(distDir);
      const output = expectRejected(result, 'empty dist/assets');
      expect(output).toContain('assets');
    },
  );
});

describe('production artifact contract: corrupted or incomplete artifacts must fail', () => {
  it(
    'fails when the entry index.html is missing',
    { timeout: TEST_TIMEOUT },
    async () => {
      const distDir = await buildDist('no-entry', {
        files: validDistFiles({ 'index.html': null }),
      });
      const result = runVerify(distDir);
      const output = expectRejected(result, 'missing entry index.html');
      expect(output).toContain('index.html');
    },
  );

  it(
    'fails when the entry index.html is empty',
    { timeout: TEST_TIMEOUT },
    async () => {
      const distDir = await buildDist('empty-entry', {
        files: validDistFiles({ 'index.html': '' }),
      });
      const result = runVerify(distDir);
      const output = expectRejected(result, 'empty entry index.html');
      expect(output).toContain('index.html');
    },
  );

  it(
    'fails when a required JS asset is truncated to zero bytes',
    { timeout: TEST_TIMEOUT },
    async () => {
      const distDir = await buildDist('empty-js', {
        files: validDistFiles({ 'assets/index-C0FFEE1.js': '' }),
      });
      const result = runVerify(distDir);
      const output = expectRejected(result, 'empty JS asset');
      expect(output).toContain('index-C0FFEE1.js');
    },
  );
});

describe('production artifact contract: forbidden markers must fail with named diagnostics', () => {
  const markerCases = [
    {
      label: 'dev-module-index',
      snippet: 'registerModule("dev-module-index");',
      target: 'assets/index-C0FFEE1.js',
    },
    {
      label: 'fixture',
      snippet: 'const s=createFixtureState();',
      target: 'assets/index-C0FFEE1.js',
    },
    {
      label: 'Mock',
      snippet: 'new MockScenarioController();',
      target: 'assets/index-C0FFEE1.js',
    },
    {
      label: '演示',
      snippet: '<title>Hyprial 演示</title>',
      target: 'index.html',
    },
  ];

  it.each(markerCases)(
    'rejects a valid dist containing forbidden marker $label',
    { timeout: TEST_TIMEOUT },
    async ({ label, snippet, target }) => {
      const distDir = await buildDist(`marker-${label}`, {
        files: validDistFiles({
          [target]: `${
            target === 'index.html' ? VALID_ENTRY_HTML : VALID_ENTRY_JS
          }\n${snippet}`,
        }),
      });
      const result = runVerify(distDir);
      const output = expectRejected(result, `forbidden marker ${label}`);
      expect(output).toContain(label);
    },
  );
});

describe('production artifact contract: forbidden module samples must fail', () => {
  const moduleCases = [
    {
      name: 'demo composition (F-FIX-006)',
      snippet: 'function demoPanelController(){return"demo"}export{demoPanelController};',
      diagnostic: 'demo',
    },
    {
      name: '隔离预览 composition (F-FIX-006)',
      snippet: 'const previewHeading="隔离预览";export{previewHeading};',
      diagnostic: '隔离预览',
    },
  ];

  it.each(moduleCases)(
    'rejects a dist whose bundle keeps the $name',
    { timeout: TEST_TIMEOUT },
    async ({ name, snippet, diagnostic }) => {
      const distDir = await buildDist(`module-${name}`, {
        files: validDistFiles({
          'assets/index-C0FFEE1.js': `${VALID_ENTRY_JS}\n${snippet}`,
        }),
      });
      const result = runVerify(distDir);
      const output = expectRejected(result, `forbidden module sample ${name}`);
      expect(output).toContain(diagnostic);
    },
  );
});
