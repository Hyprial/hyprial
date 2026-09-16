import { execFileSync } from 'node:child_process';
import { appendFileSync, readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

// Only known prose locations are exempt. Runtime templates/skills and unknown
// file types are executable inputs until proven otherwise.
export function isDocumentation(path) {
  if (path === 'docs/squire/SKILL-template.md' || path === 'docs/cutover-runbook.md') return false;
  if (['.forgejo/workflows/README.md', 'dashboard/README.md',
    'sidecar/hyprial-tsnet/README.md', 'sidecar/hyprial-tsnet/e2e/README.md', 'sidecar/h2b-tsnet/README.md', 'sidecar/h2b-tsnet/e2e/README.md'].includes(path)) return true;
  return /^(README(?:\.[\w-]+)?\.(?:md|rst|txt)|README|(?:CHANGELOG|CONTRIBUTING|AUTHORS|LICENSE)(?:\.md|\.rst|\.txt)?)$/.test(path)
    || /^(docs|notes)\/.*\.(md|rst|adoc)$/.test(path);
}

export function classify(paths, project) {
  if (!['h2b', 'hyprial', 'gui', 'all'].includes(project)) throw new Error('unknown CI project');
  if (project === 'all') return classifyRepository(paths);
  if (project === 'hyprial') {
    const result = classifyRepository(paths);
    return { unit: result.python, dashboard: false, gui_integration: result.gui_integration, reason: result.reason };
  }
  // git diff reports repository-root paths even when invoked from src/gui.
  // Keep unknown/shared backend inputs conservative; only strip our package prefix.
  const normalized = project === 'gui'
    ? paths.map(path => path.startsWith('src/gui/') ? path.slice('src/gui/'.length) : path)
    : paths;
  const code = normalized.filter(path => !isDocumentation(path));
  if (!code.length) return { unit: false, dashboard: false, reason: 'documentation-only or empty change' };
  if (project === 'h2b') return { unit: true, dashboard: false, reason: 'H2B code or build input changed' };
  let unit = false, dashboard = false;
  for (const path of code) {
    // The Node suite also tests the Dashboard server and fixtures. Shared
    // launch/install scripts and every unknown path select both gates.
    if (/^(client\/gui-|packages\/gui-layout\/|scripts\/verify-gui-studio)/.test(path)) { unit = true; dashboard = true; }
    else if (/^dashboard\/(src\/|index\.html$)/.test(path)) dashboard = true;
    else if (path.startsWith('dashboard/') || path === 'tests/dashboard.test.mjs') { unit = true; dashboard = true; }
    else if (/^tests\/gui-.*\.browser\.mjs$/.test(path)) { unit = true; dashboard = true; }
    else if (/^(client\/|static\/|imskin-(?:host-)?plugin\.js$|tests\/)/.test(path)) unit = true;
    else { unit = true; dashboard = true; }
  }
  return { unit, dashboard, reason: 'checks selected from complete change range' };
}

// Keep this policy in the GUI package so standalone source archives and the
// monorepo share one classifier without depending on files outside the archive.
const gates = ['python', 'gui_integration', 'unit', 'dashboard', 'codex', 'dsh', 'package'];
function allChecks(reason) {
  return { ...Object.fromEntries(gates.map(key => [key, true])), reason };
}
export function classifyRepository(paths) {
  const code = paths.filter(path => !isDocumentation(path.startsWith('src/gui/') ? path.slice(8) : path));
  if (!code.length) return { ...Object.fromEntries(gates.map(key => [key, false])), reason: 'documentation-only or empty change' };
  // Exempt only known browser source file types, never entire directories.
  // New scripts, providers, dependencies, install code and protocols run all.
  const browserOnly = path => /^src\/gui\/(?:client\/[^/]+\.(?:js|css)|static\/(?:client|host)\.js|dashboard\/src\/[^/]+\.(?:jsx|js|css)|dashboard\/index\.html)$/.test(path);
  if (!code.every(browserOnly)) return allChecks('shared, build, runtime or unknown input changed; run all checks');
  const gui = classify(code, 'gui');
  const dashboardOnly = code.every(path => path.startsWith('src/gui/dashboard/'));
  return { python: false, gui_integration: true, unit: gui.unit,
    dashboard: gui.dashboard, codex: !dashboardOnly, dsh: !dashboardOnly, package: true,
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
  if (project === 'all' && eventName === 'push' && ['refs/heads/main', 'refs/heads/dev'].includes(event.ref)) {
    return allChecks('main/dev is a release candidate; run all checks');
  }
  if (project === 'hyprial' && eventName === 'push' && ['refs/heads/main', 'refs/heads/dev'].includes(event.ref)) {
    return { unit: true, dashboard: false, gui_integration: true, reason: 'main/dev is a release candidate; run all checks' };
  }
  if (project === 'gui' && eventName === 'push' && ['refs/heads/main', 'refs/heads/dev'].includes(event.ref)) {
    return { unit: true, dashboard: true, reason: 'main/dev is a release candidate; run all checks' };
  }
  try { return classify(changedPaths(event, eventName, cwd, fetch), project); }
  catch {
    // Do not print git stderr: it may include credential-bearing diagnostics.
    if (project === 'all') return allChecks('comparison unavailable; run all checks');
    return { unit: true, dashboard: project === 'gui', ...(project === 'hyprial' ? { gui_integration: true } : {}), reason: 'comparison unavailable; run all checks' };
  }
}

export function runCli(project = process.argv[2]) {
  if (!['h2b', 'hyprial', 'gui', 'all'].includes(project)) throw new Error('expected hyprial, gui or all');
  let result;
  try {
    result = select(JSON.parse(readFileSync(process.env.GITHUB_EVENT_PATH, 'utf8')),
      process.env.GITHUB_EVENT_NAME, project, process.cwd());
  } catch { result = project === 'all' ? allChecks('event unavailable; run all checks') : { unit: true, dashboard: project === 'gui', ...(project === 'hyprial' ? { gui_integration: true } : {}), reason: 'event unavailable; run all checks' }; }
  console.log(`CI_SCOPE ${JSON.stringify(result)}`);
  for (const key of (project === 'all' ? gates : project === 'hyprial' ? ['unit', 'dashboard', 'gui_integration'] : ['unit', 'dashboard'])) appendFileSync(process.env.GITHUB_OUTPUT, `${key}=${result[key]}\n`);
  if (process.env.GITHUB_STEP_SUMMARY) appendFileSync(process.env.GITHUB_STEP_SUMMARY,
    `\nCI selection: ${Object.entries(result).filter(([key]) => key !== 'reason').map(([key, value]) => `${key}=${value}`).join(', ')}. ${result.reason}.\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) runCli();
