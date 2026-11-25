import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { afterAll, describe, expect, it } from 'vitest';
import {
  TOOL_LAYER_OWN,
  findProductionCopyViolations,
  findToolLayerCopyViolations,
  readBundleText,
  verifyArtifactScopes,
} from '../scripts/verify-production-copy.mjs';

it.each(['/api/reference/mock-success', 'example-not-a-credential', 'ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs', 'MOCK_SCENARIO_MISSING'])('rejects reference mock material: %s', copy => {
  expect(findProductionCopyViolations(copy).length).toBeGreaterThan(0);
});

describe('production copy gate', () => {
  it('accepts a bundle without development or placeholder copy', () => {
    expect(findProductionCopyViolations('Hyprial GUI')).toEqual([]);
  });

  it('rejects the J6 synthetic system name in a full phrase', () => {
    expect(findProductionCopyViolations('Hyprial macOS 合成系统')).toContain(
      '合成系统',
    );
  });

  it.each([
    'dev-01',
    'fixture',
    '演示',
    '样例',
    '占位',
    'dev-data-source',
    'dev-module-index',
    'dev-module-dock',
    'dev-module-entry',
    'dev-preview-frame',
    'dev-module-scroll-hint',
    'studio-mac',
    'travel-mac',
    '合成系统',
    '本地静态演示',
    '不是实时数据',
    'edge-demo-1',
    '2026-10-22 10:15',
    'GUI-LINK-001',
    'GUI-ORG-001',
    'dev-local',
    '切换为受邀成员',
    '切换为创建者',
    'Mock',
    '接口未接通',
    '邀请码演示',
    '服务接入前',
    '不会改变账号',
    '服务尚未接入',
    '未执行任何操作',
    '不会更改连接状态',
    'add-failure',
    'authorize-failure',
    'hyprialFixture',
    'hyprialScenario',
    'hyprialFirstRun',
    'mockComposition',
    'mock-composition',
  ])(
    'rejects production copy containing %s',
    (copy) => {
      expect(findProductionCopyViolations(`Hyprial ${copy}`)).toContain(copy);
    },
  );

  it.each([
    "scenario 'demo' falls back to live",
    'a demonstration of the workflow',
    'firstRunScenario: D ? "demo" : "live"',
  ])('does not flag ordinary product wording: %s', (copy) => {
    expect(findProductionCopyViolations(copy)).toEqual([]);
  });

  it('rejects the surviving first-run demo scenario branch in bundle shape', () => {
    expect(findProductionCopyViolations('firstRunScenario:D?`demo`:`live`')).toContain('demo-scenario');
    expect(findProductionCopyViolations('t===`demo`&&!c')).toContain('demo-scenario');
  });
});

// PR-F：产物/行为导向守卫（不再对源码做字符匹配）。
// 1) production 产物里不得出现被禁 marker；2) fixture 组合符号不得进 production bundle；
// 3) host/preview 判别靠正向 host 判断（OrgPage 用 === 'host'，不与 fixture 比较）。
describe('production bundle boundary', () => {
  it('fails closed when the production bundle directory is missing', async () => {
    const base = await mkdtemp(join(tmpdir(), 'gui-copy-gate-'));
    try {
      await expect(readBundleText(join(base, 'absent-dist'))).rejects.toThrow(/missing/i);
    } finally {
      await rm(base, { recursive: true, force: true });
    }
  });

  it('keeps the fixture composition symbols out of the production bundle', async () => {
    const text = await readBundleText(resolve(process.cwd(), 'dist'));
    expect(text.length, 'production JS bundle is missing or empty').toBeGreaterThan(0);
    for (const marker of ['fixture', 'createFixtureState', 'dev-preview-frame']) {
      expect(text.includes(marker)).toBe(false);
    }
    // F-FIX-006：DEV-only 的 demo/隔离预览组合必须被结构性 DCE，production 产物 0 处。
    for (const marker of ['demo', '隔离预览']) {
      expect(text.includes(marker), `production bundle still contains ${marker}`).toBe(false);
    }
    expect(findProductionCopyViolations(text)).toEqual([]);
  });
});

