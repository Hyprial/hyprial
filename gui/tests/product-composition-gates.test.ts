import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

function source(relativePath: string): string {
  return readFileSync(
    resolve(process.cwd(), 'src', relativePath),
    'utf8',
  );
}

const PRODUCT_FILES = [
  'App.tsx',
  'app/AppShell.tsx',
  'app/capabilities.tsx',
  'components/Rail.tsx',
  'components/RailFrame.tsx',
  'state/appReducer.ts',
  'state/types.ts',
  'state/selectors.ts',
  'state/agent-selectors.ts',
  'state/gui-view-model.ts',
  'state/messages.ts',
  'state/operation-lifecycle.ts',
] as const;

const STATE_FILES = [
  'state/appReducer.ts',
  'state/types.ts',
  'state/selectors.ts',
  'state/agent-selectors.ts',
] as const;

describe('product composition source gates', () => {
  it('keeps product components and state free of build-mode and dev-only branches', () => {
    for (const file of PRODUCT_FILES) {
      const text = source(file);
      expect(text, `${file} must not read import.meta.env.DEV`).not.toMatch(
        /import\.meta\.env\.DEV/,
      );
      expect(text, `${file} must not read __HYPRIAL_DEV_*`).not.toMatch(
        /__HYPRIAL_DEV/,
      );
      expect(text, `${file} must not carry dev module labels`).not.toMatch(
        /devLabels|DevModuleId/,
      );
    }
  });

  it('keeps the state graph free of fixture-data imports', () => {
    for (const file of STATE_FILES) {
      expect(source(file), `${file} must not import fixtures`).not.toMatch(
        /from '\.\.\/fixtures\//,
      );
    }
  });

  it('keeps the fixture URL out of the product sources', () => {
    for (const file of [...PRODUCT_FILES, 'main.tsx', 'dev/parity-composition.tsx'] as readonly string[]) {
      expect(source(file), `${file} must not carry the retired fixture URL`).not.toMatch(
        /hyprialFixture/,
      );
    }
    for (const file of ['App.tsx', 'app/AppShell.tsx'] as readonly string[]) {
      expect(source(file), `${file} must carry the isolated first-run scenario, not the retired demo value`).not.toMatch(
        /'demo'/,
      );
    }
  });

  it('keeps the scenario URL contract out of the formal entry', () => {
    const entry = source('main.tsx');
    expect(entry, 'the formal entry must not carry mock URL strings').not.toMatch(
      /hyprialScenario|hyprialFirstRun/,
    );
    expect(source('dev/parity-composition.tsx'), 'the parity composition owns the scenario URL contract').toMatch(
      /hyprialScenario/,
    );
  });

  it('mounts one App at the entry and keeps the preview tool out of #root', () => {
    const entry = source('main.tsx');
    expect(entry).toMatch(/from '\.\/App'/);
    const composition = source('dev/parity-composition.tsx');
    expect(
      composition,
      'the dev preview must not be rendered into #root',
    ).not.toMatch(/createRoot\(root\)\.render\(\s*<StrictMode>\s*<DevPreviewApp/);
    expect(source('main.tsx'), 'fixture data must not be imported statically').not.toMatch(
      /from '\.\/fixtures\//,
    );
    expect(
      entry,
      'the formal entry must not import dev modules statically',
    ).not.toMatch(/import(?! type\b)[^;]*from '\.\/(dev-mode|fixtures)\//);
    expect(entry).toMatch(/await import\('\.\/dev\/parity-composition'\)/);
    expect(
      source('App.tsx'),
      'the product App must not import dev or fixture implementations',
    ).not.toMatch(/import(?! type\b)[^;]*from ['"](?:\.\.?\/)*(?:dev|dev-mode|fixtures)\//);
  });

  it('keeps the rail frames free of dev label markup', () => {
    for (const file of ['components/Rail.tsx', 'components/RailFrame.tsx']) {
      expect(source(file), `${file} must not render dev labels`).not.toMatch(
        /dev-nav-label|data-dev-label/,
      );
    }
  });
});
