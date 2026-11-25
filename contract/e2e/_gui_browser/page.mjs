/**
 * Browser half of a browser-driven E2E scenario (E2E-032, 2026-11-21).
 *
 * Decision record:
 * - The browser is the one `gui/tests/support/browser.mjs` selects
 *   (HYPRIAL_BROWSER_EXECUTABLE, installed Chrome, then Playwright's
 *   Chromium) and `playwright-core` is resolved from `gui/package.json`, so
 *   the version is the one the GUI lockfile pins. It is started with
 *   `launchServer` (detached in its own process group) so that its pid can
 *   be validated as a positive integer and written atomically via
 *   `${pidFile}.tmp` + rename to `pidFile` before `chromium.connect`,
 *   allowing the Python runner to observe its live birth identity, PPID,
 *   and detached PGID for leftover tracking and cleanup.
 * - The one-time URL is read from the host's private `launch.url` here,
 *   verified to parse as a loopback HTTP URL matching `expectedOrigin`, and
 *   never printed: every error message leaving this module goes through
 *   `scrub`. After the page opens, the handoff file must be gone and the page
 *   URL must stay on `expectedOrigin` with no fragment (the same checks as
 *   `startActualGui` and `runBrowser` in
 *   gui/tests/daemon-request-host/browser-acceptance.mjs, restated here because
 *   that file exports nothing).
 * - Requests are observed with `page.on('request')` / `page.on('response')`
 *   on `/daemon/request/<method>` of the host origin, plus boolean boundary
 *   flags (`crossOrigin`, `hasAuthorization`, `hasGrantInUrl`) across http/https
 *   requests. Only the method name, the `org` parameter, the phase label, the
 *   HTTP status, the envelope's `ok`/`dispatch`/error code, boolean boundary
 *   flags and a scenario-chosen summary are kept; no header, cookie, body,
 *   URL or session value is recorded.
 */
