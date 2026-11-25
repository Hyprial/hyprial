import { useEffect, useState } from 'react';
import { Button, Notice } from '../../../components/primitives';
import type { AuthOpenResult } from '../../../platform/auth';
import { safeAuthorizationUrl } from './authorization-url';
import type { AccountProgress } from './types';

export const LONG_WAIT_SECONDS = 30;
export const WAIT_TIMEOUT_SECONDS = 90;

export interface AccountProgressCopy {
  readonly title: string;
  readonly detail: string;
}

/**
 * Copy is derived only from public runtime events. No branch claims that a
 * credential was saved or that login succeeded before the final host state.
 */
export function accountProgressCopy(
  progress: AccountProgress | null,
  authorizing: boolean,
): AccountProgressCopy {
  if (!authorizing) {
    return {
      title: '正在读取登录状态。',
      detail: '确认前不视为已登录。',
    };
  }
  switch (progress?.event) {
    case 'device':
      return {
        title: '请在浏览器完成登录或注册。',
        detail: '授权后还需等待登录完成。',
      };
    case 'authenticated':
      return {
        title: '正在确认登录结果…',
        detail:
          '等待最终登录状态。',
      };
    case 'committed':
      return {
        title: '正在确认登录结果…',
        detail: '核验前不视为已登录。',
      };
    case 'unknown':
      return {
        title: '收到未识别的登录进度。',
        detail:
          '不视为登录成功；请继续等待，或取消后重试。',
      };
    default:
      return {
        title: '正在准备登录…',
        detail:
          '尚未收到授权事件。',
      };
  }
}

export function waitNotice(elapsed: number): string | null {
  if (elapsed >= WAIT_TIMEOUT_SECONDS) {
    return `等待已超过 ${WAIT_TIMEOUT_SECONDS} 秒，登录仍未确认。可取消或重试。`;
  }
  if (elapsed >= LONG_WAIT_SECONDS) {
    return '仍在等待登录结果…';
  }
  return null;
}

function validityLabel(progress: AccountProgress, elapsed: number): string {
  if (Number.isFinite(progress.expiresIn) && (progress.expiresIn ?? 0) > 0) {
    const remaining = Math.max(0, Math.ceil((progress.expiresIn ?? 0) - elapsed));
    if (remaining === 0) {
      return '授权码已到期，请刷新账号状态。';
    }
    return `授权剩余约 ${Math.floor(remaining / 60)} 分 ${remaining % 60} 秒。`;
  }
  return '请在授权页完成确认。';
}

interface AccountProgressPanelProps {
  readonly progress: AccountProgress | null;
  readonly active: boolean;
  readonly authorizing: boolean;
  readonly onCancel: () => void;
  readonly onRetry: () => void;
  /** Native system-browser opener; its presence suppresses all WebView popups. */
  readonly onOpenAuthorization?: () => Promise<AuthOpenResult>;
  readonly cancelLabel?: string;
  readonly canRetryWarnings?: boolean;
}

type OpenAttempt =
  | { readonly kind: 'idle' }
  | { readonly kind: 'opening' }
  | { readonly kind: 'settled'; readonly result: AuthOpenResult };

/**
 * Shared by first-run and Settings. Warnings are retained from progress
 * events even when a later committed event arrives, so an unrecognised
 * progress event is never hidden by the final host snapshot.
 */
