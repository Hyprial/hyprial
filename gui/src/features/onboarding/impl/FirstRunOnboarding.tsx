/**
 * Decision record (WP4 data-driven pages, 2026-11-09):
- The former build-mode `DEV && scenario === 'demo'` (scenario value now
  `isolated`) notice, diagnostic
  label and saved-key copy gates are gone. Isolated previews inject
  `FirstRunSourceMetadata`; product compositions omit it and get only the
  formal-entry text. Identical inputs render identical DOM in both modes, and
  the fixture copy never enters the production bundle because only non-bundled
  preview compositions supply those strings.
- `scenario` stays an inert `data-scenario` label for the AppShell contract;
  it no longer selects copy, notices or actions.
 */
import { useEffect, useRef } from 'react';
import brandMark from '../../../assets/brand/hyprial-mark.svg';
import {
  Button,
  Icon,
  InfoTip,
  Notice,
  cx,
  type StepStatus,
} from '../../../components/primitives';
import { AccountStep } from './AccountStep';
import { ConversationStep } from './ConversationStep';
import { DeviceStep } from './DeviceStep';
import { ModelStep } from './ModelStep';
import { useFirstRunController } from './useFirstRunController';
import './onboarding-v17.css';
import type { FirstRunOnboardingProps, FirstRunStepId, StepViewState } from './types';

const NAV_STEPS: ReadonlyArray<{
  readonly id: string;
  readonly label: string;
  readonly steps: readonly FirstRunStepId[];
}> = [
  { id: 'account', label: '登录', steps: ['account'] },
  { id: 'device', label: '设备', steps: ['device'] },
  { id: 'runtime', label: '运行配置', steps: ['model'] },
  { id: 'conversation', label: '对话', steps: ['conversation'] },
];

const STEP_STATUS_LABELS: Readonly<Record<StepStatus, string>> = {
  pending: '待处理',
  active: '进行中',
  completed: '已完成',
  failed: '失败',
  blocked: '已阻塞',
};

function viewState(
  steps: readonly FirstRunStepId[],
  activeStep: FirstRunStepId,
  completed: Readonly<Record<FirstRunStepId, boolean>>,
): StepViewState {
  if (steps.every((step) => completed[step])) {
    return 'completed';
  }
  return steps.includes(activeStep) ? 'active' : 'locked';
}

