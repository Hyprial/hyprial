import { useState } from 'react';
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AgentChatSection, type AgentChatSectionProps } from '../../src/features/agent-chat';
import type { SessionTarget, ConversationMessage } from '../../src/state/gui-view-model';
import { OrgDomainProvider } from '../../src/features/org/impl/domain/context';
import { createUnwiredGateway } from '../../src/features/org/impl/domain/unwired';
import { ORG_PARTNER, ownerSnapshot } from '../../src/dev/org/fixture-data';

const targets: readonly SessionTarget[] = [
  { id: 'a', kind: 'agent', displayName: '研究者', role: '研究', availability: 'available', avatarTone: 'green', capabilities: ['分析资料'] },
  { id: 'b', kind: 'agent', displayName: '编辑', role: '写作', availability: 'disabled', avatarTone: 'lilac', capabilities: [] },
];
const message = (id: string, targetId = 'a', text = id): ConversationMessage => ({ id, targetId, text, author: 'assistant' });
function props(overrides: Partial<AgentChatSectionProps> = {}): AgentChatSectionProps {
  return {
    targets, activeTargetId: 'a', messages: [], draft: 'A 草稿', capability: 'ready',
    history: { targetId: 'a', status: 'ready' }, infoExpanded: true,
    onSelectTarget: vi.fn(), onDraftChange: vi.fn(), onSend: vi.fn(), onRetry: vi.fn(),
    onInfoExpandedChange: vi.fn(), ...overrides,
  };
}
function log() { return screen.getByRole('log'); }

it('folds daemon system messages while leaving the conversation visible', async () => {
  render(<AgentChatSection {...props({ messages: [message('normal', 'a', '测试成功'),
    { ...message('task', 'a', '内部任务详情'), author: 'system' }] })} />);
  expect(screen.getByText('测试成功')).toBeVisible();
  const details = screen.getByText('任务与系统消息').closest('details');
  expect(details).not.toHaveAttribute('open');
  await userEvent.click(screen.getByText('任务与系统消息'));
  expect(details).toHaveAttribute('open');
  expect(screen.getByText('内部任务详情')).toBeVisible();
});

