// Optional native-owned local management. Reading is always available; create
// and start exist only when the native side declares them, go through the same
// bounded port, and never change chat connection state. This module starts no
// daemon, talks to no socket and invents no outcome: every action reports the
// shell's own answer, refusals by their code. Native rechecks caller and ownership.
const STATES = Object.freeze({ unavailable: '本机服务不可用', starting: '本机服务启动中', ready: '本机服务已连接', stopping: '本机服务停止中', stopped: '本机服务已停止', failed: '本机服务已退出', unknown: '本机服务状态未知' });
const HARNESSES = Object.freeze(['claude', 'codex', 'pi', 'dsh']);
const READ_ONLY = Object.freeze(['status']);
const MANAGED = Object.freeze(['status', 'create', 'start']);
const sameOperations = (declared, expected) => declared.length === expected.length && expected.every((operation, index) => declared[index] === operation);
function validPort(port) {
  if (!(port && typeof port === 'object')) return false;
  if (!Object.keys(port).every(key => ['capabilities', 'status', 'create', 'start'].includes(key))) return false;
  if (typeof port.status !== 'function' || port.capabilities?.available !== true) return false;
  if (!Object.keys(port.capabilities).every(key => ['available', 'operations'].includes(key))) return false;
  const operations = port.capabilities.operations;
  if (!Array.isArray(operations)) return false;
  // The declared operations must match the members exactly, in order: a port
  // that carries an undeclared mutation is refused without ever calling it.
  const managed = sameOperations(operations, MANAGED);
  if (!managed && !sameOperations(operations, READ_ONLY)) return false;
  return managed ? typeof port.create === 'function' && typeof port.start === 'function'
    : port.create === undefined && port.start === undefined;
}
function validSnapshot(value) {
  return value && Object.hasOwn(STATES, value.runtime?.state)
    && ['available', 'unavailable'].includes(value.agents?.state) && Array.isArray(value.agents.rows)
    && value.agents.rows.length <= 200
    && (value.agents.state !== 'available' || value.runtime.state === 'ready')
    && value.agents.rows.every(row => typeof row?.actor === 'string' && row.actor.length <= 256 && typeof row.status === 'string' && row.status.length <= 64);
}
export function mountDiscovery({ document, root, port }) {
  if (port === undefined || port === null || port.capabilities?.available === false) return { destroy() {} };
  const element = (tag, cls, text = '') => { const node = document.createElement(tag); node.className = cls; if (text) node.textContent = text; return node; };
  const section = element('section', 'local-discovery');
  const heading = element('h2', 'local-heading', '本机 Agent');
  const status = element('p', 'local-status'); status.setAttribute('role', 'status');
  const agents = element('ul', 'local-agents');
  const refresh = element('button', 'local-refresh', '刷新本机状态'); refresh.setAttribute('type', 'button');
  const allowed = validPort(port);
  const managed = allowed && sameOperations(port.capabilities.operations, MANAGED);
  const actionable = element('p', 'local-action-status'); actionable.setAttribute('role', 'status');
  const nameInput = element('input', 'local-create-name'); nameInput.setAttribute('type', 'text'); nameInput.setAttribute('maxlength', '128'); nameInput.setAttribute('placeholder', '新 Agent 名称');
  const create = element('button', 'local-create', '创建'); create.setAttribute('type', 'button');
  const createRow = element('div', 'local-create-row'); createRow.replaceChildren(nameInput, create);
  section.replaceChildren(heading, status, ...(managed ? [createRow, actionable] : []), agents, refresh);
  root.appendChild(section);
  let disposed = false, pending = false, acting = false;
  // The shell's answer is the only outcome that gets reported: success wording
  // comes from the action itself, a refusal keeps its own code and message.
  const bounded = (value, key) => value && typeof value === 'object' && typeof value[key] === 'string' ? value[key].slice(0, 400) : '';
  function refusal(label, value) {
    if (disposed) return;
    const code = bounded(value, 'code'), message = bounded(value, 'message');
    actionable.textContent = [`${label}失败`, code, message].filter(part => part).join('：');
  }
  async function act(label, run, done) {
    if (disposed || acting) return;
    acting = true; create.disabled = true;
    actionable.textContent = `${label}中…`;
    try {
      const value = await run();
      if (disposed) return;
      if (value && typeof value === 'object' && value.ok === true) { actionable.textContent = done(); void update(); }
      else refusal(label, value);
    } catch (error) {
      refusal(label, error instanceof Error ? error : { message: String(error) });
    } finally { acting = false; if (!disposed) create.disabled = false; }
  }
  function rowElement(row) {
    const item = element('li', 'local-agent');
    const who = element('span', 'local-agent-label', `${row.actor} · ${row.status}`);
    if (!managed) { item.appendChild(who); return item; }
    const harness = element('select', 'local-harness');
    for (const kind of HARNESSES) { const option = document.createElement('option'); option.value = kind; option.textContent = kind; harness.appendChild(option); }
    const start = element('button', 'local-start', '启动'); start.setAttribute('type', 'button');
    start.addEventListener('click', () => act('启动', () => { const kind = harness.value || HARNESSES[0]; return port.start(row.actor, kind); }, () => `已请求启动 ${row.actor} · ${harness.value || HARNESSES[0]}`));
    item.replaceChildren(who, harness, start);
    return item;
  }
  async function update() {
    if (disposed || pending) return;
    pending = true; refresh.disabled = true;
    status.textContent = '正在读取本机状态'; agents.replaceChildren();
    try {
      const value = await port.status();
      if (disposed) return;
      if (!validSnapshot(value)) throw new Error('Invalid discovery snapshot');
      status.textContent = STATES[value.runtime.state];
      if (value.agents.state === 'unavailable') status.textContent += '；Agent 列表暂不可用';
      else if (!value.agents.rows.length) status.textContent += '；未发现 Agent';
      else agents.replaceChildren(...value.agents.rows.map(rowElement));
      if (value.agents.truncated === true) status.textContent += '；仅显示前 200 个 Agent';
    } catch {
      if (!disposed) { status.textContent = '暂时无法读取本机状态，请重试'; agents.replaceChildren(); }
    } finally { pending = false; if (!disposed) refresh.disabled = false; }
  }
  if (!allowed) { status.textContent = '本机状态接口不可用'; refresh.disabled = true; }
  else {
    if (managed) create.addEventListener('click', () => {
      const name = nameInput.value.trim();
      if (!name) { actionable.textContent = '请输入名称'; return; }
      return act('创建', () => port.create(name), () => `已创建 ${name}`);
    });
    refresh.addEventListener('click', update);
    void update();
  }
  return { destroy() { disposed = true; } };
}
