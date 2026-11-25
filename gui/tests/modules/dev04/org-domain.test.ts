import { agentOrgs, orgDataReady } from '../../../src/features/org/impl/crosstab/agent-org';
import type { OrgStore } from '../../../src/features/org/impl/domain/context';
import { UNWIRED_SNAPSHOT, createUnwiredGateway } from '../../../src/features/org/impl/domain/unwired';
import {
  orgDisplayName,
  orgPermissions,
  pendingInvites,
  peopleFromMembers,
  sortOrgs,
  spaceWriteBlock,
} from '../../../src/features/org/impl/domain/selectors';

import type { OrgDomainSnapshot, OrgGateway } from '../../../src/features/org/impl/domain/types';
import { validateOrgName } from '../../../src/features/org/impl/domain/validation';
import { ORG_COPY, zh } from '../../../src/features/org/impl/domain/copy';
import { createFixtureGateway } from '../../../src/dev/org/fixture-gateway';
import { buildOrgDevPreview } from '../../../src/dev/org/scenarios';
import {
  ORG_PARTNER,
  ORG_TEAM,
  ownerSnapshot,
} from '../../../src/dev/org/fixture-data';

it('associates known Agent memberships with their owning organization member', () => {
  const people = peopleFromMembers({
    members: [{ user: 'user:alice' }],
    spaceOwner: 'user:alice',
    selfOwner: 'user:alice',
    devices: [],
    agents: [{ id: 'a.' + 'a'.repeat(26), ownerLabel: 'user:alice' }],
  });
  expect(people?.[0].agentIds).toEqual(['a.' + 'a'.repeat(26)]);
});

function memoryStore(initial: OrgDomainSnapshot): OrgStore {
  let current = initial;
  return {
    get: () => current,
    update: (next) => {
      current = next(current);
    },
  };
}

function fixture(initial = ownerSnapshot()): { store: OrgStore; gateway: OrgGateway } {
  const store = memoryStore(initial);
  return { store, gateway: createFixtureGateway(store, { latencyMs: 0 }) };
}