import { access, readFile, rename, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { browserExecutable } from '../../../gui/tests/support/browser.mjs';

const GUI_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../../gui');
const { chromium } = createRequire(join(GUI_ROOT, 'package.json'))('playwright-core');
const REQUEST_PREFIX = '/daemon/request/';
const CONNECTED_READY = 'main#main-content';

export function scrub(text) {
  return String(text)
    .replace(/(grant=)[^\s"'&]+/g, '$1<redacted>')
    .replace(/[A-Za-z0-9_-]{32,}/g, '<redacted>');
}

export function delay(milliseconds) {
  return new Promise((resolveDelay) => setTimeout(resolveDelay, milliseconds));
}

export async function waitFor(predicate, label, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await delay(100);
  }
  throw new Error(`timed out waiting for ${label}`);
}

async function exists(path) {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

export async function launchBrowser(pidFile) {
  const server = await chromium.launchServer({
    executablePath: browserExecutable(),
    headless: true,
    args: ['--no-sandbox'],
  });
  const pid = server.process()?.pid;
  if (typeof pid !== 'number' || !Number.isInteger(pid) || pid <= 0) {
    await server.close().catch(() => undefined);
    throw new Error(`chromium.launchServer returned an invalid pid: ${pid}`);
  }
  try {
    const tmpPidFile = `${pidFile}.tmp`;
    await writeFile(tmpPidFile, `${pid}\n`);
    await rename(tmpPidFile, pidFile);
    const browser = await chromium.connect(server.wsEndpoint());
    return { browser, server, pid };
  } catch (error) {
    await server.close().catch(() => undefined);
    throw error;
  }
}

/** Record every daemon request of `page` on `origin`, labelled with the current phase. */
export function recordDaemonRequests(page, origin, summarize = () => undefined) {
  const entries = [];
  const pending = new Map();
  const boundary = {
    crossOrigin: false,
    hasAuthorization: false,
    hasGrantInUrl: false,
  };
  const recorder = { entries, phase: 'open', errors: [], boundary };
  page.on('pageerror', (error) => recorder.errors.push(scrub(error.message).slice(0, 300)));
  page.on('request', (request) => {
    const url = new URL(request.url());
    if (url.protocol === 'http:' || url.protocol === 'https:') {
      if (url.origin !== origin) boundary.crossOrigin = true;
      const headers = request.headers();
      if (Object.hasOwn(headers, 'authorization')) boundary.hasAuthorization = true;
      if (url.search.includes('grant=') || url.hash.includes('grant=')) {
        boundary.hasGrantInUrl = true;
      }
    }
    if (url.origin !== origin || !url.pathname.startsWith(REQUEST_PREFIX)) return;
    let org = null;
    try {
      const params = JSON.parse(request.postData() ?? '{}');
      if (params !== null && typeof params === 'object' && typeof params.org === 'string') org = params.org;
    } catch {
      org = null;
    }
    const entry = {
      seq: entries.length,
      phase: recorder.phase,
      method: decodeURIComponent(url.pathname.slice(REQUEST_PREFIX.length)),
      org,
      status: null,
    };
    entries.push(entry);
    pending.set(request, entry);
  });
  page.on('requestfailed', (request) => {
    const entry = pending.get(request);
    if (entry) entry.status = 'failed';
  });
  page.on('response', async (response) => {
    const entry = pending.get(response.request());
    if (!entry) return;
    let body = null;
    try {
      body = await response.json();
    } catch {
      body = null;
    }
    entry.ok = body?.ok ?? null;
    entry.dispatch = body?.dispatch ?? null;
    entry.errorCode = body?.error?.code ?? null;
    const summary = body?.ok === true ? summarize(entry.method, body.result) : undefined;
    if (summary !== undefined) entry.summary = summary;
    entry.status = response.status();
  });
  return recorder;
}

/** Wait until a recorded request matching `match` has been answered. */
export async function waitForAnswer(recorder, match, label, timeoutMs = 30_000) {
  await waitFor(
    () => recorder.entries.some((entry) => match(entry) && entry.status !== null),
    label,
    timeoutMs,
  );
}

export async function openHandoff(page, handoffPath, expectedOrigin) {
  const launchUrl = (await readFile(handoffPath, 'utf8')).trim();
  let parsedLaunch;
  try {
    parsedLaunch = new URL(launchUrl);
  } catch {
    throw new Error('the handoff URL could not be parsed');
  }
  const expected = new URL(expectedOrigin);
  if (
    parsedLaunch.protocol !== 'http:'
    || parsedLaunch.origin !== expected.origin
    || !['127.0.0.1', 'localhost', '::1', '[::1]'].includes(parsedLaunch.hostname)
  ) {
    throw new Error('the handoff URL origin did not match the expected loopback origin');
  }
  try {
    await page.goto(launchUrl, { waitUntil: 'domcontentloaded' });
  } catch (error) {
    throw new Error(`opening the handoff failed: ${scrub(error.message)}`);
  }
  await page.locator(CONNECTED_READY).waitFor();
  await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
  const currentUrl = new URL(page.url());
  if (currentUrl.origin !== expected.origin) {
    throw new Error('the page left the expected loopback origin after the handoff');
  }
  if (currentUrl.hash !== '') throw new Error('the page kept a URL fragment after the handoff');
}

/** Run `body(config)` and print exactly one JSON result line; `--probe` checks the browser. */
export async function runMain(body) {
  if (process.argv[2] === '--probe') {
    try {
      browserExecutable();
      console.log(JSON.stringify({ browser: true }));
    } catch (error) {
      console.log(JSON.stringify({ browser: false, error: scrub(error.message) }));
    }
    return;
  }
  const config = JSON.parse(await readFile(process.argv[2], 'utf8'));
  const state = { result: {} };
  let launched = null;
  try {
    launched = await launchBrowser(config.pidFile);
    await body(config, launched.browser, state.result);
    console.log(JSON.stringify({ ok: true, ...state.result }));
  } catch (error) {
    console.log(JSON.stringify({ ok: false, error: scrub(error?.stack ?? error).slice(0, 3000), ...state.result }));
    process.exitCode = 1;
  } finally {
    if (launched !== null) {
      await launched.browser.close().catch(() => undefined);
      await launched.server.close().catch(() => undefined);
    }
  }
}

export function inBrowserPage(module) {
  return fileURLToPath(module) === resolve(process.argv[1] ?? '');
}

if (inBrowserPage(import.meta.url)) await runMain(async () => {
  throw new Error('page.mjs only answers --probe; run a scenario script');
});
