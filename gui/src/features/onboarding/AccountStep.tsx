import {
  Button,
  Notice,
  StatusCard,
} from '../../components/primitives';
import { StepCard } from './StepCard';
import { AccountProgressPanel } from './AccountProgressPanel';
import {
  accountPresentation,
  devicePresentation,
  networkPresentation,
} from './presentation';
import type {
  AccountActionResult,
  AccountProgress,
  AttemptState,
  DeviceActionResult,
  FirstRunScenario,
  StepViewState,
} from './types';

export { safeAuthorizationUrl } from './authorization-url';

function resultRetryable(
  result: { readonly status: string; readonly retryable?: boolean } | null,
): boolean {
  return result !== null && (result.status !== 'failed' || result.retryable !== false);
}

interface AccountStepProps {
  readonly state: StepViewState;
  readonly scenario: FirstRunScenario;
  readonly progress: AccountProgress | null;
  readonly authorizing: boolean;
  readonly accountAttempt: AttemptState<AccountActionResult>;
  readonly deviceAttempt: AttemptState<DeviceActionResult>;
  readonly canRetryAccount: boolean;
  readonly onSubmit: () => void;
  readonly onCancelAccount: () => void;
  readonly onRetryAccount: () => void;
  readonly onInitializeDevice: () => void;
  readonly onRetryDevice: () => void;
  readonly onContinue: () => void;
}

export function AccountStep({
  state,
  scenario,
  progress,
  authorizing,
  accountAttempt,
  deviceAttempt,
  canRetryAccount,
  onSubmit,
  onCancelAccount,
  onRetryAccount,
  onInitializeDevice,
  onRetryDevice,
  onContinue,
}: AccountStepProps) {
  const account = accountPresentation(accountAttempt, scenario);
  const accountResult =
    accountAttempt.kind === 'settled' ? accountAttempt.result : null;
  const deviceResult =
    deviceAttempt.kind === 'settled' ? deviceAttempt.result : null;
  const authenticated = accountResult?.status === 'authenticated';
  const device = devicePresentation(deviceAttempt, authenticated, scenario);
  const network = networkPresentation();
  const deviceReady = deviceResult?.status === 'ready';
  const accountBusy = accountAttempt.kind === 'running';
  const deviceBusy = deviceAttempt.kind === 'running';
  const canRetryAccountResult =
    canRetryAccount && resultRetryable(accountResult);
  const canRetryDeviceResult = resultRetryable(deviceResult);

  return (
    <StepCard id="account" title="账号与设备" state={state}>
      {state === 'completed' ? (
        <>
          <div className="settings-status-list">
            <StatusCard label="账号" {...account} />
            <StatusCard label="当前设备" {...device} />
            <StatusCard label="网络" {...network} />
          </div>
          <p className="settings-footnote">
            账号与当前设备已在本次首次设置中确认；后续步骤已解锁。
          </p>
        </>
      ) : state === 'locked' ? (
        <p>完成上一步后解锁。</p>
      ) : (
        <>
          <p>先确认本应用账号，再初始化当前设备。两项都确认后才能继续。</p>
          <div className="settings-status-list">
            <StatusCard label="账号" {...account} />
            <StatusCard label="当前设备" {...device} />
            <StatusCard label="网络" {...network} />
          </div>

          <p className="settings-footnote">
            在官方授权页完成登录；无需在这里填写密码或访问令牌。账号注册由授权页提供。
          </p>
          {!authenticated && (
            <div className="settings-actions">
              <Button variant="primary" disabled={accountBusy} onClick={onSubmit}>
                登录 Hyprial
              </Button>
            </div>
          )}
          <AccountProgressPanel
            progress={progress}
            active={accountBusy}
            authorizing={authorizing}
            onCancel={onCancelAccount}
            onRetry={onRetryAccount}
            canRetryWarnings={authenticated}
          />
          {authenticated && accountResult.owner && <p>当前账号：{accountResult.owner}</p>}

          {accountResult?.status === 'failed' && (
            <Notice
              tone="danger"
              role="alert"
              title="登录失败"
              action={
                canRetryAccountResult
                  ? { label: '重试登录', onClick: onRetryAccount }
                  : undefined
              }
            >
              {accountResult.message}
            </Notice>
          )}

          {accountResult?.status === 'unknown' && (
            <Notice
              tone="warning"
              title="登录结果未知"
              action={
                canRetryAccountResult
                  ? { label: '重试登录', onClick: onRetryAccount }
                  : undefined
              }
            >
              {accountResult.message}
            </Notice>
          )}

          {accountResult?.status === 'unavailable' && (
            <Notice
              tone="warning"
              title="账号服务不可用"
              action={
                canRetryAccountResult
                  ? { label: '重试登录', onClick: onRetryAccount }
                  : undefined
              }
            >
              {accountResult.message}
            </Notice>
          )}

          {authenticated && !deviceReady && (
            <>
              {(deviceAttempt.kind === 'idle' || deviceResult?.status === 'not-ready') && (
                <div className="settings-actions">
                  <Button
                    variant="primary"
                    loading={deviceBusy}
                    onClick={onInitializeDevice}
                  >
                    初始化当前设备
                  </Button>
                </div>
              )}
              {deviceResult?.status === 'failed' && (
                <Notice
                  tone="danger"
                  role="alert"
                  title="设备初始化失败"
                  action={
                    canRetryDeviceResult
                      ? { label: '重试设备初始化', onClick: onRetryDevice }
                      : undefined
                  }
                >
                  {deviceResult.message}
                </Notice>
              )}
              {deviceResult?.status === 'unknown' && (
                <Notice
                  tone="warning"
                  title="设备状态未知"
                  action={
                    canRetryDeviceResult
                      ? { label: '重试设备初始化', onClick: onRetryDevice }
                      : undefined
                  }
                >
                  {deviceResult.message}
                </Notice>
              )}
              {deviceResult?.status === 'unavailable' && (
                <Notice
                  tone="warning"
                  title="设备初始化不可用"
                  action={
                    canRetryDeviceResult
                      ? { label: '重试设备初始化', onClick: onRetryDevice }
                      : undefined
                  }
                >
                  {deviceResult.message}
                </Notice>
              )}
            </>
          )}

          {authenticated && deviceReady && (
            <div className="settings-actions">
              <Button variant="primary" onClick={onContinue}>
                继续到模型服务
              </Button>
            </div>
          )}
        </>
      )}
    </StepCard>
  );
}
