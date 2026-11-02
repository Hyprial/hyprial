import { useEffect, useRef, useState } from 'react';
import { Button, EmptyState, ErrorState, LoadingState, Select, UnsupportedState } from '../../components/primitives';
import { ProviderMutationRejected } from './api';
import type { AgentGrant, AgentOption, AgentProviderApi, ProviderLoginFile } from './api';

type Outcomes = Map<string, 'pending' | 'unknown'>;
// Ephemeral duplicate-submission guards, never credential or backend state authority.
const capabilityOutcomes = new WeakMap<AgentProviderApi, Outcomes>();
const scenarioOutcomes = new WeakMap<object, Outcomes>();
const outcomeListeners = new WeakMap<Outcomes, Set<() => void>>();

type View<T> = { state: 'loading' } | { state: 'error'; message: string } | { state: 'ready'; value: T };
const errorMessage = () => '请求失败，请刷新后重试。';

export function AgentSelector({ view, value, onValueChange, disabled = false }: { readonly view: View<readonly AgentOption[]>; readonly value: string; readonly onValueChange: (value: string) => void; readonly disabled?: boolean }) {
  if (view.state === 'loading') return <LoadingState title="正在读取 Agent" />;
  if (view.state === 'error') return <ErrorState message={view.message} />;
  if (!view.value.length) return <EmptyState title="暂无 Agent" />;
  return <div><label htmlFor="provider-agent">Agent</label><Select id="provider-agent" value={value} onValueChange={onValueChange} disabled={disabled} options={[{ value: '', label: '选择 Agent' }, ...view.value.map(item => ({ value: item.actor, label: item.label }))]} /></div>;
}
export function ProviderDefaultView({ view, disabled = false }: { readonly view: View<string | null>; readonly disabled?: boolean }) {
  if (view.state === 'loading') return <LoadingState title="正在读取默认服务" />;
  if (view.state === 'error') return <ErrorState message={view.message} />;
  if (!view.value) return <EmptyState title="未配置默认服务" />;
  return <p aria-disabled={disabled}>默认模型服务：{view.value}</p>;
}
export function AgentGrantList({ view, actor, disabled = false }: { readonly view: View<readonly AgentGrant[]>; readonly actor: string; readonly disabled?: boolean }) {
  if (!actor) return <EmptyState title="请先选择 Agent" />;
  if (view.state === 'loading') return <LoadingState title="正在读取授权" />;
  if (view.state === 'error') return <ErrorState message={view.message} />;
  if (!view.value.length) return <EmptyState title="暂无授权记录" />;
  return <ul aria-label="Agent 密钥授权记录（与原生登录凭据独立）" aria-disabled={disabled}>{view.value.map(grant => <li key={grant.id}>{grant.source} · {grant.field} · {grant.environments.join('、') || '无环境变量'}</li>)}</ul>;
}
export function AgentProviderSection({ api, loginFiles, disabled = false }: { readonly api?: AgentProviderApi; readonly loginFiles?: (actor: string, provider: string) => Promise<readonly ProviderLoginFile[]>; readonly disabled?: boolean }) {
  const [agents, setAgents] = useState<View<readonly AgentOption[]>>({ state: 'loading' });
  const [defaultProvider, setDefaultProvider] = useState<View<string | null>>({ state: 'loading' });
  const [grants, setGrants] = useState<View<readonly AgentGrant[]>>({ state: 'ready', value: [] });
  const [actor, setActor] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const [, refreshOutcomes] = useState(0);
  const generation = useRef(0);
  const realOutcomes = useRef<Outcomes>(api ? capabilityOutcomes.get(api) ?? new Map() : new Map());
  const history = useRef({ entries: realOutcomes.current });
  const targetKey = JSON.stringify([actor, defaultProvider.state === 'ready' ? defaultProvider.value : null]);
  const targetStatus = history.current.entries.get(targetKey);
  const blocked = busy || targetStatus === 'pending' || targetStatus === 'unknown';
  const unknownMessage = '操作结果未知，已暂停此 Agent 与服务的写操作。请联系管理员核实，勿重复提交。';
  useEffect(() => {
    const currentGeneration = ++generation.current;
    // Mock scenarios are isolated; real API replacement never clears unresolved writes.
    if (api?.mockBoundary) {
      let entries = scenarioOutcomes.get(api.mockBoundary);
      if (!entries) { entries = new Map(); scenarioOutcomes.set(api.mockBoundary, entries); }
      history.current = { entries };
    } else {
      if (api) {
        realOutcomes.current = capabilityOutcomes.get(api) ?? realOutcomes.current;
        capabilityOutcomes.set(api, realOutcomes.current);
      }
      history.current = { entries: realOutcomes.current };
    }
    setAgents({ state: 'loading' });
    setDefaultProvider({ state: 'loading' });
    setGrants({ state: 'ready', value: [] });
    setActor('');
    setBusy(false);
    setResult(null);
    if (!api) return;
    let active = true;
    void api.listAgents().then(value => { if (active) setAgents({ state: 'ready', value }); }).catch(() => { if (active) setAgents({ state: 'error', message: errorMessage() }); });
    void api.getDefault().then(value => { if (active) setDefaultProvider({ state: 'ready', value }); }).catch(() => { if (active) setDefaultProvider({ state: 'error', message: errorMessage() }); });
    return () => {
      active = false;
      if (generation.current === currentGeneration) generation.current++;
    };
  }, [api]);
  useEffect(() => {
    const entries = history.current.entries;
    let listeners = outcomeListeners.get(entries);
    if (!listeners) { listeners = new Set(); outcomeListeners.set(entries, listeners); }
    const refresh = () => refreshOutcomes(value => value + 1);
    listeners.add(refresh);
    return () => { listeners.delete(refresh); };
  }, [api]);
  useEffect(() => { if (!api || !actor) { setGrants({ state: 'ready', value: [] }); return; } let active = true; setGrants({ state: 'loading' }); void api.listGrants(actor).then(value => { if (active) setGrants({ state: 'ready', value }); }).catch(() => { if (active) setGrants({ state: 'error', message: errorMessage() }); }); return () => { active = false; }; }, [api, actor]);
  async function mutate(kind: 'login' | 'logout') {
    if (disabled || !api || !actor || defaultProvider.state !== 'ready' || !defaultProvider.value || busy || history.current.entries.has(targetKey)) return;
    if (kind === 'login' && !loginFiles) return;
    const entries = history.current.entries;
    entries.set(targetKey, 'pending');
    let dispatched = false;
    const requestGeneration = generation.current;
    setBusy(true);
    setResult(null);
    try {
      if (kind === 'login') {
        const files = await loginFiles!(actor, defaultProvider.value);
        dispatched = true;
        await api.login(actor, defaultProvider.value, files);
      } else {
        dispatched = true;
        await api.logout(actor, defaultProvider.value);
      }
      entries.delete(targetKey);
      // agent.secret.list reports key grants, not native provider credential files.
      if (generation.current === requestGeneration) setResult(kind === 'login'
        ? '原生凭据写入请求已成功；授权记录不反映此操作。'
        : '原生凭据移除请求已成功；授权记录不反映此操作。');
    } catch (error) {
      const definitive = !dispatched || error instanceof ProviderMutationRejected;
      if (definitive) entries.delete(targetKey);
      else entries.set(targetKey, 'unknown');
      if (generation.current === requestGeneration) setResult(definitive ? '操作失败，请刷新后重试。' : unknownMessage);
    } finally {
      outcomeListeners.get(entries)?.forEach(refresh => refresh());
      if (generation.current === requestGeneration) setBusy(false);
    }
  }
  return <section aria-label="Agent 模型服务" aria-busy={busy}>
    <h3>Agent 模型服务</h3>
    {!api ? <UnsupportedState reason="当前后端尚未提供 Agent provider 请求能力。" /> : <>
      <AgentSelector view={agents} value={actor} onValueChange={value => { generation.current++; setActor(value); setResult(null); }} disabled={disabled || busy} />
      <ProviderDefaultView view={defaultProvider} disabled={disabled} />
      <AgentGrantList view={grants} actor={actor} disabled={disabled} />
      <p>以下密钥授权记录与原生模型服务登录凭据独立。</p>
      <div className="settings-actions"><Button disabled={disabled || blocked || !actor || defaultProvider.state !== 'ready' || !defaultProvider.value || !loginFiles} onClick={() => void mutate('login')}>登录服务</Button><Button disabled={disabled || blocked || !actor || defaultProvider.state !== 'ready' || !defaultProvider.value} onClick={() => void mutate('logout')}>退出服务</Button></div>
      {!loginFiles && <p>登录需要现有授权流程提供凭据文件；当前入口尚未接线。</p>}
      {(targetStatus === 'unknown' || result) && <p role="status">{targetStatus === 'unknown' ? unknownMessage : result}</p>}
    </>}
  </section>;
}
