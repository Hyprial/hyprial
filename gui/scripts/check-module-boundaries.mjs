#!/usr/bin/env node
// gui 深模块边界门（WIP PR #2578 C1，依据 docs/design/gui-deep-module-structure-2026-11-08.md）。
// 决策：
// - 用 TypeScript compiler API 解析 gui/src 下全部 .ts/.tsx/.mts/.cts/.js/.jsx，AST 提取
//   静态 import / export-from / 动态 import()，不使用正则匹配源码；注释天然不会进入 AST。
// - 相对路径与 `@/`（→ gui/src）别名均解析到真实文件；解析或读取失败一律按违规失败（fail closed）。
// - 语法错误、无法解析的引用是硬失败，不允许进 baseline。
// - 历史例外由 gui/config/module-boundary-baseline.json 精确逐项登记，只减不增：
//   新增违规失败；已消除（stale）例外也失败，必须显式更新 baseline 才能移除。
// - `--update-baseline` 仅允许收缩；新增例外必须再叠加 `--accept-new` 并在评审 diff 中逐条可见。
// - 唯一内置豁免：src/main.tsx 动态 import() 加载 src/dev/**（设计文档规则 5 明确允许）。
// - 本门只做静态 import 边界，不证明运行时行为、打包产物或 css/资源图。
import { readFile, readdir, stat, writeFile, mkdir } from 'node:fs/promises';
import { dirname, extname, join, relative, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import ts from 'typescript';

const DEFAULT_GUI_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const RULE_KEYS = [
  'feature-structure',
  'cross-feature-deep-import',
  'prod-imports-dev',
  'src-imports-tests',
  'feature-index-exports-mock',
  'primitives-layering',
  'base-layer-references-feature',
];
const SCAN_EXTENSIONS = ['.ts', '.tsx', '.mts', '.cts', '.js', '.jsx'];
const RESOLVE_EXTENSIONS = ['.ts', '.tsx', '.d.ts', '.mts', '.cts', '.js', '.jsx', '.json', '.css', '.svg', '.png'];
const MOCK_TOKENS = new Set(['mock', 'mocks', 'fixture', 'fixtures']);
const BASELINE_VERSION = 1;

function usage() {
  return [
    'usage: node scripts/check-module-boundaries.mjs [--gui-root <dir>] [--update-baseline [--accept-new]]',
    '  --gui-root <dir>   检查指定 gui 根目录（默认：脚本所在 package）。测试用于指向隔离合成树。',
    '  --update-baseline  将 baseline 收缩为当前例外集：仅删除 stale 项；存在新增项时拒绝写入。',
    '  --accept-new       与 --update-baseline 连用：允许并入新增例外（必须在 PR diff 中逐条评审）。',
    '退出码：0 通过；1 边界失败或拒绝更新；2 参数/baseline 文件本身不可用（同样 fail closed）。',
  ].join('\n');
}

function parseArgs(argv) {
  const options = { guiRoot: DEFAULT_GUI_ROOT, updateBaseline: false, acceptNew: false, help: false };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--gui-root' || arg === '--root') {
      i += 1;
      const value = argv[i];
      if (!value) throw new Error(`${arg} requires a directory argument`);
      options.guiRoot = resolve(value);
    } else if (arg === '--update-baseline') {
      options.updateBaseline = true;
    } else if (arg === '--accept-new') {
      options.acceptNew = true;
    } else if (arg === '--help' || arg === '-h') {
      options.help = true;
    } else {
      throw new Error(`unknown argument: ${arg}`);
    }
  }
  if (options.acceptNew && !options.updateBaseline) {
    throw new Error('--accept-new only makes sense together with --update-baseline');
  }
  return options;
}

function toPosix(path) {
  return path.split('\\').join('/');
}

function rel(root, path) {
  return toPosix(relative(root, path));
}

function isUnder(path, dir) {
  const p = toPosix(path);
  const d = toPosix(dir);
  return p === d || p.startsWith(`${d}/`);
}

async function pathExists(path) {
  try {
    await stat(path);
    return true;
  } catch {
    return false;
  }
}

async function isDirectory(path) {
  const info = await stat(path);
  if (!info.isDirectory()) throw new Error(`expected directory: ${path}`);
  return true;
}

async function listFilesRecursive(dir) {
  const entries = await readdir(dir, { withFileTypes: true });
  entries.sort((a, b) => (a.name < b.name ? -1 : 1));
  const files = [];
  for (const entry of entries) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...(await listFilesRecursive(full)));
    } else if (entry.isFile()) {
      files.push(full);
    }
  }
  return files;
}

