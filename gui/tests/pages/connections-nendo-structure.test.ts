// D-NENDO-007：Connections 结构契约（所有一级模块共用 ModuleSection，单列模块流）。
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';

const read = (relative: string) => readFileSync(new URL(relative, import.meta.url), 'utf8');
const page = read('../../src/pages/ConnectionsPage.tsx');
const credentials = read('../../src/features/connections/CredentialsSection.tsx');
const platform = read('../../src/features/connections/PlatformIntegrationsSection.tsx');
const css = read('../../src/styles/app.css');

describe('connections module flow', () => {
  it('renders every first-level module through the shared ModuleSection', () => {
    expect(page).toContain('connections-flow');
    expect(page).not.toContain('connections-grid');
    for (const section of [credentials, platform]) {
      expect(section).toContain('ModuleSection');
    }
    expect((credentials.match(/<ModuleSection/g) ?? []).length).toBe(1);
    expect((platform.match(/<ModuleSection/g) ?? []).length).toBe(1);
  });

  it('ships ModuleSection from shared primitives as a single-surface section', () => {
    const primitives = read('../../src/components/primitives/index.ts');
    const module = read('../../src/components/primitives/ModuleSection.tsx');
    expect(primitives).toContain('ModuleSection');
    expect(module).toContain('as="section"');
    expect(module).toContain('data-module=""');
    expect(module).toContain('actions?: ReactNode');
    expect(module).not.toContain('className="ui-module"');
    // 不允许外层 section 再套 Card
    expect(module).not.toMatch(/<section[\s\S]*<Card/);
  });

  it('keeps module headings inside the surface instead of outside the card', () => {
    expect(page).not.toContain('className="section-heading"');
    expect(credentials).not.toContain('section-heading');
    expect(platform).not.toContain('section-heading');
  });

  it('defines one shared left baseline and width rule for modules', () => {
    const flow = css.match(/\.connections-flow \{([^}]*)\}/s)?.[1] ?? '';
    expect(flow).toContain('grid-template-columns: minmax(0, 1fr);');
    const module = css.match(/\n\.ui-module \{([^}]*)\}/s)?.[1] ?? '';
    expect(module).toContain('width: 100%;');
    expect(module).toContain('max-width: 100%;');
    expect(css).not.toMatch(/\.ui-module:nth-child/);
    expect(css).not.toMatch(/\.connections-flow[^}]*position: absolute/);
  });
});
