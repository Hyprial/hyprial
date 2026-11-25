/**
 * Native Auth metadata comes from agent.provider.auth.list independently from Key grants.
 * Missing provenance remains null; file timestamps are not import or launch evidence.
 * Startup evidence is not vendor authentication. Removing files never stops an Agent or removes Key grants.
 * 2026-11-14: beforeRemove runs before the confirmation opens; a caller that already knows the
 * removal is refused notifies and returns false, so no dialog opens and nothing is re-read.
 * A read the catalog refuses before dispatch means the host has no Auth reader: the list (and its
 * optional heading) is not rendered, never shown as a failure or as an empty inventory.
 * 2026-11-15 (integration): headingLevel lets the Agent credentials tab keep its h4 beside the Key
 * heading while the heading still disappears together with a list that has no reader.
 */
import { useEffect, useRef, useState } from 'react';
import { methodRefusal } from '../../../client';
import { Badge } from '../../../components/Badge';
import { Button, Dialog, EmptyState } from '../../../components/primitives';
import { CredentialList, CredentialListItem } from './credential-list';

export interface NativeAuthRecord {
  readonly actor: string;
  readonly displayName: string;
  readonly harness: string;
  readonly files: readonly string[];
  readonly state: 'stored' | 'partial' | 'unavailable';
  readonly missingFiles: readonly string[];
  readonly errorCode: string | null;
  readonly importedAt: number | null;
  readonly startupConfirmedAt: number | null;
}

export interface NativeAuthApi {
  list(actor?: string): Promise<readonly NativeAuthRecord[]>;
}

interface Props {
  readonly api?: NativeAuthApi;
  readonly actor?: string;
  readonly heading?: string;
  readonly headingLevel?: 3 | 4;
  readonly revision?: number;
  readonly disabled?: boolean;
  readonly onRemove?: (record: NativeAuthRecord) => Promise<void>;
  readonly beforeRemove?: (record: NativeAuthRecord) => boolean;
}

type ReadState =
  | { readonly status: 'loading' }
  | { readonly status: 'failed' }
  | { readonly status: 'absent' }
  | { readonly status: 'ready'; readonly rows: readonly NativeAuthRecord[] };

function sameRecord(left: NativeAuthRecord, right: NativeAuthRecord): boolean {
  return left.actor === right.actor && left.harness === right.harness;
}

