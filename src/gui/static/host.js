import { installPacTools } from '../integration/pac-tools.js';
import { installPacCarrier } from '../integration/pac-carrier.js';
import { createGuiStudioHost, installGuiStudioTools } from '../integration/gui-studio-host.mjs';
import { subagentRelease } from '../integration/subagent-release.mjs';
import { kanbanStatus } from '../integration/kanban-gui.mjs';
import { createWorkflowWorkbench } from '../integration/workflow-workbench.mjs';
import { installWorkflowTools } from '../integration/workflow-tools.js';
import { launchStateDir } from '../integration/agent-launch-settings.mjs';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';
import { homedir } from 'node:os';
import path from 'node:path';
import { installSessionTools } from '../integration/session-tools.js';
import { MANAGEMENT_OPERATIONS, assertManagementRequest, createConsoleManagement } from '../integration/console-management.mjs';

const ROUTE = '/plugins/h2b-talk/rpc';
const MAX_BODY_BYTES = 262144;
const BRIDGE_PATH = fileURLToPath(new URL('../h2b-session-bridge.mjs', import.meta.url));
const PACKAGE_ROOT = fileURLToPath(new URL('..', import.meta.url));
const H2B_STATE_ROOT = process.env.HARNESS_STATE_DIR?.trim()
  || path.join(process.env.H2B_HOME?.trim() || path.join(homedir(), '.h2b'), 'state');
const LEDGER_PATH = process.env.H2B_DSH_DEMO_LEDGER?.trim()
  ? path.resolve(PACKAGE_ROOT, process.env.H2B_DSH_DEMO_LEDGER.trim())
  : path.join(H2B_STATE_ROOT, 'dsh-web-injected.json');
const LEDGER_ROOT = path.dirname(LEDGER_PATH);
const CONTROL_BRIDGE_PATH = fileURLToPath(new URL('../h2b-control-bridge.mjs', import.meta.url));
const PROTOCOL_VERSION = 2;
const OPERATIONS = new Set([
  'connect',
  'status',
  'pending',
  'mark-injected',
  'send',
  'reply',
  'ack',
  'participant-authorize',
  'participant-revoke',
  'participant-list',
  'contact-add',
  'contact-remove',
  'contact-list',
  'remote-contact-add',
  'remote-contact-remove',
  'remote-contact-list',
  'reception-policy-get',
  'reception-policy-set',
  'whitelist-list',
  'whitelist-add',
  'whitelist-remove',
  'remote-reception-policy-get',
  'remote-reception-policy-set',
  'remote-whitelist-list',
  'remote-whitelist-add',
  'remote-whitelist-remove',
  'chat-list',
  'chat-bind',
  'chat-binding',
  'chat-unbind',
  'chat-message-append',
      'chat-history-clear',
      'chat-work-link',
      'chat-work-unlink',
      'remote-identity',
      'remote-connect',
      'remote-pending',
      'remote-mark-injected',
      'remote-complete',
      'remote-reply',
      'remote-ack',
      'remote-bind',
      'remote-unbind',
      'remote-bindings',
      'remote-name-configure',
      'remote-broadcast-configure',
      'remote-broadcast',
      'remote-disconnect',
      'disconnect'
]);
const AGENT_TASK_OPERATIONS = new Set([
  'agent.task.capabilities', 'agent.task.start', 'agent.task.status',
  'agent.task.result', 'agent.task.cancel', 'agent.task.observe'
]);

// Read-only control-plane surface. Every argv string is a repository-owned
// constant; the browser can only choose an allowlisted operation key.
const CONTROL_QUERIES = Object.freeze({
  version: { section: 'overview', label: '版本', command: 'h2b version --json' },
  processes: { section: 'overview', label: '进程', command: 'h2b ps --json' },
  topology: { section: 'overview', label: '全景', command: 'h2b top --json' },
  doctor: { section: 'overview', label: '诊断', command: 'h2b doctor --json' },
  service: { section: 'system', label: '服务', command: 'h2b service --json' },
  targets: { section: 'agents', label: '目标', command: 'h2b targets --json' },
  hosts: { section: 'agents', label: '节点', command: 'h2b hosts --json' },
  agents: { section: 'agents', label: 'Agent', command: 'h2b agent list --json' },
  workflows: { section: 'workflows', label: 'Workflow', command: 'h2b workflow list --json' },
  routines: { section: 'schedules', label: 'Routine', command: 'h2b routine list --json' },
  outbox: { section: 'delivery', label: '发件箱', command: 'h2b outbox list --json' },
  adapters: { section: 'integrations', label: 'Adapter', command: 'h2b adapter list --json' },
  channels: { section: 'integrations', label: 'Channel', command: 'h2b channel list --json' },
  adapterPins: { section: 'integrations', label: '接收绑定', command: 'h2b adapter pins --json' },
  organization: { section: 'system', label: '组织槽位', command: 'h2b org status --json' },
  autoupdate: { section: 'system', label: '自动更新', command: 'h2b autoupdate status --json' }
});
const CONTROL_ACTIONS = new Set([
  'dispatch-matrix', 'profile-list', 'org-show', 'routine-templates', 'routine-template',
  'workflow-plan', 'workflow-run', 'workflow-status', 'workflow-cancel',
  'workflow-history-status', 'workflow-history-list', 'workflow-complete', 'workflow-fail',
  'routine-plan', 'routine-add', 'routine-status', 'routine-pause', 'routine-resume', 'routine-remove',
  'delivery-status', 'trajectory', 'log-query',
  'adapter-status', 'adapter-doctor', 'adapter-identities', 'adapter-start', 'adapter-stop',
  'adapter-pin', 'adapter-unpin', 'adapter-reload',
  'channel-join', 'channel-part',
  'agent-launch-config', 'agent-restart', 'agent-create', 'agent-destroy', 'agent-start', 'agent-stop'
]);
const controlPreviews = new Map();

