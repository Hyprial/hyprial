import { useId } from 'react';
import { Button, Stepper } from '../../components/primitives';
import type { StepStatus } from '../../components/primitives/Stepper';
import './channels.css';
import type { OperationStatus } from '../../state/operation-lifecycle';

export type LarkStep = 'chooseAgent' | 'auth' | 'switch' | 'result';
export type LarkAction = 'auth' | 'switch';
export type LarkCapability = 'ready' | 'unavailable' | 'unconfirmed';
export interface LarkOperation {
  readonly agentId: string;
  readonly requestId: string;
  readonly status: OperationStatus;
  readonly canRetry: boolean;
  readonly error?: string;
}
export interface LarkActionState {
  readonly capability: LarkCapability;
  readonly operation?: LarkOperation;
}
export interface LarkAuthorization {
  readonly agentId: string;
  readonly status: 'unconfirmed' | 'unauthorized' | 'authorized' | 'expired' | 'revoked';
}
export interface LarkChannelResult {
  readonly agentId: string;
  readonly status: 'unconfirmed' | 'connected' | 'disconnected' | 'unavailable';
  readonly detail?: string;
}
export interface LarkChannelViewProps {
  readonly agents: ReadonlyArray<{ readonly id: string; readonly name: string }>;
  readonly selectedAgentId: string | null;
  readonly step: LarkStep;
  readonly stepStatuses?: Readonly<Partial<Record<'chooseAgent' | 'result', OperationStatus>>>;
  readonly auth?: LarkActionState;
  readonly switch?: LarkActionState;
  readonly authorization?: LarkAuthorization;
  readonly channelResult?: LarkChannelResult;
  readonly onAgentChange: (agentId: string) => void;
  readonly onStepChange: (step: LarkStep) => void;
  readonly onAuthorize: (agentId: string) => void;
  readonly onSwitch: (agentId: string) => void;
  readonly onRetry: (action: LarkAction, requestId: string) => void;
}

const STEPS: ReadonlyArray<{ id: LarkStep; label: string }> = [
  { id: 'chooseAgent', label: '选择 Agent' },
  { id: 'auth', label: '授权飞书' },
  { id: 'switch', label: '切换渠道' },
  { id: 'result', label: '连接结果' },
];

const OPERATION_LABELS: Readonly<Record<OperationStatus, string>> = {
  'not-sent': '尚未操作',
  'in-flight': '正在处理，请等待结果。',
  success: '操作已完成',
  'definitive-failure': '操作失败',
  unknown: '结果未知，请等待确认，暂不能重新操作。',
};
const AUTHORIZATION_LABELS: Readonly<Record<LarkAuthorization['status'], string>> = {
  unconfirmed: '授权状态尚未确认',
  unauthorized: '尚未授权',
  authorized: '已授权',
  expired: '授权已过期，请重新授权',
  revoked: '授权已撤销，请重新授权',
};

function progress(status: OperationStatus, current: boolean): StepStatus {
  switch (status) {
    case 'success': return 'completed';
    case 'definitive-failure': return 'failed';
    case 'unknown': return 'blocked';
    case 'in-flight': return 'active';
    default: return current ? 'active' : 'pending';
  }
}

/**
 * Props-only presentation; the controller owns selection, authorization, results,
 * and all operations. After accepting an intent it must synchronously project
 * in-flight before another intent can be accepted. These types are UI evidence,
 * not a backend request or authorization contract.
 */
