// PR-F FIX-004：正式 bootstrap 失败页的文案与视觉契约（源码层守卫）。
// 运行时证据由 production dist 冒烟脚本提供；这里防止文案/视觉被静默改回。
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';

const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');
const main = read('../src/main.tsx');
const css = read('../src/styles/bootstrap-unavailable.css');

describe('bootstrap unavailable page copy', () => {
  it('renders the owner-approved title, body, primary action and problem code', () => {
    expect(main).toContain('这个页面不能直接打开');
    expect(main).toContain(
      '请从 Hyprial 桌面应用或启动器打开界面，以连接本机服务。打开后，如果此页面仍未恢复，请点击“重新检查”。',
    );
    expect(main).toContain('重新检查');
    expect(main).toContain('问题编号：');
    expect(main).toContain('alt="Hyprial"');
  });

  it('keeps the previous copy and obsolete styling hooks out', () => {
    expect(main).not.toContain('需要可信启动入口');
    expect(main).not.toContain('重新检查会话');
    expect(main).not.toContain('这里不会回退到示例数据');
    expect(main).not.toContain('button button-secondary');
  });

  it('keeps the bootstrap decision and DCE/host logic untouched', () => {
    expect(main).toContain("error instanceof BrowserBootstrapError ? error.code : 'BOOTSTRAP_FAILED'");
    expect(main).toContain('if (__HYPRIAL_DEV_PREVIEW__)');
    expect(main).toContain('await connectBrowserBackend()');
    // 失败页不得引入 fixture / demo 分支
    expect(main).not.toMatch(/import\.meta\.env\.DEV/);
  });
});

describe('bootstrap unavailable page visuals', () => {
  it('uses the v13 paper shell, card and type scale', () => {
    expect(css).toContain('background: var(--ui-ground);');
    expect(css).toContain('background: var(--ui-surface);');
    expect(css).not.toContain('border: 1px solid');
    expect(css).toContain('border-radius: var(--ui-radius-lg);');
    expect(css).toContain('padding: var(--ui-space-8);');
    expect(css).toContain('font-size: var(--ui-text-3xl);');
    expect(css).toContain('color: var(--ui-ink-2);');
    expect(css).toContain('font-size: var(--ui-text-xs);');
  });

  it('paints the primary action as the v13 ink pill without blue', () => {
    // BUTTON-008：主按钮配色/圆角/焦点由共享 Button 提供，页面 CSS 不再定义。
    expect(main).toContain('variant="primary"');
    expect(css).not.toContain('bootstrap-card__primary');
    expect(css).not.toMatch(/background:\s*#1a1620/);
    expect(css).not.toMatch(/border-radius:\s*999px/);
    expect(css).not.toMatch(/#[0-9a-f]{0,2}(00[0-9a-f]{0,2}ff|0048a8|1d4ed8|2563eb|3b82f6)/i);
    expect(css).not.toMatch(/\bblue\b/i);
  });

  it('is scoped to the bootstrap page only', () => {
    const declared = css.replace(/\/\*[\s\S]*?\*\//g, '');
    const selectors = [...declared.matchAll(/(?:^|[{}])([^{}@]+)\{/g)]
      .map((match) => match[1].trim())
      .filter(Boolean);
    expect(selectors.length).toBeGreaterThan(0);
    for (const selector of selectors) {
      expect(/bootstrap-page|bootstrap-card|bootstrap-primary-layout/.test(selector)).toBe(true);
    }
  });
});
