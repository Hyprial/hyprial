/**
 * One shared credential view consumes saved-entry metadata and grant observations
 * independently. Saving/replacing uses the existing Secret writer; revocation
 * targets the exact grant. Missing metadata remains unknown and deletion is
 * hidden when no endpoint exists. Native Auth keeps its independent reader.
 */
import { useEffect, useRef, useState, type FormEvent } from 'react';
import { CredentialList, CredentialListItem, NativeAuthList } from '../agent-provider';
import { parseSecretEntryReference, secretEntryReference } from './impl/secret-entries';
import { methodRefusal } from '../../client';
import { useNotifyUnavailable } from '../../components/notifications';
import {
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  LoadingState,
  ModuleSection,
  Notice,
} from '../../components/primitives';
import { SecretField } from '../../components/forms';
import {
  connectionLabel,
  type CredentialEntry,
  type CredentialGrant,
  type SharedCredentialWrite,
  type SharedCredentialsApi,
  type SharedGrantRow,
} from './shared-credentials-api';
import {
  CREDENTIAL_PROTOCOLS,
  CREDENTIALS_SECTION_COPY,
  type CredentialsSectionModel,
} from './credentials-view-model';

import { AddConnection } from './impl/model-connections/AddConnection';
import { ConnectionProbe } from './impl/model-connections/Probe';

type View =
  | { readonly state: 'loading' }
  | { readonly state: 'absent' }
  | { readonly state: 'error' }
  | { readonly state: 'ready' };

type ShownWrite = Exclude<SharedCredentialWrite, { readonly status: 'unavailable' }>;

interface AuthGroup {
  readonly key: string;
  readonly owner: string;
  readonly entryId: string;
  readonly grants: readonly SharedGrantRow[];
}

function groupAuth(grants: readonly SharedGrantRow[]): readonly AuthGroup[] {
  const groups = new Map<string, AuthGroup>();
  for (const grant of grants) {
    if (grant.source === 'user-provider') continue;
    const owner = grant.source === 'agent-private'
      ? grant.actor
      : grant.ownerActor ?? '未确认';
    const key = `${grant.source}\u0000${owner}\u0000${grant.entryId}\u0000${grant.fieldName ?? ''}`;
    const existing = groups.get(key);
    groups.set(key, {
      key,
      owner,
      entryId: grant.entryId,
      grants: [...(existing?.grants ?? []), grant],
    });
  }
  return [...groups.values()].sort((left, right) => (
    left.owner.localeCompare(right.owner) || left.entryId.localeCompare(right.entryId)
  ));
}

function failureView(error: unknown): View {
  return methodRefusal(error) ? { state: 'absent' } : { state: 'error' };
}

function writeTone(outcome: ShownWrite): 'info' | 'warning' | 'danger' {
  if (outcome.status === 'saved') return 'info';
  return outcome.status === 'failed' ? 'danger' : 'warning';
}

function writeText(outcome: ShownWrite): string {
  if (outcome.status === 'saved') return outcome.message ?? '操作已保存。';
  return outcome.message;
}

function nameOf(entry: CredentialEntry): string {
  return entry.label || `${entry.provider} · ${entry.keyId.slice(-6)}`;
}

function agentOf(grant: CredentialGrant): string {
  return grant.agentId ?? grant.agent;
}

function formatTime(value: number | null): string {
  if (value === null) return '—';
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}

export interface CredentialsSectionProps {
  readonly api?: SharedCredentialsApi;
  readonly onKeysChanged?: () => void;
  readonly creationOnly?: boolean;
  /** Fixture-only display model; live composition uses `api`. */
  readonly model?: CredentialsSectionModel;
}

