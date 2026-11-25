// BUTTON-006：结构化（TSX AST）合同：legacy token 不得以任何写法被产品消费；裸 action 必须迁移。
import { describe, expect, it } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, resolve } from 'node:path';
import ts from 'typescript';

const FORBIDDEN = ['button-strong', 'button-secondary', 'text-button'];

interface Finding { file: string; line: number; reason: string }

function scanSource(fileName: string, text: string): Finding[] {
  const source = ts.createSourceFile(fileName, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const findings: Finding[] = [];
  const lineOf = (node: ts.Node) => source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1;
  const mentionsForbidden = (node: ts.Node): string | null => {
    const raw = node.getText(source);
    const hit = FORBIDDEN.find((token) => raw.includes(token));
    return hit ?? null;
  };

  const visit = (node: ts.Node) => {
    if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
      const tag = node.tagName.getText(source);
      const attrs = node.attributes.properties;
      const attrByName = (name: string) => attrs.find(
        (a): a is ts.JsxAttribute => ts.isJsxAttribute(a) && a.name.getText(source) === name,
      );
      const classAttr = attrByName('className');
      if (classAttr?.initializer) {
        const hit = mentionsForbidden(classAttr.initializer);
        if (hit) findings.push({ file: fileName, line: lineOf(node), reason: `className consumes ${hit}` });
      }
      if (tag === 'button') {
        const staticString = (attr?: ts.JsxAttribute): string | null => {
          if (!attr?.initializer) return null;
          if (ts.isStringLiteral(attr.initializer)) return attr.initializer.text;
          return null;
        };
        const classTokens = (): string[] | null => {
          if (!classAttr?.initializer) return [];
          const init = classAttr.initializer;
          if (ts.isStringLiteral(init)) return init.text.split(/\s+/).filter(Boolean);
          if (ts.isJsxExpression(init) && init.expression) {
            const expr = init.expression;
            if (ts.isConditionalExpression(expr)) {
              const branch = (node2: ts.Expression) =>
                ts.isStringLiteral(node2) ? node2.text.split(/\s+/).filter(Boolean) : null;
              const whenTrue = branch(expr.whenTrue);
              const whenFalse = branch(expr.whenFalse);
              if (whenTrue && whenFalse) return [...whenTrue, ...whenFalse];
              return null;
            }
            if (ts.isStringLiteral(expr)) return expr.text.split(/\s+/).filter(Boolean);
            if (ts.isNoSubstitutionTemplateLiteral(expr)) return expr.text.split(/\s+/).filter(Boolean);
            // 模板表达式：只接受「静态段都能解析为结构 token」的情形
            if (ts.isTemplateExpression(expr)) {
              // 动态模板：只有「第一个表达式之前的静态 head」含结构 base token 才允许；
              // 后续 span 只当作修饰（例如 chat-list-item${active ? '--selected' : ''}）。
              const headTokens = expr.head.text.split(/\s+/).filter(Boolean);
              if (headTokens.length === 0) return null;
              return headTokens;
            }
          }
          return null; // 无法静态求值 → 视为不合格
        };
        const ROLE_ALLOW = new Set(['menuitem', 'menuitemradio', 'menuitemcheckbox', 'tab', 'option', 'switch', 'checkbox', 'radio']);
        const STRUCTURAL = new Set([
          'nav-item', 'filter-chip', 'chat-list-item', 'contact-card', 'agent-row', 'org-row__main',
          'org-count-card', 'org-side-panel__back',
          'notice-close', 'notif-bell', 'toast-close', 'ui-dialog__close', 'ui-notice__dismiss',
          'account-trigger', 'dev-session-button', 'dev-tools-fab', 'dev-tools-scenario-preset', 'disclosure-trigger',
          'icon-button', 'ui-infotip__button',
          // 精确到独立控件 token（禁止子串匹配）
          'secret-field-inline__reveal', 'chat-workspace__info-toggle', 'chat-workspace__target',
          'chat-list-avatar', 'topology-edge', 'topology-node', 'org-switcher__button',
          'org-live__dismiss', 'file-row', 'org-view-switch', 'topology-view-tab',
          'org-tree__folder', 'org-tree__file', 'org-graph__node', 'org-graph__edge', 'task-tab',
        ]);
        const role = staticString(attrByName('role'));
        const tokens = classTokens();
        const hasStructuralToken = tokens !== null && tokens.some((t) => STRUCTURAL.has(t));
        const pressed = attrByName('aria-pressed');
        const selected = attrByName('aria-selected');
        const expanded = attrByName('aria-expanded');
        const controls = attrByName('aria-controls');
        const roleAllowed = role !== null && ROLE_ALLOW.has(role);
        // aria-pressed/selected 仅在精确结构 token 或精确 role 下允许
        const stateAllowed = (!!pressed || !!selected) && (hasStructuralToken || roleAllowed);
        // aria-expanded 需 aria-controls + 精确 trigger/disclosure 结构 token
        const expandedAllowed = !!expanded && !!controls && tokens !== null &&
          tokens.some((t) => ['disclosure-trigger', 'menu-item', 'nav-item', 'tab', 'list-row'].includes(t));
        const allowed = roleAllowed || hasStructuralToken || stateAllowed || expandedAllowed;
        if (!allowed) {
          findings.push({ file: fileName, line: lineOf(node), reason: 'raw <button> lacks static structural semantics' });
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return findings;
}

const root = resolve(process.cwd(), 'src');
const walk = (dir: string): string[] =>
  readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    return statSync(full).isDirectory() ? walk(full) : full.endsWith('.tsx') ? [full] : [];
  });

describe('button AST contract', () => {
  it('product TSX consumes no legacy action token in any form', () => {
    const findings = walk(root).flatMap((file) => scanSource(file, readFileSync(file, 'utf8')));
    expect(findings).toEqual([]);
  });

  it.each([
    ['double quotes', `export const A = () => <button className="button button-secondary">x</button>;`],
    ['single quotes', `export const A = () => <button className='text-button'>x</button>;`],
    ['template literal', 'export const A = () => <button className={`button-strong ${x}`}>x</button>;'],
    ['jsx expression', `export const A = () => <button className={'button-secondary'}>x</button>;`],
    ['cx helper', `export const A = () => <button className={cx('text-button')}>x</button>;`],
    ['bare raw action', `export const A = () => <button type="button">Retry</button>;`],
    ['role=button action', `export const A = () => <button type="button" role="button">Retry</button>;`],
    ['close-account substring', `export const A = () => <button type="button" className="close-account">x</button>;`],
    ['menu-cta substring', `export const A = () => <button type="button" className="menu-cta">x</button>;`],
    ['step-back substring', `export const A = () => <button type="button" className="step-back">x</button>;`],
    ['agent-jump-link action', `export const A = () => <button type="button" className="agent-jump-link">x</button>;`],
    ['pseudo aria-pressed action', `export const A = () => <button type="button" className="retry-all" aria-pressed="true">x</button>;`],
    ['bootstrap primary action', `export const A = () => <button type="button" className="bootstrap-card__primary">重新检查</button>;`],
    ['dynamic class identifier', 'export const A = ({ c }: { c: string }) => <button type="button" className={c}>x</button>;'],
    ['template with suffix-only structural token', 'export const A = ({ x }: { x: string }) => <button type="button" className={`${x} nav-item`}>x</button>;'],
  ])('detects %s', (_label, source) => {
    expect(scanSource('negative.tsx', source).length).toBeGreaterThan(0);
  });

  it('allows structural controls (menuitem / aria-pressed)', () => {
    const source = `
      export const A = () => <button type="button" role="menuitem">item</button>;
      export const B = () => <button type="button" role="tab" aria-pressed="true">tab</button>;
      export const C = () => <button type="button" className="filter-chip filter-clear">chip</button>;
      export const D = () => <button type="button" className="nav-item">nav</button>;
      export const E = () => <button type="button" aria-expanded="false" aria-controls="p" className="disclosure-trigger">more</button>;
    `;
    expect(scanSource('allowed.tsx', source)).toEqual([]);
  });

  it('keeps the shared Button event-aware API', () => {
    const primitive = readFileSync(resolve(process.cwd(), 'src/components/primitives/Button.tsx'), 'utf8');
    expect(primitive).toContain('MouseEventHandler<HTMLButtonElement>');
    expect(primitive).toContain('onClick?.(event)');
  });
});
