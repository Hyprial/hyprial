/**
 * 组织开发数据的动作实现（只供开发定位层与测试使用）。
 *
 * 所有结果都写在内存快照里，带可调的延迟，用来走通进行中、成功、失败、冲突、
 * 过期等界面状态。它不是后端：不发请求，不保存任何数据，刷新页面即恢复初始值。
 */
import type { OrgStore } from '../domain/context';
import { JOIN_FAILURES, MACHINE_STATUS_LABELS, ORG_COPY } from '../domain/copy';
import type {
  ConfigValidation,
  MigrationTicket,
  OrgConfig,
  OrgDetail,
  OrgDomainSnapshot,
  OrgFileEntry,
  OrgGateway,
  OrgResult,
  OrgSummary,
  RemovalPreview,
} from '../domain/types';
import { PLAN_FOR, TEMPLATES, joinedOrgDetail, newOrgDetail } from './fixture-data';

export interface FixtureGatewayOptions {
  /** 每个动作的基础延迟；测试传 0。 */
  readonly latencyMs?: number;
}

const ME_LABEL = '姚';

function pad(value: number): string {
  return String(value).padStart(2, '0');
}

function clock(): string {
  const now = new Date();
  return `${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

/** 目录按路径分段排序，保证文件夹排在自己的子项之前。 */
function sortEntries(entries: readonly OrgFileEntry[]): OrgFileEntry[] {
  return [...entries].sort((a, b) => {
    const left = a.path.split('/');
    const right = b.path.split('/');
    for (let i = 0; i < Math.min(left.length, right.length); i += 1) {
      if (left[i] !== right[i]) return left[i].localeCompare(right[i]);
    }
    return left.length - right.length;
  });
}

function validate(config: OrgConfig): ConfigValidation {
  const errors: string[] = [];
  const warnings: string[] = [];
  const names = new Set<string>();
  const ids = new Set(config.departments.map((dept) => dept.id));
  config.departments.forEach((dept, index) => {
    if (!dept.name.trim()) errors.push(`第 ${index + 1} 个部门缺少名称`);
    else if (names.has(dept.name)) errors.push(`部门名称重复：${dept.name}`);
    names.add(dept.name);
    if (dept.parent && !ids.has(dept.parent)) errors.push(`部门“${dept.name}”的上级不存在`);
    if (!dept.lead.trim()) warnings.push(`部门“${dept.name || index + 1}”没有负责人`);
  });
  config.routes.forEach((route, index) => {
    if (!route.target.trim()) errors.push(`第 ${index + 1} 条路由缺少目标`);
  });
  config.lines.forEach((line) => {
    if (!line.repo.trim()) warnings.push(`业务线“${line.name}”没有填写仓库`);
  });
  return { errors, warnings };
}

function diffConfig(before: OrgConfig | null | undefined, after: OrgConfig): string[] {
  if (!before) return ['首次设置组织配置'];
  const lines: string[] = [];
  const beforeDepts = new Map(before.departments.map((dept) => [dept.id, dept]));
  for (const dept of after.departments) {
    const old = beforeDepts.get(dept.id);
    if (!old) lines.push(`新增部门：${dept.name}`);
    else if (old.name !== dept.name || old.lead !== dept.lead) lines.push(`修改部门：${dept.name}`);
  }
  for (const dept of before.departments) {
    if (!after.departments.some((item) => item.id === dept.id)) lines.push(`删除部门：${dept.name}`);
  }
  if (after.routes.length !== before.routes.length) {
    lines.push(`路由：${before.routes.length} 条 → ${after.routes.length} 条`);
  }
  if (JSON.stringify(after.norms) !== JSON.stringify(before.norms)) lines.push('修改规范');
  return lines.length > 0 ? lines : ['与当前版本相同'];
}

function isConfig(value: unknown): value is OrgConfig {
  if (!value || typeof value !== 'object') return false;
  const record = value as Record<string, unknown>;
  return (
    Array.isArray(record.departments) &&
    Array.isArray(record.lines) &&
    Array.isArray(record.roles) &&
    Array.isArray(record.routes) &&
    typeof record.norms === 'object' &&
    record.norms !== null
  );
}

export function createFixtureGateway(
  store: OrgStore,
  options: FixtureGatewayOptions = {},
): OrgGateway {
  const latency = options.latencyMs ?? 450;
  const wait = (factor = 1) =>
    new Promise<void>((resolve) => setTimeout(resolve, latency * factor));
  const later = (factor: number, task: () => void) => {
    setTimeout(task, Math.max(latency, 1) * factor);
  };
  let seq = 0;
  const conflictOnce = new Set(['space-docs:guides/onboarding.md']);
  const staleOnce = new Set(['p-chen']);
  // 迁移中的迁入请求第一次取消没有送达，用来走通失败后重试。
  const cancelFailOnce = new Set(['mig-in-2']);
  const revisions = new Map<string, number>();
  const plans = new Map<string, MigrationTicket>();
  const trashed = new Map<string, readonly OrgFileEntry[]>();

  const snapshot = () => store.get();
  const update = (next: (current: OrgDomainSnapshot) => OrgDomainSnapshot) => store.update(next);
  const findOrg = (orgId: string) => snapshot().orgs?.find((org) => org.id === orgId) ?? null;
  const updateDetail = (orgId: string, next: (detail: OrgDetail) => OrgDetail) =>
    update((current) => {
      const detail = current.details[orgId];
      return detail ? { ...current, details: { ...current.details, [orgId]: next(detail) } } : current;
    });
  const updateFiles = (
    orgId: string,
    spaceId: string,
    next: (files: readonly OrgFileEntry[]) => readonly OrgFileEntry[],
  ) =>
    updateDetail(orgId, (detail) => ({
      ...detail,
      files: { ...detail.files, [spaceId]: sortEntries(next(detail.files[spaceId] ?? [])) },
      updatedAt: clock(),
    }));
  const addOrg = (summary: OrgSummary, detail: OrgDetail) =>
    update((current) => ({
      ...current,
      orgs: [...(current.orgs ?? []), summary],
      details: { ...current.details, [summary.id]: detail },
    }));
  const updateOrg = (orgId: string, next: (org: OrgSummary) => OrgSummary) =>
    update((current) => ({
      ...current,
      orgs: current.orgs?.map((org) => (org.id === orgId ? next(org) : org)) ?? null,
    }));
  const findMachine = (machineId: string) => {
    for (const detail of Object.values(snapshot().details)) {
      const found = detail.machines?.find((item) => item.id === machineId);
      if (found) return found;
    }
    return null;
  };
  const updateOutbound = (ticketId: string, next: (ticket: MigrationTicket) => MigrationTicket) =>
    update((current) => {
      const entry = Object.entries(current.outboundMigrations).find(([, ticket]) => ticket.id === ticketId);
      if (!entry) return current;
      return {
        ...current,
        outboundMigrations: { ...current.outboundMigrations, [entry[0]]: next(entry[1]) },
      };
    });
  const outbound = (ticketId: string) =>
    Object.values(snapshot().outboundMigrations).find((ticket) => ticket.id === ticketId) ?? null;

  function removalPreview(orgId: string, key: string, deviceHosts: readonly string[]): RemovalPreview {
    const org = findOrg(orgId);
    return {
      revision: revisions.get(key) ?? 7,
      devices: deviceHosts.length > 0 ? deviceHosts : ['（没有设备记录）'],
      tailnet: `${org?.name ?? orgId} 组织网络`,
      authority: org?.authorityDevice ? '这台电脑（authority）' : `${org?.ownerLabel ?? '所有者'} 的 authority 设备`,
      impact: '确认后先改组织文件，再撤销组织网络；待确认、待 authority 或网络失败都不等于已完成。',
    };
  }

  return {
    async setCurrentOrg(orgId) {
      await wait();
      if (!findOrg(orgId)) return { kind: 'failed', message: '组织不存在或已离开' };
      update((current) => ({ ...current, currentOrgId: orgId }));
      return { kind: 'ok', value: undefined };
    },

    async createOrg(name) {
      await wait(2);
      if (snapshot().orgs?.some((org) => org.name === name)) {
        return { kind: 'failed', code: 'ORG_NAME_TAKEN', message: '组织名已被占用，请换一个名称。' };
      }
      const summary: OrgSummary = {
        id: `org-${name}`,
        name,
        kind: 'work',
        role: 'owner',
        lifecycle: 'active',
        ownerLabel: '我',
        authorityDevice: true,
      };
      addOrg(summary, { ...newOrgDetail(), updatedAt: clock() });
      return { kind: 'ok', value: summary };
    },

    async previewRepair() {
      await wait();
      return { kind: 'ok', value: ['补齐组织配置空间（space-config）', '重新发布这台电脑的设备记录'] };
    },

    async repairOrg(orgId) {
      await wait(2);
      updateOrg(orgId, (org) => ({ ...org, lifecycle: 'active' }));
      updateDetail(orgId, (detail) => ({ ...detail, updatedAt: clock() }));
      return { kind: 'ok', value: undefined };
    },

    async createInvite(orgId, account) {
      await wait(2);
      if (account.includes('fail')) {
        return { kind: 'failed', message: '邀请服务没有响应，请稍后重试' };
      }
      seq += 1;
      const link = `https://invite.example.invalid/j/sim-${orgId.replace('org-', '')}-${seq}`;
      const expires = new Date(Date.now() + 3600_000);
      return {
        kind: 'ok',
        value: {
          link,
          joinCommand: `hyprial org join '${link}'`,
          expiresAt: `${pad(expires.getHours())}:${pad(expires.getMinutes())}`,
          shortLink: seq % 2 === 1 ? `https://s.example.invalid/${seq}x7` : null,
        },
      };
    },

    async claimInvite(inviteId) {
      await wait(2);
      const invite = snapshot().receivedInvites?.find((item) => item.id === inviteId);
      if (!invite) return { kind: 'failed', message: '邀请已不存在，请刷新' };
      if (inviteId === 'inv-ops') {
        return { kind: 'failed', code: 'version', message: '邀请版本过旧，请邀请人重新生成新版邀请' };
      }
      const existing = snapshot().orgs?.find((org) => org.name === invite.orgName);
      // 同一组织不重复加入：其余指向该组织的邀请标为已跳过（模拟后端的处理结果）。
      update((current) => ({
        ...current,
        receivedInvites:
          current.receivedInvites
            ?.filter((item) => item.id !== inviteId)
            .map((item) => (item.orgName === invite.orgName ? { ...item, skipReason: '本机已在该组织' } : item)) ??
          null,
      }));
      if (existing) return { kind: 'skipped', message: '本机已在该组织' };
      const summary: OrgSummary = {
        id: `org-${invite.orgName}`,
        name: invite.orgName,
        kind: 'work',
        role: 'member',
        lifecycle: 'active',
        ownerLabel: invite.inviterLabel,
        authorityDevice: false,
      };
      addOrg(summary, joinedOrgDetail());
      return { kind: 'ok', value: summary };
    },

    async joinByLink(link, onStep) {
      const fail = (code: keyof typeof JOIN_FAILURES): OrgResult<OrgSummary> => ({
        kind: 'failed',
        code,
        message: JOIN_FAILURES[code].reason,
      });
      onStep('connect');
      await wait(2);
      if (link.includes('expired')) return fail('expired');
      if (link.includes('removed')) return fail('removed');
      onStep('join');
      await wait(2);
      if (link.includes('timeout')) return fail('timeout');
      onStep('register');
      await wait(2);
      onStep('peers');
      await wait(2);
      if (snapshot().orgs?.some((org) => org.id === 'org-partner-net')) {
        return { kind: 'skipped', message: '已在该组织中，不重复加入' };
      }
      const summary: OrgSummary = {
        id: 'org-partner-net',
        name: 'partner-net',
        kind: 'work',
        role: 'member',
        lifecycle: 'active',
        ownerLabel: '许沐',
        authorityDevice: false,
      };
      addOrg(summary, joinedOrgDetail());
      return { kind: 'ok', value: summary };
    },

    async refresh(orgId) {
      await wait();
      update((current) => {
        const details = { ...current.details };
        for (const id of orgId ? [orgId] : Object.keys(details)) {
          if (details[id]) details[id] = { ...details[id], updatedAt: clock() };
        }
        // 目标方已在目标机器同意：源端刷新后看到“可以开始”。
        const outboundMigrations = Object.fromEntries(
          Object.entries(current.outboundMigrations).map(([agentId, ticket]) => [
            agentId,
            ticket.status === 'awaiting-target' ? { ...ticket, status: 'ready' as const } : ticket,
          ]),
        );
        return { ...current, details, outboundMigrations };
      });
      return { kind: 'ok', value: undefined };
    },

    async previewRemoveMember(orgId, personId) {
      await wait();
      const detail = snapshot().details[orgId];
      const target = detail?.people?.find((item) => item.id === personId);
      if (!target) return { kind: 'failed', message: '成员已不在组织中，请刷新' };
      const hosts = target.machineIds.map((id) => findMachine(id)?.hostname ?? id);
      // 确认前对方又接入了一台设备：新预览多出 chen-laptop，由界面对比两次预览标出。
      const extra = revisions.has(personId) ? ['chen-laptop'] : [];
      return { kind: 'ok', value: removalPreview(orgId, personId, [...hosts, ...extra]) };
    },

    async removeMember(orgId, personId, revision) {
      await wait(2);
      if (staleOnce.has(personId) || revision !== (revisions.get(personId) ?? 7)) {
        staleOnce.delete(personId);
        revisions.set(personId, (revisions.get(personId) ?? 7) + 1);
        return { kind: 'failed', code: 'stale', message: ORG_COPY.previewChanged };
      }
      later(6, () =>
        updateDetail(orgId, (detail) => ({
          ...detail,
          people: detail.people?.filter((item) => item.id !== personId) ?? null,
          updatedAt: clock(),
        })),
      );
      return { kind: 'submitted' };
    },

    async previewLeave(orgId) {
      await wait();
      const detail = snapshot().details[orgId];
      const me = detail?.people?.find((item) => item.isMe);
      const hosts = (me?.machineIds ?? []).map((id) => findMachine(id)?.hostname ?? id);
      return { kind: 'ok', value: removalPreview(orgId, `leave:${orgId}`, hosts) };
    },

    async leaveOrg(orgId, revision) {
      await wait(2);
      if (revision !== (revisions.get(`leave:${orgId}`) ?? 7)) {
        return { kind: 'failed', code: 'stale', message: ORG_COPY.previewChanged };
      }
      later(6, () =>
        update((current) => {
          const details = { ...current.details };
          delete details[orgId];
          const agentOrgs = current.agentOrgs
            ? Object.fromEntries(
                Object.entries(current.agentOrgs).map(([id, orgIds]) => [
                  id,
                  orgIds.filter((item) => item !== orgId),
                ]),
              )
            : null;
          const personal = current.orgs?.find((org) => org.kind === 'personal')?.id ?? null;
          return {
            ...current,
            orgs: current.orgs?.filter((org) => org.id !== orgId) ?? null,
            details,
            agentOrgs,
            currentOrgId: current.currentOrgId === orgId ? personal : current.currentOrgId,
          };
        }),
      );
      return { kind: 'submitted' };
    },

    async renameOrg(orgId, name) {
      await wait();
      if (snapshot().orgs?.some((org) => org.id !== orgId && org.name === name)) {
        return { kind: 'failed', message: '组织名已被占用，请换一个名称。' };
      }
      return { kind: 'submitted', message: '改名：已提交，等待处理' };
    },

    async listTemplates() {
      await wait(0.5);
      return { kind: 'ok', value: TEMPLATES };
    },

    async previewImport(orgId, source) {
      await wait();
      const adopted = snapshot().details[orgId]?.config?.adopted?.config;
      let config: OrgConfig;
      if ('templateId' in source) {
        const template = TEMPLATES.find((item) => item.id === source.templateId);
        if (!template) return { kind: 'failed', message: '模板不存在' };
        config = template.config;
      } else {
        let parsed: unknown;
        try {
          parsed = JSON.parse(source.text);
        } catch {
          parsed = null;
        }
        if (!isConfig(parsed)) {
          return {
            kind: 'failed',
            message: `${source.fileName} 不是有效的组织配置：需要 JSON，含 departments、lines、roles、routes、norms。`,
          };
        }
        config = parsed;
      }
      return { kind: 'ok', value: { config, diff: diffConfig(adopted, config), validation: validate(config) } };
    },

    async validateConfig(_orgId, config) {
      await wait(0.5);
      return { kind: 'ok', value: validate(config) };
    },

    async publishCandidate(orgId, config) {
      await wait(2);
      const validation = validate(config);
      if (validation.errors.length > 0) {
        return { kind: 'failed', message: `校验未通过：${validation.errors.join('；')}` };
      }
      updateDetail(orgId, (detail) => {
        const state = detail.config ?? { adopted: null, candidate: null, spaceId: 'space-config' };
        const version = Math.max(state.adopted?.version ?? 0, state.candidate?.version ?? 0) + 1;
        return {
          ...detail,
          config: {
            ...state,
            candidate: {
              version,
              publishedAt: clock(),
              publisher: ME_LABEL,
              config,
              diff: diffConfig(state.adopted?.config, config),
            },
          },
          updatedAt: clock(),
        };
      });
      return { kind: 'ok', value: undefined };
    },

    async adoptCandidate(orgId, version) {
      await wait(2);
      const state = snapshot().details[orgId]?.config;
      const candidate = state?.candidate;
      if (!state || !candidate || candidate.version !== version) {
        return { kind: 'failed', message: '候选版本已变化，请刷新后再采信。' };
      }
      updateDetail(orgId, (detail) => ({
        ...detail,
        config: {
          ...state,
          adopted: {
            version: candidate.version,
            publishedAt: candidate.publishedAt,
            publisher: candidate.publisher,
            config: candidate.config,
          },
          candidate: null,
        },
        routes: candidate.config.routes,
        updatedAt: clock(),
      }));
      return { kind: 'ok', value: undefined };
    },

    async saveFile(orgId, spaceId, path, content, baseVersion) {
      await wait();
      const key = `${spaceId}:${path}`;
      const entry = snapshot().details[orgId]?.files[spaceId]?.find((item) => item.path === path);
      if (!entry || entry.kind !== 'file') return { kind: 'failed', message: '文件已不存在，请刷新' };
      const record = (next: OrgFileEntry) => {
        updateFiles(orgId, spaceId, (files) => files.map((item) => (item.path === path ? next : item)));
        updateDetail(orgId, (detail) => ({
          ...detail,
          history: {
            ...detail.history,
            [key]: [
              {
                version: next.version,
                modifiedBy: next.modifiedBy,
                modifiedAt: next.modifiedAt,
                content: next.content ?? '',
              },
              ...(detail.history[key] ?? []),
            ],
          },
        }));
      };
      if (conflictOnce.has(key)) {
        // 保存前对方刚好改过同一文件：后端返回对方版本，由用户选择。
        conflictOnce.delete(key);
        const theirs = `${entry.content ?? ''}4. （林澄刚补充）加入 #gui-init 频道。\n`;
        record({ ...entry, content: theirs, version: entry.version + 1, modifiedBy: '林澄', modifiedAt: clock() });
        return {
          kind: 'failed',
          code: 'conflict',
          message: '保存前，林澄修改了同一文件。',
          conflict: { theirs, theirVersion: entry.version + 1 },
        };
      }
      if (entry.version !== baseVersion) {
        return {
          kind: 'failed',
          code: 'conflict',
          message: '文件已被他人修改。',
          conflict: { theirs: entry.content ?? '', theirVersion: entry.version },
        };
      }
      const version = entry.version + 1;
      record({ ...entry, content, version, modifiedBy: ME_LABEL, modifiedAt: clock() });
      return { kind: 'ok', value: version };
    },

    async createEntry(orgId, spaceId, path, kind) {
      await wait();
      const files = snapshot().details[orgId]?.files[spaceId] ?? [];
      if (files.some((item) => item.path === path)) return { kind: 'failed', message: '同名文件或文件夹已存在' };
      const parts = path.split('/');
      const parents = parts
        .slice(0, -1)
        .map((_, index) => parts.slice(0, index + 1).join('/'))
        .filter((parent) => !files.some((item) => item.path === parent));
      const make = (entryPath: string, entryKind: 'file' | 'folder'): OrgFileEntry => ({
        path: entryPath,
        name: entryPath.split('/').at(-1) ?? entryPath,
        kind: entryKind,
        modifiedBy: ME_LABEL,
        modifiedAt: clock(),
        version: 1,
        ...(entryKind === 'file' ? { content: '' } : {}),
      });
      updateFiles(orgId, spaceId, (current) => [
        ...current,
        ...parents.map((parent) => make(parent, 'folder')),
        make(path, kind),
      ]);
      return { kind: 'ok', value: undefined };
    },

    async moveEntry(orgId, spaceId, from, to) {
      await wait();
      const files = snapshot().details[orgId]?.files[spaceId] ?? [];
      if (to.startsWith(`${from}/`)) return { kind: 'failed', message: '不能把文件夹移到它自己里面' };
      if (files.some((item) => item.path === to)) return { kind: 'failed', message: '目标位置已有同名文件' };
      const parts = to.split('/');
      const parents = parts
        .slice(0, -1)
        .map((_, index) => parts.slice(0, index + 1).join('/'))
        .filter((parent) => !files.some((item) => item.path === parent));
      updateFiles(orgId, spaceId, (current) => [
        ...current.map((item) => {
          if (item.path !== from && !item.path.startsWith(`${from}/`)) return item;
          const path = `${to}${item.path.slice(from.length)}`;
          return { ...item, path, name: path.split('/').at(-1) ?? path, modifiedAt: clock() };
        }),
        ...parents.map((parent) => ({
          path: parent,
          name: parent.split('/').at(-1) ?? parent,
          kind: 'folder' as const,
          modifiedBy: ME_LABEL,
          modifiedAt: clock(),
          version: 1,
        })),
      ]);
      return { kind: 'ok', value: undefined };
    },

    async deleteEntry(orgId, spaceId, path) {
      await wait();
      const files = snapshot().details[orgId]?.files[spaceId] ?? [];
      const removed = files.filter((item) => item.path === path || item.path.startsWith(`${path}/`));
      const target = removed.find((item) => item.path === path);
      if (!target) return { kind: 'failed', message: '文件已不存在，请刷新' };
      seq += 1;
      // 与预置回收站条目（t-1 …）区分，避免列表 key 重复。
      const id = `t-sim-${seq}`;
      trashed.set(id, removed);
      updateFiles(orgId, spaceId, (current) => current.filter((item) => !removed.includes(item)));
      updateDetail(orgId, (detail) => ({
        ...detail,
        trash: {
          ...detail.trash,
          [spaceId]: [
            { id, path, deletedBy: ME_LABEL, deletedAt: clock(), kind: target.kind, childCount: removed.length - 1 },
            ...(detail.trash[spaceId] ?? []),
          ],
        },
      }));
      return { kind: 'ok', value: undefined };
    },

    async restoreEntry(orgId, spaceId, trashId) {
      await wait();
      const detail = snapshot().details[orgId];
      const item = detail?.trash[spaceId]?.find((entry) => entry.id === trashId);
      if (!item) return { kind: 'failed', message: '回收站中已没有这一项，请刷新' };
      if (detail?.files[spaceId]?.some((entry) => entry.path === item.path)) {
        return { kind: 'failed', message: '原位置已有同名文件，先移动或改名后再恢复。' };
      }
      const restored = trashed.get(trashId) ?? [
        {
          path: item.path,
          name: item.path.split('/').at(-1) ?? item.path,
          kind: 'file' as const,
          modifiedBy: item.deletedBy,
          modifiedAt: item.deletedAt,
          version: 1,
          content: '',
        },
      ];
      updateFiles(orgId, spaceId, (current) => [...current, ...restored]);
      updateDetail(orgId, (current) => ({
        ...current,
        trash: { ...current.trash, [spaceId]: (current.trash[spaceId] ?? []).filter((entry) => entry.id !== trashId) },
      }));
      return { kind: 'ok', value: undefined };
    },

    async purgeEntry(orgId, spaceId, trashId) {
      await wait();
      trashed.delete(trashId);
      updateDetail(orgId, (detail) => ({
        ...detail,
        trash: { ...detail.trash, [spaceId]: (detail.trash[spaceId] ?? []).filter((entry) => entry.id !== trashId) },
        updatedAt: clock(),
      }));
      return { kind: 'ok', value: undefined };
    },

    async retryMachine(orgId, machineId) {
      await wait(2);
      later(4, () =>
        updateDetail(orgId, (detail) => ({
          ...detail,
          machines:
            detail.machines?.map((item) =>
              item.id === machineId ? { ...item, status: 'online', dormant: undefined } : item,
            ) ?? null,
          updatedAt: clock(),
        })),
      );
      return { kind: 'submitted', message: '已发起重试，等待结果。' };
    },

    async decideMigration(ticketId, decision) {
      await wait(2);
      update((current) => ({
        ...current,
        inboundMigrations:
          current.inboundMigrations?.map((ticket) =>
            ticket.id === ticketId
              ? { ...ticket, status: decision === 'accept' ? 'verifying' : 'rejected' }
              : ticket,
          ) ?? null,
      }));
      return { kind: 'ok', value: undefined };
    },

    async addLink(agentId, peer) {
      await wait();
      const links = snapshot().agentLinks?.[agentId] ?? [];
      if (links.some((link) => link.peerLabel === peer)) {
        return { kind: 'failed', message: '已经可以向该对象发送' };
      }
      seq += 1;
      update((current) => ({
        ...current,
        agentLinks: {
          ...(current.agentLinks ?? {}),
          [agentId]: [
            ...links,
            { id: `lk-new-${seq}`, holderAgentId: agentId, peerLabel: peer, state: 'active', inFlight: 0, lastUsedAt: null },
          ],
        },
      }));
      return { kind: 'ok', value: undefined };
    },

    async revokeLink(agentId, linkId) {
      await wait();
      update((current) => ({
        ...current,
        agentLinks: {
          ...(current.agentLinks ?? {}),
          [agentId]: (current.agentLinks?.[agentId] ?? []).filter((link) => link.id !== linkId),
        },
      }));
      return { kind: 'ok', value: undefined };
    },

    async joinAgentOrg(agentId, orgId) {
      await wait();
      const org = findOrg(orgId);
      if (!org) return { kind: 'failed', message: '组织不存在' };
      if (org.kind === 'work') return { kind: 'unavailable', message: ORG_COPY.agentJoinWorkUnavailable };
      update((current) => ({
        ...current,
        agentOrgs: {
          ...(current.agentOrgs ?? {}),
          [agentId]: [...(current.agentOrgs?.[agentId] ?? []), orgId],
        },
      }));
      return { kind: 'ok', value: undefined };
    },

    async leaveAgentOrg(agentId, orgId) {
      await wait();
      const orgIds = snapshot().agentOrgs?.[agentId] ?? [];
      if (orgIds.length <= 1) return { kind: 'failed', code: 'AGENT_LAST_ORG', message: ORG_COPY.agentLastOrg };
      update((current) => ({
        ...current,
        agentOrgs: { ...(current.agentOrgs ?? {}), [agentId]: orgIds.filter((id) => id !== orgId) },
      }));
      updateDetail(orgId, (detail) => ({
        ...detail,
        agents: detail.agents?.filter((item) => item.id !== agentId) ?? null,
        updatedAt: clock(),
      }));
      return { kind: 'ok', value: undefined };
    },

    async checkMigration(_agentId, machineId) {
      await wait(2);
      const target = findMachine(machineId);
      if (!target) return { kind: 'failed', message: '目标机器已不在组织中，请刷新' };
      return {
        kind: 'ok',
        value: [
          { label: '目标机器属于你', ok: target.mine, detail: `主人 ${target.ownerLabel}` },
          {
            label: '目标机器在线',
            ok: target.status === 'online',
            detail:
              target.status === 'online'
                ? '在线'
                : `目标机器${MACHINE_STATUS_LABELS[target.status]}，等它在线后再试`,
          },
          { label: 'harness 已安装', ok: true, detail: 'codex · 与源机器版本一致' },
          { label: '磁盘空间', ok: true, detail: '可用 212 GB，需要约 1.3 GB' },
        ],
      };
    },

    async planMigration(agentId, machineId) {
      await wait(2);
      const target = findMachine(machineId);
      const record = Object.values(snapshot().details)
        .flatMap((detail) => detail.agents ?? [])
        .find((item) => item.id === agentId);
      // 每次新发起都是一张新的迁移单，编号不与已有迁移单重复。
      seq += 1;
      const ticketNo = `MT-2026-1030-${String(10 + seq).padStart(2, '0')}`;
      const plan = PLAN_FOR(agentId, record?.name ?? agentId, target?.hostname ?? machineId, ticketNo);
      plans.set(plan.id, plan);
      return { kind: 'ok', value: plan };
    },

    async submitMigration(ticketId) {
      await wait(2);
      const plan = plans.get(ticketId);
      if (!plan) return { kind: 'failed', message: '迁移单已失效，请重新发起' };
      update((current) => ({
        ...current,
        outboundMigrations: { ...current.outboundMigrations, [plan.agentId]: { ...plan, status: 'awaiting-target' } },
      }));
      return { kind: 'ok', value: undefined };
    },

    async startMigration(ticketId) {
      await wait(2);
      const ticket = outbound(ticketId);
      if (!ticket || ticket.status !== 'ready') {
        return { kind: 'failed', message: '目标方还没有同意，或迁移单状态已变化。请刷新。' };
      }
      updateOutbound(ticketId, (current) => ({
        ...current,
        status: 'running',
        rollbackAllowed: true,
        progress: {
          phase: '复制 Agent home',
          bytes: '0.3 / 1.2 GB',
          files: '820 / 3,412 个文件',
          rate: '38 MB/s',
          lastProgress: clock(),
        },
      }));
      later(10, () =>
        updateOutbound(ticketId, (current) =>
          current.status === 'running'
            ? {
                ...current,
                status: 'completed',
                rollbackAllowed: false,
                progress: { phase: '完成', bytes: '1.2 / 1.2 GB', files: '3,412 / 3,412 个文件', rate: '—', lastProgress: clock() },
              }
            : current,
        ),
      );
      return { kind: 'ok', value: undefined };
    },

    async cancelMigration(ticketId) {
      await wait();
      if (cancelFailOnce.has(ticketId)) {
        cancelFailOnce.delete(ticketId);
        return { kind: 'failed', message: '源机器暂时连不上，取消没有送达。可以稍后重试。' };
      }
      const inbound = snapshot().inboundMigrations?.find((ticket) => ticket.id === ticketId) ?? null;
      const ticket = inbound ?? outbound(ticketId);
      if (!ticket || ['completed', 'failed', 'cancelled', 'rejected'].includes(ticket.status)) {
        return { kind: 'failed', message: '迁移单状态已变化，请刷新。' };
      }
      const cancel = (current: MigrationTicket): MigrationTicket => ({
        ...current,
        status: 'cancelled',
        rollbackAllowed: false,
      });
      if (inbound) {
        update((current) => ({
          ...current,
          inboundMigrations:
            current.inboundMigrations?.map((item) => (item.id === ticketId ? cancel(item) : item)) ?? null,
        }));
      } else {
        updateOutbound(ticketId, cancel);
      }
      return { kind: 'ok', value: undefined };
    },

    async rollbackMigration(ticketId) {
      await wait(2);
      updateOutbound(ticketId, (current) => ({
        ...current,
        status: 'cancelled',
        rollbackAllowed: false,
        progress: current.progress ? { ...current.progress, phase: '已回滚', lastProgress: clock() } : undefined,
      }));
      return { kind: 'ok', value: undefined };
    },
  };
}
