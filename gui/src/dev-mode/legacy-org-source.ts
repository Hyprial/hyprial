import type { OrgDomainSource } from '../features/org/domain/context';
import { UNWIRED_SOURCE } from '../features/org/domain/context';
import { UNWIRED_SNAPSHOT, createUnwiredGateway } from '../features/org/domain/unwired';
import type { OrgDetail, OrgResult, OrgSummary } from '../features/org/domain/types';
import { sessionReducer } from './session/session-reducer';
import type { LinkedSession, SessionAction } from './session/session-types';
import { ORG_FAILURE_MESSAGE, type OrgActionOutcome, type OrgViewModel } from '../pages/org/org-view-model';

const ID = 'preview-linked-org';
const summary = (name: string, owner: boolean, id = ID): OrgSummary => ({
  id, name, kind: 'work', role: owner ? 'owner' : 'member', lifecycle: 'active',
  ownerLabel: '模拟所有者', authorityDevice: false,
});
const emptyDetail: OrgDetail = { people: [], agents: null, machines: null, spaces: null,
  files: {}, trash: {}, history: {}, config: null, routes: null, stats: null, updatedAt: null };

/** Translate old J5 scenario data into the shared organization domain, never a second page. */
export function legacyOrgSource(model: OrgViewModel | undefined, dispatch?: (action: SessionAction) => void, validate?: (action: SessionAction) => string | undefined, orgName?: string, getSession?: () => LinkedSession | undefined): OrgDomainSource {
  if (!model || (model.scene === 'unavailable' && Object.values(model.actions).every(value => value === 'unavailable'))) return UNWIRED_SOURCE;
  const linked = getSession?.();
  const id = linked?.org?.id ?? ID;
  const org = summary(orgName ?? '演示组织', model.showInvite, id);
  const nextSession = (action: SessionAction) => {
    const current = getSession?.();
    return current ? sessionReducer(current, action) : undefined;
  };
  const detail: OrgDetail = { ...emptyDetail, people: model.members.map(member => ({
    id: member.id, displayName: member.name, account: '', role: 'member', source: '模拟数据',
    sync: 'unknown', isMe: false, machineIds: [], agentIds: [],
  })) };
  async function outcome<T>(plan: OrgActionOutcome, message: string, action: () => T): Promise<OrgResult<T>> {
    if (plan === 'pending') return new Promise(() => undefined);
    if (plan === 'unavailable') return { kind: 'unavailable', message: '暂不可用' };
    if (plan === 'failure') return { kind: 'failed', message };
    return { kind: 'ok', value: action() };
  }
  return {
    initial: { ...UNWIRED_SNAPSHOT, status: 'ready', nodeOwner: model.showCreate,
      orgs: model.showMembers ? [org] : [], currentOrgId: model.showMembers ? id : null,
      details: model.showMembers ? { [id]: detail } : {}, receivedInvites: [] },
    createGateway: store => ({
      ...createUnwiredGateway(),
      async setCurrentOrg(orgId) {
        if (!store.get().orgs?.some(value => value.id === orgId)) return { kind: 'unavailable', message: '组织不存在' };
        store.update(state => ({ ...state, currentOrgId: orgId }));
        return { kind: 'ok', value: undefined };
      },
      async createOrg(name) {
        const error = validate?.({ type: 'org-create', name });
        if (error) return { kind: 'failed', message: error };
        return outcome(model.actions.create, ORG_FAILURE_MESSAGE.create, () => {
          const created = summary(name, true, nextSession({ type: 'org-create', name })?.org?.id);
          store.update(state => ({ ...state, orgs: [created], details: { [created.id]: emptyDetail } }));
          dispatch?.({ type: 'org-create', name }); return created;
        });
      },
      async createInvite() {
        const error = validate?.({ type: 'org-invite' });
        if (error) return { kind: 'failed', message: error };
        return outcome(model.actions.invite, ORG_FAILURE_MESSAGE.invite, () => {
          const code = nextSession({ type: 'org-invite' })?.org?.inviteCode;
          dispatch?.({ type: 'org-invite' });
          return { link: code ?? model.invitation.link ?? 'https://example.invalid/invite/preview',
            joinCommand: code ?? model.invitation.code ?? 'preview-only', expiresAt: '模拟数据', shortLink: null };
        });
      },
      async joinByLink(link, onStep) {
        const error = validate?.({ type: 'org-join', code: link });
        if (error) return { kind: 'failed', message: error };
        return outcome(model.actions.join, ORG_FAILURE_MESSAGE.join, () => {
          for (const step of ['connect', 'join', 'register', 'peers'] as const) onStep(step);
          const next = nextSession({ type: 'org-join', code: link });
          const joined = summary(next?.org?.name ?? '演示组织', false, next?.org?.id);
          const joinedDetail = next ? { ...detail, people: next.members.map(member => ({ id: member.id, displayName: member.name, account: '', role: member.role, source: '模拟数据', sync: 'unknown' as const, isMe: member.identity === next.activeViewer, machineIds: [], agentIds: [] })) } : detail;
          store.update(state => ({ ...state, orgs: [joined], currentOrgId: joined.id, details: { [joined.id]: joinedDetail } }));
          dispatch?.({ type: 'org-join', code: link }); return joined;
        });
      },
    }),
  };
}
