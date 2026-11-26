/*
 * Decision record (first-run P2, 2026-11-18):
 * - The generic runtime step reuses the shared Agent form; the legacy DeepSeek
 *   key form remains only for hosts without configureAgent.
 * - The write ticket and pending/unknown lifecycle live in the controller, so an
 *   in-flight write still blocks a re-open and a result from an older actions
 *   generation is discarded there. A late same-generation result never
 *   advances the step; nothing retries automatically.
 * - S2d (2026-11-22): first-run AgentForm starts with empty displayName and
 *   role, matching ordinary Agent creation so an untouched submit never
 *   creates a fixed squire.
 */
import { unknownModelKeySaveResult } from './model-save-result';
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { useNotifyUnavailable } from '../../../components/notifications';
import { Button, Notice, StatusCard } from '../../../components/primitives';
import { SecretField } from '../../../components/forms';
import { AgentForm, ModelRecovery, keyCompatibleWithHarness, type AgentDraft, type AgentsApi } from '../../agents';
import type { ProviderLoginFile } from '../../agent-provider';
import { CredentialsSection, type SharedCredentialsApi } from '../../connections';
import { StepActions } from './StepActions';
import { StepCard } from './StepCard';
import type {
  ModelKeySaveResult, ModelKeyStatusCheck, RuntimeConfigurationResult, StepViewState,
} from './types';

const FIRST_RUN_RUNTIME = Object.freeze({
  harness: 'pi',
  provider: 'deepseek',
  model: 'deepseek-flash',
});
const RUNTIME_COMPATIBLE = keyCompatibleWithHarness(
  FIRST_RUN_RUNTIME.harness,
  FIRST_RUN_RUNTIME.provider,
);

interface ModelStepProps {
  readonly modelApi?: AgentsApi;
  readonly state: StepViewState;
  readonly saved: boolean;
  readonly restored?: boolean;
  readonly savePending?: boolean;
  /** Note appended to the saved-key status value; injected by isolated previews. */
  readonly savedKeyNote?: string;
  readonly draft: string;
  readonly onDraftChange: (value: string) => void;
  readonly onBack: () => void;
  readonly onSave?: (key: string) => Promise<ModelKeySaveResult>;
  readonly onCheckStatus?: () => Promise<ModelKeyStatusCheck>;
  readonly onSavedChange: (saved: boolean, restored?: boolean) => void;
  readonly onSaveUnknown?: () => void;
  readonly onContinue: () => void;
  readonly onConfigureAgent?: (draft: AgentDraft) => Promise<RuntimeConfigurationResult>;
  readonly runtimeCredentials?: SharedCredentialsApi;
  readonly pickRuntimeLoginFiles?: (provider: string) => Promise<readonly ProviderLoginFile[]>;
  readonly onRuntimeConfigured?: (result: RuntimeConfigurationResult) => void;
  /** An unknown runtime write stays locked across back/reopen until readback clears it. */
  readonly runtimeWriteLocked?: boolean;
  readonly runtimeWritePending?: boolean;
  readonly runtimeUnknown?: RuntimeConfigurationResult | null;
  readonly onBeginRuntimeWrite?: () => number | null;
  readonly onRuntimeWriteUnknown?: (attempt: number, result: RuntimeConfigurationResult) => void;
  readonly onFinishRuntimeWrite?: (attempt: number) => void;
}

