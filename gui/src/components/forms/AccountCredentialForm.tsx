import { useId, useState, type FormEvent } from 'react';
import { Button, Field } from '../primitives';
import { SecretField } from './SecretField';

export interface AccountCredentialValues {
  readonly account: string;
  readonly token: string;
}

export interface AccountCredentialFormProps {
  readonly onSubmit: (values: AccountCredentialValues) => void;
  readonly onCancel?: () => void;
  readonly busy?: boolean;
  readonly submitLabel?: string;
  readonly initialAccount?: string;
  readonly idPrefix?: string;
}

/**
 * Account credential form shared by everyday settings and first-run.
 *
 * This is a UI callback contract only: it names no HTTP endpoint, command or
 * daemon protocol. Callers decide how to attempt authorization and must keep
 * the outcome unknown or unavailable until a real adapter confirms it.
 */
export function AccountCredentialForm({
  onSubmit,
  onCancel,
  busy = false,
  submitLabel = '登录',
  initialAccount = '',
  idPrefix,
}: AccountCredentialFormProps) {
  const generatedId = useId().replace(/:/g, '');
  const baseId = idPrefix ?? `dev01-account-${generatedId}`;
  const [account, setAccount] = useState(initialAccount);
  const [token, setToken] = useState('');

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const nextAccount = account.trim();
    const nextToken = token.trim();
    if (!nextAccount || !nextToken || busy) {
      return;
    }
    setAccount(nextAccount);
    setToken('');
    onSubmit({ account: nextAccount, token: nextToken });
  }

  function cancel(): void {
    setToken('');
    onCancel?.();
  }

  return (
    <form className="settings-form" onSubmit={submit}>
      <Field
        label="账号"
        htmlFor={`${baseId}-account`}
        hint="非敏感输入会在本次页面内保留。"
        required
      >
        <input
          id={`${baseId}-account`}
          value={account}
          autoComplete="username"
          onChange={(event) => setAccount(event.target.value)}
        />
      </Field>
      <SecretField
        id={`${baseId}-token`}
        label="访问令牌"
        value={token}
        required
        hint="提交后立即从表单清除；仅在本次页面内存中用于重试，取消或离开页面即丢弃。"
        showLabel="显示访问令牌"
        hideLabel="隐藏访问令牌"
        onChange={setToken}
      />
      <div className="settings-actions">
        <Button
          type="submit"
          variant="primary"
          loading={busy}
          disabled={!account.trim() || !token.trim()}
        >
          {submitLabel}
        </Button>
        {onCancel && (
          <Button variant="secondary" onClick={cancel}>
            取消
          </Button>
        )}
      </div>
      <p className="settings-footnote">
        访问令牌不写入 localStorage、sessionStorage、URL 或日志。
      </p>
    </form>
  );
}
