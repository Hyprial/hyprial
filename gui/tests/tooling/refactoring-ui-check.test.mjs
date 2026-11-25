import { describe, expect, it } from 'vitest';
import { addedLines, checkCss, checkTsx, layerOf, parseCss, scan, summarize } from '../../scripts/check-refactoring-ui.mjs';

const rulesOf = (findings) => findings.map((finding) => finding.rule).sort();

describe('refactoring-ui advisory checker', () => {
  it('assigns files to base, composite and page layers', () => {
    expect(layerOf('src/styles/primitives.css')).toBe('base');
    expect(layerOf('src/components/primitives/Button.tsx')).toBe('base');
    expect(layerOf('src/components/Rail.tsx')).toBe('composite');
    expect(layerOf('src/features/org/impl/workspace/workspace.css')).toBe('composite');
    expect(layerOf('src/pages/SettingsPage.tsx')).toBe('page');
    expect(layerOf('src/styles/app.css')).toBe('page');
    expect(layerOf('src/styles/themes/hyprial-v5.css')).toBe('base');
    expect(layerOf('src/dev/session.css')).toBeNull();
    expect(layerOf('src/client/generated/client.ts')).toBeNull();
  });

  it('parses declarations with their selector, line and media context', () => {
    const { declarations } = parseCss('/* c */\n.a {\n  color: red;\n}\n@media (max-width: 640px) {\n  .b { gap: 8px; }\n}\n');
    expect(declarations).toEqual([
      { prop: 'color', value: 'red', line: 3, selector: '.a', media: [] },
      { prop: 'gap', value: '8px', line: 6, selector: '.b', media: ['@media (max-width: 640px)'] },
    ]);
  });

  it('accepts token-based and on-scale declarations', () => {
    const css = `.ok {
      color: var(--ui-ink-1);
      padding: var(--ui-space-2) 16px;
      font-size: 14px;
      font-weight: 600;
      border-radius: var(--ui-radius-sm);
      box-shadow: var(--ui-shadow-card);
      line-height: var(--ui-leading-normal);
      width: min(100%, 480px);
    }
    @media (max-width: 640px) { .ok { gap: 8px; } }`;
    expect(checkCss('src/features/x/x.css', css)).toEqual([]);
  });

  it('reports each off-system value once with its rule', () => {
    const css = `.bad {
      color: #123456;
      padding: 7px 16px;
      font-size: 11.5px;
      font-weight: 300;
      border-radius: 9px;
      box-shadow: 0 2px 9px rgb(0 0 0 / 10%);
      line-height: 1.37;
      z-index: 40;
      width: 520px;
      text-transform: uppercase;
    }
    @media (max-width: 777px) { .bad { gap: 8px; } }`;
    expect(rulesOf(checkCss('src/features/x/x.css', css))).toEqual([
      'breakpoint',
      'color-literal',
      'color-literal',
      'fixed-size',
      'font-weight',
      'line-height',
      'radius-scale',
      'shadow-literal',
      'spacing-scale',
      'type-scale',
      'uppercase-tracking',
      'z-index-literal',
    ]);
  });

  it('lets only the theme directory declare literal colours, shadows and font families', () => {
    const literals = ':root { --card: #fff; --shadow-float: 0 1px 2px rgb(0 0 0 / 4%); } body { background: #fff; font-family: "Geist"; box-shadow: 0 1px 2px rgb(0 0 0 / 4%); }';
    expect(checkCss('src/styles/themes/hyprial-v5.css', literals)).toEqual([]);
    expect(rulesOf(checkCss('src/styles/app.css', 'body { background: #fff; }'))).toEqual(['color-literal']);
    expect(rulesOf(checkCss('src/styles/tokens.css', 'body { background: #fff; }'))).toEqual(['color-literal']);
    expect(rulesOf(checkCss('src/features/x/x.css', '.a { font-family: "Geist", sans-serif; }'))).toEqual(['font-literal']);
    expect(checkCss('src/features/x/x.css', '.a { font-family: var(--ui-font-mono); }')).toEqual([]);
  });

  it('flags theme variables and --ui-* declarations outside the theme layers', () => {
    expect(rulesOf(checkCss('src/features/x/x.css', '.a { color: var(--foreground); background: var(--card); }'))).toEqual([
      'theme-bypass',
      'theme-bypass',
    ]);
    expect(rulesOf(checkCss('src/features/x/x.css', '.a { --ui-local: 4px; }'))).toEqual(['theme-bypass']);
    expect(checkCss('src/features/x/x.css', '.a { color: var(--ui-ink-1); --local-gap: 4px; }')).toEqual([]);
    expect(checkCss('src/styles/tokens.css', ':root { --ui-ink-1: var(--foreground); }')).toEqual([]);
    expect(checkCss('src/styles/shadcn.css', '@theme inline { --color-card: var(--card); }')).toEqual([]);
    expect(rulesOf(checkTsx('src/features/x/A.tsx', 'const a = "var(--primary)";'))).toEqual(['theme-bypass']);
    expect(checkTsx('src/features/x/A.tsx', 'const a = "var(--ui-accent)";')).toEqual([]);
  });

  it('treats a fixed width paired with max-width as responsive', () => {
    expect(checkCss('src/pages/a.css', '.a { width: 480px; max-width: 100%; }')).toEqual([]);
    expect(rulesOf(checkCss('src/pages/a.css', '.a { min-width: 480px; }'))).toEqual(['fixed-size']);
  });

  it('flags jargon, long copy, inline literals and repeated primary actions in TSX', () => {
    const tsx = `export function A() {
      // 后端 comment is ignored
      return (
        <div style={{ width: 320 }}>
          <p>服务后端尚未接线</p>
          <p>{'这是一段非常非常长的说明文字它反复解释同一件事情并且远远超过了一句话可以承载的信息量用户通常不会读完它'}</p>
          <Button variant="primary">保存</Button>
          <Button variant="primary">继续</Button>
          <p>保存</p>
        </div>
      );
    }`;
    expect(rulesOf(checkTsx('src/features/x/A.tsx', tsx))).toEqual([
      'copy-jargon',
      'copy-length',
      'inline-style',
      'primary-action',
    ]);
  });

  it('flags weight 700 and bold as off the 400/500/600 scale', () => {
    expect(rulesOf(checkCss('src/features/x/x.css', '.a { font-weight: 700; } .b { font-weight: bold; }'))).toEqual(['font-weight', 'font-weight']);
  });

  it('computes added lines with the three-dot diff when it works', () => {
    const calls = [];
    const git = (args) => {
      calls.push(args.join(' '));
      if (args.includes('--show-toplevel')) return '/repo\n';
      return '+++ b/src/a.css\n@@ -1,0 +3,2 @@\n+x\n+y\n';
    };
    const added = addedLines('/repo', 'origin/dev', git);
    expect([...added.get('src/a.css')]).toEqual([3, 4]);
    expect(calls.some((call) => call.includes('origin/dev...HEAD'))).toBe(true);
  });

  it('falls back to the two-dot diff when there is no merge base', () => {
    const git = (args) => {
      if (args.includes('--show-toplevel')) return '/repo\n';
      if (args.some((arg) => arg.includes('...'))) throw new Error('no merge base');
      return '+++ b/src/a.css\n@@ -1 +1 @@\n+x\n';
    };
    expect([...addedLines('/repo', 'origin/dev', git).get('src/a.css')]).toEqual([1]);
  });

  it('returns null when no diff can be computed', () => {
    const git = (args) => {
      if (args.includes('--show-toplevel')) return '/repo\n';
      throw new Error('bad revision');
    };
    expect(addedLines('/repo', 'origin/dev', git)).toBeNull();
  });

  describe('inline-feedback', () => {
    const inline = (source, file = 'src/features/x/A.tsx') =>
      checkTsx(file, source).filter((finding) => finding.rule === 'inline-feedback');

    it('reports hand-rolled live regions on lowercase tags', () => {
      expect(inline('const a = <p role="status">已保存</p>;')).toHaveLength(1);
      expect(inline('const a = <div role="alert">{error}</div>;')).toHaveLength(1);
      expect(inline('const a = <span aria-live="polite">已发送给对方</span>;')).toHaveLength(1);
      const multi = 'const a = (\n  <p\n    className="note"\n    role="status"\n  >\n    已复制\n  </p>\n);';
      const found = inline(multi);
      expect(found).toHaveLength(1);
      expect(found[0].line).toBe(2);
    });

    it('ignores components, progress, field validation, screen-reader text and opt-outs', () => {
      expect(inline('const a = <Notice role="alert">失败</Notice>;')).toEqual([]);
      expect(inline('const a = <LoadingState />;')).toEqual([]);
      expect(inline('const a = <p role="status" aria-busy="true">已保存</p>;')).toEqual([]);
      expect(inline('const a = <p role="status">正在读取…</p>;')).toEqual([]);
      expect(inline('const a = <p role="status">\n  正在保存\n</p>;')).toEqual([]);
      expect(inline('const a = <p role="status">{label}…</p>;')).toEqual([]);
      expect(inline('const a = <p id="name-error" role="alert">{error}</p>;')).toEqual([]);
      expect(inline('const a = <p id={`${id}-hint`} aria-live="polite">{hint}</p>;')).toEqual([]);
      expect(inline('const a = <p role="alert" aria-errormessage="x">{e}</p>;')).toEqual([]);
      expect(inline('const a = <p role="alert">{e}</p>;', 'src/components/forms/SecretField.tsx')).toEqual([]);
      expect(inline('const a = <span className="ui-visually-hidden" role="status">已保存</span>;')).toEqual([]);
      expect(inline('const a = <span className="sr-only" aria-live="polite">已保存</span>;')).toEqual([]);
      expect(inline('const a = <p role="alert" data-inline-feedback="blocking">{e}</p>;')).toEqual([]);
      expect(inline('const a = <p\n  role="alert"\n  data-inline-feedback="secret"\n>{e}</p>;')).toEqual([]);
    });

    it('reports an unknown data-inline-feedback reason once', () => {
      const found = inline('const a = <p role="alert" data-inline-feedback="whatever">{e}</p>;');
      expect(found).toHaveLength(1);
      expect(found[0].detail).toMatch(/whatever/);
      expect(found[0].detail).toMatch(/不在允许/);
      expect(inline('const a = <Notice data-inline-feedback="whatever" />;')).toHaveLength(1);
    });

    it('reports action-result strings passed to local message setters', () => {
      expect(inline("setMessage('已保存');")).toHaveLength(1);
      expect(inline('setResult(`已复制 ${n} 项`);')).toHaveLength(1);
      expect(inline('setStatusText("操作成功");')).toHaveLength(1);
    });

    it('does not report failures, other setters, forwarding or excluded paths', () => {
      expect(inline("setMessage('保存失败');")).toEqual([]);
      expect(inline("setError('已保存');")).toEqual([]);
      expect(inline("setMessage('已保存'); dispatch({ type: 'push-notice' });")).toEqual([]);
      expect(inline("setMessage('已保存');", 'src/features/x/impl/api/client.tsx')).toEqual([]);
      expect(inline("setMessage('已保存');", 'src/features/x/daemon-api/a.tsx')).toEqual([]);
      expect(inline("setMessage('已保存');", 'src/features/x/read-adapters/a.tsx')).toEqual([]);
      expect(inline("setMessage('已保存');", 'src/adapters/a.tsx')).toEqual([]);
      expect(inline("setMessage('已保存'); // inline", 'src/features/x/A.tsx')).toHaveLength(1);
      expect(inline("// setMessage('已保存')\n")).toEqual([]);
    });

    it('skips base-layer files and the notification surfaces themselves', () => {
      const live = 'const a = <p role="status">已保存</p>; setMessage("已保存");';
      expect(inline(live, 'src/components/primitives/Notice.tsx')).toEqual([]);
      expect(inline(live, 'src/components/ToastHost.tsx')).toEqual([]);
      expect(inline(live, 'src/components/NotificationCenter.tsx')).toEqual([]);
      expect(inline(live, 'src/pages/SettingsPage.tsx')).toHaveLength(2);
    });
  });

  it('summarises the real tree by layer without throwing', () => {
    const summary = summarize(scan());
    expect(Object.keys(summary)).toEqual(['base', 'composite', 'page']);
    for (const layer of Object.values(summary)) {
      expect(layer.total).toBe(Object.values(layer.byRule).reduce((sum, count) => sum + count, 0));
    }
  });
});
