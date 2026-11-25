import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import {
  type ChatSendApi,
  ConnectedChatSendPicker,
  type MessageReadApi,
} from '../../src/features/chat-channels';
import { GuiCapabilitiesProvider, createWebGuiCapabilities, type GuiCapabilities } from '../../src/app/capabilities';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';

const targetUri = 'agent:owner:node:worker';
const sent: unknown[] = [];
const send: ChatSendApi = {
  storageName: 'test', pendingOperationKey: () => null,
  send: async intent => { sent.push(intent); return { status: 'not-started', reason: 'capability-unavailable', notice: 'test' }; },
  recover: async () => ({ status: 'idle', notice: '没有可恢复的发送操作键。' }),
  sendWithKey: async () => ({ status: 'not-started', reason: 'capability-unavailable', notice: 'test' }),
  recoverKey: async () => ({ status: 'idle', notice: '没有可恢复的发送操作键。' }),
  clear: () => undefined,
};
const read: MessageReadApi = {
  query: async view => view === 'outbox' ? [{ messageId: 'm1', conversationId: 'c1', sender: 'user:owner', recipient: targetUri }] : [],
  history: async () => ({ entries: [], nextCursor: null }),
  lookup: async () => ({ status: 'unknown', reason: 'not-found' }),
  conversations: async () => ({ entries: [{ documentId: 'c.c1', conversationId: 'c1', peer: targetUri, lastMessageAtMs: 1 }], nextCursor: null }),
};

it('requires an explicit existing agent conversation before sending', async () => {
  sent.length = 0;
  render(<ConnectedChatSendPicker send={send} read={read} identity="trusted-user" generation="one"
    targets={[{ targetKind: 'agent', targetUri, deliverable: true, displayName: 'Alfred' }]} />);
  const choice = await screen.findByRole('option', { name: 'Alfred · c1' });
  expect(choice).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
  fireEvent.change(screen.getByRole('combobox', { name: '发送会话' }), { target: { value: 'c1' } });
  fireEvent.change(screen.getByLabelText('给 Alfred 的消息'), { target: { value: 'hello' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(sent).toEqual([{ to: [targetUri], conversationId: 'c1', message: 'hello' }]));
});

it('does not expose a selection when the target directory is empty', async () => {
  sent.length = 0;
  render(<ConnectedChatSendPicker send={send} read={read} identity="trusted-user" generation="one" targets={[]} />);
  expect(await screen.findByText('暂无可发送的已存在 Agent 会话。')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
  expect(sent).toEqual([]);
});

it('uses the ordinary connected page read capability for send selection', async () => {
  render(<GuiCapabilitiesProvider value={{ ...createWebGuiCapabilities(), messageRead: read }}>
    <ConnectedChatPage chatSendDirectory={{ api: send, identity: 'trusted-user', generation: 'one',
      targets: [{ targetKind: 'agent', targetUri, deliverable: true }] }} />
  </GuiCapabilitiesProvider>);
  expect(await screen.findByRole('combobox', { name: '发送会话' })).toBeInTheDocument();
  expect(screen.getByRole('option', { name: /worker.*c1/ })).toBeInTheDocument();
});

it('invalidates the old selection and clears its recovery key when the host generation changes', async () => {
  const clears: string[] = [];
  const scopedSend: ChatSendApi = { ...send, clear: reason => { clears.push(reason); } };
  const view = render(<ConnectedChatSendPicker send={scopedSend} read={read} identity="trusted-user" generation="one"
    targets={[{ targetKind: 'agent', targetUri, deliverable: true }]} />);
  fireEvent.change(await screen.findByRole('combobox', { name: '发送会话' }), { target: { value: 'c1' } });
  expect(screen.getByLabelText(`给 ${targetUri} 的消息`)).toBeInTheDocument();
  view.rerender(<ConnectedChatSendPicker send={scopedSend} read={read} identity="trusted-user" generation="two"
    targets={[{ targetKind: 'agent', targetUri, deliverable: true }]} />);
  expect(clears).toEqual(['identity-change']);
  expect(screen.queryByLabelText(`给 ${targetUri} 的消息`)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: '发送' })).toBeDisabled();
});

it('renders the shared chat layout from the capability bundle without an AppShell send prop', async () => {
  const intents: unknown[] = [];
  const scopedSend: ChatSendApi = {
    ...send,
    send: async intent => { intents.push(intent); return { status: 'not-started', reason: 'capability-unavailable', notice: 'test' }; },
  };
  const queries: string[] = [];
  const scopedRead: MessageReadApi = {
    ...read,
    query: async view => { queries.push(view); return read.query(view); },
  };
  const capabilities = {
    ...createWebGuiCapabilities(), messageRead: scopedRead, chatSend: scopedSend,
    chatTargets: async () => [{ targetKind: 'agent', targetUri, deliverable: true, actor: targetUri }],
    chatSession: { actor: 'user:owner', generation: 'host-one' },
  } satisfies GuiCapabilities;
  render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);

  expect(await screen.findByTestId('chat-layout')).toBeInTheDocument();
  expect(await screen.findByRole('button', { name: /worker/ })).toBeInTheDocument();
  await waitFor(() => expect(queries.sort()).toEqual(['inbox', 'outbox']));
  expect(screen.queryByRole('combobox', { name: '发送会话' })).not.toBeInTheDocument();

  fireEvent.click(screen.getByRole('button', { name: /worker/ }));
  expect(await screen.findByText('暂无消息。')).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText(`给 ${targetUri} 的消息`), { target: { value: 'hello' } });
  fireEvent.click(screen.getByRole('button', { name: '发送' }));
  await waitFor(() => expect(intents).toEqual([{ to: [targetUri], conversationId: 'c1', message: 'hello' }]));
  expect(screen.getByText('test')).toBeInTheDocument();
});

