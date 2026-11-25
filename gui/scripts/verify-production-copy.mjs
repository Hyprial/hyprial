// Decisions (gui dev/product parity, docs/design/gui-dev-product-parity-2026-11-09.md):
// - The artifact gate fails closed: a missing bundle directory or a missing
//   dist/index.html is a hard failure, never a silent skip.
// - Markers cover dev tooling identifiers and dev-only CSS class families in
//   addition to the existing fixture/scenario copy markers, so any dev tool,
//   fixture composition, scenario copy or dev CSS that reaches the production
//   bundle fails the gate.
// - The parity URL parameters (hyprialFixture/hyprialScenario/hyprialFirstRun)
//   and the mock composition symbols belong to the dist-parity test artifact
//   only; the release dist must never carry them, and this gate scans only the
//   release dist, never dist-parity.
// - `demo` as a backtick template literal is the surviving first-run demo
//   scenario branch (measured in an integration release bundle at
//   `firstRunScenario=D?`demo`:`live`` and `t===`demo``): the demo scenario is
//   fixture composition and must be DCE'd out of the release. The backtick
//   shape is required precisely so ordinary product words such as
//   "demonstration" or a plain quoted "demo" never trip the gate.
// - Merge-forward (fix/gui-dev-merge-forward, 2026-11-10): the release dist
//   also passes the dev-side production artifact gate before the copy scan:
//   dist and dist/assets exist and are non-empty, every JS asset is non-empty,
//   index.html is non-empty and its src/href asset references resolve to
//   non-empty files; demoPanelController joins the forbidden marker list.
//   main() uses verifyArtifact in place of the earlier standalone entrypoint
//   stat, which it strictly subsumes while staying fail closed.
// - Developer tool layer (PR 4b-G, 2026-11-15): the release ships the tool
//   layer as a chunk under dist/dev-tools/, emitted only when the host reports
//   the capability. The gate therefore judges three scopes. (1) The product
//   closure is index.html and every text file outside dev-tools/: the full
//   forbidden list, plus index.html must not reference dev-tools/. (2)
//   dev-tools/ outside docs/ runs the same list minus TOOL_LAYER_OWN, the
//   names of the tool layer itself, so mock, fixture URL and scenario data are
//   still forbidden there. (3) dev-tools/docs/ is third-party: it is not
//   text-scanned; its file set is exact and every file's sha256 equals the
//   file in node_modules/swagger-ui-dist. A tree without dev-tools/ passes, so
//   the layer can be reverted without touching the gate.
import { createHash } from 'node:crypto';
import { readFile, readdir, stat } from 'node:fs/promises';
import { dirname, extname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const FORBIDDEN_COPY = [
  { label: 'reference-mock-proxy', pattern: /\/api\/reference\/mock-/u },
  { label: 'reference-sentinel', pattern: /example-not-a-credential|ZXhhbXBsZS1ub3QtYS1jcmVkZW50aWFs/u },
  { label: 'reference-mock-service', pattern: /MOCK_SCENARIO_MISSING/u },
  { label: 'dev-01', pattern: /dev-0[1-6]/u },
  { label: 'fixture', pattern: /fixture/iu },
  { label: '演示', pattern: /演示/u },
  { label: '样例', pattern: /样例/u },
  { label: '占位', pattern: /占位/u },
  { label: '开发预览', pattern: /开发预览/u },
  { label: '本地测试数据', pattern: /本地测试数据/u },
  { label: '场景', pattern: /场景/u },
  { label: 'dev-data-source', pattern: /dev-data-source/u },
  { label: 'dev-module-index', pattern: /dev-module-index/u },
  { label: 'dev-module-dock', pattern: /dev-module-dock/u },
  { label: 'dev-module-entry', pattern: /dev-module-entry/u },
  { label: 'dev-preview-frame', pattern: /dev-preview-frame/u },
  { label: 'dev-module-scroll-hint', pattern: /dev-module-scroll-hint/u },
  { label: 'studio-mac', pattern: /studio-mac/u },
  { label: 'travel-mac', pattern: /travel-mac/u },
  { label: '合成系统', pattern: /合成系统/u },
  { label: '本地静态演示', pattern: /本地静态演示/u },
  { label: '不是实时数据', pattern: /不是实时数据/u },
  { label: 'edge-demo-1', pattern: /edge-demo-1/u },
  { label: '2026-10-22 10:15', pattern: /2026-10-22 10:15/u },
  { label: 'GUI-LINK-001', pattern: /GUI-LINK-001/u },
  { label: 'GUI-ORG-001', pattern: /GUI-ORG-001/u },
  { label: 'dev-local', pattern: /dev-local/u },
  { label: '切换为受邀成员', pattern: /切换为受邀成员/u },
  { label: '切换为创建者', pattern: /切换为创建者/u },
  { label: 'Mock', pattern: /Mock/u },
  { label: '接口未接通', pattern: /接口未接通/u },
  { label: '邀请码演示', pattern: /邀请码演示/u },
  { label: '服务接入前', pattern: /服务接入前/u },
  { label: '不会改变账号', pattern: /不会改变账号/u },
  { label: '服务尚未接入', pattern: /服务尚未接入/u },
  { label: '未执行任何操作', pattern: /未执行任何操作/u },
  { label: '不会更改连接状态', pattern: /不会更改连接状态/u },
  { label: 'add-failure', pattern: /add-failure/u },
  { label: 'authorize-failure', pattern: /authorize-failure/u },
  { label: 'dev-token', pattern: /__HYPRIAL_DEV/u },
  { label: 'dev-preview-toolbar', pattern: /dev-preview-toolbar/u },
  { label: 'dev-preview-footer', pattern: /dev-preview-footer/u },
  { label: 'dev-session', pattern: /dev-session/u },
  { label: 'dev-scenario', pattern: /dev-scenario/u },
  { label: '隔离预览', pattern: /隔离预览/u },
  { label: 'DevPreviewApp', pattern: /DevPreviewApp/u },
  { label: 'DevModuleIndex', pattern: /DevModuleIndex/u },
  { label: 'DevScenarioSelector', pattern: /DevScenarioSelector/u },
  { label: 'devLabels', pattern: /devLabels/u },
  { label: 'hyprialFixture', pattern: /hyprialFixture/u },
  { label: 'hyprialScenario', pattern: /hyprialScenario/u },
  { label: 'hyprialFirstRun', pattern: /hyprialFirstRun/u },
  { label: 'mockComposition', pattern: /mockComposition/u },
  { label: 'mock-composition', pattern: /mock-composition/u },
  { label: 'demo-scenario', pattern: /`demo`/u },
  { label: 'demo', pattern: /demoPanelController/u },
];
const TEXT_EXTENSIONS = new Set(['.css', '.html', '.js', '.json', '.map', '.txt']);

export const TOOL_LAYER_OWN = new Set([
  'dev-01',
  '样例',
  'dev-module-index',
  'dev-module-dock',
  'dev-module-entry',
  'dev-module-scroll-hint',
  'dev-data-source',
  'dev-scenario',
]);

const TOOL_LAYER_DIRECTORY = 'dev-tools';
const DOCS_DIRECTORY = 'docs';
export const VENDORED_DOCS_FILES = ['LICENSE', 'NOTICE', 'swagger-ui-bundle.js', 'swagger-ui.css'];
const DEFAULT_VENDOR_DIRECTORY = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../node_modules/swagger-ui-dist',
);

