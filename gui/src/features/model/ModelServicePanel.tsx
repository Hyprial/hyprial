import {
  useEffect,
  useId,
  useRef,
  useState,
} from 'react';
import {
  Button,
  Card,
  Notice,
  StatusCard,
  type UiTone,
} from '../../components/primitives';
import {
  AccountCredentialForm,
  type AccountCredentialValues,
  ApiKeyForm,
  type ApiKeyValues,
} from '../../components/forms';

export type ModelAuthMode = 'account' | 'api-key';

/**
 * A deliberately small status surface for the shared connection selector.
 * `failed` and `unknown` both map to `unknown`: neither proves the model is
 * unconfigured, so integration must not present them as a final negative.
 */
export type ModelConnectionState = 'unknown' | 'connected';

export type ModelValidationRequest =
  | {
      readonly mode: 'account';
      readonly values: AccountCredentialValues;
    }
  | {
      readonly mode: 'api-key';
      readonly values: ApiKeyValues;
    };

export type ModelValidationResult =
  | {
      readonly status: 'connected';
      readonly detail?: string;
    }
  | {
      readonly status: 'failed';
      readonly message: string;
      readonly retryable?: boolean;
    }
  | {
      readonly status: 'unknown';
      readonly message: string;
    };

export interface ModelServicePanelProps {
  /**
   * UI callback only. The caller owns the adapter/command and must return
   * `unknown` or `failed` until a real backend confirms a connection.
   */
  readonly onValidate: (
    request: ModelValidationRequest,
  ) => Promise<ModelValidationResult>;
  readonly onCancel?: () => void;
  /**
   * Optional integration hook for the shared connection selector. It starts
   * at `unknown` by contract; this component does not emit an initial value.
   */
  readonly onStatusChange?: (state: ModelConnectionState) => void;
  readonly initialMode?: ModelAuthMode;
  readonly className?: string;
}

type ModelPhase =
  | { readonly kind: 'idle' }
  | { readonly kind: 'submitting' }
  | {
      readonly kind: 'failed';
      readonly message: string;
      readonly retryable: boolean;
    }
  | { readonly kind: 'unknown'; readonly message: string }
  | { readonly kind: 'connected'; readonly detail?: string };

interface StatusPresentation {
  readonly value: string;
  readonly tone: UiTone;
  readonly hint: string;
}

function statusPresentation(phase: ModelPhase): StatusPresentation {
  switch (phase.kind) {
    case 'submitting':
      return {
        value: '正在校验 · 状态未知',
        tone: 'info',
        hint: '只发起调用方提供的校验；结果返回前不视为已连接。',
      };
    case 'failed':
      return {
        value: '校验失败 · 未连接',
        tone: 'danger',
        hint: '失败结果不会改变连接状态；可重试或取消。',
      };
    case 'unknown':
      return {
        value: '状态未知 · 未连接',
        tone: 'warning',
        hint: '未取得可确认的结果；不会显示为已连接。',
      };
    case 'connected':
      return {
        value: '已连接',
        tone: 'positive',
        hint: phase.detail ?? '连接状态由调用方确认。',
      };
    case 'idle':
    default:
      return {
        value: '未连接 · 状态未知',
        tone: 'neutral',
        hint: '未读取真实模型服务状态，也不会显示虚构成功。',
      };
  }
}

/**
 * dev-01 private model-service area.
 *
 * Covers account authorization and API-key configuration, secret reveal,
 * cancel, validation, retryable failure and unknown results. It deliberately
 * defines no HTTP/daemon contract: integration supplies `onValidate`, and the
 * current fixture path must return `unknown` rather than a fabricated success.
 */
