/** Starting consumes saved configuration; directory approval remains explicit. */
import { useEffect, useRef, useState } from 'react';
import { Dialog } from '../../../../components/primitives';
import type { AgentsApi, AgentStartOptions } from '../api';
import type { RuntimeActionOutcome } from '../api/contract-gateway';
import { modelReady, type ModelStatus } from './api';
export function ModelStartDialog({ actor, status, api, cwd, onStart, onClose, onOutcome }: {
  actor: string; status: ModelStatus; api: AgentsApi; cwd: string;
  onStart: (options: AgentStartOptions) => Promise<RuntimeActionOutcome>;
  onClose: () => void; onOutcome: (result: RuntimeActionOutcome) => void;
}) {
  const [directory, setDirectory] = useState(cwd);
  const [approved, setApproved] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);
  const epoch = useRef(0);
  useEffect(() => { epoch.current += 1; return () => { epoch.current += 1; }; }, [actor, api]);
  const config = status.binding!.configuration;
  const pi = config.harness === 'pi';
  async function start(): Promise<void> {
    if (busy.current || (pi && (!approved || !directory.trim()))) return;
    busy.current = true; setPending(true); setError(null);
    const attempt = epoch.current;
    try {
      const current = await api.modelStatus!(actor);
      if (attempt !== epoch.current) return;
      if (!modelReady(current) || current.agentId !== status.agentId || current.member !== status.member
        || current.binding?.bindingRef !== status.binding?.bindingRef || current.binding?.bindingRevision !== status.binding?.bindingRevision
        || Object.entries(status.binding!.configuration).some(([key, value]) => (current.binding!.configuration as unknown as Record<string, unknown>)[key] !== value)) {
        setError('模型配置已变化或尚未完成，请关闭并刷新核对。'); return;
      }
      const result = await onStart({ ...(directory.trim() ? { cwd: directory.trim() } : {}), ...(pi ? { approve: approved } : {}) });
      if (attempt !== epoch.current) return;
      onOutcome(result); onClose();
    } catch { if (attempt === epoch.current) setError('启动前无法核对模型配置；未重复启动，请刷新状态。'); }
    finally { if (attempt === epoch.current) { busy.current = false; setPending(false); } }
  }
  return <Dialog open title="启动 Agent" onClose={() => { if (!pending) onClose(); }} actions={[
    { label: '取消', variant: 'secondary', disabled: pending, onClick: onClose },
    { label: '启动', variant: 'primary', loading: pending, disabled: pending || (pi && (!approved || !directory.trim())), onClick: () => void start() },
  ]}>
    <div className="agent-config-form">
      <p>{config.harness} · {config.authKind === 'connection' ? '连接 / Key' : '原生 Auth'} · {config.model}</p>
      <p>使用已保存配置。更改模型或凭据请返回“配置模型”。启动成功不代表模型响应已验证。</p>
      <label htmlFor="bound-start-cwd">工作目录</label>
      <input id="bound-start-cwd" className="ui-native-control" value={directory} disabled={pending} onChange={event => { setDirectory(event.target.value); setApproved(false); }} />
      {pi && <label><input type="checkbox" checked={approved} disabled={pending} onChange={event => setApproved(event.target.checked)} />信任此工作目录并允许自动恢复</label>}
      {error && <p role="alert">{error}</p>}
    </div>
  </Dialog>;
}
