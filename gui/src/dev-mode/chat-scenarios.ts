import { CHAT_FIXTURE_DIRECTORY, CHAT_FIXTURE_TARGET } from '../fixtures/chat-directory';
import type { AgentChatScenario } from '../features/agent-chat/api/mock';
import type { ChatSeed } from '../state/types';

/**
 * J3 development scenarios.
 *
 * Each M scenario seeds the chat journey with the contract fixture directory.
 * Messages and the sendable Agent exist in development data only; the product fixtures keep every Agent without
 * a connection, and the product entry never passes a seed.
 */
export const J3_SCENARIOS = [
  { value: 'baseline', label: '基线（无可用 Agent）' },
  { value: 'm06', label: 'M-06 需要连接模型' },
  { value: 'm07', label: 'M-07 发送失败' },
  { value: 'm08', label: 'M-08 无消息' },
  { value: 'm23', label: 'M-23 对话完成' },
  { value: 'contract-success', label: '聊天契约 · 成功' },
  { value: 'contract-empty', label: '聊天契约 · 空数据' },
  { value: 'contract-error', label: '聊天契约 · 读取错误' },
  { value: 'contract-unconfigured', label: '聊天契约 · 未配置' },
  { value: 'contract-unknown', label: '聊天契约 · 发送结果未知' },
  { value: 'contract-failure', label: '聊天契约 · 发送明确失败' },
] as const;

export type J3Scenario = (typeof J3_SCENARIOS)[number]['value'];

export function chatContractScenarioForJ3(scenario: J3Scenario): AgentChatScenario | undefined {
  switch (scenario) {
    case 'contract-success': return 'success';
    case 'contract-empty': return 'empty';
    case 'contract-error': return 'error';
    case 'contract-unconfigured': return 'unconfigured';
    case 'contract-unknown': return 'unknown';
    case 'contract-failure': return 'failure';
    default: return undefined;
  }
}

export function isJ3Scenario(value: string): value is J3Scenario {
  return J3_SCENARIOS.some((option) => option.value === value);
}

const REPLY_TEXT =
  '已经整理好今天的安排：先确认模型服务，再查看 Agent 状态。';

const M23_THREADS: NonNullable<ChatSeed['threads']> = {
  [CHAT_FIXTURE_TARGET]: [
    {
      id: 'seed-squire-1',
      targetId: CHAT_FIXTURE_TARGET,
      author: 'user' as const,
      text: '请把今天的安排整理一下。',
      delivery: 'sent' as const,
    },
    {
      id: 'seed-squire-2',
      targetId: CHAT_FIXTURE_TARGET,
      author: 'assistant' as const,
      text: REPLY_TEXT,
      delivery: 'sent' as const,
    },
  ],
  ...Object.fromEntries(CHAT_FIXTURE_DIRECTORY.slice(1).filter(target => target.status === 'online').map(target => [target.targetUri, [
    { id: `seed-${target.targetUri}-1`, targetId: target.targetUri, author: 'user' as const, text: `请${target.displayName}整理当前任务。`, delivery: 'sent' as const },
    { id: `seed-${target.targetUri}-2`, targetId: target.targetUri, author: 'assistant' as const, text: `FIXTURE · ${target.displayName} 已完成任务整理。`, delivery: 'sent' as const },
  ]])),
};

function directoryTargets(connected: boolean): NonNullable<ChatSeed['targets']> {
  const targets = CHAT_FIXTURE_DIRECTORY.map(target => ({
    id: target.targetUri,
    kind: 'agent' as const,
    displayName: target.displayName,
    role: target.targetUri,
    availability: connected && target.status === 'online' ? 'available' as const : 'needs-connection' as const,
    avatarTone: 'green' as const,
    capabilities: [],
    modelLabel: '',
  }));
  return [targets[0], ...targets.slice(1)];
}

const CHAT_DIRECTORY_SEED = {
  targets: directoryTargets(true),
  activeTargetId: CHAT_FIXTURE_TARGET,
} as const satisfies ChatSeed;

export function buildChatSeed(scenario: J3Scenario): ChatSeed | undefined {
  switch (scenario) {
    case 'm06':
      return { ...CHAT_DIRECTORY_SEED, targets: directoryTargets(false), threads: { [CHAT_FIXTURE_TARGET]: [] }, sendMode: 'unavailable' };
    case 'm07':
      return {
        ...CHAT_DIRECTORY_SEED,
        threads: { [CHAT_FIXTURE_TARGET]: [{ id: 'seed-failed-1', targetId: CHAT_FIXTURE_TARGET, author: 'user', text: '请帮我检查模型连接。', delivery: 'failed' }] },
        availableTargetId: CHAT_FIXTURE_TARGET,
        sendMode: 'fail',
        replyText: REPLY_TEXT,
      };
    case 'm08':
      return {
        ...CHAT_DIRECTORY_SEED,
        threads: { [CHAT_FIXTURE_TARGET]: [] },
        availableTargetId: CHAT_FIXTURE_TARGET,
        sendMode: 'reply',
        replyText: REPLY_TEXT,
      };
    case 'm23':
      return {
        ...CHAT_DIRECTORY_SEED,
        availableTargetId: CHAT_FIXTURE_TARGET,
        threads: M23_THREADS,
        sendMode: 'reply',
        replyText: REPLY_TEXT,
      };
    case 'baseline':
    default:
      return undefined;
  }
}