function validateControlWrite(input) {
  const now = Date.now();
  for (const [token, preview] of controlPreviews) if (preview.expiresAt <= now) controlPreviews.delete(token);
  if (['workflow-run', 'routine-add'].includes(input.operation)) {
    const preview = controlPreviews.get(input.previewToken);
    if (!preview || preview.expiresAt <= now || preview.yaml !== input.yaml || preview.from !== input.from || preview.kind !== (input.operation === 'routine-add' ? 'routine-plan' : 'workflow-plan')) {
      throw new Error('workflow preview is missing, expired, or does not match the current request');
    }
    controlPreviews.delete(input.previewToken);
  }
  if (['workflow-complete','workflow-fail','workflow-cancel', 'routine-add', 'routine-pause', 'routine-resume', 'routine-remove', 'channel-join', 'channel-part', 'agent-restart', 'agent-create', 'agent-destroy', 'agent-start', 'agent-stop', 'adapter-start', 'adapter-stop', 'adapter-pin', 'adapter-unpin', 'adapter-reload'].includes(input.operation) && input.confirmed !== true) {
    throw new Error('explicit confirmation is required for this h2b control action');
  }
}

function attachControlPreview(input, document) {
  if (!['workflow-plan', 'routine-plan'].includes(input.operation)) return document;
  while (controlPreviews.size >= 32) controlPreviews.delete(controlPreviews.keys().next().value);
  const previewToken = randomUUID();
  controlPreviews.set(previewToken, { kind: input.operation, yaml: input.yaml, from: input.from, expiresAt: Date.now() + 5 * 60 * 1000 });
  return { ...document, previewToken, previewExpiresInSeconds: 300 };
}

const capabilityCaches = new WeakMap();
const managementControllers = new WeakMap();
async function consoleManagement(ctx, input) {
  if (!managementControllers.has(ctx)) managementControllers.set(ctx, createConsoleManagement({
    requireCapability: async operation => {
      const caps = await controlCapabilities(ctx);
      if (!caps.management.includes(operation)) throw Object.assign(new Error('Installed CLI does not support this management operation'), { code: 'UNSUPPORTED_COMMAND' });
    },
    readOnlyStatus: async () => (await controlQuery(ctx, { operation: 'organization' })).document
  }));
  return managementControllers.get(ctx)(input);
}
async function controlCapabilities(ctx) {
  let capabilityCache = capabilityCaches.get(ctx);
  if (!capabilityCache || capabilityCache.expiresAt <= Date.now()) {
    capabilityCache = { expiresAt: Date.now() + 60000, promise: (async () => {
      const result = await ctx.shell.run(resolveGuiCommand(ctx, {
        command: 'node "$H2B_CLI_CAPABILITIES_PATH"',
        env: { H2B_CLI_CAPABILITIES_PATH: fileURLToPath(new URL('../h2b-cli-capabilities.mjs', import.meta.url)) },
        workdir: PACKAGE_ROOT, timeoutMs: process.env.HYPRIAL_DESKTOP_COMPONENTS === '1' ? 60000 : 30000, stdoutMaxBytes: 262144
      }));
      let doc;
      try { doc = JSON.parse(result.stdout?.text || ''); } catch {}
      if (result.timedOut || result.aborted || result.exitCode !== 0 || result.stdout?.truncated || !doc?.ok || !Array.isArray(doc.supported) || !Array.isArray(doc.unavailable)) {
        const code = result.timedOut ? 'CAPABILITY_PROBE_TIMEOUT' : 'CAPABILITY_PROBE_FAILED';
        return { supported: [], unavailable: [...Object.keys(CONTROL_QUERIES).map(operation => ({ operation, kind: 'query' })), ...[...CONTROL_ACTIONS].map(operation => ({ operation, kind: 'action' }))].map(item => ({ ...item, code, message: 'Installed H2B CLI capability discovery failed; operation is unavailable' })) };
      }
      return doc;
    })() };
    capabilityCaches.set(ctx, capabilityCache);
  }
  const detected = await capabilityCache.promise.catch(() => ({ supported: [], unavailable: [...Object.keys(CONTROL_QUERIES).map(operation => ({ operation, kind: 'query' })), ...[...CONTROL_ACTIONS].map(operation => ({ operation, kind: 'action' }))].map(item => ({ ...item, code: 'CAPABILITY_PROBE_FAILED', message: 'CLI help discovery could not be launched' })) }));
  const supported = (kind, operation) => detected.supported.some(item => item.kind === kind && item.operation === operation);
  return {
    ok: true,
    protocolVersion: 1,
    mode: 'controlled-write',
    checkedAt: detected.checkedAt || null,
    unavailable: detected.unavailable,
    queries: Object.keys(CONTROL_QUERIES).filter(operation => supported('query', operation)).map((operation) => ({
      operation,
      section: CONTROL_QUERIES[operation].section,
      label: CONTROL_QUERIES[operation].label
    })),
    actions: [...CONTROL_ACTIONS].filter(operation => supported('action', operation)).sort(),
    management: MANAGEMENT_OPERATIONS.filter(operation => supported('action', operation))
  };
}

async function requireControlCapability(ctx, kind, operation) {
  const caps = await controlCapabilities(ctx);
  if (kind === 'query' ? caps.queries.some(item => item.operation === operation) : caps.actions.includes(operation)) return;
  const failure = caps.unavailable.find(item => item.kind === kind && item.operation === operation);
  throw Object.assign(new Error(failure?.message || 'Installed CLI capability is unavailable'), { code: failure?.code || 'UNSUPPORTED_COMMAND', details: { operation, kind } });
}

function controlFailure(document, fallback, code = 'COMMAND_FAILED') {
  const failure = document?.error;
  return Object.assign(new Error(diagnostic(failure?.message) || diagnostic(failure) || fallback), {
    code: typeof failure?.code === 'string' ? failure.code : code,
    ...(failure?.details === undefined ? {} : { details: failure.details })
  });
}

