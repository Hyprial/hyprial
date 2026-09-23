import { execFileSync } from 'node:child_process';
import { appendFileSync, readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

// Only known prose locations are exempt. Runtime templates/skills and unknown
// file types are executable inputs until proven otherwise.
export function isDocumentation(path) {
  if (path === 'docs/squire/SKILL-template.md' || path === 'docs/cutover-runbook.md') return false;
  if (['.forgejo/workflows/README.md', 'browser-tests/README.md',
    'sidecar/hyprial-tsnet/README.md', 'sidecar/hyprial-tsnet/e2e/README.md', 'sidecar/h2b-tsnet/README.md', 'sidecar/h2b-tsnet/e2e/README.md'].includes(path)) return true;
  return /^(README(?:\.[\w-]+)?\.(?:md|rst|txt)|README|(?:CHANGELOG|CONTRIBUTING|AUTHORS|LICENSE)(?:\.md|\.rst|\.txt)?)$/.test(path)
    || /^(docs|notes)\/.*\.(md|rst|adoc)$/.test(path);
}

export function classify(paths, project) {
  if (!['h2b', 'hyprial', 'gui', 'all'].includes(project)) throw new Error('unknown CI project');
  if (project === 'all') return classifyRepository(paths);
  if (project === 'hyprial') {
    const result = classifyRepository(paths);
    return { unit: result.python, browser: false, gui_integration: result.gui_integration, reason: result.reason };
  }
  // git diff reports repository-root paths even when invoked from src/gui.
  // Keep unknown/shared backend inputs conservative; only strip our package prefix.
  const normalized = project === 'gui'
    ? paths.map(path => path.startsWith('src/gui/') ? path.slice('src/gui/'.length) : path)
    : paths;
  const code = normalized.filter(path => !isDocumentation(path));
  if (!code.length) return { unit: false, browser: false, reason: 'documentation-only or empty change' };
  if (project === 'h2b') return { unit: true, browser: false, reason: 'H2B code or build input changed' };
  let unit = false, browser = false;
  for (const path of code) {
    if (/^(client\/gui-|packages\/gui-layout\/|scripts\/verify-gui-|tests\/gui-.*\.browser\.mjs$)/.test(path)) { unit = true; browser = true; }
    else if (/^(client\/|static\/|imskin-(?:host-)?plugin\.js$|tests\/)/.test(path)) unit = true;
    else { unit = true; browser = true; }
  }
  return { unit, browser, reason: 'checks selected from complete change range' };
}

// Keep this policy in the GUI package so standalone source archives and the
// monorepo share one classifier without depending on files outside the archive.
const gates = ['python', 'gui_integration', 'unit', 'browser', 'codex', 'dsh', 'package'];
function allChecks(reason) {
  return { ...Object.fromEntries(gates.map(key => [key, true])), reason };
}
// Audited Node-only CLI consumers, not a directory-wide GUI exemption.
// All GUI gates stay enabled; Python CLI/home and GUI integration still run.
// See docs/ci-gui-native-scope.md before extending this inventory.
const nativeCliPaths = new Set([
  'src/gui/h2b-cli-capabilities.mjs',
  'src/gui/h2b-control-bridge.mjs',
  'src/gui/integration/adapter-enrollment.mjs',
  'src/gui/integration/org-cli-runner.mjs',
  'src/gui/integration/hyprial-cli.mjs',
  'src/gui/tests/adapter-enrollment.test.mjs',
  'src/gui/tests/console-management.test.mjs',
  'src/gui/tests/h2b-cli-capabilities.test.mjs',
  'src/gui/tests/h2b-control-bridge.test.mjs',
  'src/gui/tests/hyprial-cli.test.mjs',
]);
export function classifyRepository(paths) {
  const code = paths.filter(path => !isDocumentation(path.startsWith('src/gui/') ? path.slice(8) : path));
  if (!code.length) return { ...Object.fromEntries(gates.map(key => [key, false])), reason: 'documentation-only or empty change' };
  // Exempt only known browser source file types, never entire directories.
  // New scripts, providers, dependencies, install code and protocols run all.
  const browserOnly = path => /^src\/gui\/(?:client\/[^/]+\.(?:js|css)|static\/(?:client|host)\.js)$/.test(path);
  if (code.some(path => nativeCliPaths.has(path)) && code.every(path => browserOnly(path) || nativeCliPaths.has(path))) {
    return { ...allChecks('audited GUI native CLI consumers; retain all GUI gates and Python CLI/home integration'), python: false };
  }
  if (!code.every(browserOnly)) return allChecks('shared, build, runtime or unknown input changed; run all checks');
  const gui = classify(code, 'gui');
  return { python: false, gui_integration: true, unit: gui.unit,
    browser: gui.browser, codex: true, dsh: true, package: true,
    reason: 'browser-only change; retain GUI and Python GUI integration checks' };
}

function git(args, cwd) {
  return execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], timeout: 60000 });
}