it('keeps an opened system message in its own detail block with the timestamp in the label row', async () => {
  const createdAt = '2026-11-16T09:00:00.000Z';
  render(<AgentChatSection {...props({ messages: [{ ...message('task', 'a',
    'workflow: wf-3017fd4ce4b84e2db1fa2bb929a0bfbe#execute'), author: 'system', createdAt }] })} />);
  const article = document.querySelector<HTMLElement>('.chat-workspace__message[data-author="system"]');
  expect(article).not.toBeNull();
  await userEvent.click(within(article!).getByText('任务与系统消息'));
  const heading = article!.querySelector<HTMLElement>('.chat-workspace__system-heading');
  const details = article!.querySelector<HTMLElement>('details');
  const body = article!.querySelector<HTMLElement>('.chat-workspace__system-body');
  expect(heading).not.toBeNull();
  expect(details).not.toBeNull();
  expect(heading!.querySelector('time')?.getAttribute('datetime')).toBe(createdAt);
  expect(heading!.compareDocumentPosition(details!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  expect(details!.querySelector('time')).toBeNull();
  expect(body?.textContent).toContain('wf-3017fd4ce4b84e2db1fa2bb929a0bfbe#execute');
});
function scrollMetrics(node: HTMLElement, top: number, height = 1000, viewport = 200) {
  Object.defineProperties(node, {
    scrollHeight: { configurable: true, value: height },
    clientHeight: { configurable: true, value: viewport },
  });
  node.scrollTop = top;
  fireEvent.scroll(node);
}
function modelBrowserScroll(node: HTMLElement) {
  let top = 0;
  // jsdom has no layout: model 400px messages and the browser's scroll range clamp.
  const height = () => Math.max(200, node.querySelectorAll('article').length * 400);
  const clamp = (value: number) => Math.max(0, Math.min(value, height() - 200));
  Object.defineProperties(node, {
    scrollHeight: { configurable: true, get: height },
    clientHeight: { configurable: true, value: 200 },
    scrollTop: {
      configurable: true,
      get: () => (top = clamp(top)),
      set: (value: number) => { top = clamp(value); },
    },
  });
}

describe('controlled AgentChatSection', () => {
  it('groups local, current organization, and other targets without duplicates', async () => {
    const groupedTargets: readonly SessionTarget[] = [
      { ...targets[0], id: 'agent-squire', displayName: 'Squire' },
      { ...targets[0], id: 'agent-lin-review', displayName: '评审助手' },
      { ...targets[0], id: 'agent-outside', displayName: '外部助手' },
    ];
    render(<OrgDomainProvider source={{ initial: ownerSnapshot(), createGateway: () => createUnwiredGateway() }}>
      <AgentChatSection {...props({ targets: groupedTargets, activeTargetId: 'agent-lin-review' })}
        localAgentUris={['agent-squire']} />
    </OrgDomainProvider>);
    expect(within(screen.getByRole('group', { name: '本机 Agent' })).getByRole('button', { name: /Squire/ })).toBeInTheDocument();
    expect(within(screen.getByRole('group', { name: '当前组织 · hyprial-dev' })).getByRole('button', { name: /评审助手/ })).toBeInTheDocument();
    expect(within(screen.getByRole('group', { name: '其他可联系' })).getByRole('button', { name: /外部助手/ })).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /Squire/ })).toHaveLength(1);
    await userEvent.click(screen.getByRole('button', { name: /收起本机 Agent/ }));
    expect(screen.queryByRole('button', { name: /Squire/ })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /收起当前组织/ }));
    expect(screen.queryByRole('button', { name: /评审助手/ })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: '评审助手' })).toBeInTheDocument();
  });

  it('moves targets to the new current organization group after a switch', () => {
    const snapshot = ownerSnapshot();
    const groupedTargets: readonly SessionTarget[] = [
      { ...targets[0], id: 'agent-lin-review', displayName: '评审助手' },
      { ...targets[0], id: 'agent-writer', displayName: '写作者' },
    ];
    const source = { initial: snapshot, createGateway: () => createUnwiredGateway() };
    const { rerender } = render(<OrgDomainProvider source={source}>
      <AgentChatSection {...props({ targets: groupedTargets, activeTargetId: 'agent-lin-review' })} />
    </OrgDomainProvider>);
    rerender(<OrgDomainProvider source={source} externalSnapshot={{ ...snapshot, currentOrgId: ORG_PARTNER }}>
      <AgentChatSection {...props({ targets: groupedTargets, activeTargetId: 'agent-lin-review' })} />
    </OrgDomainProvider>);
    expect(screen.getByRole('group', { name: '当前组织 · acme-lab' })).toBeInTheDocument();
    expect(within(screen.getByRole('group', { name: '当前组织 · acme-lab' })).getByRole('button', { name: /评审助手/ })).toBeInTheDocument();
  });
  it.each([null, 'missing'])('handles empty targets and invalid selection %s without leaking props or issuing intents', (activeTargetId) => {
    const p = props({ targets: [], activeTargetId, messages: [message('private')], draft: 'private draft' });
    render(<AgentChatSection {...p} />);
    expect(screen.getByText('暂无聊天对象')).toBeInTheDocument();
    expect(screen.getByText('请选择聊天对象')).toBeInTheDocument();
    expect(screen.queryByText('private')).not.toBeInTheDocument();
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(p.onSelectTarget).not.toHaveBeenCalled();
    expect(p.onSend).not.toHaveBeenCalled();
  });

  it('does not fall back to the first target when the selected id is invalid', () => {
    render(<AgentChatSection {...props({ activeTargetId: 'missing' })} />);
    expect(screen.getByText('请选择聊天对象')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /研究者/ })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('projects selection as an intent and only displays matching message text supplied by props', async () => {
    const p = props({ messages: [message('a1', 'a', '<b>原样文本</b>'), message('b1', 'b', 'B 私有消息')] });
    render(<AgentChatSection {...p} />);
    expect(within(log()).getByText('<b>原样文本</b>')).toBeInTheDocument();
    expect(screen.queryByText('B 私有消息')).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /编辑/ }));
    expect(p.onSelectTarget).toHaveBeenCalledExactlyOnceWith('b');
    expect(screen.getByRole('textbox')).toHaveValue('A 草稿');
    expect(p.onSend).not.toHaveBeenCalled();
  });

  it.each(['loading', 'error'] as const)('shows %s without an empty-history claim and retains matching prior messages', (status) => {
    render(<AgentChatSection {...props({ history: { targetId: 'a', status, error: '读取失败', stale: true }, messages: [message('已有记录')] })} />);
    expect(within(log()).getByText('已有记录')).toBeInTheDocument();
    expect(screen.getByText(status === 'loading' ? '正在加载历史消息…' : '读取失败')).toBeInTheDocument();
    expect(screen.getByText('当前显示上次取得的消息。')).toBeInTheDocument();
    expect(screen.queryByText('暂无消息。')).not.toBeInTheDocument();
    expect(log()).toHaveAttribute('aria-busy', String(status === 'loading'));
  });

  it.each(['loading', 'error'] as const)('shows %s without messages honestly', (status) => {
    render(<AgentChatSection {...props({ history: { targetId: 'a', status } })} />);
    expect(screen.getByText(status === 'loading' ? '正在加载历史消息…' : '历史消息加载失败。')).toBeInTheDocument();
    expect(screen.queryByText('暂无消息。')).not.toBeInTheDocument();
  });

  it('shows ready empty history and does not project a late previous-target history error', () => {
    const p = props();
    const { rerender } = render(<AgentChatSection {...p} />);
    expect(screen.getByText('暂无消息。')).toBeInTheDocument();
    rerender(<AgentChatSection {...p} activeTargetId="b" messages={[message('previous')]} history={{ targetId: 'a', status: 'error', error: '旧对象错误' }} />);
    expect(screen.getByText('正在等待当前对象的历史消息。')).toBeInTheDocument();
    expect(screen.queryByText('旧对象错误')).not.toBeInTheDocument();
    expect(screen.queryByText('previous')).not.toBeInTheDocument();
    expect(screen.queryByText('暂无消息。')).not.toBeInTheDocument();
  });

  it('shows a stale ready history distinctly from a current result', () => {
    render(<AgentChatSection {...props({ history: { targetId: 'a', status: 'ready', stale: true }, messages: [message('旧快照')] })} />);
    expect(screen.getByText('当前显示上次取得的消息。')).toBeInTheDocument();
    expect(screen.getByText('旧快照')).toBeInTheDocument();
  });

  it('uses the controller’s per-target draft through A/B selection without owning a second draft store', async () => {
    const p = props();
    function Controller() {
      const [active, setActive] = useState('a');
      const [drafts, setDrafts] = useState<Record<string, string>>({ a: 'A 草稿', b: 'B 草稿' });
      return <AgentChatSection {...p} activeTargetId={active} draft={drafts[active]} history={{ targetId: active, status: 'ready' }}
        onSelectTarget={setActive} onDraftChange={(id, value) => {
          p.onDraftChange(id, value); setDrafts((before) => ({ ...before, [id]: value }));
        }} />;
    }
    render(<Controller />);
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'A 修订' } });
    expect(p.onDraftChange).toHaveBeenCalledExactlyOnceWith('a', 'A 修订');
    await userEvent.click(screen.getByRole('button', { name: /编辑/ }));
    expect(screen.getByRole('textbox')).toHaveValue('B 草稿');
    await userEvent.click(screen.getByRole('button', { name: /研究者/ }));
    expect(screen.getByRole('textbox')).toHaveValue('A 修订');
    expect(p.onSend).not.toHaveBeenCalled();
  });

  it('defaults to unavailable and never invokes manage without an explicit callback', async () => {
    const p = props({ capability: undefined });
    render(<AgentChatSection {...p} />);
    expect(screen.queryByRole('button', { name: '管理 Agent' })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: '发送' }));
    fireEvent.submit(screen.getByRole('form', { name: '发送消息' }));
    expect(p.onSend).not.toHaveBeenCalled();
    expect(p.onRetry).not.toHaveBeenCalled();
  });

  it('allows explicit manage intent only for Agent targets and labels human targets honestly', async () => {
    const onManageTarget = vi.fn();
    const p = props({ onManageTarget });
    const { rerender } = render(<AgentChatSection {...p} />);
    await userEvent.click(screen.getByRole('button', { name: '管理 Agent' }));
    expect(onManageTarget).toHaveBeenCalledExactlyOnceWith('a');
    rerender(<AgentChatSection {...p} targets={[{ ...targets[0], kind: 'human' }]} />);
    expect(screen.getByRole('complementary', { name: '对象信息' })).toHaveTextContent('联系人');
    expect(screen.queryByRole('button', { name: '管理 Agent' })).not.toBeInTheDocument();
  });

  it('restores each target’s reading position and leaves late messages alone while scrolled up', () => {
    const p = props({ messages: [message('a1')] });
    const { rerender } = render(<AgentChatSection {...p} />);
    scrollMetrics(log(), 120);
    rerender(<AgentChatSection {...p} messages={[message('a1'), message('a2')]} />);
    expect(log().scrollTop).toBe(120);
    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={[message('b1', 'b')]} />);
    scrollMetrics(log(), 260);
    rerender(<AgentChatSection {...p} messages={[message('a1'), message('a2')]} />);
    expect(log().scrollTop).toBe(120);
    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={[message('b1', 'b')]} />);
    expect(log().scrollTop).toBe(260);
  });

  it('follows newly supplied messages only when the reader was already at the bottom', () => {
    const p = props({ messages: [message('a1')] });
    const { rerender } = render(<AgentChatSection {...p} />);
    scrollMetrics(log(), 800);
    Object.defineProperty(log(), 'scrollHeight', { configurable: true, value: 1200 });
    rerender(<AgentChatSection {...p} messages={[message('a1'), message('a2')]} />);
    expect(log().scrollTop).toBe(1200);
  });

  it('restores a saved reading position after empty and partial loading history clamps the log', () => {
    const aMessages = Array.from({ length: 4 }, (_, index) => message(`a${index}`));
    const bMessages = [message('b1', 'b'), message('b2', 'b')];
    const p = props({ messages: aMessages });
    const { rerender } = render(<AgentChatSection {...p} />);
    const node = log();
    modelBrowserScroll(node);
    node.scrollTop = 620;
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={bMessages} />);
    node.scrollTop = 260;
    fireEvent.scroll(node);

    rerender(<AgentChatSection {...p} history={{ targetId: 'a', status: 'loading' }} messages={[]} />);
    expect(node.scrollTop).toBe(0);
    fireEvent.scroll(node); // Browser scroll event after removing the prior target's content.
    rerender(<AgentChatSection {...p} history={{ targetId: 'a', status: 'loading' }} messages={aMessages.slice(0, 1)} />);
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} />);
    expect(node.scrollTop).toBe(620);
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} messages={[...aMessages, message('late')]} />);
    expect(node.scrollTop).toBe(620);
    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={bMessages} />);
    expect(node.scrollTop).toBe(260);
  });

  it('keeps pending restoration with its target while another target follows the bottom', () => {
    const aMessages = [message('a1'), message('a2'), message('a3')];
    const bMessages = [message('b1', 'b'), message('b2', 'b')];
    const p = props({ messages: aMessages });
    const { rerender } = render(<AgentChatSection {...p} />);
    const node = log();
    modelBrowserScroll(node);
    node.scrollTop = 620;
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={bMessages} />);
    expect(node.scrollTop).toBe(600);
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} history={{ targetId: 'a', status: 'loading' }} messages={[]} />);
    expect(node.scrollTop).toBe(0);
    fireEvent.scroll(node);

    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={[...bMessages, message('b3', 'b')]} />);
    expect(node.scrollTop).toBe(1000);
    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={[...bMessages, message('b3', 'b'), message('b4', 'b')]} />);
    expect(node.scrollTop).toBe(1400);
    rerender(<AgentChatSection {...p} />);
    expect(node.scrollTop).toBe(620);
  });

  it.each(['wheel', 'touchMove', 'pointerDown', 'keyDown'] as const)('lets %s navigation replace a pending restoration during partial loading', (input) => {
    const aMessages = [message('a1'), message('a2'), message('a3')];
    const p = props({ messages: aMessages });
    const { rerender } = render(<AgentChatSection {...p} />);
    const node = log();
    modelBrowserScroll(node);
    node.scrollTop = 620;
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={[message('b1', 'b')]} />);
    rerender(<AgentChatSection {...p} history={{ targetId: 'a', status: 'loading' }} messages={[]} />);
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} history={{ targetId: 'a', status: 'loading' }} messages={aMessages.slice(0, 1)} />);
    if (input === 'wheel') fireEvent.wheel(node, { deltaY: 100 });
    else if (input === 'keyDown') fireEvent.keyDown(node, { key: 'ArrowDown' });
    else fireEvent[input](node);
    node.scrollTop = 100;
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} />);
    expect(node.scrollTop).toBe(100);
    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={[message('b1', 'b')]} />);
    rerender(<AgentChatSection {...p} />);
    expect(node.scrollTop).toBe(100);
  });

  it.each(['ready', 'error'] as const)('settles an unavailable saved offset when matching history is %s and resumes recording scroll', (status) => {
    const aMessages = [message('a1'), message('a2'), message('a3')];
    const p = props({ messages: aMessages });
    const { rerender } = render(<AgentChatSection {...p} />);
    const node = log();
    modelBrowserScroll(node);
    node.scrollTop = 620;
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} activeTargetId="b" history={{ targetId: 'b', status: 'ready' }} messages={[message('b1', 'b')]} />);
    rerender(<AgentChatSection {...p} history={{ targetId: 'a', status: 'loading' }} messages={[]} />);
    expect(node.scrollTop).toBe(0);
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} history={{ targetId: 'a', status }} messages={aMessages.slice(0, 1)} />);
    expect(node.scrollTop).toBe(200);
    fireEvent.scroll(node);
    node.scrollTop = 100;
    fireEvent.scroll(node);
    rerender(<AgentChatSection {...p} />);
    expect(node.scrollTop).toBe(100);
  });

  it('projects info controls and returns focus to the toggle when the controller closes the panel', async () => {
    const p = props({ infoExpanded: false });
    function Controller() {
      const [expanded, setExpanded] = useState(false);
      return <AgentChatSection {...p} infoExpanded={expanded} onInfoExpandedChange={(value) => {
        p.onInfoExpandedChange(value); setExpanded(value);
      }} />;
    }
    render(<Controller />);
    const toggle = screen.getByRole('button', { name: '信息' });
    expect(toggle).toHaveAttribute('aria-expanded', 'false');
    await userEvent.click(toggle);
    expect(p.onInfoExpandedChange).toHaveBeenLastCalledWith(true);
    const info = screen.getByRole('complementary', { name: '对象信息' });
    expect(toggle).toHaveAttribute('aria-controls', info.id);
    await userEvent.click(within(info).getByRole('button', { name: '收起信息' }));
    expect(p.onInfoExpandedChange).toHaveBeenLastCalledWith(false);
    expect(screen.queryByRole('complementary', { name: '对象信息' })).not.toBeInTheDocument();
    expect(toggle).toHaveFocus();
  });

  it('does not repeat a target URI as the visible role and keeps the full address in object information', () => {
    const address = 'agent:fixture@example.test:fixture-node:squire';
    const p = props({
      targets: [{ id: address, kind: 'agent', displayName: 'Squire', role: address, availability: 'available', avatarTone: 'green', capabilities: [] }],
      activeTargetId: address,
      history: { targetId: address, status: 'ready' },
    });
    render(<AgentChatSection {...p} />);
    const directory = screen.getByLabelText('聊天对象');
    expect(within(directory).queryByText(address)).not.toBeInTheDocument();
    expect(within(directory).getByText('Squire')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'Squire' })).toBeInTheDocument();
    const info = screen.getByRole('complementary', { name: '对象信息' });
    expect(within(info).getByText('地址')).toBeInTheDocument();
    expect(within(info).getByText(address)).toBeInTheDocument();
  });

  it('labels delivery on the user message only', () => {
    render(<AgentChatSection {...props({ messages: [
      { id: 'u', targetId: 'a', author: 'user', text: '问题', delivery: 'sent' },
      { id: 'r', targetId: 'a', author: 'assistant', text: '回答', delivery: 'sent' },
    ] })} />);
    const [question, answer] = within(log()).getAllByRole('article');
    expect(within(question).getByText('已发送')).toBeInTheDocument();
    expect(within(answer).queryByText('已发送')).not.toBeInTheDocument();
  });

  it('keeps conversation actions inside the conversation they act on', () => {
    render(<AgentChatSection {...props({ conversationToolbar: <button type="button">刷新消息</button> })} />);
    const conversation = screen.getByRole('region', { name: '研究者' });
    expect(within(conversation).getByRole('button', { name: '刷新消息' })).toBeInTheDocument();
  });

  it('renders heading actions before the info toggle', () => {
    render(<AgentChatSection {...props({ conversationActions: <button type="button">刷新消息</button> })} />);
    const heading = screen.getByRole('heading', { level: 2, name: '研究者' }).closest('.chat-workspace__heading');
    expect(heading).not.toBeNull();
    const refresh = within(heading as HTMLElement).getByRole('button', { name: '刷新消息' });
    const info = within(heading as HTMLElement).getByRole('button', { name: '信息' });
    expect(refresh.compareDocumentPosition(info) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});
