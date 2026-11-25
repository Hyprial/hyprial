/**
 * H-02 example feature adapter.
 *
 * This file is the boundary the reference frontend replaces its temporary
 * hand-written DTO adapter with. It consumes only generated types, keeps the
 * transport envelope out of components, and lets tests/dev choose a live or
 * mock port without changing the feature contract.
 */
import type { GeneratedClient } from './fixtures/generated';
import type {
  AgentListResult,
  AgentProviderDefaultResult,
  AgentProviderLogoutResult,
  AgentSecretListResult,
} from './fixtures/generated/types';

export interface AgentOption {
  readonly actor: string;
  readonly label: string;
}

export interface AgentGrant {
  readonly id: string;
  readonly source: string;
  readonly field: string;
  readonly environments: readonly string[];
}

export interface AgentProviderView {
  readonly agents: readonly AgentOption[];
  readonly defaultProvider: string | null;
  readonly grants: readonly AgentGrant[];
}

/** Business port: returns generated business results, never the HTTP envelope. */
export interface AgentProviderPort {
  list(): Promise<AgentListResult>;
  defaultProvider(actor: string): Promise<AgentProviderDefaultResult>;
  grants(actor: string): Promise<AgentSecretListResult>;
  logout(actor: string, provider: string): Promise<AgentProviderLogoutResult>;
}

export interface AgentProviderFeatureApi {
  load(actor: string): Promise<AgentProviderView>;
  logout(actor: string, provider: string): Promise<void>;
}

export function createLiveAgentProviderPort(client: GeneratedClient): AgentProviderPort {
  return {
    list: () => client.agent.list({}),
    // `agent.provider.default` is a mixed read/write method: omit `clear` for the read path.
    defaultProvider: (actor) => client.agent.provider.default({ actor }),
    grants: (actor) => client.agent.secret.list({ actor }),
    logout: (actor, provider) => client.agent.provider.logout({ actor, provider }),
  };
}

export function createMockAgentProviderPort(initial: AgentProviderView): AgentProviderPort {
  let view = initial;
  return {
    async list() {
      return { agents: view.agents.map((agent) => ({ actor: agent.actor, uri: agent.actor, name: agent.label })) };
    },
    async defaultProvider(actor) {
      return { actor, provider: view.defaultProvider };
    },
    async grants() {
      return {
        grants: view.grants.map((grant) => ({
          grantId: grant.id,
          source: grant.source,
          fieldName: grant.field,
          environmentNames: [...grant.environments],
        })),
      };
    },
    async logout(actor, provider) {
      view = { ...view, defaultProvider: view.defaultProvider === provider ? null : view.defaultProvider };
      return { actor, provider, revoked: true };
    },
  };
}

export function createAgentProviderFeatureApi(port: AgentProviderPort): AgentProviderFeatureApi {
  return {
    async load(actor) {
      const [agents, defaultProvider, grants] = await Promise.all([
        port.list(),
        port.defaultProvider(actor),
        port.grants(actor),
      ]);
      return {
        agents: agents.agents.map((agent) => ({ actor: agent.actor, label: agent.name ?? agent.actor })),
        defaultProvider: defaultProvider.provider,
        grants: grants.grants.map((grant) => ({
          id: grant.grantId,
          source: grant.source,
          field: grant.fieldName,
          environments: grant.environmentNames,
        })),
      };
    },
    async logout(actor, provider) {
      await port.logout(actor, provider);
    },
  };
}

export interface AgentProviderFeatureOptions {
  readonly mode: 'live' | 'mock';
  readonly client?: GeneratedClient;
  readonly mock?: AgentProviderView;
}

export function createAgentProviderFeature(options: AgentProviderFeatureOptions): AgentProviderFeatureApi {
  if (options.mode === 'live') {
    if (!options.client) throw new Error('live mode requires a generated client');
    return createAgentProviderFeatureApi(createLiveAgentProviderPort(options.client));
  }
  if (!options.mock) throw new Error('mock mode requires fixture data');
  return createAgentProviderFeatureApi(createMockAgentProviderPort(options.mock));
}
