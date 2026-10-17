// Optional native-owned read-only discovery. It never changes chat connection
// state, starts a daemon or manages an Agent. Native rechecks caller and ownership.
const STATES = Object.freeze({ unavailable: '本机服务不可用', starting: '本机服务启动中', ready: '本机服务已连接', stopping: '本机服务停止中', stopped: '本机服务已停止', failed: '本机服务已退出', unknown: '本机服务状态未知' });
function validPort(port) {
  return port && typeof port === 'object' && Object.keys(port).every(key => ['capabilities', 'status'].includes(key))
    && typeof port.status === 'function' && port.capabilities?.available === true
    && Object.keys(port.capabilities).every(key => ['available', 'operations'].includes(key))
    && Array.isArray(port.capabilities.operations) && port.capabilities.operations.length === 1
    && port.capabilities.operations[0] === 'status';
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
  section.replaceChildren(heading, status, agents, refresh); root.appendChild(section);
  let disposed = false, pending = false;
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
      else agents.replaceChildren(...value.agents.rows.map(row => element('li', 'local-agent', `${row.actor} · ${row.status}`)));
      if (value.agents.truncated === true) status.textContent += '；仅显示前 200 个 Agent';
    } catch {
      if (!disposed) { status.textContent = '暂时无法读取本机状态，请重试'; agents.replaceChildren(); }
    } finally { pending = false; if (!disposed) refresh.disabled = false; }
  }
  if (!validPort(port)) { status.textContent = '本机状态接口不可用'; refresh.disabled = true; }
  else { refresh.addEventListener('click', update); void update(); }
  return { destroy() { disposed = true; } };
}