export function changedPaths(event, eventName, cwd, fetch = true) {
  let base, head;
  if (eventName === 'pull_request') {
    base = event.pull_request?.base?.sha;
    head = event.pull_request?.head?.sha;
  } else if (eventName === 'push') {
    base = event.before;
    head = event.after;
  } else throw new Error('manual or unsupported event');
  for (const sha of [base, head]) {
    if (!/^[a-f0-9]{40}$/.test(sha ?? '') || /^0+$/.test(sha)) throw new Error('missing comparison commit');
  }
  if (fetch) git(['fetch', '--no-tags', '--depth=128', 'origin', base, head], cwd);
  if (eventName === 'pull_request') base = git(['merge-base', base, head], cwd).trim();
  // --no-renames includes the old AND new path, including code renamed to docs.
  return git(['diff', '--no-renames', '--name-only', '-z', base, head, '--'], cwd).split('\0').filter(Boolean);
}

export function select(event, eventName, project, cwd, fetch = true) {
  // The integration-to-release boundary never inherits a PR subset decision,
  // including docs-only batches. Keep every existing gate name intact.
  if (eventName === 'pull_request' && ['dev', 'main'].includes(event.pull_request?.base?.ref)) {
    const reason = 'PR targets main/dev; full release-boundary validation';
    if (project === 'all') return allChecks(reason);
    return { unit: true, browser: project === 'gui',
      ...(project === 'hyprial' ? { gui_integration: true } : {}), reason };
  }
  if (project === 'all' && eventName === 'push' && ['refs/heads/main', 'refs/heads/dev'].includes(event.ref)) {
    return allChecks('main/dev is a release candidate; run all checks');
  }
  if (project === 'hyprial' && eventName === 'push' && ['refs/heads/main', 'refs/heads/dev'].includes(event.ref)) {
    return { unit: true, browser: false, gui_integration: true, reason: 'main/dev is a release candidate; run all checks' };
  }
  if (project === 'gui' && eventName === 'push' && ['refs/heads/main', 'refs/heads/dev'].includes(event.ref)) {
    return { unit: true, browser: true, reason: 'main/dev is a release candidate; run all checks' };
  }
  try {
    const paths = changedPaths(event, eventName, cwd, fetch);
    if (paths.some(path => nativeCliPaths.has(path)) && ['all', 'hyprial'].includes(project)) {
      const head = eventName === 'pull_request' ? event.pull_request.head.sha : event.after;
      let base = eventName === 'pull_request' ? event.pull_request.base.sha : event.before;
      if (eventName === 'pull_request') base = git(['merge-base', base, head], cwd).trim();
      const fields = git(['diff', '--no-renames', '--name-status', '-z', base, head, '--'], cwd).split('\0');
      for (let i = 0; i < fields.length - 1; i += 2) {
        if (!nativeCliPaths.has(fields[i + 1])) continue;
        const tree = git(['ls-tree', '--full-tree', head, '--', fields[i + 1]], cwd);
        if (!['A', 'M'].includes(fields[i]) || !/^100(?:644|755) blob /.test(tree)) {
          const result = allChecks('audited GUI CLI structural change; run all checks');
          return project === 'all' ? result : { unit: true, browser: false, gui_integration: true, reason: result.reason };
        }
      }
    }
    const result = classify(paths, project);
    if (eventName === 'pull_request' && event.pull_request?.base?.ref === 'intg/gaga-gui') {
      result.reason = `gaga integration tier: ${result.reason}`;
    }
    return result;
  }
  catch {
    // Do not print git stderr: it may include credential-bearing diagnostics.
    if (project === 'all') return allChecks('comparison unavailable; run all checks');
    return { unit: true, browser: project === 'gui', ...(project === 'hyprial' ? { gui_integration: true } : {}), reason: 'comparison unavailable; run all checks' };
  }
}

export function runCli(project = process.argv[2]) {
  if (!['h2b', 'hyprial', 'gui', 'all'].includes(project)) throw new Error('expected hyprial, gui or all');
  let result;
  try {
    result = select(JSON.parse(readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8')),
      process.env.GITHUB_EVENT_NAME, project, process.cwd());
  } catch { result = project === 'all' ? allChecks('event unavailable; run all checks') : { unit: true, browser: project === 'gui', ...(project === 'hyprial' ? { gui_integration: true } : {}), reason: 'event unavailable; run all checks' }; }
  // One self-contained log line; keep gate output keys and fallback policy unchanged.
  console.log(`CI_SCOPE ${JSON.stringify({ project, event: process.env.GITHUB_EVENT_NAME ?? 'unknown', ...result })}`);
  for (const key of (project === 'all' ? gates : project === 'hyprial' ? ['unit', 'browser', 'gui_integration'] : ['unit', 'browser'])) appendFileSync(process.env.GITHUB_OUTPUT, `${key}=${result[key]}\n`);
  if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY,
    `\nCI selection: ${Object.entries(result).filter(([key]) => key !== 'reason').map(([key, value]) => `${key}=${value}`).join(', ')}. ${result.reason}.\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) runCli();