export function AccountProgressPanel({
  progress,
  active,
  authorizing,
  onCancel,
  onRetry,
  onOpenAuthorization,
  cancelLabel = '取消登录',
  canRetryWarnings = true,
}: AccountProgressPanelProps) {
  const [elapsed, setElapsed] = useState(0);
  const [warnings, setWarnings] = useState<AccountProgress[]>([]);
  const [authorizationElapsed, setAuthorizationElapsed] = useState(0);
  const [openAttempt, setOpenAttempt] = useState<OpenAttempt>({ kind: 'idle' });
  const [autoOpenFailed, setAutoOpenFailed] = useState(false);
  const authorization = progress?.event === 'device' ? progress : null;
  const authorizationKey = authorization
    ? [
        authorization.event,
        authorization.userCode ?? '',
        authorization.verificationUri ?? '',
        authorization.expiresIn ?? '',
        authorization.interval ?? '',
        authorization.restarted ?? '',
      ].join('|')
    : '';
  const authorizationUrl = safeAuthorizationUrl(authorization?.verificationUri);
  const nativeOpen = onOpenAuthorization !== undefined;
  const settledOpen =
    openAttempt.kind === 'settled' ? openAttempt.result : null;
  const opening = openAttempt.kind === 'opening';
  const nativeOpenFailed = settledOpen
    ? !(settledOpen.ok && settledOpen.opened)
    : autoOpenFailed;
  const canNativeOpen =
    nativeOpen &&
    authorizationUrl !== undefined &&
    authorization?.openAvailable !== false;

  useEffect(() => {
    if (!active) {
      setElapsed(0);
      return;
    }
    setElapsed(0);
    const timer = window.setInterval(() => {
      setElapsed((current) => current + 1);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [active]);

  useEffect(() => {
    if (active && progress === null) {
      setElapsed(0);
      setWarnings([]);
    }
  }, [active, progress]);

  useEffect(() => {
    if (progress?.event !== 'unknown') {
      return;
    }
    setWarnings((current) => {
      const duplicate = current.some(
        (item) =>
          item.event === progress.event &&
          item.code === progress.code &&
          item.owner === progress.owner,
      );
      return duplicate ? current : [...current, progress];
    });
  }, [progress]);

  useEffect(() => {
    setOpenAttempt({ kind: 'idle' });
    setAutoOpenFailed(false);
  }, [authorizationKey]);

  useEffect(() => {
    if (progress?.openOpened) setAutoOpenFailed(false);
    else if (progress?.openError) setAutoOpenFailed(true);
  }, [progress?.openError, progress?.openOpened, authorizationKey]);

  useEffect(() => {
    setAuthorizationElapsed(0);
    if (!active || !authorization) return;
    const timer = window.setInterval(() => {
      setAuthorizationElapsed((current) => current + 1);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [active, authorizationKey]);

  if (!active && warnings.length === 0) {
    return null;
  }

  async function openAuthorizationPage(): Promise<void> {
    if (nativeOpen && onOpenAuthorization) {
      if (opening) return;
      setOpenAttempt({ kind: 'opening' });
      let result: AuthOpenResult;
      try { result = await onOpenAuthorization(); }
      catch { result = { ok: false, opened: false }; }
      setOpenAttempt({ kind: 'settled', result });
      return;
    }
  }

  const copy = accountProgressCopy(progress, authorizing);
  const waited = waitNotice(elapsed);
  const timedOut = elapsed >= WAIT_TIMEOUT_SECONDS;

  return (
    <div className="account-progress" data-account-progress>
      {active && (
        <div role="status" aria-live="polite" className="account-progress__live">
          <p className="account-progress__title">
            <span className="ui-button__spinner" aria-hidden="true" />
            {copy.title}
          </p>
          {progress?.event === 'unknown'
            ? <p className="account-progress__detail">{copy.detail}</p> : null}
          {waited && <p className="account-progress__detail">{waited}</p>}
          {authorization?.userCode && <p>授权码：{authorization.userCode}</p>}
          {authorization && <p className="account-progress__meta">{validityLabel(authorization, authorizationElapsed)}</p>}
          {nativeOpenFailed && (
            <p role="alert" className="account-progress__detail">
              {settledOpen?.retryable === false
                ? '无法打开系统浏览器，请稍后重试登录。'
                : settledOpen
                  ? '无法打开系统浏览器，请点击“打开授权页”重试。'
                  : '无法自动打开系统浏览器，请点击“打开授权页”重试。'}
            </p>
          )}
          {authorization && nativeOpen && authorization.openAvailable === false && (
            <p className="account-progress__detail">
              无法打开系统浏览器，请取消后重新登录。
            </p>
          )}
          <div className="settings-actions">
            {authorizationUrl && (
              nativeOpen ? <Button
                variant="primary"
                disabled={opening || !canNativeOpen}
                onClick={() => void openAuthorizationPage()}
              >打开授权页</Button> : <a
                className="account-progress__open"
                href={authorizationUrl}
                target="_blank"
                rel="noopener noreferrer"
              >打开授权页</a>
            )}
            {authorizing && (
              <Button variant="text" onClick={onCancel}>
                {cancelLabel}
              </Button>
            )}
            {authorizing && timedOut && (
              <Button variant="primary" onClick={onRetry}>
                重试登录
              </Button>
            )}
          </div>
        </div>
      )}
      {!active && warnings.map((warning) => (
        <Notice
          key={`${warning.event}:${warning.code ?? ''}:${warning.owner ?? ''}`}
          tone="warning"
          title="登录进度未识别"
          action={
            canRetryWarnings
              ? { label: '重试登录', onClick: onRetry }
              : undefined
          }
        >
          收到未识别的进度，未视为成功。请重试登录。
        </Notice>
      ))}
    </div>
  );
}
