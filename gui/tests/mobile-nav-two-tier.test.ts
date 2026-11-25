import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

const css = readFileSync(resolve(import.meta.dirname, '../src/styles/app.css'), 'utf8');

describe('mobile two-tier navigation contract', () => {
  const block = css.slice(css.indexOf('@media (max-width: 640px) {\n  .shell {'));

  it('uses a brand/foot top bar with a fixed bottom tab bar', () => {
    expect(block).toContain('grid-template-areas: "brand foot";');
    expect(block).not.toContain('"nav nav"');
    expect(block).toContain('.nav-separator {\n    display: none;');
    expect(block).toContain('.rail-spacer {\n    display: none;');
    expect(block).toContain('position: fixed;');
  });

  it('keeps six equally sized tabs of at least 56px and the graphic mark on the ground', () => {
    expect(block).toContain('flex: 1 1 0;');
    expect(block).toContain('min-height: 56px');
    expect(block).toContain('.brand-mark--on-ground {\n    display: block;');
    expect(block).toContain('.brand-mark--on-rail {\n    display: none;');
    expect(block).toContain('.rail .notif-panel {');
    expect(block).toContain('width: min(360px, calc(100vw - 24px));');
  });
});
