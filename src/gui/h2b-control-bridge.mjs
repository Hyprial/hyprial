#!/usr/bin/env node

import { launchAction } from './integration/agent-launch-settings.mjs';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { READONLY_CLI_OPERATIONS, buildReadonlyOpsArgv } from './integration/cli-readonly-ops.mjs';

// JSON escaping can nearly double a 64 KiB YAML document (for example many
// newlines/backslashes), while the Host route still caps the full body at 256 KiB.
const MAX_REQUEST_BYTES = 192 * 1024;
const MAX_YAML_BYTES = 64 * 1024;
const MAX_OUTPUT_BYTES = 512 * 1024;
const CANONICAL_AGENT = /^agent:[^\s:]+:[^\s:]+:[^\s:]+$/;
const OPAQUE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
const CHANNEL_NAME = /^#[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;
const AGENT_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
const MODEL_VALUE = /^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/;
const HARNESSES = new Set(['claude', 'pi', 'codex', 'dsh']);
const LOG_LEVELS = new Set(['debug', 'info', 'warn', 'error']);
const RFC3339 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/;

function fail(message, code = 'INVALID_ARGUMENT', details) {
  const error = new Error(message);
  error.code = code;
  error.details = details;
  throw error;
}

async function readInput() {
  const chunks = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    size += chunk.length;
    if (size > MAX_REQUEST_BYTES) fail('h2b control request is too large');
    chunks.push(chunk);
  }
  let value;
  try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { fail('h2b control request must be valid JSON'); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) fail('h2b control request must be a JSON object');
  return value;
}

function requireYaml(input) {
  if (typeof input.yaml !== 'string' || !input.yaml.trim()) fail('yaml is required');
  if (Buffer.byteLength(input.yaml, 'utf8') > MAX_YAML_BYTES) fail('yaml exceeds 64 KiB');
  if (input.yaml.includes('\0')) fail('yaml contains a NUL byte');
  return input.yaml;
}

function requireActor(input) {
  if (!CANONICAL_AGENT.test(String(input.from || ''))) fail('from must be a canonical agent:owner:node:actor URI');
  return input.from;
}

function requireExpectedActor(input) {
  if (!CANONICAL_AGENT.test(String(input.expectedActor || ''))) fail('expectedActor must be a canonical agent:owner:node:actor URI');
  return input.expectedActor;
}

function optionalId(input, field) {
  const value = String(input[field] || '').trim();
  if (!value) return '';
  if (!OPAQUE_ID.test(value)) fail(field + ' contains unsupported characters');
  return value;
}

function requireId(input, field) {
  const value = String(input[field] || '');
  if (!OPAQUE_ID.test(value)) fail(field + ' contains unsupported characters');
  return value;
}

function requireChannel(input) {
  const value = String(input.channel || '').trim();
  if (!CHANNEL_NAME.test(value)) fail('channel must start with # and contain only letters, digits, dot, underscore, or dash');
  return value;
}

function requireAgentName(input) {
  const value = String(input.name || '').trim();
  if (!AGENT_NAME.test(value)) fail('agent name contains unsupported characters');
  return value;
}

function optionalModelValue(input, field) {
  const value = String(input[field] || '').trim();
  if (!value) return '';
  if (!MODEL_VALUE.test(value)) fail(field + ' contains unsupported characters');
  return value;
}

function requireHarness(input) {
  const value = String(input.harness || '').trim();
  if (!HARNESSES.has(value)) fail('harness must be one of claude, pi, codex, or dsh');
  return value;
}

function optionalScope(input, field) {
  return optionalId(input, field);
}

function requireLogWindow(input) {
  const since = String(input.since || '').trim();
  const until = String(input.until || '').trim();
  if (!RFC3339.test(since) || !RFC3339.test(until)) fail('since and until must be RFC3339 timestamps');
  const sinceMs = Date.parse(since);
  const untilMs = Date.parse(until);
  if (!Number.isFinite(sinceMs) || !Number.isFinite(untilMs) || untilMs < sinceMs) fail('log time window is invalid');
  if (untilMs - sinceMs > 60 * 60 * 1000) fail('log time window cannot exceed 60 minutes');
  return { since, until };
}

async function runCli(argv) {
  const executable = process.env.H2B_CONTROL_BIN || 'h2b';
  return await new Promise((resolve, reject) => {
    const child = spawn(executable, argv, { stdio: ['ignore', 'pipe', 'pipe'], env: process.env });
    const stdout = [];
    const stderr = [];
    let outputBytes = 0;
    let overflow = false;
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; child.kill('SIGKILL'); }, 25_000);
    timer.unref();
    function collect(target, chunk) {
      outputBytes += chunk.length;
      if (outputBytes > MAX_OUTPUT_BYTES) { overflow = true; child.kill('SIGKILL'); return; }
      target.push(chunk);
    }
    child.stdout.on('data', (chunk) => collect(stdout, chunk));
    child.stderr.on('data', (chunk) => collect(stderr, chunk));
    child.on('error', (error) => { clearTimeout(timer); reject(error); });
    child.on('close', (status) => {
      clearTimeout(timer);
      resolve({
        status,
        overflow,
        timedOut,
        stdout: Buffer.concat(stdout).toString('utf8'),
        stderr: Buffer.concat(stderr).toString('utf8')
      });
    });
  });
}

