import { Button, Notice, StatusCard } from '../../components/primitives';
import { StepCard } from './StepCard';
import type { AttemptState, FirstRunScenario, SquirePreparationResult, SquirePreparationState, StepViewState } from './types';

interface SquireStepProps {
  readonly state: StepViewState;
  readonly scenario: FirstRunScenario;
  readonly attempt: AttemptState<SquirePreparationResult>;
  readonly stages: SquirePreparationState;
  readonly onRetry: () => void;
  readonly onContinue: () => void;
}

export function SquireStep({ state, scenario, attempt, stages, onRetry, onContinue }: SquireStepProps) {
  const result = attempt.kind === 'settled' ? attempt.result : null;
  const started = result?.status === 'started' && stages.created && (stages.granted || stages.grantRecorded) && stages.started;
  const suffix = scenario === 'demo' ? '（隔离预览）' : '';
  return (
    <StepCard id="squire" title="创建与启动 Squire" state={state}>
      <p>使用 Pi 自动完成准备。每一步独立确认；进程启动不代表模型可用或已收到回复。</p>
      <div className="settings-status-list">
        <StatusCard label="创建 Squire" value={stages.created ? `已创建${suffix}` : '未确认'} tone={stages.created ? 'positive' : 'neutral'} />
        <StatusCard label="授权记录" value={stages.granted || stages.grantRecorded ? `已记录授权${suffix} · 待会话验证` : '未确认'} tone={stages.granted || stages.grantRecorded ? 'info' : 'neutral'} />
        <StatusCard label="启动 Pi" value={stages.started ? `已启动${suffix} · 等待真实回复` : '未确认'} tone={stages.started ? 'info' : 'neutral'} />
      </div>
      {attempt.kind === 'running' && <p role="status">正在准备 Squire…</p>}
      {result && !started && <Notice tone="warning" title="Squire 准备未完成" action={{ label: '重试准备', onClick: onRetry }}>
        {result.message ?? '尚未确认全部准备步骤，请重试。'}
      </Notice>}
      {started && <div className="settings-actions"><Button variant="primary" onClick={onContinue}>继续到首次对话</Button></div>}
      <p className="settings-footnote">只有真实对话回复才能确认模型可用。本页不会显示未经验证的在线或就绪状态。</p>
    </StepCard>
  );
}
