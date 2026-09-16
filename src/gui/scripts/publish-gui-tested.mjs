#!/usr/bin/env node
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import { setTimeout as sleep } from 'node:timers/promises';

const COMMAND_TIMEOUT_MS = 60000;
const QUERY_BACKOFF_MS = [1000, 2000];

export function sanitizeDiagnostic(value, env = process.env) {
  let text = String(value ?? '');
  const token = env.GITHUB_TOKEN;
  if (token) {
    // Git may print a literal or percent-encoded password, or an HTTP auth header.
    for (const secret of [token, encodeURIComponent(token),
      Buffer.from(`x-access-token:${token}`).toString('base64')]) {
      text = text.split(secret).join('[REDACTED]');
    }
  }
  return text
    // Do not depend on the credential matching GITHUB_TOKEN (e.g. a stored URL).
    .replace(/\b[a-z][a-z0-9+.-]*:\/\/[^\s<>]+/gi, url =>
      url.includes('@') ? url.replace(/\/\/.*@/, '//[REDACTED]@') : url)
    .replace(/((?:proxy-)?authorization\s*:\s*)(?:basic|bearer)\s+\S+/gi, '$1[REDACTED]')
    .replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g, '');
}

function retryableQueryFailure(result) {
  const detail = `${result.stderr || ''}\n${result.error?.message || ''}`;
  // Permanent authentication/authorization failures should not be hammered.
  if (/authentication failed|authorization failed|could not read (?:username|password)|terminal prompts disabled|permission denied|access denied|repository not found|requested URL returned error: (?:401|403)|\b(?:401|403)\b/i.test(detail)) return false;
  return result.error?.code === 'ETIMEDOUT'
    || /could not resolve (?:host|proxy)|failed to connect|couldn't connect|connection (?:reset|timed out|refused)|operation timed out|remote end hung up|empty reply from server|TLS connection was non-properly terminated|requested URL returned error: (?:408|429|5\d\d)/i.test(detail);
}

export function createCommandRunner({ spawn = spawnSync, env = process.env,
  wait = sleep, warn = console.error } = {}) {
  return async function run(command, args) {
    // Only this read is safe to repeat. In particular a failed push can have
    // reached the remote: never retry it, even on timeout/unknown outcome.
    const attempts = command === 'git' && args[0] === 'ls-remote' ? QUERY_BACKOFF_MS.length + 1 : 1;
    for (let attempt = 1; attempt <= attempts; attempt++) {
      let result;
      try {
        result = spawn(command, args, { encoding: 'utf8', timeout: COMMAND_TIMEOUT_MS,
          killSignal: 'SIGKILL', env: { ...env, GIT_TERMINAL_PROMPT: '0' } });
      } catch (error) {
        result = { error, status: null, signal: null };
      }
      if (!result.error && result.status === 0) return result.stdout.trim();
      const retry = attempt < attempts && retryableQueryFailure(result);
      // Redact BEFORE truncation, and never attach the raw child error as cause.
      const detail = sanitizeDiagnostic([
        `${command} ${args[0]} failed (attempt ${attempt}/${attempts}, status=${result.status ?? 'null'}, signal=${result.signal ?? 'none'}, code=${result.error?.code ?? 'none'}, timeout=${result.error?.code === 'ETIMEDOUT'}, limit=${COMMAND_TIMEOUT_MS}ms)`,
        result.error?.message && `error: ${result.error.message}`,
        result.stderr && `stderr: ${result.stderr.trim()}`,
      ].filter(Boolean).join('\n'), env).slice(0, 4096);
      if (!retry) throw new Error(detail);
      warn(`${detail}\nRetrying read-only git ls-remote in ${QUERY_BACKOFF_MS[attempt - 1]}ms`);
      await wait(QUERY_BACKOFF_MS[attempt - 1]);
    }
  };
}

export function assertPromotion({ event, ref, commit, head, main, verified, latest }) {
  assert.equal(event, 'push', 'Only main push CI may promote a release');
  assert.equal(ref, 'refs/heads/main');
  assert.match(commit || '', /^[a-f0-9]{40}$/);
  assert.equal(head, commit, 'Checkout differs from the tested commit');
  assert.equal(main, commit, 'main advanced; a stale run cannot publish');
  assert.match(verified || '', /^\d+\.\d+\.\d+(?:-[\w.]+)?$/);
  assert.equal(latest, verified, 'npm latest changed since validation; rerun CI');
}

export async function publishGuiTested({ env = process.env, log = console.log, ...options } = {}) {
  const run = createCommandRunner({ ...options, env });
  const refs = await run('git', ['ls-remote', 'origin', 'refs/heads/main', 'refs/tags/gui-tested']);
  const remote = new Map(refs.split('\n').filter(Boolean).map(line => { const [sha, ref] = line.split(/\s+/); return [ref, sha]; }));
  const verified = env.VERIFIED_DSH_VERSION;
  assertPromotion({ event: env.GITHUB_EVENT_NAME, ref: env.GITHUB_REF,
    commit: env.GITHUB_SHA, head: await run('git', ['rev-parse', 'HEAD']),
    main: remote.get('refs/heads/main'), verified,
    latest: JSON.parse(await run('npm', ['view', '@deepseek-ai/dsh', 'dist-tags.latest', '--json', '--registry=https://registry.npmjs.org'])) });
  // A lease prevents concurrent runs from overwriting a newer promotion.
  await run('git', ['push', '--force-with-lease=refs/tags/gui-tested:' + (remote.get('refs/tags/gui-tested') || ''),
    'origin', env.GITHUB_SHA + ':refs/tags/gui-tested']);
  log(sanitizeDiagnostic(`Promoted ${env.GITHUB_SHA} to gui-tested after DSH latest ${verified} passed`, env));
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    await publishGuiTested();
  } catch (error) {
    console.error(sanitizeDiagnostic(error.message));
    process.exitCode = 1;
  }
}