async function resolveSpecifier(fromFile, specifier, srcRoot, guiRoot) {
  const base = specifier.startsWith('@/')
    ? join(srcRoot, specifier.slice(2))
    : specifier.startsWith('./') || specifier.startsWith('../')
      ? resolve(dirname(fromFile), specifier)
      : null;
  if (base === null && !specifier.startsWith('/')) return { kind: 'external' };
  const absolute = base ?? resolve(guiRoot, `.${specifier}`);
  const candidates = [absolute];
  if (extname(absolute) === '') {
    for (const ext of RESOLVE_EXTENSIONS) candidates.push(absolute + ext);
    for (const ext of ['.ts', '.tsx']) candidates.push(join(absolute, `index${ext}`));
  }
  for (const candidate of candidates) {
    if (await pathExists(candidate)) {
      const info = await stat(candidate);
      if (info.isFile()) return { kind: 'file', path: candidate };
    }
  }
  return { kind: 'unresolved' };
}

function parseSource(file, content) {
  const scriptKind = file.endsWith('.tsx') || file.endsWith('.jsx')
    ? ts.ScriptKind.TSX
    : file.endsWith('.js') || file.endsWith('.jsx')
      ? ts.ScriptKind.JS
      : ts.ScriptKind.TS;
  const source = ts.createSourceFile(file, content, ts.ScriptTarget.ES2022, true, scriptKind);
  if (!Array.isArray(source.parseDiagnostics)) {
    return { source, errors: ['typescript parser did not expose parseDiagnostics; refusing to trust this file'] };
  }
  const errors = source.parseDiagnostics
    .filter((diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error)
    .map((diagnostic) => ts.flattenDiagnosticMessageText(diagnostic.messageText, ' '));
  return { source, errors };
}

function collectImportEdges(file, source) {
  const edges = [];
  const push = (node, kind, specifier) => {
    edges.push({
      kind,
      specifier,
      exportedNames: ts.isExportDeclaration(node) && node.exportClause && ts.isNamedExports(node.exportClause)
        ? node.exportClause.elements.flatMap((element) => [element.name.text, element.propertyName?.text ?? ''])
        : [],
      line: source.getLineAndCharacterOfPosition(node.getStart()).line + 1,
    });
  };
  const visit = (node) => {
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      push(node, 'static-import', node.moduleSpecifier.text);
    } else if (
      ts.isExportDeclaration(node)
      && node.moduleSpecifier !== undefined
      && ts.isStringLiteral(node.moduleSpecifier)
    ) {
      push(node, 'export-from', node.moduleSpecifier.text);
    } else if (
      ts.isCallExpression(node)
      && node.expression.kind === ts.SyntaxKind.ImportKeyword
      && node.arguments.length >= 1
      && ts.isStringLiteral(node.arguments[0])
    ) {
      push(node, 'dynamic-import', node.arguments[0].text);
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return edges;
}

function featureOf(path, featuresRoot) {
  if (!isUnder(path, featuresRoot)) return null;
  const relPath = rel(featuresRoot, path);
  const name = relPath.split('/')[0];
  return name === '' ? null : name;
}

function isFeatureIndex(path, featuresRoot) {
  const relPath = rel(featuresRoot, path);
  const parts = relPath.split('/');
  return parts.length === 2 && parts[1] === 'index.ts';
}

function isMockOrFixturePath(path) {
  const segments = toPosix(path).split('/');
  for (const segment of segments) {
    const withoutExt = extname(segment) === '' ? segment : segment.slice(0, segment.lastIndexOf('.'));
    const tokens = withoutExt.split(/[._-]+/).filter((token) => token !== '');
    if (tokens.some((token) => MOCK_TOKENS.has(token.toLowerCase()))) return true;
  }
  return false;
}

function edgeId(importerRel, targetRel) {
  return `${importerRel} -> ${targetRel}`;
}

async function scanTree(guiRoot) {
  const srcRoot = join(guiRoot, 'src');
  const testsRoot = join(guiRoot, 'tests');
  const featuresRoot = join(srcRoot, 'features');
  const devRoot = join(srcRoot, 'dev');
  const mainEntry = toPosix(join(srcRoot, 'main.tsx'));
  await isDirectory(srcRoot);

  const violations = Object.fromEntries(RULE_KEYS.map((key) => [key, new Set()]));
  const hardFailures = [];
  const add = (rule, id) => violations[rule].add(id);

  const sourceFiles = (await listFilesRecursive(srcRoot)).filter((file) =>
    SCAN_EXTENSIONS.includes(extname(file)));
  for (const file of sourceFiles) {
    const fileRel = rel(guiRoot, file);
    let content;
    try {
      content = await readFile(file, 'utf8');
    } catch (error) {
      hardFailures.push(`unreadable-file:${fileRel}:${error.message}`);
      continue;
    }
    const { source, errors } = parseSource(file, content);
    if (errors.length > 0) {
      hardFailures.push(`parse-error:${fileRel}:${errors[0]}`);
      continue;
    }
    for (const edge of collectImportEdges(file, source)) {
      const resolved = await resolveSpecifier(file, edge.specifier, srcRoot, guiRoot);
      if (resolved.kind === 'external') continue;
      if (resolved.kind === 'unresolved') {
        hardFailures.push(`unresolved-import:${fileRel}:${edge.line}:${edge.specifier}`);
        continue;
      }
      const targetRel = rel(guiRoot, resolved.path);
      const id = edgeId(fileRel, targetRel);
      const importerFeature = featureOf(file, featuresRoot);
      const targetFeature = featureOf(resolved.path, featuresRoot);
      const targetIsFeatureIndex = targetFeature !== null && isFeatureIndex(resolved.path, featuresRoot);

      if (targetFeature !== null && importerFeature !== targetFeature && !targetIsFeatureIndex) {
        add('cross-feature-deep-import', id);
      }
      if (isUnder(resolved.path, testsRoot)) {
        add('src-imports-tests', id);
      }
      const importerIsDev = isUnder(file, devRoot);
      const targetIsDev = isUnder(resolved.path, devRoot);
      const mainDynamicDevAllowance = toPosix(file) === mainEntry
        && edge.kind === 'dynamic-import'
        && targetIsDev;
      if (targetIsDev && !importerIsDev && !mainDynamicDevAllowance) {
        add('prod-imports-dev', id);
      }
      if (
        importerFeature !== null
        && isFeatureIndex(file, featuresRoot)
        && (edge.exportedNames.some((name) => /(?:^|[a-z])(Mock|Fixture)|^(mock|fixture)/.test(name))
          || isMockOrFixturePath(resolved.path) || targetIsDev || isUnder(resolved.path, testsRoot)
          || isUnder(resolved.path, join(srcRoot, 'fixtures')))
      ) {
        add('feature-index-exports-mock', id);
      }
      if (isUnder(file, join(srcRoot, 'components', 'primitives'))) {
        const layered = [featuresRoot, join(srcRoot, 'pages'), devRoot, join(srcRoot, 'state')];
        if (layered.some((dir) => isUnder(resolved.path, dir))) {
          add('primitives-layering', id);
        }
      }
      const baseLayer = isUnder(file, join(srcRoot, 'client')) || isUnder(file, join(srcRoot, 'platform'));
      if (baseLayer && targetFeature !== null) {
        add('base-layer-references-feature', id);
      }
    }
  }

  if (await pathExists(featuresRoot)) {
    await isDirectory(featuresRoot);
    const entries = (await readdir(featuresRoot, { withFileTypes: true }))
      .filter((entry) => entry.isDirectory())
      .map((entry) => entry.name)
      .sort();
    for (const name of entries) {
      const featureDir = join(featuresRoot, name);
      if (!(await pathExists(join(featureDir, 'index.ts')))) {
        add('feature-structure', `features/${name}:missing-index`);
      }
      const implDir = join(featureDir, 'impl');
      if (!(await pathExists(implDir))) {
        add('feature-structure', `features/${name}:missing-impl`);
      } else {
        const implFiles = (await listFilesRecursive(implDir))
          .filter((file) => SCAN_EXTENSIONS.includes(extname(file)));
        if (implFiles.length === 0) add('feature-structure', `features/${name}:empty-impl`);
      }
    }
  }

  return {
    violations: Object.fromEntries(RULE_KEYS.map((key) => [key, [...violations[key]].sort()])),
    hardFailures: hardFailures.sort(),
  };
}

async function readBaseline(path) {
  let raw;
  try {
    raw = await readFile(path, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') {
      return { ok: false, missing: true, error: `cannot read baseline ${rel(dirname(path), path)}: ${error.message}` };
    }
    return { ok: false, missing: false, error: `cannot read baseline ${rel(dirname(path), path)}: ${error.message}` };
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    return { ok: false, error: `baseline is not valid JSON: ${error.message}` };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, error: 'baseline must be a JSON object' };
  }
  if (parsed.version !== BASELINE_VERSION) {
    return { ok: false, error: `baseline version must be ${BASELINE_VERSION}` };
  }
  if (typeof parsed.rules !== 'object' || parsed.rules === null || Array.isArray(parsed.rules)) {
    return { ok: false, error: 'baseline.rules must be an object' };
  }
  const known = new Set(RULE_KEYS);
  for (const key of Object.keys(parsed.rules)) {
    if (!known.has(key)) {
      return { ok: false, error: `baseline.rules has unknown rule key: ${key}` };
    }
  }
  const rules = {};
  for (const key of RULE_KEYS) {
    const list = parsed.rules[key] ?? [];
    if (!Array.isArray(list) || list.some((item) => typeof item !== 'string')) {
      return { ok: false, error: `baseline.rules.${key} must be an array of strings` };
    }
    if (new Set(list).size !== list.length) {
      return { ok: false, error: `baseline.rules.${key} contains duplicate entries` };
    }
    rules[key] = [...list].sort();
  }
  return { ok: true, rules };
}

async function writeBaseline(path, violations) {
  const baseline = {
    version: BASELINE_VERSION,
    rules: Object.fromEntries(RULE_KEYS.map((key) => [key, violations[key]])),
  };
  await mkdir(dirname(path), { recursive: true });
  await writeFile(path, `${JSON.stringify(baseline, null, 2)}\n`, 'utf8');
}

function diffRule(current, baseline) {
  const currentSet = new Set(current);
  const baselineSet = new Set(baseline);
  const added = current.filter((id) => !baselineSet.has(id));
  const removed = baseline.filter((id) => !currentSet.has(id));
  return { added, removed };
}

async function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    process.stderr.write(`${error.message}\n${usage()}\n`);
    process.exitCode = 2;
    return;
  }
  if (options.help) {
    process.stdout.write(`${usage()}\n`);
    return;
  }

  const baselinePath = join(options.guiRoot, 'config', 'module-boundary-baseline.json');
  let scan;
  try {
    scan = await scanTree(options.guiRoot);
  } catch (error) {
    process.stderr.write(`module-boundaries: FAIL (cannot scan ${options.guiRoot}: ${error.message})\n`);
    process.exitCode = 1;
    return;
  }

  if (options.updateBaseline) {
    if (scan.hardFailures.length > 0) {
      process.stdout.write('module-boundaries: FAIL — hard failures cannot be baselined:\n');
      for (const failure of scan.hardFailures) process.stdout.write(`  ${failure}\n`);
      process.exitCode = 1;
      return;
    }
    const existing = await readBaseline(baselinePath);
    if (!existing.ok && !existing.missing) {
      process.stderr.write(`module-boundaries: FAIL (baseline unusable, refusing to update: ${existing.error})\n`);
      process.exitCode = 2;
      return;
    }
    const currentRules = existing.ok ? existing.rules : Object.fromEntries(RULE_KEYS.map((key) => [key, []]));
    if (!existing.ok) {
      process.stderr.write(`note: ${existing.error}; treating baseline as empty for creation\n`);
    }
    const diffs = RULE_KEYS.map((key) => diffRule(scan.violations[key], currentRules[key]));
    const added = diffs.flatMap((diff) => diff.added);
    const removed = diffs.flatMap((diff) => diff.removed);
    if (added.length > 0 && !options.acceptNew) {
      process.stdout.write('module-boundaries: FAIL — refusing to add new exceptions without --accept-new:\n');
      for (const id of added) process.stdout.write(`  NEW ${id}\n`);
      process.stdout.write('fix the violations, or re-run with --update-baseline --accept-new and review them in the diff.\n');
      process.exitCode = 1;
      return;
    }
    await writeBaseline(baselinePath, scan.violations);
    process.stdout.write(`baseline updated: ${baselinePath}\n`);
    process.stdout.write(`  pruned ${removed.length} stale exception(s), added ${added.length} new exception(s)\n`);
    for (const id of removed) process.stdout.write(`  PRUNED ${id}\n`);
    for (const id of added) process.stdout.write(`  ADDED ${id}\n`);
    return;
  }

  const baseline = await readBaseline(baselinePath);
  if (!baseline.ok) {
    process.stderr.write(`module-boundaries: FAIL (baseline unusable: ${baseline.error})\n`);
    process.exitCode = 2;
    return;
  }

  let failed = false;
  for (const failure of scan.hardFailures) {
    process.stdout.write(`HARD FAILURE ${failure}\n`);
    failed = true;
  }
  for (const key of RULE_KEYS) {
    const { added, removed } = diffRule(scan.violations[key], baseline.rules[key]);
    const total = scan.violations[key].length;
    if (added.length === 0 && removed.length === 0) {
      process.stdout.write(`PASS ${key}: ${total} registered exception(s)\n`);
      continue;
    }
    failed = true;
    process.stdout.write(`FAIL ${key}: ${total} current, ${added.length} new, ${removed.length} stale\n`);
    for (const id of added) process.stdout.write(`  NEW ${id}\n`);
    for (const id of removed) process.stdout.write(`  STALE ${id}\n`);
  }
  process.stdout.write(failed
    ? 'module-boundaries: FAIL — new violations must be fixed; eliminated ones require --update-baseline (see gui/docs/module-boundaries.md)\n'
    : 'module-boundaries: PASS\n');
  if (failed) process.exitCode = 1;
}

await main();
