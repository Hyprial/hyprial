import { useId, useState, type FormEvent } from 'react';
import { Button, Field } from '../primitives';
import { SecretField } from './SecretField';

export interface ApiKeyValues {
  readonly endpoint: string;
  readonly apiKey: string;
}

export interface ApiKeyFormProps {
  readonly onSubmit: (values: ApiKeyValues) => void;
  readonly onCancel?: () => void;
  readonly busy?: boolean;
  readonly submitLabel?: string;
  readonly initialEndpoint?: string;
  readonly idPrefix?: string;
}

/**
 * Model API-key form shared by model configuration and first-run.
 *
 * The endpoint is non-sensitive and stays visible after a failed attempt. The
 * key is only held in component memory and is cleared before the callback and
 * on cancel. No request is made by this component; the caller owns validation.
 */
export function ApiKeyForm({
  onSubmit,
  onCancel,
  busy = false,
  submitLabel = '校验并保存',
  initialEndpoint = '',
  idPrefix,
}: ApiKeyFormProps) {
  const generatedId = useId().replace(/:/g, '');
  const baseId = idPrefix ?? `dev01-api-key-${generatedId}`;
  const [endpoint, setEndpoint] = useState(initialEndpoint);
  const [apiKey, setApiKey] = useState('');

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const nextEndpoint = endpoint.trim();
    const nextKey = apiKey.trim();
    if (!nextEndpoint || !nextKey || busy) {
      return;
    }
    setEndpoint(nextEndpoint);
    setApiKey('');
    onSubmit({ endpoint: nextEndpoint, apiKey: nextKey });
  }

  function cancel(): void {
    setApiKey('');
    onCancel?.();
  }

  return (
    <form className="settings-form" onSubmit={submit}>
      <Field
        label="服务端点"
        htmlFor={`${baseId}-endpoint`}
        hint="此处只检查 URL 格式，不会连接该地址。"
        required
      >
        <input
          id={`${baseId}-endpoint`}
          type="url"
          value={endpoint}
          placeholder="https://example.invalid/v1"
          autoComplete="off"
          spellCheck={false}
          onChange={(event) => setEndpoint(event.target.value)}
        />
      </Field>
      <SecretField
        id={`${baseId}-key`}
        label="API Key"
        value={apiKey}
        required
        hint="仅保存在本页内存中，不写入浏览器存储、URL 或日志。"
        showLabel="显示 API Key"
        hideLabel="隐藏 API Key"
        onChange={setApiKey}
      />
      <div className="settings-actions">
        <Button
          type="submit"
          variant="primary"
          loading={busy}
          disabled={!endpoint.trim() || !apiKey.trim()}
        >
          {submitLabel}
        </Button>
        {onCancel && (
          <Button variant="secondary" onClick={cancel}>
            取消
          </Button>
        )}
      </div>
    </form>
  );
}
