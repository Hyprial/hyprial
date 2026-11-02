/**
 * J4 message connection model.
 *
 * The section and the chat channel switch both read this one model; the session
 * keeps the only authoritative connection state.
 */
export type MessageConnectionState = 'not-connected' | 'connected' | 'unavailable';

export interface MessageConnectionModel {
  readonly state: MessageConnectionState;
  readonly boundAgentName: string | null;
  /** Result the authorization step will produce; the caller supplies it. */
  readonly authorizeOutcome: 'success' | 'failure';
}

export const MESSAGE_CONNECTION_COPY = {
  title: '消息连接',
  add: '添加连接',
  platform: '选择消息平台',
  platformFeishu: '飞书',
  agent: '选择要绑定的 Agent',
  authorize: '前往飞书完成授权',
  authorizeLocal: '完成本地授权',
  reauthorize: '重新授权',
  failure: '授权未完成。请重新授权。',
  connected: '已连接',
  next: '下一步',
  back: '返回上一步',
  cancel: '取消',
  inApp: '应用内',
  feishu: '用飞书沟通',
} as const;