export function NativeAuthList({ api, actor, heading, headingLevel = 3, revision = 0, disabled = false, onRemove, beforeRemove }: Props) {
  const epoch = useRef(0);
  const removing = useRef(false);
  const lockedRecord = useRef<NativeAuthRecord | null>(null);
  const [read, setRead] = useState<ReadState>({ status: 'loading' });
  const [reload, setReload] = useState(0);
  const [target, setTarget] = useState<NativeAuthRecord | null>(null);
  const [busy, setBusy] = useState(false);
  const [locked, setLocked] = useState<NativeAuthRecord | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setTarget(null);
    setLocked(null);
    lockedRecord.current = null;
    setError(null);
    setBusy(false);
    removing.current = false;
  }, [api, actor]);

  useEffect(() => {
    const current = ++epoch.current;
    setRead({ status: 'loading' });
    if (api) void api.list(actor).then(rows => {
      if (epoch.current !== current) return;
      setRead({ status: 'ready', rows });
      const previous = lockedRecord.current;
      if (previous && !rows.some(row => sameRecord(row, previous))) {
        lockedRecord.current = null;
        setLocked(null);
        setTarget(null);
        setError(null);
      }
    }).catch((error: unknown) => {
      if (epoch.current === current) setRead({ status: methodRefusal(error) ? 'absent' : 'failed' });
    });
    return () => { epoch.current += 1; };
  }, [api, actor, revision, reload]);

  async function remove(): Promise<void> {
    if (!target || !onRemove || removing.current || locked || disabled) return;
    const current = epoch.current;
    removing.current = true;
    setBusy(true);
    setError(null);
    try {
      await onRemove(target);
      if (epoch.current !== current) return;
      setTarget(null);
      setReload(value => value + 1);
    } catch (failure) {
      if (epoch.current !== current) return;
      const notDispatched = failure !== null && typeof failure === 'object'
        && 'dispatch' in failure && failure.dispatch === 'not-dispatched';
      if (!notDispatched) {
        lockedRecord.current = target;
        setLocked(target);
      }
      setError(notDispatched ? '移除未提交，请重试。' : '移除结果待确认，请重新读取。');
    } finally {
      if (epoch.current === current) {
        removing.current = false;
        setBusy(false);
      }
    }
  }

  if (!api || read.status === 'absent') return null;
  return (
    <div className="native-auth-list">
      {heading && (headingLevel === 4 ? <h4>{heading}</h4> : <h3>{heading}</h3>)}
      <Button variant="secondary" size="compact" disabled={busy || read.status === 'loading'} onClick={() => setReload(value => value + 1)}>刷新 Auth</Button>
      {read.status === 'loading' && <p role="status">正在读取 Auth…</p>}
      {read.status === 'failed' && <p role="alert">Auth 读取失败。</p>}
      {error && <p role="alert">{error}</p>}
      {(read.status === 'failed' || locked) && (
        <Button variant="secondary" size="compact" disabled={busy || read.status === 'loading'} onClick={() => setReload(value => value + 1)}>重新读取</Button>
      )}
      {read.status === 'ready' && (read.rows.length === 0 ? <EmptyState title="暂无 Auth" /> : (
        <CredentialList label="Auth 清单">
          {read.rows.map(row => (
            <CredentialListItem key={`${row.actor}:${row.harness}`} title={actor ? row.harness : row.displayName}
              summary={<>Auth · {row.harness} · <Badge tone={row.state === 'stored' ? 'neutral' : 'warning'}>{row.state === 'stored' ? '已保存' : row.state === 'partial' ? '文件不完整' : '读取失败'}</Badge></>}
              defaultExpanded={Boolean(actor)}>
              <dl className="credentials-facts">
                <div><dt>harness</dt><dd>{row.harness}</dd></div>
                <div><dt>登录文件</dt><dd>{row.files.map(file => <div key={file}>{file}</div>)}</dd></div>
                {row.missingFiles.length > 0 && <div><dt>缺少文件</dt><dd>{row.missingFiles.join('、')}</dd></div>}
                {row.errorCode && <div><dt>错误码</dt><dd>{row.errorCode}</dd></div>}
                {row.importedAt !== null && <div><dt>导入时间</dt><dd>{new Date(row.importedAt).toLocaleString('zh-CN', { hour12: false })}</dd></div>}
                {row.startupConfirmedAt !== null ? <div><dt>本版本成功回复</dt><dd>{(
                  <time dateTime={new Date(row.startupConfirmedAt).toISOString()}>{new Date(row.startupConfirmedAt).toLocaleString('zh-CN', { hour12: false })}</time>
                )}</dd></div> : <div><dt>本版本验证</dt><dd>暂无本版本成功回复记录</dd></div>}
              </dl>
              {onRemove && <div><Button variant="danger" size="compact" disabled={disabled || busy || locked !== null} onClick={() => { if (beforeRemove && !beforeRemove(row)) return; setTarget(row); setError(null); }}>移除 Auth</Button></div>}
            </CredentialListItem>
          ))}
        </CredentialList>
      ))}
      <Dialog open={target !== null} title="移除 Auth" onClose={() => { if (!busy) setTarget(null); }} actions={[
        { label: '取消', variant: 'secondary', disabled: busy, onClick: () => setTarget(null) },
        { label: '移除', variant: 'danger', disabled: disabled || busy || locked !== null, loading: busy, onClick: () => void remove() },
      ]}>
        <p>移除 {target?.displayName} 的 {target?.harness} 登录文件？</p>
        <p>运行中的 Agent 不会停止。</p>
      </Dialog>
    </div>
  );
}
