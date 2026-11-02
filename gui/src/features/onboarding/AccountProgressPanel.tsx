import { useEffect, useState } from 'react';
import { Button, Notice } from '../../components/primitives';
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
      title: '正在读取当前实例的登录状态。',
      detail: '结果返回前不视为已登录；不会启动新的登录。',
    };
  }
  switch (progress?.event) {
    case 'device':
      return {
        title: '请在浏览器完成账号授权。',
        detail: '账号尚未登录；收到授权确认后还要等待登录提交结果。',
      };
    case 'binding-device':
      return {
        title: progress.restarted
          ? '需要补充授权：附加设备授权已重新发起，请再次确认。'
          : '需要补充授权：请完成附加设备授权。',
        detail: '账号授权已确认；此步骤确认本机设备绑定，账号尚未完成登录，界面不会绕过绑定。',
      };
    case 'authenticated':
      return {
        title: '账号授权已确认，正在等待后台结果。',
        detail:
          '尚未显示为已登录；需要等待本机返回登录提交与最终状态，界面不会把等待当作成功。',
      };
    case 'committed':
      return {
        title: '登录提交已确认，正在核验本机登录状态。',
        detail: '核验完成前不显示为已登录，也不把提交当作最终结果。',
      };
    case 'binding-proof-failed':
      return {
        title: '附加设备授权未确认。',
        detail:
          '账号授权已确认，但设备绑定未完成；不会自动重试或绕过绑定。登录提交可能仍会继续，最终以本机状态为准。',
      };
    case 'unknown':
      return {
        title: '收到未识别的登录进度。',
        detail:
          '该进度不会视为登录成功；请继续等待、取消后重试，或检查本机服务版本。',
      };
    default:
      return {
        title: '等待后台结果，原因待确认。',
        detail:
          '尚未收到授权事件；界面无法确认更细的服务端步骤，不会把等待显示为成功。',
      };
  }
}

export function waitNotice(elapsed: number): string | null {
  if (elapsed >= WAIT_TIMEOUT_SECONDS) {
    return `等待已超过 ${WAIT_TIMEOUT_SECONDS} 秒，后台状态仍未确认。可取消后重试；界面不会自动重复登录或覆盖已有凭据。`;
  }
  if (elapsed >= LONG_WAIT_SECONDS) {
    return '后台仍在处理且尚未返回确认；界面无法确认更细的服务端步骤，不会把等待显示为成功。';
  }
  return null;
}

function validityLabel(progress: AccountProgress, elapsed: number): string {
  if (Number.isFinite(progress.expiresIn) && (progress.expiresIn ?? 0) > 0) {
    const remaining = Math.max(0, Math.ceil((progress.expiresIn ?? 0) - elapsed));
    if (remaining === 0) {
      return '授权有效期已结束；请取消后重新登录，界面不会自动重复登录。';
    }
    return `授权剩余约 ${Math.floor(remaining / 60)} 分 ${remaining % 60} 秒（由授权服务返回，以授权页为准）。`;
  }
  return '授权有效期未提供，请尽快完成；实际有效期以授权页为准。';
}

interface AccountProgressPanelProps {
  readonly progress: AccountProgress | null;
  readonly active: boolean;
  readonly authorizing: boolean;
  readonly onCancel: () => void;
  readonly onRetry: () => void;
  readonly cancelLabel?: string;
  readonly canRetryWarnings?: boolean;
}

/**
 * Shared by first-run and Settings. Warnings are retained from progress
 * events even when a later committed event arrives, so a binding failure is
 * never hidden by the final host snapshot.
 */
export function AccountProgressPanel({
  progress,
  active,
  authorizing,
  onCancel,
  onRetry,
  cancelLabel = '取消登录',
  canRetryWarnings = true,
}: AccountProgressPanelProps) {
  const [elapsed, setElapsed] = useState(0);
  const [warnings, setWarnings] = useState<AccountProgress[]>([]);
  const [popupBlocked, setPopupBlocked] = useState(false);
  const [authorizationElapsed, setAuthorizationElapsed] = useState(0);
  const authorization =
    progress?.event === 'device' || progress?.event === 'binding-device'
      ? progress
      : null;
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
    if (
      progress?.event !== 'binding-proof-failed' &&
      progress?.event !== 'unknown'
    ) {
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
    setPopupBlocked(false);
  }, [authorizationUrl]);

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

  function openAuthorizationPage(): void {
    if (!authorizationUrl) return;
    try {
      const popped = window.open('about:blank', '_blank');
      if (!popped) {
        setPopupBlocked(true);
        return;
      }
      popped.opener = null;
      popped.location.replace(authorizationUrl);
      setPopupBlocked(false);
    } catch {
      setPopupBlocked(true);
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
          <p className="account-progress__detail">{copy.detail}</p>
          {elapsed > 0 && (
            <p className="account-progress__meta">已等待 {elapsed} 秒。</p>
          )}
          {waited && <p className="account-progress__detail">{waited}</p>}
          {authorization?.userCode && (
            <p>授权码：{authorization.userCode}</p>
          )}
          {authorization && (
            <p className="account-progress__meta">
              {authorization.event === 'binding-device'
                ? '附加设备授权'
                : '账号授权'}
              ；{validityLabel(authorization, authorizationElapsed)}
              {authorization.interval
                ? ` 轮询间隔约 ${authorization.interval} 秒。`
                : ''}
            </p>
          )}
          {authorization?.restarted && (
            <p className="account-progress__detail">
              授权已重新发起，请完成最新一次确认。
            </p>
          )}
          {authorization && (
            <p className="account-progress__detail">
              请在官方授权页按提示输入设备码并完成授权；设备码以授权页为准，本界面不会显示或保存访问令牌。
            </p>
          )}
          {authorization && (
            <p className="account-progress__meta">
              若浏览器拦截了弹窗，请点击下方按钮或授权链接；无需在这里填写密码或访问令牌。
            </p>
          )}
          {authorizationUrl && (
            <div className="settings-actions">
              <Button variant="secondary" onClick={openAuthorizationPage}>
                打开授权页
              </Button>
              <a
                href={authorizationUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                在浏览器中打开授权页
              </a>
            </div>
          )}
          {popupBlocked && (
            <p className="account-progress__detail">
              浏览器未允许打开新窗口；请直接使用上方授权链接继续。
            </p>
          )}
          <div className="settings-actions">
            {authorizing && (
              <Button variant="secondary" onClick={onCancel}>
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
          tone={warning.event === 'binding-proof-failed' ? 'danger' : 'warning'}
          title={
            warning.event === 'binding-proof-failed'
              ? '附加设备授权未确认'
              : '登录进度未识别'
          }
          action={
            canRetryWarnings
              ? { label: '重试登录', onClick: onRetry }
              : undefined
          }
        >
          {warning.event === 'binding-proof-failed' ? (
            <>
              错误代码：{warning.code ?? '未提供'}。账号可能已登录，但设备绑定未更新；
              请重新登录以再次完成附加授权，不会绕过绑定或修改登录规则。
            </>
          ) : (
            <>
              收到未识别的进度事件，未视为成功。请重试登录或检查本机服务版本。
            </>
          )}
        </Notice>
      ))}
    </div>
  );
}
