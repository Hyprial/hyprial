import net from 'node:net';
import { createHash, randomUUID } from 'node:crypto';
import { dirname, join, isAbsolute } from 'node:path';
import { homedir } from 'node:os';
import { stat, open, unlink } from 'node:fs/promises';

const fail = (code, message, details) => { throw Object.assign(new Error(message), { code, details }); };
function socketPath(env) {
  return env.HARNESS_SOCKET_PATH || join(env.HARNESS_STATE_DIR || join(env.H2B_HOME || join(env.HOME || homedir(), '.h2b'), 'state'), 'daemon.sock');
}
export function launchStateDir(env = process.env) { return dirname(socketPath(env)); }
export function requestDaemon(method, params, env = process.env) {
  return new Promise((resolve, reject) => {
    const id = randomUUID(), socket = net.createConnection(socketPath(env));
    socket.setEncoding('utf8');
    let buffer = '', settled = false;
    const finish = (error, value) => { if (settled) return; settled = true; clearTimeout(timer); socket.destroy(); error ? reject(error) : resolve(value); };
    const uncertain = () => Object.assign(new Error('H2B 请求未确认完成，请刷新核对当前状态后再操作。'), { code: 'OUTCOME_UNKNOWN' });
    const timer = setTimeout(() => finish(uncertain()), 65000);
    socket.on('connect', () => socket.write(JSON.stringify({ version: 1, id, method, params }) + '\n'));
    socket.on('error', () => finish(uncertain()));
    socket.on('end', () => finish(uncertain()));
    socket.on('data', chunk => {
      buffer += chunk;
      if (Buffer.byteLength(buffer) > 8 * 1024 * 1024) return finish(uncertain());
      if (!buffer.includes('\n')) return;
      try {
        const response = JSON.parse(buffer.slice(0, buffer.indexOf('\n')));
        if (response.id !== id || response.version !== 1) return finish(uncertain());
        if (response.error) return finish(Object.assign(new Error(response.error.message), { code: response.error.code || 'DAEMON_ERROR' }));
        if (!response.result || typeof response.result !== 'object' || response.result.ok === false) return finish(uncertain());
        finish(null, response.result);
      } catch { finish(uncertain()); }
    });
  });
}

