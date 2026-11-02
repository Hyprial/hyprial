import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Button, Notice, StatusCard } from '../../components/primitives';
import { SecretField } from '../../components/forms';
import type { FirstRunActions, ModelKeySaveResult } from '../onboarding';

/** Credential persistence only: this component never starts Pi or verifies a model. */
export function ModelCredentialSettings({ actions }: { readonly actions: FirstRunActions }) {
  const [saved, setSaved] = useState<boolean | null>(null);
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ModelKeySaveResult | null>(null);
  const epoch = useRef(0);
  const saving = useRef(false);
  useEffect(() => {
    const id = ++epoch.current;
    void actions.getSetupState?.().then(value => {
      if (id === epoch.current) setSaved(value.keySaved);
    }).catch(() => { if (id === epoch.current) setSaved(null); });
    return () => { epoch.current += 1; };
  }, [actions]);
  async function save(event: FormEvent) {
    event.preventDefault(); if (saving.current || !key.trim()) return;
    const id = ++epoch.current;
    saving.current = true; setBusy(true); setResult(null);
    const value = key.trim(); setKey('');
    try {
      const next = await actions.saveModelKey?.(value) ?? { status: 'unavailable' as const, message: '凭证保存入口未启用。' };
      if (id !== epoch.current) return;
      setResult(next);
      // A failed replacement does not erase an earlier confirmed credential.
      if (next.status === 'saved') setSaved(true);
    } catch { if (id === epoch.current) setResult({ status: 'failed', message: '保存结果未知，请检查当前实例状态。' }); }
    finally { if (id === epoch.current) { saving.current = false; setBusy(false); } }
  }
  return <>
    <StatusCard label="DeepSeek 凭证" value={saved === true ? '凭证已保存' : saved === false ? '未保存凭证' : '凭证状态未知'}
      tone={saved === true ? 'positive' : 'neutral'} hint="保存凭证不代表模型可用；模型响应须由真实对话确认。" />
    <p>当前首次使用由 Pi 运行 Squire。这里仅管理 DeepSeek 凭证，不自动创建、授权或重启 Agent。</p>
    <form onSubmit={event => void save(event)}>
      <SecretField id="settings-deepseek-key" label="DeepSeek API Key" value={key} onChange={setKey} disabled={busy}
        showLabel="显示 Key" hideLabel="隐藏 Key" hint="Key 仅保存在当前实例，页面不写浏览器存储。" />
      {result && <Notice tone={result.status === 'saved' ? 'info' : 'warning'} title={result.status === 'saved' ? '凭证已保存' : '凭证保存未确认'}>{result.message}</Notice>}
      <div className="settings-actions"><Button type="submit" variant="primary" disabled={busy || !key.trim()} loading={busy}>保存 Key</Button>
        <Button disabled={busy} variant="text" onClick={() => { setKey(''); setResult(null); }}>取消</Button></div>
    </form>
    {saved && <p className="settings-footnote">已授权凭证的替换可能被当前服务拒绝；不会自动撤销既有授权。</p>}
  </>;
}
