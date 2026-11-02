import type {
  AgentConnectionView,
  AgentOrganization,
  AgentSeedRecord,
  ManagedAgentView,
} from '../../state/agent-selectors';
import { buildManagedAgent, nextAgentId, nextAvatarTone } from '../../state/agent-selectors';
import type { ConnectionSummary } from '../../state/gui-view-model';

/**
 * Agent create / update / delete across one seam.
 *
 * The default gateway never succeeds: no write interface exists in this build.
 * Only a composition entry (development preview or a test) may inject a gateway
 * that resolves locally, and it never reaches the network or browser storage.
 */
export interface AgentDraft {
  readonly displayName: string;
  readonly role: string;
  readonly modelServiceId: string;
  readonly organizationId: string | null;
  /** Optional preferences mirroring `hyprial agent create` flags. */
  readonly provider?: string | null;
  readonly model?: string | null;
  readonly preferredHarness?: string | null;
}

export type AgentMutationResult =
  | { readonly kind: 'created'; readonly agent: ManagedAgentView }
  | { readonly kind: 'updated'; readonly agent: ManagedAgentView }
  | { readonly kind: 'deleted'; readonly agentId: string }
  | { readonly kind: 'failed'; readonly message: string }
  | { readonly kind: 'unknown'; readonly message: string }
  | { readonly kind: 'unavailable'; readonly message: string };

export interface AgentMutationGateway {
  create(draft: AgentDraft): Promise<AgentMutationResult>;
  update(agentId: string, draft: AgentDraft): Promise<AgentMutationResult>;
  delete(agentId: string): Promise<AgentMutationResult>;
}

export const AGENT_UNAVAILABLE = {
  create: 'Agent 创建暂不可用。',
  update: 'Agent 更新暂不可用。',
  delete: 'Agent 删除暂不可用。',
} as const;

export const AGENT_UNKNOWN_NOTE = '请先核对列表或详情，再决定是否重试。';
export const AGENT_LOCAL_FAILURE = '本地测试失败，未修改 Agent。';

/** Delay so the pending state is observable; no result is produced here. */
export const AGENT_MUTATION_DELAY_MS = 250;

export function createUnavailableAgentGateway(): AgentMutationGateway {
  return {
    async create() {
      return { kind: 'unavailable', message: AGENT_UNAVAILABLE.create };
    },
    async update() {
      return { kind: 'unavailable', message: AGENT_UNAVAILABLE.update };
    },
    async delete() {
      return { kind: 'unavailable', message: AGENT_UNAVAILABLE.delete };
    },
  };
}

interface LocalGatewayOptions {
  readonly agents: readonly ManagedAgentView[];
  readonly connections: readonly ConnectionSummary[];
  readonly organizations: readonly AgentOrganization[];
  /** When true every write resolves to a local failure. */
  readonly fail?: boolean;
  /** When true writes resolve as unknown instead of success. */
  readonly unknown?: boolean;
}

function recordFromView(
  agent: ManagedAgentView,
  displayName: string,
  role: string,
  modelServiceId: string,
  organizationId: string | null,
): AgentSeedRecord {
  return {
    id: agent.id,
    displayName,
    role,
    avatarTone: agent.avatarTone,
    modelServiceId,
    channelServiceId: agent.channelServiceId ?? undefined,
    organizationId,
    modelLabel: agent.modelLabel,
    capabilities: agent.capabilities,
  };
}

/**
 * In-memory gateway used by the development preview and by tests.
 *
 * It keeps the current Agent views in memory only. Nothing is written to
 * storage and nothing leaves the page.
 */
export function createLocalAgentGateway({
  agents,
  connections,
  organizations,
  fail = false,
  unknown = false,
}: LocalGatewayOptions): AgentMutationGateway {
  const views = new Map<string, ManagedAgentView>(
    agents.map((agent) => [agent.id, agent]),
  );

  const resolve = (
    result: AgentMutationResult,
  ): Promise<AgentMutationResult> =>
    new Promise((resolvePromise) => {
      window.setTimeout(() => resolvePromise(result), AGENT_MUTATION_DELAY_MS);
    });

  if (fail || unknown) {
    const message = unknown ? AGENT_UNKNOWN_NOTE : AGENT_LOCAL_FAILURE;
    const kind = unknown ? 'unknown' : 'failed';
    return {
      create: () => resolve({ kind, message } as AgentMutationResult),
      update: () => resolve({ kind, message } as AgentMutationResult),
      delete: () => resolve({ kind, message } as AgentMutationResult),
    };
  }

  return {
    create: async (draft) => {
      const existing = [...views.values()];
      const id = nextAgentId(existing);
      const modelService = connections.find(
        (connection) => connection.id === draft.modelServiceId,
      );
      const record: AgentSeedRecord = {
        id,
        displayName: draft.displayName,
        role: draft.role,
        avatarTone: nextAvatarTone(existing.length),
        modelServiceId: draft.modelServiceId,
        organizationId: draft.organizationId,
        modelLabel: modelService?.label ?? '未指定模型',
        capabilities: [],
      };
      const agent = buildManagedAgent(record, connections, organizations);
      views.set(agent.id, agent);
      return resolve({ kind: 'created', agent });
    },
    update: async (agentId, draft) => {
      const current = views.get(agentId);
      if (!current) {
        return resolve({ kind: 'unknown', message: AGENT_UNKNOWN_NOTE });
      }
      const modelService = connections.find(
        (connection) => connection.id === draft.modelServiceId,
      );
      const agent = buildManagedAgent(
        recordFromView(
          current,
          draft.displayName,
          draft.role,
          draft.modelServiceId,
          draft.organizationId,
        ),
        connections,
        organizations,
      );
      views.set(agent.id, {
        ...agent,
        modelLabel: modelService?.label ?? agent.modelLabel,
      });
      return resolve({ kind: 'updated', agent: views.get(agent.id) as ManagedAgentView });
    },
    delete: async (agentId) => {
      if (!views.has(agentId)) {
        return resolve({ kind: 'unknown', message: AGENT_UNKNOWN_NOTE });
      }
      views.delete(agentId);
      return resolve({ kind: 'deleted', agentId });
    },
  };
}

export function connectionSummaryLabel(
  connection: AgentConnectionView | undefined,
): string {
  return connection?.label ?? '状态未确认';
}
