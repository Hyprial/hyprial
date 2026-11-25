import { createGeneratedClient } from '../../src/client/generated';
import { createChatChannelsDaemonApi } from '../../src/features/chat-channels';

it('uses one injected client for chat reads', async () => {
  const calls: string[] = [];
  const client = createGeneratedClient({ invoke: async <T>(method: string): Promise<T> => {
    calls.push(method);
    return { ok: true, conversationId: 'c.conv-1', entries: [], nextCursor: null } as T;
  } });
  const api = createChatChannelsDaemonApi(client);
  expect(await api.messageRead.history('c.conv-1')).toEqual({ entries: [], nextCursor: null });
  expect(calls).toEqual(['conversations.history']);
});

it('keeps backend failures visible without a local success fallback', async () => {
  const failure = new Error('backend unavailable');
  const client = createGeneratedClient({ invoke: async <T>(): Promise<T> => { throw failure; } });
  const api = createChatChannelsDaemonApi(client);
  await expect(api.messageRead.history('c.conv-1')).rejects.toBe(failure);
});