export function findProductionCopyViolations(copy) {
  return FORBIDDEN_COPY
    .filter(({ pattern }) => pattern.test(copy))
    .map(({ label }) => label);
}

export function findToolLayerCopyViolations(copy) {
  return findProductionCopyViolations(copy).filter((label) => !TOOL_LAYER_OWN.has(label));
}

async function readTextFiles(directory, skip) {
  const entries = await readdir(directory, { withFileTypes: true });
  const chunks = [];
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (skip(path)) continue;
    if (entry.isDirectory()) {
      chunks.push(await readTextFiles(path, skip));
    } else if (TEXT_EXTENSIONS.has(extname(entry.name))) {
      chunks.push(await readFile(path, 'utf8'));
    }
  }
  return chunks.join('\n');
}

async function directoryExists(path) {
  const info = await stat(path).catch(() => null);
  return info?.isDirectory() === true;
}

async function sha256(path) {
  return createHash('sha256').update(await readFile(path)).digest('hex');
}

async function docsProblems(docsDirectory, vendorDirectory) {
  const problems = [];
  const entries = await readdir(docsDirectory, { withFileTypes: true });
  for (const entry of entries) {
    if (!entry.isFile() || !VENDORED_DOCS_FILES.includes(entry.name)) {
      problems.push(`dev-tools/docs/${entry.name} is not part of the vendored Swagger UI file set`);
    }
  }
  for (const name of VENDORED_DOCS_FILES) {
    const shipped = join(docsDirectory, name);
    if (!entries.some((entry) => entry.isFile() && entry.name === name)) {
      problems.push(`dev-tools/docs/${name} is missing`);
      continue;
    }
    const vendored = await sha256(join(vendorDirectory, name)).catch(() => null);
    if (vendored === null) {
      problems.push(`cannot verify dev-tools/docs/${name}: ${join(vendorDirectory, name)} is unreadable`);
    } else if (vendored !== await sha256(shipped)) {
      problems.push(`dev-tools/docs/${name} differs from the vendored swagger-ui-dist file`);
    }
  }
  return problems;
}