async function controlQuery(ctx, input) {
  if (!input || typeof input !== 'object' || Array.isArray(input)) throw new Error('h2b control query requires a JSON object');
  const query = CONTROL_QUERIES[input.operation];
  if (!query) throw new Error('unsupported h2b control query');
  await requireControlCapability(ctx, 'query', input.operation);
  const spec = resolveGuiCommand(ctx, { command: query.command, timeoutMs: 10000, stdoutMaxBytes: MAX_BODY_BYTES });
  const result = await ctx.shell.run(spec);
  if (result.timedOut) throw controlFailure(null, query.label + ' query timed out', 'COMMAND_TIMEOUT');
  if (result.aborted) throw controlFailure(null, query.label + ' query was aborted', 'COMMAND_ABORTED');
  if (result.exitCode !== 0) {
    let failed;
    try {
      failed = JSON.parse(result.stdout?.text || '');
    } catch {}
    const error = controlFailure(failed, diagnostic(result.stderr?.text) || query.label + ' query failed');
    if (input.operation === 'organization' && /read-only file system/i.test(error.message)) {
      const fallback = await controlAction(ctx, { operation: 'org-show' });
      const view = fallback.document;
      if (!['absent', 'accepted'].includes(view?.status)) throw error;
      return { ok: true, operation: input.operation, document: {
        slot: view.status, accepted: view.status === 'accepted' ? view : null,
        pending: null, pendingCount: null, partial: true,
        warning: '当前 CLI 的组织候选查询尝试写目录，已退回只读组织详情；候选数量未知。',
        diagnostic: { code: error.code, message: error.message }, source: 'h2b org show --json'
      } };
    }
    throw error;
  }
  if (result.stdout?.truncated) throw controlFailure(null, query.label + ' query output exceeded the safety limit', 'OUTPUT_TOO_LARGE');
  let document;
  try { document = JSON.parse(result.stdout?.text || ''); }
  catch { throw controlFailure(null, query.label + ' query returned invalid JSON', 'INVALID_RESPONSE'); }
  if (!document || typeof document !== 'object') throw new Error(query.label + ' query returned an invalid document');
  return { ok: true, operation: input.operation, document };
}

async function controlAction(ctx, input) {
  if (!input || typeof input !== 'object' || Array.isArray(input) || !CONTROL_ACTIONS.has(input.operation)) {
    throw new Error('unsupported h2b control action');
  }
  validateControlWrite(input);
  await requireControlCapability(ctx, 'action', input.operation);
  const spec = resolveGuiCommand(ctx, {
    command: 'node "$H2B_CONTROL_BRIDGE_PATH"',
    stdin: JSON.stringify(input),
    env: { H2B_CONTROL_BRIDGE_PATH: CONTROL_BRIDGE_PATH },
    workdir: PACKAGE_ROOT,
    // SQLite read-only connections still need their WAL coordination files.
    sandboxPolicy: { mode: 'workspace-write', workspaceRoot: ['agent-launch-config', 'agent-restart'].includes(input.operation) ? launchStateDir() : PACKAGE_ROOT },
    timeoutMs: input.operation === 'agent-restart' ? 300000 : 30000,
    stdoutMaxBytes: 524288
  });
  const result = await ctx.shell.run(spec);
  if (result.timedOut) throw controlFailure(null, 'h2b control action timed out', 'COMMAND_TIMEOUT');
  if (result.aborted) throw controlFailure(null, 'h2b control action was aborted', 'COMMAND_ABORTED');
  if (result.stdout?.truncated) throw controlFailure(null, 'h2b control action output exceeded the safety limit', 'OUTPUT_TOO_LARGE');
  let document;
  try { document = JSON.parse(result.stdout?.text || ''); }
  catch { throw controlFailure(null, 'h2b control action returned invalid JSON', 'INVALID_RESPONSE'); }
  if (result.exitCode !== 0 || !document || document.ok !== true) {
    throw controlFailure(document, diagnostic(result.stderr?.text) || 'h2b control action failed');
  }
  return attachControlPreview(input, document);
}

// ★ kanban 薄入口 —— 与 imskin-host-plugin.js 里那份 allowlist 【必须一致】。
//
// ⚠️⚠️ 【bridge 不在这个包里】—— 它住在 HyprialOS/kanban-tw。
//   上一版把它放在包内,而那意味着【别人的业务逻辑住在这个仓】:
//     换掉这个插件就得重写,而它里面的报表名改了要两个仓一起改。
//   ⇒ 记:一个「薄入口」薄的应该是【这一侧】—— 判断标准很简单:
//     这个仓的代码里出现过对方的业务词吗?出现了,它就不是薄的。
//   ⇒ 现在这里只有一句常量命令 + 一个由运维在 DSH 启动前设的路径。
//     那个路径【不是浏览器输入】,所以"没有请求的字节进 argv"这条仍然成立。
// ⚠️ 【每次调用时读】,不在模块加载时定死 —— 静态 Host 只有 DSH 重启才重新加载,
//   而模块级常量会把「启动那一刻的值」冻住,让「改了环境变量却不生效」变得无声。
const kanbanBridgePath = () => process.env.H2B_KANBAN_BRIDGE || '';
// ⚠️ TaskWarrior 的库目录 —— 沙箱要放行它,否则 bridge 连读都做不到(WAL 要写)。
//
// ⚠️⚠️ 【没有默认值】,这是有意的。上一版写的是 `process.env.HOME + '/.task'` ——
//   那是把【对方的默认布局】复制进了这个仓,而这个仓是「薄入口」:
//   判断标准就是上面那句「这个仓的代码里出现过对方的业务词吗?出现了,它就不是薄的」。
//   `~/.task` 正是对方的业务词。
//   ⇒ 记:一个默认值也是一份复制品 —— 它比显式赋值更难被发现,
//     因为它平时【正好是对的】,只在对方改了布局时才错,而那时它不报错,只是空板。
// ⇒ 与 H2B_KANBAN_BRIDGE 同一条规矩:不猜,没设就【响】。
//   那个值该从 `task _get rc.data.location` 解引用得到 —— 而那件事发生在 kanban
//   那一侧的安装器里(它把该设的值打印出来),不在这里。
const kanbanDataDir = () => process.env.H2B_KANBAN_DATA_DIR || '';
const KANBAN_OPERATIONS = new Set(['board', 'board-html', 'export']);
const KANBAN_PROTOCOL_VERSION = 1;
// ⚠️ 4MB —— 按 118 张卡 243KB 估，够十几倍增长；上限仍在，只是不再卡住正常的板。
const KANBAN_MAX_BODY_BYTES = 4 * 1024 * 1024;

