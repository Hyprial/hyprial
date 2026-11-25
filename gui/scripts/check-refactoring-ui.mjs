#!/usr/bin/env node
/*
 * Refactoring UI advisory checker for the shared GUI.
 *
 * Decision record (Allen, 2026-11-10):
 * - The checker is a reminder, not a gate. It always exits 0 unless --strict
 *   is passed; --strict is the single upgrade point if blocking is wanted.
 * - It inventories the whole product tree (src/, excluding src/dev and the
 *   generated client) so the visual backlog can be worked down layer by
 *   layer: base (tokens/primitives) -> composite (shared components and
 *   features) -> page (pages and shell layout).
 * - With --base <ref> the ::warning annotations are limited to lines added
 *   by the diff, so a pull request only hears about what it introduces.
 * - Rules encode Refactoring UI principles that can be read from source:
 *   a constrained spacing/type/radius/shadow/colour system, restrained font
 *   weights, fewer borders, one primary action, responsive sizing, and
 *   interface copy that carries no implementation jargon or filler.
 * - Heuristics stay conservative: a value that cannot be classified yields
 *   no finding.
 * - Theme reuse (Allen, 2026-11-11): src/styles/themes/ is the only place colour,
 *   shadow and font-family literals may appear; tokens.css now holds scales and
 *   var() aliases only. theme-bypass flags theme variables referenced, or --ui-*
 *   declared, outside the theme directory, tokens.css and shadcn.css. The hard
 *   gate is tests/theme-contract.test.ts; this script only reminds.
 * - Unified notification (Allen, 2026-11-11): asked for a reminder, not a gate,
 *   that steers one-off action results to dispatch push-notice (toast) instead
 *   of page text. inline-feedback only applies to TSX in the composite and page
 *   layers and skips ToastHost, NotificationCenter and adapter/API paths
 *   (/impl/api/, /daemon-api/, /read-adapters/, src/adapters/). It reports
 *   (A) lowercase HTML tags carrying role=status|alert or aria-live and
 *   (B) local setters (setMessage, setResult, setNotice, setFeedback,
 *   setStatus, setStatusText) given an action-result string such as 已保存.
 *   Failure texts are deliberately not matched in B: many stay next to the
 *   thing that failed. Not reported: field validation (id ending -error/-hint,
 *   aria-errormessage, src/components/forms/), progress (aria-busy, 正在/
 *   加载中/… text), screen-reader-only text (ui-visually-hidden, sr-only),
 *   a setter line that already forwards push-notice, and any tag carrying
 *   data-inline-feedback="<reason>" with reason in validation, progress,
 *   state, blocking, secret, message, flow (the documented opt-out; an
 *   unknown reason is itself reported). Component tags are never reported.
 *   Anything that cannot be classified yields no finding.
 */
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const GUI_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');

export const SPACING_SCALE = [0, 1, 2, 4, 8, 12, 16, 20, 24, 32, 40, 48, 64, 80, 96, 128];
export const TYPE_SCALE = [12, 13, 14, 16, 20, 24, 30];
export const RADIUS_SCALE = [0, 2, 4, 6, 8, 12, 20, 28, 40, 999, 9999];
export const WEIGHT_SCALE = ['400', '500', '600', 'normal', 'inherit'];
export const BREAKPOINTS = [430, 640, 820, 979, 980, 1359, 1360];
export const LEADING_SCALE = ['1', '1.2', '1.5', '1.65', 'normal', 'inherit'];
const THEME_DIR = 'src/styles/themes/';
const TOKENS_FILE = 'src/styles/tokens.css';
const SHADCN_FILE = 'src/styles/shadcn.css';
const FIXED_SIZE_LIMIT = 320;
const BORDER_DENSITY_LIMIT = 12;
const COPY_CJK_LIMIT = 40;
const COPY_LATIN_LIMIT = 110;

