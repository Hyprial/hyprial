/**
 * Decision record 2026-11-10 (平台接入详情).
 *
 * - 列表只放摘要；授权、运行、平台身份明细和删除都在条目详情里。
 * - 平台身份明细由能力端口读取；正式 MethodSpec 缺口见统一对账，未接时不伪造表格。
 * - 身份行只展示已记录数据（observed/verified）；IM 账号只是用户的可选信息，hyprial 不判定谁是
 *   owner（2026-11-19 决策），所以没有 owner 列、查询绑定与配置绑定面板。
 * - observed 显示为已采集，不暗示存在本页人工核验流程；权限保留后端能力 ID。
 * - 不可用不在页面上写说明（决策 PR 1f/F4）：端口上没有的方法不渲染控件（没有
 *   listIdentities 就没有平台身份块，没有 removePreview/remove 就没有删除）；被目录拒绝的
 *   自动读取等于没有端口，块不渲染、不显示为空；用户触发的读取或操作被拒绝时控件保持
 *   可用，只发一次通知（主题为控件的可见文案），不改动已有数据与状态。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { methodRefusal } from '../../client';
import { useNotifyUnavailable } from '../../components/notifications';
import { Button } from '../../components/primitives';
import {
  capabilityRefusal,
  type AdapterIdentityRow,
  type AdapterOperation,
  type PlatformIntegration,
  type PlatformIntegrationsApi,
} from './platform-integrations-api';
import {
  active,
  identityLabel,
  runtimeLabel,
} from './platform-integrations-view';
import { OperationProgress } from './impl/platform/OperationProgress';

interface PlatformIntegrationDetailProps {
  readonly row: PlatformIntegration;
  readonly api: PlatformIntegrationsApi;
  readonly busy: string | null;
  readonly operation?: AdapterOperation;
  readonly authorizationReadPending?: boolean;
  readonly onAuthorize: (label: string) => void;
  readonly onPollAuthorization: () => void;
  readonly onCancelAuthorization: () => void;
  readonly onStartStop: () => void;
  readonly onSyncIdentities: () => Promise<boolean | void>;
  readonly onRemove: () => void;
}

type IdentityView =
  | { readonly state: 'loading' }
  | { readonly state: 'ready'; readonly rows: readonly AdapterIdentityRow[] }
  | { readonly state: 'error' }
  | { readonly state: 'absent' };

const KIND_LABELS: Record<AdapterIdentityRow['kind'], string> = {
  app: '应用',
  bot: '机器人',
  user: '用户',
};

const STANDING_LABELS: Record<AdapterIdentityRow['standing'], string> = {
  verified: '已核验',
  observed: '已采集',
};

function kindLabel(kind: AdapterIdentityRow['kind']): string {
  return KIND_LABELS[kind];
}

function standingLabel(standing: AdapterIdentityRow['standing']): string {
  return STANDING_LABELS[standing];
}

function timeLabel(ms: number | null): string {
  return ms === null ? '—' : new Date(ms).toLocaleString('zh-CN', { hour12: false });
}

export function PlatformIntegrationDetail({
  row,
  api,
  busy,
  operation,
  authorizationReadPending = false,
  onAuthorize,
  onPollAuthorization,
  onCancelAuthorization,
  onStartStop,
  onSyncIdentities,
  onRemove,
}: PlatformIntegrationDetailProps) {
  const notifyUnavailable = useNotifyUnavailable();
  const notifyRef = useRef(notifyUnavailable);
  useEffect(() => { notifyRef.current = notifyUnavailable; });
  const [identities, setIdentities] = useState<IdentityView>(() => (
    api.listIdentities ? { state: 'loading' } : { state: 'absent' }
  ));
  const [syncing, setSyncing] = useState(false);
  const [authorization, setAuthorization] = useState(row.authorization);
  const [permissionRead, setPermissionRead] = useState<'ready' | 'loading' | 'failed'>('ready');
  const permissionGeneration = useRef(0);
  const identityGeneration = useRef(0);
  const syncGeneration = useRef(0);
  const syncActive = useRef(false);

  const loadAuthorization = useCallback(async (userInitiated = false) => {
    const current = ++permissionGeneration.current;
    if (!api.authorizationStatus) {
      setAuthorization(row.authorization);
      setPermissionRead('ready');
      return;
    }
    setPermissionRead('loading');
    try {
      const result = await api.authorizationStatus(row.name);
      if (current !== permissionGeneration.current) return;
      setAuthorization(result);
      setPermissionRead('ready');
    } catch (error) {
      if (current !== permissionGeneration.current) return;
      const refusal = methodRefusal(error);
      if (refusal) {
        setPermissionRead('ready');
        if (userInitiated) notifyRef.current('刷新权限', refusal);
        return;
      }
      setAuthorization({ adapter: row.name, appId: null, state: 'unconfirmed', capabilities: [], missingCapabilities: [], checkedAt: null });
      setPermissionRead('failed');
    }
  }, [api, row.name, row.authorization]);

  useEffect(() => {
    setAuthorization(row.authorization);
    void loadAuthorization();
    return () => { permissionGeneration.current += 1; };
  }, [loadAuthorization, row.authorization]);

  const loadIdentities = useCallback(async () => {
    const current = ++identityGeneration.current;
    if (!api.listIdentities) {
      setIdentities({ state: 'absent' });
      return;
    }
    setIdentities({ state: 'loading' });
    try {
      const result = await api.listIdentities(row.name);
      if (current !== identityGeneration.current) return;
      setIdentities({ state: 'ready', rows: result.identities });
    } catch (error) {
      if (current === identityGeneration.current) {
        setIdentities(methodRefusal(error) ? { state: 'absent' } : { state: 'error' });
      }
    }
  }, [api, row.name]);

  useEffect(() => {
    syncGeneration.current += 1;
    syncActive.current = false;
    setSyncing(false);
    void loadIdentities();
    return () => {
      identityGeneration.current += 1;
      syncGeneration.current += 1;
      syncActive.current = false;
    };
  }, [loadIdentities]);

  async function sync(): Promise<void> {
    if (!api.syncIdentities || syncActive.current || busy !== null) return;
    const current = ++syncGeneration.current;
    syncActive.current = true;
    setSyncing(true);
    try {
      const performed = await onSyncIdentities();
      if (current !== syncGeneration.current) return;
      if (performed !== false) await loadIdentities();
    } finally {
      if (current === syncGeneration.current) {
        syncActive.current = false;
        setSyncing(false);
      }
    }
  }

  function authorize(label: string): void {
    const refusal = capabilityRefusal(api.capabilities?.authorization);
    if (refusal) {
      notifyUnavailable(label, refusal);
      return;
    }
    onAuthorize(label);
  }

  function requestSync(): void {
    const refusal = capabilityRefusal(api.capabilities?.identities);
    if (refusal) {
      notifyUnavailable('同步平台身份', refusal);
      return;
    }
    void sync();
  }

  const canRemove = api.removePreview !== undefined && api.remove !== undefined;
  const rowBusy = busy !== null || syncing;
  const missing = authorization.missingCapabilities;
  const authorizing = operation !== undefined && active(operation);

  return (
    <div className="platform-integration__detail">
      <dl className="platform-integration__facts">
        <div>
          <dt>App ID</dt>
          <dd>{authorization.appId ?? '—'}</dd>
        </div>
        <div>
          <dt>绑定 Agent</dt>
          <dd>{row.boundAgent ?? '未绑定'}</dd>
        </div>
      </dl>

      <section className="platform-integration__section" aria-labelledby={`authorization-${row.name}`}>
        <div className="platform-integration__section-head">
          <h4 id={`authorization-${row.name}`}>授权</h4>
          {api.authorizationStatus && (
            <Button variant="text" size="sm" disabled={rowBusy || permissionRead === 'loading'} onClick={() => void loadAuthorization(true)}>
              刷新权限
            </Button>
          )}
          {!authorizing && api.beginAuthorization && (
            <Button
              variant="secondary"
              size="sm"
              disabled={rowBusy || authorizationReadPending}
              onClick={() => authorize(authorization.state === 'authorized' ? '重新授权' : '授权')}
            >
              {authorization.state === 'authorized' ? '重新授权' : '授权'}
            </Button>
          )}
        </div>
        {permissionRead === 'failed' && <p className="platform-integrations__error" role="alert">权限读取失败。</p>}
        {authorization.capabilities.length > 0 ? (
          <div className="platform-permissions">
            <span className="platform-integrations__hint">已授权</span>
            <ul aria-label="已授权权限">
              {authorization.capabilities.map(id => <li key={id}>{id}</li>)}
            </ul>
          </div>
        ) : (
          <p className="platform-integrations__hint">
            {authorization.state === 'unconfirmed' ? '权限状态未确认' : '暂无已授权权限'}
          </p>
        )}
        {missing.length > 0 && (
          <div className="platform-permissions">
            <span className="platform-integrations__error">缺少权限</span>
            <ul aria-label="缺少权限" className="platform-integrations__error">
              {missing.map(id => <li key={id}>{id}</li>)}
            </ul>
          </div>
        )}
        {operation && <OperationProgress operation={operation} busy={rowBusy}
          onRefresh={api.authorizationOperation ? onPollAuthorization : undefined}
          onCancel={api.cancelAuthorization && operation.operationId ? onCancelAuthorization : undefined} />}
      </section>

      <section className="platform-integration__section" aria-labelledby={`runtime-${row.name}`}>
        <div className="platform-integration__section-head">
          <h4 id={`runtime-${row.name}`}>运行：{runtimeLabel(row)}</h4>
          <Button
            variant="secondary"
            size="sm"
            disabled={rowBusy}
            onClick={onStartStop}
          >
            {row.processRunning || row.desired ? '停止' : '启动'}
          </Button>
        </div>
        {row.error && <p className="platform-integrations__error" role="alert">{row.error}</p>}
      </section>

      {identities.state !== 'absent' && (
      <section className="platform-integration__section" aria-labelledby={`identities-${row.name}`}>
        <div className="platform-integration__section-head">
          <h4 id={`identities-${row.name}`}>平台身份</h4>
          {api.syncIdentities && (
            <Button
              variant="secondary"
              size="sm"
              disabled={rowBusy}
              loading={syncing}
              onClick={requestSync}
            >
              同步平台身份
            </Button>
          )}
        </div>
        {row.identities.state === 'synced' && (
          <p className="platform-integrations__hint">{identityLabel(row)}</p>
        )}
        {identities.state === 'loading' && <p role="status">正在读取平台身份…</p>}
        {identities.state === 'error' && (
          <p className="platform-integrations__error" role="alert">平台身份读取失败。</p>
        )}
        {identities.state === 'ready' && identities.rows.length === 0 && (
          <p className="platform-integrations__hint">
            {row.identities.state === 'not-synced' ? '尚未同步平台身份。' : '没有可显示的平台身份。'}
          </p>
        )}
        {identities.state === 'ready' && identities.rows.length > 0 && (
          <div className="platform-identities__scroll">
            <table className="platform-identities" aria-label="平台身份明细">
              <thead>
                <tr>
                  <th>类型</th>
                  <th>显示名</th>
                  <th>平台 ID</th>
                  <th>记录状态</th>
                  <th>来源</th>
                  <th>最近发现</th>
                </tr>
              </thead>
              <tbody>
                {identities.rows.map(identity => (
                  <tr key={`${identity.kind}:${identity.platformId}`}>
                    <td>{kindLabel(identity.kind)}</td>
                    <td>{identity.displayName ?? '—'}</td>
                    <td><code>{identity.platformId}</code></td>
                    <td>{standingLabel(identity.standing)}</td>
                    <td>{identity.source}</td>
                    <td>{timeLabel(identity.lastSeenMs)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
      )}

      {canRemove && (
        <div className="platform-integration__section">
          <div className="platform-integration__section-head">
            <Button
              variant="danger"
              size="sm"
              disabled={rowBusy}
              onClick={onRemove}
            >
              删除接入
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
