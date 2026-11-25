/**
 * Decision record (group chat Contract Mock verification, slice GC5-1, 2026-11-21):
 * - Exercises the published contract examples through the Contract Mock engine,
 *   product transport, generated client, `createConnectedGuiCapabilities`, and
 *   `ConnectedChatPage` without any hand-written responses.
 * - Empirically tests every row of Plan §4 against the Contract Mock table so
 *   any divergence is caught directly by tests.
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { connectBrowserBackend } from '../../src/app/backend';
import { GuiCapabilitiesProvider, createConnectedGuiCapabilities } from '../../src/app/capabilities';
import { connectContractMock } from '../../src/dev/contract-mock';
import { ConnectedChatPage } from '../../src/pages/ConnectedChatPage';
import {
  MOCK_ORIGIN,
  createContractMock,
  createContractMockFetch,
  projectContract,
  withDigest,
} from '../../contract-mock/index.ts';

const GROUP_ID = 'g.4lvgrfdnb4vtmpxk36wa55phb4';
const WORKER_ID = 'a.workeraaaaaaaaaaaaaaaaaaaa';
const REVIEWER_ID = 'a.revieweraaaaaaaaaaaaaaaaaa';

const CONTRACT = resolve(dirname(fileURLToPath(import.meta.url)), '../../../contract/daemon-request');
const contractFile = (name: string) => JSON.parse(readFileSync(resolve(CONTRACT, name), 'utf8'));

describe('group chat through Contract Mock', () => {
  it('lists the release group from with-groups and renders group-page history on ConnectedChatPage', async () => {
    const user = userEvent.setup();
    const bootstrap = await connectContractMock({ scenario: 'success' });
    const capabilities = createConnectedGuiCapabilities(bootstrap.connection);
    expect(capabilities.groupChatRead).toBeDefined();

    render(<GuiCapabilitiesProvider value={capabilities}><ConnectedChatPage /></GuiCapabilitiesProvider>);

    const groupRegion = await screen.findByRole('group', { name: '群聊' });
    const releaseButton = within(groupRegion).getByRole('button', { name: /release/ });
    expect(within(releaseButton).getByText('3 位成员')).toBeVisible();

    await user.click(releaseButton);
    const log = await screen.findByRole('log', { name: '群聊 release 的消息' });
    expect(await within(log).findByText('please cut the release')).toBeVisible();
    expect(within(log).getByText('please tag the release')).toBeVisible();
    expect(within(log).getByText('tag is pushed')).toBeVisible();
    expect(within(log).getByText('alice创建了群聊「release」')).toBeVisible();
    expect(within(log).getByText('alice添加了 worker')).toBeVisible();
    expect(within(log).getByText('alice添加了 reviewer')).toBeVisible();
    expect(within(log).getByText('@worker')).toBeVisible();
    expect(within(log).getByText('回复 alice：please cut the release')).toBeVisible();

    const input = screen.getByLabelText('给 release 的消息');
    expect(input).toBeVisible();
    await user.type(input, 'ship preview release');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await waitFor(() => {
      expect(input).toHaveValue('');
    });
    expect(screen.queryByText(/发送结果未知/)).not.toBeInTheDocument();
  });

  it('verifies every row of the Plan §4 Contract Mock example-selection table', async () => {
    const table = withDigest(projectContract(contractFile('openapi.json'), contractFile('http-envelope.schema.json')));
    const mock = createContractMock(table, { preset: 'success' });
    const memory = new Map<string, string>();
    const bootstrap = await connectBrowserBackend({
      fetch: createContractMockFetch(mock),
      storage: {
        getItem: key => memory.get(key) ?? null,
        setItem: (key, value) => { memory.set(key, value); },
        removeItem: key => { memory.delete(key); },
      },
      location: { origin: MOCK_ORIGIN, hash: '#grant=contract-mock-launch', pathname: '/', search: '' },
      history: { replaceState() {} },
    });
    const client = bootstrap.connection.client;

    const listed = await client.conversations.list({ kinds: ['direct', 'group'] });
    expect(listed.conversations.some(c => c.kind === 'group' && c.conversationId === GROUP_ID)).toBe(true);

    const history = await client.conversations.history({ conversationId: GROUP_ID });
    expect(history.entries).toHaveLength(6);
    expect(history.memberNames).toEqual({
      'user:alice': 'alice',
      [WORKER_ID]: 'worker',
      [REVIEWER_ID]: 'reviewer',
    });

    const got = await client.conversations.get({ conversationId: GROUP_ID });
    expect(got.title).toBe('release');
    expect(got.members).toHaveLength(3);

    const added = await client.conversations.members.add({
      conversationId: GROUP_ID,
      member: WORKER_ID,
      idempotencyKey: 'k'.repeat(43),
    });
    expect(added.conversationId).toBe(GROUP_ID);

    const removedReviewer = await client.conversations.members.remove({
      conversationId: GROUP_ID,
      memberId: REVIEWER_ID,
      idempotencyKey: 'r'.repeat(43),
    });
    expect(removedReviewer.conversationId).toBe(GROUP_ID);

    const removedWorker = await client.conversations.members.remove({
      conversationId: GROUP_ID,
      memberId: WORKER_ID,
      idempotencyKey: 'w'.repeat(43),
    });
    expect(removedWorker.conversationId).toBe(GROUP_ID);
    expect(removedWorker.members.map(member => member.memberId)).toEqual(['user:alice', REVIEWER_ID]);

    const posted = await client.conversations.post({
      conversationId: GROUP_ID,
      message: 'hello',
      summary: 'hello',
      idempotencyKey: 'p'.repeat(43),
    });
    expect(posted.operationId).toBe('p'.repeat(43));

    const created = await client.conversations.create({
      title: 'release',
      members: [WORKER_ID, REVIEWER_ID],
      idempotencyKey: 'c'.repeat(43),
    });
    expect(created.conversationId).toBe(GROUP_ID);
    expect(created.title).toBe('release');
    expect(created.members.map(member => member.memberId)).toEqual(['user:alice', WORKER_ID, REVIEWER_ID]);

    await expect(client.conversations.create({
      title: 'custom-group',
      members: [WORKER_ID],
      idempotencyKey: 'c'.repeat(43),
    })).rejects.toMatchObject({ code: 'MOCK_EXAMPLE_MISSING' });
  });
});