function kanbanCapabilities() {
  return {
    ok: true,
    protocolVersion: KANBAN_PROTOCOL_VERSION,
    operations: [...KANBAN_OPERATIONS].sort()
  };
}

async function kanbanRpc(ctx, input) {
  if (!input || typeof input !== 'object' || Array.isArray(input) || !KANBAN_OPERATIONS.has(input.operation)) {
    throw new Error('unsupported h2b kanban operation');
  }
  // The command is deliberately constant; caller values travel in stdin only.
  if (process.env.H2B_GUI_KANBAN_CONFIG_ERROR) throw new Error(process.env.H2B_GUI_KANBAN_CONFIG_ERROR);
  const bridgePath = kanbanBridgePath();
  if (!bridgePath) {
    // ⚠️ 不猜路径:猜错的样子是"装了但不生效",而那是无声的。
    throw new Error('H2B_KANBAN_BRIDGE is not set — point it at kanban-tw\'s ' +
      'tools/panel/bridge.py on this machine, before DSH starts');
  }
  // ⚠️ 同上一条:没设就响,不猜。猜错的样子是"板是空的",而空板看起来完全正常。
  const dataDir = kanbanDataDir();
  if (!dataDir) {
    throw new Error('H2B_KANBAN_DATA_DIR is not set — point it at the TaskWarrior ' +
      'data directory on this machine (kanban\'s installer prints the value; it comes ' +
      'from `task _get rc.data.location`), before DSH starts');
  }
  const spec = resolveGuiCommand(ctx, {
    command: 'python3 "$H2B_KANBAN_BRIDGE" rpc',
    stdin: JSON.stringify(input),
    env: process.env.H2B_KANBAN_TASK_BIN
      ? { H2B_KANBAN_BRIDGE: bridgePath, H2B_KANBAN_TASK_BIN: process.env.H2B_KANBAN_TASK_BIN }
      : { H2B_KANBAN_BRIDGE: bridgePath },
    timeoutMs: 20000,
    // ⚠️⚠️ board-html 【比 h2b 那条通道的载荷大一个量级】：
    //   118 张卡的整页 HTML ≈ 243KB，而 MAX_BODY_BYTES 是 256KB ——
    //   JSON 转义之后就超了，报 "output exceeded the safety limit"（响的，不是静默）。
    //   ⇒ 而板【只会越来越大】：done 列按设计不设上限（yaosh 2026-08-19 定）。
    //   ⇒ 所以这里给 kanban 单独一个更大的限额，而不是把 MAX_BODY_BYTES 整个提高 ——
    //     那会连带放宽 h2b 那条通道，而它的载荷本来就该是小的。
    //   ⚠️ 仍然【有上限】：这道闸防的是"渲染器疯了输出几个 G"，不是防大板。
    stdoutMaxBytes: KANBAN_MAX_BODY_BYTES
  });
  // ⚠️⚠️ TaskWarrior 3.x 即使【只读操作】也要在库目录写 WAL/SHM ——
  //   默认沙箱只给 workspaceRoot + /tmp 可写 ⇒ 库目录打不开：
  //   "unable to open database file: Error code 14"
  //   ⇒ 把可写范围【收窄到库目录本身】，而不是放开全权。
  spec.sandboxPolicy = { mode: 'workspace-write', workspaceRoot: dataDir };
  const result = await ctx.shell.run(spec);
  if (result.timedOut) throw new Error('kanban bridge timed out');
  if (result.aborted) throw new Error('kanban bridge was aborted');
  if (result.exitCode !== 0) {
    let bridgeError = '';
    try {
      const failed = JSON.parse(result.stdout?.text || '');
      bridgeError = diagnostic(failed?.error?.message);
    } catch {}
    throw new Error('kanban bridge failed: ' + (bridgeError || diagnostic(result.stderr?.text) || 'unknown error'));
  }
  if (result.stdout?.truncated) throw new Error('kanban bridge output exceeded the safety limit');
  const document = JSON.parse(result.stdout?.text || '');
  if (!document || typeof document !== 'object' || Array.isArray(document)) throw new Error('kanban bridge returned an invalid document');
  return document;
}


function capabilities() {
  return {
    ok: true,
    protocolVersion: PROTOCOL_VERSION,
    operations: [...OPERATIONS].sort(),
    features: ['durable-chat-history', 'durable-work-link', 'orphan-auto-recovery', 'unique-agent-chat', 'shared-direct-work-links']
  };
}

function clean(value, limit) {
  if (typeof value !== 'string') return '';
  const text = value.trim();
  if (!text || text.length > limit || /[\u0000-\u001f\u007f]/.test(text)) return '';
  return text;
}

function diagnostic(value) {
  return String(value || '').replace(/\s+/g, ' ').trim().slice(0, 300);
}