export function FirstRunOnboarding(props: FirstRunOnboardingProps) {
  const { chatStatus = 'unavailable', conversation, onOpenChat, onOpenReplyChat, onComplete, onExit, className, source } = props;
  const {
    scenario, actions, activeStep, completed, firstRun, runtimeStale, runtimeWritePending, runtimeWriteUnknown,
    accountProgress, accountAttempt, deviceAttempt, authorizing, canRetryAccount,
    modelSaved, modelRestored, modelSavePending, modelDraft,
    shouldFocusNextStep, stepStatuses, runAccount, cancelAccount, retryAccount,
    runDevice, continueFromAccount, continueFromDevice, goBackFromDevice,
    handleModelSavedChange, handleModelSaveUnknown,
    checkModelKeyStatus, setModelDraft, continueFromModel, goBackFromModel,
    exit,
    continueFromRuntimeConfiguration,
    beginRuntimeWrite, markRuntimeWriteUnknown, finishRuntimeWrite,
    skipFirstRun, skipPending, skipNotice,
    replyAttempt, replyAgentUri, replyMessages, replyBusy, replyNotice,
    runReplyConfirmation, refreshReplyMessages, confirmReply, cancelReplyConfirmation,
  } = useFirstRunController(props);
  const rootRef = useRef<HTMLDivElement>(null);
  const conversationCompletedRef = useRef(false);
  const replyCompletedRef = useRef(false);
  const conversationStepReached = activeStep === 'conversation'
    && completed.account && completed.device && completed.model;
  const receiptNeedsRecovery = props.resumeFromStatus && completed.account && completed.device
    && firstRun !== null && firstRun.state !== 'none' && firstRun.state !== 'recorded';
  useEffect(() => {
    // The P3 reply loop owns completion whenever it is composed; a legacy
    // chat acceptance is not evidence of a durable reply.
    if (actions.replyConfirmation) return;
    if (!conversation?.sendAccepted || conversationCompletedRef.current) return;
    if (!conversationStepReached) return;
    conversationCompletedRef.current = true;
    onComplete?.();
  }, [actions.replyConfirmation, conversation?.sendAccepted, onComplete, conversationStepReached]);
  useEffect(() => {
    if (replyAttempt?.state !== 'confirmed' || replyCompletedRef.current) return;
    replyCompletedRef.current = true;
    onComplete?.();
  }, [onComplete, replyAttempt?.state]);
  useEffect(() => {
    if (!shouldFocusNextStep.current) {
      return;
    }
    const heading = rootRef.current?.querySelector<HTMLHeadingElement>(
      `[data-step="${activeStep}"] h2`,
    );
    heading?.focus();
  }, [activeStep]);

  const steps = NAV_STEPS.map((group) => {
    const statuses = group.steps.map((id) => stepStatuses[id]);
    const status: StepStatus = statuses.every((value) => value === 'completed')
      ? 'completed'
      : statuses.includes('failed')
        ? 'failed'
        : statuses.includes('blocked')
          ? 'blocked'
          : group.steps.includes(activeStep)
            ? 'active'
            : 'pending';
    return {
      id: group.id,
      label: group.label,
      status,
      view: viewState(group.steps, activeStep, completed),
      current: group.steps.includes(activeStep),
    };
  });

  function renderActiveStep() {
    switch (activeStep) {
      case 'account':
        return (
          <AccountStep
            state="active"
            progress={accountProgress}
            authorizing={authorizing}
            accountAttempt={accountAttempt}
            canRetryAccount={canRetryAccount}
            onSubmit={actions.authenticateAccount ? () => void runAccount() : undefined}
            onCancelAccount={() => void cancelAccount()}
            onRetryAccount={retryAccount}
            onOpenAuthorization={actions.openAuthorization}
            onContinue={continueFromAccount}
          />
        );
      case 'device':
        return (
          <DeviceStep
            state="active"
            deviceAttempt={deviceAttempt}
            onInitialize={actions.initializeDevice ? () => void runDevice() : undefined}
            onRetry={actions.initializeDevice ? () => void runDevice() : undefined}
            onBack={goBackFromDevice}
            onContinue={continueFromDevice}
          />
        );
      case 'model':
        return (
          <ModelStep
            state="active"
            saved={modelSaved}
            restored={modelRestored}
            savePending={modelSavePending}
            savedKeyNote={source?.savedKeyNote}
            draft={modelDraft}
            onDraftChange={setModelDraft}
            onBack={goBackFromModel}
            onSave={actions.configureAgent ? undefined : actions.saveModelKey}
            onCheckStatus={actions.configureAgent ? undefined : (actions.getSetupState ? checkModelKeyStatus : undefined)}
            onSavedChange={handleModelSavedChange}
            onSaveUnknown={handleModelSaveUnknown}
            onContinue={continueFromModel}
            onConfigureAgent={actions.configureAgent}
            runtimeCredentials={actions.runtimeCredentials}
            pickRuntimeLoginFiles={actions.pickRuntimeLoginFiles}
            onRuntimeConfigured={continueFromRuntimeConfiguration}
            runtimeWriteLocked={runtimeWritePending || runtimeWriteUnknown !== null}
            runtimeWritePending={runtimeWritePending}
            runtimeUnknown={runtimeWriteUnknown}
            onBeginRuntimeWrite={beginRuntimeWrite}
            onRuntimeWriteUnknown={markRuntimeWriteUnknown}
            onFinishRuntimeWrite={finishRuntimeWrite}
          />
        );
      case 'conversation': {
        const p3Reply = actions.replyConfirmation;
        const chatTarget = p3Reply ? replyAttempt?.agentUri ?? null : null;
        return (
          <ConversationStep
            state="active"
            chatStatus={chatStatus}
            conversation={conversation}
            reply={p3Reply ? {
              attempt: replyAttempt,
              agentUri: replyAgentUri,
              busy: replyBusy,
              notice: replyNotice,
              messages: replyMessages,
              onStart: () => void runReplyConfirmation(),
              onRefresh: refreshReplyMessages,
              onConfirm: () => void confirmReply(),
              onCancel: () => void cancelReplyConfirmation(),
            } : undefined}
            onOpenChat={p3Reply
              ? (onOpenReplyChat && chatTarget ? () => onOpenReplyChat(chatTarget) : undefined)
              : onOpenChat}
            onComplete={onComplete}
          />
        );
      }
    }
  }

  return (
    <div
      ref={rootRef}
      className={cx('dev01-v17-onboarding', className)}
      data-scenario={scenario}
      data-active-step={activeStep}
    >
      <h1 className="ui-visually-hidden">首次设置</h1>

      <header className="dev01-v17-brand">
        <img className="dev01-v17-brand-mark" src={brandMark} alt="Hyprial" />
      </header>

      {source?.notice && (
        <Notice
          tone="warning"
          title={source.notice.title}
          className="dev01-v17-notice"
        >
          {source.notice.body}
        </Notice>
      )}

      {firstRun && (!props.resumeFromStatus || completed.account && completed.device)
        && firstRun.state !== 'none' && firstRun.state !== 'recorded' && (
        <Notice
          tone="warning"
          title="首次设置记录需要核对"
          className="dev01-v17-notice"
        >
          {firstRun.state === 'corrupt'
            ? '检测到损坏的首次设置记录，原件已保留；不会自动重写或跳过步骤。'
            : firstRun.state === 'unsupported-version'
              ? '首次设置记录版本不受支持；原件已保留，请升级或核对本机状态。'
              : '无法读取首次设置记录；不会当作新用户或已完成。'}
        </Notice>
      )}

      {runtimeStale && (
        <Notice
          tone="warning"
          title="历史运行配置待核对"
          className="dev01-v17-notice"
        >
          本机当前读回的 Key、Agent 或启动状态与历史回执不一致；不会当作已完成，请重新核对或配置。
        </Notice>
      )}

      {skipNotice && (
        <Notice
          tone="warning"
          title="跳过首次设置未完成"
          className="dev01-v17-notice"
        >
          {skipNotice}
        </Notice>
      )}

      <nav className="dev01-v17-steps" aria-label="首次设置步骤">
        <ol className="dev01-v17-steps-list">
          {steps.map((step, index) => {
            const current = step.current;
            return (
              <li
                key={step.id}
                className={cx(
                  'dev01-v17-step',
                  current && 'dev01-v17-step--current',
                )}
                data-step-nav={step.id}
                data-status={step.status}
                data-view={step.view}
                aria-current={current ? 'step' : undefined}
              >
                {step.status === 'completed' && <Icon name="check" size="sm" />}
                <span className="dev01-v17-step__label">
                  {index + 1} · {step.label}
                </span>
                <span className="ui-visually-hidden">
                  （{STEP_STATUS_LABELS[step.status]}）
                </span>
              </li>
            );
          })}
        </ol>
      </nav>

      {!receiptNeedsRecovery && <div className="dev01-v17-stage">{renderActiveStep()}</div>}

      <aside className="dev01-v17-debug" aria-label="诊断与预览">
        {actions.skipFirstRun && (
          <>
            <Button
              variant="ghost"
              size="sm"
              disabled={skipPending}
              onClick={() => void skipFirstRun()}
              data-testid="first-run-skip"
            >
              {skipPending ? '正在跳过…' : '跳过首次设置'}
            </Button>
            <InfoTip
              label="跳过首次设置的说明"
              content="跳过后不会执行四步设置；以后可在设置中继续配置模型和真实回复。"
            />
          </>
        )}
        {source?.label && (
          <span className="dev01-v17-debug__scenario">
            <span className="dev01-v17-debug__label">预览</span> {source.label}
          </span>
        )}
        {onExit && (
          <>
            <Button
              variant="ghost"
              size="sm"
              leadingIcon={<Icon name="arrow-left" size="sm" />}
              onClick={exit}
            >
              返回设置
            </Button>
            <InfoTip
              label="返回设置的说明"
              content="返回设置不会保存本次输入，下次从账号步骤重新开始。"
            />
          </>
        )}
      </aside>
    </div>
  );
}
