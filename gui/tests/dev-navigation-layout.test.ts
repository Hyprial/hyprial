import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const css = readFileSync(
  resolve(process.cwd(), 'src/styles/app.css'),
  'utf8',
);

function rule(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = source.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`, 's'));
  expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
  return match?.[1] ?? '';
}

describe('navigation label layout', () => {
  it('keeps the 72px icon rail and clips long entry names', () => {
    expect(rule(css, '.shell')).toMatch(
      /grid-template-columns\s*:\s*72px\s+minmax\(0,\s*1fr\)/i,
    );
    expect(rule(css, '.nav-item')).toMatch(/display\s*:\s*flex/i);
    expect(rule(css, '.nav-item')).toMatch(/min-width\s*:\s*0/i);
    expect(rule(css, '.nav-item-label')).toMatch(/overflow\s*:\s*hidden/i);
  });

  it('stacks 56px icon-over-label entries vertically from 641px up', () => {
    expect(rule(css, '.nav')).toMatch(/flex-direction\s*:\s*column/i);
    expect(rule(css, '.nav-item')).toMatch(/flex-direction\s*:\s*column/i);
    expect(rule(css, '.nav-item')).toMatch(/width\s*:\s*56px/i);
    expect(rule(css, '.nav-item')).toMatch(/height\s*:\s*56px/i);
    expect(css).not.toMatch(/@media\s*\(min-width:\s*760px\)\s*and\s*\(max-width:\s*979px\)/i);
  });
});