// Read only: H2B remains the sole writer of launch configuration. Refuse unknown schemas.
export async function readLaunch(actor, env = process.env) {
  const { DatabaseSync } = await import('node:sqlite');
  const database = new DatabaseSync(join(dirname(socketPath(env)), 'lifecycle-operations.sqlite3'), { readOnly: true });
  try {
    if (database.prepare('SELECT schema_version FROM root_state WHERE id = 1').get()?.schema_version !== 1) fail('UNSUPPORTED_SCHEMA', '当前 H2B 启动配置格式不受支持，请使用 CLI。');
    const rows = database.prepare('SELECT * FROM harnesses WHERE name = ?').all(actor.split(':')[3]);
    if (rows.length !== 1) fail('LAUNCH_NOT_FOUND', '没有唯一的受管 Worker 启动配置；交互会话或已停止对象请使用启动操作。');
    return { ...rows[0] };
  } finally { database.close(); }
}
function parseSettings(row) {
  let args;
  try { args = JSON.parse(row.args_json); } catch { return null; }
  if (!Array.isArray(args) || args.some(a => typeof a !== 'string')) return null;
  if (row.harness !== 'codex') return { cwd: row.cwd || '', model: row.model || '' };
  const settings = { cwd: row.cwd || '', model: row.model || '', sandbox: 'inherit', approval: 'inherit' };
  const keys = { '--sandbox': 'sandbox', '-s': 'sandbox', '--ask-for-approval': 'approval', '-a': 'approval', '--model': 'model', '-m': 'model' };
  const seen = new Set();
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--dangerously-bypass-approvals-and-sandbox') {
      if (seen.has('sandbox') || seen.has('approval')) return null;
      settings.sandbox = 'danger-full-access'; settings.approval = 'never'; seen.add('sandbox'); seen.add('approval'); continue;
    }
    const at = args[i].indexOf('='), option = at < 0 ? args[i] : args[i].slice(0, at), key = keys[option];
    if (!key || seen.has(key)) return null;
    seen.add(key);
    settings[key] = at < 0 ? args[++i] : args[i].slice(at + 1);
    if (typeof settings[key] !== 'string' || !settings[key]) return null;
  }
  // A separate H2B model setting takes precedence over a CLI --model.
  if (row.model) settings.model = row.model;
  return settings;
}
function launchSpec(row) {
  const spec = { provider: row.harness, name: row.name, headless: !!row.headless, args: JSON.parse(row.args_json), command: JSON.parse(row.command_json), ownership: row.ownership };
  for (const [column, key] of Object.entries({ nickname: 'nickname', cwd: 'cwd', endpoint: 'endpoint', session_ref: 'sessionRef', turn_timeout_seconds: 'turnTimeoutSeconds', idle_timeout_seconds: 'idleTimeoutSeconds', pinned_owner: 'pinnedOwner', container_image: 'containerImage', model_vendor: 'modelProvider', model: 'model' })) {
    if (row[column] != null) spec[key] = row[column];
  }
  spec.containerized = !!row.containerized;
  return spec;
}
export async function launchSnapshot(actor, { env = process.env, request = requestDaemon, read = readLaunch } = {}) {
  if (!/^agent:[^\s:]+:[^\s:]+:[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(actor || '')) fail('INVALID_ARGUMENT', '需要完整 Agent 地址。');
  const ps = await request('ps', {}, env), parts = actor.split(':');
  if (ps.daemon?.owner !== parts[1] || ps.daemon?.nodeId !== parts[2]) fail('NOT_LOCAL', '只能修改已核验的本机 Agent。');
  const row = await read(actor, env);
  const connectorId = row.harness + ':' + row.name;
  const connector = (ps.connectors || []).find(c => c.id === connectorId);
  const settings = parseSettings(row);
  const args = settings ? JSON.parse(row.args_json) : [];
  const hasOption = option => args.some(arg => arg === option || arg.startsWith(option + '='));
  const modelLocked = row.harness !== 'codex' && (hasOption('--model') || (row.harness === 'pi' && hasOption('-m')));
  let endpointAllowed = !row.endpoint;
  if (row.harness === 'dsh') {
    // Explicit session attachment does not implement managed-worker restart.
    const endpoints = args.flatMap((arg, index) => arg === '--endpoint' ? [args[index + 1]] : arg.startsWith('--endpoint=') ? [arg.slice(11)] : []);
    const endpoint = endpoints.length ? endpoints[0] : row.endpoint || 'http://127.0.0.1:3080';
    try {
      const url = new URL(endpoint);
      endpointAllowed = endpoints.length <= 1 && !hasOption('--session-id') && ['http:', 'https:'].includes(url.protocol) && ['127.0.0.1', '[::1]', 'localhost'].includes(url.hostname) && !url.username && !url.password;
    } catch { endpointAllowed = false; }
  }
  const knownColumns = new Set(['harness', 'name', 'headless', 'args_json', 'ownership', 'nickname', 'cwd', 'endpoint', 'session_ref', 'command_json', 'turn_timeout_seconds', 'idle_timeout_seconds', 'containerized', 'pinned_owner', 'container_image', 'model_vendor', 'model', 'status']);
  const editable = Object.keys(row).every(key => knownColumns.has(key)) && ['codex', 'claude', 'pi', 'dsh'].includes(row.harness) && row.headless === 1 && row.ownership === 'managed' && !row.containerized && endpointAllowed && !row.pinned_owner && !!settings;
  const version = createHash('sha256').update(JSON.stringify([row, ps.daemon.epoch, connector?.pid || null])).digest('hex');
  const harnessLabel = { codex: 'Codex', claude: 'Claude Code（CC）', pi: 'Pi', dsh: 'DSH（DeepSeek Harness）' }[row.harness] || row.harness;
  const fields = row.harness === 'codex' ? ['cwd', 'model', 'sandbox', 'approval'] : ['cwd', 'model'];
  return { row, document: {
    actor, connectorId, version, editable, harness: row.harness, harnessLabel, fields, modelLocked,
    modelProvider: row.model_vendor || '', running: connector?.running === true, pid: connector?.pid || null,
    settings: settings || { cwd: row.cwd || '', model: row.model || '', sandbox: 'unknown', approval: 'unknown' },
    note: editable ? '显示的是 H2B 保存的启动参数；继承项的实际值由 ' + harnessLabel + ' 配置和会话决定。' + (row.harness !== 'codex' ? '其他启动参数保持原样。' : '') + (modelLocked ? '模型由额外启动参数指定，初版请通过 CLI 修改模型。' : '') : '此配置仅供查看：需要标准本机受管 Worker 配置；容器、外部连接端点及 DSH 已有会话接入暂不支持编辑。',
    restartNote: row.harness === 'dsh' ? '重启会中断当前任务，并可能新建 DSH 工作会话。Agent 身份和地址保留，DSH 服务本身不会重启。' : '重启会中断当前任务。身份和地址保留，H2B 将尝试恢复原会话。'
  } };
}
function validateSettings(settings) {
  if (!settings || typeof settings !== 'object' || Object.keys(settings).some(k => !['cwd','model','sandbox','approval'].includes(k))) fail('INVALID_ARGUMENT', '不支持的启动参数。');
  if (typeof settings.cwd !== 'string' || !isAbsolute(settings.cwd) || settings.cwd.length > 4096 || settings.cwd.includes('\0')) fail('INVALID_ARGUMENT', '工作目录必须是绝对路径。');
  if (typeof settings.model !== 'string' || settings.model.length > 128 || (settings.model && !/^[A-Za-z0-9][A-Za-z0-9._:/-]*$/.test(settings.model))) fail('INVALID_ARGUMENT', '模型名称格式不正确。');
  if ((settings.sandbox !== undefined || settings.approval !== undefined) && (!['inherit','read-only','workspace-write','danger-full-access'].includes(settings.sandbox) || !['inherit','never','on-request','untrusted'].includes(settings.approval))) fail('INVALID_ARGUMENT', '不支持的沙箱或审批策略。');
}
export async function restartAgent(input, deps = {}) {
  const env = deps.env || process.env, request = deps.request || requestDaemon;
  if (input.confirmed !== true || Object.keys(input).some(k => !['operation','actor','version','settings','confirmed'].includes(k))) fail('CONFIRMATION_REQUIRED', '请核对参数并确认重启。');
  validateSettings(input.settings);
  if (!(await (deps.stat || stat)(input.settings.cwd)).isDirectory()) fail('INVALID_ARGUMENT', '工作目录不存在或不是目录。');
  const lockPath = join(launchStateDir(env), 'gui-agent-restart.lock');
  let lock;
  try { lock = await open(lockPath, 'wx', 0o600); } catch (error) { if (error.code === 'EEXIST') fail('RESTART_BUSY', '重启锁仍被占用，请稍后刷新；若 GUI 曾异常退出，请由运维核查 gui-agent-restart.lock。'); throw error; }
  try {
    const { row, document } = await launchSnapshot(input.actor, deps);
    if (!document.editable) fail('UNSUPPORTED_LAUNCH', document.note);
    if (document.version !== input.version) fail('CONFIG_CHANGED', '配置或进程已变化，请重新读取并确认。');
    if (Object.keys(input.settings).length !== document.fields.length || document.fields.some(key => !Object.hasOwn(input.settings, key)) || Object.keys(input.settings).some(key => !document.fields.includes(key))) fail('INVALID_ARGUMENT', '此 Harness 不支持这些启动参数。');
    if (document.modelLocked && input.settings.model !== document.settings.model) fail('INVALID_ARGUMENT', '模型由额外启动参数指定，请使用 CLI 修改。');
    const original = launchSpec(row), next = { ...original, cwd: input.settings.cwd };
    if (!document.modelLocked) {
      if (input.settings.model) next.model = input.settings.model; else delete next.model;
    }
    if (row.harness === 'codex') {
      next.args = [];
      if (input.settings.sandbox !== 'inherit') next.args.push('--sandbox', input.settings.sandbox);
      if (input.settings.approval !== 'inherit') next.args.push('--ask-for-approval', input.settings.approval);
    }
    // Never compensate an unconfirmed timeout: the daemon may still be applying the request.
    await request('down', { target: document.connectorId }, env);
    try {
      const result = await request('lifecycle.start', next, env);
      return { ok: true, actor: input.actor, connectorId: document.connectorId, applied: true, operationId: result.operationId };
    } catch (error) {
      if (error.code === 'OUTCOME_UNKNOWN') throw error;
      try {
        // Do not overwrite a concurrent start (including a failed new configuration).
        try { await (deps.read || readLaunch)(input.actor, env); fail('CONFIG_PRESENT', '启动配置已存在'); }
        catch (check) { if (check.code !== 'LAUNCH_NOT_FOUND') throw check; }
        await request('lifecycle.start', original, env);
      } catch { fail('RESTART_FAILED', '新参数启动失败；未自动恢复，请刷新核对 Worker 状态。', { cause: error.message, restored: false }); }
      fail('RESTART_FAILED', '新参数启动失败，已按原参数重新启动。', { cause: error.message, restored: true });
    }
  } finally { await lock.close(); await unlink(lockPath); }
}
export async function launchAction(input) {
  if (input.operation === 'agent-launch-config') {
    if (Object.keys(input).some(k => !['operation','actor'].includes(k))) fail('INVALID_ARGUMENT', '不支持的查询参数。');
    return { ok: true, operation: input.operation, document: (await launchSnapshot(input.actor)).document };
  }
  return { ok: true, operation: input.operation, document: await restartAgent(input) };
}
