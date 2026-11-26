/** Status is daemon-owned. Unknown writes lock the editor until explicit readback. */
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Dialog } from '../../../../components/primitives';
import { useNotifyUnavailable } from '../../../../components/notifications';
import { methodRefusal, type MethodRefusal } from '../../../../client';
import type { SharedCredentialsApi } from '../../../connections';
import type { ProviderLoginFile } from '../../../agent-provider';
import type { AgentsApi } from '../api';
import { Fields, choiceConfiguration, choiceError, type ModelChoice } from './Fields';
import { modelError, modelReady, type ModelStatus, type NativeRecord } from './api';

export function ModelPanel({ actor, api, credentials, nativeLoginFiles, onStatus, checkAccess, initialHarness = '', initialModel = '' }: {
  actor: string; api: AgentsApi; credentials?: SharedCredentialsApi;
  nativeLoginFiles?: (actor: string, harness: string) => Promise<readonly ProviderLoginFile[]>;
  onStatus: (status: ModelStatus | null) => void; initialHarness?: string; initialModel?: string;
  checkAccess?: (method: string) => MethodRefusal | null;
}) {
  const [state, setState] = useState<ModelStatus | null>(null);
  const [readError, setReadError] = useState(false);
  const [loading, setLoading] = useState(true);
  const [locked, setLocked] = useState(false);
  const [busy, setBusy] = useState(false);
  const working = useRef(false);
  const epoch = useRef(0);
  const [message, setMessage] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [targets, setTargets] = useState<readonly NativeRecord[]>([]);
  const [targetsFailed, setTargetsFailed] = useState(false);
  const [choice, setChoice] = useState<ModelChoice>({ harness: initialHarness, authKind: 'connection', model: initialModel });
  const notify = useNotifyUnavailable();
  const notifyRef = useRef(notify); notifyRef.current = notify;
  const statusRef = useRef(onStatus); statusRef.current = onStatus;
  const accept = useCallback((value: ModelStatus) => { setState(value); statusRef.current(value); }, []);
  const reload = useCallback(async () => {
    const current = ++epoch.current;
    setLoading(true); setReadError(false); statusRef.current(null);
    try {
      if (!api.modelStatus) throw new Error('no reader');
      const value = await api.modelStatus(actor);
      if (current !== epoch.current) return;
      accept(value); setLocked(false);
    } catch (error) {
      if (current !== epoch.current) return;
      setReadError(true); setLocked(true);
      const refusal = methodRefusal(error); if (refusal) notifyRef.current('读取模型配置', refusal);
    } finally { if (current === epoch.current) setLoading(false); }
  }, [actor, api, accept]);
  useEffect(() => {
    setState(null); setEditing(false); setMessage(null); setBusy(false); working.current = false;
    void reload();
    return () => { epoch.current += 1; };
  }, [reload]);
  const pending = state?.operation?.phase === 'prepared';
  const blocked = loading || readError || locked || busy || state?.cleanupPending === true;
  function refused(method: string, label: string): boolean {
    const refusal = checkAccess?.(method);
    if (!refusal) return false;
    notify(label, refusal); return true;
  }
  async function readTargets(current = epoch.current): Promise<void> {
    setTargets([]); setTargetsFailed(false);
    if (!api.nativeTargets) { setTargetsFailed(true); return; }
    try {
      const result = await api.nativeTargets(actor);
      if (current === epoch.current) setTargets(result);
    } catch { if (current === epoch.current) setTargetsFailed(true); }
  }
  async function open(): Promise<void> {
    if (blocked || pending || refused('agent.model.configure', '配置模型')) return;
    const config = state?.binding?.configuration;
    setChoice({ harness: config?.harness ?? initialHarness, model: config?.model ?? initialModel,
      authKind: config?.authKind ?? 'connection', ...(config?.authKind === 'native' ? { nativeAuthRef: config.nativeAuthRef } : {}) });
    setEditing(true); setMessage(null);
    const current = epoch.current;
    if (config?.authKind === 'connection' && credentials) {
      try {
        const matches = (await credentials.listKeys()).filter(row => row.state === 'active' && row.connection?.connectionRef === config.connectionRef && row.connection.configRevision === config.configRevision);
        if (current === epoch.current && matches.length === 1) setChoice(value => ({ ...value, connection: matches[0].connection }));
      } catch { if (current === epoch.current) setMessage('连接清单读取失败，请刷新核对。'); }
    }
    if (current === epoch.current) await readTargets(current);
  }
  async function importNative(): Promise<void> {
    if (working.current || !nativeLoginFiles || !api.loginNative || !choice.harness || refused('agent.provider.login', '导入登录文件')) return;
    working.current = true; setBusy(true); setMessage(null);
    const current = epoch.current;
    try {
      const files = await nativeLoginFiles(actor, choice.harness);
      if (files.length === 0 || current !== epoch.current) return;
      await api.loginNative(actor, choice.harness, files, { replace: true });
      if (current !== epoch.current) return;
      setChoice(value => ({ ...value, nativeAuthRef: undefined }));
      await readTargets();
    } catch (error) {
      if (current !== epoch.current) return;
      const refusal = methodRefusal(error);
      if (refusal) notify('导入登录文件', refusal);
      else { setMessage('登录文件导入未确认；请刷新核对后再配置。'); setLocked(true); statusRef.current(null); }
    } finally { if (current === epoch.current) { working.current = false; setBusy(false); } }
  }
  async function save(): Promise<void> {
    if (working.current || blocked || pending || !state || !api.configureModel) return;
    const invalid = choiceError(choice);
    if (invalid) { setMessage(invalid); return; }
    if (choice.authKind === 'native' && (targetsFailed || targets.filter(row => row.state === 'stored' && row.harness === choice.harness).flatMap(row => row.targets ?? []).filter(target => target.nativeAuthRef === choice.nativeAuthRef).length !== 1)) { setMessage('登录目标尚未核对，请重新读取。'); return; }
    if (state.cell.revision === null) { setMessage('Cell 修订尚未确认，请刷新。'); return; }
    working.current = true; setBusy(true); setMessage(null);
    const current = epoch.current;
    const id = crypto.randomUUID();
    try {
      if (choice.authKind === 'connection') {
        if (!api.providerStatus || !api.allowProvider || !credentials) throw new Error('no grant');
        const entries = await credentials.listKeys();
        if (current !== epoch.current) return;
        const selected = entries.filter(row => row.state === 'active' && row.connection?.connectionRef === choice.connection?.connectionRef);
        if (selected.length !== 1 || selected[0].connection?.configRevision !== choice.connection?.configRevision) throw new Error('stale connection');
        const grants = await api.providerStatus(actor);
        if (current !== epoch.current) return;
        const existing = grants.find(row => row.grantId === 'model-connection');
        if (existing?.keyId !== selected[0].keyId || !['ok', 'granted'].includes(existing?.state ?? '')) await api.allowProvider(actor, 'openai', {
          keyId: selected[0].keyId, grantId: 'model-connection', revision: (existing?.revision ?? 0) + 1,
        });
      }
      if (current !== epoch.current) return;
      const result = await api.configureModel({ name: actor, configuration: choiceConfiguration(choice), operationId: id,
        expectedBindingRevision: state.binding?.bindingRevision ?? 0, expectedCellRevision: state.cell.revision });
      if (current !== epoch.current) return;
      if (result.agentId !== state.agentId || result.member !== state.member || result.operation?.operationId !== id
        || result.operation.expectedBindingRevision !== (state.binding?.bindingRevision ?? 0)
        || result.operation.expectedCellRevision !== state.cell.revision
        || (result.operation.phase === 'finalized' && (!result.binding || Object.entries(choiceConfiguration(choice)).some(([key, value]) => (result.binding!.configuration as unknown as Record<string, unknown>)[key] !== value)))) throw new Error('receipt mismatch');
      accept(result); setEditing(false);
      setLocked(true);
      setMessage(modelReady(result) ? '模型配置已保存；尚未启动或验证模型响应。' : '配置尚未完成，请刷新核对。');
      working.current = false; setBusy(false);
      await reload();
    } catch (error) {
      if (current !== epoch.current) return;
      const refusal = methodRefusal(error);
      if (refusal) notify('保存模型配置', refusal);
      else setMessage(modelError(error));
      setLocked(true); statusRef.current(null);
    } finally { if (current === epoch.current) { working.current = false; setBusy(false); } }
  }
  async function cancel(): Promise<void> {
    const operation = state?.operation;
    if (working.current || blocked || !state || operation?.phase !== 'prepared' || !api.cancelModel || refused('agent.model.cancel', '取消配置')) return;
    working.current = true; setBusy(true); setMessage(null);
    const current = epoch.current;
    try {
      const result = await api.cancelModel({ name: actor, operationId: operation.operationId, expectedBindingRevision: operation.expectedBindingRevision });
      if (current !== epoch.current) return;
      if (result.agentId !== state.agentId || result.member !== state.member || result.operation?.operationId !== operation.operationId
        || result.operation.expectedBindingRevision !== operation.expectedBindingRevision
        || result.operation.phase !== 'cancelled') throw new Error('receipt mismatch');
      accept(result); setLocked(result.operation?.phase !== 'cancelled');
      setMessage(result.operation?.phase === 'cancelled' ? '配置操作已取消；保留原绑定。' : '取消尚未确认，请刷新核对。');
    } catch (error) { if (current === epoch.current) { setLocked(true); statusRef.current(null); setMessage(modelError(error)); } }
    finally { if (current === epoch.current) { working.current = false; setBusy(false); } }
  }
  return <section className="agent-manage-section" aria-label="模型配置">
    <h3>模型配置</h3>
    {loading ? <p role="status">读取模型配置…</p> : readError ? <p role="alert">模型配置读取失败；启动与配置已暂停。</p> : state?.binding ? <p>
      {state.binding.configuration.harness} · {state.binding.configuration.authKind === 'connection' ? '连接 / Key' : '原生 Auth'} · {state.binding.configuration.model} · 后台运行
    </p> : <p>尚无显式模型配置。请配置后再启动；旧 Agent 可核对旧配置。</p>}
    {state?.cleanupPending && <p role="alert">Agent 清理尚未完成，不能配置或启动。</p>}
    {pending && <p role="status">配置操作尚未完成：{state?.operation?.operationId}。刷新核对，或显式取消；关闭页面不会取消。</p>}
    {message && <p className="agent-manage-note" role="status">{message}</p>}
    <div className="agent-form-actions">
      <Button variant="secondary" disabled={blocked || pending || !api.configureModel} onClick={() => void open()}>配置模型</Button>
      <Button variant="text" disabled={busy || loading} onClick={() => void reload()}>刷新模型配置</Button>
      {pending && api.cancelModel && <Button variant="secondary" disabled={blocked} onClick={() => void cancel()}>取消未完成配置</Button>}
    </div>
    <Dialog open={editing} title="配置模型" onClose={() => { if (!busy) setEditing(false); }} actions={[
      { label: '关闭', variant: 'secondary', disabled: busy, onClick: () => setEditing(false) },
      { label: '保存配置', variant: 'primary', disabled: blocked || pending, onClick: () => void save() },
    ]}><div className="agent-config-form">
      <Fields value={choice} onChange={setChoice} credentials={credentials} targets={targets} disabled={busy || locked} />
      {choice.authKind === 'native' && <>
        {targetsFailed && <p role="alert">登录目标尚未核对，不能保存。</p>}
        {nativeLoginFiles && api.loginNative && <Button variant="secondary" disabled={busy || locked || !choice.harness} onClick={() => void importNative()}>导入或替换登录文件</Button>}
      </>}
      <p className="agent-field-hint">保存连接配置会授权此连接给 Agent。授权与配置分步执行；失败后请核对，不会启动或自动回退。</p>
      {message && <p role="alert">{message}</p>}
    </div></Dialog>
  </section>;
}