function cliError(result) {
  let parsed;
  try { parsed = JSON.parse(result.stdout); } catch {}
  const nested = parsed && typeof parsed === 'object' ? parsed.error : null;
  const message = typeof nested === 'string' ? nested
    : nested && typeof nested.message === 'string' ? nested.message
      : result.stderr.trim() || result.stdout.trim() || 'h2b command failed';
  const code = typeof parsed?.code === 'string' ? parsed.code
    : typeof nested?.code === 'string' ? nested.code : 'H2B_COMMAND_FAILED';
  fail(message.slice(0, 500), code, parsed && typeof parsed === 'object' ? parsed : undefined);
}

async function invoke(input) {
  if (['agent-launch-config', 'agent-restart'].includes(input.operation)) return launchAction(input);
  const operation = input.operation;
  let directory = '';
  try {
    let argv;
    if (Object.hasOwn(READONLY_CLI_OPERATIONS, operation)) {
      argv = buildReadonlyOpsArgv(input);
    } else if (operation === 'workflow-plan' || operation === 'workflow-run' || operation === 'routine-add' || operation === 'routine-plan') {
      const yaml = requireYaml(input);
      const actor = requireActor(input);
      directory = await mkdtemp(join(tmpdir(), 'dsh-h2b-control-'));
      const file = join(directory, operation.startsWith('routine-') ? 'routine.yaml' : 'workflow.yaml');
      await writeFile(file, yaml, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
      if (operation === 'workflow-plan') argv = ['workflow', 'plan', file, '--json'];
      else if (operation === 'workflow-run') argv = ['workflow', 'run', file, '--from', actor, '--yes', '--json'];
      else if (operation === 'routine-plan') argv = ['routine', 'plan', file, '--json'];
      else argv = ['routine', 'add', file, '--from', actor, '--json'];
    } else if (operation === 'routine-templates') {
      argv = ['routine', 'templates', '--json'];
    } else if (operation === 'routine-template') {
      argv = ['routine', 'templates', 'show', requireId(input, 'name')];
    } else if (operation === 'workflow-status') {
      argv = ['workflow', 'status', requireId(input, 'runId'), '--json'];
    } else if (operation === 'workflow-cancel') {
      argv = ['workflow', 'cancel', requireId(input, 'runId'), '--json'];
    } else if (operation === 'routine-status') {
      argv = ['routine', 'status', requireId(input, 'name'), '--json'];
    } else if (operation === 'routine-pause') {
      argv = ['routine', 'pause', requireId(input, 'name'), '--json'];
    } else if (operation === 'routine-resume') {
      argv = ['routine', 'resume', requireId(input, 'name'), '--json'];
    } else if (operation === 'routine-remove') {
      argv = ['routine', 'rm', requireId(input, 'name'), '--json'];
    } else if (operation === 'delivery-status') {
      argv = ['delivery', 'status', '--from', requireActor(input)];
      const messageId = optionalId(input, 'messageId');
      if (messageId) argv.push('--message-id', messageId);
      argv.push('--timeout', '2', '--json');
    } else if (operation === 'trajectory') {
      argv = ['trajectory', requireId(input, 'messageId'), '--json'];
    } else if (operation === 'log-query') {
      const window = requireLogWindow(input);
      argv = ['log'];
      for (const field of ['component', 'name']) {
        const value = optionalScope(input, field);
        if (value) argv.push('--' + field, value);
      }
      const level = String(input.level || '').trim();
      if (level && !LOG_LEVELS.has(level)) fail('level must be debug, info, warn, or error');
      if (level) argv.push('--level', level);
      const actor = String(input.actor || '').trim();
      if (actor) argv.push('--actor', requireActor({ from: actor }));
      for (const [field, option] of [['conversation', '--conversation'], ['correlationId', '--correlation-id']]) {
        const value = optionalScope(input, field);
        if (value) argv.push(option, value);
      }
      argv.push('--since', window.since, '--until', window.until, '--json');
    } else if (operation === 'adapter-reload') {
      if (input.confirmed !== true) fail('explicit confirmation is required for adapter reload');
      if (Object.keys(input).some(key => !['operation', 'confirmed'].includes(key))) {
        fail('adapter reload accepts no adapter, identity, or extra arguments');
      }
      argv = ['adapter', 'reload', '--json'];
    } else if (operation === 'adapter-status') {
      argv = ['adapter', 'status', requireId(input, 'adapter'), '--json'];
    } else if (operation === 'adapter-doctor') {
      argv = ['adapter', 'doctor', requireId(input, 'adapter'), '--json'];
    } else if (operation === 'adapter-identities') {
      argv = ['adapter', 'identities', 'list', requireId(input, 'adapter'), '--json'];
    } else if (operation === 'adapter-start' || operation === 'adapter-stop') {
      argv = ['adapter', operation === 'adapter-start' ? 'start' : 'stop', requireId(input, 'adapter'), '--json'];
    } else if (operation === 'adapter-pin') {
      argv = ['adapter', 'pin', requireId(input, 'adapter'), requireActor(input), '--json'];
    } else if (operation === 'adapter-unpin') {
      const adapter = requireId(input, 'adapter');
      const expectedActor = requireExpectedActor(input);
      const current = await runCli(['adapter', 'pins', '--json']);
      if (current.timedOut) fail('h2b adapter pins timed out after 25 seconds', 'COMMAND_TIMEOUT');
      if (current.overflow) fail('h2b adapter pins output exceeded 512 KiB', 'OUTPUT_TOO_LARGE');
      if (current.status !== 0) cliError(current);
      let pins;
      try { pins = JSON.parse(current.stdout); }
      catch { fail('h2b adapter pins returned invalid JSON', 'INVALID_RESPONSE'); }
      const actual = pins && typeof pins === 'object' && pins.pins && typeof pins.pins === 'object'
        ? pins.pins[adapter] : undefined;
      if (actual !== expectedActor) {
        fail('adapter pin changed; refresh before unpinning', 'PIN_CHANGED', {
          adapter,
          expectedActor,
          actualActor: typeof actual === 'string' ? actual : null
        });
      }
      argv = ['adapter', 'unpin', adapter, '--json'];
    } else if (operation === 'channel-join' || operation === 'channel-part') {
      argv = ['channel', operation === 'channel-join' ? 'join' : 'part', requireChannel(input)];
      const actor = String(input.as || '').trim();
      if (actor) argv.push('--as', requireActor({ from: actor }));
      argv.push('--json');
    } else if (operation === 'agent-create') {
      argv = ['agent', 'create', '--name', requireAgentName(input)];
      const provider = optionalModelValue(input, 'provider');
      const model = optionalModelValue(input, 'model');
      const preferred = String(input.preferredHarness || '').trim();
      if (provider) argv.push('--provider', provider);
      if (model) argv.push('--model', model);
      if (preferred) argv.push('--preferred-harness', requireHarness({ harness: preferred }));
      argv.push('--json');
    } else if (operation === 'agent-start') {
      argv = ['start', requireHarness(input), '--name', requireAgentName(input), '--headless', '--json'];
    } else if (operation === 'agent-destroy') {
      if (input.confirmed !== true) fail('explicit confirmation is required for agent destroy');
      if (Object.keys(input).some(key => !['operation', 'expectedActor', 'confirmed'].includes(key))) fail('unsupported agent destroy fields');
      const actor = requireExpectedActor(input);
      const current = await runCli(['ps', '--json']);
      if (current.timedOut || current.overflow || current.status !== 0) fail('cannot verify local daemon identity', 'IDENTITY_UNAVAILABLE');
      let document;
      try { document = JSON.parse(current.stdout); }
      catch { fail('invalid daemon identity response', 'INVALID_RESPONSE'); }
      const daemon = document && document.ok === true && document.daemon;
      const parts = actor.split(':');
      if (!daemon || parts[1] !== daemon.owner || parts[2] !== daemon.nodeId) fail('destroy target must belong to this local daemon', 'IDENTITY_MISMATCH');
      requireAgentName({ name: parts[3] });
      argv = ['agent', 'destroy', actor, '--yes', '--json'];
    } else if (operation === 'agent-stop') {
      argv = ['down', requireId(input, 'connectorId'), '--json'];
    } else {
      fail('unsupported h2b control action');
    }

    const result = await runCli(argv);
    if (result.timedOut) fail('h2b command timed out after 25 seconds', 'COMMAND_TIMEOUT');
    if (result.overflow) fail('h2b command output exceeded 512 KiB', 'OUTPUT_TOO_LARGE');
    if (result.status !== 0) cliError(result);
    // templates show intentionally emits verbatim YAML, not JSON. Preserve
    // runtime placeholders and never evaluate template text in the bridge.
    if (operation === 'routine-template') return { ok: true, operation, document: { name: input.name, yaml: result.stdout } };
    let document;
    try { document = JSON.parse(result.stdout); }
    catch { fail('h2b command returned invalid JSON', 'INVALID_RESPONSE'); }
    if (!document || typeof document !== 'object' || Array.isArray(document)) fail('h2b command returned an invalid document', 'INVALID_RESPONSE');
    return { ok: true, operation, document };
  } finally {
    if (directory) await rm(directory, { recursive: true, force: true });
  }
}

try {
  const input = await readInput();
  process.stdout.write(JSON.stringify(await invoke(input)));
} catch (error) {
  process.stdout.write(JSON.stringify({
    ok: false,
    error: {
      code: typeof error?.code === 'string' ? error.code : 'CONTROL_ACTION_FAILED',
      message: String(error?.message || 'h2b control action failed').slice(0, 500),
      ...(error?.details ? { details: error.details } : {})
    }
  }));
  process.exitCode = 1;
}
