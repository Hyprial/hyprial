import { useState } from 'react';
import { LarkChannelView, type LarkStep } from '../../pages/channels/LarkChannelView';
import type { MessageConnectionModel } from './message-connection-view-model';

export interface MessageConnectionSectionProps {
  readonly model: MessageConnectionModel;
  readonly onAuthorize: (agentId: string) => void;
  readonly agentOptions: readonly { readonly id: string; readonly name: string }[];
  readonly defaultAgentId: string | null;
}

/** In-memory scenario controller; production and preview share LarkChannelView. */
export function MessageConnectionSection({ model, onAuthorize, agentOptions, defaultAgentId }: MessageConnectionSectionProps) {
  const [step, setStep] = useState<LarkStep>('chooseAgent');
  const [selected, setSelected] = useState(defaultAgentId ?? agentOptions[0]?.id ?? '');
  const [authorized, setAuthorized] = useState<string | null>(null);
  const [failed, setFailed] = useState<string | null>(null);
  const id = agentOptions.some(agent => agent.id === selected) ? selected : null;
  const connected = model.state === 'connected' && agentOptions.some(agent => agent.id === id && agent.name === model.boundAgentName);
  const ready = model.state !== 'unavailable';
  function authorize(agentId: string) {
    if (!ready) return;
    if (model.authorizeOutcome === 'failure') { setFailed(agentId); onAuthorize(agentId); return; }
    setFailed(null); setAuthorized(agentId);
  }
  function connect(agentId: string) {
    if (!ready || (authorized !== agentId && !connected)) return;
    onAuthorize(agentId); setStep('result');
  }
  return <LarkChannelView agents={agentOptions} selectedAgentId={id} step={step}
    onAgentChange={setSelected} onStepChange={setStep}
    auth={{ capability: ready ? 'ready' : 'unavailable', operation: id && failed === id ? {
      agentId: id, requestId: `preview-auth:${id}`, status: 'definitive-failure', canRetry: true, error: '模拟授权失败，请重试。',
    } : undefined }}
    switch={{ capability: ready ? 'ready' : 'unavailable' }}
    authorization={id ? { agentId: id, status: connected || authorized === id ? 'authorized' : ready ? 'unauthorized' : 'unconfirmed' } : undefined}
    channelResult={id ? { agentId: id, status: connected ? 'connected' : ready ? 'disconnected' : 'unavailable' } : undefined}
    onAuthorize={authorize} onSwitch={connect}
    onRetry={(action) => { if (id) { if (action === 'auth') authorize(id); else connect(id); } }} />;
}
