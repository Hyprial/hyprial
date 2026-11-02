import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { Dispatch } from 'react';
import type { AppAction } from '../../state/types';
import { Button, Select } from '../../components/primitives';

/**
 * 登录服务商（provider 凭据）流程 —— 研发补齐，待产品规范设计。
 *
 * 依据 mvp-required-flows.md M1 与 provider-key-ownership 设计（#2077）：
 * 秘钥属于用户账号，登录后 provider 出现在连接页列表，Agent 经 grant 使用。
 * 网关接缝：生产构建走 unavailable（绝不显示成功）；开发/测试走内存模拟。
 * 约束：API Key 只存在于输入框与一次提交过程，不进入普通页面 state 之外的
 * 任何地方，不写浏览器存储，不打日志，提交/取消后立即清空。
 */

export const PROVIDER_OPTIONS = ['claude', 'codex', 'pi'] as const;
export type ProviderOption = (typeof PROVIDER_OPTIONS)[number];
export type ProviderLoginMethod = 'cli' | 'api-key';

export interface ProviderLoginDraft {
  readonly provider: ProviderOption;
  readonly method: ProviderLoginMethod;
}

export type ProviderLoginResult =
  | { readonly kind: 'logged-in'; readonly provider: ProviderOption }
  | { readonly kind: 'failed'; readonly message: string }
  | { readonly kind: 'unavailable'; readonly message: string };

export interface ProviderLoginGateway {
  login(draft: ProviderLoginDraft): Promise<ProviderLoginResult>;
}

export const PROVIDER_LOGIN_UNAVAILABLE = '凭据操作暂不可用。';
export const VALIDATE_USAGE_NOTE = '校验可能调用模型并产生少量用量。';

const STEP_MS = 450;

export function createUnavailableProviderLoginGateway(): ProviderLoginGateway {
  return {
    async login() {
      return { kind: 'unavailable', message: PROVIDER_LOGIN_UNAVAILABLE };
    },
  };
}

/** In-memory simulation for development and tests; never leaves the page. */
export function createLocalProviderLoginGateway(): ProviderLoginGateway {
  return {
    async login(draft) {
      return { kind: 'logged-in', provider: draft.provider };
    },
  };
}

type Stage = 'form' | 'saving' | 'saved' | 'validating';