export async function verifyArtifactScopes(directory, { vendorDirectory = DEFAULT_VENDOR_DIRECTORY } = {}) {
  const toolDirectory = join(directory, TOOL_LAYER_DIRECTORY);
  const docsDirectory = join(toolDirectory, DOCS_DIRECTORY);
  const problems = [];

  const productMarkers = findProductionCopyViolations(await readBundleText(directory));
  if (productMarkers.length > 0) {
    problems.push(`product closure contains forbidden markers: ${productMarkers.join(', ')}`);
  }
  const html = await readFile(join(directory, 'index.html'), 'utf8').catch(() => '');
  if (html.includes(`${TOOL_LAYER_DIRECTORY}/`)) {
    problems.push(`index.html references ${TOOL_LAYER_DIRECTORY}/: the product entry must not know the tool layer`);
  }

  if (!await directoryExists(toolDirectory)) return problems;
  const toolMarkers = findToolLayerCopyViolations(
    await readTextFiles(toolDirectory, (path) => path === docsDirectory),
  );
  if (toolMarkers.length > 0) {
    problems.push(`dev-tools contains forbidden markers: ${toolMarkers.join(', ')}`);
  }
  if (await directoryExists(docsDirectory)) {
    problems.push(...await docsProblems(docsDirectory, vendorDirectory));
  }
  return problems;
}

export async function readBundleText(directory) {
  let directoryStat;
  try {
    directoryStat = await stat(directory);
  } catch {
    throw new Error(`Production bundle missing: ${directory} (run npm run build first; missing artifacts must FAIL, not skip)`);
  }
  if (!directoryStat.isDirectory()) {
    throw new Error(`Production bundle missing: ${directory} is not a directory`);
  }
  const toolDirectory = join(directory, TOOL_LAYER_DIRECTORY);
  return readTextFiles(directory, (path) => path === toolDirectory);
}

async function requireDirectory(path) {
  const info = await stat(path).catch(() => {
    throw new Error(`Missing production directory: ${path}`);
  });
  if (!info.isDirectory()) throw new Error(`Not a production directory: ${path}`);
  return readdir(path, { withFileTypes: true });
}

async function requireNonemptyFile(path) {
  const info = await stat(path).catch(() => {
    throw new Error(`Missing production file: ${path}`);
  });
  if (!info.isFile() || info.size === 0) {
    throw new Error(`Empty or invalid production file: ${path}`);
  }
  return readFile(path, 'utf8');
}

async function verifyArtifact(directory) {
  await requireDirectory(directory);
  const assetsDirectory = join(directory, 'assets');
  const assets = await requireDirectory(assetsDirectory);
  if (assets.length === 0) throw new Error(`Empty production assets: ${assetsDirectory}`);

  const html = await requireNonemptyFile(join(directory, 'index.html'));
  const javascript = assets.filter((entry) => entry.isFile() && extname(entry.name) === '.js');
  if (javascript.length === 0) throw new Error(`Missing production JS asset: ${assetsDirectory}`);
  await Promise.all(javascript.map((entry) => requireNonemptyFile(join(assetsDirectory, entry.name))));

  const references = [...html.matchAll(/(?:src|href)=["'](?:\.\/|\/)?(assets\/[^"']+)["']/gu)];
  const entryScripts = references.filter(([, path]) => extname(path) === '.js');
  if (entryScripts.length === 0) throw new Error(`Missing JS entry reference: ${join(directory, 'index.html')}`);
  await Promise.all(references.map(([, path]) => requireNonemptyFile(join(directory, path))));
}

async function main() {
  const directory = resolve(process.argv[2] ?? 'dist');
  await verifyArtifact(directory);
  const problems = await verifyArtifactScopes(directory);
  if (problems.length > 0) {
    throw new Error(`Production artifact gate failed:\n- ${problems.join('\n- ')}`);
  }
  const scopes = ['product closure'];
  if (await directoryExists(join(directory, TOOL_LAYER_DIRECTORY))) scopes.push('dev-tools');
  if (await directoryExists(join(directory, TOOL_LAYER_DIRECTORY, DOCS_DIRECTORY))) scopes.push('dev-tools/docs');
  process.stdout.write(`Production copy gate passed: 0 forbidden markers (scopes: ${scopes.join(', ')}).\n`);
}

const isMain = process.argv[1]
  ? pathToFileURL(resolve(process.argv[1])).href === import.meta.url
  : false;
if (isMain) {
  await main();
}