export const RULES = {
  'color-literal': '颜色字面量：只允许出现在 src/styles/themes/ 主题文件；组件改用 --ui-* token，不临时调色。',
  'font-literal': '字体族字面量：改用 var(--ui-font-*)，字体名只在主题文件中声明。',
  'theme-bypass': '绕过主题层：只有 tokens.css 与 shadcn.css 可引用主题变量，--ui-* 只在 tokens.css 声明；组件改用 --ui-*。',
  'spacing-scale': '间距不在 4px 间距体系内：改用 --ui-space-* 或体系内的值。',
  'type-scale': '字号不在字号体系内：改用 --ui-text-*（12/13/14/16/20/24/30）。',
  'font-weight': '字重超出体系：界面只用 400/500/600，避免 400 以下的细字重。',
  'radius-scale': '圆角不在体系内：改用 --ui-radius-* 或体系内的值。',
  'shadow-literal': '阴影字面量：改用 --ui-shadow-* 层级 token，字面量只在主题文件。',
  'line-height': '行高不在体系内：改用 --ui-leading-*，字号越大行高越紧。',
  'z-index-literal': 'z-index 字面量：改用具名层级 token。',
  'border-density': '边框过多：优先用间距、背景色差或阴影区分区域。',
  'fixed-size': '固定大尺寸：改用 max-width / min() / clamp() / minmax(0, 1fr) 保持自适应。',
  'breakpoint': '断点不在既定集合内（430/640/820/979/1359）。',
  'uppercase-tracking': '全大写文本缺少字间距：补 letter-spacing。',
  'inline-style': '内联样式字面量：移入样式表并使用 token。',
  'primary-action': '同一组件出现多个主按钮：每个视图只保留一个主操作，其余降为次级/三级。',
  'copy-jargon': '界面文案含实现术语：面向用户的文字不暴露实现细节。',
  'copy-length': '界面文案过长：压缩为一句要点，次要说明降级或移除。',
  'inline-feedback': '操作结果写成了界面文字：一次性的操作结果请走统一通知（dispatch push-notice → toast）；确属校验、进度、持续状态、阻断性错误、一次性凭据、消息流或流程阶段的，改用对应 primitive 或标注 data-inline-feedback。',
};