describe('组织领域：校验与选择器', () => {
  it('组织名按 B1 就地校验', () => {
    expect(validateOrgName('')).toBe('请填写组织名称');
    expect(validateOrgName('Team A')).toBe('只能使用小写字母、数字和连字符');
    expect(validateOrgName('a'.repeat(58))).toBe('最长 57 个字符');
    expect(validateOrgName('team-acl')).toBe('不能以 -acl 结尾');
    expect(validateOrgName('  team-1 ')).toBeNull();
  });

  it('工作组织按名称排序，显示名为组织名', () => {
    const snapshot = ownerSnapshot();
    const sorted = sortOrgs([...(snapshot.orgs ?? [])].reverse());
    expect(sorted.map((org) => org.id)).toEqual([ORG_PARTNER, ORG_TEAM]);
    expect(orgDisplayName(sorted[0])).toBe('acme-lab');
  });

  it('按钮可见性只取后端给的角色与 authority 标记', () => {
    const snapshot = ownerSnapshot();
    const [team, partner] = snapshot.orgs ?? [];
    expect(orgPermissions(team, snapshot)).toMatchObject({
      invite: true,
      configure: true,
      leave: true,
      leaveBlockedReason: '这台电脑是该组织的管理设备，不能直接离开组织。',
    });
    expect(orgPermissions(partner, snapshot)).toMatchObject({
      invite: false,
      configure: false,
      leave: true,
      leaveBlockedReason: null,
      adopt: true,
    });
  });

  it('空间写操作按空间状态给出禁用原因', () => {
    expect(spaceWriteBlock(null)).toBe('该空间状态未确认，刷新后再操作。');
    expect(spaceWriteBlock({ id: 's', name: 's', health: 'history-anomaly', writable: true })).toBe(
      '该空间的历史记录异常，写操作和历史暂不可用。',
    );
    expect(spaceWriteBlock({ id: 's', name: 's', health: 'healthy', writable: false })).toBe(
      '你没有该空间的写权限。',
    );
    expect(spaceWriteBlock({ id: 's', name: 's', health: 'healthy', writable: true })).toBeNull();
  });

  it('未接通时没有组织数据，Agent 页不显示组织标签', () => {
    expect(orgDataReady(true, UNWIRED_SNAPSHOT)).toBe(false);
    expect(orgDataReady(false, ownerSnapshot())).toBe(false);
    expect(agentOrgs(ownerSnapshot(), 'agent-writer')?.map((org) => org.id)).toEqual([
      ORG_TEAM,
      ORG_PARTNER,
    ]);
  });

  it('统计开发数据自洽：折线合计 = 发出合计 = 收到合计，Top 关系不超过合计', () => {
    const windows = ownerSnapshot().details[ORG_TEAM].stats ?? {};
    for (const stats of Object.values(windows)) {
      if (!stats) continue;
      const sum = (values: readonly number[]) => values.reduce((acc, value) => acc + value, 0);
      const total = sum(stats.hourly);
      expect(sum(stats.participants.map((item) => item.sent))).toBe(total);
      expect(sum(stats.participants.map((item) => item.received))).toBe(total);
      expect(sum(stats.topPairs.map((item) => item.count))).toBeLessThanOrEqual(total);
    }
  });

  it('待处理邀请不含数据源标为已跳过的邀请；未知时为 null', () => {
    const base = ownerSnapshot();
    const receivedInvites = [
      { id: 'a', orgName: 'x', inviterLabel: '周屿', acceptedAt: '10-29 09:00' },
      { id: 'b', orgName: 'x', inviterLabel: '林澄', acceptedAt: '10-29 10:00', skipReason: '本机已在该组织' },
    ];
    expect(pendingInvites({ ...base, receivedInvites })?.map((invite) => invite.id)).toEqual(['a']);
    expect(pendingInvites({ ...base, receivedInvites: null })).toBeNull();
  });

  it('句子里的名字：中文挨着中文不留空格，中英文、数字之间留一个空格', () => {
    expect(ORG_COPY.noSharedOrg('许沐')).toBe('你和许沐没有共同组织。');
    expect(ORG_COPY.noSharedOrg('Allen')).toBe('你和 Allen 没有共同组织。');
    expect(ORG_COPY.fixJoinOrg('许沐', 'Squire')).toBe('把 Squire 加入许沐所在的组织');
    expect(ORG_COPY.fixInvite('许沐')).toBe('邀请许沐加入组织');
    expect(ORG_COPY.policyDenied('hyprial-dev')).toBe('组织 hyprial-dev 的通信设置不允许这条消息。');
    expect(ORG_COPY.inviteReminder(3)).toBe('你有 3 条组织邀请');
    expect(zh`把 ${'运营执行'} 加入组织`).toBe('把运营执行加入组织');
    expect(zh`${'Squire'} 可以向 ${'许沐'} 发送。`).toBe('Squire 可以向许沐发送。');
  });
});

describe('未接通网关', () => {
  it('每个动作立即返回不带文字的 unavailable，不发请求', async () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch');
    const gateway = createUnwiredGateway();
    await expect(gateway.createOrg('team')).resolves.toMatchObject({ kind: 'unavailable' });
    await expect(gateway.createInvite('org', 'a@b')).resolves.toMatchObject({ kind: 'unavailable' });
    const steps: string[] = [];
    await expect(gateway.joinByLink('link', (step) => steps.push(step))).resolves.toMatchObject({
      kind: 'unavailable',
    });
    expect(steps).toEqual([]);
    const result = await gateway.removeMember('org', 'p', 1);
    expect(result).toEqual({ kind: 'unavailable' });
    expect(fetchSpy).not.toHaveBeenCalled();
    fetchSpy.mockRestore();
  });
});