function bridgeFailure(document, fallback) {
  const failure = new Error(diagnostic(document?.error?.message) || fallback);
  failure.code = diagnostic(document?.error?.code) || 'BRIDGE_ERROR';
  if (document?.error?.details !== undefined) failure.details = document.error.details;
  return failure;
}

async function readJson(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) throw new Error('request body is too large');
    chunks.push(chunk);
  }
  let value;
  // Native JSON parse errors may quote credential-bearing request text.
  try { value = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
  catch { throw Object.assign(new Error('request body must be valid JSON'), { code: 'INVALID_JSON' }); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('request must be a JSON object');
  return value;
}

function writeJson(res, status, value) {
  const body = JSON.stringify(value);
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(body)
  });
  res.end(body);
}

async function bridgeRpc(ctx, input, trustedTool = false) {
  if (!input || typeof input !== 'object' || Array.isArray(input) ||
      (!OPERATIONS.has(input.operation) && !AGENT_TASK_OPERATIONS.has(input.operation) && !(trustedTool && ['pac-tool', 'pac-poll', 'pac-reserve', 'pac-received', 'session-tool', 'carrier-list', 'carrier-heartbeat', 'carrier-pending', 'carrier-mark-injected'].includes(input.operation)))) {
    throw new Error('unsupported h2b demo operation');
  }
  const spec = resolveGuiCommand(ctx, {
    // The active DSH Session may use any workspace. Resolve package-owned
    // files from this Host module instead of inheriting that workspace.
    command: 'node "$H2B_DSH_BRIDGE_PATH" rpc',
    stdin: JSON.stringify(input),
    env: {
      H2B_DSH_BRIDGE_PATH: BRIDGE_PATH,
      H2B_DSH_DEMO_LEDGER: LEDGER_PATH
    },
    workdir: PACKAGE_ROOT,
    // Runtime state must survive replacement of the upgrade-managed GUI source.
    // Only the fixed ledger directory is writable; package code remains readable.
    sandboxPolicy: {
      mode: 'workspace-write',
      workspaceRoot: LEDGER_ROOT
    },
    timeoutMs: input.operation.startsWith('pac-') ? 30000 : 10000,
    stdoutMaxBytes: trustedTool && input.tool === 'workflow-node' ? 1048576 : MAX_BODY_BYTES
  });
  const result = await ctx.shell.run(spec);
  if (result.timedOut) throw new Error('h2b demo bridge timed out');
  if (result.aborted) throw new Error('h2b demo bridge was aborted');
  if (result.exitCode !== 0) {
    let failed;
    try {
      failed = JSON.parse(result.stdout?.text || '');
    } catch {}
    throw bridgeFailure(failed, 'h2b demo bridge failed: ' + (diagnostic(result.stderr?.text) || 'unknown error'));
  }
  if (result.stdout?.truncated) throw new Error('h2b demo bridge output exceeded the safety limit');
  const document = JSON.parse(result.stdout?.text || '');
  if (!document || typeof document !== 'object' || Array.isArray(document)) throw new Error('h2b demo bridge returned an invalid document');
  return document;
}

// Desktop GUI commands own application state, separate from Agent workspaces.
// Keep the ACL sandbox enabled and grant only this private state directory.
function resolveGuiCommand(ctx, request) {
  if (process.env.HYPRIAL_DESKTOP_COMPONENTS === '1' && !request.sandboxPolicy) {
    request = { ...request, sandboxPolicy: { mode: 'workspace-write', workspaceRoot: H2B_STATE_ROOT } };
  }
  return ctx.shell.resolve(request);
}

async function targets(ctx) {
  const spec = resolveGuiCommand(ctx, {
    command: 'h2b targets --json',
    timeoutMs: 5000,
    stdoutMaxBytes: MAX_BODY_BYTES
  });
  const result = await ctx.shell.run(spec);
  if (result.timedOut) throw new Error('h2b targets timed out');
  if (result.aborted) throw new Error('h2b targets was aborted');
  if (result.exitCode !== 0) {
    let failure;
    try { failure = JSON.parse(result.stdout?.text || ''); } catch {}
    throw controlFailure(failure, diagnostic(result.stderr?.text) || `Hyprial targets exited with code ${result.exitCode}`);
  }
  if (result.stdout?.truncated) throw new Error('h2b targets output exceeded the safety limit');
  const document = JSON.parse(result.stdout?.text || '');
  if (!document || document.ok !== true || !Array.isArray(document.targets)) throw new Error('h2b targets returned an invalid document');
  return {
    ok: true,
    targets: document.targets.flatMap((item) => {
      if (!item || typeof item !== 'object') return [];
      const targetKind = clean(item.targetKind, 64);
      const targetUri = clean(item.targetUri, 2048);
      const actor = clean(item.actor, 2048);
      const status = clean(item.status, 64);
      return ['agent', 'channel_route'].includes(targetKind) && targetUri
        ? [{ targetKind, targetUri, actor, status }]
        : [];
    })
  };
}

