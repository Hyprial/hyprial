import { legacyOrgSource } from '../../../src/dev/legacy-org-source';
import { buildOrgViewModel, type OrgActionOutcome } from '../../../src/pages/org/org-view-model';
import type { OrgDomainSnapshot } from '../../../src/features/org/impl/domain/types';

function harness(plan: OrgActionOutcome, scene: Parameters<typeof buildOrgViewModel>[0] = 'created-empty') {
  const dispatch = vi.fn();
  const source = legacyOrgSource(buildOrgViewModel(scene, { create: plan, invite: plan, join: plan }), dispatch)!;
  let snapshot: OrgDomainSnapshot = source.initial;
  const gateway = source.createGateway({ get: () => snapshot, update: fn => { snapshot = fn(snapshot); } });
  return { gateway, dispatch, get: () => snapshot };
}
describe('J5 scenarios now feed the shared organization domain', () => {
  it.each(['unavailable', 'failure'] as const)('does not fabricate state for %s actions', async plan => {
    const { gateway, dispatch, get } = harness(plan); const before = get();
    expect((await gateway.createOrg('design')).kind).toBe(plan === 'failure' ? 'failed' : 'unavailable');
    expect((await gateway.createInvite('preview-linked-org', 'person@example.invalid')).kind).toBe(plan === 'failure' ? 'failed' : 'unavailable');
    expect((await gateway.joinByLink('preview-code', () => undefined)).kind).toBe(plan === 'failure' ? 'failed' : 'unavailable');
    expect(get()).toBe(before); expect(dispatch).not.toHaveBeenCalled();
  });
  it('only changes the current organization after explicit selection', async () => {
    const { gateway, dispatch, get } = harness('success', 'join-failed');
    const result = await gateway.createOrg('design'); expect(result.kind).toBe('ok');
    expect(get().currentOrgId).toBeNull();
    expect(dispatch).toHaveBeenCalledWith({ type: 'org-create', name: 'design' });
    await gateway.setCurrentOrg!('preview-linked-org'); expect(get().currentOrgId).toBe('preview-linked-org');
  });
  it('projects invitations and joins into the existing linked session without network writes', async () => {
    const fetch = vi.fn(); vi.stubGlobal('fetch', fetch);
    try {
      const { gateway, dispatch } = harness('success');
      expect((await gateway.createInvite('preview-linked-org', 'person@example.invalid')).kind).toBe('ok');
      const progress = vi.fn(); expect((await gateway.joinByLink('preview-code', progress)).kind).toBe('ok');
      expect(progress.mock.calls.flat()).toEqual(['connect','join','register','peers']);
      expect(dispatch).toHaveBeenCalledWith({ type: 'org-invite' });
      expect(dispatch).toHaveBeenCalledWith({ type: 'org-join', code: 'preview-code' });
      expect(fetch).not.toHaveBeenCalled();
    } finally { vi.unstubAllGlobals(); }
  });
  it('keeps pending actions pending without fabricating success', async () => {
    const { gateway, dispatch } = harness('pending'); let settled = false;
    void gateway.createOrg('design').then(() => { settled = true; });
    await Promise.resolve(); expect(settled).toBe(false); expect(dispatch).not.toHaveBeenCalled();
  });
  it('maps only supplied members and keeps absent machine and credential data unknown', () => {
    const { get } = harness('failure', 'member-joined');
    const detail = get().details['preview-linked-org'];
    expect(detail.people?.map(p => p.displayName)).toEqual(['新成员一']);
    expect(detail.machines).toBeNull(); expect(detail.agents).toBeNull();
    expect(get().thisComputer).toBeNull();
  });
});
