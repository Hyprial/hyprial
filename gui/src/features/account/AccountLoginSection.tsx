import { Button, Card, EmptyState, ErrorState, LoadingState, Notice, StatusCard } from '../../components/primitives';
import { AccountProgressPanel } from '../onboarding/AccountProgressPanel';
import { accountPresentation } from '../onboarding/presentation';
import type { AccountActionResult } from '../onboarding';
import { useAccountSettings } from './useAccountSettings';

/** Read-only projection of the existing auth status capability. */
export function AccountStatusField({ value, loading = false, disabled = false }: { readonly value: AccountActionResult; readonly loading?: boolean; readonly disabled?: boolean }) {
  if (loading) return <div aria-disabled={disabled}><LoadingState title="正在读取账号状态" /></div>;
  if (value.status === 'unavailable' || value.status === 'failed') return <div aria-disabled={disabled}><ErrorState message={value.message ?? '账号状态不可用'} /></div>;
  if (value.status === 'unauthenticated') return <div aria-disabled={disabled}><StatusCard label="账号状态" {...accountPresentation({ kind: 'settled', result: value }, 'live')} /><EmptyState title="尚未登录" /></div>;
  return <div aria-disabled={disabled}><StatusCard label="账号状态" {...accountPresentation({ kind: 'settled', result: value }, 'live')} />{value.status === 'authenticated' && value.owner && <p>当前账号：{value.owner}</p>}</div>;
}
/** Composes status, login, refresh and cancellation through one existing action port. */
export function AccountLoginSection({ state, disabled = false }: { readonly state: ReturnType<typeof useAccountSettings>; readonly disabled?: boolean }) {
  const { snapshot, busy, progress, run, cancel, retry } = state;
  const authenticated = snapshot.account.status === 'authenticated';
  return <Card as="section" className="settings-card" aria-label="账号登录">
    <h2>账号</h2>
    <AccountStatusField value={snapshot.account} loading={busy === 'read'} disabled={disabled} />
    <p>在官方授权页登录，无需在这里填写密码或访问令牌。</p>
    <div className="settings-actions">
      {!authenticated && <Button variant="primary" disabled={disabled || busy !== null} onClick={() => void retry()}>登录 Hyprial</Button>}
      <Button disabled={disabled || busy !== null} onClick={() => void run('read')}>刷新账号状态</Button>
      {busy === 'device' && <Button disabled={disabled} onClick={() => { if (!disabled) void cancel(); }}>取消授权操作</Button>}
    </div>
    {busy === 'device' && <p role="status">正在确认当前实例状态。</p>}
    <fieldset disabled={disabled} style={{ border: 0, margin: 0, padding: 0 }}>
      <AccountProgressPanel progress={progress} active={busy === 'login' || busy === 'read'} authorizing={busy === 'login'} cancelLabel="取消授权操作" canRetryWarnings={authenticated} onCancel={() => { if (!disabled) void cancel(); }} onRetry={() => { if (!disabled) void retry(); }} />
    </fieldset>
    {busy !== 'read' && (snapshot.account.status === 'unauthenticated' || snapshot.account.status === 'unknown') && snapshot.account.message && <Notice title="账号状态" tone="warning">{snapshot.account.message}</Notice>}
  </Card>;
}
