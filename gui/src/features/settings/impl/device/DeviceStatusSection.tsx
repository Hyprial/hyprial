/**
 * One shared initialization gate; device data is mounted only for an initialized account.
 * A status read refused before dispatch hides only the account/initialization block; the
 * cards that have ports still render (the shell hides the page when none exist).
 */
import type { ReactNode } from 'react';
import { useState } from 'react';
import { Button, Card, Notice } from '../../../../components/primitives';
import { useAccountSettings } from '../../../account';
import type { FirstRunActions } from '../../../onboarding';
import type { LocalManagementApi } from '../api';
import { LocalManagementSection } from '../LocalManagementSection';
import { RefreshButton } from '../views/Readout';

export function DeviceStatusSection({ actions, api, children }: {
  readonly actions: FirstRunActions; readonly api?: LocalManagementApi; readonly children?: ReactNode;
}) {
  const { snapshot, busy, run, hasDeviceInit } = useAccountSettings(actions);
  const [loginRequired, setLoginRequired] = useState(false);
  if (snapshot.account.status === 'unauthenticated') return !hasDeviceInit ? null : <Card as="section" padding="lg" className="settings-card" aria-label="初始化当前设备">
    <Button variant="primary" onClick={() => setLoginRequired(true)}>初始化当前设备</Button>
    {loginRequired && <p role="status">请先登录</p>}
  </Card>;
  const cards = <>{api && <LocalManagementSection api={api} />}{children}</>;
  if (snapshot.account.status === 'unavailable' && snapshot.account.refusal) return cards;
  if (snapshot.account.status !== 'authenticated') return <p role="status">{busy === 'read' ? '正在读取…' : '账号状态未确认'}</p>;
  if (snapshot.device.status === 'ready') return cards;
  if (!hasDeviceInit) {
    return snapshot.device.message ? <Card as="section" padding="lg" className="settings-card" aria-label="设备状态">
      <Notice tone="warning" title="设备状态待恢复">{snapshot.device.message}</Notice>
      <RefreshButton label="刷新设备状态" disabled={busy !== null} onClick={() => void run('read', '刷新设备状态')} />
    </Card> : cards;
  }
  const device = snapshot.device;
  const unsettled = ['unknown', 'unavailable', 'failed'].includes(device.status);
  return <Card as="section" padding="lg" className="settings-card" aria-label="初始化当前设备">
    <Button variant="primary" disabled={busy !== null || device.status === 'unknown'} onClick={() => void run('device')}>
      {busy === 'device' ? '正在初始化…' : '初始化当前设备'}
    </Button>
    {unsettled && <>
      <Notice tone="warning" title={device.status === 'unknown' ? '初始化状态未确认' : '初始化未完成'}>{device.message}</Notice>
      <RefreshButton label="初始化状态" disabled={busy !== null} onClick={() => void run('read', '刷新初始化状态')} />
    </>}
  </Card>;
}
