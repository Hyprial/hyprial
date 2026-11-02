import { useEffect, useRef, useState } from 'react';
import {
  Button,
  Dialog,
  EmptyState,
  Field,
  Notice,
  UnsupportedState,
} from '../../components/primitives';
import {
  MODEL_METHOD_LABELS,
  MODEL_SECTION_COPY,
  type ModelMethod,
  type ModelServicesSectionModel,
} from './model-services-view-model';
import './connections.css';

export interface ModelServiceSectionProps {
  readonly model: ModelServicesSectionModel;
  readonly onAdd: (label: string, method: ModelMethod, credential: string) => 'success' | 'failure';
  readonly onSetDefault: (serviceId: string) => void;
  readonly disabled?: boolean;
}

type Mode = 'list' | 'form';

export function ModelServiceSection({
  model,
  onAdd,
  onSetDefault,
  disabled = false,
}: ModelServiceSectionProps) {
  const [mode, setMode] = useState<Mode>('list');
  const [label, setLabel] = useState('');
  const [provider, setProvider] = useState('provider-a');
  const [method, setMethod] = useState<ModelMethod>('account');
  const [credential, setCredential] = useState('');
  const [result, setResult] = useState<'idle' | 'failure'>('idle');
  const [askDiscard, setAskDiscard] = useState(false);
  const [focusAdd, setFocusAdd] = useState(false);
  const sectionRef = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!focusAdd || mode !== 'list') return;
    const button = sectionRef.current?.querySelector<HTMLButtonElement>(
      '.model-service-add',
    );
    button?.focus();
    setFocusAdd(false);
  }, [focusAdd, mode]);

  const hasInput = label.trim().length > 0 || credential.length > 0;
  // A failure scenario still shows the form; submit then reports the failure.
  const unavailable = disabled;

  function reset(): void {
    setLabel('');
    setProvider('provider-a');
    setMethod('account');
    setCredential('');
    setResult('idle');
    setMode('list');
    setFocusAdd(true);
  }

  function submit(): void {
    const outcome = onAdd(label, method, credential);
    if (outcome === 'success') {
      setCredential('');
      reset();
      return;
    }
    setCredential('');
    setResult('failure');
  }

  function cancel(): void {
    if (hasInput) {
      setAskDiscard(true);
      return;
    }
    reset();
  }

  return (
    <section
      ref={sectionRef}
      className="card model-service-section"
      aria-labelledby="model-service-title"
    >
      <div className="section-heading">
        <div>
          <h2 id="model-service-title" className="section-title">
            {MODEL_SECTION_COPY.title}
          </h2>
          <p className="copy">{MODEL_SECTION_COPY.quotaNote}</p>
        </div>
        {mode === 'list' ? (
          <Button
            className="model-service-add"
            variant="primary"
            onClick={() => setMode('form')}
            disabled={unavailable}
          >
            {MODEL_SECTION_COPY.add}
          </Button>
        ) : null}
      </div>

      {mode === 'list' ? (
        unavailable ? <UnsupportedState reason="模型服务状态尚未确认" /> : model.services.length === 0 ? (
          <EmptyState
            title={MODEL_SECTION_COPY.empty}
            description={MODEL_SECTION_COPY.quotaNote}
          />
        ) : (
          <ul className="model-service-list" aria-label="模型服务列表">
            {model.services.map((service) => (
              <li className="model-service-row" key={service.id} data-service-id={service.id}>
                <span className="model-service-name">{service.label}</span>
                <span className="badge badge-positive">{MODEL_SECTION_COPY.connected}</span>
                {service.isDefault ? (
                  <span className="badge badge-neutral">{MODEL_SECTION_COPY.defaultTag}</span>
                ) : null}
                <span className="model-service-meta">
                  {MODEL_METHOD_LABELS[service.method]} · {service.agentCount}
                </span>
                {service.isDefault ? null : (
                  <Button variant="secondary" onClick={() => onSetDefault(service.id)}>
                    设为默认
                  </Button>
                )}
              </li>
            ))}
          </ul>
        )
      ) : (
        <div className="model-service-form">
          {unavailable ? (
            <UnsupportedState reason="暂不可用" />
          ) : (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                submit();
              }}
            >
              <Field label="服务名称" htmlFor="model-label">
                <input
                  id="model-label"
                  value={label}
                  disabled={disabled}
                  onChange={(event) => setLabel(event.target.value)}
                />
              </Field>

              <Field label="提供方" htmlFor="model-provider">
                <select
                  id="model-provider"
                  value={provider}
                  disabled={disabled}
                  onChange={(event) => setProvider(event.target.value)}
                >
                  <option value="provider-a">提供方一</option>
                  <option value="provider-b">提供方二</option>
                </select>
              </Field>

              <fieldset className="model-service-methods">
                <legend>接入方式</legend>
                {(['account', 'api-key'] as const).map((value) => (
                  <label key={value}>
                    <input
                      type="radio"
                      name="model-method"
                      value={value}
                      checked={method === value}
                      onChange={() => setMethod(value)}
                    />
                    {MODEL_METHOD_LABELS[value]}
                  </label>
                ))}
              </fieldset>

              {method === 'api-key' ? (
                <Field label={MODEL_SECTION_COPY.credentialLabel} htmlFor="model-credential">
                  <input
                    id="model-credential"
                    type="password"
                    value={credential}
                    disabled={disabled}
                    onChange={(event) => setCredential(event.target.value)}
                  />
                </Field>
              ) : null}

              {result === 'failure' ? (
                <Notice tone="danger">{MODEL_SECTION_COPY.failure}</Notice>
              ) : null}

              <div className="model-service-actions">
                <Button
                  type="submit"
                  variant="primary"
                  disabled={method === 'api-key' && credential.trim().length === 0}
                >
                  {result === 'failure' ? '重试' : '添加'}
                </Button>
                <Button variant="secondary" onClick={cancel}>
                  取消
                </Button>
              </div>
            </form>
          )}
        </div>
      )}

      {askDiscard ? (
        <Dialog
          open={askDiscard}
          title={MODEL_SECTION_COPY.discardTitle}
          description={MODEL_SECTION_COPY.discardNote}
          onClose={() => setAskDiscard(false)}
          actions={[
            {
              label: MODEL_SECTION_COPY.keepEditing,
              onClick: () => setAskDiscard(false),
              variant: 'primary',
            },
            {
              label: MODEL_SECTION_COPY.discardConfirm,
              onClick: () => {
                setAskDiscard(false);
                reset();
              },
              variant: 'secondary',
            },
          ]}
        />
      ) : null}
    </section>
  );
}
