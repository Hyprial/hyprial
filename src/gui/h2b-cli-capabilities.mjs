#!/usr/bin/env node
// Only repository-owned argv are probed. No daemon operations or caller shell input.
import { spawn } from 'node:child_process';
import { pathToFileURL } from 'node:url';

const queryPaths = {
  version: 'version', processes: 'ps', topology: 'top', doctor: 'doctor', service: 'service',
  targets: 'targets', hosts: 'hosts', agents: 'agent list', workflows: 'workflow list',
  routines: 'routine list', outbox: 'outbox list', adapters: 'adapter list', channels: 'channel list',
  adapterPins: 'adapter pins', organization: 'org status', autoupdate: 'autoupdate status'
};
const actionPaths = {
  'adapter-enroll-preview': 'adapter add', 'adapter-enroll': 'adapter add', 'adapter-authorize': 'adapter authorize',
  'org-management-status': 'org status', 'org-fetch': 'org fetch', 'org-import-preview': 'org import', 'org-import': 'org import',
  'dispatch-matrix': 'dispatch matrix', 'profile-list': 'profile list', 'org-show': 'org show',
  'workflow-plan': 'workflow plan', 'workflow-run': 'workflow run', 'workflow-status': 'workflow status', 'workflow-cancel': 'workflow cancel',
  'routine-plan': 'routine plan', 'routine-add': 'routine add', 'routine-status': 'routine status', 'routine-pause': 'routine pause',
  'routine-resume': 'routine resume', 'routine-remove': 'routine rm', 'routine-templates': 'routine templates', 'routine-template': 'routine templates show',
  'delivery-status': 'delivery status', trajectory: 'trajectory', 'log-query': 'log',
  'adapter-status': 'adapter status', 'adapter-doctor': 'adapter doctor', 'adapter-identities': 'adapter identities list',
  'adapter-start': 'adapter start', 'adapter-stop': 'adapter stop', 'adapter-pin': 'adapter pin', 'adapter-unpin': 'adapter unpin', 'adapter-reload': 'adapter reload',
  'channel-join': 'channel join', 'channel-part': 'channel part', 'agent-launch-config': 'ps', 'agent-restart': 'start', 'agent-destroy': 'agent destroy', 'agent-create': 'agent create', 'agent-start': 'start', 'agent-stop': 'down'
};
const extraFlags = {
  'adapter-enroll-preview': ['--app-id', '--route', '--default-route'],
  'adapter-enroll': ['--app-id', '--route', '--default-route'],
  'org-fetch': ['--from', '--timeout'], 'org-import': ['--force'],
  'dispatch-matrix': ['--tier'],
  'workflow-run': ['--from', '--yes'], 'routine-add': ['--from'],
  'delivery-status': ['--from', '--message-id', '--timeout'],
  'log-query': ['--since', '--until', '--actor', '--conversation', '--correlation-id', '--level', '--name'],
  'channel-join': ['--as'], 'channel-part': ['--as'],
  'agent-destroy': ['--yes'], 'agent-create': ['--name', '--provider', '--model', '--preferred-harness'], 'agent-start': ['--name', '--headless']
};
export const CLI_SURFACE = Object.freeze([
  ...Object.entries(queryPaths).map(([operation, path]) => ({ operation, kind: 'query', argv: path.split(' '), flags: ['--json'] })),
  ...Object.entries(actionPaths).map(([operation, path]) => ({ operation, kind: 'action', argv: path.split(' '), flags: [...(operation === 'routine-template' ? [] : ['--json']), ...(extraFlags[operation] || [])] }))
]);

export function probeHelp(argv, { timeoutMs = 2000, env = process.env } = {}) {
  return new Promise((resolve) => {
    let output = '', timedOut = false, overflow = false, finished = false;
    const child = spawn('h2b', [...argv, '--help'], { env: { ...env, NO_COLOR: '1', TERM: 'dumb', COLUMNS: '200' }, stdio: ['ignore', 'pipe', 'pipe'], shell: false });
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, timeoutMs);
    function finish(result) { if (finished) return; finished = true; clearTimeout(timer); resolve({ output, timedOut, overflow, ...result }); }
    for (const stream of [child.stdout, child.stderr]) stream.on('data', (chunk) => {
      if (Buffer.byteLength(output) + chunk.length > 65536) { overflow = true; child.kill('SIGKILL'); return; }
      output += chunk.toString();
    });
    child.on('error', (error) => finish({ status: null, errorCode: error.code }));
    child.on('close', (status) => finish({ status }));
  });
}

export function classifyHelp(spec, result) {
  const text = String(result.output || '').replace(/\u001b\[[0-9;]*m/g, '');
  if (result.timedOut) return { code: 'CAPABILITY_PROBE_TIMEOUT', message: 'CLI help probe timed out' };
  if (result.errorCode || result.overflow) return { code: 'CAPABILITY_PROBE_FAILED', message: result.errorCode === 'ENOENT' ? 'H2B executable was not found' : 'CLI help probe could not complete safely' };
  if (result.status !== 0) return /No such command|invalid choice/i.test(text)
    ? { code: 'UNSUPPORTED_COMMAND', message: 'Installed CLI does not support this command' }
    : { code: 'CAPABILITY_PROBE_FAILED', message: 'CLI help probe exited unsuccessfully' };
  // An unknown child can return its parent help with exit 0; do not accept that.
  // The internal h2b compatibility launcher preserves Hyprial's help output.
  const usage = text.match(/Usage:\s*(?:hyprial|h2b)\s+([^\n]+)/i)?.[1]?.trim();
  const path = spec.argv.join(' ');
  if (!usage || !(usage === path || usage.startsWith(path + ' '))) return { code: 'CAPABILITY_PROBE_FAILED', message: 'CLI help did not identify the requested command path' };
  const flags = new Set(text.match(/--[a-z][a-z0-9-]*/g) || []);
  const missing = spec.flags.filter((flag) => !flags.has(flag));
  if (missing.length) return { code: 'UNSUPPORTED_FLAG', message: 'Installed CLI is missing required options: ' + missing.join(', ') };
  return null;
}

export async function discoverCapabilities({ probe = probeHelp, surface = CLI_SURFACE } = {}) {
  const results = new Map();
  const queue = [...new Map(surface.map((spec) => [spec.argv.join(' '), spec.argv])).entries()];
  let cursor = 0;
  await Promise.all(Array.from({ length: Math.min(4, queue.length) }, async () => {
    while (cursor < queue.length) {
      const [key, argv] = queue[cursor++];
      try { results.set(key, await probe(argv)); }
      catch { results.set(key, { status: null, errorCode: 'PROBE_ERROR' }); }
    }
  }));
  const supported = [], unavailable = [];
  for (const spec of surface) {
    const failure = classifyHelp(spec, results.get(spec.argv.join(' ')));
    const entry = { operation: spec.operation, kind: spec.kind };
    if (failure) unavailable.push({ ...entry, ...failure }); else supported.push(entry);
  }
  return { ok: true, checkedAt: new Date().toISOString(), supported, unavailable };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  process.stdout.write(JSON.stringify(await discoverCapabilities()));
}
