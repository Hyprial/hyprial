// GUI-owned plumbing only. Kanban owns data, filters and HTML rendering.
import { readFile, access, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { homedir } from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

export const KANBAN_MAX_BYTES = 4 * 1024 * 1024;
const KEYS = ['H2B_KANBAN_BRIDGE', 'H2B_KANBAN_DATA_DIR', 'H2B_KANBAN_TASK_BIN', 'KANBAN_STAGE_RC'];
const appRoot = (env) => path.join(env.HYPRIAL_HOME || env.H2B_HOME || path.join(env.HOME || homedir(), '.h2b'), 'apps/kanban');
const failure = (code, message) => Object.assign(new Error(message), { code });

// Decode literal shell words emitted by kanban install (including shlex.quote).
// Never execute env.sh, expand variables, or read taskrc / shared credentials.
export function literalValue(source) {
  let result = '', quote = '';
  for (let i = 0; i < source.length; i++) {
    const c = source[i];
    if (quote === "'") { if (c === "'") quote = ''; else result += c; }
    else if (c === '\\') {
      const next = source[++i];
      if (next === undefined || (quote === '"' && !'"\\$`'.includes(next))) throw Error('invalid escape');
      result += next;
    } else if (quote === '"') {
      if (c === '"') quote = '';
      else if ('$`'.includes(c)) throw Error('expansion is not a literal');
      else result += c;
    } else if (c === "'" || c === '"') quote = c;
    else if (/[\s$`;|&<>()#]/.test(c)) throw Error('not a literal');
    else result += c;
  }
  if (quote || /[\0\r\n]/.test(result)) throw Error('invalid literal');
  return result;
}

export async function loadKanbanEnvironment(env = process.env) {
  env = { ...env };
  for (const suffix of ['ENV_FILE', 'BRIDGE', 'DATA_DIR', 'TASK_BIN']) {
    if (env['HYPRIAL_KANBAN_' + suffix] !== undefined) env['H2B_KANBAN_' + suffix] = env['HYPRIAL_KANBAN_' + suffix];
  }
  const values = {};
  const modernValues = {};
  const filename = env.H2B_KANBAN_ENV_FILE || path.join(appRoot(env), 'env.sh');
  let error = null;
  try {
    const info = await stat(filename);
    if (info.size > 65536) throw Error('configuration too large');
    for (const line of (await readFile(filename, 'utf8')).split('\n')) {
      const match = /^\s*(?:export\s+)?([A-Z0-9_]+)=(.*)$/.exec(line);
      if (!match) continue;
      const key = match[1].replace(/^HYPRIAL_KANBAN_/, 'H2B_KANBAN_');
      if (KEYS.includes(key) && env[key] === undefined) {
        (match[1].startsWith('HYPRIAL_KANBAN_') ? modernValues : values)[key] = literalValue(match[2].trim());
      }
    }
  } catch (e) {
    if (e.code !== 'ENOENT' || env.H2B_KANBAN_ENV_FILE) error = '无法读取 Kanban 安装配置，请检查安装或显式路径配置。';
  }
  Object.assign(values, modernValues);
  for (const key of KEYS) if (env[key] !== undefined) values[key] = env[key];
  if (error) values.H2B_GUI_KANBAN_CONFIG_ERROR = error;
  return { ...env, ...values };
}

export async function kanbanStatus(env = process.env) {
  let version = null, lastSyncAt = null;
  try {
    const text = await readFile(path.join(appRoot(env), 'VERSION'), 'utf8');
    version = /^version=([\w.+-]{1,64})$/m.exec(text)?.[1] || null;
  } catch {}
  try {
    const value = JSON.parse(await readFile(path.join(appRoot(env), 'last-sync.json'), 'utf8')).at_epoch;
    if (typeof value === 'number' && Number.isFinite(value) && value > 0 && value * 1000 <= Date.now() + 60000) lastSyncAt = new Date(value * 1000).toISOString();
  } catch {}
  const base = { ok: true, version, lastSyncAt, readOnly: true };
  if (env.H2B_GUI_KANBAN_CONFIG_ERROR) return { ...base, state: 'invalid', message: env.H2B_GUI_KANBAN_CONFIG_ERROR };
  if (!env.H2B_KANBAN_BRIDGE || !env.H2B_KANBAN_DATA_DIR) return { ...base, state: 'unconfigured', message: '尚未连接本机 Kanban。安装 Kanban 后重启 GUI，或配置看板路径。' };
  try {
    for (const key of KEYS) if (env[key] && !path.isAbsolute(env[key])) throw Error('relative path');
    await access(env.H2B_KANBAN_BRIDGE, constants.R_OK);
    if (!(await stat(env.H2B_KANBAN_DATA_DIR)).isDirectory()) throw Error('not a directory');
    if (env.H2B_KANBAN_TASK_BIN) await access(env.H2B_KANBAN_TASK_BIN, constants.X_OK);
    if (env.KANBAN_STAGE_RC) await access(env.KANBAN_STAGE_RC, constants.R_OK);
  } catch { return { ...base, state: 'unavailable', message: '本机 Kanban 配置指向不可用的文件或目录，请检查安装。' }; }
  // Configuration availability is not a successful board read or sync probe.
  return { ...base, state: 'configured', message: '已配置本机看板' };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  if (process.argv[2] === 'environment') {
    const env = await loadKanbanEnvironment();
    for (const key of [...KEYS, 'H2B_GUI_KANBAN_CONFIG_ERROR']) if (env[key] !== undefined) process.stdout.write(`${key}=${env[key]}\0`);
    process.stdout.write(`H2B_GUI_KANBAN_HELPER=${fileURLToPath(import.meta.url)}\0`);
  } else if (process.argv[2] === 'status') process.stdout.write(JSON.stringify(await kanbanStatus()));
  else process.exitCode = 2;
}
