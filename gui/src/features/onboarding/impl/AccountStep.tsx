import { Notice, StatusCard } from '../../../components/primitives';
import { StepActions } from './StepActions';
import { StepCard } from './StepCard';
import { AccountProgressPanel } from './AccountProgressPanel';
import type { AuthOpenResult } from '../../../platform/auth';
import { accountPresentation } from './presentation';
import type { StatusCardState } from '../../../components/primitives';
import type {
  AccountActionResult,
  AccountProgress,
  AttemptState,
  StepViewState,
} from './types';

function resultRetryable(
  result: { readonly status: string; readonly retryable?: boolean } | null,
): boolean {
  return result !== null && (result.status !== 'failed' || result.retryable !== false);
}

interface AccountStepProps {
  readonly state: StepViewState;
  readonly progress: AccountProgress | null;
  readonly authorizing: boolean;
  readonly accountAttempt: AttemptState<AccountActionResult>;
  readonly canRetryAccount: boolean;
  readonly onSubmit?: () => void;
  readonly onCancelAccount: () => void;
  readonly onRetryAccount: () => void;
  readonly onOpenAuthorization?: () => Promise<AuthOpenResult>;
  readonly onContinue: () => void;
}

export function AccountStep({
  state,
  progress,
  authorizing,
  accountAttempt,
  canRetryAccount,
  onSubmit,
  onCancelAccount,
  onRetryAccount,
  onOpenAuthorization,
  onContinue,
}: AccountStepProps) {
  const account = accountPresentation(accountAttempt);
  const accountResult =
    accountAttempt.kind === 'settled' ? accountAttempt.result : null;
  const authenticated = accountResult?.status === 'authenticated';
  const accountBusy = accountAttempt.kind === 'running';
  const canRetryAccountResult =
    canRetryAccount && resultRetryable(accountResult);
  const failedStatuses = new Set(['failed', 'unknown', 'unavailable']);
  const accountState: StatusCardState = accountBusy
    ? 'running'
    : accountResult?.status === 'authenticated'
      ? 'done'
      : accountResult && failedStatuses.has(accountResult.status)
        ? 'failed'
        : 'pending';

  return (
    <StepCard id="account" title="登录" state={state}>
      {!accountBusy && (
        <div className="settings-status-list">
          <StatusCard label="账号" state={accountState} {...account} />
        </div>
      )}

      {!authenticated && !accountBusy && onSubmit && (
        <StepActions
          model={{
            kind: 'single',
            primary: { label: '登录 Hyprial', onClick: onSubmit, disabled: accountBusy },
          }}
        />
      )}
      <AccountProgressPanel
        progress={progress}
        active={accountBusy}
        authorizing={authorizing}
        onCancel={onCancelAccount}
        onRetry={onRetryAccount}
        onOpenAuthorization={onOpenAuthorization}
        canRetryWarnings={authenticated}
      />
      {authenticated && (accountResult.username ?? accountResult.owner) && (
        <p className="settings-footnote">当前账号：{accountResult.username ?? accountResult.owner}</p>
      )}

      {accountResult?.status === 'failed' && (
        <Notice
          tone="danger"
          role="alert"
          title="登录失败"
          action={canRetryAccountResult ? { label: '重试登录', onClick: onRetryAccount } : undefined}
        >
          {accountResult.message}
        </Notice>
      )}

      {accountResult?.status === 'unknown' && (
        <Notice
          tone="warning"
          title="登录结果未知"
          action={canRetryAccountResult ? { label: '重试登录', onClick: onRetryAccount } : undefined}
        >
          {accountResult.message}
        </Notice>
      )}

      {accountResult?.status === 'unavailable' && accountResult.message && (
        <Notice
          tone="warning"
          title="账号服务不可用"
          action={canRetryAccountResult ? { label: '重试登录', onClick: onRetryAccount } : undefined}
        >
          {accountResult.message}
        </Notice>
      )}

      {authenticated && (
        <StepActions
          model={{
            kind: 'single',
            primary: { label: '继续到设备', onClick: onContinue },
          }}
        />
      )}
    </StepCard>
  );
}
