import { SECRET_PROVIDER_ENVIRONMENTS } from '../../features/connections';
import type {
  CredentialEntry,
  SharedCredentialWrite,
  SharedCredentialsApi,
  SharedGrantRow,
} from '../../features/connections';
const DEFAULT_ENTRIES: readonly CredentialEntry[] = [
  {
    keyId: 'deepseek:DEEPSEEK_API_KEY',
    provider: 'deepseek',
    label: 'DeepSeek',
    state: 'active',
    createdAt: 1_760_000_000_000,
    lastUsedAt: null,
    grants: [
      {
        agent: 'squire',
        agentId: 'a.mock-squire',
        grantId: 'grant.mock-squire-deepseek',
        provider: 'deepseek',
        state: 'active',
      },
    ],
  },
];

const DEFAULT_AUTH_GRANTS: readonly SharedGrantRow[] = [
  {
    grantId: 'grant.mock-claude-auth',
    actor: 'squire',
    source: 'agent-shared',
    entryId: 'claude',
    fieldName: 'CLAUDE_CODE_OAUTH_TOKEN',
    environments: ['CLAUDE_CODE_OAUTH_TOKEN'],
    ownerActor: 'squire',
    revision: 1,
  },
];

function saved(message: string): SharedCredentialWrite {
  return { status: 'saved', message };
}

export function createMockSharedCredentialsApi(seed: { entries?: readonly CredentialEntry[]; authGrants?: readonly SharedGrantRow[] } = {}): SharedCredentialsApi {
  let entries = (seed.entries ?? DEFAULT_ENTRIES).map(entry => ({
    ...entry,
    grants: entry.grants.map(grant => ({ ...grant })),
  }));
  let sequence = 0;
  return {
    async listKeys() {
      return entries.map(entry => ({ ...entry, grants: entry.grants.map(grant => ({ ...grant })) }));
    },
    async listSecretGrants() {
      return (seed.authGrants ?? DEFAULT_AUTH_GRANTS).map(grant => ({ ...grant }));
    },
    async testKey(request) {
      if (!request.model.trim() || !request.secret.trim()) throw new Error('请填写 Key 和测试模型。');
      return { kind: 'model', status: 200, ok: true, latencyMs: 0, simulated: true };
    },
    async addKey(provider, label) {
      sequence += 1;
      entries = [
        ...entries,
        {
          keyId: `mock-${sequence}:${SECRET_PROVIDER_ENVIRONMENTS[provider] ?? 'apiKey'}`,
          provider,
          label,
          state: 'active',
          createdAt: Date.now(),
          lastUsedAt: null,
          grants: [],
        },
      ];
      return saved('Key 已保存。');
    },
    async replaceKey(keyId) {
      if (!entries.some(entry => entry.keyId === keyId)) {
        return { status: 'failed', message: '凭据不存在。' };
      }
      return saved('Key 已更新。');
    },
    async revokeGrant(agent, grantId) {
      entries = entries.map(entry => ({
        ...entry,
        grants: entry.grants.filter(grant => !(grant.agent === agent && grant.grantId === grantId)),
      }));
      return saved('授权已撤销。');
    },
  };
}
