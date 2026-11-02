import { useState, useSyncExternalStore } from 'react';
import type { AuthorizationOperation } from '../../adapters/live';
import { Button } from '../../components/primitives';
import { LarkChannelView, type LarkStep } from '../channels/LarkChannelView';
import type { OperationStatus } from '../../state/operation-lifecycle';
import type { LiveRuntime } from './runtime';
import './live.css';
const AUTH_LABELS: Readonly<Record<AuthorizationOperation['status'], string>> = {
  starting: '正在准备授权…', awaiting_user: '请打开飞书授权页面完成确认。', checking: '正在核对授权权限…',
  succeeded: '已核对所需权限。', denied: '本次授权已拒绝。', expired: '本次授权已过期。',
  cancelled: '本次授权已取消，已有授权仍以查询结果为准。', failed: '本次授权失败。', unknown: '本次授权结果尚未确认，请刷新查询。',
};
function operationStatus(op: AuthorizationOperation): OperationStatus {
  if (op.status === 'succeeded') return 'success';
  if (op.status === 'unknown') return 'unknown';
  if (['denied', 'expired', 'cancelled', 'failed'].includes(op.status)) return 'definitive-failure';
  return 'in-flight';
}
export function LiveLarkPage({ live }: { readonly live: LiveRuntime }) {
  const state = useSyncExternalStore(live.lark.subscribe, live.lark.getSnapshot);
  const chat = useSyncExternalStore(live.chat.subscribe, live.chat.getSnapshot);
  const [step, setStep] = useState<LarkStep>('chooseAgent');
  const row = state.adapters.find((value) => value.adapter === state.selectedAdapter);
  const previousAuth = row && state.authorizationOperations[row.adapter];
  const oldApp = previousAuth?.authorization && previousAuth.authorization.appId !== row?.authorization.appId;
  const previousActive = previousAuth && ['starting', 'awaiting_user', 'checking', 'unknown'].includes(previousAuth.status);
  const authOp = (oldApp && !previousActive) || (previousAuth?.status === 'succeeded' && row?.authorization.authorization !== 'authorized') ? undefined : previousAuth;
  const cleanupRequired = oldApp && previousActive;
  const switchOp = row && state.switchOperations[row.adapter];
  const agentId = state.selectedAgent;
  const ready = live.status === 'ready' && state.status === 'ready';
  const authorization = row?.authorization.authorization;
  const authorized = ready && authorization === 'authorized';
  const activeAuth = authOp && ['starting', 'awaiting_user', 'checking', 'unknown'].includes(authOp.status);
  const connected = authorized && row?.binding.actor === agentId && row?.runtime.online === true;
  return <section className="live-lark-page" aria-label="真实飞书连接">
    <div className="live-status-row">
      <p role="status">{live.error ?? state.error ?? (state.status === 'loading' ? '正在查询飞书状态…' : '授权、绑定和在线状态由本机服务分别核对。')}</p>
      <Button variant="ghost" onClick={() => { void live.lark.poll(); }}>刷新飞书状态</Button>
    </div>
    {state.status === 'ready' && state.adapters.length === 0 && <p role="status">尚未配置飞书 App。请先完成本机飞书配置。</p>}
    {state.adapters.length > 0 && <label className="live-adapter-selector">飞书 App
      <select aria-label="飞书 App" value={state.selectedAdapter ?? ''} onChange={(event) => { live.lark.chooseAdapter(event.currentTarget.value); setStep('chooseAgent'); }}>
        {state.adapters.map((value) => <option key={value.adapter} value={value.adapter}>{value.adapter} · {value.authorization.appId ?? '尚未配置'}</option>)}
      </select>
    </label>}
    {row && <>
      {!row.bindingAvailable && <p role="status">渠道切换暂不可用，需要支持绑定状态校验的服务。</p>}
      {switchOp?.status === 'unknown' && <p role="status">绑定操作结果未知，当前显示上次查询的绑定。请刷新状态，暂不能再次切换。</p>}
      <p>当前绑定：{row.binding.actor ?? '未绑定'} · 运行状态：{ready ? row.runtime.online ? '在线' : '离线' : '未确认'}</p>
      {authOp && <section className="live-authorization" aria-label="飞书授权进度">
        <p role="status">{cleanupRequired ? `旧 App（${authOp.authorization?.appId}）的授权进程尚未确认退出。请先重试清理，再为当前 App 发起授权。` : AUTH_LABELS[authOp.status]}</p>
        {!cleanupRequired && authOp.verificationUrl && (authOp.expiresAt ?? 0) * 1000 > Date.now() && <a href={authOp.verificationUrl} target="_blank" rel="noopener noreferrer">打开飞书授权页面</a>}
        {!cleanupRequired && authOp.expiresAt && <p>链接有效期至：{new Date(authOp.expiresAt * 1000).toLocaleString()}</p>}
        {activeAuth && authOp.operationId && <Button variant="ghost" onClick={() => { void live.lark.cancelAuthorization(); }}>{cleanupRequired ? '重试清理旧 App 授权' : '取消本次授权'}</Button>}
      </section>}
    </>}
      <LarkChannelView agents={chat.targets.map((target) => ({ id: target.id, name: target.displayName }))}
        selectedAgentId={agentId} step={step}
        onAgentChange={(id) => live.lark.chooseAgent(id)} onStepChange={setStep}
        authorization={agentId ? { agentId, status: ready && authorization === 'authorized' ? 'authorized' : ready && authorization === 'unauthorized' ? 'unauthorized' : 'unconfirmed' } : undefined}
        auth={{ capability: ready && !!row && authorization !== 'unconfigured' ? 'ready' : 'unavailable', operation: authOp && agentId ? {
          agentId, requestId: authOp.operationId ?? 'pending', status: operationStatus(authOp), canRetry: !activeAuth, error: AUTH_LABELS[authOp.status],
        } : undefined }}
        switch={{ capability: !row?.bindingAvailable ? 'unavailable' : ready && !(switchOp?.status === 'unknown' && switchOp.agentId !== agentId) ? 'ready' : 'unconfirmed', operation: switchOp && agentId === switchOp.agentId ? {
          agentId, requestId: `binding:${row?.adapter}`, status: switchOp.status, canRetry: switchOp.status === 'definitive-failure', error: switchOp.error,
        } : undefined }}
        channelResult={agentId ? { agentId, status: !ready || !row ? 'unconfirmed' : connected ? 'connected' : row.binding.actor !== agentId || !authorized ? 'disconnected' : 'unavailable',
          detail: ready && authorized && row?.binding.actor === agentId && !row?.runtime.online ? '授权和绑定已确认，飞书连接目前离线。' : undefined } : undefined}
        onAuthorize={() => { void live.lark.authorize(); }} onSwitch={() => live.lark.prepareSwitch()}
        onRetry={(action) => { if (action === 'auth') void live.lark.authorize(); else live.lark.prepareSwitch(); }} />
    {state.confirmation && <section className="live-confirmation" role="alertdialog" aria-modal="false" aria-labelledby="lark-binding-confirm-title">
      <h2 id="lark-binding-confirm-title">确认切换飞书绑定</h2>
      <p>飞书 App：{state.confirmation.adapter}</p>
      <p>当前 Agent：{state.confirmation.expectedActor ?? '未绑定'}</p>
      <p>切换到：{state.confirmation.agentId}</p>
      <p>后续飞书消息将交给所选 Agent。若当前绑定已变更，本次切换将被拒绝。</p>
      <div className="live-status-row"><Button onClick={() => live.lark.dismissConfirmation()}>取消切换</Button>
        <Button variant="primary" onClick={() => { void live.lark.confirmSwitch(); }}>确认切换</Button></div>
    </section>}
  </section>;
}