describe('production artifact scopes (product closure, dev-tools, vendored docs)', () => {
  const OWN_MARKER = 'dev-module-index';
  const DOCS_FILES = {
    'swagger-ui-bundle.js': 'bundle-bytes',
    'swagger-ui.css': 'css-bytes',
    LICENSE: 'licence-bytes',
    NOTICE: 'notice-bytes',
  };
  const HTML = '<!doctype html><script type="module" src="./assets/main.js"></script>';
  const roots = [];

  afterAll(async () => {
    await Promise.all(roots.map((root) => rm(root, { recursive: true, force: true })));
  });

  async function tree(files, vendor = DOCS_FILES) {
    const base = await mkdtemp(join(tmpdir(), 'gui-copy-scopes-'));
    roots.push(base);
    const dist = join(base, 'dist');
    const vendorDirectory = join(base, 'vendor');
    await mkdir(vendorDirectory, { recursive: true });
    for (const [name, content] of Object.entries(vendor)) {
      await writeFile(join(vendorDirectory, name), content);
    }
    for (const [name, content] of Object.entries({ 'index.html': HTML, 'assets/main.js': 'ok', ...files })) {
      await mkdir(join(dist, name, '..'), { recursive: true });
      await writeFile(join(dist, name), content);
    }
    return { dist, vendorDirectory };
  }

  async function problems(files, vendor) {
    const { dist, vendorDirectory } = await tree(files, vendor);
    return verifyArtifactScopes(dist, { vendorDirectory });
  }

  const docs = (overrides = {}) => Object.fromEntries(
    Object.entries({ ...DOCS_FILES, ...overrides }).map(([name, content]) => [`dev-tools/docs/${name}`, content]),
  );

  it('keeps the tool layer own names apart from mock, fixture and scenario data', () => {
    expect([...TOOL_LAYER_OWN].sort()).toEqual([
      'dev-01', 'dev-data-source', 'dev-module-dock', 'dev-module-entry', 'dev-module-index',
      'dev-module-scroll-hint', 'dev-scenario', '样例',
    ]);
    expect(findToolLayerCopyViolations(`x ${OWN_MARKER}`)).toEqual([]);
    expect(findToolLayerCopyViolations('hyprialScenario')).toEqual(['hyprialScenario']);
    expect(findProductionCopyViolations(OWN_MARKER)).toEqual([OWN_MARKER]);
  });

  it('fails a tool layer name outside dev-tools and accepts it inside', async () => {
    expect((await problems({ 'assets/x.js': OWN_MARKER })).join('\n')).toContain(OWN_MARKER);
    expect(await problems({ 'dev-tools/x.js': OWN_MARKER })).toEqual([]);
  });

  it('fails mock, fixture and scenario data inside dev-tools', async () => {
    expect((await problems({ 'dev-tools/x.js': 'hyprialScenario' })).join('\n')).toContain('hyprialScenario');
    expect((await problems({ 'dev-tools/x.css': '.a::after{content:"Mock"}' })).join('\n')).toContain('Mock');
  });

  it.each(['./dev-tools/x.js', '/dev-tools/x.js', 'dev-tools/x.css'])('fails index.html referencing %s', async (reference) => {
    const found = await problems({ 'index.html': `${HTML}<link rel="modulepreload" href="${reference}">`, 'dev-tools/x.js': 'ok' });
    expect(found.join('\n')).toContain('index.html');
  });

  it('accepts the exact vendored docs set and rejects any deviation', async () => {
    expect(await problems({ 'dev-tools/x.js': 'ok', ...docs() })).toEqual([]);
    expect((await problems({ ...docs({ 'extra.js': 'x' }) })).join('\n')).toContain('extra.js');
    expect((await problems({ ...docs({ 'swagger-ui.css': 'tampered' }) })).join('\n')).toContain('swagger-ui.css');
    const missing = docs();
    delete missing['dev-tools/docs/NOTICE'];
    expect((await problems(missing)).join('\n')).toContain('NOTICE');
    expect((await problems({ 'dev-tools/docs/nested/swagger-ui.css': 'x', ...docs() })).join('\n')).toContain('nested');
  });

  it('does not scan vendored docs as tool layer copy', async () => {
    expect(await problems({ ...docs({ 'swagger-ui-bundle.js': 'Mock fixture hyprialScenario' }) }, { ...DOCS_FILES, 'swagger-ui-bundle.js': 'Mock fixture hyprialScenario' })).toEqual([]);
  });

  it('passes without a dev-tools directory, so the layer can be reverted', async () => {
    expect(await problems({})).toEqual([]);
  });

  it('reads the product closure only through readBundleText', async () => {
    const { dist } = await tree({ 'dev-tools/x.js': OWN_MARKER, ...docs() });
    expect(await readBundleText(dist)).not.toContain(OWN_MARKER);
  });
});
