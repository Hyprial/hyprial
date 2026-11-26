/**
 * Development-only Agent directory for the init preview.
 *
 * This legacy preview exposes only its implemented Agent capabilities.
 * Model binding previews use the canonical Contract Mock composition.
 *
 * It is an in-memory preview only: no transport, storage, production daemon or
 * daemon credential is touched. Explicitly selected Auth bytes are not retained by this mock.
 */

import { createGeneratedClient } from '../../client/generated';
import { DomainError } from '../../client';
import { createAgentsApi, createContractAgentGateway, type AgentsApi } from '../../features/agents';
import type { NativeAuthApi, NativeAuthRecord } from '../../features/agent-provider';
import type { FirstRunActions } from '../../features/onboarding';
import { createDemoActions } from '../demo-actions';
import { createMockSharedCredentialsApi } from './agent-settings';
import { syntheticProviderLoginFiles } from './agent-provider';

export interface MockAgentRow {
  readonly uri: string;
  readonly actor: string;
  displayName: string;
  readonly owner: string;
  readonly machine: string;
  readonly cwd: string;
  readonly provider: string | null;
  readonly model: string | null;
  readonly harness: string | null;
  readonly preferredHarness: string | null;
  readonly createdAtMs: number;
  readonly lastActiveAtMs: number | null;
  readonly activityHints: {
    readonly createdAtMs: number;
    readonly hasLastSessionId: boolean;
    readonly agentHomeMtimeMs: number;
  };
  readonly capabilities: Record<string, unknown>;
  readonly runState: Record<string, unknown>;
}

const OWNER = 'preview@example.invalid';
const MACHINE = 'dev-local';
const HOST = 'preview@example.invalid';
const NODE = 'dev-local';

function row(
  actor: string,
  displayName: string,
  state: string,
  provider: string | null = 'deepseek',
  model: string | null = 'deepseek-flash',
  preferredHarness: string | null = 'claude',
  cwd?: string,
): MockAgentRow {
  const createdAtMs = Date.now();
  return {
    uri: `agent:${HOST}:${NODE}:${actor}`,
    actor,
    displayName,
    owner: OWNER,
    machine: MACHINE,
    cwd: cwd || `/var/mock-agents/${actor}/workspace`,
    provider,
    model,
    harness: state === 'not-running' || state === 'unknown' ? null : preferredHarness,
    preferredHarness,
    createdAtMs,
    lastActiveAtMs: null,
    activityHints: {
      createdAtMs,
      hasLastSessionId: false,
      agentHomeMtimeMs: createdAtMs,
    },
    capabilities: {},
    runState: {
      state,
      certainty: state === 'unknown' ? 'unknown' : 'observed',
      observationHealth: state === 'unknown' ? 'unavailable' : 'current',
    },
  };
}


export interface StartupMockAgents {
  readonly nativeAuth: NativeAuthApi;
  readonly api: Omit<Required<AgentsApi>, 'modelStatus' | 'configureModel' | 'cancelModel' | 'nativeTargets'>;
  readonly remove: (actor: string) => void;
  readonly chatTargets: (kind: 'agent') => Promise<readonly {
    readonly targetUri: string;
    readonly targetKind: string;
    readonly deliverable: boolean;
    readonly actor: string | null;
    readonly displayName: string;
    readonly status: string;
  }[]>;
}

export interface CreatedStartupAgent {
  readonly uri: string;
  readonly displayName: string;
}

