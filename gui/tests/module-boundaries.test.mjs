// gui 模块边界门的独立测试（WIP PR #2578 C1）。
// 决策：
// - 全部用临时目录里的合成 gui 树驱动 scripts/check-module-boundaries.mjs 子进程，
//   不读取生产配置、baseline 或凭据；目录用完即删。
// - 只断言退出码与输出中的规则标识，不解析人类可读格式以外的结构。
import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { afterEach, describe, expect, it } from 'vitest';

const SCRIPT = join(
  dirname(fileURLToPath(import.meta.url)),
  '..',
  'scripts',
  'check-module-boundaries.mjs',
);
const roots = [];

async function makeTree(files) {
  const root = await mkdtemp(join(tmpdir(), 'gui-boundary-'));
  roots.push(root);
  for (const [path, content] of Object.entries(files)) {
    const full = join(root, path);
    await mkdir(join(full, '..'), { recursive: true });
    await writeFile(full, content, 'utf8');
  }
  return root;
}

function run(root, args = []) {
  const result = spawnSync(process.execPath, [SCRIPT, '--gui-root', root, ...args], {
    encoding: 'utf8',
  });
  return {
    code: result.status,
    stdout: result.stdout ?? '',
    stderr: result.stderr ?? '',
  };
}

const CLEAN_TREE = {
  'src/main.tsx': [
    "const start = async () => {",
    "  const mod = await import('./dev/DevRoot');",
    '  mod.render();',
    '};',
    'void start();',
  ].join('\n'),
  'src/App.tsx': [
    "import { Widget } from './features/alpha';",
    "export function App() { return <Widget />; }",
    "export function Lazy() { return import('./features/beta'); }",
  ].join('\n'),
  'src/features/alpha/index.ts': "export { Widget } from './impl/Widget';\n",
  'src/features/alpha/impl/Widget.tsx': 'export function Widget() { return null; }\n',
  'src/features/beta/index.ts': "export { Beta } from './impl/Beta';\n",
  'src/features/beta/impl/Beta.tsx': 'export function Beta() { return null; }\n',
  'src/dev/DevRoot.tsx': [
    '// import { Widget } from "@/features/alpha/impl/Widget";',
    "import { Beta } from '../features/beta';",
    'export function render() { return <Beta />; }',
  ].join('\n'),
  'src/components/primitives/index.ts': "export { Button } from './Button';\n",
  'src/components/primitives/Button.tsx': 'export function Button() { return null; }\n',
  'src/client/index.ts': "export { fetchThing } from './transport';\n",
  'src/client/transport.ts': 'export function fetchThing() { return 1; }\n',
  'src/state/types.ts': 'export type GuiPage = "chat";\n',
  'config/module-boundary-baseline.json': JSON.stringify({
    version: 1,
    rules: {
      'feature-structure': [],
      'cross-feature-deep-import': [],
      'prod-imports-dev': [],
      'src-imports-tests': [],
      'feature-index-exports-mock': [],
      'primitives-layering': [],
      'base-layer-references-feature': [],
    },
  }, null, 2),
};

function baselineWith(rules) {
  return JSON.stringify({ version: 1, rules }, null, 2);
}

function emptyRules() {
  return {
    'feature-structure': [],
    'cross-feature-deep-import': [],
    'prod-imports-dev': [],
    'src-imports-tests': [],
    'feature-index-exports-mock': [],
    'primitives-layering': [],
    'base-layer-references-feature': [],
  };
}

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

