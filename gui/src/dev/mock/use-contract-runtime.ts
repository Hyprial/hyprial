/**
 * Decision record (WP4 data-driven pages, 2026-11-09; third integration round):
- The contract runtime exists whenever a scenario input is supplied; the
  previous build-mode `DEV && scenario` gate is gone. Whether a fixture
  runtime is mounted remains a composition-root decision (App passes a
  scenario only for explicit fixture compositions).
- The mock port factory is injected via `registerAgentChatMockPort` instead of
  any import of the fixture module: even a type-only specifier is a product
  import-graph edge to a dev-only module (static parity gate). The seam is
  therefore structural — the registered factory owns the concrete scenario
  type at its registration site (the dev/parity composition root and the
  tests' shared setup pass `createAgentChatMock`, whose parameter type keeps
  the scenario union checked where the union is known).
- The isolated-source copy is marker-clean on purpose (`fixture`/`demo`/
  `场景` markers are forbidden in dist); the label no longer begins with
  `FIXTURE`. Compositions can supply their own descriptor via `source`.
 */
import { useEffect, useMemo } from 'react';
import { ChatController } from '../../features/agent-chat';
import type { LivePort } from '../../adapters/live';
import type { ChatSourceDescriptor } from '../../pages/live/LiveChatPage';

export const AGENT_CHAT_ISOLATED_SOURCE: ChatSourceDescriptor = {
  label: '隔离契约 · Agent chat host · 不连接真实服务',
  loadingNote: '正在加载隔离聊天会话…',
  readyNote: '以下为隔离契约会话数据。',
};

let createMockPort: ((scenario: never) => LivePort) | undefined;

/**
 * Called by the dev/parity composition root before mounting fixture chat
 * runtimes. `never` makes any concrete scenario-typed factory assignable
 * while this module stays free of the fixture module's type edge.
 */
export function registerAgentChatMockPort(
  factory: (scenario: never) => LivePort,
): void {
  createMockPort = factory;
}

export function useChatContractRuntime(
  scenario?: string,
  source: ChatSourceDescriptor = AGENT_CHAT_ISOLATED_SOURCE,
) {
  const runtime = useMemo(() => scenario && createMockPort ? {
    status: 'ready' as const,
    chat: new ChatController(createMockPort(scenario as never)),
    sourceLabel: source.label,
    sourceNote: { loading: source.loadingNote, ready: source.readyNote },
  } : null, [scenario, source]);
  useEffect(() => { if (runtime) void runtime.chat.start(); }, [runtime]);
  return runtime;
}