export function ModelServicePanel({
  onValidate,
  onCancel,
  onStatusChange,
  initialMode = 'account',
  className,
}: ModelServicePanelProps) {
  const generatedId = useId().replace(/:/g, '');
  const headingId = `${generatedId}-model-heading`;
  const [mode, setMode] = useState<ModelAuthMode>(initialMode);
  const [phase, setPhase] = useState<ModelPhase>({ kind: 'idle' });
  const lastRequest = useRef<ModelValidationRequest | null>(null);
  const attempt = useRef(0);
  const busy = phase.kind === 'submitting';
  const status = statusPresentation(phase);

  useEffect(
    () => () => {
      attempt.current += 1;
      lastRequest.current = null;
    },
    [],
  );

  async function submit(request: ModelValidationRequest): Promise<void> {
    const currentAttempt = attempt.current + 1;
    attempt.current = currentAttempt;
    lastRequest.current = request;
    setPhase({ kind: 'submitting' });
    onStatusChange?.('unknown');

    try {
      const result = await onValidate(request);
      if (attempt.current !== currentAttempt) {
        return;
      }
      if (result.status === 'connected') {
        lastRequest.current = null;
        setPhase({ kind: 'connected', detail: result.detail });
        onStatusChange?.('connected');
        return;
      }
      if (result.status === 'failed') {
        setPhase({
          kind: 'failed',
          message: result.message,
          retryable: result.retryable ?? true,
        });
        onStatusChange?.('unknown');
        return;
      }
      setPhase({ kind: 'unknown', message: result.message });
      onStatusChange?.('unknown');
    } catch {
      if (attempt.current !== currentAttempt) {
        return;
      }
      setPhase({
        kind: 'unknown',
        message: '校验结果未知：无法联系模型服务。',
      });
      onStatusChange?.('unknown');
    }
  }

  function selectMode(nextMode: ModelAuthMode): void {
    if (busy || nextMode === mode) {
      return;
    }
    attempt.current += 1;
    lastRequest.current = null;
    setMode(nextMode);
    setPhase({ kind: 'idle' });
    onStatusChange?.('unknown');
  }

  function retry(): void {
    const request = lastRequest.current;
    if (!request || busy) {
      return;
    }
    void submit(request);
  }

  function cancel(): void {
    attempt.current += 1;
    lastRequest.current = null;
    setPhase({ kind: 'idle' });
    onStatusChange?.('unknown');
    onCancel?.();
  }

  return (
    <Card
      as="section"
      padding="md"
      className={className}
      aria-labelledby={headingId}
    >
      <div className="settings-form">
        <div className="section-heading">
          <div>
            <h3 id={headingId}>模型服务配置</h3>
            <p>账号授权或 API Key 二选一；未接入时保持未连接。</p>
          </div>
        </div>

        <div className="settings-status-list">
          <StatusCard
            label="模型服务"
            value={status.value}
            tone={status.tone}
            hint={status.hint}
          />
          <StatusCard
            label="额度与费用"
            value={null}
            hint="服务未提供；不推测额度、限制或费用。"
          />
        </div>

        <div
          className="settings-actions"
          role="group"
          aria-label="模型配置方式"
        >
          <Button
            size="sm"
            variant={mode === 'account' ? 'primary' : 'secondary'}
            aria-pressed={mode === 'account'}
            disabled={busy}
            onClick={() => selectMode('account')}
          >
            账号授权
          </Button>
          <Button
            size="sm"
            variant={mode === 'api-key' ? 'primary' : 'secondary'}
            aria-pressed={mode === 'api-key'}
            disabled={busy}
            onClick={() => selectMode('api-key')}
          >
            API Key
          </Button>
        </div>

        {phase.kind === 'failed' && (
          <Notice
            tone="danger"
            role="alert"
            title="校验失败"
            action={
              phase.retryable
                ? {
                    label: '重试校验',
                    onClick: retry,
                    disabled: busy,
                  }
                : undefined
            }
          >
            {phase.message}
          </Notice>
        )}

        {phase.kind === 'unknown' && (
          <Notice
            tone="warning"
            role="alert"
            title="状态未知"
            action={{
              label: '重试校验',
              onClick: retry,
              disabled: busy,
            }}
          >
            {phase.message}
          </Notice>
        )}

        {mode === 'account' ? (
          <AccountCredentialForm
            idPrefix={`${generatedId}-model-account`}
            busy={busy}
            submitLabel="使用账号授权并校验"
            onSubmit={(values) =>
              void submit({ mode: 'account', values })
            }
            onCancel={cancel}
          />
        ) : (
          <ApiKeyForm
            idPrefix={`${generatedId}-model-api-key`}
            busy={busy}
            submitLabel="校验并保存"
            onSubmit={(values) =>
              void submit({ mode: 'api-key', values })
            }
            onCancel={cancel}
          />
        )}
      </div>
    </Card>
  );
}
