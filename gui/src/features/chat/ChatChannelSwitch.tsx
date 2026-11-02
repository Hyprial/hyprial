import {
  MESSAGE_CONNECTION_COPY,
  type MessageConnectionModel,
} from '../connections/message-connection-view-model';


export interface ChatChannelSwitchProps {
  readonly model: MessageConnectionModel;
  readonly channel: 'in-app' | 'feishu';
  readonly onChange: (channel: 'in-app' | 'feishu') => void;
}

/** Channel switch for the chat composer; hidden until the connection exists. */
export function ChatChannelSwitch({
  model,
  channel,
  onChange,
}: ChatChannelSwitchProps) {
  if (model.state !== 'connected') {
    return null;
  }
  return (
    <div className="chat-channel-switch" role="group" aria-label="沟通方式">
      <button
        type="button"
        className="filter-chip"
        aria-pressed={channel === 'in-app'}
        onClick={() => onChange('in-app')}
      >
        {MESSAGE_CONNECTION_COPY.inApp}
      </button>
      <button
        type="button"
        className="filter-chip"
        aria-pressed={channel === 'feishu'}
        onClick={() => onChange('feishu')}
      >
        {MESSAGE_CONNECTION_COPY.feishu}
      </button>
    </div>
  );
}