describe('module boundary gate', () => {
  it('passes a clean tree and allows main.tsx to dynamically import dev composition', async () => {
    const root = await makeTree(CLEAN_TREE);
    const result = run(root);
    expect(result.code, result.stdout + result.stderr).toBe(0);
    expect(result.stdout).toContain('module-boundaries: PASS');
  });

  it('flags deep impl imports across features, including multiline and alias forms', async () => {
    const root = await makeTree({
      ...CLEAN_TREE,
      'src/features/beta/impl/Beta.tsx': [
        'import {',
        '  Widget,',
        "} from '@/features/alpha/impl/Widget';",
        'export function Beta() { return <Widget />; }',
      ].join('\n'),
    });
    const result = run(root);
    expect(result.code).toBe(1);
    expect(result.stdout).toContain('FAIL cross-feature-deep-import');
    expect(result.stdout).toContain(
      'NEW src/features/beta/impl/Beta.tsx -> src/features/alpha/impl/Widget.tsx',
    );
  });

  it('flags production imports of dev/ and gui/tests, including type-only imports', async () => {
    const root = await makeTree({
      ...CLEAN_TREE,
      'src/App.tsx': [
        "import { Widget } from './features/alpha';",
        "import type { DevRoot } from './dev/DevRoot';",
        "import { seed } from '../tests/fixtures/seed';",
        'export function App() { return <Widget />; }',
      ].join('\n'),
      'tests/fixtures/seed.ts': 'export const seed = 1;\n',
    });
    const result = run(root);
    expect(result.code).toBe(1);
    expect(result.stdout).toContain('NEW src/App.tsx -> src/dev/DevRoot.tsx');
    expect(result.stdout).toContain('NEW src/App.tsx -> tests/fixtures/seed.ts');
  });

  it('flags features missing an index, an impl directory, or with an empty impl', async () => {
    const root = await makeTree({
      ...CLEAN_TREE,
      'src/features/gamma/a.ts': 'export const a = 1;\n',
      'src/features/delta/index.ts': 'export const d = 1;\n',
      'src/features/epsilon/index.ts': 'export const e = 1;\n',
      'src/features/epsilon/impl/.keep': '\n',
    });
    const result = run(root);
    expect(result.code).toBe(1);
    expect(result.stdout).toContain('NEW features/gamma:missing-index');
    expect(result.stdout).toContain('NEW features/gamma:missing-impl');
    expect(result.stdout).toContain('NEW features/delta:missing-impl');
    expect(result.stdout).toContain('NEW features/epsilon:empty-impl');
  });

  it('flags a public feature index that re-exports a mock or fixture', async () => {
    const root = await makeTree({
      ...CLEAN_TREE,
      'src/features/alpha/index.ts': [
        "export { Widget } from './impl/Widget';",
        "export { alphaMock } from './impl/api/mock';",
      ].join('\n'),
      'src/features/alpha/impl/api/mock.ts': 'export const alphaMock = 1;\n',
    });
    const result = run(root);
    expect(result.code).toBe(1);
    expect(result.stdout).toContain('FAIL feature-index-exports-mock');
    expect(result.stdout).toContain(
      'NEW src/features/alpha/index.ts -> src/features/alpha/impl/api/mock.ts',
    );
  });

  it('rejects mock factories exported from an otherwise production-named module', async () => {
    const root = await makeTree({
      ...CLEAN_TREE,
      'src/features/alpha/index.ts': [
        "export { Widget } from './impl/Widget';",
        "export { createMockApi as createApi } from './impl/api';",
      ].join('\n'),
      'src/features/alpha/impl/api.ts': 'export function createMockApi() { return {}; }\n',
    });
    const result = run(root);
    expect(result.code).toBe(1);
    expect(result.stdout).toContain('FAIL feature-index-exports-mock');
  });

  it('flags primitives that depend on features, pages, dev or state, and base layers referencing features', async () => {
    const root = await makeTree({
      ...CLEAN_TREE,
      'src/components/primitives/Button.tsx': [
        "import { Widget } from '@/features/alpha';",
        'export function Button() { return <Widget />; }',
      ].join('\n'),
      'src/client/transport.ts': [
        "import type { Widget } from '../features/alpha';",
        'export function fetchThing() { return 1; }',
      ].join('\n'),
    });
    const result = run(root);
    expect(result.code).toBe(1);
    expect(result.stdout).toContain('NEW src/components/primitives/Button.tsx -> src/features/alpha/index.ts');
    expect(result.stdout).toContain('NEW src/client/transport.ts -> src/features/alpha/index.ts');
  });

  it('fails closed on unresolved imports and parse errors, and refuses to baseline them', async () => {
    const unresolvedRoot = await makeTree({
      ...CLEAN_TREE,
      'src/App.tsx': "import { Widget } from './features/alpha';\nimport { gone } from './gone-module';\n",
    });
    const unresolved = run(unresolvedRoot);
    expect(unresolved.code).toBe(1);
    expect(unresolved.stdout).toContain('HARD FAILURE unresolved-import:src/App.tsx:2:./gone-module');

    const parseRoot = await makeTree({
      ...CLEAN_TREE,
      'src/App.tsx': "import { Widget } from './features/alpha';\nexport function App() { return <Widget /; }\n",
    });
    const parsed = run(parseRoot);
    expect(parsed.code).toBe(1);
    expect(parsed.stdout).toContain('HARD FAILURE parse-error:src/App.tsx');

    const updated = run(parseRoot, ['--update-baseline', '--accept-new']);
    expect(updated.code).toBe(1);
    expect(updated.stdout).toContain('cannot be baselined');
  });

  it('passes when the baseline matches the current violations exactly', async () => {
    const rules = emptyRules();
    rules['cross-feature-deep-import'] = ['src/App.tsx -> src/features/alpha/impl/Widget.tsx'];
    const root = await makeTree({
      ...CLEAN_TREE,
      'src/App.tsx': "import { Widget } from './features/alpha/impl/Widget';\nexport function App() { return <Widget />; }\n",
      'config/module-boundary-baseline.json': baselineWith(rules),
    });
    const result = run(root);
    expect(result.code, result.stdout).toBe(0);
  });

  it('fails on stale baseline entries so exceptions can only shrink through explicit updates', async () => {
    const rules = emptyRules();
    rules['cross-feature-deep-import'] = [
      'src/App.tsx -> src/features/alpha/impl/Widget.tsx',
      'src/App.tsx -> src/features/beta/impl/Beta.tsx',
    ];
    const root = await makeTree({
      ...CLEAN_TREE,
      'src/App.tsx': "import { Widget } from './features/alpha/impl/Widget';\nexport function App() { return <Widget />; }\n",
      'config/module-boundary-baseline.json': baselineWith(rules),
    });
    const result = run(root);
    expect(result.code).toBe(1);
    expect(result.stdout).toContain('FAIL cross-feature-deep-import');
    expect(result.stdout).toContain('STALE src/App.tsx -> src/features/beta/impl/Beta.tsx');
  });

  it('rejects malformed baselines instead of ignoring unknown keys', async () => {
    const unknownKeyRoot = await makeTree({
      ...CLEAN_TREE,
      'config/module-boundary-baseline.json': baselineWith({ ...emptyRules(), 'made-up-rule': [] }),
    });
    const unknownKey = run(unknownKeyRoot);
    expect(unknownKey.code).toBe(2);
    expect(unknownKey.stderr).toContain('unknown rule key');

    const brokenJsonRoot = await makeTree({
      ...CLEAN_TREE,
      'config/module-boundary-baseline.json': '{ not json',
    });
    const brokenJson = run(brokenJsonRoot);
    expect(brokenJson.code).toBe(2);
    expect(brokenJson.stderr).toContain('not valid JSON');
  });

  it('update-baseline prunes stale entries but refuses new exceptions without --accept-new', async () => {
    const rules = emptyRules();
    rules['cross-feature-deep-import'] = ['src/App.tsx -> src/features/beta/impl/Beta.tsx'];
    const root = await makeTree({
      ...CLEAN_TREE,
      'src/App.tsx': "import { Widget } from './features/alpha/impl/Widget';\nexport function App() { return <Widget />; }\n",
      'config/module-boundary-baseline.json': baselineWith(rules),
    });
    const refused = run(root, ['--update-baseline']);
    expect(refused.code).toBe(1);
    expect(refused.stdout).toContain('refusing to add new exceptions');
    const afterRefusal = run(root);
    expect(afterRefusal.code).toBe(1);

    const accepted = run(root, ['--update-baseline', '--accept-new']);
    expect(accepted.code).toBe(0);
    expect(accepted.stdout).toContain('ADDED src/App.tsx -> src/features/alpha/impl/Widget.tsx');
    expect(accepted.stdout).toContain('PRUNED src/App.tsx -> src/features/beta/impl/Beta.tsx');

    const passing = run(root);
    expect(passing.code, passing.stdout).toBe(0);

    const pruneOnly = await makeTree({
      ...CLEAN_TREE,
      'config/module-boundary-baseline.json': baselineWith(rules),
    });
    const pruned = run(pruneOnly, ['--update-baseline']);
    expect(pruned.code).toBe(0);
    expect(pruned.stdout).toContain('pruned 1 stale exception(s), added 0 new exception(s)');
  });
});