function installRemoteCarrier(ctx) {
  const remoteInbox = new Map();
  const remoteTurns = new Map();
  const remoteCurrentTurn = new Map();
  const remoteCompleted = new Map();
  const deliveryInFlight = new Map();
  const bindingsBySession = new Map();
  const localTurns = new Map();
  const remoteWarnings = new Map();
  let polling = false;

  function warnRemote(key, error) {
    const now = Date.now();
    if (now - (remoteWarnings.get(key) || 0) < 30000) return;
    remoteWarnings.set(key, now);
    console.warn('[dsh-hyprial-plugin] remote entry unavailable for ' + key + ':', diagnostic(error?.message));
  }

  function messageText(message) {
    if (!message || !Array.isArray(message.content)) return '';
    return message.content
      .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
      .map((block) => block.text)
      .join('\n')
      .trim();
  }

  const restoredAgents = new Map();
  let carrierDisposed = false;
  ctx.on?.('dispose', () => {
    carrierDisposed = true;
    // SessionController owns restored agents and their preset scopes.
    restoredAgents.clear();
  });
  async function carrierAgent(sessionId) {
    if (carrierDisposed) return undefined;
    const live = ctx.agents.get(sessionId);
    if (live) return live;
    const controller = ctx.get?.('sessionController');
    if (typeof controller?.resolveAgent !== 'function') {
      throw new Error('Hyprial carrier requires SessionController.resolveAgent to restore a session');
    }
    if (!restoredAgents.has(sessionId)) {
      // Raw agents.resume bypasses persisted preset/model composition and loses native tools.
      const loading = Promise.resolve().then(() => controller.resolveAgent(sessionId)).then(result => {
        if (result?.error || result?.agent?.session?.id !== sessionId) {
          throw new Error('Hyprial carrier could not resolve the requested session');
        }
        return result.agent;
      }).finally(() => {
        if (restoredAgents.get(sessionId) === loading) restoredAgents.delete(sessionId);
      });
      restoredAgents.set(sessionId, loading);
    }
    const agent = await restoredAgents.get(sessionId);
    return carrierDisposed ? undefined : agent;
  }

  function deliver(remote) {
    const key = remote.sessionId + ':' + remote.messageId;
    if (deliveryInFlight.has(key)) return deliveryInFlight.get(key);
    const promise = (async () => {
      await bridgeRpc(ctx, { operation: 'remote-complete', sessionId: remote.sessionId, messageId: remote.messageId, message: remote.text || '' });
      remoteCompleted.delete(key);
    })().finally(() => deliveryInFlight.delete(key));
    deliveryInFlight.set(key, promise);
    return promise;
  }

  function broadcastLocal(sessionId, role, eventId, message) {
    for (const binding of bindingsBySession.get(sessionId) || []) {
      if (!binding.broadcastRoute || binding.broadcastMode === 'off') continue;
      if (role === 'user' && binding.broadcastMode !== 'full') continue;
      const prefix = role === 'user' ? '[DSH 用户]\n' : '[DSH Agent]\n';
      void bridgeRpc(ctx, { operation: 'remote-broadcast', sessionId, adapter: binding.adapter, role, eventId, message: prefix + message })
        .catch((error) => warnRemote(sessionId + ':broadcast', error));
    }
  }

  function queuedRemoteMessage(agent, messageId) {
    const inbox = agent.inbox;
    return Boolean(inbox && [inbox.nextTurn, inbox.nextStep].some(messages =>
      Array.isArray(messages) && messages.some(message => message && message.id === messageId)));
  }

  // Session exposes snapshotEvents(), not the API controller's `events` DTO.
  // Keep one append-only index per Session; ordinary polls read only new events.
  const recoveredHistories = new WeakMap();
  function recoveredTurn(agent, messageId) {
    const session = agent.session;
    const modern = typeof session?.snapshotEvents === 'function';
    const legacy = !modern && Array.isArray(session?.events) ? session.events : null;
    if (!modern && !legacy) throw new Error('Remote recovery requires a readable Session history');
    const end = modern ? session.seq : legacy.length;
    const start = modern ? (session.inheritedEventCount ?? 0) : 0;
    if (!Number.isSafeInteger(end) || !Number.isSafeInteger(start) || start < 0 || end < start) throw new Error('Invalid Session history cursor');
    let index = recoveredHistories.get(session);
    const last = index?.cursor ? (modern && typeof session.eventAt === 'function' ? session.eventAt(index.cursor - 1) : legacy?.[index.cursor - 1]) : undefined;
    if (!index || index.start !== start || end < index.cursor || (last !== undefined && last !== index.last)) {
      index = { start, cursor: start, last: undefined, currentTurn: null, messages: new Map(), turns: new Map() };
    }
    if (end > index.cursor) {
      const events = modern ? session.snapshotEvents(index.cursor, end) : legacy.slice(index.cursor, end);
      // Fail closed on read/shape errors; missing history is not permission to resubmit.
      if (!Array.isArray(events) || events.length !== end - index.cursor || events.some((event, offset) =>
        !event || typeof event.type !== 'string' || !event.data || (modern && event.seq !== index.cursor + offset))) {
        throw new Error('Incomplete Session history snapshot');
      }
      for (const event of events) {
        const data = event.data;
        if (event.type === 'turn/start') {
          index.currentTurn = data.turn;
          index.turns.set(data.turn, { turn: data.turn, text: '', done: false });
        } else if (event.type === 'user/message' && typeof data.id === 'string' && !index.messages.has(data.id)) {
          index.messages.set(data.id, index.turns.get(index.currentTurn) || { turn: null, text: '', done: false });
        } else if (event.type === 'assistant/message') {
          const record = index.turns.get(data.turn);
          if (record) record.text = messageText(data.message) || record.text;
        } else if (event.type === 'turn/end') {
          const record = index.turns.get(data.turn);
          if (record) record.done = true;
          index.turns.delete(data.turn);
          if (index.currentTurn === data.turn) index.currentTurn = null;
        }
      }
      index.cursor = end;
      index.last = events.at(-1);
    }
    recoveredHistories.set(session, index);
    const record = index.messages.get(messageId);
    if (record && !Number.isInteger(record.turn)) throw new Error('Remote message has no recoverable turn');
    return record ? { ...record } : null;
  }

  async function poll() {
    if (polling) return;
    polling = true;
    try {
      for (const remote of [...remoteCompleted.values()]) {
        try { await deliver(remote); }
        catch (error) { warnRemote(remote.sessionId, error); }
      }
      const listed = await bridgeRpc(ctx, { operation: 'remote-bindings' });
      const bindings = Array.isArray(listed.bindings) ? listed.bindings : [];
      const carriers = await bridgeRpc(ctx, { operation: 'carrier-list' }, true);
      const managed = new Map((carriers.sessions || []).map(item => [item.sessionId, item]));
      bindingsBySession.clear();
      for (const binding of bindings) {
        if (!binding || typeof binding.sessionId !== 'string') continue;
        const current = bindingsBySession.get(binding.sessionId) || [];
        current.push(binding);
        bindingsBySession.set(binding.sessionId, current);
      }
      for (const binding of [...new Map([...bindings, ...(carriers.sessions || []).filter(item => !bindings.some(b => b.sessionId === item.sessionId))].map(item => [item.sessionId, item])).values()]) {
        if (!binding || typeof binding.sessionId !== 'string') continue;
        const sessionId = binding.sessionId;
        try {
          const selected = managed.get(sessionId);
          if (selected?.enabled === false) continue;
          const agent = selected?.humanChat ? undefined : await carrierAgent(sessionId);
          if (!agent && !selected?.humanChat) continue;
          // Legacy bound entries are adopted once; then renew, never register each tick.
          if (!selected || selected.legacy) await bridgeRpc(ctx, { operation: 'remote-connect', sessionId }, true);
          const heartbeat = await bridgeRpc(ctx, { operation: 'carrier-heartbeat', sessionId }, true);
          if (heartbeat.disabled || selected?.humanChat) continue;
          const pending = await bridgeRpc(ctx, { operation: 'carrier-pending', sessionId }, true);
          // An injected delivery may still be awaiting its correlated reply. Revisit it so
          // a restarted Host can recover the completed DSH turn from the durable session log.
          const candidate = Array.isArray(pending.messages)
            ? pending.messages.find((item) => item)
            : pending.message;
          if (!candidate) continue;
          const deliveryId = String(candidate.deliveryId || candidate.messageId || '');
          const messageId = String(candidate.messageId || '');
          if (!deliveryId || !messageId) continue;
          if (!agent) continue;
          const recovered = recoveredTurn(agent, messageId);
          if (recovered?.done) {
            const completed = { sessionId, messageId, deliveryId, text: recovered.text };
            remoteCompleted.set(sessionId + ':' + messageId, completed);
            await bridgeRpc(ctx, { operation: 'carrier-mark-injected', sessionId, deliveryId, messageId }, true);
            await deliver(completed);
            continue;
          }
          if (recovered) {
            remoteTurns.set(sessionId + ':' + recovered.turn, { sessionId, messageId, deliveryId, text: recovered.text });
          } else if (!remoteInbox.has(sessionId + ':' + messageId)) {
            // Queue projection is committed before synchronous followup returns.
            // No await between checking it and admission on the shared Agent.
            if (!queuedRemoteMessage(agent, messageId)) {
              // Report any throw without a memory marker; the next poll checks
              // the queue first, including an append committed before the error.
              agent.followup(Object.freeze({
                  id: messageId,
                  role: 'user',
                  content: [Object.freeze({
                    type: 'text',
                    text: (String(candidate.from || '').startsWith('agent:') ? '【H2B Agent · ' : '【飞书 · ') + String(candidate.from || 'unknown') + '】\n' + '[messageId=' + messageId + '; intent=' + String(candidate.intent || 'unknown') + ']\n' + (String(candidate.from || '').startsWith('agent:') ? 'Host 仅确认消费，不自动发送本轮最终文字。需要返回工作结果时用 h2b_session_reply 一次；收到结果或确认只消费，不回复待命/无动作。PAC 通知先读当前 context，以节点状态推进。\n' : '') + String(candidate.message || '')
                  })],
                  source: Object.freeze({ kind: 'user' })
              }));
            }
            remoteInbox.set(sessionId + ':' + messageId, { sessionId, messageId, deliveryId });
          }
          await bridgeRpc(ctx, { operation: 'carrier-mark-injected', sessionId, deliveryId, messageId }, true);
          remoteWarnings.delete(sessionId);
        } catch (error) {
          warnRemote(sessionId, error);
        }
      }
    } catch (error) {
      console.warn('[dsh-hyprial-plugin] remote entry poll failed:', diagnostic(error?.message));
    } finally {
      polling = false;
    }
  }

  ctx.on('session/event', (session, event) => {
    if (!session || !event) return;
    if (event.type === 'turn/start') remoteCurrentTurn.set(session.id, event.data.turn);
    if (event.type === 'user/message') {
      const inboxKey = session.id + ':' + String(event.data?.id || '');
      const remote = remoteInbox.get(inboxKey);
      const turn = remoteCurrentTurn.get(session.id);
      if (remote && Number.isInteger(turn)) {
        remoteInbox.delete(inboxKey);
        remoteTurns.set(session.id + ':' + turn, { ...remote, text: '' });
      } else if (Number.isInteger(turn) && (!event.data.source || event.data.source.kind !== 'plugin')) {
        const text = messageText(event.data);
        if (text) {
          localTurns.set(session.id + ':' + turn, { assistantText: '', assistantMessageId: '' });
          broadcastLocal(session.id, 'user', String(event.data.id || 'turn-' + turn + '-user'), text);
        }
      }
    }
    if (event.type === 'assistant/message') {
      const key = session.id + ':' + event.data.turn;
      const remote = remoteTurns.get(key);
      const text = messageText(event.data.message);
      if (remote && text) remoteTurns.set(key, { ...remote, text });
      const local = localTurns.get(key);
      if (local && text) localTurns.set(key, { ...local, assistantText: text, assistantMessageId: String(event.data.message?.id || '') });
    }
    if (event.type === 'turn/end') {
      const key = session.id + ':' + event.data.turn;
      const remote = remoteTurns.get(key);
      const local = localTurns.get(key);
      remoteTurns.delete(key);
      localTurns.delete(key);
      remoteCurrentTurn.delete(session.id);
      if (remote) {
        remoteCompleted.set(remote.sessionId + ':' + remote.messageId, remote);
        void deliver(remote).catch((error) => warnRemote(remote.sessionId, error));
      } else if (local?.assistantText) {
        broadcastLocal(session.id, 'assistant', local.assistantMessageId || 'turn-' + event.data.turn + '-assistant', local.assistantText);
      }
    }
  });
  let renewing = false;
  ctx.interval(async () => {
    if (renewing) return;
    renewing = true;
    try {
      const listed = await bridgeRpc(ctx, { operation: 'carrier-list' }, true);
      await Promise.all((listed.sessions || []).filter(item => item.enabled && (item.humanChat || ctx.agents.get(item.sessionId))).map(async item => {
        try { await bridgeRpc(ctx, { operation: 'carrier-heartbeat', sessionId: item.sessionId }, true); }
        catch (error) { warnRemote(item.sessionId, error); }
      }));
    } catch (error) { warnRemote('heartbeat', error); }
    finally { renewing = false; }
  }, 3000);
  ctx.interval(poll, 1000);
  void poll();
}

