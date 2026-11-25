import type { MethodRefusal, RequestDiagnostics } from '../../../client';
import type {
  AgentConnectionView,
  AgentOrganization,
  AgentSeedRecord,
  ManagedAgentView,
} from '../../../state/agent-selectors';
import { buildManagedAgent, nextAgentId, nextAvatarTone } from '../../../state/agent-selectors';
import type { ConnectionSummary } from '../../../state/gui-view-model';

/**
 * Agent create / update / delete across one seam.
 *
 * There is no default gateway: a composition without write interface has no
 * gateway and the page does not offer the operation. Only a composition entry
 * (development preview or a test) may inject a gateway that resolves locally,
 * and it never reaches the network or browser storage. update and delete are
 * optional: a gateway that lacks one leaves its control unrendered. A call that
 * the catalog refuses before dispatch resolves as `refused` and carries the
 * machine refusal only; the page raises the notification.
 */
export type AgentCredentialPlan =
  | { readonly kind: 'none' }
  | { readonly kind: 'key'; readonly keyId: string; readonly provider: string }
  | {
      readonly kind: 'auth';
      readonly harness: string;
      readonly files: readonly { readonly filename: string; readonly contentBase64: string }[];
    };

export interface AgentDraft {
  readonly displayName: string;
  readonly role: string;
  readonly modelServiceId: string;
  readonly organizationId: string | null;
  /** Optional preferences mirroring `hyprial agent create` flags. */
  readonly provider?: string | null;
  readonly model?: string | null;
  readonly preferredHarness?: string | null;
  readonly cwd?: string | null;
  /** Creating a live Agent may apply one credential after the create returns. */
  readonly credential?: AgentCredentialPlan;
}

export type AgentMutationResult =
  | {
      readonly kind: 'created';
      readonly agent: ManagedAgentView;
      /** Canonical actor URI returned by the create authority, when available. */
      readonly actorUri?: string;
      /** False when the daemon reused an existing Agent by the same name. */
      readonly created?: boolean;
      /** Optional page notice; the default wording stays with the page. */
      readonly notice?: string;
    }
  | { readonly kind: 'updated'; readonly agent: ManagedAgentView }
  | { readonly kind: 'deleted'; readonly agentId: string }
  | { readonly kind: 'failed'; readonly message: string; readonly code?: string; readonly diagnostics?: RequestDiagnostics | null }
  | { readonly kind: 'unknown'; readonly message: string; readonly code?: string; readonly diagnostics?: RequestDiagnostics | null }
  | { readonly kind: 'refused'; readonly refusal: MethodRefusal };

export interface AgentMutationGateway {
  create(draft: AgentDraft): Promise<AgentMutationResult>;
  update?(agentId: string, draft: AgentDraft): Promise<AgentMutationResult>;
  delete?(agentId: string): Promise<AgentMutationResult>;
}

export const AGENT_UNKNOWN_NOTE = '请先核对列表或详情，再决定是否重试。';
export const AGENT_LOCAL_FAILURE = '本地测试失败，未修改 Agent。';

/** Delay so the pending state is observable; no result is produced here. */
export const AGENT_MUTATION_DELAY_MS = 250;

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
}: LocalGatewayOptions): Required<AgentMutationGateway> {
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
