import { useEffect, useRef } from 'react';
import {
  Button,
  Notice,
  cx,
  type StepStatus,
} from '../../components/primitives';
import brandLockUrl from './assets/hyprial-lock-full.png';
import { AccountStep } from './AccountStep';
import { ConversationStep } from './ConversationStep';
import { ModelStep } from './ModelStep';
import { SquireStep } from './SquireStep';
import { useFirstRunController } from './useFirstRunController';
import './onboarding-v17.css';
import type { FirstRunOnboardingProps, FirstRunStepId, StepViewState } from './types';

const STEP_LABELS: Readonly<Record<FirstRunStepId, string>> = {
  account: '账号',
  model: '模型服务',
  squire: '创建 Squire',
  conversation: '首次对话',
};

const STEP_STATUS_LABELS: Readonly<Record<StepStatus, string>> = {
  pending: '待处理',
  active: '进行中',
  completed: '已完成',
  failed: '失败',
  blocked: '已阻塞',
};

function viewState(
  step: FirstRunStepId,
  activeStep: FirstRunStepId,
  completed: Readonly<Record<FirstRunStepId, boolean>>,
): StepViewState {
  if (completed[step]) {
    return 'completed';
  }
  return activeStep === step ? 'active' : 'locked';
}

export function FirstRunOnboarding(props: FirstRunOnboardingProps) {
  const { chatStatus = 'unavailable', conversationPreview, onOpenChat, onComplete, onExit, className } = props;
  const {
    scenario, defaultUnavailable, actions, activeStep, completed,
    accountProgress, accountAttempt, deviceAttempt, authorizing, canRetryAccount,
    modelSaved, modelRestored, preparation, preparationStages,
    shouldFocusNextStep, stepStatuses, runAccount, cancelAccount, retryAccount,
    runDevice, continueFromAccount, handleModelSavedChange, continueFromModel,
    runPreparation, continueFromSquire, exit,
  } = useFirstRunController(props);
  const rootRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!shouldFocusNextStep.current) {
      return;
    }
    const heading = rootRef.current?.querySelector<HTMLHeadingElement>(
      `[data-step="${activeStep}"] h2`,
    );
    heading?.focus();
  }, [activeStep]);

  const steps = (Object.keys(STEP_LABELS) as FirstRunStepId[]).map(id => ({
    id, label: STEP_LABELS[id], status: stepStatuses[id],
  }));

  function renderActiveStep() {
    switch (activeStep) {
      case 'account':
        return (
          <AccountStep
            state="active"
            scenario={scenario}
            progress={accountProgress}
            authorizing={authorizing}
            accountAttempt={accountAttempt}
            deviceAttempt={deviceAttempt}
            canRetryAccount={canRetryAccount}
            onSubmit={() => void runAccount()}
            onCancelAccount={() => void cancelAccount()}
            onRetryAccount={retryAccount}
            onInitializeDevice={() => void runDevice()}
            onRetryDevice={() => void runDevice()}
            onContinue={continueFromAccount}
          />
        );
      case 'model':
        return (
          <ModelStep
            state="active"
            scenario={scenario}
            saved={modelSaved}
            restored={modelRestored}
            onSave={actions.saveModelKey}
            onSavedChange={handleModelSavedChange}
            onContinue={continueFromModel}
          />
        );
      case 'squire':
        return (
          <SquireStep
            state="active"
            scenario={scenario}
            attempt={preparation}
            stages={preparationStages}
            onRetry={() => void runPreparation()}
            onContinue={continueFromSquire}
          />
        );
      case 'conversation':
        return (
          <ConversationStep
            state="active"
            scenario={scenario}
            chatStatus={chatStatus}
            conversationPreview={conversationPreview}
            onOpenChat={onOpenChat}
            onComplete={onComplete}
          />
        );
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
        <img
          src={brandLockUrl}
          alt="HYPRIAL AI 拟像未来"
          width={760}
          height={146}
        />
      </header>

      {import.meta.env.DEV && scenario === 'demo' && (
        <Notice
          tone="warning"
          title="隔离预览场景"
          className="dev01-v17-notice"
        >
          所有成功状态仅由本地隔离回调返回；不会登录真实账号、访问网络、创建真实
          Squire 或发送聊天消息。
        </Notice>
      )}

      {defaultUnavailable && (
        <Notice
          tone="warning"
          title="当前服务不可用"
          className="dev01-v17-notice"
        >
          首次设置无法完成。请检查本机服务与授权状态；未知结果不会被视为成功。
        </Notice>
      )}

      <nav className="dev01-v17-steps" aria-label="首次设置步骤">
        <ol className="dev01-v17-steps-list">
          {steps.map((step, index) => {
            const current = step.id === activeStep;
            const view = viewState(step.id, activeStep, completed);
            return (
              <li
                key={step.id}
                className={cx(
                  'dev01-v17-step',
                  current && 'dev01-v17-step--current',
                )}
                data-step-nav={step.id}
                data-status={step.status}
                data-view={view}
                aria-current={current ? 'step' : undefined}
              >
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

      <div className="dev01-v17-stage">{renderActiveStep()}</div>

      <aside className="dev01-v17-debug" aria-label="诊断与预览">
        <span className="dev01-v17-debug__label">诊断</span>
        <span className="dev01-v17-debug__scenario">
          {scenario === 'demo'
            ? '隔离预览 · 不访问网络、不写存储'
            : defaultUnavailable
              ? '正式入口 · 无后端预览'
              : '正式入口'}
        </span>
        <p className="dev01-v17-debug__note">
          返回设置后，本次未完成的输入不会保存；重新进入会从账号步骤开始，并重新读取当前实例的登录与设备状态。步骤只按顺序前进。
        </p>
        {onExit && (
          <Button variant="ghost" size="sm" onClick={exit}>
            返回设置
          </Button>
        )}
      </aside>
    </div>
  );
}