export const inject = ['shell', 'webServer', 'agents', 'timer', 'tools', 'sessions', 'sessionPersistence'];

const workbenches = new WeakMap();
function workflowWorkbench(ctx, input, context) {
  if (!workbenches.has(ctx)) workbenches.set(ctx, createWorkflowWorkbench({
    root: path.join(H2B_STATE_ROOT, 'workflow-workbench'),
    control: input => controlAction(ctx, input),
    native: (sessionId,operation,args) => bridgeRpc(ctx,{operation:'session-tool',sessionId,tool:'workflow-'+operation,args},true),
    observeNode: (sessionId,args) => bridgeRpc(ctx, {operation:'session-tool',sessionId,tool:'workflow-node',args}, true),
    resolveSession: async sessionId => {
      const attached = ctx.sessions.get(sessionId);
      if (attached) return attached.header;
      try { return (await ctx.sessionPersistence.inspect(sessionId)).meta; }
      catch { return null; }
    },
    identity: sessionId => bridgeRpc(ctx, { operation:'session-tool', sessionId, tool:'identity', args:{} }, true),
    prepare: sessionId => bridgeRpc(ctx, { operation:'session-tool', sessionId, tool:'prepare', args:{} }, true)
  }));
  return workbenches.get(ctx)(input, context);
}