export function LarkChannelView(props: LarkChannelViewProps) {
  const selectId = useId();
  const panelId = useId();
  const selectedAgent = props.agents.find((agent) => agent.id === props.selectedAgentId);
  const agentId = selectedAgent?.id;
  const authorization = agentId !== undefined && props.authorization?.agentId === agentId
    ? props.authorization : undefined;
  const authorized = authorization?.status === 'authorized';
  const channel = agentId !== undefined && props.channelResult?.agentId === agentId
    ? props.channelResult : undefined;
  const currentOperation = (action: LarkAction) => {
    const operation = props[action]?.operation;
    return agentId !== undefined && operation?.agentId === agentId ? operation : undefined;
  };
  const action = props.step === 'auth' || props.step === 'switch' ? props.step : undefined;
  const actionState = action ? props[action] : undefined;
  const operation = action ? currentOperation(action) : undefined;
  const capability = actionState?.capability ?? 'unavailable';
  const actionReady = agentId !== undefined && capability === 'ready' &&
    (action !== 'switch' || authorized);
  const canStart = actionReady && (operation === undefined || operation.status === 'not-sent');
  const canRetry = actionReady && operation?.status === 'definitive-failure' &&
    operation.canRetry === true && operation.requestId.length > 0;
  const currentIndex = STEPS.findIndex((item) => item.id === props.step);
  const operationStatus = operation?.status ?? 'not-sent';
  const panelTitle = STEPS[currentIndex].label;

  let connectionLabel = '连接状态尚未确认';
  if (authorization?.status === 'unauthorized' || authorization?.status === 'expired' ||
      authorization?.status === 'revoked') {
    connectionLabel = '尚未连接';
  } else if (authorized && channel?.status === 'connected') {
    connectionLabel = '飞书已连接';
  } else if (channel?.status === 'disconnected') {
    connectionLabel = '尚未连接';
  } else if (channel?.status === 'unavailable') {
    connectionLabel = '连接结果暂不可用';
  }

  return (
    <section className="lark-channel-view" aria-label="飞书连接">
      <header className="lark-channel-view__header">
        <p className="lark-channel-view__eyebrow">消息连接 · 飞书</p>
        <h1>连接飞书</h1>
        <p>选择 Agent，确认授权后切换消息渠道。</p>
      </header>
      <Stepper
        ariaLabel="飞书连接步骤"
        currentId={props.step}
        steps={STEPS.map(({ id, label }) => {
          if (id === 'auth' || id === 'switch') {
            const status = currentOperation(id)?.status ?? 'not-sent';
            const unavailable = props[id]?.capability !== 'ready';
            return { id, label, status: unavailable && status === 'not-sent'
              ? 'blocked' : progress(status, id === props.step) };
          }
          return { id, label, status: progress(props.stepStatuses?.[id] ?? 'not-sent', id === props.step) };
        })}
      />
      <section className="lark-channel-view__panel" aria-labelledby={panelId}>
        <h2 id={panelId}>{panelTitle}</h2>
        {props.agents.length === 0 && <p role="status">暂无可选 Agent</p>}
        {props.step === 'chooseAgent' ? (
          <>
            <label htmlFor={selectId}>选择 Agent</label>
            <select
              id={selectId}
              className="lark-channel-view__select"
              value={agentId ?? ''}
              disabled={props.agents.length === 0}
              onChange={(event) => {
                const id = event.currentTarget.value;
                if (props.agents.some((agent) => agent.id === id)) props.onAgentChange(id);
              }}
            >
              <option value="" disabled>请选择 Agent</option>
              {props.agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
            </select>
            <p className="lark-channel-view__hint" role="status" aria-label="选择步骤状态">
              {OPERATION_LABELS[props.stepStatuses?.chooseAgent ?? 'not-sent']}
            </p>
          </>
        ) : (
          <p className="lark-channel-view__agent">当前 Agent：{selectedAgent?.name ?? '请选择 Agent'}</p>
        )}
        {action && (
          <>
            <p className="lark-channel-view__authorization" aria-label="飞书授权状态">
              {AUTHORIZATION_LABELS[authorization?.status ?? 'unconfirmed']}
            </p>
            <div
              className="lark-channel-view__status"
              role={operationStatus === 'definitive-failure' ? 'alert' : 'status'}
              aria-label={action === 'auth' ? '授权操作状态' : '切换操作状态'}
              aria-live={operationStatus === 'definitive-failure' ? 'assertive' : 'polite'}
            >
              <p>{OPERATION_LABELS[operationStatus]}</p>
              {capability !== 'ready' && <p>{capability === 'unavailable'
                ? '此操作暂不可用。' : '此操作能力尚未确认。'}</p>}
              {operationStatus === 'definitive-failure' && operation?.error && <p>{operation.error}</p>}
              {action === 'switch' && !authorized && <p>请先确认当前 Agent 已授权。</p>}
            </div>
            <div className="lark-channel-view__actions">
              <Button
                variant="primary"
                disabled={!canStart}
                loading={operationStatus === 'in-flight'}
                onClick={() => {
                  if (!canStart || agentId === undefined) return;
                  if (action === 'auth') props.onAuthorize(agentId);
                  else props.onSwitch(agentId);
                }}
              >{action === 'auth' ? '授权飞书' : '切换到飞书'}</Button>
              {canRetry && operation && <Button onClick={() => {
                if (canRetry) props.onRetry(action, operation.requestId);
              }}>{action === 'auth' ? '重试授权' : '重试切换'}</Button>}
            </div>
          </>
        )}
        {props.step === 'result' && (
          <>
            <div className="lark-channel-view__result" role="status" aria-label="飞书连接结果">
              <strong>{connectionLabel}</strong>
              {channel?.detail && <p>{channel.detail}</p>}
            </div>
            <p className="lark-channel-view__hint" role="status" aria-label="结果步骤状态">
              {OPERATION_LABELS[props.stepStatuses?.result ?? 'not-sent']}
            </p>
          </>
        )}
        <footer className="lark-channel-view__navigation">
          {currentIndex > 0 && <Button variant="ghost" onClick={() => {
            props.onStepChange(STEPS[currentIndex - 1].id);
          }}>上一步</Button>}
          {props.step === 'chooseAgent' && <Button disabled={!selectedAgent} onClick={() => {
            if (selectedAgent) props.onStepChange('auth');
          }}>下一步：授权</Button>}
          {props.step === 'auth' && <Button disabled={!authorized} onClick={() => {
            if (authorized) props.onStepChange('switch');
          }}>下一步：切换渠道</Button>}
          {props.step === 'switch' && <Button onClick={() => props.onStepChange('result')}>查看连接结果</Button>}
        </footer>
      </section>
    </section>
  );
}