export function CredentialsSection({ api, model, onKeysChanged, creationOnly = false }: CredentialsSectionProps) {
  const notifyUnavailable = useNotifyUnavailable();
  const generation = useRef(0);
  const [authRevision, setAuthRevision] = useState(0);
  const [view, setView] = useState<View>(
    api ? { state: 'loading' } : model ? { state: 'ready' } : { state: 'absent' },
  );
  const [entries, setEntries] = useState<readonly CredentialEntry[]>(model?.entries ?? []);
  const [grantView, setGrantView] = useState<View>(api ? { state: 'loading' } : { state: 'ready' });
  const [authGrants, setAuthGrants] = useState<readonly SharedGrantRow[]>(model?.authGrants ?? []);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [busy, setBusy] = useState<'refresh' | 'add' | 'rotate' | 'delete' | 'revoke' | null>(null);
  const [write, setWrite] = useState<ShownWrite | null>(null);
  const [locked, setLocked] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<CredentialEntry | null>(null);
  const [rotateTarget, setRotateTarget] = useState<CredentialEntry | null>(null);
  const [rotateValue, setRotateValue] = useState('');
  const [probeTarget,setProbeTarget]=useState<CredentialEntry|null>(null);
  const [rotateTesting, setRotateTesting] = useState(false);
  const rotateKeyRef = useRef<HTMLInputElement>(null);
  const rotateFormRef = useRef<HTMLFormElement>(null);

  const canTest = (provider: string) => Boolean(api?.testKey && (!api.testProviders || api.testProviders.includes(provider)));

  function applyKeys(keys: readonly CredentialEntry[]): void {
    setEntries(keys);
    setView({ state: 'ready' });
  }

  function load(nextApi: SharedCredentialsApi, current: number): void {
    setView({ state: 'loading' });
    void nextApi.listKeys()
      .then(snapshot => {
        if (generation.current !== current) return;
        applyKeys(snapshot);
      })
      .catch((error: unknown) => {
        if (generation.current !== current) return;
        setView(failureView(error));
      });
  }

  useEffect(() => {
    const current = ++generation.current;
    setWrite(null);
    setExpanded(null);
    setFormOpen(false);
    setLocked(false);
    setDeleteTarget(null);
    setRotateTarget(null);
    setProbeTarget(null);
    setRotateValue('');
    setRotateTesting(false);
    if (!api) {
      setEntries(model?.entries ?? []);
      setAuthGrants(model?.authGrants ?? []);
      setView(model ? { state: 'ready' } : { state: 'absent' });
      return;
    }
    load(api, current);
    return () => {
      generation.current += 1;
    };
  }, [api, model]);

  useEffect(() => {
    let disposed = false;
    setAuthGrants(model?.authGrants ?? []);
    setGrantView(api ? { state: 'loading' } : { state: 'ready' });
    if (api && !creationOnly) void api.listSecretGrants().then(grants => {
      if (disposed) return;
      setAuthGrants(grants);
      setGrantView({ state: 'ready' });
    }).catch(error => {
      if (!disposed) setGrantView(failureView(error));
    });
    return () => { disposed = true; };
  }, [api, model, authRevision, creationOnly]);

  async function reload(): Promise<void> {
    if (!api) return;
    const current=generation.current;
    const snapshot = await api.listKeys();
    if(current!==generation.current)return;
    applyKeys(snapshot);
    setAuthRevision(value => value + 1);
    onKeysChanged?.();
  }

  async function refresh(): Promise<void> {
    if (!api || busy) return;
    const previous = view;
    const current = ++generation.current;
    setBusy('refresh');
    setView({ state: 'loading' });
    try {
      const snapshot = await api.listKeys();
      if (generation.current !== current) return;
      setWrite(null);
      setLocked(false);
      setAuthRevision(value => value + 1);
      applyKeys(snapshot);
      onKeysChanged?.();
    } catch (error) {
      if (generation.current !== current) return;
      const refusal = methodRefusal(error);
      if (refusal) {
        setView(previous);
        notifyUnavailable(CREDENTIALS_SECTION_COPY.refresh, refusal);
      } else {
        setView({ state: 'error' });
      }
    } finally {
      if (generation.current === current) setBusy(null);
    }
  }

  function permitted(action: 'add' | 'replace' | 'delete' | 'revoke', subject: string, entry?: CredentialEntry): boolean {
    const refusal = api?.refusal?.(action, entry) ?? null;
    if (!refusal) return true;
    notifyUnavailable(subject, refusal);
    return false;
  }

  function openForm(): void {
    if (!permitted('add', CREDENTIALS_SECTION_COPY.add)) return;
    if (!api?.configureKey) { notifyUnavailable(CREDENTIALS_SECTION_COPY.add, { code: 'GUI_CAPABILITY_UNAVAILABLE', source: 'agent.secret.provider-configure' }); return; }
    setWrite(null);
    setFormOpen(true);
  }

  function closeForm(): void { setFormOpen(false); }
  async function connectionOutcome(outcome: SharedCredentialWrite): Promise<void> {
    if(outcome.status==='unavailable')return;
    const current = generation.current;
    setWrite(outcome);
    if(outcome.status==='saved'){
      closeForm();
      setBusy('add');
      try { await reload(); }
      catch {
        if (generation.current !== current) return;
        setWrite({status:'unknown',message:'连接已保存，但清单回读失败；请刷新核对后再操作。'});
        setLocked(true);
      } finally {
        if (generation.current === current) setBusy(null);
      }
    }
    if(outcome.status==='unknown'){setLocked(true);closeForm();}
  }

  function openRotate(entry: CredentialEntry): void {
    if (!permitted('replace', CREDENTIALS_SECTION_COPY.replaceKey, entry)) return;
    if (entry.connection && !api?.replaceConnection) { notifyUnavailable(CREDENTIALS_SECTION_COPY.replaceKey, { code: 'GUI_CAPABILITY_UNAVAILABLE', source: 'agent.secret.provider-configure' }); return; }
    setRotateTarget(entry);
    setRotateValue('');
    setRotateTesting(false);
  }

  function closeRotate(): void {
    setRotateTarget(null);
    setRotateValue('');
    setRotateTesting(false);
  }

  async function saveReplacement(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!api || busy || locked || rotateTesting || !rotateTarget || (rotateTarget.connection && !api.replaceConnection)) return;
    const current=generation.current;
    const target = rotateTarget;
    const value = rotateValue;
    setBusy('rotate');
    try {
      const outcome = target.connection
        ? await api.replaceConnection!(target,value)
        : await api.replaceKey(target.keyId, value);
      if(generation.current!==current)return;
      if (outcome.status === 'unavailable') {
        notifyUnavailable(CREDENTIALS_SECTION_COPY.save, outcome.refusal);
        return;
      }
      setWrite(outcome);
      closeRotate();
      if (outcome.status === 'saved') {
        await reload();
      } else if (outcome.status === 'unknown') {
        setLocked(true);
      }
    } catch {
      if (generation.current !== current) return;
      setWrite({ status: 'unknown', message: '操作结果未知，已暂停重复提交。请刷新凭据状态后再试。' });
      setLocked(true);
      closeRotate();
    } finally {
      if (generation.current === current) setBusy(null);
    }
  }

  async function confirmDelete(): Promise<void> {
    if (!api?.deleteKey || busy || locked || !deleteTarget) return;
    const target = deleteTarget;
    setBusy('delete');
    try {
      const outcome = await api.deleteKey(target.keyId);
      if (outcome.status === 'unavailable') {
        notifyUnavailable(CREDENTIALS_SECTION_COPY.deleteConfirm, outcome.refusal);
        return;
      }
      setDeleteTarget(null);
      setWrite(outcome);
      if (outcome.status === 'saved') {
        await reload();
      } else if (outcome.status === 'unknown') {
        setLocked(true);
      }
    } catch {
      setDeleteTarget(null);
      setWrite({ status: 'unknown', message: '操作结果未知，已暂停重复提交。请刷新凭据状态后再试。' });
      setLocked(true);
    } finally {
      setBusy(null);
    }
  }

  async function revokeGrant(grant: CredentialGrant): Promise<void> {
    if (!api || busy || locked) return;
    if (!permitted('revoke', CREDENTIALS_SECTION_COPY.revokeGrant)) return;
    setBusy('revoke');
    try {
      const outcome = await api.revokeGrant(grant.agent, grant.grantId);
      if (outcome.status === 'unavailable') {
        notifyUnavailable(CREDENTIALS_SECTION_COPY.revokeGrant, outcome.refusal);
        return;
      }
      setWrite(outcome);
      if (outcome.status === 'saved') {
        await reload();
      } else if (outcome.status === 'unknown') {
        setLocked(true);
      }
    } catch {
      setWrite({ status: 'unknown', message: '操作结果未知，已暂停重复提交。请刷新凭据状态后再试。' });
      setLocked(true);
    } finally {
      setBusy(null);
    }
  }

  const authGroups = grantView.state === 'ready' ? groupAuth(authGrants) : [];
  const visibleEntries = view.state === 'ready' ? entries.map(entry => entry.grantsKnown !== false ? entry : ({
    ...entry, grantsKnown: grantView.state === 'ready',
    grants: grantView.state !== 'ready' ? [] : authGrants.filter(grant => grant.source === 'user-provider'
      && grant.fieldName !== null && secretEntryReference(grant.entryId, grant.fieldName) === entry.keyId)
      .map(grant => ({ agent: grant.actor, agentId: null, grantId: grant.grantId, provider: entry.provider, state: 'active' })),
  })) : [];
  const writesDisabled = !api || view.state !== 'ready' || busy !== null || locked;
  const total = visibleEntries.length + authGroups.length;
  const writeNotice = write && (
    <Notice tone={writeTone(write)} title={write.status === 'saved' ? '已保存' : write.status === 'failed' ? '操作未完成' : '未确认'}>
      {writeText(write)}
    </Notice>
  );

  if (view.state === 'absent') return null;

  return (
    <ModuleSection
      id="credentials-title"
      className="credentials-section"
      title={CREDENTIALS_SECTION_COPY.title}
      actions={api ? (
        <>
          <Button variant="secondary" disabled={busy !== null || rotateTesting} onClick={() => void refresh()}>
            {CREDENTIALS_SECTION_COPY.refresh}
          </Button>
          <Button
            variant="primary"
            disabled={writesDisabled || formOpen}
            onClick={openForm}
          >
            {CREDENTIALS_SECTION_COPY.add}
          </Button>
        </>
      ) : undefined}
    >
      {formOpen && api && <AddConnection api={api} disabled={writesDisabled}
        onClose={closeForm} onOutcome={connectionOutcome} />}

      {!formOpen && writeNotice}

      {view.state === 'loading' && <LoadingState title="正在读取凭据" />}
      {view.state === 'error' && <ErrorState message="凭据读取失败，请刷新后重试。" />}
      {!creationOnly && <>
      {grantView.state === 'loading' && <LoadingState title="正在读取服务密钥授权" />}
      {grantView.state === 'error' && <ErrorState message="服务密钥授权读取失败，请刷新后重试。" />}
      {view.state === 'ready' && total === 0 && <EmptyState title={CREDENTIALS_SECTION_COPY.empty} />}
      {total > 0 && (
        <CredentialList label="凭据列表">
          {visibleEntries.map(entry => {
            const key = `key-${entry.keyId}`;
            const open = expanded === key;
            return (
              <CredentialListItem key={key} title={nameOf(entry)}
                summary={<>{CREDENTIALS_SECTION_COPY.keyBadge} · {connectionLabel(entry)} · {entry.grantsKnown === false ? '授权状态未确认' : `${entry.grants.length} 条授权`}</>}
                expanded={open} onExpandedChange={value => setExpanded(value ? key : null)}>
                    <dl className="credentials-facts">
                      <div>
                        <dt>{entry.connection ? '连接类型' : '旧 Key 类型'}</dt>
                        <dd>{connectionLabel(entry)}</dd>
                      </div>
                      {entry.endpoint && <div><dt>服务地址</dt><dd>{entry.endpoint}</dd></div>}
                      {entry.protocol && <div><dt>兼容协议</dt><dd>{CREDENTIAL_PROTOCOLS.find(item => item.value === entry.protocol)?.label}</dd></div>}
                      <div>
                        <dt>状态</dt>
                        <dd>{entry.state === 'active' ? '已保存' : entry.state === 'deleted' ? '已删除' : '状态未确认'}</dd>
                      </div>
                      <div>
                        <dt>创建时间</dt>
                        <dd>{formatTime(entry.createdAt)}</dd>
                      </div>
                      <div>
                        <dt>最近使用</dt>
                        <dd>{formatTime(entry.lastUsedAt)}</dd>
                      </div>
                    </dl>
                    <p className="credentials-row__meta">已授权 Agent</p>
                    {entry.grantsKnown === false ? <p className="credentials-row__meta">授权状态未确认。</p> : entry.grants.length === 0 ? (
                      <p className="credentials-row__meta">尚未授权给任何 Agent。</p>
                    ) : (
                      <ul className="credentials-grant-list" aria-label="授权记录">
                        {entry.grants.map(grant => (
                          <li key={`${grant.grantId}:${grant.agent}`}>
                            <span>{agentOf(grant)}</span>
                            {api && (
                              <Button
                                variant="secondary"
                                size="sm"
                                disabled={busy !== null || locked}
                                aria-label={`撤销 ${agentOf(grant)} 的授权`}
                                onClick={() => void revokeGrant(grant)}
                              >
                                {CREDENTIALS_SECTION_COPY.revokeGrant}
                              </Button>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                    {api && (
                      <div className="credentials-actions">
                        {entry.connection && api.testKey && <Button variant="secondary" disabled={writesDisabled}
                          onClick={()=>setProbeTarget(entry)}>测试已保存连接</Button>}
                        <Button
                          variant="secondary"
                          disabled={busy !== null || locked}
                          onClick={() => openRotate(entry)}
                        >
                          {CREDENTIALS_SECTION_COPY.replaceKey}
                        </Button>
                        {api.deleteKey && <Button
                          variant="danger"
                          disabled={busy !== null || locked}
                          onClick={() => {
                            if (permitted('delete', CREDENTIALS_SECTION_COPY.deleteCredential)) setDeleteTarget(entry);
                          }}
                        >
                          {CREDENTIALS_SECTION_COPY.deleteCredential}
                        </Button>}
                      </div>
                    )}
              </CredentialListItem>
            );
          })}
          {authGroups.map(group => {
            const key = `auth-${group.key}`;
            const open = expanded === key;
            return (
              <CredentialListItem key={key} title={group.entryId}
                summary={<>{CREDENTIALS_SECTION_COPY.authBadge} · 所有者 {group.owner} · {group.grants.length} 个 Agent 已授权</>}
                expanded={open} onExpandedChange={value => setExpanded(value ? key : null)}>
                    <dl className="credentials-facts">
                      <div>
                        <dt>所有者</dt>
                        <dd>{group.owner}</dd>
                      </div>
                    </dl>
                    <p className="credentials-row__meta">使用该凭据的 Agent（只读）</p>
                    <ul className="credentials-grant-list" aria-label="授权记录">
                      {group.grants.map(grant => (
                        <li key={`${grant.grantId}:${grant.actor}`}>
                          <span>{grant.actor}</span>
                        </li>
                      ))}
                    </ul>
                    <p className="credentials-row__meta">{CREDENTIALS_SECTION_COPY.authManaged}</p>
              </CredentialListItem>
            );
          })}
        </CredentialList>
      )}

      <NativeAuthList heading="Auth" api={api?.nativeAuth} revision={authRevision} />
      </>}

      <Dialog
        open={rotateTarget !== null}
        title={CREDENTIALS_SECTION_COPY.replaceTitle}
        description={CREDENTIALS_SECTION_COPY.replaceDescription}
        initialFocusRef={rotateKeyRef}
        onClose={() => {
          if (busy !== 'rotate') closeRotate();
        }}
        actions={[
          {
            label: CREDENTIALS_SECTION_COPY.cancel,
            variant: 'secondary',
            disabled: busy === 'rotate',
            onClick: closeRotate,
          },
          {
            label: CREDENTIALS_SECTION_COPY.save,
            variant: 'primary',
            loading: busy === 'rotate',
            disabled: writesDisabled || rotateTesting || !rotateValue,
            onClick: () => rotateFormRef.current?.requestSubmit(),
          },
        ]}
      >
        {rotateTarget && (
          <form className="credentials-form" ref={rotateFormRef} onSubmit={(event) => void saveReplacement(event)}>
            <p className="credentials-row__meta">
              {nameOf(rotateTarget)} · {connectionLabel(rotateTarget)}
            </p>
            <SecretField
              layout="inline"
              id="credentials-rotate-key"
              label="新 Key"
              value={rotateValue}
              onChange={setRotateValue}
              inputRef={rotateKeyRef}
              disabled={writesDisabled || rotateTesting}
            />
            {api && canTest(rotateTarget.provider) && <ConnectionProbe api={api} id="rotate-model"
              identity={`${rotateTarget.keyId}\u0000${rotateValue}`} disabled={writesDisabled || !rotateValue}
              onTestingChange={setRotateTesting} request={model=>({provider:rotateTarget.connection ? 'custom' : rotateTarget.provider,
                secret:rotateValue,model,...(rotateTarget.endpoint ? {endpoint:rotateTarget.endpoint,protocol:rotateTarget.protocol} : {})})} />}
            <p className="credentials-row__meta">{rotateTarget.connection ? '更换会更新连接版本。请重新配置使用此连接的 Agent；已运行的 Agent 不会自动切换。' : CREDENTIALS_SECTION_COPY.replaceHint}</p>
          </form>
        )}
      </Dialog>

      {probeTarget && api && <Dialog open title="测试已保存连接" onClose={()=>setProbeTarget(null)}>
        <p>{nameOf(probeTarget)}</p><p className="credentials-row__meta">{probeTarget.endpoint}</p>
        <ConnectionProbe api={api} id="saved-model" identity={probeTarget.keyId} request={model=>{
          const target=parseSecretEntryReference(probeTarget.keyId);
          if(!target || !probeTarget.connection)throw new Error('Connection reference is invalid');
          return {connection:{...target,connectionRef:probeTarget.connection.connectionRef,configRevision:probeTarget.connection.configRevision},model};
        }} />
      </Dialog>}

      <Dialog
        open={deleteTarget !== null}
        title={CREDENTIALS_SECTION_COPY.deleteConfirmTitle}
        description={CREDENTIALS_SECTION_COPY.deleteConfirmDescription}
        onClose={() => {
          if (busy !== 'delete') setDeleteTarget(null);
        }}
        actions={[
          {
            label: CREDENTIALS_SECTION_COPY.cancel,
            variant: 'secondary',
            onClick: () => setDeleteTarget(null),
          },
          {
            label: CREDENTIALS_SECTION_COPY.deleteConfirm,
            variant: 'danger',
            loading: busy === 'delete',
            onClick: () => void confirmDelete(),
          },
        ]}
      >
        {deleteTarget && <p>{nameOf(deleteTarget)}</p>}
      </Dialog>
    </ModuleSection>
  );
}
