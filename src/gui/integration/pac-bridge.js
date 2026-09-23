import { readFile } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';

const SERVICE = fileURLToPath(new URL('./pac-service.py', import.meta.url));
export function pacStateDir(env) {
  return env.HARNESS_STATE_DIR || path.join(env.HYPRIAL_HOME || env.H2B_HOME || path.join(env.HOME || os.homedir(), '.hyprial'), 'state');
}
export async function pacConfig(env) {
  try {
    const config = JSON.parse(await readFile(path.join(pacStateDir(env), 'dsh-pac/config.json'), 'utf8'));
    if (config.version !== 1 || typeof config.python !== 'string' || !path.isAbsolute(config.python)) throw new Error('PAC_INVALID_CONFIG');
    for (const role of ['coordinator', 'worker', 'verifier']) {
      const value = config.roles?.[role];
      if (!value || typeof value.sessionId !== 'string' || !/^agent:[^:\s]+:[^:\s]+:[^:\s]+$/.test(value.actor)) throw new Error('PAC_INVALID_CONFIG: ' + role);
    }
    if (new Set(Object.values(config.roles).map(r => r.actor)).size !== 3 || new Set(Object.values(config.roles).map(r => r.sessionId)).size !== 3) throw new Error('PAC_INVALID_CONFIG: roles require separate sessions');
    return config;
  } catch (error) { if (error.code === 'ENOENT') return null; throw error; }
}
export async function pacRequest(request, env, config) {
  if (!config?.enabled) throw new Error('PAC_NOT_CONFIGURED: configure and enable PAC roles');
  const childEnv = Object.fromEntries(Object.entries(env).filter(([key]) => !key.startsWith('H2B_')));
  childEnv.HARNESS_STATE_DIR = pacStateDir(env);
  childEnv.HYPRIAL_HOME = env.HYPRIAL_HOME || env.H2B_HOME || path.dirname(childEnv.HARNESS_STATE_DIR);
  return new Promise((resolve, reject) => {
    const child = spawn(config.python, [SERVICE], { env: childEnv, stdio: ['pipe', 'pipe', 'pipe'] });
    let stdout = '', stderr = '', settled = false;
    const finish = (error, value) => { if (settled) return; settled = true; clearTimeout(timer); error ? reject(error) : resolve(value); };
    const timer = setTimeout(() => { child.kill(); finish(new Error('PAC_TIMEOUT: inspect current state before retrying')); }, 20000);
    child.stdout.on('data', chunk => { stdout += chunk; if (stdout.length > 1024 * 1024) { child.kill(); finish(new Error('PAC_OUTPUT_LIMIT')); } });
    child.stderr.on('data', chunk => { stderr = (stderr + chunk).slice(-4000); });
    child.on('error', error => finish(error));
    child.on('close', code => {
      try {
        const doc = JSON.parse(stdout);
        if (code || doc.ok === false) throw Object.assign(new Error(doc.error?.message || 'PAC failed'), { code: doc.error?.code });
        finish(null, doc);
      } catch (error) { finish(stdout ? error : new Error('PAC runtime unavailable: ' + stderr)); }
    });
    child.stdin.on('error', error => finish(error));
    child.stdin.end(JSON.stringify(request));
  });
}
