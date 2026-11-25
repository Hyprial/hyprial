// PR-E 手机底部主导航：结构守卫（static）+ 真实浏览器测量。
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const read = (path: string) => readFileSync(resolve(process.cwd(), path), 'utf8');
const appCss = read('src/styles/app.css');
const railFrame = read('src/components/RailFrame.tsx');

const media640 = appCss.slice(appCss.lastIndexOf('@media', appCss.indexOf('.rail .nav {')));

describe('mobile bottom nav static guard', () => {
  it('keeps DOM order brand → nav → footer (no visual reorder of tabs)', () => {
    const brand = railFrame.indexOf('className="brand"');
    const nav = railFrame.indexOf('className="nav"');
    const footer = railFrame.indexOf('{footer}');
    expect(brand).toBeGreaterThan(-1);
    expect(nav).toBeGreaterThan(brand);
    expect(footer).toBeGreaterThan(nav);
  });

  it('fixes the nav to the bottom only at the ≤640 phone breakpoint', () => {
    expect(media640).not.toBe('');
    expect(media640).toContain('@media (max-width: 640px)');
    expect(media640).toContain('.rail .nav {');
    expect(media640).toContain('position: fixed;');
    expect(media640).toContain('bottom: 0;');
    expect(media640).toContain('env(safe-area-inset-bottom)');
  });

  it('never lets a rail blur rule reach the fixed-nav phone width', () => {
    const mediaBlocks = [
      ...appCss.matchAll(/@media[^{]+\{(?:[^{}]|\{[^{}]*\})*\}/g),
    ].map((match) => match[0]);
    const phoneBlocks = mediaBlocks.filter((block) =>
      /max-width:\s*640px/.test(block),
    );
    expect(phoneBlocks.length).toBeGreaterThan(0);
    for (const block of phoneBlocks) {
      expect(block).not.toMatch(/\.rail\s*\{[^}]*backdrop-filter/);
    }
    const railBlurBlocks = mediaBlocks.filter((block) =>
      /\.rail\s*\{[^}]*backdrop-filter:\s*blur/.test(block),
    );
    for (const block of railBlurBlocks) {
      expect(block).toMatch(/min-width:\s*641px/);
    }
  });

  it('reserves content space only for pages that render the rail', () => {
    expect(media640).toContain('.shell .content {');
    expect(media640).toContain('padding-bottom: calc(56px + env(safe-area-inset-bottom));');
    // first-run 不渲染 .shell，因此不会命中预留规则
    expect(media640).not.toMatch(/\.app-first-run[^{]*\{[^}]*padding-bottom/);
  });

  it('uses the 72px icon rail from 641px up and a brand/foot top bar on phones', () => {
    expect(appCss).toMatch(/grid-template-columns:\s*72px\s+minmax\(0,\s*1fr\)/);
    expect(appCss).toMatch(/grid-template-areas:\s*"brand foot"/);
    expect(appCss).not.toContain('"nav nav"');
  });
});

// PR-E FIX-002：开始页推荐卡 v13 绿色底与绿线（限定选择器，避免被 ui-card/Tailwind 覆盖）。
describe('start page recommendation card colours', () => {
  it('paints the recommendation card with the v13 green surface and line', () => {
    const rule = appCss.match(/\.page-start \.recommendation-card\.ui-card \{([^}]*)\}/s);
    expect(rule).not.toBeNull();
    expect(rule?.[1]).toContain('background: var(--ui-lime-soft);');
    expect(rule?.[1]).not.toMatch(/border/);
  });

  it('takes the v13 button colours from the shared Button contract', () => {
    // BUTTON-004：主/次按钮配色上移到共享 Button（页面覆盖已删除）。
    const buttonCss = read('src/components/ui/button.tsx');
    const tokens = read('src/styles/tokens.css');
    expect(buttonCss).toContain('tw:bg-[var(--ui-accent)]');
    expect(buttonCss).toContain('tw:text-[var(--ui-on-accent)]');
    expect(buttonCss).toContain('tw:rounded-[var(--ui-radius-pill)]');
    expect(buttonCss).toContain('tw:bg-[var(--ui-neutral-fill)]');
    expect(tokens).not.toContain('--btn-');

    // 页面不得再重复定义同一批按钮覆盖。
    expect(appCss).not.toMatch(/\.page-start \.ui-button--primary \{/);
    expect(appCss).not.toMatch(/\.ui-button--primary \{/m);
  });

  it('keeps the v13 green tokens at their approved values', () => {
    const tokens = read('src/styles/tokens.css');
    const theme = read('src/styles/themes/hyprial-v5.css');
    expect(theme).toContain('--accent-soft: #dff493;');
    expect(tokens).toContain('--ui-lime-soft: var(--accent-soft);');
  });

  it('does not repaint other cards or the global card surface', () => {
    const rule = appCss.match(/\.page-start \.recommendation-card\.ui-card \{([^}]*)\}/s)?.[1] ?? '';
    expect(rule).not.toMatch(/^\.ui-card/m);
    const genericCard = appCss.match(/^\.ui-card[^{]*\{([^}]*)\}/m)?.[1] ?? '';
    expect(genericCard).not.toContain('--ui-lime');
  });
});
