/*
 * Decision record (PR 1f/F1 verification fixes, 2026-11-14):
 * - The formal composition is `App backend={connected}` over a backend
 *   connection whose served catalog lists methods. A test removes one method
 *   from the catalog and observes what a user sees: the catalog refuses it
 *   before dispatch, exactly as the real host does.
 * - Handlers answer only the reads and writes the Agent surfaces use; an
 *   unexpected method fails the test instead of returning invented data.
 */
import { render } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from '../../src/App';
import { createBackendConnection } from '../../src/app/backend';
import { AGENTS_LIST_ROWS_DATA } from '../fixtures/agents/agent-list';

export interface FormalCall {
  readonly method: string;
  readonly params: Record<string, unknown>;
}

const READS = [
  'onboarding.status', 'org.list', 'message.query', 'message.history', 'targets', 'hosts',
  'top.snapshot', 'agent.list', 'agent.provider.default',
  'agent.provider.auth.list', 'agent.secret.list', 'agent.secret.provider-list',
  'adapter.list', 'agent.cell.get',
];

const WRITES = [
  'agent.create', 'agent.start', 'agent.stop', 'agent.rename', 'agent.secret.grant',
  'agent.provider.login', 'agent.provider.logout', 'agent.cell.update',
];

const DESCRIPTOR = {
  interfaceVersion: 1,
  driver: 'contract-mock',
  phase: 'ready',
  generation: 'generation-1',
  catalogDigest: 'sha256:catalog',
} as const;

type Handler = (params: Record<string, unknown>) => unknown;

export interface FormalOptions {
  readonly omit?: readonly string[];
  readonly extraReads?: readonly string[];
  readonly handlers?: Readonly<Record<string, Handler>>;
}

function defaultResult(method: string, params: Record<string, unknown>): unknown {
  switch (method) {
    case 'onboarding.status': return {
      operations: [], nextActions: [], ok: true, phase: 'serving', authenticated: true,
      owner: 'test@example.invalid', deviceReady: true, initialized: true,
    };
    case 'org.list': return { orgs: [], leftOrgs: [] };
    case 'message.query': return { ok: true, view: params.view, actor: 'user:owner', entries: [] };
    case 'message.history': return { ok: true, conversationId: params.conversationId, entries: [], nextCursor: null };
    case 'targets': return { targets: [] };
    case 'hosts': return { hosts: [] };
    case 'top.snapshot': return { daemon: {}, quota: {}, actors: [], stranded: [] };
    case 'agent.list': return { ok: true, agents: AGENTS_LIST_ROWS_DATA, agentsWithoutRoutine: [] };
    case 'agent.provider.default': return { provider: null };
    case 'agent.provider.auth.list': return { ok: true, records: [] };
    case 'agent.secret.list': return { ok: true, grants: [] };
    case 'agent.secret.provider-list': return { ok: true, entries: [{ entryId: 'test-key', fieldNames: ['ANTHROPIC_API_KEY'] }] };
    case 'adapter.list': return { ok: true, adapters: [] };
    case 'agent.stop': return { ok: true, removed: [] };
    case 'agent.start': return { ok: true, accepted: true, running: true, warnings: [] };
    case 'agent.rename': return { ok: true, agent: { ...AGENTS_LIST_ROWS_DATA[1], displayName: String(params.displayName) } };
    case 'agent.secret.grant': return { ok: true, grant: { ...params, ownerActor: null, ownerEntityToken: null } };
    case 'agent.provider.login': return { ok: true, actor: params.actor, provider: params.provider, files: [] };
    case 'agent.provider.logout': return { ok: true };
    default: throw new Error(`unexpected method ${method}`);
  }
}

export function renderFormalAgents(options: FormalOptions = {}) {
  const omit = new Set(options.omit ?? []);
  const calls: FormalCall[] = [];
  const reads = [...READS, ...(options.extraReads ?? [])];
  const connection = createBackendConnection({
    async invoke<TResult>(method: string, params: object = {}): Promise<TResult> {
      const record = params as Record<string, unknown>;
      calls.push({ method, params: record });
      const handler = options.handlers?.[method];
      return (handler ? handler(record) : defaultResult(method, record)) as TResult;
    },
  }, {
    ...DESCRIPTOR,
    methods: [
      ...reads.filter((name) => !omit.has(name)).map((name) => ({
        name, callable: true, state: 'available', mutates: false,
      })),
      ...WRITES.filter((name) => !omit.has(name)).map((name) => ({
        name, callable: true, state: 'available', mutates: true,
      })),
    ],
  });
  const user = userEvent.setup();
  render(<App backend={{ status: 'connected', connection }} />);
  return { user, calls };
}

export function toastTexts(): (string | null)[] {
  return Array.from(document.querySelectorAll('.toast.toast-warning .toast-text'))
    .map((node) => node.textContent);
}

export function bodyTextWithoutToasts(): string {
  const clone = document.body.cloneNode(true) as HTMLElement;
  clone.querySelectorAll('.toast-host').forEach((node) => node.remove());
  return clone.textContent ?? '';
}
