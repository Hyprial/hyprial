/**
 * J10 model service section model.
 *
 * The section renders only from this model. Credentials never reach it: the
 * component holds typed secrets transiently and the session stores none.
 */
export type ModelMethod = 'account' | 'api-key';

export interface ModelServiceRow {
  readonly id: string;
  readonly label: string;
  readonly connected: boolean;
  readonly isDefault: boolean;
  readonly agentCount: number;
  readonly method: ModelMethod;
}

export interface ModelServicesSectionModel {
  readonly services: readonly ModelServiceRow[];
  readonly addMode: 'ready' | 'failure';
}

export const MODEL_METHOD_LABELS: Readonly<Record<ModelMethod, string>> = {
  account: '账号授权',
  'api-key': 'API Key',
};

export const MODEL_SECTION_COPY = {
  title: '模型服务',
  add: '添加模型服务',
  empty: '还没有模型服务',
  connected: '已连接',
  defaultTag: '默认',
  quotaNote: '额度、使用限制与费用以所选服务为准。',
  failure: '添加未完成。请重试。',
  credentialLabel: 'API Key 凭据',
  discardTitle: '放弃添加模型服务？',
  discardNote: '返回列表后，当前输入会丢失。',
  discardConfirm: '丢弃并返回',
  keepEditing: '继续填写',
} as const;
