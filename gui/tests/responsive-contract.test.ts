import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const appCss = readFileSync(
  resolve(process.cwd(), 'src/styles/app.css'),
  'utf8',
);
const tokensCss = readFileSync(
  resolve(process.cwd(), 'src/styles/tokens.css'),
  'utf8',
);

describe('responsive layout contract', () => {
  const css = `${tokensCss}\n${appCss}`;

  it('does not inherit the prototype 980px desktop minimum', () => {
    expect(css).not.toMatch(/min-width\s*:\s*980px/i);
    expect(css).toMatch(/min-width\s*:\s*0;/);
  });

  it('uses a white viewport without an outer colour frame', () => {
    expect(tokensCss).not.toContain('radial-gradient(circle at 18% 8%, #e8f3ff 0, transparent 35%)');
    expect(tokensCss).not.toContain('linear-gradient(135deg, #cbd9ef, #e9e1ef)');
    expect(tokensCss).toMatch(/body\s*\{[^}]*background:\s*var\(--ui-ground\);/s);
    expect(tokensCss).toContain('--ui-ground: var(--background);');
    expect(tokensCss).toContain('--ui-ink-1: var(--foreground);');
    expect(tokensCss).toContain('--ui-ink-3: var(--ink-3);');
    expect(tokensCss).toContain('--ui-border: var(--border);');
  });

  it('keeps the app frame fixed and scrolls inside the content area', () => {
    expect(appCss).toMatch(/\.app\s*\{[^}]*height:\s*100dvh;[^}]*overflow:\s*hidden;/s);
    expect(appCss).toMatch(/\.shell\s*\{[^}]*height:\s*100%;/s);
    expect(appCss).toMatch(/\.main\s*\{[^}]*min-height:\s*0;[^}]*overflow:\s*hidden;/s);
    expect(appCss).toMatch(/\.content\s*\{[^}]*overflow-y:\s*auto;/s);
  });

  it('lets chat fill the full workspace beside navigation', () => {
    expect(appCss).toMatch(/\.chat-layout\s*\{[^}]*height:\s*100%;[^}]*margin:\s*0;[^}]*border:\s*0;/s);
    expect(appCss).toMatch(/\.page-chat\s*\{[^}]*height:\s*100%;/s);
  });

  it('declares the prototype brand font family without claiming a bundled asset', () => {
    expect(tokensCss).toContain('--ui-font-brand: var(--font-sans);');
    expect(tokensCss).toContain('--ui-font-ui: var(--font-sans);');
    expect(appCss).toMatch(/\.brand-mark\s*\{[^}]*width:\s*var\(--ui-space-8\);/s);
    expect(appCss).not.toContain('brand-wordmark');
    expect(appCss).toMatch(
      /\.page-title\s*\{[^}]*font-family:\s*var\(--ui-font-ui\);[^}]*font-size:\s*var\(--ui-text-3xl\);[^}]*font-weight:\s*600;/s,
    );
    expect(appCss).not.toContain('@font-face');
  });

  it('defines the desktop and intermediate chat column contracts', () => {
    expect(css).toMatch(
      /grid-template-columns:\s*280px\s+minmax\(0,\s*1fr\)\s+300px/,
    );
    expect(css).toMatch(/@media \(max-width: 1359px\)/);
    expect(css).toMatch(
      /248px\s+minmax\(0,\s*1fr\)\s+272px/,
    );
  });

  it('reflows below 980px and accounts for mobile safe areas', () => {
    expect(css).toMatch(/@media \(max-width: 979px\)/);
    expect(css).toContain('env(safe-area-inset-bottom)');
    expect(css).toContain('100dvh');
    expect(css).toMatch(
      /@media \(max-width: 979px\)[\s\S]*\.chat-layout,[\s\S]*overflow-y:\s*auto;/,
    );
  });

  it('stabilizes scrolling and respects reduced-motion preferences', () => {
    expect(css).toMatch(/overflow-anchor:\s*auto/);
    expect(css).toMatch(/scrollbar-gutter:\s*stable/);
    expect(css).toMatch(/@media \(prefers-reduced-motion: reduce\)/);
  });
});
