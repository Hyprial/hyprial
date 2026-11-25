import { Notice, StatusCard } from '../../../components/primitives';
import type { StatusCardState } from '../../../components/primitives';
import { StepActions } from './StepActions';
import { StepCard } from './StepCard';
import { devicePresentation, networkPresentation } from './presentation';
import type { AttemptState, DeviceActionResult, StepViewState } from './types';

interface DeviceStepProps {
  readonly state: StepViewState;
  readonly deviceAttempt: AttemptState<DeviceActionResult>;
  readonly onInitialize?: () => void;
  readonly onRetry?: () => void;
  readonly onBack: () => void;
  readonly onContinue: () => void;
}

function resultRetryable(
  result: { readonly status: string; readonly retryable?: boolean } | null,
): boolean {
  return result !== null && (result.status !== 'failed' || result.retryable !== false);
}

export function DeviceStep({
  state,
  deviceAttempt,
  onInitialize,
  onRetry,
  onBack,
  onContinue,
}: DeviceStepProps) {
  const deviceResult =
    deviceAttempt.kind === 'settled' ? deviceAttempt.result : null;
  const device = devicePresentation(deviceAttempt, true);
  const network = networkPresentation();
  const busy = deviceAttempt.kind === 'running';
  const ready = deviceResult?.status === 'ready';
  const failedStatuses = new Set(['failed', 'unknown', 'unavailable']);
  const deviceState: StatusCardState = busy
    ? 'running'
    : ready
      ? 'done'
      : deviceResult && failedStatuses.has(deviceResult.status)
        ? 'failed'
        : 'pending';
  const canRetry = resultRetryable(deviceResult);

  return (
    <StepCard id="device" title="设备" state={state}>
      <div className="settings-status-list">
        <StatusCard label="当前设备" state={deviceState} {...device} />
        <StatusCard label="网络" state="pending" {...network} />
      </div>

      {!ready && onInitialize && (deviceAttempt.kind === 'idle' || deviceResult?.status === 'not-ready') && (
        <StepActions
          model={{
            kind: 'single',
            primary: {
              label: '初始化当前设备',
              onClick: onInitialize,
              loading: busy,
            },
          }}
        />
      )}

      {deviceResult?.status === 'failed' && (
        <Notice
          tone="danger"
          role="alert"
          title="设备初始化失败"
          action={canRetry && onRetry ? { label: '重试设备初始化', onClick: onRetry } : undefined}
        >
          {deviceResult.message}
        </Notice>
      )}

      {deviceResult?.status === 'unknown' && (
        <Notice
          tone="warning"
          title="设备状态未知"
          action={canRetry && onRetry ? { label: '重试设备初始化', onClick: onRetry } : undefined}
        >
          {deviceResult.message}
        </Notice>
      )}

      {deviceResult?.status === 'unavailable' && deviceResult.message && (
        <Notice
          tone="warning"
          title={device.value}
          action={canRetry && onRetry ? { label: '重试设备初始化', onClick: onRetry } : undefined}
        >
          {deviceResult.message}
        </Notice>
      )}

      <StepActions
        model={{
          kind: 'pair',
          start: { label: '返回上一步', onClick: onBack },
          end: ready ? { label: '继续到运行配置', onClick: onContinue } : undefined,
        }}
      />
    </StepCard>
  );
}
