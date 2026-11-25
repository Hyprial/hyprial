import { useState } from 'react';
import { Button, Card, Dialog, ErrorState, Icon, LoadingState, Notice } from '../../../components/primitives';
import { AccountProgressPanel, type AccountActionResult } from '../../onboarding';
import type { AuthOpenResult } from '../../../platform/auth';
import { useAccountSettings } from './useAccountSettings';

/** Read-only projection of the existing auth status capability. */
export function AccountStatusField({ value, loading = false }: { readonly value: AccountActionResult; readonly loading?: boolean }) {
  if (loading) return <div><LoadingState title="正在读取账号状态" /></div>;
  if (value.status === 'unavailable') return value.message ? <div><ErrorState message={value.message} /></div> : null;
  if (value.status === 'failed') return <div><ErrorState message={value.message} /></div>;
  if (value.status === 'authenticated') {
    const displayName = value.username ?? value.owner;
    return <div>{displayName ? <p>{displayName}</p> : <ErrorState message="未取得账号信息" />}</div>;
  }
  return <p role="status">{value.status === 'unauthenticated' ? '未登录' : value.message ?? '暂时无法读取账号信息，请刷新重试。'}</p>;
}
/** Composes status, login, refresh and cancellation through one existing action port. */
export function AccountLoginSection({ state, onOpenAuthorization }: { readonly state: ReturnType<typeof useAccountSettings>; readonly onOpenAuthorization?: () => Promise<AuthOpenResult> }) {
  const { snapshot, busy, progress, run, cancel, retry, logout, logoutState, canLogout, hasLogout, hasLogin } = state;
  const [confirmLogout, setConfirmLogout] = useState(false);
  const authenticated = snapshot.account.status === 'authenticated';
  return <Card as="section" padding="lg" className="settings-card" aria-label="账号登录">
    {busy !== 'login' && <><AccountStatusField value={snapshot.account} loading={busy === 'read'} />
    <div className="settings-actions">
      {!authenticated && hasLogin && <Button variant="primary" disabled={busy !== null || logoutState?.status === 'unknown' || logoutState?.nextAction === 'inspect'} onClick={() => void retry()}>登录 Hyprial</Button>}
      <Button variant="text" size="compact" leadingIcon={<Icon name="arrow-clockwise" />} disabled={busy !== null} onClick={() => void run('read')}>刷新账号状态</Button>
      {authenticated && hasLogout && <Button variant="secondary" disabled={busy !== null || !canLogout}
        onClick={() => setConfirmLogout(true)}>退出本机登录</Button>}
      {busy === 'device' && <Button onClick={() => void cancel()}>取消授权</Button>}
    </div></>}
    {busy === 'logout' && <p role="status">正在退出本机登录，请等待结果确认。</p>}
    {logoutState && <Notice title={logoutState.status === 'logged-out' ? '本机退出已确认' : '账号退出'}
      tone={logoutState.status === 'logged-out' ? 'info' : 'warning'}>{logoutState.message}</Notice>}
    <Dialog open={confirmLogout} title="确认退出本机登录" onClose={() => setConfirmLogout(false)}
      actions={[{ label: '保留登录', onClick: () => setConfirmLogout(false) },
        { label: '确认退出', variant: 'danger', disabled: busy !== null || !canLogout,
          onClick: () => { setConfirmLogout(false); void logout(); } }]}>
      <p>将移除本实例的登录信息；如服务正在使用该身份，会先停止服务，进行中的工作可能中断。</p>
      <p>不会退出授权网站，设备与 Agent 凭据保留。</p>
    </Dialog>
    {busy === 'device' && <p role="status">正在确认当前实例状态。</p>}
    <fieldset className="settings-fieldset">
      <AccountProgressPanel progress={progress} active={busy === 'login' || busy === 'read'} authorizing={busy === 'login'} cancelLabel="取消授权" canRetryWarnings={authenticated} onCancel={() => void cancel()} onRetry={() => void retry()} onOpenAuthorization={onOpenAuthorization} />
    </fieldset>
  </Card>;
}