export const INLINE_FEEDBACK_REASONS = ['validation', 'progress', 'state', 'blocking', 'secret', 'message', 'flow'];
const FEEDBACK_TAGS = ['p', 'span', 'div', 'small', 'strong', 'em', 'li', 'output', 'section'];
const FEEDBACK_SETTERS = /\b(?:setMessage|setResult|setNotice|setFeedback|setStatus|setStatusText)\(/g;
const RESULT_VOCAB = /已保存|已复制|已创建|已删除|已更新|已发送|已提交|已撤销|已移除|已添加|已启用|已停用|已刷新|已完成|成功/;
const PROGRESS_VOCAB = /正在|读取中|处理中|加载中|提交中|保存中/;
const INLINE_FEEDBACK_SKIP = ['/impl/api/', '/daemon-api/', '/read-adapters/', 'src/adapters/'];
const NOTIFICATION_SURFACES = ['src/components/ToastHost.tsx', 'src/components/NotificationCenter.tsx'];

const JARGON = [
  /\bfixture\b/i,
  /\bmock\b/i,
  /\bstub\b/i,
  /\bnot implemented\b/i,
  /\bHTTP\s*5\d\d\b/i,
  /\bbackend\b/i,
  /\bdaemon\b/i,
  /\bcontroller\b/i,
  /\badapter\b/i,
  /\bcapabilit(y|ies)\b/i,
  /接线/,
  /占位/,
  /后端/,
  /适配器/,
  /控制器/,
];

export function layerOf(file) {
  const path = file.split(sep).join('/');
  if (path.startsWith('src/dev/') || path.startsWith('src/client/generated/')) return null;
  if (
    path.startsWith(THEME_DIR) ||
    path === 'src/styles/tokens.css' ||
    path === 'src/styles/primitives.css' ||
    path === 'src/styles/shadcn.css' ||
    path.startsWith('src/components/primitives/') ||
    path.startsWith('src/components/ui/')
  ) {
    return 'base';
  }
  if (path.startsWith('src/components/') || path.startsWith('src/features/')) return 'composite';
  if (
    path.startsWith('src/pages/') ||
    path.startsWith('src/styles/') ||
    path.startsWith('src/app/') ||
    path === 'src/App.tsx'
  ) {
    return 'page';
  }
  return null;
}

function stripCssComments(source) {
  return source.replace(/\/\*[\s\S]*?\*\//g, (match) => match.replace(/[^\n]/g, ' '));
}

export function parseCss(source) {
  const text = stripCssComments(source);
  const declarations = [];
  const rules = [];
  const stack = [];
  let buffer = '';
  let bufferLine = 1;
  let line = 1;
  let quote = null;
  let paren = 0;
  const flushDeclaration = () => {
    const raw = buffer.trim();
    buffer = '';
    if (!raw) return;
    const colon = raw.indexOf(':');
    if (colon < 1) return;
    const rule = [...stack].reverse().find((entry) => entry.kind === 'rule');
    if (!rule) return;
    const declaration = {
      prop: raw.slice(0, colon).trim().toLowerCase(),
      value: raw.slice(colon + 1).trim(),
      line: bufferLine,
      selector: rule.prelude,
      media: stack.filter((entry) => entry.kind === 'at').map((entry) => entry.prelude),
    };
    declarations.push(declaration);
    rule.declarations.push(declaration);
  };
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (!buffer.trim()) bufferLine = line;
    if (char === '\n') line += 1;
    if (quote) {
      buffer += char;
      if (char === quote && text[index - 1] !== '\\') quote = null;
      continue;
    }
    if (char === '"' || char === "'") {
      quote = char;
      buffer += char;
      continue;
    }
    if (char === '(') paren += 1;
    if (char === ')') paren = Math.max(0, paren - 1);
    if (paren > 0) {
      buffer += char;
      continue;
    }
    if (char === '{') {
      const prelude = buffer.trim().replace(/\s+/g, ' ');
      buffer = '';
      const entry = {
        kind: prelude.startsWith('@') ? 'at' : 'rule',
        prelude,
        line: bufferLine,
        declarations: [],
      };
      if (entry.kind === 'rule') rules.push(entry);
      stack.push(entry);
      continue;
    }
    if (char === '}') {
      flushDeclaration();
      stack.pop();
      continue;
    }
    if (char === ';') {
      flushDeclaration();
      continue;
    }
    buffer += char;
  }
  const atRules = [];
  const atPattern = /@media[^{]+/g;
  let match;
  while ((match = atPattern.exec(text)) !== null) {
    atRules.push({
      prelude: match[0].trim(),
      line: text.slice(0, match.index).split('\n').length,
    });
  }
  return { declarations, rules, atRules };
}

function pxValues(value) {
  const withoutVars = value.replace(/var\([^)]*\)/g, ' ');
  return [...withoutVars.matchAll(/(-?\d*\.?\d+)px\b/g)].map((match) => Math.abs(Number(match[1])));
}

const SPACING_PROPS = /^(margin|padding)(-(top|right|bottom|left|inline|block)(-(start|end))?)?$|^(gap|row-gap|column-gap)$/;
const SIZE_PROPS = /^(width|min-width|height|min-height|flex-basis)$/;
const COLOR_LITERAL = /#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|hsla|oklch|oklab)\(/;
const FONT_FAMILY_OK = /^(?:var\(--ui-font-[a-z-]+\)|inherit)$/;

let cachedThemeNames = null;
export function themeVariableNames(root = GUI_ROOT) {
  if (root === GUI_ROOT && cachedThemeNames) return cachedThemeNames;
  const names = new Set();
  try {
    const text = stripCssComments(readFileSync(join(root, THEME_DIR, 'hyprial-v5.css'), 'utf8'));
    for (const match of text.matchAll(/(?:^|[;{\s])--([a-z0-9-]+)\s*:/g)) names.add(match[1]);
  } catch {
    return names;
  }
  if (root === GUI_ROOT) cachedThemeNames = names;
  return names;
}

function themeBypassUses(path, text, themeNames) {
  if (path.startsWith(THEME_DIR) || path === TOKENS_FILE || path === SHADCN_FILE) return [];
  const uses = [];
  for (const match of text.matchAll(/var\(\s*--([a-z0-9-]+)/g)) {
    if (themeNames.has(match[1])) uses.push({ index: match.index, detail: `var(--${match[1]})` });
  }
  for (const match of text.matchAll(/(?:^|[;{\s'"])(--ui-[a-z0-9-]+)['"]?\s*:/g)) {
    uses.push({ index: match.index, detail: `${match[1]} 声明` });
  }
  return uses;
}

export function checkCss(file, source, themeNames = themeVariableNames()) {
  const findings = [];
  const add = (rule, line, detail) => findings.push({ rule, file, line, detail });
  const path = file.split(sep).join('/');
  const isTheme = path.startsWith(THEME_DIR);
  for (const use of themeBypassUses(path, stripCssComments(source), themeNames)) {
    add('theme-bypass', lineOf(stripCssComments(source), use.index), use.detail);
  }
  const { declarations, rules, atRules } = parseCss(source);

  for (const at of atRules) {
    for (const width of [...at.prelude.matchAll(/(?:max|min)-width:\s*(\d+)px/g)]) {
      if (!BREAKPOINTS.includes(Number(width[1]))) add('breakpoint', at.line, at.prelude);
    }
  }

  for (const declaration of declarations) {
    const { prop, value, line } = declaration;
    const label = `${prop}: ${value}`;
    if (prop.startsWith('--')) {
      continue;
    }
    if (!isTheme && COLOR_LITERAL.test(value)) add('color-literal', line, label);
    if (!isTheme && prop === 'font-family' && !FONT_FAMILY_OK.test(value)) add('font-literal', line, label);
    if (SPACING_PROPS.test(prop)) {
      const off = pxValues(value).filter((px) => !SPACING_SCALE.includes(px));
      if (off.length > 0) add('spacing-scale', line, label);
    }
    if (prop === 'font-size') {
      const off = pxValues(value).filter((px) => !TYPE_SCALE.includes(px));
      if (off.length > 0 || /\d(em|rem|%)\b/.test(value.replace(/var\([^)]*\)/g, ''))) {
        add('type-scale', line, label);
      }
    }
    if (prop === 'font-weight' && !value.startsWith('var(') && !WEIGHT_SCALE.includes(value)) {
      add('font-weight', line, label);
    }
    if (prop === 'border-radius' || /^border-(top|bottom)-(left|right)-radius$/.test(prop)) {
      const off = pxValues(value).filter((px) => !RADIUS_SCALE.includes(px));
      if (off.length > 0) add('radius-scale', line, label);
    }
    if (!isTheme && prop === 'box-shadow' && value !== 'none' && !/^var\([^)]*\)$/.test(value) && value !== 'inherit') {
      if (!/^(?:inset )?0 0 0 /.test(value) || COLOR_LITERAL.test(value)) add('shadow-literal', line, label);
    }
    if (prop === 'line-height' && !value.startsWith('var(') && !LEADING_SCALE.includes(value)) {
      add('line-height', line, label);
    }
    if (prop === 'z-index' && /^-?\d+$/.test(value) && Math.abs(Number(value)) > 1) {
      add('z-index-literal', line, label);
    }
    if (SIZE_PROPS.test(prop) && !/min\(|max\(|clamp\(|calc\(|var\(/.test(value)) {
      const big = pxValues(value).filter((px) => px >= FIXED_SIZE_LIMIT);
      const guarded = prop === 'width' && declarations.some(
        (other) => other.selector === declaration.selector && other.prop === 'max-width',
      );
      if (big.length > 0 && !guarded) add('fixed-size', line, label);
    }
    if (/\b100vw\b/.test(value)) add('fixed-size', line, label);
  }

  for (const rule of rules) {
    const upper = rule.declarations.find(
      (declaration) => declaration.prop === 'text-transform' && declaration.value === 'uppercase',
    );
    if (upper && !rule.declarations.some((declaration) => declaration.prop === 'letter-spacing')) {
      add('uppercase-tracking', upper.line, rule.prelude);
    }
  }

  const borders = declarations.filter(
    (declaration) =>
      /^border(-(top|right|bottom|left|inline|block)(-(start|end))?)?$/.test(declaration.prop) &&
      /\bsolid\b|\bdashed\b/.test(declaration.value),
  );
  if (borders.length > BORDER_DENSITY_LIMIT) {
    add('border-density', borders[0].line, `${borders.length} 处边框声明`);
  }
  return findings;
}

function lineOf(source, index) {
  return source.slice(0, index).split('\n').length;
}

function stripTsComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, (match) => match.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:"'`\\])\/\/[^\n]*/g, (match, lead) => lead + ' '.repeat(match.length - lead.length));
}

function openingTagEnd(text, from) {
  let depth = 0;
  let quote = null;
  const limit = Math.min(text.length, from + 2000);
  for (let index = from; index < limit; index += 1) {
    const char = text[index];
    if (quote) {
      if (char === quote && text[index - 1] !== '\\') quote = null;
      continue;
    }
    if (char === '"' || char === "'" || char === '`') quote = char;
    else if (char === '{') depth += 1;
    else if (char === '}') depth -= 1;
    else if (char === '>' && depth <= 0) return index;
  }
  return -1;
}

function followingText(text, end) {
  const rest = text.slice(end + 1);
  const lines = rest.split('\n');
  const window = lines.length > 1 && lines[0].trim() === '' ? `${lines[0]}\n${lines[1]}` : lines[0];
  return window;
}

function isProgressText(snippet) {
  if (PROGRESS_VOCAB.test(snippet)) return true;
  const literal = snippet.split('<')[0].trim();
  return /(?:…|\.\.\.)$/.test(literal);
}

function checkInlineFeedback(path, text, add) {
  if (INLINE_FEEDBACK_SKIP.some((part) => path.includes(part))) return;
  if (NOTIFICATION_SURFACES.includes(path)) return;
  const layer = layerOf(path);
  if (layer !== 'composite' && layer !== 'page') return;
  const inForms = path.includes('src/components/forms/');
  const detailOf = (tag) => tag.replace(/\s+/g, ' ').slice(0, 90);

  for (const open of text.matchAll(/<([a-z][a-z0-9]*|[A-Z][A-Za-z0-9.]*)(?=[\s/>])/g)) {
    if (/[A-Za-z0-9_$]/.test(text[open.index - 1] ?? ' ')) continue;
    const end = openingTagEnd(text, open.index + open[0].length);
    if (end < 0) continue;
    const tag = text.slice(open.index, end + 1);
    const line = lineOf(text, open.index);
    const marked = /data-inline-feedback=(?:"([^"]*)"|\{\s*(['"`])([^'"`]*)\2\s*\})/.exec(tag);
    if (marked) {
      const reason = marked[1] ?? marked[3];
      if (!INLINE_FEEDBACK_REASONS.includes(reason)) {
        add('inline-feedback', line, `data-inline-feedback="${reason}" 不在允许的 reason 列表（${INLINE_FEEDBACK_REASONS.join('/')}）`);
      }
      continue;
    }
    if (!FEEDBACK_TAGS.includes(open[1])) continue;
    if (!/\brole=(?:"(?:status|alert)"|\{\s*['"`](?:status|alert)['"`]\s*\})|\baria-live\b/.test(tag)) continue;
    if (inForms) continue;
    if (/\bid=[^\s>]*-(?:error|hint)\b|\baria-errormessage\b/.test(tag)) continue;
    if (/\baria-busy\b/.test(tag)) continue;
    if (/\bclassName=[^>]*(?:ui-visually-hidden|sr-only)/.test(tag)) continue;
    if (isProgressText(followingText(text, end))) continue;
    add('inline-feedback', line, detailOf(tag));
  }

  for (const call of text.matchAll(FEEDBACK_SETTERS)) {
    const lineStart = text.lastIndexOf('\n', call.index) + 1;
    const lineEnd = text.indexOf('\n', call.index);
    const lineText = text.slice(lineStart, lineEnd < 0 ? text.length : lineEnd);
    if (lineText.includes('push-notice')) continue;
    const rest = text.slice(call.index + call[0].length, lineEnd < 0 ? text.length : lineEnd);
    const literals = [...rest.matchAll(/(['"`])((?:(?!\1)[^\\\n]|\\.)*)\1/g)];
    if (literals.some((literal) => RESULT_VOCAB.test(literal[2]))) {
      add('inline-feedback', lineOf(text, call.index), lineText.trim().slice(0, 90));
    }
  }
}

function cjkCount(text) {
  return (text.match(/[一-鿿]/g) ?? []).length;
}

export function checkTsx(file, source, themeNames = themeVariableNames()) {
  const findings = [];
  const add = (rule, line, detail) => findings.push({ rule, file, line, detail });
  const text = stripTsComments(source);
  for (const use of themeBypassUses(file.split(sep).join('/'), text, themeNames)) {
    add('theme-bypass', lineOf(text, use.index), use.detail);
  }

  for (const match of text.matchAll(/style=\{\{([^}]*)\}\}/g)) {
    if (/\d+px|#[0-9a-fA-F]{3,8}\b|rgb\(|:\s*\d+\s*[,}]/.test(match[1]) || /:\s*\d+\s*$/.test(match[1].trim())) {
      add('inline-style', lineOf(text, match.index), match[0].replace(/\s+/g, ' ').slice(0, 90));
    }
  }

  const primaries = [...text.matchAll(/variant=(?:"primary"|\{'primary'\})/g)];
  if (primaries.length > 1) {
    add('primary-action', lineOf(text, primaries[1].index), `${primaries.length} 个 variant="primary"`);
  }

  const copy = [];
  for (const match of text.matchAll(/>([^<>{}]*[一-鿿A-Za-z][^<>{}]*)</g)) {
    copy.push({ value: match[1].trim(), index: match.index });
  }
  for (const match of text.matchAll(/(['"`])((?:(?!\1)[^\\\n]|\\.)*[一-鿿](?:(?!\1)[^\\\n]|\\.)*)\1/g)) {
    copy.push({ value: match[2].trim(), index: match.index });
  }
  checkInlineFeedback(file.split(sep).join('/'), text, add);
  const seen = new Set();
  for (const { value, index } of copy) {
    const line = lineOf(text, index);
    const key = `${line}:${value}`;
    if (!value || seen.has(key)) continue;
    seen.add(key);
    if (cjkCount(value) === 0 && !/\s/.test(value)) continue;
    const jargon = JARGON.find((pattern) => pattern.test(value));
    if (jargon && cjkCount(value) > 0) {
      add('copy-jargon', line, value.slice(0, 80));
      continue;
    }
    if (cjkCount(value) > COPY_CJK_LIMIT || (cjkCount(value) === 0 && value.length > COPY_LATIN_LIMIT && /^[^=;(){}]+$/.test(value))) {
      add('copy-length', line, value.slice(0, 80));
    }
  }
  return findings;
}

function walk(directory, out = []) {
  for (const name of readdirSync(directory).sort()) {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) walk(path, out);
    else out.push(path);
  }
  return out;
}

export function scan(root = GUI_ROOT) {
  const findings = [];
  for (const path of walk(join(root, 'src'))) {
    const file = relative(root, path).split(sep).join('/');
    const layer = layerOf(file);
    if (!layer) continue;
    let fileFindings = [];
    if (file.endsWith('.css')) fileFindings = checkCss(file, readFileSync(path, 'utf8'));
    else if (file.endsWith('.tsx')) fileFindings = checkTsx(file, readFileSync(path, 'utf8'));
    for (const finding of fileFindings) findings.push({ ...finding, layer });
  }
  return findings;
}

const runGit = (args) => execFileSync('git', args, { encoding: 'utf8', maxBuffer: 256 * 1024 * 1024, stdio: ['ignore', 'pipe', 'ignore'] });

export function addedLines(root, base, git = runGit) {
  let top;
  try {
    top = git(['-C', root, 'rev-parse', '--show-toplevel']).trim();
  } catch {
    return null;
  }
  const prefix = relative(top, root).split(sep).join('/');
  const pathspec = prefix ? `${prefix}/src` : 'src';
  const diffArgs = (range) => ['-C', top, 'diff', '--unified=0', '--no-color', ...range, '--', pathspec];
  let diff = null;
  for (const range of [[`${base}...HEAD`], [base, 'HEAD']]) {
    try {
      diff = git(diffArgs(range));
      break;
    } catch {
      diff = null;
    }
  }
  if (diff === null) return null;
  const added = new Map();
  let current = null;
  for (const line of diff.split('\n')) {
    if (line.startsWith('+++ ')) {
      const path = line.slice(4).replace(/^b\//, '');
      current = path === '/dev/null' ? null : (prefix ? path.slice(prefix.length + 1) : path);
      if (current && !added.has(current)) added.set(current, new Set());
      continue;
    }
    const hunk = /^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@/.exec(line);
    if (hunk && current) {
      const start = Number(hunk[1]);
      const count = hunk[2] === undefined ? 1 : Number(hunk[2]);
      for (let offset = 0; offset < count; offset += 1) added.get(current).add(start + offset);
    }
  }
  return added;
}

export function summarize(findings) {
  const summary = {};
  for (const layer of ['base', 'composite', 'page']) {
    const inLayer = findings.filter((finding) => finding.layer === layer);
    const byRule = {};
    const byFile = {};
    for (const finding of inLayer) {
      byRule[finding.rule] = (byRule[finding.rule] ?? 0) + 1;
      byFile[finding.file] = (byFile[finding.file] ?? 0) + 1;
    }
    summary[layer] = { total: inLayer.length, byRule, byFile };
  }
  return summary;
}

function parseArgs(argv) {
  const options = { strict: false, annotations: false, base: null, json: null, verbose: false, layer: null };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (arg === '--strict') options.strict = true;
    else if (arg === '--annotations') options.annotations = true;
    else if (arg === '--verbose') options.verbose = true;
    else if (arg === '--base') options.base = argv[(index += 1)];
    else if (arg === '--json') options.json = argv[(index += 1)];
    else if (arg === '--layer') options.layer = argv[(index += 1)];
    else throw new Error(`unknown argument: ${arg}`);
  }
  return options;
}

function main() {
  const options = parseArgs(process.argv.slice(2));
  let findings = scan();
  if (options.layer) findings = findings.filter((finding) => finding.layer === options.layer);
  const summary = summarize(findings);
  let reported = findings;
  let diffUnavailable = false;
  if (options.base) {
    const added = addedLines(GUI_ROOT, options.base);
    if (added === null) {
      diffUnavailable = true;
      reported = [];
    } else {
      reported = findings.filter((finding) => added.get(finding.file)?.has(finding.line));
    }
  }
  if (options.json) {
    writeFileSync(options.json, `${JSON.stringify({ summary, findings }, null, 2)}\n`);
  }
  const lines = ['Refactoring UI advisory（提示，不阻断）'];
  for (const [layer, data] of Object.entries(summary)) {
    lines.push(`\n[${layer}] ${data.total} 处`);
    for (const [rule, count] of Object.entries(data.byRule).sort((a, b) => b[1] - a[1])) {
      lines.push(`  ${String(count).padStart(5)}  ${rule}`);
    }
    const files = Object.entries(data.byFile).sort((a, b) => b[1] - a[1]);
    for (const [file, count] of files.slice(0, options.verbose ? files.length : 8)) {
      lines.push(`  ${String(count).padStart(5)}  ${file}`);
    }
  }
  if (diffUnavailable) lines.push('\n无法计算 diff，跳过新增行过滤');
  else if (options.base) lines.push(`\n本次 diff 新增行命中 ${reported.length} 处（base ${options.base}）`);
  process.stdout.write(`${lines.join('\n')}\n`);
  if (options.verbose || options.base) {
    for (const finding of reported) {
      process.stdout.write(`${finding.file}:${finding.line} [${finding.rule}] ${finding.detail}\n`);
    }
  }
  if (options.annotations) {
    for (const finding of reported.slice(0, 200)) {
      process.stdout.write(
        `::warning file=gui/${finding.file},line=${finding.line},title=refactoring-ui/${finding.rule}::${RULES[finding.rule]} (${finding.detail.replace(/\n/g, ' ')})\n`,
      );
    }
  }
  if (options.strict && reported.length > 0) process.exitCode = 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main();
}