it('does not enable send when the trusted host session is absent', async () => {
  const capabilities = {
    ...createWebGuiCapabilities(), messageRead: read, chatSend: send,
    chatTargets: async () => [{ targetKind: 'agent', targetUri, deliverable: true, actor: targetUri }],
  } satisfies GuiCapabilities;
  render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);
  expect(await screen.findByRole('button', { name: /worker/ })).toBeInTheDocument();
  expect(document.querySelector('.chat-page-status')).toBeNull();
  expect(screen.queryByRole('combobox', { name: '发送会话' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: /worker/ }));
  expect(screen.queryByRole('button', { name: '发送' })).not.toBeInTheDocument();
});

it('drops an old target response when the host session changes', async () => {
  let finishOld!: (value: readonly { targetKind: string; targetUri: string; deliverable: boolean; actor: string | null }[]) => void;
  const oldTargets = () => new Promise<readonly { targetKind: string; targetUri: string; deliverable: boolean; actor: string | null }[]>(resolve => { finishOld = resolve; });
  const first = { ...createWebGuiCapabilities(), messageRead: read, chatSend: send,
    chatTargets: oldTargets, chatSession: { actor: 'user:owner', generation: 'one' } } satisfies GuiCapabilities;
  const second = { ...createWebGuiCapabilities(), messageRead: read, chatSend: send,
    chatTargets: async () => [], chatSession: { actor: 'user:other', generation: 'two' } } satisfies GuiCapabilities;
  const view = render(<GuiCapabilitiesProvider value={first}><ConnectedChatPage /></GuiCapabilitiesProvider>);
  await waitFor(() => expect(finishOld).toBeDefined());
  view.rerender(<GuiCapabilitiesProvider value={second}><ConnectedChatPage /></GuiCapabilitiesProvider>);
  finishOld([{ targetKind: 'agent', targetUri, deliverable: true, actor: targetUri }]);
  expect(await screen.findByText('暂无聊天对象')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /worker/ })).not.toBeInTheDocument();
});
