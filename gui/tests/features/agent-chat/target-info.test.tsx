import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { AgentChatSection } from '../../../src/features/agent-chat';
import { chatTargetInfo } from '../../../src/pages/chat-target-info';
import { UNWIRED_SNAPSHOT, type OrgDomainSnapshot } from '../../../src/features/org';
import { OrgDomainProvider, createUnwiredGateway } from '../../../src/features/org';
import { GuiCapabilitiesProvider, createWebGuiCapabilities } from '../../../src/app/capabilities';
import type { AgentsApi } from '../../../src/features/agents';
import { ConnectedChatPage } from '../../../src/pages/ConnectedChatPage';
import type { SessionTarget } from '../../../src/state/gui-view-model';

const target: SessionTarget = { id: 'agent:alice:host:stable-id', agentId: 'stable-id', kind: 'agent',
  displayName: 'Research', role: 'agent:alice:host:stable-id', availability: 'available',
  avatarTone: 'green', capabilities: ['fixture-only-capability'] };
const directory = [{ uri: target.id, owner: 'Alice', host: 'mac-studio' }];
const snapshot: OrgDomainSnapshot = { ...UNWIRED_SNAPSHOT, status: 'ready',
  orgs: [{ id: 'visible', name: 'Team', kind: 'work', role: 'member', lifecycle: 'active',
    ownerLabel: null, authorityDevice: null, deviceCount: null }],
  agentOrgs: { 'stable-id': ['visible', 'hidden'] } };

describe('chat object facts', () => {
  it('joins by exact URI and shows only visible organization facts', () => {
    expect(chatTargetInfo(target, directory, snapshot)).toEqual({ targetId: target.id,
      host: 'mac-studio', owner: 'Alice', organizations: ['Team'] });
    expect(chatTargetInfo({ ...target, id: 'agent:bob:other:stable-id', agentId: null }, directory, snapshot))
      .toEqual({ targetId: 'agent:bob:other:stable-id', host: undefined, owner: undefined, organizations: undefined });
  });

  it('distinguishes unavailable membership from no visible memberships', () => {
    expect(chatTargetInfo(target, [], UNWIRED_SNAPSHOT)?.organizations).toBeUndefined();
    expect(chatTargetInfo(target, [], { ...snapshot, status: 'error' })?.organizations).toBeUndefined();
    expect(chatTargetInfo(target, [], { ...snapshot, agentOrgs: null })?.organizations).toBeUndefined();
    expect(chatTargetInfo(target, [], { ...snapshot, agentOrgs: { 'stable-id': [] } })?.organizations).toEqual([]);
    expect(chatTargetInfo(target, [], { ...snapshot, agentOrgs: { 'stable-id': ['hidden'] } })?.organizations).toEqual([]);
  });

  it('does not infer membership, host or owner from the address or display name', () => {
    expect(chatTargetInfo({ ...target, agentId: undefined }, [], snapshot))
      .toEqual({ targetId: target.id, host: undefined, owner: undefined, organizations: undefined });
    expect(chatTargetInfo(undefined, directory, snapshot)).toBeUndefined();
  });
});

function show(info = chatTargetInfo(target, directory, snapshot), selected = target) {
  return render(<AgentChatSection targets={[selected]} activeTargetId={selected.id} messages={[]} draft=""
    history={{ targetId: selected.id, status: 'ready' }} infoExpanded targetInfo={info}
    onSelectTarget={vi.fn()} onDraftChange={vi.fn()} onSend={vi.fn()} onRetry={vi.fn()}
    onInfoExpandedChange={vi.fn()} />);
}

it('shows real object facts and removes the placeholder capabilities panel', () => {
  show();
  const info = within(screen.getByRole('complementary', { name: '对象信息' }));
  for (const text of ['名称', 'Research', '类型', 'Agent', '主机', 'mac-studio', '所有者', 'Alice', '可见组织', 'Team']) {
    expect(info.getByText(text)).toBeVisible();
  }
  expect(info.queryByText('能力')).toBeNull();
  expect(info.queryByText('fixture-only-capability')).toBeNull();
});

it('does not reuse a previous object’s facts after selection changes', () => {
  show(chatTargetInfo(target, directory, snapshot), { ...target, id: 'agent:other:node:new', displayName: 'Other' });
  const info = within(screen.getByRole('complementary', { name: '对象信息' }));
  expect(info.queryByText('Alice')).toBeNull();
  expect(info.queryByText('Team')).toBeNull();
  expect(info.queryByText('暂无可见组织')).toBeNull();
});

it('labels contacts as contacts and closing information calls the shared controller', () => {
  const close = vi.fn();
  render(<AgentChatSection targets={[{ ...target, kind: 'human' }]} activeTargetId={target.id}
    messages={[]} draft="" history={{ targetId: target.id, status: 'ready' }} infoExpanded
    onSelectTarget={vi.fn()} onDraftChange={vi.fn()} onSend={vi.fn()} onRetry={vi.fn()} onInfoExpandedChange={close} />);
  expect(within(screen.getByRole('complementary', { name: '对象信息' })).getByText('联系人')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: '收起信息' }));
  expect(close).toHaveBeenCalledWith(false);
});

it('connects directory and organization reads to the ordinary chat page', async () => {
  const capabilities = { ...createWebGuiCapabilities(),
    agents: { list: async () => directory.map(row => ({ ...row, actor: 'stable-id', displayName: 'Research',
      provider: null, model: null, harness: null, preferredHarness: null, capabilities: [],
      runState: { runtime: 'unknown' }, cwd: null, homeMtimeMs: null,
      createdAtMs: null, hasLastSessionId: null, lastActiveAtMs: null })) } as AgentsApi,
    chatTargets: async () => [{ targetUri: target.id, agentId: target.agentId, displayName: 'Research',
      actor: 'actor-name-must-not-be-used', targetKind: 'agent', deliverable: true, status: 'online' }],
    messageRead: { query: async () => [], conversations: async () => ({ entries: [], nextCursor: null }),
      history: async () => ({ entries: [], nextCursor: null }),
      lookup: async () => ({ status: 'unknown' as const, reason: 'not-found' as const }) },
  };
  const source = { initial: snapshot, createGateway: createUnwiredGateway };
  render(<OrgDomainProvider source={source}><GuiCapabilitiesProvider value={capabilities}>
    <ConnectedChatPage initialTargetId={target.id} />
  </GuiCapabilitiesProvider></OrgDomainProvider>);
  fireEvent.click(await screen.findByRole('button', { name: '信息' }));
  const info = within(screen.getByRole('complementary', { name: '对象信息' }));
  expect(await info.findByText('mac-studio')).toBeVisible();
  expect(info.getByText('Alice')).toBeVisible();
  expect(info.getByText('Team')).toBeVisible();
  expect(info.queryByText('hidden')).toBeNull();
  expect(info.queryByText('actor-name-must-not-be-used')).toBeNull();
});
