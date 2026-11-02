import { useEffect, useMemo } from 'react';
import { ChatController } from './chat-controller';
import { createAgentChatMock, type AgentChatScenario } from './mock';
export function useChatContractRuntime(scenario?: AgentChatScenario) {
  const runtime = useMemo(() => import.meta.env.DEV && scenario ? {
    status: 'ready' as const, chat: new ChatController(createAgentChatMock(scenario)),
    sourceLabel: 'FIXTURE · Agent chat host contract · 不连接真实服务',
  } : null, [scenario]);
  useEffect(() => { if (runtime) void runtime.chat.start(); }, [runtime]);
  return runtime;
}