export function ProviderLoginFlow({
  onClose,
  onComplete,
  dispatch,
  gateway,
}: {
  readonly onClose: () => void;
  readonly onComplete: (provider: ProviderOption) => void;
  readonly dispatch: Dispatch<AppAction>;
  readonly gateway?: ProviderLoginGateway;
}) {
  const [provider, setProvider] = useState<ProviderOption>('claude');
  const [method, setMethod] = useState<ProviderLoginMethod>('cli');
  const [apiKey, setApiKey] = useState('');
  const [showKey, setShowKey] = useState(false);
  const [stage, setStage] = useState<Stage>('form');
  const [message, setMessage] = useState<{ tone: 'error' | 'status'; text: string } | null>(null);
  const providerRef = useRef<HTMLButtonElement>(null);
  const client =
    gateway ??
    (import.meta.env.MODE === 'production'
      ? createUnavailableProviderLoginGateway()
      : createLocalProviderLoginGateway());

  useEffect(() => {
    providerRef.current?.focus();
  }, []);

  const busy = stage !== 'form';

  function close(): void {
    setApiKey('');
    onClose();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'Escape' && !busy) {
      event.stopPropagation();
      close();
    }
  }

  async function submit(): Promise<void> {
    if (busy) {
      return;
    }
    if (method === 'api-key' && apiKey.trim() === '') {
      setMessage({ tone: 'error', text: '请先粘贴 API Key。' });
      return;
    }
    setMessage(null);
    // The key is cleared here and never carried into the draft: this mock
    // has no channel for it, and the real integration must use the
    // dedicated sensitive-field path instead of the ordinary state machine.
    setApiKey('');
    setStage('saving');
    await new Promise((resolve) => window.setTimeout(resolve, STEP_MS));
    setStage('saved');
    await new Promise((resolve) => window.setTimeout(resolve, STEP_MS));
    setStage('validating');
    await new Promise((resolve) => window.setTimeout(resolve, STEP_MS));

    const result = await client.login({ provider, method });
    if (result.kind === 'logged-in') {
      dispatch({
        type: 'push-notice',
        tone: 'success',
        text: `${result.provider} 已登录（内存数据，未接后端）。`,
        source: 'provider-login',
      });
      onComplete(result.provider);
      return;
    }
    setStage('form');
    if (result.kind === 'unavailable') {
      dispatch({
        type: 'push-notice',
        tone: 'warning',
        text: result.message,
        source: 'provider-login',
      });
      setMessage({ tone: 'error', text: result.message });
      return;
    }
    setMessage({
      tone: 'error',
      text: `${result.message} Key 已清空，请重新填写。`,
    });
  }

  const stageCopy: Readonly<Record<Stage, string>> = {
    form: '',
    saving: '正在保存凭据…',
    saved: '已保存，待校验。',
    validating: '正在校验（可能调用模型并产生少量用量）…',
  };

  return (
    <div className="provider-login-layer">
      <div
        className="provider-login"
        role="dialog"
        aria-modal="true"
        aria-labelledby="provider-login-title"
        onKeyDown={handleKeyDown}
      >
        <h2 id="provider-login-title">登录服务商</h2>
        <p className="provider-login-note">
          秘钥属于你的账号，按账号计费；Agent 经授权（grant）使用。研发补齐，待产品规范设计。
        </p>

        <label htmlFor="provider-login-provider">服务商</label>
        <Select
          id="provider-login-provider"
          ref={providerRef}
          value={provider}
          disabled={busy}
          onValueChange={(value) => setProvider(value as ProviderOption)}
          options={PROVIDER_OPTIONS.map((option) => ({
            value: option,
            label: option,
          }))}
        />

        <fieldset className="provider-login-method">
          <legend>凭据方式</legend>
          <label>
            <input
              type="radio"
              name="provider-login-method"
              checked={method === 'cli'}
              disabled={busy}
              onChange={() => setMethod('cli')}
            />
            从本机服务商 CLI 登录种入（OAuth）
          </label>
          <label>
            <input
              type="radio"
              name="provider-login-method"
              checked={method === 'api-key'}
              disabled={busy}
              onChange={() => setMethod('api-key')}
            />
            粘贴 API Key
          </label>
        </fieldset>

        {method === 'api-key' && (
          <>
            <label htmlFor="provider-login-key">API Key</label>
            <div className="provider-login-key">
              <input
                id="provider-login-key"
                type={showKey ? 'text' : 'password'}
                autoComplete="off"
                value={apiKey}
                disabled={busy}
                aria-invalid={message?.tone === 'error' || undefined}
                onChange={(event) => setApiKey(event.target.value)}
              />
              <Button
                variant="secondary"
                className="button button-secondary"
                onClick={() => setShowKey((visible) => !visible)}
              >
                {showKey ? '隐藏' : '显示'}
              </Button>
            </div>
            <p className="provider-login-hint">
              Key 只在输入框与一次提交中短暂存在，不写入存储或日志。
            </p>
          </>
        )}

        {stage !== 'form' && (
          <p className="provider-login-stage" role="status">
            {stageCopy[stage]}
          </p>
        )}

        {message && (
          <p className="provider-login-message" role={message.tone === 'error' ? 'alert' : 'status'}>
            {message.text}
          </p>
        )}

        <div className="provider-login-actions">
          <Button
            variant="primary"
            className="button button-strong"
            loading={busy}
            onClick={() => void submit()}
          >
            {busy ? '处理中…' : '保存并校验'}
          </Button>
          <Button
            variant="secondary"
            className="button button-secondary"
            disabled={busy}
            onClick={close}
          >
            取消
          </Button>
        </div>
      </div>
    </div>
  );
}
