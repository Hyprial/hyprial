/**
 * Decision record 2026-11-23 (平台绑定, I6-4).
 *
 * - 绑定是 Agent cell 文档的 adapter 成员：本页 bind/unbind 经 agent.cell.get +
 *   agent.cell.update（CAS）完成，不再调用 adapter.pin/unpin（已退役）。
 * - 绑定目标是 Agent 的 canonical actor URI；fixture 组合直接使用本地 Agent id。
 * - 本页只有“绑定 / 解除绑定”：已绑定本 Agent 时显示“解除绑定”，已绑定其它
 *   Agent 的 adapter 只读（跨 Agent 冲突会被路由索引 fail-closed，不制造第二处绑定）。
 * - 同一 Agent 可持有多个 adapter 成员（cell 文档没有一对一约束）。
 * - 消息路由复用 adapter.routes.*，只配置当前绑定的接入；不等于 Agent 入站绑定。
 * - 自行发起的读取（挂载、绑定/解绑后重读）在发出前被拒绝不是失败：不发通知、不写
 *   「读取失败」、没有重试——保留已有的行；没有任何行可显示时本页签内容不渲染。
 *   用户点「刷新」被拒绝才发一条通知。绑定/解除绑定在忙碌态开始之前先查方法目录
 *   （checkAccess），被拒绝只发一条通知。
 * - 前台每两秒只读更新，保留路由草稿；手动读取、写操作及来源变化拒绝旧回包。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { methodRefusal, type MethodRefusal } from '../../../../client';
import { useNotifyUnavailable } from '../../../../components/notifications';
import { Button } from '../../../../components/primitives';
import { ClientError, DomainError } from '../../../../client';
import { RouteSettings } from '../routes/RouteSettings';
import type {
  AgentAdapterBindingRow,
  AgentAdapterBindingsApi,
} from '../api/agent-adapter-bindings';

interface PlatformBindingsTabProps {
  readonly agentActor: string;
  readonly agentLabels?: readonly { readonly uri: string; readonly displayName: string }[];
  readonly api: AgentAdapterBindingsApi;
  readonly onNavigateConnections?: () => void;
  readonly checkAccess?: (method: string) => MethodRefusal | null;
}

const PROVIDER_LABELS: Record<string, string> = {
  lark: '飞书',
  wecom: '企业微信',
  whatsapp: 'WhatsApp',
};

function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider] ?? provider;
}

function agentLabel(
  labels: readonly { readonly uri: string; readonly displayName: string }[] | undefined,
  actor: string,
): string {
  return labels?.find(row => row.uri === actor)?.displayName ?? actor;
}

function isRevisionConflict(error: ClientError): boolean {
  if (!(error instanceof DomainError)) return false;
  const data = error.data;
  return !!data && typeof data === 'object' && !Array.isArray(data)
    && 'expected' in data && 'current' in data;
}

function noticeText(error: unknown): string {
  if (error instanceof ClientError) {
    if (error.code === 'INVALID_ARGUMENT' && isRevisionConflict(error)) return '绑定已变化，请查看当前状态。';
    if (error.code === 'CALLER_NOT_AUTHORIZED') return '当前账号无权修改该绑定。';
    if (error.code === 'ORG_SOURCE_UNREACHABLE') return 'OrgFS 未连接，现在不能编辑 cell，请稍后重试。';
    return `绑定操作未确认（${error.code}），请核对当前状态。`;
  }
  return error instanceof Error ? error.message : '操作结果尚未确认。';
}

function forwardingLabel(row: AgentAdapterBindingRow): string {
  if (row.boundAgent === null) return '未绑定';
  if (row.error) return `接入错误：${row.error}`;
  return row.online && row.processRunning ? '接入运行中' : '接入未运行';
}

export function PlatformBindingsTab({
  agentActor, agentLabels, api, onNavigateConnections, checkAccess,
}: PlatformBindingsTabProps) {
  const source = useRef(0);
  const readVersion = useRef(0);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error' | 'absent'>('loading');
  const notifyUnavailable = useNotifyUnavailable();
  const notifyRef = useRef(notifyUnavailable);
  notifyRef.current = notifyUnavailable;
  const [rows, setRows] = useState<readonly AgentAdapterBindingRow[]>([]);
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const [notice, setNotice] = useState<{ tone: 'status' | 'error'; text: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [refreshError, setRefreshError] = useState(false);

  const reload = useCallback(async (manual = false) => {
    const current = source.current;
    if (!manual) setStatus('loading');
    const version = ++readVersion.current;
    setNotice(null);
    setRefreshError(false);
    try {
      const result = await api.list();
      if (current !== source.current || version !== readVersion.current) return;
      setRows(result);
      setStatus('ready');
    } catch (error) {
      if (current !== source.current || version !== readVersion.current) return;
      const refusal = methodRefusal(error);
      if (refusal) {
        if (manual) notifyRef.current('读取平台绑定', refusal);
        setStatus(rowsRef.current.length > 0 ? 'ready' : 'absent');
        return;
      }
      setStatus('error');
      setNotice({ tone: 'error', text: noticeText(error) });
    }
  }, [api]);

  useEffect(() => {
    source.current += 1;
    setRows([]); setBusy(null); setNotice(null);
    setStatus('loading');
    void reload();
    return () => { source.current += 1; };
  }, [reload, api, agentActor]);

  useEffect(() => {
    if (!api || status !== 'ready' || busy !== null) return;
    let cancelled = false;
    let reading = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const current = source.current;
    const schedule = () => {
      clearTimeout(timer);
      if (!cancelled && !reading && document.visibilityState !== 'hidden') timer = setTimeout(() => void read(), 2000);
    };
    const read = async () => {
      const version = ++readVersion.current;
      reading = true;
      try {
        const result = await api.list();
        if (!cancelled && current === source.current && version === readVersion.current) {
          setRows(result); setRefreshError(false);
        }
      } catch {
        if (!cancelled && current === source.current && version === readVersion.current) setRefreshError(true);
      } finally { reading = false; schedule(); }
    };
    schedule();
    document.addEventListener('visibilitychange', schedule);
    return () => {
      cancelled = true; clearTimeout(timer);
      document.removeEventListener('visibilitychange', schedule);
    };
  }, [api, agentActor, status, busy]);

  async function refresh(): Promise<void> {
    if (busy !== null) return;
    const current = source.current;
    setBusy('refresh');
    try {
      await reload(true);
    } finally {
      if (current === source.current) setBusy(null);
    }
  }

  async function run(
    key: string,
    subject: string,
    method: string,
    action: () => Promise<void>,
  ): Promise<void> {
    if (busy !== null) return;
    const refusal = checkAccess?.(method);
    if (refusal) {
      notifyRef.current(subject, refusal);
      return;
    }
    const current = source.current;
    readVersion.current += 1;
    setBusy(key);
    setNotice(null);
    try {
      await action();
      if (current !== source.current) return;
      await reload();
    } catch (error) {
      const refusal = methodRefusal(error);
      if (refusal) {
        notifyRef.current(subject, refusal);
      } else if (current === source.current) {
        await reload();
        setNotice({ tone: 'error', text: noticeText(error) });
      }
    } finally {
      if (current === source.current) setBusy(null);
    }
  }

  if (status === 'absent') return null;
  return (
    <div className="agent-detail-stack">
      <section className="card agent-detail-card" aria-labelledby="platform-bindings-title">
        <div className="agent-section-heading">
          <h3 id="platform-bindings-title" className="agent-detail-card-title">平台绑定</h3>
          <Button variant="secondary" size="compact" disabled={busy !== null || status === 'loading'}
            onClick={() => void refresh()}>刷新</Button>
        </div>

        {status === 'loading' && <p role="status">正在读取平台绑定…</p>}
        {refreshError && <p role="alert">平台绑定更新失败，当前显示上次读取的结果。</p>}
        {status === 'error' && (
          <Button variant="secondary" onClick={() => void reload()}>
            重试
          </Button>
        )}
        {status === 'ready' && rows.length === 0 && (
          <div className="agent-detail-stack">
            <p className="agent-detail-card-hint">暂无平台接入。</p>
            {onNavigateConnections && <Button variant="secondary" onClick={onNavigateConnections}>查看 Agent 设置</Button>}
          </div>
        )}
        {status === 'ready' && rows.length > 0 && (
          <ul className="platform-bindings">
            {rows.map(row => {
              const boundHere = row.boundAgent === agentActor;
              return (
                <li key={row.name} className="platform-binding-row">
                  <div>
                    <strong>{providerLabel(row.provider)} · {row.name}</strong>
                    <span className="agent-detail-card-hint">
                      {boundHere
                        ? `已绑定 · ${forwardingLabel(row)}`
                        : row.boundAgent === null
                          ? '未绑定'
                          : `已绑定 ${agentLabel(agentLabels, row.boundAgent)} · 到该 Agent 解除`}
                    </span>
                  </div>
                  <div className="platform-binding-actions">
                    {boundHere ? (
                      <Button
                        variant="secondary"
                        disabled={busy !== null}
                        onClick={() => void run(`unbind:${row.name}`, '解除绑定', 'agent.cell.update', () => api.unbind(row.name, agentActor))}
                      >
                        解除绑定
                      </Button>
                    ) : row.boundAgent === null ? (
                      <Button
                        variant="secondary"
                        disabled={busy !== null}
                        onClick={() => void run(`bind:${row.name}`, '绑定平台', 'agent.cell.update', () => api.bind(row.name, agentActor))}
                      >
                        绑定
                      </Button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {notice && (
          <p
            className="agent-form-message"
            role={notice.tone === 'error' ? 'alert' : 'status'}
          >
            {notice.text}
          </p>
        )}
      </section>

      {api.routes.available && api.routes.api && (
        <RouteSettings key={agentActor} name={status === 'ready' ? rows.find(row => row.boundAgent === agentActor)?.name : undefined}
          api={api.routes.api} />
      )}
    </div>
  );
}
