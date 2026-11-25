// BUTTON-005 attempt3：产品代码不得再消费 legacy action class（静态合同，无 allowlist）。
import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';

const root = resolve(process.cwd(), 'src');
const walk = (dir: string): string[] =>
  readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : full.endsWith('.tsx') ? [full] : [];
  });

describe('legacy action classes', () => {
  it('are not consumed by any product TSX', () => {
    const offenders = walk(root).flatMap((file) => {
      const text = readFileSync(file, 'utf8');
      return /className="[^"]*\bbutton-(strong|secondary)\b/.test(text) ||
        /className="button"(?!\s*\/)/.test(text)
        ? [file.replace(`${root}/`, 'src/')]
        : [];
    });
    expect(offenders).toEqual([]);
  });

  it('keeps action semantics on the shared Button (variant mapping)', () => {
    const primitive = readFileSync(resolve(process.cwd(), 'src/components/primitives/Button.tsx'), 'utf8');
    expect(primitive).toContain("primary: 'default'");
    expect(primitive).toContain("secondary: 'outline'");
    expect(primitive).toContain('forwardRef<HTMLButtonElement, ButtonProps>');
  });
});