export function createStartupMockAgents(
  onCreated?: (agent: CreatedStartupAgent) => void,
  onChanged?: (agents: readonly MockAgentRow[]) => void,
): StartupMockAgents {
  let agents: MockAgentRow[] = [];
  const grants = new Map<string, Map<string, Record<string, unknown>>>();
  const nativeFiles = new Map<string, NativeAuthRecord>();

  const client = createGeneratedClient({
    async invoke<T>(method: string, params: Record<string, unknown>): Promise<T> {
      const selector = typeof params.agent === 'string'
        ? params.agent
        : typeof params.actor === 'string'
          ? params.actor
          : '';
      const actor = agents.find(agent => agent.actor === selector || agent.uri === selector)?.uri ?? selector;
      switch (method) {
        case 'agent.list':
          return {
            ok: true,
            agents: agents.map((agent) => ({ ...agent })),
            agentsWithoutRoutine: [],
          } as T;
        case 'agent.secret.list':
          return {
            ok: true,
            grants: [...(grants.get(actor)?.values() ?? [])],
          } as T;
        case 'agent.provider.default':
          return { ok: true, provider: 'deepseek', previous: null } as T;
        case 'agent.create': {
          const name = String(params.name ?? '');
          const existing = agents.find((agent) => agent.actor === name);
          if (existing) {
            return { ok: true, created: false, agent: { ...existing }, providerDefault: null } as T;
          }
          const created = row(
            name,
            name,
            'unknown',
            params.provider === null || params.provider === undefined ? null : String(params.provider),
            params.model === null || params.model === undefined ? null : String(params.model),
            params.preferredHarness === null || params.preferredHarness === undefined
              ? null
              : String(params.preferredHarness),
            typeof params.cwd === 'string' ? params.cwd : undefined,
          );
          agents = [...agents, created];
          onCreated?.({ uri: created.uri, displayName: created.displayName });
          onChanged?.(agents);
          return { ok: true, created: true, agent: { ...created }, providerDefault: null } as T;
        }
        case 'agent.rename': {
          const target = agents.find((agent) => agent.actor === params.name || agent.uri === params.name);
          if (!target) throw new Error('找不到该 Agent');
          const renamed = { ...target, displayName: String(params.displayName ?? target.displayName) };
          agents = agents.map(agent => agent.uri === target.uri ? renamed : agent);
          onChanged?.(agents);
          return { ok: true, agent: { ...renamed } } as T;
        }
        case 'agent.start': {
          const target = agents.find((agent) => agent.actor === params.name || agent.uri === params.name);
          if (target) {
            const harness = params.harness === null || params.harness === undefined
              ? target.preferredHarness ?? target.harness
              : String(params.harness);
            const sameHarness = harness === (target.capabilities.harness ?? target.preferredHarness);
            if (params.approve !== undefined) {
              if (harness !== 'pi' || typeof params.approve !== 'boolean') throw new Error('approve 仅用于 Pi。');
              if (!params.approve) throw new Error('请确认 Pi 工作目录。');
              if (typeof params.cwd !== 'string' || !params.cwd.trim()) throw new Error('请填写工作目录。');
            }
            if (target.harness !== null) throw new Error('Agent 已在运行，请先停止。');
            const provider = params.provider ?? (sameHarness ? target.provider : null);
            const model = params.model ?? (sameHarness ? target.model : null);
            if (harness === 'pi' && provider && !model) throw new Error('请设置模型后启动。');
            if (provider === 'hyprial' && (!model || !String(model).includes('/'))) {
              throw new Error('模型格式：服务商/模型。');
            }
            if (harness === 'codex' && provider === 'deepseek' && model && model !== 'deepseek-flash') {
              throw new Error('Codex 的 DeepSeek 模型仅支持 deepseek-flash。');
            }
            agents = agents.map((agent) => agent.actor === target.actor
              ? {
                  ...agent,
                  harness,
                  capabilities: { ...agent.capabilities, harness },
                  provider: provider === null ? null : String(provider),
                  model: model === null ? null : String(model),
                  cwd: typeof params.cwd === 'string' && params.cwd.trim() ? params.cwd : agent.cwd,
                  lastActiveAtMs: Date.now(),
                  runState: {
                    state: 'idle',
                    certainty: 'observed',
                    observationHealth: 'current',
                  },
                }
              : agent);
          }
          onChanged?.(agents);
          return { ok: true, accepted: true, running: true } as T;
        }
        case 'agent.stop': {
          const target = agents.find((agent) => agent.actor === params.name || agent.uri === params.name);
          if (target) {
            agents = agents.map((agent) => agent.actor === target.actor
              ? {
                  ...agent,
                  harness: null,
                  lastActiveAtMs: Date.now(),
                  runState: {
                    state: 'not-running',
                    certainty: 'observed',
                    observationHealth: 'current',
                  },
                }
              : agent);
          }
          onChanged?.(agents);
          return { ok: true, removed: target ? [`claude:${target.actor}`] : [] } as T;
        }
        case 'agent.secret.grant': {
          const grantId = String(params.grantId);
          const rows = grants.get(actor) ?? new Map<string, Record<string, unknown>>();
          const grant = { ...params, actor, ownerActor: null, ownerEntityToken: null };
          rows.set(grantId, grant);
          grants.set(actor, rows);
          return { ok: true, grant } as T;
        }
        case 'agent.secret.revoke':
          return { ok: true, revoked: grants.get(actor)?.delete(String(params.grantId)) ?? false } as T;
        case 'agent.provider.login': {
          const files = Array.isArray(params.files) ? params.files : [];
          const key = `${actor}:${String(params.provider)}`;
          const previous = nativeFiles.get(key);
          if (previous && !params.force) {
            throw new DomainError('PROVIDER_CREDENTIAL_EXISTS', '已有 Auth；请确认替换。', null, {
              method, requestId: 'preview-auth', status: 500, dispatch: 'dispatched',
              catalogDigest: null, attempts: 1, startedAt: 0, durationMs: 0,
            });
          }
          const target = agents.find(agent => agent.uri === actor);
          if (!target) throw new Error('Agent 不存在。');
          nativeFiles.set(key, {
            actor, displayName: target.displayName, harness: String(params.provider),
            files: files.map(file => String((file as Record<string, unknown>).filename)),
            state: 'stored', missingFiles: [], errorCode: null,
            importedAt: Date.now(), startupConfirmedAt: null,
          });
          return {
            ok: true,
            actor,
            provider: params.provider,
            files: files.map((file) => {
              const entry = file as Record<string, unknown>;
              return {
                path: `/var/mock-agents/${actor}/secrets/native/${String(params.provider)}/${String(entry.filename)}`,
                bytes: 2,
                replaced: false,
                unchanged: previous?.files.includes(String(entry.filename)) ?? false,
              };
            }),
          } as T;
        }
        case 'agent.provider.logout': {
          const key = `${actor}:${String(params.provider)}`;
          const removed = nativeFiles.get(key)?.files ?? [];
          nativeFiles.delete(key);
          return {
            ok: true,
            actor,
            provider: params.provider,
            removed,
            absent: [],
          } as T;
        }
        case 'routine.list':
          return { ok: true, routines: [] } as T;
        case 'routine.status':
          return {
            ok: true,
            name: params.name,
            agent: actor,
            state: 'idle',
            outcomes: [],
            scheduleEvents: [],
          } as T;
        case 'workflow.list':
          return { ok: true, runs: [] } as T;
        case 'agent.home-census': {
          const target = agents.find((agent) => agent.actor === actor || agent.uri === actor);
          const census = target === undefined ? [] : [{
            actor: target.actor,
            uri: target.uri,
            preferredHarness: target.preferredHarness,
            lastHarness: target.harness,
            harness: target.harness,
            homeProvisioned: true,
            explicitConfig: false,
            configInsideHome: false,
            cwdInsideHome: true,
            residentOrDsh: false,
            transferReceived: false,
            mode: 'p2',
            legacyReason: null,
          }];
          return {
            ok: true,
            readAtMs: Date.now(),
            registry: { path: '/var/mock-registry.json', version: 1 },
            agents: census,
            totals: { agents: census.length, byModeHarness: [], homeUnprovisioned: 0 },
          } as T;
        }
        default:
          throw new Error(`startup mock: unexpected method ${method}`);
      }
    },
  });
  const { modelStatus, configureModel, cancelModel, nativeTargets, ...api } = createAgentsApi(client);
  return {
    api,
    nativeAuth: {
      async list(selector) {
        return [...nativeFiles.values()].flatMap(record => {
          const agent = agents.find(row => row.uri === record.actor);
          if (!agent || (selector && selector !== agent.uri && selector !== agent.actor)) return [];
          return [{ ...record, files: [...record.files], displayName: agent.displayName }];
        });
      },
    },
    remove(selector) {
      const target = agents.find(agent => agent.actor === selector || agent.uri === selector);
      if (!target) throw new Error('Agent 不存在。');
      agents = agents.filter(agent => agent.uri !== target.uri);
      for (const actor of [target.uri, target.actor]) {
        grants.delete(actor);
        for (const harness of ['claude', 'codex', 'pi', 'agy']) nativeFiles.delete(`${actor}:${harness}`);
      }
      onChanged?.(agents);
    },
    chatTargets: async (kind) => kind === 'agent'
      ? agents.map((agent) => ({
          targetUri: agent.uri,
          targetKind: 'agent',
          deliverable: true,
          actor: agent.actor,
          displayName: agent.displayName,
          status: agent.harness === null ? 'offline' : 'online',
        }))
      : [],
  };
}

/** First-run preview composition built from the mock Agent authority and existing fixture APIs. */
export function createRuntimeDemoActions(): FirstRunActions {
  const runtime = createStartupMockAgents();
  const gateway = createContractAgentGateway({ api: runtime.api });
  return {
    ...createDemoActions(),
    runtimeCredentials: createMockSharedCredentialsApi(),
    pickRuntimeLoginFiles: async () => syntheticProviderLoginFiles(),
    async configureAgent(draft) {
      const created = await gateway.create(draft);
      if (created.kind !== 'created' || created.created === false || created.actorUri === undefined) {
        return { status: 'failed', message: '隔离预览：配置未完成。' };
      }
      const credential = draft.credential;
      if (credential?.kind === 'key') {
        await runtime.api.allowProvider(created.actorUri, credential.provider, {
          keyId: credential.keyId, grantId: `model-${credential.provider}`, revision: 1,
        });
      } else if (credential?.kind === 'auth') {
        await runtime.api.loginNative(created.actorUri, credential.harness, credential.files);
      }
      return { status: 'configured', actorUri: created.actorUri, created: true,
        message: `隔离预览：${draft.displayName} 的配置已记录，没有创建真实 Agent。` };
    },
  };
}
