import type { MethodAccess } from '../../../../client';
import type { GeneratedClient } from '../../../../client/generated';
import { createMessageReadApiFromClient } from '../message-read-api';

export {
  CHAT_SEND_ACCEPTED_NOTICE,
  CHAT_SEND_NO_RECOVERY_NOTICE,
  CHAT_SEND_OPERATION_KEY_NAME,
  CHAT_SEND_PENDING_NOTICE,
  CHAT_SEND_RECOVERY_ACCEPTED_NOTICE,
  CHAT_SEND_RECOVERY_UNKNOWN_NOTICE,
  CHAT_SEND_REFUSED_NOTICE,
  CHAT_SEND_STORE_FAILED_NOTICE,
  CHAT_SEND_UNKNOWN_NOTICE,
  createChatSendApi,
  isChatSendMethodRefused,
  type ChatSendMethodRefused,
  isCanonicalAgentUri,
  type ChatSendApi,
  type ChatSendIntent,
  type ChatSendOptions,
  type ChatSendOutcome,
  type ChatSendParams,
  type ChatSendPort,
  type ChatSendRecovery,
  type ChatSendStorage,
} from './chat-send';
export {
  GROUP_POST_ACCEPTED_NOTICE,
  GROUP_POST_INVALID_INTENT_NOTICE,
  GROUP_POST_KEY_FAILED_NOTICE,
  GROUP_POST_REFUSED_NOTICE,
  GROUP_POST_UNKNOWN_NOTICE,
  createGroupPostApiFromClient,
  isGroupPostMethodRefused,
  type GroupPostApi,
  type GroupPostIntent,
  type GroupPostMethodRefused,
  type GroupPostOptions,
  type GroupPostOutcome,
} from './group-post';

export type ChatChannelsBackendClient = Pick<GeneratedClient, 'adapter' | 'message' | 'conversations'>;

export function createChatChannelsDaemonApi(client: ChatChannelsBackendClient, access?: MethodAccess) {
  return {
    messageRead: createMessageReadApiFromClient(client, access),
  };
}
