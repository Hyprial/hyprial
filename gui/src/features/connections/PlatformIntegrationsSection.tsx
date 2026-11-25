/**
 * Decision record 2026-11-10 (平台接入).
 *
 * - 本模块只管理 adapter 生命周期：添加/授权/启动/停止/身份同步/删除。
 * - Agent 绑定（cell 文档 adapter 成员）和 route 选择属于 Agent 配置，不在本页。
 * - 调用面使用 provider-neutral API；UI 不出现 `lark.*` 方法名。
 * - daemon 尚缺 register/onboard/authorization/identities/remove 时按能力 fail-close，
 *   不伪造成功；不可用不在页面上写说明（决策 PR 1f/F4）：
 *   端口上不存在的方法不渲染对应控件（没有 register 与 onboard 就没有添加入口，
 *   没有 removePreview/remove 就没有删除）；
 *   列表被目录拒绝（未派发）等于没有端口，整个模块不渲染，不显示为空；
 *   用户触发的刷新被拒绝只发通知，保留原有数据；
 *   写操作被拒绝时控件保持可用，在用户操作时发一次通知，不留下窗口、对话框或忙碌状态；
 *   已知被拒绝的添加在打开对话框之前判断；通知主题就是被操作控件的可见文案。
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react';
import {
  Button,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  ModuleSection,
  Notice,
  Select,
} from '../../components/primitives';
import { SecretField } from '../../components/forms';
import { ClientError, methodRefusal, type MethodRefusal } from '../../client';
import { useNotifyUnavailable } from '../../components/notifications';
import {
  PLATFORM_PROVIDERS,
  capabilityRefusal,
  type AdapterOperation,
  type AddPlatformInput,
  type PlatformIntegration,
  type PlatformIntegrationsApi,
  type PlatformProvider,
  type RemovePreview,
} from './platform-integrations-api';
import {
  active,
  groupByProvider,
  providerLabel,
} from './platform-integrations-view';
import { PlatformIntegrationDetail } from './PlatformIntegrationDetail';
import { OperationProgress } from './impl/platform/OperationProgress';

type View =
  | { readonly state: 'loading' }
  | { readonly state: 'absent' }
  | { readonly state: 'error' }
  | { readonly state: 'ready' };

interface NoticeState {
  readonly tone: 'info' | 'warning' | 'danger';
  readonly text: string;
}

const COPY = {
  title: '平台接入',
  description: '平台接入、授权与运行状态。',
  add: '添加接入',
  refresh: '刷新',
  reload: '重新加载全部配置',
  empty: '暂无平台接入',
  addTitle: '添加平台接入',
  platform: '平台',
  name: '名称',
  mode: '添加方式',
  createMode: '创建新应用',
  adoptMode: '使用已有应用',
  appId: 'App ID',
  appSecret: 'App Secret',
  addCancel: '取消',
  addSubmit: '开始添加',
  unknownRegistration: '添加结果未知，请关闭后重新加载配置并查看列表。',
  removeTitle: '删除平台接入',
  removeDescription: '请核对本次删除范围；运行中的平台接入需要先停止。',
  removeConfirm: '确认删除',
} as const;

export interface PlatformIntegrationsSectionProps {
  readonly api?: PlatformIntegrationsApi;
}

type ReloadOutcome = { readonly ok: true } | { readonly ok: false; readonly refusal: MethodRefusal | null };

const VALID_NAME = /^[a-z0-9][a-z0-9._-]*$/;

export function PlatformIntegrationsSection({ api }: PlatformIntegrationsSectionProps) {
  const notifyUnavailable = useNotifyUnavailable();
  const generation = useRef(0);
  const [view, setView] = useState<View>(api ? { state: 'loading' } : { state: 'absent' });
  const [rows, setRows] = useState<readonly PlatformIntegration[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [notice, setNotice] = useState<NoticeState | null>(null);
  const [authorizationOperations, setAuthorizationOperations] = useState<Readonly<Record<string, AdapterOperation>>>({});
  const [authorizationRead, setAuthorizationRead] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [addReadPending, setAddReadPending] = useState(false);
  const [addReadFailed, setAddReadFailed] = useState(false);
  const [addReadAttempt, setAddReadAttempt] = useState(0);
  const [addPlatform, setAddPlatform] = useState<PlatformProvider>('lark');
  const [addMode, setAddMode] = useState<'create' | 'adopt'>('create');
  const [addName, setAddName] = useState('');
  const [addAppId, setAddAppId] = useState('');
  const [addSecret, setAddSecret] = useState('');
  const [addOperation, setAddOperation] = useState<AdapterOperation | null>(null);
  const [addError, setAddError] = useState<string | null>(null);
  const [registrationUnknown, setRegistrationUnknown] = useState(false);
  const [pendingReload, setPendingReload] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<PlatformIntegration | null>(null);
  const [removePreview, setRemovePreview] = useState<RemovePreview | null>(null);
  const [removeSubmitted, setRemoveSubmitted] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);
  const groups = useMemo(() => groupByProvider(rows), [rows]);

  useEffect(() => {
    if (!api?.currentAdd || !addOpen || addMode !== 'create' || !VALID_NAME.test(addName.trim())) {
      setAddReadPending(false);
      setAddReadFailed(false);
      return;
    }
    let cancelled = false;
    setAddReadPending(true);
    setAddReadFailed(false);
    setAddError(null);
    const current = generation.current;
    const timer = setTimeout(() => {
      void api.currentAdd!(addName.trim()).then(operation => {
        if (cancelled) return;
        setAddOperation(operation);
        setAddReadPending(false);
        if (operation?.status === 'succeeded') void finishAdd(operation, current);
      }).catch((error: unknown) => {
        if (cancelled) return;
        setAddReadPending(false);
        if (methodRefusal(error)) return;
        setAddReadFailed(true);
        setAddError('添加记录读取失败，请重试。');
      });
    }, 200);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [api, addOpen, addMode, addName, addReadAttempt]);

  useEffect(() => {
    if (!api?.currentAuthorization || !expanded) return;
    const name = expanded;
    let cancelled = false;
    setAuthorizationRead(name);
    void api.currentAuthorization(name).then(operation => {
      if (cancelled) return;
      setAuthorizationOperations(current => {
        const next = { ...current };
        if (operation) next[name] = operation;
        else delete next[name];
        return next;
      });
      setAuthorizationRead(null);
    }).catch((error: unknown) => {
      if (cancelled) return;
      if (methodRefusal(error)) setAuthorizationRead(null);
      else setNotice({ tone: 'warning', text: '授权记录读取失败，请收起后重试。' });
    });
    return () => { cancelled = true; };
  }, [api, expanded]);

  function applyRows(next: readonly PlatformIntegration[]): void {
    setRows(next);
    setView({ state: 'ready' });
  }

  async function reload(current = ++generation.current): Promise<ReloadOutcome> {
    if (!api) return { ok: false, refusal: null };
    try {
      const next = await api.list();
      if (generation.current !== current) return { ok: false, refusal: null };
      applyRows(next);
      return { ok: true };
    } catch (error) {
      const refusal = methodRefusal(error);
      if (generation.current === current && !refusal) setView({ state: 'error' });
      return { ok: false, refusal };
    }
  }

  useEffect(() => {
    const current = ++generation.current;
    setBusy(null);
    setNotice(null);
    setAuthorizationOperations({});
    setAddOpen(false);
    setAddReadFailed(false);
    setAddOperation(null);
    setAddError(null);
    setRegistrationUnknown(false);
    setPendingReload(false);
    setAddSecret('');
    setRemoveTarget(null);
    setRemovePreview(null);
    setExpanded(null);
    if (!api) {
      setRows([]);
      setView({ state: 'absent' });
      return;
    }
    setView({ state: 'loading' });
    void reload(current).then(result => {
      if (!result.ok && result.refusal && generation.current === current) setView({ state: 'absent' });
    });
    return () => { generation.current += 1; };
  }, [api]);

  async function refresh(): Promise<void> {
    if (!api || busy) return;
    const previous = view;
    setBusy('refresh');
    setView({ state: 'loading' });
    try {
      const result = await reload(++generation.current);
      if (!result.ok && result.refusal) {
        setView(previous);
        notifyUnavailable(COPY.refresh, result.refusal);
      } else setNotice(null);
    } finally {
      setBusy(null);
    }
  }

  async function reloadConfig(): Promise<void> {
    if (!api || busy) return;
    const current = generation.current;
    setBusy('reload-config');
    try {
      await api.reload();
      if (generation.current !== current) return;
      const readBack = await reload(current);
      if (generation.current !== current) return;
      if (!readBack.ok) {
        setNotice({ tone: 'warning', text: '配置已重新加载，列表读取失败，请刷新。' });
        return;
      }
      setPendingReload(false);
      setRegistrationUnknown(false);
      setNotice({ tone: 'info', text: '已重新加载接入配置。' });
    } catch (error) {
      if (generation.current !== current) return;
      const refusal = methodRefusal(error);
      if (refusal) {
        notifyUnavailable(COPY.reload, refusal);
        return;
      }
      setNotice({ tone: 'warning', text: '重新加载结果尚未确认，请刷新后重试。' });
    } finally {
      if (generation.current === current) setBusy(null);
    }
  }

  async function startStop(row: PlatformIntegration): Promise<void> {
    if (!api || busy) return;
    const action = row.processRunning || row.desired ? 'stop' : 'start';
    setBusy(`${action}:${row.name}`);
    try {
      if (action === 'start') await api.start(row.name);
      else await api.stop(row.name);
      await reload();
      setNotice({ tone: 'info', text: action === 'start' ? '已启动。' : '已停止。' });
    } catch (error) {
      const refusal = methodRefusal(error);
      if (refusal) {
        notifyUnavailable(action === 'start' ? '启动' : '停止', refusal);
        return;
      }
      setNotice({ tone: 'warning', text: '操作结果尚未确认，请刷新平台接入状态。' });
      await reload();
    } finally {
      setBusy(null);
    }
  }

  async function beginAuthorization(row: PlatformIntegration, label: string): Promise<void> {
    if (!api?.beginAuthorization || busy || authorizationRead === row.name || active(authorizationOperations[row.name])) return;
    const current = generation.current;
    setBusy(`authorize:${row.name}`);
    try {
      const operation = await api.beginAuthorization(row.name);
      if (current !== generation.current) return;
      setAuthorizationOperations(current => ({ ...current, [row.name]: operation }));
      setNotice(null);
      if (operation.status === 'succeeded') {
        await reload(current);
      }
    } catch (error) {
      if (current !== generation.current) return;
      const refusal = methodRefusal(error);
      if (refusal) {
        notifyUnavailable(label, refusal);
        return;
      }
      if (!(error instanceof ClientError && error.dispatch === 'not-dispatched')) {
        setAuthorizationOperations(previous => ({ ...previous, [row.name]: {
          operationId: '', adapter: row.name, provider: row.provider, kind: 'authorization', status: 'unknown',
        } }));
      }
      setNotice({ tone: 'warning', text: '授权结果未确认，请刷新授权状态。' });
    } finally {
      if (current === generation.current) setBusy(null);
    }
  }

  async function pollAuthorization(row: PlatformIntegration): Promise<void> {
    const operation = authorizationOperations[row.name];
    if (!api?.authorizationOperation || !operation || busy) return;
    const current = generation.current;
    setBusy(`authorize-poll:${row.name}`);
    try {
      const next = operation.operationId ? await api.authorizationOperation(operation.operationId)
        : await api.currentAuthorization?.(row.name);
      if (current !== generation.current) return;
      setNotice(null);
      if (next) setAuthorizationOperations(previous => ({ ...previous, [row.name]: next }));
      else if (next === null) setAuthorizationOperations(previous => {
        const updated = { ...previous };
        delete updated[row.name];
        return updated;
      });
      if (next?.status === 'succeeded') await reload(current);
    } catch (error) {
      if (current !== generation.current) return;
      const refusal = methodRefusal(error);
      if (refusal) notifyUnavailable(`刷新授权状态`, refusal);
      else setNotice({ tone: 'warning', text: '授权记录读取失败。' });
    } finally {
      if (current === generation.current) setBusy(null);
    }
  }

  async function cancelAuthorization(row: PlatformIntegration): Promise<void> {
    const operation = authorizationOperations[row.name];
    if (!api?.cancelAuthorization || !operation?.operationId || busy) return;
    const current = generation.current;
    setBusy(`authorize-cancel:${row.name}`);
    try {
      const next = await api.cancelAuthorization(operation.operationId);
      if (current !== generation.current) return;
      setAuthorizationOperations(current => ({ ...current, [row.name]: next }));
      setNotice(null);
    } catch (error) {
      if (current !== generation.current) return;
      const refusal = methodRefusal(error);
      if (refusal) notifyUnavailable('取消授权', refusal);
      else setNotice({ tone: 'warning', text: '取消结果未确认，请刷新授权状态。' });
    } finally {
      if (current === generation.current) setBusy(null);
    }
  }

  async function syncIdentities(row: PlatformIntegration): Promise<boolean> {
    if (!api?.syncIdentities || busy) return false;
    setBusy(`identities:${row.name}`);
    try {
      const result = await api.syncIdentities(row.name);
      await reload();
      setNotice(result.ok && result.errors.length === 0
        ? { tone: 'info', text: `平台身份已同步：${result.identitiesRecorded} 个。` }
        : {
            tone: 'warning',
            text: `平台身份同步未完成：已记录 ${result.identitiesRecorded} 个，${result.errors.length} 个错误。`,
          });
    } catch (error) {
      const refusal = methodRefusal(error);
      if (refusal) {
        notifyUnavailable('同步平台身份', refusal);
        return false;
      }
      setNotice({ tone: 'warning', text: '平台身份同步结果尚未确认，请刷新后重试。' });
    } finally {
      setBusy(null);
    }
    return true;
  }

  function openAdd(): void {
    const refusal = capabilityRefusal(api?.capabilities?.add);
    if (refusal) {
      notifyUnavailable(COPY.add, refusal);
      return;
    }
    setAddOpen(true);
    if (addOperation && active(addOperation)) return;
    setAddPlatform('lark');
    setAddMode(api?.beginAdd && capabilityRefusal(api.capabilities?.onboard) === null ? 'create' : 'adopt');
    setAddName('');
    setAddAppId('');
    setAddSecret('');
    setAddOperation(null);
    setAddError(registrationUnknown ? COPY.unknownRegistration : null);
  }

  function closeAdd(): void {
    if (busy?.startsWith('add')) return;
    setAddOpen(false);
    setAddError(null);
    setAddSecret('');
  }

  function clearAddOutcome(): void {
    setAddOperation(null);
    setAddReadFailed(false);
    if (!registrationUnknown) setAddError(null);
  }

  async function finishAdd(operation: AdapterOperation, current: number): Promise<void> {
    setPendingReload(operation.reloaded === false);
    setAddOpen(false);
    await reload(current);
    if (current !== generation.current) return;
    setNotice(operation.reloaded === false
      ? { tone: 'warning', text: '接入已保存，请重新加载配置。' }
      : { tone: 'info', text: '平台接入已添加。' });
  }

  async function beginAdd(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!api || busy || (addMode === 'create' && (addReadPending || addReadFailed)) || (addOperation && active(addOperation)) || registrationUnknown) return;
    const name = addName.trim();
    if (!name) {
      setAddError('请输入接入名称。');
      return;
    }
    if (!VALID_NAME.test(name)) {
      setAddError('名称须以小写字母或数字开头，仅含小写字母、数字、点、下划线或连字符。');
      return;
    }
    if (addMode === 'adopt' && (!addAppId.trim() || !addSecret)) {
      setAddError('使用已有应用需要 App ID 和 App Secret。');
      return;
    }
    const register = addMode === 'adopt' ? api.register : undefined;
    const begin = addMode === 'create' ? api.beginAdd : undefined;
    if (!register && !begin) return;
    const refusal = capabilityRefusal(addMode === 'adopt' ? api.capabilities?.register : api.capabilities?.onboard);
    if (refusal) {
      notifyUnavailable(COPY.addSubmit, refusal);
      return;
    }
    const input: AddPlatformInput = {
      provider: addPlatform,
      name,
      mode: addMode,
      ...(addMode === 'adopt' ? { appId: addAppId.trim(), secret: addSecret } : {}),
    };
    const typedSecret = addSecret;
    setBusy('add');
    setAddError(null);
    setAddSecret('');
    const current = generation.current;
    try {
      if (register) {
        const result = await register({ provider: addPlatform, name, appId: input.appId!, secret: input.secret! });
        if (generation.current !== current) return;
        setPendingReload(!result.reloaded);
        setAddOpen(false);
        await reload(current);
        if (generation.current !== current) return;
        setNotice(result.reloaded
          ? { tone: 'info', text: '平台接入已添加。' }
          : { tone: 'warning', text: '接入已保存，请重新加载配置。' });
        return;
      }
      const operation = await begin!(input);
      if (generation.current !== current) return;
      setAddOperation(operation);
      if (operation.status === 'succeeded') {
        await finishAdd(operation, current);
      }
    } catch (error) {
      if (generation.current !== current) return;
      const refused = methodRefusal(error);
      if (refused) {
        setAddSecret(typedSecret);
        notifyUnavailable(COPY.addSubmit, refused);
        return;
      }
      if (addMode === 'adopt') {
        const rejected = error instanceof ClientError && (error.dispatch === 'not-dispatched'
          || ['ADAPTER_EXISTS', 'INVALID_ARGUMENT', 'CALLER_NOT_AUTHORIZED', 'DAEMON_NOT_READY'].includes(error.code));
        if (rejected) setAddError(error.code === 'ADAPTER_EXISTS' ? '同名平台接入已存在。' : `添加失败（${error.code}）。`);
        else {
          setRegistrationUnknown(true);
          setPendingReload(true);
          setAddError(COPY.unknownRegistration);
        }
      } else {
        const rejected = error instanceof ClientError && (error.dispatch === 'not-dispatched'
          || ['ADAPTER_EXISTS', 'INVALID_ARGUMENT', 'CALLER_NOT_AUTHORIZED', 'DAEMON_NOT_READY', 'ADAPTER_ONBOARD_UNAVAILABLE'].includes(error.code));
        if (rejected) setAddError(`添加失败（${error.code}）。`);
        else {
          setAddOperation({ operationId: '', adapter: name, provider: addPlatform, kind: 'add', status: 'unknown' });
          setAddError('添加结果未确认，请刷新添加状态。');
        }
      }
    } finally {
      if (generation.current === current) setBusy(null);
    }
  }

  async function pollAdd(): Promise<void> {
    if (!api?.addOperation || !addOperation || busy) return;
    const current = generation.current;
    setBusy('add-poll');
    try {
      const next = addOperation.operationId ? await api.addOperation(addOperation.operationId) : await api.currentAdd?.(addOperation.adapter);
      if (current !== generation.current) return;
      setAddError(null);
      if (next !== undefined) setAddOperation(next);
      if (next?.status === 'succeeded') await finishAdd(next, current);
    } catch (error) {
      if (current !== generation.current) return;
      const refusal = methodRefusal(error);
      if (refusal) notifyUnavailable('刷新添加状态', refusal);
      else setAddError('添加记录读取失败。');
    } finally {
      if (current === generation.current) setBusy(null);
    }
  }

  async function cancelAdd(): Promise<void> {
    if (!api?.cancelAdd || !addOperation?.operationId || busy) return;
    const current = generation.current;
    setBusy('add-cancel');
    try {
      const next = await api.cancelAdd(addOperation.operationId);
      if (current !== generation.current) return;
      setAddOperation(next);
      setAddError(null);
    } catch (error) {
      if (current !== generation.current) return;
      const refusal = methodRefusal(error);
      if (refusal) notifyUnavailable('取消添加', refusal);
      else setAddError('取消结果尚未确认。');
    } finally {
      if (current === generation.current) setBusy(null);
    }
  }

  async function prepareRemove(row: PlatformIntegration): Promise<void> {
    if (!api?.removePreview || busy) return;
    const current = generation.current;
    setBusy(`remove-preview:${row.name}`);
    try {
      const preview = await api.removePreview(row.name);
      if (current !== generation.current) return;
      setRemovePreview(preview);
      setRemoveTarget(row);
      setRemoveSubmitted(false);
      setRemoveError(null);
      setNotice(null);
    } catch (error) {
      if (current !== generation.current) return;
      const refusal = methodRefusal(error);
      if (refusal) notifyUnavailable('删除接入', refusal);
      else setNotice({ tone: 'warning', text: '删除预览尚未确认。' });
    } finally {
      if (current === generation.current) setBusy(null);
    }
  }

  async function confirmRemove(): Promise<void> {
    if (!api?.remove || !removePreview || !removeTarget || busy || removeSubmitted || removePreview.processRunning) return;
    const target = removePreview;
    const current = generation.current;
    setBusy('remove');
    setRemoveSubmitted(true);
    try {
      await api.remove(target.name, target);
      if (current !== generation.current) return;
      const remaining = await api.list();
      if (current !== generation.current) return;
      if (remaining.some(row => row.name === target.name)) throw new Error('删除后仍在列表中');
      setRemoveTarget(null);
      setRemovePreview(null);
      applyRows(remaining);
      setNotice({ tone: 'info', text: '平台接入已删除。' });
    } catch (error) {
      if (current !== generation.current) return;
      const refusal = methodRefusal(error);
      if (refusal) {
        setRemoveSubmitted(false);
        notifyUnavailable(COPY.removeConfirm, refusal);
      } else if (error instanceof ClientError && ['PREVIEW_STALE', 'PREVIEW_EXPIRED', 'PREVIEW_UNKNOWN'].includes(error.code)) {
        setRemoveError('删除预览已失效，请关闭后刷新并重新预览。');
      } else if (error instanceof ClientError && error.code === 'OPERATION_IN_PROGRESS') {
        setRemoveError('平台接入仍在运行或处理中，请关闭后刷新，停止接入后重新预览。');
      } else setRemoveError('删除结果尚未确认，已暂停重复提交。请关闭后刷新列表，核对状态再重新预览。');
    } finally {
      if (current === generation.current) setBusy(null);
    }
  }

  const adding = addOperation !== null && active(addOperation);
  const canRegister = Boolean(api?.register);
  const canOnboard = Boolean(api?.beginAdd);

  if (!api || view.state === 'absent') return null;

  return (
    <ModuleSection
      id="platform-integrations-title"
      className="platform-integrations"
      title={COPY.title}
      actions={api ? (
        <>
          <Button variant="secondary" disabled={busy !== null} onClick={() => void refresh()}>
            {COPY.refresh}
          </Button>
          {(canRegister || canOnboard) && (
            <Button variant="primary" disabled={busy !== null} onClick={openAdd}>
              {COPY.add}
            </Button>
          )}
        </>
      ) : undefined}
    >
      {notice && <Notice tone={notice.tone}>{notice.text}</Notice>}

      {view.state === 'loading' && <LoadingState title="正在读取平台接入" />}
      {view.state === 'error' && <ErrorState message="平台接入读取失败，请刷新后重试。" />}
      {view.state === 'ready' && (rows.length === 0 ? (
        <EmptyState title={COPY.empty} />
      ) : (
        <ul className="platform-integrations__list" aria-label="平台接入列表">
          {groups.map(group => (
            <li key={group.provider} className="platform-integrations__group">
              <h3 className="platform-integrations__group-title">
                {providerLabel(group.provider)}
                <span>{group.rows.length}</span>
              </h3>
              <ul
                className="platform-integrations__items"
                aria-label={`${providerLabel(group.provider)}接入`}
              >
                {group.rows.map(row => {
                  const open = expanded === row.name;
                  return (
                    <li key={`${row.provider}:${row.name}`} className="platform-integration">
                      <div className="platform-integration__main">
                        <div className="platform-integration__row">
                          <div>
                            <strong>{row.name}</strong>
                            {row.authorization.state === 'authorized' && row.online && (
                              <p className="platform-integrations__available" role="status">可用</p>
                            )}
                          </div>
                          <Button
                            variant="secondary"
                            aria-expanded={open}
                            onClick={() => {
                              if (!open && api?.currentAuthorization) setAuthorizationRead(row.name);
                              setExpanded(open ? null : row.name);
                            }}
                          >
                            {open ? '收起' : '详情'}
                          </Button>
                        </div>
                        {open && api && (
                          <PlatformIntegrationDetail
                            row={row}
                            api={api}
                            busy={busy}
                            operation={authorizationOperations[row.name]}
                            authorizationReadPending={authorizationRead === row.name}
                            onAuthorize={(label) => void beginAuthorization(row, label)}
                            onPollAuthorization={() => void pollAuthorization(row)}
                            onCancelAuthorization={() => void cancelAuthorization(row)}
                            onStartStop={() => void startStop(row)}
                            onSyncIdentities={() => syncIdentities(row)}
                            onRemove={() => void prepareRemove(row)}
                          />
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </li>
          ))}
        </ul>
      ))}

      {api && ((view.state === 'ready' && rows.length > 0) || pendingReload) && (
        <div className="platform-integrations__config-actions">
          <Button variant="text" title="重新读取本机全部平台接入配置" disabled={busy !== null} onClick={() => void reloadConfig()}>
            {COPY.reload}
          </Button>
        </div>
      )}

      <Dialog
        open={addOpen}
        title={COPY.addTitle}
        onClose={closeAdd}
        actions={[
          { label: adding ? '关闭' : COPY.addCancel, variant: 'secondary', disabled: busy?.startsWith('add'), onClick: closeAdd },
          ...(!adding ? [{
            label: COPY.addSubmit,
            variant: 'primary' as const,
            loading: busy === 'add',
            disabled: busy !== null || registrationUnknown || !addName.trim()
              || (addMode === 'create' ? !canOnboard || addReadPending || addReadFailed : !canRegister || !addAppId.trim() || !addSecret),
            onClick: () => {
              const form = document.getElementById('platform-add-form') as HTMLFormElement | null;
              form?.requestSubmit();
            },
          }] : []),
        ]}
      >
        <form id="platform-add-form" className="platform-add-form" onSubmit={(event) => void beginAdd(event)}>
          <Field label={COPY.platform} htmlFor="platform-add-provider">
            <Select
              id="platform-add-provider"
              value={addPlatform}
              options={PLATFORM_PROVIDERS.map(item => ({
                value: item.id,
                label: item.label,
              }))}
              onValueChange={(value) => {
                setAddPlatform(value as PlatformProvider);
                clearAddOutcome();
              }}
              disabled={busy !== null || adding}
            />
          </Field>
          {canOnboard && canRegister && (
            <Field label={COPY.mode} htmlFor="platform-add-mode">
              <div className="platform-add-mode">
                <label>
                  <input
                    type="radio"
                    name="platform-add-mode"
                    checked={addMode === 'create'}
                    disabled={busy !== null || adding}
                    onChange={() => { setAddMode('create'); setAddReadPending(Boolean(api?.currentAdd && addName.trim())); clearAddOutcome(); }}
                  />
                  {COPY.createMode}
                </label>
                <label>
                  <input
                    type="radio"
                    name="platform-add-mode"
                    checked={addMode === 'adopt'}
                    disabled={busy !== null || adding}
                    onChange={() => { setAddMode('adopt'); clearAddOutcome(); }}
                  />
                  {COPY.adoptMode}
                </label>
              </div>
            </Field>
          )}
          <Field label={COPY.name} htmlFor="platform-add-name">
            <input
              id="platform-add-name"
              value={addName}
              disabled={busy !== null || adding}
              onChange={(event) => { setAddName(event.target.value); setAddReadPending(Boolean(api?.currentAdd && event.target.value.trim())); clearAddOutcome(); }}
            />
          </Field>
          {addMode === 'adopt' && (
            <>
              <Field label={COPY.appId} htmlFor="platform-add-app-id">
                <input
                  id="platform-add-app-id"
                  value={addAppId}
                  disabled={busy !== null || adding}
                  onChange={(event) => { setAddAppId(event.target.value); clearAddOutcome(); }}
                />
              </Field>
              <SecretField
                layout="inline"
                id="platform-add-secret"
                label={COPY.appSecret}
                value={addSecret}
                onChange={(value) => { setAddSecret(value); clearAddOutcome(); }}
                disabled={busy !== null || adding}
              />
            </>
          )}
          {addError && <p className="platform-integrations__error" role="alert">{addError}</p>}
          {addMode === 'create' && addReadFailed && <Button type="button" variant="secondary"
            disabled={busy !== null || addReadPending} onClick={() => setAddReadAttempt(value => value + 1)}>重新读取添加记录</Button>}
          {addOperation && <OperationProgress operation={addOperation} busy={busy !== null}
            onRefresh={api?.addOperation ? () => void pollAdd() : undefined}
            onCancel={api?.cancelAdd && addOperation.operationId ? () => void cancelAdd() : undefined} />}
          {addOperation?.remoteOutcomeUnknown && addOperation.status === 'cancelled'
            && <p role="status">已停止等待；已创建的应用需在飞书中处理。</p>}
        </form>
      </Dialog>

      <Dialog
        open={removeTarget !== null}
        title={COPY.removeTitle}
        description={COPY.removeDescription}
        onClose={() => { if (busy !== 'remove') setRemoveTarget(null); }}
        actions={[
          { label: COPY.addCancel, variant: 'secondary', disabled: busy === 'remove', onClick: () => setRemoveTarget(null) },
          {
            label: COPY.removeConfirm,
            variant: 'danger',
            loading: busy === 'remove',
            disabled: removeSubmitted || removePreview?.processRunning === true,
            onClick: () => void confirmRemove(),
          },
        ]}
      >
        {removePreview && (
          <div>
            <p>{providerLabel(removePreview.provider)} · {removePreview.name}</p>
            <p>绑定 Agent：{removePreview.boundAgent ?? '未绑定'}</p>
            <p>删除只移除本机配置，保留 Agent、其 cell 绑定与平台上的应用。</p>
            {removePreview.routes && <p>同时移除消息路由：{removePreview.routes.length ? removePreview.routes.join('、') : '无'}</p>}
            {removePreview.credentialRemoved === true && <p>同时删除此接入专用的凭据。</p>}
            {removePreview.credentialRemoved === false && <p>共享或外部引用的接入凭据将保留。</p>}
            <p>运行状态：{removePreview.online ? '在线' : removePreview.processRunning ? '进程运行中' : '已停止'}</p>
            {removePreview.processRunning && <p role="alert">请先停止该平台接入。</p>}
            {removeError && <p role="alert">{removeError}</p>}
          </div>
        )}
      </Dialog>
    </ModuleSection>
  );
}
