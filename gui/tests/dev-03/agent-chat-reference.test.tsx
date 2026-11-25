import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import {
  AgentHistoryField,
  AgentTargetField,
} from '../../src/features/agent-chat';
import type { SessionTarget, ConversationMessage } from '../../src/state/gui-view-model';
const target: SessionTarget = { id: 'agent:a', kind: 'agent', displayName: 'Agent A', role: '研究', availability: 'available', avatarTone: 'green', capabilities: [] };
const message: ConversationMessage = { id: 'm1', targetId: 'agent:a', author: 'assistant', text: '确认收到' };
describe('dev-03 Agent chat fields', () => {
  it('projects target loading, empty, error and controlled selection', () => {
    const select = vi.fn();
    const field = render(<AgentTargetField targets={[]} activeTargetId={null} onValueChange={select} state="loading" />);
    expect(screen.getByText('正在加载聊天对象')).toBeInTheDocument();
    field.rerender(<AgentTargetField targets={[]} activeTargetId={null} onValueChange={select} state="ready" />);
    expect(screen.getByText('暂无聊天对象')).toBeInTheDocument();
    field.rerender(<AgentTargetField targets={[]} activeTargetId={null} onValueChange={select} state="error" error="目录不可用" />);
    expect(screen.getByRole('alert')).toHaveTextContent('目录不可用');
    field.rerender(<AgentTargetField targets={[target]} activeTargetId={null} onValueChange={select} />);
    fireEvent.click(screen.getByRole('button', { name: /Agent A/ }));
    expect(select).toHaveBeenCalledWith('agent:a');
    expect(screen.getByRole('button', { name: /Agent A/ })).toHaveAttribute('aria-pressed', 'false');
    field.rerender(<AgentTargetField targets={[target]} activeTargetId="agent:a" onValueChange={select} disabled />);
    expect(screen.getByRole('button', { name: /Agent A/ })).toBeDisabled();
  });
  it('projects history states without creating a second message store', () => {
    const field = render(<AgentHistoryField target={target} messages={[message]} history={{ status: 'loading' }} />);
    expect(screen.getByText('正在加载历史消息…')).toBeInTheDocument();
    expect(screen.getByText('确认收到')).toBeInTheDocument();
    field.rerender(<AgentHistoryField target={target} messages={[message]} history={{ status: 'error', error: '历史不可用', stale: true }} />);
    expect(screen.getByRole('alert')).toHaveTextContent('历史不可用');
    expect(screen.getByText('当前显示上次取得的消息。')).toBeInTheDocument();
    field.rerender(<AgentHistoryField target={target} messages={[]} history={{ status: 'ready' }} disabled />);
    expect(screen.getByText('暂无消息。')).toBeInTheDocument();
    expect(screen.getByRole('log')).toHaveAttribute('aria-disabled', 'true');
  });
});