describe('组织开发数据网关（内存）', () => {
  it('新建组织：重名失败；成功后出现在列表中，成员只有自己', async () => {
    const { store, gateway } = fixture();
    await expect(gateway.createOrg('hyprial-dev')).resolves.toMatchObject({
      kind: 'failed',
      code: 'ORG_NAME_TAKEN',
    });
    const created = await gateway.createOrg('growth');
    expect(created).toMatchObject({ kind: 'ok', value: { id: 'org-growth', role: 'owner' } });
    expect(store.get().details['org-growth'].people).toHaveLength(1);
    expect(store.get().currentOrgId).toBe(ORG_TEAM);
  });

  it('领取邀请：成功、失败、跳过各自返回，不在本地推断成员', async () => {
    const { store, gateway } = fixture();
    await expect(gateway.claimInvite!('inv-ops')).resolves.toMatchObject({ kind: 'failed', code: 'version' });
    const joined = await gateway.claimInvite!('inv-design');
    expect(joined).toMatchObject({ kind: 'ok', value: { name: 'design-guild', role: 'member' } });
    expect(store.get().details['org-design-guild'].people).toBeNull();
    expect(store.get().receivedInvites?.map((invite) => invite.id)).toEqual(['inv-ops']);
  });

  it('生成邀请：有效期只给本地时间 hh:mm，“前”由界面拼接', async () => {
    const { gateway } = fixture();
    const result = await gateway.createInvite(ORG_TEAM, 'new@example.invalid');
    expect(result.kind === 'ok' && result.value.expiresAt).toMatch(/^\d{2}:\d{2}$/);
  });

  it('领取邀请成功后，同一组织的其他邀请由数据源标为已跳过', async () => {
    const preview = buildOrgDevPreview('invites');
    if (!preview) throw new Error('invites 情形缺失');
    const { store, gateway } = fixture(preview.source.initial);
    await gateway.claimInvite!('inv-design');
    const rest = store.get().receivedInvites ?? [];
    expect(rest.map((invite) => [invite.id, invite.skipReason])).toEqual([
      ['inv-ops', undefined],
      ['inv-design-2', '本机已在该组织'],
    ]);
  });

  it('用邀请链接加入：按四步上报进度；过期在第一步失败', async () => {
    const { gateway } = fixture();
    const steps: string[] = [];
    await expect(gateway.joinByLink('https://x/expired', (step) => steps.push(step))).resolves.toMatchObject({
      kind: 'failed',
      code: 'expired',
    });
    expect(steps).toEqual(['connect']);
    steps.length = 0;
    await expect(gateway.joinByLink('https://x/ok', (step) => steps.push(step))).resolves.toMatchObject({
      kind: 'ok',
    });
    expect(steps).toEqual(['connect', 'join', 'register', 'peers']);
  });

  it('移除成员：预览变化时要求重新确认；确认后只返回“已提交”', async () => {
    const { gateway } = fixture();
    const first = await gateway.previewRemoveMember(ORG_TEAM, 'p-chen');
    expect(first.kind).toBe('ok');
    const revision = first.kind === 'ok' ? first.value.revision : 0;
    await expect(gateway.removeMember(ORG_TEAM, 'p-chen', revision)).resolves.toMatchObject({
      kind: 'failed',
      code: 'stale',
    });
    const second = await gateway.previewRemoveMember(ORG_TEAM, 'p-chen');
    expect(second.kind === 'ok' && second.value.devices).toContain('chen-laptop');
    expect(first.kind === 'ok' && first.value.devices).not.toContain('chen-laptop');
    const next = second.kind === 'ok' ? second.value.revision : 0;
    await expect(gateway.removeMember(ORG_TEAM, 'p-chen', next)).resolves.toEqual({ kind: 'submitted' });
  });

  it('保存文件：对方先改过时返回冲突与对方版本；按新版本再保存成功', async () => {
    const { store, gateway } = fixture();
    const conflict = await gateway.saveFile(ORG_TEAM, 'space-docs', 'guides/onboarding.md', '我的修改', 3);
    expect(conflict).toMatchObject({ kind: 'failed', code: 'conflict', conflict: { theirVersion: 4 } });
    await expect(
      gateway.saveFile(ORG_TEAM, 'space-docs', 'guides/onboarding.md', '我的修改', 4),
    ).resolves.toEqual({ kind: 'ok', value: { version: 5, versionKey: '5' } });
    const saved = store.get().details[ORG_TEAM].files['space-docs'].find((item) => item.path === 'guides/onboarding.md');
    expect(saved).toMatchObject({ version: 5, content: '我的修改' });
  });

  it('文件：新建补齐上级目录，删除进回收站后文件可以恢复原位置，文件夹不恢复', async () => {
    const { store, gateway } = fixture();
    await gateway.createEntry(ORG_TEAM, 'space-docs', 'plans/q4/roadmap.md', 'file');
    const paths = () => store.get().details[ORG_TEAM].files['space-docs'].map((item) => item.path);
    expect(paths()).toEqual(expect.arrayContaining(['plans', 'plans/q4', 'plans/q4/roadmap.md']));
    expect(paths().indexOf('plans')).toBeLessThan(paths().indexOf('plans/q4/roadmap.md'));
    await gateway.deleteEntry(ORG_TEAM, 'space-docs', 'plans');
    expect(paths()).not.toContain('plans/q4/roadmap.md');
    const trashIds = store.get().details[ORG_TEAM].trash['space-docs'].map((item) => item.id);
    expect(new Set(trashIds).size).toBe(trashIds.length);
    const trashId = store.get().details[ORG_TEAM].trash['space-docs'][0].id;
    await expect(gateway.restoreEntry(ORG_TEAM, 'space-docs', trashId)).resolves.toMatchObject({
      kind: 'failed',
      code: 'ORG_FOLDER_RESTORE_UNSUPPORTED',
    });
    expect(paths()).not.toContain('plans');
    expect(store.get().details[ORG_TEAM].trash['space-docs'][0].id).toBe(trashId);
    await gateway.createEntry(ORG_TEAM, 'space-docs', 'notes.md', 'file');
    await gateway.deleteEntry(ORG_TEAM, 'space-docs', 'notes.md');
    const fileTrashId = store.get().details[ORG_TEAM].trash['space-docs'][0].id;
    await expect(gateway.restoreEntry(ORG_TEAM, 'space-docs', fileTrashId)).resolves.toEqual({ kind: 'ok', value: undefined });
    expect(paths()).toContain('notes.md');
  });

  it('配置：校验错误时不能发布；发布候选后采信成为当前版本', async () => {
    const { store, gateway } = fixture();
    const config = store.get().details[ORG_TEAM].config?.candidate?.config;
    if (!config) throw new Error('fixture candidate missing');
    const broken = { ...config, departments: [{ ...config.departments[0], name: '' }] };
    await expect(gateway.publishCandidate!(ORG_TEAM, broken)).resolves.toMatchObject({ kind: 'failed' });
    await gateway.publishCandidate!(ORG_TEAM, config);
    expect(store.get().details[ORG_TEAM].config?.candidate?.version).toBe(5);
    await expect(gateway.adoptCandidate!(ORG_TEAM, 4)).resolves.toMatchObject({ kind: 'failed' });
    await gateway.adoptCandidate!(ORG_TEAM, 5);
    expect(store.get().details[ORG_TEAM].config).toMatchObject({ adopted: { version: 5 }, candidate: null });
  });

  it('Agent 组织：允许移出最后一个已关联组织，不再返回 AGENT_LAST_ORG', async () => {
    const { store, gateway } = fixture();
    await expect(gateway.joinAgentOrg!('agent-ops', ORG_TEAM)).resolves.toMatchObject({ kind: 'unavailable' });
    await expect(gateway.leaveAgentOrg!('agent-ops', ORG_TEAM)).resolves.toEqual({ kind: 'ok', value: undefined });
    expect(store.get().agentOrgs?.['agent-ops']).toEqual([]);
    await gateway.leaveAgentOrg!('agent-writer', ORG_PARTNER);
    expect(store.get().agentOrgs?.['agent-writer']).toEqual([ORG_TEAM]);
  });

  it('迁移：发起后等目标方；刷新看到“可以开始”后才能开始', async () => {
    const { store, gateway } = fixture();
    const checks = await gateway.checkMigration!('agent-writer', 'm-yao-mini');
    expect(checks.kind === 'ok' && checks.value.find((check) => check.label === '目标机器在线')?.ok).toBe(false);
    const plan = await gateway.planMigration!('agent-writer', 'm-yao-mini');
    if (plan.kind !== 'ok') throw new Error('plan failed');
    expect(plan.value.items).toHaveLength(7);
    await gateway.submitMigration!(plan.value.id);
    expect(store.get().outboundMigrations['agent-writer'].status).toBe('awaiting-target');
    await expect(gateway.startMigration!(plan.value.id)).resolves.toMatchObject({ kind: 'failed' });
    await gateway.refresh(null);
    expect(store.get().outboundMigrations['agent-writer'].status).toBe('ready');
    await expect(gateway.startMigration!(plan.value.id)).resolves.toEqual({ kind: 'ok', value: undefined });
    expect(store.get().outboundMigrations['agent-writer'].status).toBe('running');
  });
});
