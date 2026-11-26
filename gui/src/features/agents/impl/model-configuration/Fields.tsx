/** One model selection for create and configure. No vendor inference or implicit model. */
import { useEffect, useState } from 'react';
import type { SharedCredentialsApi, CredentialEntry } from '../../../connections';
import { Select } from '../../../../components/primitives';
import type { ModelConfiguration, NativeRecord } from './api';
export interface ModelChoice {
  harness: string;
  authKind: 'connection' | 'native';
  model: string;
  connection?: CredentialEntry['connection'];
  nativeAuthRef?: string;
}
export function choiceError(choice: ModelChoice, creating = false): string | null {
  if (!['pi', 'claude', 'codex'].includes(choice.harness)) return '请选择 harness。';
  if (!choice.model || choice.model !== choice.model.trim() || /[\s\x00-\x1f\x7f]/u.test(choice.model)) return '请手填完整模型 ID，不含空白或控制字符。';
  if (choice.authKind === 'connection') {
    if (choice.harness !== 'pi') return '自定义连接当前仅支持 Pi 后台运行。';
    if (!choice.connection) return '请选择连接。';
  } else if (!creating && !choice.nativeAuthRef) return '请选择当前 Agent 的登录目标。';
  return null;
}
export function choiceConfiguration(choice: ModelChoice): ModelConfiguration {
  const error = choiceError(choice);
  if (error) throw new Error(error);
  const common = { harness: choice.harness as ModelConfiguration['harness'], mode: 'headless' as const, model: choice.model };
  return choice.authKind === 'native'
    ? { ...common, authKind: 'native', nativeAuthRef: choice.nativeAuthRef! }
    : { ...common, authKind: 'connection', connectionRef: choice.connection!.connectionRef, configRevision: choice.connection!.configRevision };
}
export function Fields({ value, onChange, credentials, targets = [], disabled, revision = 0, creating = false }: {
  value: ModelChoice; onChange: (value: ModelChoice) => void; credentials?: SharedCredentialsApi;
  targets?: readonly NativeRecord[]; disabled: boolean; revision?: number; creating?: boolean;
}) {
  const [entries, setEntries] = useState<readonly CredentialEntry[] | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let active = true;
    setEntries(null); setFailed(false);
    if (credentials) void credentials.listKeys().then(rows => {
      if (active) setEntries(rows.filter(row => row.state === 'active' && row.connection));
    }).catch(() => { if (active) setFailed(true); });
    return () => { active = false; };
  }, [credentials, revision]);
  const choices = targets.filter(row => row.harness === value.harness && row.state === 'stored').flatMap(row => row.targets ?? []);
  return <>
    <label htmlFor="agent-harness">harness</label>
    <Select id="agent-harness" value={value.harness} disabled={disabled} onValueChange={harness => onChange({ ...value, harness, connection: undefined, nativeAuthRef: undefined })}
      options={[{ value: '', label: '请选择' }, ...['pi', 'claude', 'codex'].map(harness => ({ value: harness, label: harness }))]} />
    <label htmlFor="agent-model-auth">凭据</label>
    <Select id="agent-model-auth" value={value.authKind} disabled={disabled} onValueChange={authKind => onChange({ ...value, authKind: authKind as ModelChoice['authKind'], connection: undefined, nativeAuthRef: undefined })}
      options={[{ value: 'connection', label: '连接 / Key' }, { value: 'native', label: '原生 Auth' }]} />
    {value.authKind === 'connection' ? <>
      <label htmlFor="agent-model-connection">连接</label>
      <Select id="agent-model-connection" value={value.connection?.connectionRef ?? ''} disabled={disabled || entries === null} onValueChange={ref => onChange({ ...value, connection: entries?.find(row => row.connection?.connectionRef === ref)?.connection })}
        options={[{ value: '', label: '请选择连接' }, ...(entries ?? []).map(row => ({ value: row.connection!.connectionRef, label: `${row.label} · ${row.connection!.endpoint}` }))]} />
      <p className="agent-field-hint">{failed ? '连接清单读取失败，请刷新后核对。' : !credentials ? '请先到 Agent 设置核对连接清单，或选择原生 Auth。' : entries === null ? '读取连接…' : entries.length === 0 ? '请先在 Agent 设置中添加连接；旧 Key 需显式保存为连接。' : '当前仅支持 Pi 后台运行；最终以启动检查为准。'}</p>
      {value.connection && <p className="agent-field-hint">{value.connection.endpoint} · 修订 {value.connection.configRevision}</p>}
    </> : creating ? <p className="agent-field-hint">创建身份后，在详情导入此 Agent 的登录文件并选择登录目标。完成前不能启动。</p> : <>
      <label htmlFor="agent-native-target">登录目标</label>
      <Select id="agent-native-target" value={value.nativeAuthRef ?? ''} disabled={disabled} onValueChange={nativeAuthRef => onChange({ ...value, nativeAuthRef })}
        options={[{ value: '', label: '请选择登录目标' }, ...choices.map(row => ({ value: row.nativeAuthRef, label: `${row.target} · ${row.kind}` }))]} />
      {choices.length === 0 && <p className="agent-field-hint">尚无可选登录目标，请导入当前 Agent 的登录文件。</p>}
    </>}
    <label htmlFor="agent-model">模型 ID</label>
    <input id="agent-model" className="ui-native-control" value={value.model} disabled={disabled} autoComplete="off" onChange={event => onChange({ ...value, model: event.target.value })} />
    <p className="agent-field-hint">手填服务实际接受的完整模型 ID。运行方式：后台运行。</p>
  </>;
}
