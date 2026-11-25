import { mkdtemp, mkdir, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import {
  COMPOSITION_ROOT_EXCEPTIONS,
  analyzeTree,
  formatViolations,
  isDevOnlyModule,
} from '../scripts/verify-dev-product-parity.mjs';

async function writeTree(files) {
  const root = await mkdtemp(join(tmpdir(), 'gui-parity-static-'));
  for (const [relativePath, content] of Object.entries(files)) {
    const absolute = join(root, relativePath);
    await mkdir(join(absolute, '..'), { recursive: true });
    await writeFile(absolute, content, 'utf8');
  }
  return root;
}

let roots = [];
beforeEach(() => {
  roots = [];
});
afterEach(async () => {
  await Promise.all(roots.map(root => rm(root, { recursive: true, force: true })));
});

async function treeWith(files) {
  const root = await writeTree(files);
  roots.push(root);
  return root;
}

const ENTRY = "import { createRoot } from 'react-dom/client';\nvoid createRoot;\n";
const DEV_TOOL = "export const DevTool = () => null;\nexport const devLabel = __HYPRIAL_DEV_PREVIEW__;\n";
const NO_EXCEPTIONS = {};

function rulesOf(violations) {
  return violations.map(violation => violation.rule);
}

describe('dev/product parity static gate', () => {
  it('passes a clean product tree', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/App.tsx': "import { Page } from './pages/Page';\nexport const App = () => null;\n",
      'src/pages/Page.tsx': 'export const Page = () => null;\n',
    });
    const { violations, exceptionsUsed } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(violations).toEqual([]);
    expect(exceptionsUsed).toEqual([]);
  });

  it('fails closed when the source root is missing', async () => {
    const root = await treeWith({ 'not-src/keep.txt': 'x' });
    await expect(analyzeTree(join(root, 'src'))).rejects.toThrow(/missing/i);
  });

  it('requires the composition root entry to exist', async () => {
    const root = await treeWith({ 'src/App.tsx': 'export const App = () => null;\n' });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(rulesOf(violations)).toContain('missing-entry');
  });

  it.each([
    {
      name: 'relative dev import',
      source: "import { DevTool } from '../dev-mode';\nexport const Page = () => null;\n",
    },
    {
      name: 'type-only dev import',
      source: "import type { DevModuleId } from '../dev-mode';\nexport const Page = () => null;\n",
    },
    {
      name: 'dev session type import',
      source: "import type { Session } from '../dev-mode/session/session-types';\nexport const Page = () => null;\n",
    },
    {
      name: 'dev re-export',
      source: "export { DevTool } from '../dev-mode/DevTool';\n",
    },
    {
      name: 'dynamic dev import',
      source: "import('../dev-mode/DevTool');\nexport const Page = () => null;\n",
    },
    {
      name: 'aliased dev import',
      source: "import { DevTool } from '@/dev-mode/DevTool';\nexport const Page = () => null;\n",
    },
    {
      name: 'dev css import',
      source: "import '../dev-mode/dev-module-index.css';\nexport const Page = () => null;\n",
    },
    {
      name: 'tests tree import',
      source: "import { SEED } from '../../tests/fixtures/agents/j2-seeds';\nexport const Page = () => null;\n",
    },
  ])('rejects product import of dev-only material: $name', async ({ source }) => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/pages/Page.tsx': source,
      'src/dev-mode/DevTool.tsx': DEV_TOOL,
      'src/dev-mode/session/session-types.ts': 'export type Session = { id: string };\n',
      'src/dev-mode/dev-module-index.css': '.dev-module-index {}\n',
      'tests/fixtures/agents/j2-seeds.ts': 'export const SEED = 1;\n',
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(rulesOf(violations)).toContain('dev-import');
  });

  it('rejects product imports of mock adapters and fixture data', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/App.tsx': [
        "import { createAgentChatMock } from './features/agent-chat/api/mock';",
        "import type { FixtureAgent } from './fixtures/demo';",
        'export const App = () => null;',
        '',
      ].join('\n'),
      'src/features/agent-chat/api/mock.ts': 'export const createAgentChatMock = () => ({});\n',
      'src/fixtures/demo.ts': 'export type FixtureAgent = { id: string };\n',
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(rulesOf(violations).filter(rule => rule === 'dev-import').length).toBe(2);
  });

  it.each([
    { name: 'DEV', member: 'DEV' },
    { name: 'PROD', member: 'PROD' },
  ])('rejects build-mode branch on import.meta.env.$name', async ({ member }) => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/state/selectors.ts': `export const ready = import.meta.env.${member};\n`,
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(violations.find(v => v.rule === 'env-branch')?.detail).toContain(`import.meta.env.${member}`);
  });

  it('rejects __HYPRIAL_DEV_* identifiers outside the composition root', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/components/Rail.tsx': 'export const flag = __HYPRIAL_DEV_MODULE_LABELS__;\n',
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(rulesOf(violations)).toContain('dev-token');
    expect(violations.find(v => v.rule === 'dev-token')?.detail).toContain('__HYPRIAL_DEV_MODULE_LABELS__');
  });

  it('allows dev tokens and dev material inside dev-only directories', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/dev-mode/DevTool.tsx': DEV_TOOL,
      'src/dev/DevIndex.tsx': "export { DevTool } from '../dev-mode/DevTool';\n",
      'src/fixtures/demo.ts': 'export const demo = { id: "dev-preview-frame" };\n',
      'src/features/org/dev/OrgDevApp.tsx': 'export const OrgDevApp = () => null;\n',
      'src/features/agent-chat/api/mock.ts': 'export const createAgentChatMock = () => ({});\n',
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(violations).toEqual([]);
  });

  it.each([
    {
      name: 'dev preview class',
      source: 'export const Rail = () => <div className="dev-preview-frame" />;\n',
      marker: 'dev-preview-frame',
    },
    {
      name: 'dev ui data attribute',
      source: 'export const Rail = () => <div data-dev-ui="DEV-UI-ORG-011" />;\n',
      marker: 'DEV-UI-ORG-011',
    },
    {
      name: 'devLabels attribute',
      source: 'export const Rail = ({ labels }) => <nav devLabels={labels} />;\n',
      marker: 'devLabels',
    },
    {
      name: 'dev session class literal',
      source: 'const cls = "dev-session-toolbar";\nexport const Rail = () => null;\n',
      marker: 'dev-session-toolbar',
    },
  ])('rejects dev markers in product sources: $name', async ({ source, marker }) => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/components/Rail.tsx': source,
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    const markerViolations = violations.filter(v => v.rule === 'dev-marker');
    expect(markerViolations.length).toBeGreaterThan(0);
    expect(formatViolations(markerViolations)).toContain(marker);
  });

  it('exempts the legacy org diagnostic ID namespace exactly and narrowly', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/features/org/sections/StatsSection.tsx': 'export const StatsSection = () => <div data-dev-ui="DEV-UI-ORG-011" />;\n',
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(violations).toEqual([]);
  });

  it.each([
    { name: 'outside the org scope', file: 'src/pages/StatsPage.tsx', marker: 'DEV-UI-ORG-011' },
    { name: 'lowercase variant', file: 'src/features/org/sections/StatsSection.tsx', marker: 'dev-ui-org-011' },
    { name: 'suffixed variant', file: 'src/features/org/sections/StatsSection.tsx', marker: 'DEV-UI-ORG-011-extra' },
    { name: 'short digit variant', file: 'src/features/org/sections/StatsSection.tsx', marker: 'DEV-UI-ORG-11' },
    { name: 'other namespace', file: 'src/features/org/sections/StatsSection.tsx', marker: 'DEV-UI-LINK-001' },
  ])('keeps rejecting diagnostic-shaped markers when $name', async ({ file, marker }) => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      [file]: `export const StatsSection = () => <div data-dev-ui="${marker}" />;\n`,
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    const markerViolations = violations.filter(v => v.rule === 'dev-marker');
    expect(markerViolations.length).toBeGreaterThan(0);
    expect(formatViolations(markerViolations)).toContain(marker);
  });

  it('rejects dev classes defined in product CSS', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/styles/app.css': '.dev-nav-label { display: none; }\n.app { color: red; }\n',
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(rulesOf(violations)).toContain('dev-marker');
    expect(violations.find(v => v.rule === 'dev-marker')?.file).toContain('app.css');
  });

  it('does not flag the word dev outside dev markers', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/components/Rail.tsx': 'export const deviceFrame = "device-frame";\nexport const note = "developer hint";\n',
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(violations).toEqual([]);
  });

  it('classifies dev-only modules', () => {
    expect(isDevOnlyModule('src/dev-mode/index.ts')).toBe(true);
    expect(isDevOnlyModule('src/dev/DevIndex.tsx')).toBe(true);
    expect(isDevOnlyModule('src/fixtures/demo.ts')).toBe(true);
    expect(isDevOnlyModule('src/features/org/dev/OrgDevApp.tsx')).toBe(true);
    expect(isDevOnlyModule('src/features/agent-chat/api/mock.ts')).toBe(true);
    expect(isDevOnlyModule('src/features/agent-provider/api/mock.tsx')).toBe(true);
    expect(isDevOnlyModule('src/features/chat/api/client.ts')).toBe(false);
    expect(isDevOnlyModule('src/App.tsx')).toBe(false);
  });

  it('registers the minimal explicit composition-root exceptions', () => {
    expect(Object.keys(COMPOSITION_ROOT_EXCEPTIONS)).toEqual([
      'src/main.tsx',
      'src/dev/parity-composition.tsx',
    ]);
    expect(COMPOSITION_ROOT_EXCEPTIONS['src/main.tsx'].allowTokens).toEqual([
      '__HYPRIAL_DEV_PREVIEW__',
      '__HYPRIAL_PARITY__',
    ]);
    expect(COMPOSITION_ROOT_EXCEPTIONS['src/main.tsx'].allowDevImportSpecifiers).toEqual([
      './dev/parity-composition',
      './dev/tools',
    ]);
    expect(COMPOSITION_ROOT_EXCEPTIONS['src/main.tsx'].allowDevImports).not.toBe(true);
    expect(COMPOSITION_ROOT_EXCEPTIONS['src/dev/parity-composition.tsx'].allowDevImports).toBe(true);
    expect(COMPOSITION_ROOT_EXCEPTIONS['src/dev/parity-composition.tsx'].allowTokens).toBeFalsy();
  });

  it('exercises the composition root exceptions for the parity architecture', async () => {
    const root = await treeWith({
      'src/main.tsx': [
        "declare const __HYPRIAL_PARITY__: boolean;",
        '',
        "async function renderApp(): Promise<void> {",
        "  if (__HYPRIAL_DEV_PREVIEW__) {",
        "    const { renderDevEntry } = await import('./dev/parity-composition');",
        '    void renderDevEntry;',
        '    return;',
        '  }',
        '  if (__HYPRIAL_PARITY__) {',
        "    const { renderParityEntry } = await import('./dev/parity-composition');",
        '    void renderParityEntry;',
        '    return;',
        '  }',
        '}',
        '',
      ].join('\n'),
      'src/dev/parity-composition.tsx': "import { createFixtureFirstRunActions } from '../dev-mode/first-run-capabilities';\nexport const renderDevEntry = () => null;\nexport const renderParityEntry = () => null;\nvoid createFixtureFirstRunActions;\n",
      'src/dev-mode/first-run-capabilities.ts': 'export const createFixtureFirstRunActions = () => ({});\n',
    });
    const { violations, exceptionsUsed } = await analyzeTree(join(root, 'src'));
    expect(violations).toEqual([]);
    expect(exceptionsUsed).toContain('src/main.tsx');
    expect(exceptionsUsed).toContain('src/dev/parity-composition.tsx');
  });

  it('lets the formal entry load only the tool layer entry besides the parity composition', async () => {
    const entry = (specifier) => [
      'async function renderApp(): Promise<void> {',
      `  const { mountDevTools } = await import('${specifier}');`,
      '  void mountDevTools;',
      '}',
      '',
    ].join('\n');
    const files = (specifier) => ({
      'src/main.tsx': entry(specifier),
      'src/dev/parity-composition.tsx': 'export const renderDevEntry = () => null;\n',
      'src/dev/tools/index.ts': 'export const mountDevTools = () => null;\n',
      'src/dev/tools/impl/mount.tsx': 'export const mountDevTools = () => null;\n',
    });
    const allowed = await analyzeTree(join(await treeWith(files('./dev/tools')), 'src'));
    expect(allowed.violations.filter(v => v.file === 'src/main.tsx' && v.rule === 'dev-import')).toEqual([]);
    const refused = await analyzeTree(join(await treeWith(files('./dev/tools/impl/mount')), 'src'));
    expect(refused.violations.filter(v => v.rule === 'dev-import' && v.file === 'src/main.tsx').length).toBeGreaterThan(0);
  });

  it('keeps old main-only dev imports red', async () => {
    const root = await treeWith({
      'src/main.tsx': [
        "async function renderApp(): Promise<void> {",
        "  if (__HYPRIAL_DEV_PREVIEW__) {",
        "    const { DevPreviewApp } = await import('./dev-mode/DevPreviewApp');",
        '    void DevPreviewApp;',
        '    return;',
        '  }',
        '}',
        '',
      ].join('\n'),
      'src/dev/parity-composition.tsx': "export const renderDevEntry = () => null;\n",
      'src/dev-mode/DevPreviewApp.tsx': DEV_TOOL,
    });
    const { violations } = await analyzeTree(join(root, 'src'));
    const importViolations = violations.filter(v => v.rule === 'dev-import' && v.file === 'src/main.tsx');
    expect(importViolations.length).toBeGreaterThan(0);
  });

  it('fails closed when the registered parity composition root is missing', async () => {
    const root = await treeWith({
      'src/main.tsx': "declare const __HYPRIAL_PARITY__: boolean;\nexport const boot = __HYPRIAL_PARITY__;\n",
    });
    const { violations } = await analyzeTree(join(root, 'src'));
    const missing = violations.filter(v => v.rule === 'stale-exception' && v.file === 'src/dev/parity-composition.tsx');
    expect(missing.length).toBeGreaterThan(0);
    expect(formatViolations(missing)).toContain('does not exist');
  });

  it('rejects the parity token in product components outside the composition root', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/components/Rail.tsx': 'export const parity = __HYPRIAL_PARITY__;\n',
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(rulesOf(violations)).toContain('dev-token');
    expect(formatViolations(violations)).toContain('__HYPRIAL_PARITY__');
  });

  it('allows ambient dev and parity declarations in d.ts files', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/vite-env.d.ts': [
        'declare global {',
        '  const __HYPRIAL_DEV_PREVIEW__: boolean;',
        '  const __HYPRIAL_PARITY__: boolean;',
        '}',
        'export {};',
        '',
      ].join('\n'),
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(violations).toEqual([]);
  });

  it('rejects the legacy dev token inside the composition root exception', async () => {
    const root = await treeWith({
      'src/main.tsx': "export const preview = __HYPRIAL_DEV_PREVIEW__;\n",
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(rulesOf(violations)).toContain('dev-token');
    expect(formatViolations(violations)).toContain('__HYPRIAL_DEV_PREVIEW__');
  });

  it('fails when a registered exception token is no longer used', async () => {
    const root = await treeWith({ 'src/main.tsx': ENTRY });
    const { violations } = await analyzeTree(join(root, 'src'));
    expect(rulesOf(violations)).toContain('stale-exception');
    expect(formatViolations(violations)).toContain('__HYPRIAL_PARITY__');
  });

  it('fails when a registered exception path is missing', async () => {
    const root = await treeWith({
      'src/main.tsx': "export const boot = __HYPRIAL_DEV_PREVIEW__;\n",
      'src/legacy.tsx': 'export const legacy = 1;\n',
    });
    const exceptions = {
      ...COMPOSITION_ROOT_EXCEPTIONS,
      'src/legacy.tsx': { allowTokens: ['__HYPRIAL_DEV_PREVIEW__'], allowDevImports: false },
    };
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions });
    expect(formatViolations(violations)).toContain('src/legacy.tsx');
  });

  it('rejects dev tokens in files that have no registered exception', async () => {
    const root = await treeWith({
      'src/main.tsx': ENTRY,
      'src/AppShell.tsx': 'export const preview = __HYPRIAL_DEV_PREVIEW__;\n',
    });
    const { violations } = await analyzeTree(join(root, 'src'), { exceptions: NO_EXCEPTIONS });
    expect(rulesOf(violations)).toContain('dev-token');
    expect(violations.find(v => v.rule === 'dev-token')?.file).toBe('src/AppShell.tsx');
  });

  it('keeps the real product tree free of dev/product parity violations', async () => {
    const { violations, files, exceptionsUsed } = await analyzeTree(resolve(process.cwd(), 'src'));
    expect(files.length).toBeGreaterThan(50);
    expect(exceptionsUsed).toContain('src/main.tsx');
    expect(formatViolations(violations)).toBe('');
  });
});