/** Connected first-run reuses the shared Agent form; legacy hosts keep the DeepSeek key form. */
export function ModelStep({ modelApi, state, saved, restored = false, savePending = false, savedKeyNote, draft, onDraftChange, onBack, onSave, onCheckStatus, onSavedChange, onSaveUnknown, onContinue, onConfigureAgent, runtimeCredentials, pickRuntimeLoginFiles, onRuntimeConfigured, runtimeWriteLocked = false, runtimeWritePending = false, runtimeUnknown = null, onBeginRuntimeWrite, onRuntimeWriteUnknown, onFinishRuntimeWrite }: ModelStepProps) {
  const notifyUnavailable = useNotifyUnavailable();
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [result, setResult] = useState<ModelKeySaveResult | null>(null);
  const [statusCheck, setStatusCheck] = useState<ModelKeyStatusCheck | null>(null);
  const [configuring, setConfiguring] = useState(false);
  const [credentialsRevision, setCredentialsRevision] = useState(0);
  const [submittedDraft, setSubmittedDraft] = useState<AgentDraft | null>(null);
  const [configResult, setConfigResult] = useState<RuntimeConfigurationResult | null>(null);
  const configuringRef = useRef(false);
  const configAttempt = useRef(0);
  const configUnknown = useRef(false);
  const attempt = useRef(0);
  const saving = useRef(false);
  const checkingRef = useRef(false);
  const unconfirmed = useRef(false);
  const previousRecord = useRef({ saved: false, restored: false });
  const saveUnknown = unconfirmed.current || savePending;
  useEffect(() => () => {
    attempt.current += 1;
    configAttempt.current += 1;
  }, []);

  function replaceKey(): void {
    previousRecord.current = { saved, restored };
    unconfirmed.current = false;
    setEditing(true); onDraftChange(''); setResult(null); setStatusCheck(null);
    onSavedChange(false);
  }
  function cancelEdit(): void {
    if (saving.current || checkingRef.current) return;
    attempt.current += 1;
    onDraftChange(''); setStatusCheck(null);
    if (saveUnknown) {
      setResult({ status: 'unknown', message: '替换结果未确认，旧 Key 不会恢复为已确认。请先检查状态。' });
      onSavedChange(false);
      return;
    }
    setResult(null); setEditing(false);
    onSavedChange(previousRecord.current.saved, previousRecord.current.restored);
  }
  async function checkStatus(): Promise<void> {
    if (!onCheckStatus || saving.current || checkingRef.current) return;
    const id = attempt.current;
    checkingRef.current = true;
    setChecking(true); setStatusCheck(null);
    try {
      const next = await onCheckStatus();
      if (id === attempt.current) setStatusCheck(next);
    } catch {
      if (id === attempt.current) {
        setStatusCheck({ status: 'unavailable', message: '无法读取 Key 保存状态，请检查本机服务。' });
      }
    } finally {
      checkingRef.current = false;
      if (id === attempt.current) setChecking(false);
    }
  }
  async function save(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (saving.current || checkingRef.current || !draft.trim()) return;
    const id = ++attempt.current;
    saving.current = true;
    setBusy(true); setResult(null); setStatusCheck(null);
    onSavedChange(false);
    let remaining = '';
    try {
      if (!onSave) return;
      const pending = onSave(draft.trim());
      onDraftChange('');
      const next: ModelKeySaveResult = await pending;
      if (id !== attempt.current) return;
      if (next.status === 'unavailable' && next.refusal) {
        remaining = draft.trim();
        notifyUnavailable('保存并继续', next.refusal);
        return;
      }
      if (next.status === 'unknown') onSaveUnknown?.();
      unconfirmed.current = unconfirmed.current || next.status === 'unknown';
      setResult(next);
      if (next.status === 'saved') {
        onSavedChange(true);
        onContinue();
      }
    } catch {
      if (id === attempt.current) {
        onSaveUnknown?.();
        unconfirmed.current = true;
        setResult(unknownModelKeySaveResult());
      }
    } finally {
      if (id === attempt.current) { onDraftChange(remaining); setBusy(false); saving.current = false; }
    }
  }

  async function configure(draftValue: AgentDraft): Promise<void> {
    if (!onConfigureAgent || configuringRef.current || configUnknown.current || runtimeWriteLocked) return;
    const writeAttempt = onBeginRuntimeWrite ? onBeginRuntimeWrite() : 1;
    if (writeAttempt === null) return;
    const id = ++configAttempt.current;
    configuringRef.current = true;
    setConfiguring(true);
    setConfigResult(null); setSubmittedDraft(draftValue);
    let next: RuntimeConfigurationResult;
    try {
      next = await onConfigureAgent(draftValue);
    } catch {
      next = {
        status: 'unknown',
        message: '运行配置结果未知，请核对 Agent 列表后重试；不会自动重复创建。',
      };
    }
    if (next.status === 'unavailable' && next.refusal) {
      notifyUnavailable('创建 Agent', next.refusal);
    }
    if (next.status === 'unknown' || next.status === 'incomplete') {
      configUnknown.current = true;
      onRuntimeWriteUnknown?.(writeAttempt, next);
    } else {
      onFinishRuntimeWrite?.(writeAttempt);
    }
    const current = id === configAttempt.current;
    configuringRef.current = false;
    setConfiguring(false);
    if (!current) return;
    setConfigResult(next);
    if (next.status === 'configured') onRuntimeConfigured?.(next);
  }

  const runtimeLocked = runtimeWriteLocked || configUnknown.current;
  const runtimeMessage = configResult ?? runtimeUnknown ?? (runtimeWritePending
    ? { status: 'unknown' as const, message: '上次运行配置仍在处理中；请等待结果，不要重复创建。' }
    : null);
  const statusAction = onCheckStatus ? {
    label: checking ? '正在检查…' : '检查状态',
    onClick: () => void checkStatus(),
    disabled: busy || checking,
  } : undefined;

  return (
    <StepCard id="model" title={onConfigureAgent ? '运行配置' : '模型服务'} state={state}>
      {state === 'active' && <>
        {!onConfigureAgent && <StatusCard
          label="运行配置"
          value={RUNTIME_COMPATIBLE
            ? 'Pi · DeepSeek'
            : '运行配置需要核对'}
          tone={RUNTIME_COMPATIBLE ? 'neutral' : 'warning'}
          hint={RUNTIME_COMPATIBLE
            ? '模型 deepseek-flash（默认）；由保存的 DeepSeek Key 决定，当前仅提供这一组合。'
            : '当前 Key 与 harness 组合未通过兼容校验，请核对本机配置。'}
        />}
        {onConfigureAgent ? (<>
          {runtimeCredentials && <CredentialsSection api={runtimeCredentials} creationOnly
            onKeysChanged={() => setCredentialsRevision(value => value + 1)} />}
          {runtimeMessage?.created && runtimeMessage.actorUri && modelApi ? <ModelRecovery
            actor={runtimeMessage.actorUri} api={modelApi} credentials={runtimeCredentials}
            nativeLoginFiles={pickRuntimeLoginFiles ? (_actor, harness) => pickRuntimeLoginFiles(harness) : undefined}
            draft={submittedDraft} onConfigured={() => onRuntimeConfigured?.({ status: 'configured', actorUri: runtimeMessage.actorUri, created: true })} /> : <AgentForm
            mode="create"
            explicitModel={modelApi?.configureModel !== undefined}
            initial={{
              displayName: '',
              role: '',
              modelServiceId: '',
              organizationId: null,
              preferredHarness: 'pi',
            }}
            credentialsApi={runtimeCredentials}
            credentialsRevision={credentialsRevision}
            pickLoginFiles={pickRuntimeLoginFiles}
            autoFocusName={false}
            pending={configuring}
            submitDisabled={runtimeLocked}
            submitDisabledReason="上次运行配置结果未确认；请先核对 Agent 状态，不要重复创建。"
            message={runtimeMessage && runtimeMessage.status !== 'configured'
              ? {
                  tone: 'error',
                  text: runtimeMessage.message ?? '运行配置未完成。',
                }
              : null}
            onSubmit={(next) => void configure(next)}
            onCancel={onBack}
          />}
        </>
        ) : saved && !editing ? <>
          <StatusCard label="DeepSeek 凭证" value={`已保存 DeepSeek Key${savedKeyNote ?? ''}`} tone="positive"
            hint={restored ? '已找到本机保存记录。' : '尚未确认模型可用。'} />
          <div className="dev01-model-secondary-actions">
            {onSave && <Button variant="text" onClick={replaceKey}>更换 Key</Button>}
          </div>
          <StepActions
            model={{
              kind: 'pair',
              start: { label: '返回上一步', onClick: onBack },
              end: { label: '继续', onClick: onContinue },
            }}
          />
        </> : !onSave ? (
          <StepActions model={{ kind: 'pair', start: { label: '返回上一步', onClick: onBack } }} />
        ) : <form className="dev01-model-key-form" onSubmit={(event) => void save(event)}>
          <SecretField id="dev01-deepseek-key" label="DeepSeek API Key" value={draft}
            layout="inline" disabled={busy} required showLabel="显示 Key" hideLabel="隐藏 Key"
            onChange={(value) => { onDraftChange(value); setResult(null); setStatusCheck(null); onSavedChange(false); }}
            hint="Key 仅保存在本机。" />
          {editing && saveUnknown && <p className="settings-footnote">替换结果未确认，取消不会恢复旧 Key。</p>}
          {result && result.status !== 'saved' && (
            <Notice
              tone="warning"
              title={result.status === 'unknown' ? 'Key 保存结果未知' : 'Key 保存未确认'}
              action={result.status === 'unknown' ? statusAction : undefined}
            >
              {result.message ?? (result.status === 'unknown'
                ? unknownModelKeySaveResult().message
                : '请重新输入 Key 并重试。')}
            </Notice>
          )}
          {saveUnknown && result?.status !== 'unknown' && (
            <Notice
              tone="warning"
              title="此前保存结果未知"
              action={statusAction}
            >
              上次保存可能已生效。请先检查状态，确认前不要重复保存。
            </Notice>
          )}
          {statusCheck && (
            <Notice
              tone={statusCheck.status === 'checked' && statusCheck.keySaved === false ? 'warning' : 'info'}
              title="只读状态检查"
            >
              {statusCheck.message}
            </Notice>
          )}
          {editing && previousRecord.current.saved && (
            <div className="dev01-model-secondary-actions">
              <Button variant="text" disabled={busy || checking} onClick={cancelEdit}>取消</Button>
            </div>
          )}
          <StepActions
            model={{
              kind: 'pair',
              start: { label: '返回上一步', onClick: onBack, disabled: busy || checking },
              end: {
                label: busy ? '正在保存…' : '保存并继续',
                type: 'submit',
                disabled: busy || checking || !draft.trim(),
                loading: busy,
              },
            }}
          />
        </form>}
      </>}
    </StepCard>
  );
}