const guiStudios = new WeakMap();
function guiStudio(ctx, input, context) {
  if (!guiStudios.has(ctx)) guiStudios.set(ctx, createGuiStudioHost({ root: path.join(H2B_STATE_ROOT, 'gui-studio') }));
  return guiStudios.get(ctx)(input, context);
}

export function apply(ctx) {
  installGuiStudioTools(ctx, (input, context) => guiStudio(ctx, input, context));
  installWorkflowTools(ctx, (input, context) => workflowWorkbench(ctx, input, context));
  installSessionTools(ctx, input => bridgeRpc(ctx, input, true));
  installPacTools(ctx, input => bridgeRpc(ctx, input, true));
  installPacCarrier(ctx, input => bridgeRpc(ctx, input, true));
  if (ctx.agents && typeof ctx.on === 'function' && typeof ctx.interval === 'function') installRemoteCarrier(ctx);
  return ctx.webServer.register({
      kind: 'exact',
      path: ROUTE,
      async handler(req, res) {
        if (req.method !== 'POST') {
          writeJson(res, 405, { ok: false, error: { message: 'method not allowed' } });
          return;
        }
        try {
          const request = await readJson(req);
          if (['h2b-console-management', 'h2b-workflow-workbench', 'h2b-gui-studio', 'h2b-subagent-release'].includes(request.method)) assertManagementRequest(req);
          const value = request.method === 'h2b-subagent-release'
            ? await subagentRelease(ctx, request.args)
            : request.method === 'h2b-gui-studio'
            ? await guiStudio(ctx, request.args)
            : request.method === 'h2b-workflow-workbench'
            ? await workflowWorkbench(ctx, request.args)
            : request.method === 'h2b-demo-rpc'
            ? await bridgeRpc(ctx, request.args)
            : request.method === 'h2b-agent-task-rpc'
              ? (AGENT_TASK_OPERATIONS.has(request.args?.operation)
                ? await bridgeRpc(ctx, request.args)
                : (() => { throw new Error('unsupported h2b agent.task operation'); })())
            : request.method === 'h2b-mfu-workflow-rpc'
              ? (new Set(['workflow.capabilities', 'workflow.start', 'workflow.status', 'workflow.result', 'workflow.cancel']).has(request.args?.operation)
                ? await bridgeRpc(ctx, { ...request.args, surface: 'mfu-workflow' })
                : (() => { throw new Error('unsupported MFU workflow operation'); })())
            : request.method === 'h2b-console-management'
              ? await consoleManagement(ctx, request.args)
            : request.method === 'h2b-control-action'
              ? await controlAction(ctx, request.args)
            : request.method === 'h2b-control-query'
              ? await controlQuery(ctx, request.args)
              : request.method === 'h2b-control-capabilities'
                ? await controlCapabilities(ctx)
            : request.method === 'h2b-targets'
              ? await targets(ctx)
              : request.method === 'h2b-capabilities'
                ? capabilities()
                : request.method === 'h2b-kanban-rpc'
                  ? await kanbanRpc(ctx, request.args)
                  : request.method === 'h2b-kanban-status'
                    ? await kanbanStatus()
                  : request.method === 'h2b-kanban-capabilities'
                    ? kanbanCapabilities()
              : (() => { throw new Error('unsupported host method'); })();
          writeJson(res, 200, { ok: true, value });
        } catch (error) {
          writeJson(res, 400, {
            ok: false,
            error: {
              code: diagnostic(error?.code) || 'HOST_RPC_ERROR',
              message: diagnostic(error?.message) || 'request failed',
              ...(error?.details === undefined ? {} : { details: error.details })
            }
          });
        }
      }
  });
}
