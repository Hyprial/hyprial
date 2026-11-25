// BUTTON-004：共享 Button 的 v13 胶囊契约（无蓝、ink 系 primary、outlined danger）。
import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { render, screen } from '@testing-library/react';
import { Button } from '../../src/components/primitives';

const read = (path: string) => readFileSync(resolve(process.cwd(), path), 'utf8');
const css = read('src/components/ui/button.tsx');
const tokens = read('src/styles/tokens.css');
const primitivesCss = read('src/styles/primitives.css');

describe('shared Button v13 contract', () => {
  it('keeps every variant pill-shaped and free of blue tokens', () => {
    const variants = ['primary', 'secondary', 'ghost', 'text', 'danger'] as const;
    for (const variant of variants) {
      render(<Button variant={variant}>{variant}</Button>);
      const button = screen.getByRole('button', { name: variant });
      expect(button.className).toContain('tw:rounded-[var(--ui-radius-pill)]');
      expect(button.className).toContain(`ui-button--${variant}`);
      // 组件源码不得再使用 shadcn 蓝色 primary/accent token
      expect(button.className).not.toContain('bg-primary');
      expect(button.className).not.toContain('bg-accent');
    }
    expect(css).not.toMatch(/tw:bg-primary|tw:bg-accent|tw:border-primary|tw:text-primary\b/);
    expect(tokens).not.toContain('--btn-');
    expect(tokens).not.toMatch(/#[0-9a-fA-F]{3,8}\b|rgba?\(/);
    expect(css).not.toContain('--btn-');
    expect(primitivesCss).not.toContain('--btn-');
  });

  it('paints primary with ink and keeps danger on a wash', () => {
    expect(css).toContain('tw:bg-[var(--ui-accent)]');
    expect(css).toContain('tw:text-[var(--ui-on-accent)]');
    expect(css).toContain('tw:enabled:hover:bg-[var(--ui-accent-hover)]');
    expect(css).toContain('tw:text-[var(--ui-danger-ink)]');
    expect(css).toContain('tw:bg-[var(--ui-danger-wash)]');
    expect(css).toContain('tw:enabled:hover:bg-[var(--ui-danger-wash-hover)]');
    expect(css).toContain('tw:focus-visible:outline-[var(--ui-focus)]');
  });

  it('uses prototype-exact tokens in the shared primitives CSS', () => {
    expect(primitivesCss).toContain('.ui-button--primary {');
    expect(primitivesCss).toContain('background: var(--ui-accent);');
    expect(primitivesCss).toContain('background: var(--ui-neutral-fill);');
    expect(primitivesCss).toContain('.ui-button--danger {');
    expect(primitivesCss).toContain('color: var(--ui-danger-ink);');
    expect(primitivesCss).toContain('border-radius: var(--ui-radius-pill);');
    // 仅按钮变体块不得引用 accent/blue；其他 primitives（selection/info）仍可用 accent。
    const buttonBlock =
      primitivesCss.match(/\.ui-button--primary \{[\s\S]*?\.ui-button:disabled \{[^}]*\}/)?.[0] ?? '';
    expect(buttonBlock.length).toBeGreaterThan(0);
    expect(buttonBlock).not.toMatch(/#[0-9a-fA-F]{3,8}\b|--ui-blue/);
  });

  it('consumes every v13 button token (no dead declarations)', () => {
    expect(css).toContain('tw:text-[var(--ui-ink-1)]');
    expect(css).toContain('tw:bg-[var(--ui-neutral-fill)]');
    expect(primitivesCss).toContain('color: var(--ui-ink-1);');
    expect(primitivesCss).toContain('background: var(--ui-danger-wash);');
    expect(primitivesCss).toContain('[data-slot="button"][data-variant="default"]');
    expect(primitivesCss).toContain('[data-slot="button"][data-variant="destructive"]');
    expect(primitivesCss).toContain('[data-slot="button"][data-variant="secondary"]');
    expect(primitivesCss).toContain('[data-slot="button"][data-variant="outline"]');
    expect(primitivesCss).toContain('[data-slot="button"][data-variant="ghost"]');
    expect(primitivesCss).toContain('[data-slot="button"][data-variant="link"]');
  });

  it('routes every button focus ring through --ui-focus', () => {
    const focusBlock = primitivesCss.match(/\[data-slot="button"\]:focus-visible,\s*\.ui-button:focus-visible \{[^}]*\}/)?.[0] ?? '';
    expect(focusBlock.length).toBeGreaterThan(0);
    expect(focusBlock).toContain('outline: 3px solid var(--ui-focus) !important;');
    expect(focusBlock).toContain('outline-offset: 2px !important;');
    // 不允许残留把 focus 环写成 danger 红/蓝色的旧规则
    expect(primitivesCss).not.toContain('outline-color: var(--ui-danger');
    expect(primitivesCss).not.toContain('outline: 2px solid var(--ui-danger');
  });

  it('does not repaint whole-app state colours', () => {
    const appCss = read('src/styles/app.css');
    expect(appCss).not.toMatch(/\.page-start \.ui-button--primary \{/);
    expect(appCss).not.toMatch(/\.lark-binding \.ui-button--danger \{/);
    // --ui-accent 现为主题近黑 primary，不再是蓝色
    expect(tokens).toContain('--ui-accent: var(--primary);');
  });
});
