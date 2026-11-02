import { useEffect, useRef, useState, type FormEvent } from 'react';
import { Button, Notice, StatusCard } from '../../components/primitives';
import { SecretField } from '../../components/forms';
import { StepCard } from './StepCard';
import type { FirstRunScenario, ModelKeySaveResult, StepViewState } from './types';

interface ModelStepProps {
  readonly state: StepViewState;
  readonly scenario: FirstRunScenario;
  readonly saved: boolean;
  readonly restored?: boolean;
  readonly onSave?: (key: string) => Promise<ModelKeySaveResult>;
  readonly onSavedChange: (saved: boolean, restored?: boolean) => void;
  readonly onContinue: () => void;
}

/** First-run saves one DeepSeek key. Full model settings remain separate. */
export function ModelStep({ state, scenario, saved, restored = false, onSave, onSavedChange, onContinue }: ModelStepProps) {
  const [key, setKey] = useState('');
  const [editing, setEditing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<ModelKeySaveResult | null>(null);
  const attempt = useRef(0);
  const saving = useRef(false);
  const previousRecord = useRef({ saved: false, restored: false });
  useEffect(() => () => { attempt.current += 1; }, []);

  function replaceKey(): void {
    previousRecord.current = { saved, restored };
    setEditing(true); setKey(''); setResult(null);
    onSavedChange(false);
  }
  function cancelEdit(): void {
    if (saving.current) return;
    attempt.current += 1;
    setKey(''); setResult(null); setEditing(false);
    onSavedChange(previousRecord.current.saved, previousRecord.current.restored);
  }
  async function save(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (saving.current || !key.trim()) return;
    const id = ++attempt.current;
    saving.current = true;
    setBusy(true); setResult(null);
    onSavedChange(false);
    try {
      const pending = onSave?.(key.trim());
      setKey('');
      const next: ModelKeySaveResult = pending ? await pending : {
        status: 'unavailable', message: '凭证保存入口尚未接入，未保存 Key。',
      };
      if (id !== attempt.current) return;
      setResult(next);
      if (next.status === 'saved') {
        onSavedChange(true);
        onContinue();
      }
    } catch {
      if (id === attempt.current) setResult({ status: 'failed', message: 'Key 保存结果未确认，请重新输入并重试。' });
    } finally {
      if (id === attempt.current) { setKey(''); setBusy(false); saving.current = false; }
    }
  }

  return (
    <StepCard id="model" title="模型服务" state={state}>
      <p>为 Squire 配置 DeepSeek API Key，后续由 Pi 运行。</p>
      {state === 'active' && <>
        {saved && !editing ? <>
          <StatusCard label="DeepSeek 凭证" value={scenario === 'demo' ? '已保存 DeepSeek Key（隔离预览）' : '已保存 DeepSeek Key'} tone="positive"
            hint={restored ? '已恢复本机保存记录，不读取或显示 Key。模型可用性仍需真实回复确认。' : '保存不代表模型已连接；真实回复后才能确认模型可用。'} />
          <div className="settings-actions dev01-model-actions">
            <Button variant="text" onClick={replaceKey}>更换 Key</Button>
            <Button variant="primary" onClick={onContinue}>继续</Button>
          </div>
        </> : <form className="dev01-model-key-form" onSubmit={(event) => void save(event)}>
          <SecretField id="dev01-deepseek-key" label="DeepSeek API Key" value={key}
            layout="inline" disabled={busy} required showLabel="显示 Key" hideLabel="隐藏 Key"
            onChange={(value) => { setKey(value); setResult(null); onSavedChange(false); }}
            hint="Key 仅由当前实例保存。" />
          {editing && <p className="settings-footnote">取消可返回已有保存记录。</p>}
          {result && result.status !== 'saved' && <Notice tone="warning" title="Key 保存未确认">{result.message ?? '请重新输入 Key 并重试。'}</Notice>}
          <div className="settings-actions dev01-model-actions">
            {editing && previousRecord.current.saved && <Button variant="text" disabled={busy} onClick={cancelEdit}>取消</Button>}
            <Button variant="primary" type="submit" disabled={busy || !key.trim()} loading={busy}>{busy ? '正在保存…' : '保存并继续'}</Button>
          </div>
        </form>}
        <p className="settings-footnote">Key 将安全保存在当前实例中。下一步将准备 Squire。</p>
      </>}
    </StepCard>
  );
}
