/** WP6 dual-entry browser parity support.
 *
 * Decisions recorded here (docs/design/gui-dev-product-parity-2026-11-09.md §4.3, §5 WP6,
 * plus the 2026-11-09 artifact-separation amendment):
 * - Mock input contract, unified with WP1: URL query `hyprialScenario` (scenario id,
 *   default `default`, using WP1's `key=value;key=value` dimension format) and
 *   `hyprialFirstRun` (`true`/`false` for capabilities.firstRunWizardEnabled) are
 *   consumed by both entries.
 * - Production side of the gate uses WP5's wiring only: `npm run build:dist-parity`
 *   (WP2's `HYPRIAL_GUI_PARITY=1` build convention) produces the PROD+DCE
 *   `dist-parity/` test artifact and `scripts/serve.mjs --mode dist-parity` serves
 *   it; this helper adds no second build or serve path. The official `dist/` is
 *   never used or modified by the gate; the zero-mock artifact gate owns it.
 * - Floating dev tools hooks follow the WP2 implementation: `[data-testid="dev-tools-layer"]`
 *   portalled outside `#root`, `[data-testid="dev-tools-fab"]` collapsed by default
 *   (aria-expanded, accessible name 开发工具), `[data-testid="dev-tools-panel"]` surface.
 * - DOM normalization removes ONLY framework-generated ids (React/Radix useId tokens
 *   like `«r1»` / `:r1:` / React 19 `_r_1_`) consistently in both snapshots. Text, class, extra nodes,
 *   disabled states and route markers are never normalized away.
 * - The wall clock is frozen to a fixed epoch in both entries so any time-derived
 *   text is identical mock input, not a real-time difference.
 * - A snapshot is taken only after `document.fonts.ready` and two animation frames
 *   (2026-11-15). The web font ships as unicode-range subsets that load on first use;
 *   a snapshot taken while a subset is still loading measures the fallback font, and
 *   the two entries then differ by a pixel depending on which one had finished
 *   (seen as 452 vs 453 on the organization page, in CI and locally under load).
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const guiDir = resolve(fileURLToPath(new URL('../..', import.meta.url)));

export const PARITY_SCENARIO_PARAM = 'hyprialScenario';
export const PARITY_FIRST_RUN_PARAM = 'hyprialFirstRun';
export const PARITY_DIST_DIRNAME = 'dist-parity';
export const DEFAULT_FIXTURE_SCENARIO = 'default';

export const DEV_TOOLS_LAYER_SELECTOR = '[data-testid="dev-tools-layer"]';
export const DEV_TOOLS_TOGGLE_SELECTOR = '[data-testid="dev-tools-fab"]';
export const DEV_TOOLS_PANEL_SELECTOR = '[data-testid="dev-tools-panel"]';

export const PERSISTENT_DEV_SELECTORS = [
  '.dev-preview-frame',
  '.dev-preview-toolbar',
  '.dev-preview-footer',
  '.dev-data-source',
  '.dev-module-index',
];

const PROBE_SELECTORS = {
  layer: DEV_TOOLS_LAYER_SELECTOR,
  toggle: DEV_TOOLS_TOGGLE_SELECTOR,
  panel: DEV_TOOLS_PANEL_SELECTOR,
};

export const PARITY_FROZEN_EPOCH_MS = Date.UTC(2026, 10, 9, 8, 0, 0);

export function parityUrl(base, { scenario = DEFAULT_FIXTURE_SCENARIO, firstRun } = {}) {
  const url = new URL(base);
  url.searchParams.set(PARITY_SCENARIO_PARAM, scenario);
  if (firstRun !== undefined) {
    url.searchParams.set(PARITY_FIRST_RUN_PARAM, firstRun ? 'true' : 'false');
  }
  return url.toString();
}

export function buildParityArtifact() {
  const distParity = resolve(guiDir, PARITY_DIST_DIRNAME);
  const indexFile = resolve(distParity, 'index.html');
  if (existsSync(indexFile) && process.env.HYPRIAL_PARITY_REBUILD !== '1') {
    return { built: false, dir: distParity, indexFile };
  }
  const result = spawnSync('npm', ['run', 'build:dist-parity'], { cwd: guiDir, stdio: 'inherit' });
  if (result.status !== 0 || !existsSync(indexFile)) {
    throw new Error(`dist-parity artifact for the parity gate could not be built (npm run build:dist-parity status ${result.status})`);
  }
  return { built: true, dir: distParity, indexFile };
}

export async function startParityProductionServer() {
  process.env.HYPRIAL_GUI_HOME = guiDir;
  process.env.HYPRIAL_BACKEND_ENDPOINT = '';
  delete process.env.HYPRIAL_GUI_AUTH;
  delete process.env.HYPRIAL_GUI_SETUP;
  delete process.env.HYPRIAL_GUI_SETUP_CLI;
  const artifact = buildParityArtifact();
  const { startGuiServer } = await import('../../scripts/serve.mjs');
  const previousParity = process.env.HYPRIAL_GUI_PARITY;
  process.env.HYPRIAL_GUI_PARITY = '1';
  let server;
  try {
    server = await startGuiServer({ mode: 'dist-parity', host: '127.0.0.1', port: 0, strictPort: true });
  } finally {
    if (previousParity === undefined) delete process.env.HYPRIAL_GUI_PARITY;
    else process.env.HYPRIAL_GUI_PARITY = previousParity;
  }
  return { server, artifact };
}

export async function startParityServers() {
  process.env.HYPRIAL_GUI_HOME = guiDir;
  process.env.HYPRIAL_BACKEND_ENDPOINT = '';
  delete process.env.HYPRIAL_GUI_AUTH;
  delete process.env.HYPRIAL_GUI_SETUP;
  delete process.env.HYPRIAL_GUI_SETUP_CLI;
  const { startGuiServer } = await import('../../scripts/serve.mjs');
  const previousDevPreview = process.env.HYPRIAL_GUI_DEV_PREVIEW;
  process.env.HYPRIAL_GUI_DEV_PREVIEW = '1';
  let dev;
  try {
    dev = await startGuiServer({ mode: 'development', host: '127.0.0.1', port: 0, strictPort: true });
  } finally {
    if (previousDevPreview === undefined) delete process.env.HYPRIAL_GUI_DEV_PREVIEW;
    else process.env.HYPRIAL_GUI_DEV_PREVIEW = previousDevPreview;
  }
  let production;
  let artifact;
  try {
    ({ server: production, artifact } = await startParityProductionServer());
  } catch (error) {
    await dev.close();
    throw error;
  }
  return { dev, production, artifact };
}

export async function startParityDevServer() {
  process.env.HYPRIAL_GUI_HOME = guiDir;
  process.env.HYPRIAL_BACKEND_ENDPOINT = '';
  delete process.env.HYPRIAL_GUI_AUTH;
  delete process.env.HYPRIAL_GUI_SETUP;
  delete process.env.HYPRIAL_GUI_SETUP_CLI;
  const { startGuiServer } = await import('../../scripts/serve.mjs');
  const previousDevPreview = process.env.HYPRIAL_GUI_DEV_PREVIEW;
  process.env.HYPRIAL_GUI_DEV_PREVIEW = '1';
  let dev;
  try {
    dev = await startGuiServer({ mode: 'development', host: '127.0.0.1', port: 0, strictPort: true });
  } finally {
    if (previousDevPreview === undefined) delete process.env.HYPRIAL_GUI_DEV_PREVIEW;
    else process.env.HYPRIAL_GUI_DEV_PREVIEW = previousDevPreview;
  }
  return dev;
}

export async function openParityPage(browser, { url, viewport, beforeNavigate }) {
  const context = await browser.newContext({ viewport, deviceScaleFactor: 1 });
  const page = await context.newPage();
  await context.addInitScript((epoch) => {
    class FrozenDate extends Date {
      constructor(...args) {
        if (args.length === 0) super(epoch);
        else super(...args);
      }
      static now() {
        return epoch;
      }
    }
    window.Date = FrozenDate;
  }, PARITY_FROZEN_EPOCH_MS);
  if (beforeNavigate) await beforeNavigate(page);
  await page.goto(url, { waitUntil: 'networkidle' });
  await page.waitForFunction(() => document.getElementById('root')?.children.length > 0, undefined, { timeout: 15_000 });
  return { context, page };
}

const GENERATED_ID_TOKEN = /^(?:[:«][rR][0-9A-Za-z]{0,31}[:»]|_[rR]_[0-9A-Za-z]{1,31}_)$/;
const NORMALIZED_ID_ATTRS = [
  'id',
  'for',
  'aria-controls',
  'aria-labelledby',
  'aria-describedby',
  'aria-owns',
  'aria-activedescendant',
  'aria-errormessage',
  'headers',
];

async function settleFonts(page) {
  await page.evaluate(async () => {
    await document.fonts.ready;
    await new Promise((resolveFrame) => requestAnimationFrame(() => requestAnimationFrame(resolveFrame)));
  });
}

export async function captureParitySnapshot(page) {
  await settleFonts(page);
  return page.evaluate(({ idAttrs, tokenSource }) => {
    const tokenPattern = new RegExp(tokenSource);
    const root = document.getElementById('root');
    if (!root) return { missingRoot: true };
    const generatedIds = new Map();
    let generatedCounter = 0;
    const placeholderFor = (token) => {
      let placeholder = generatedIds.get(token);
      if (placeholder === undefined) {
        placeholder = `devpar-id-${generatedCounter}`;
        generatedCounter += 1;
        generatedIds.set(token, placeholder);
      }
      return placeholder;
    };
    const isGeneratedList = (value) => value.split(' ').every((part) => tokenPattern.test(part));
    for (const element of root.querySelectorAll('*')) {
      for (const attr of idAttrs) {
        const value = element.getAttribute(attr);
        if (value !== null && value !== '' && isGeneratedList(value)) {
          element.setAttribute(attr, value.split(' ').map(placeholderFor).join(' '));
        }
      }
    }
    const rectOf = (element) => {
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      return [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)];
    };
    const activeNav = document.querySelector('.nav-item[data-nav][aria-current="page"]');
    const pageElement = root.querySelector('[data-page]');
    return {
      route: {
        nav: activeNav ? activeNav.getAttribute('data-nav') : null,
        page: pageElement ? pageElement.getAttribute('data-page') : null,
        hash: location.hash,
      },
      dom: root.outerHTML,
      layout: {
        viewport: [window.innerWidth, window.innerHeight],
        scroll: [document.documentElement.scrollWidth, document.documentElement.scrollHeight],
        root: rectOf(root),
        app: rectOf(root.querySelector('.app')),
        window: rectOf(root.querySelector('.window')),
        shell: rectOf(root.querySelector('.shell')),
        rail: rectOf(root.querySelector('.rail')),
        nav: rectOf(root.querySelector('.nav')),
        content: rectOf(root.querySelector('.content')),
        page: rectOf(pageElement),
      },
    };
  }, { idAttrs: NORMALIZED_ID_ATTRS, tokenSource: GENERATED_ID_TOKEN.source });
}

function firstDiffContext(left, right) {
  const length = Math.min(left.length, right.length);
  let index = 0;
  while (index < length && left[index] === right[index]) index += 1;
  const from = Math.max(0, index - 80);
  const to = index + 120;
  return {
    index,
    lengthDelta: left.length - right.length,
    left: left.slice(from, to),
    right: right.slice(from, to),
  };
}

export function snapshotMismatches(expected, actual, label) {
  const problems = [];
  if (expected.missingRoot || actual.missingRoot) {
    problems.push(`${label}: #root is missing (dev missing=${Boolean(expected.missingRoot)}, production missing=${Boolean(actual.missingRoot)})`);
    return problems;
  }
  if (JSON.stringify(actual.route) !== JSON.stringify(expected.route)) {
    problems.push(`${label}: route differs (nav/page/hash) — dev=${JSON.stringify(expected.route)} production=${JSON.stringify(actual.route)}`);
  }
  for (const key of Object.keys(expected.layout)) {
    if (JSON.stringify(actual.layout[key]) !== JSON.stringify(expected.layout[key])) {
      problems.push(`${label}: layout.${key} differs — dev=${JSON.stringify(expected.layout[key])} production=${JSON.stringify(actual.layout[key])}`);
    }
  }
  if (actual.dom !== expected.dom) {
    const diff = firstDiffContext(expected.dom, actual.dom);
    problems.push(
      `${label}: normalized #root DOM differs at index ${diff.index} (length dev=${expected.dom.length}, production=${actual.dom.length}, delta=${diff.lengthDelta})\n` +
      `dev        ...${diff.left}...\n` +
      `production ...${diff.right}...`,
    );
  }
  return problems;
}

export function assertSnapshotsEqual(expected, actual, label) {
  const problems = snapshotMismatches(expected, actual, label);
  assert.deepEqual(problems, [], `parity mismatches (${problems.length}):\n${problems.join('\n')}`);
}

export async function assertNoPersistentDevChromeInProductRoot(page, label) {
  const found = await page.evaluate((selectors) => {
    const root = document.getElementById('root');
    if (!root) return [];
    return selectors.filter((selector) => root.querySelector(selector) !== null);
  }, PERSISTENT_DEV_SELECTORS);
  assert.deepEqual(
    found,
    [],
    `${label}: persistent development chrome must be removed from the product tree (#root), found: ${found.join(', ')}`,
  );
}

export async function probeDevTools(page) {
  return page.evaluate((selectors) => {
    const productRoot = document.getElementById('root');
    const layer = document.querySelector(selectors.layer);
    const toggle = document.querySelector(selectors.toggle);
    const panel = document.querySelector(selectors.panel);
    const rectOf = (element) => {
      if (!element) return null;
      const rect = element.getBoundingClientRect();
      return [Math.round(rect.left), Math.round(rect.top), Math.round(rect.width), Math.round(rect.height)];
    };
    const visible = (element) => {
      if (!element) return false;
      const style = getComputedStyle(element);
      return element.getClientRects().length > 0 && style.visibility !== 'hidden' && style.display !== 'none';
    };
    return {
      toolsRootPresent: layer !== null,
      toolsOutsideProductRoot: layer && productRoot ? !productRoot.contains(layer) : null,
      toolsRootRect: rectOf(layer),
      productRootRect: rectOf(productRoot),
      toggleVisible: visible(toggle),
      toggleAriaExpanded: toggle ? toggle.getAttribute('aria-expanded') : null,
      toggleFocused: toggle ? document.activeElement === toggle : null,
      panelVisible: visible(panel),
      panelRect: rectOf(panel),
    };
  }, PROBE_SELECTORS);
}

export async function assertFloatingToolsAbsent(page, label) {
  const probe = await probeDevTools(page);
  assert.equal(
    probe.toolsRootPresent,
    false,
    `${label}: production must not mount the development tools layer (${DEV_TOOLS_LAYER_SELECTOR})`,
  );
  assert.equal(
    probe.toggleVisible,
    false,
    `${label}: production must not render the floating ball (${DEV_TOOLS_TOGGLE_SELECTOR})`,
  );
  const devChrome = await page.evaluate((selectors) => selectors
    .filter((selector) => document.querySelector(selector) !== null), PERSISTENT_DEV_SELECTORS);
  assert.deepEqual(devChrome, [], `${label}: production must not render persistent development chrome`);
}

export async function waitForVisible(page, locator, timeoutMs, message) {
  try {
    await locator.waitFor({ state: 'visible', timeout: timeoutMs });
  } catch (error) {
    const bodyText = await page.evaluate(() => document.body.innerText.slice(0, 400));
    assert.fail(`${message}\nlocator: ${locator}\nbody excerpt: ${JSON.stringify(bodyText)}`);
  }
}
