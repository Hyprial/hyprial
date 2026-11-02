/**
 * 组织开发数据（只供开发定位层与测试使用，产品入口不引用）。
 *
 * 数据是内存中的模拟值：人员、账号、机器名都是虚构的；邀请链接不是真实凭据。
 * Agent id 与 Agent 页、聊天页的开发数据一致，跨 tab 跳转才能落到同一个对象。
 */
import type {
  ConfigVersion,
  MigrationPlanItem,
  MigrationTicket,
  OrgAgentRecord,
  OrgConfig,
  OrgDetail,
  OrgDomainSnapshot,
  OrgFileEntry,
  OrgMachineRecord,
  OrgPerson,
  OrgStats,
  OrgSummary,
  OrgTemplate,
} from '../domain/types';

export const ORG_PERSONAL = 'org-personal';
export const ORG_TEAM = 'org-hyprial-dev';
export const ORG_PARTNER = 'org-acme-lab';

export const THIS_COMPUTER = { hostname: 'yao-mbp', system: 'macOS 26.1' } as const;

const ME = '姚';

export const ORGS: Readonly<Record<string, OrgSummary>> = {
  [ORG_PERSONAL]: {
    id: ORG_PERSONAL,
    name: 'personal-yao',
    kind: 'personal',
    role: 'owner',
    lifecycle: 'active',
    ownerLabel: ME,
    authorityDevice: true,
  },
  [ORG_TEAM]: {
    id: ORG_TEAM,
    name: 'hyprial-dev',
    kind: 'work',
    role: 'owner',
    lifecycle: 'active',
    ownerLabel: ME,
    authorityDevice: true,
  },
  [ORG_PARTNER]: {
    id: ORG_PARTNER,
    name: 'acme-lab',
    kind: 'work',
    role: 'member',
    lifecycle: 'active',
    ownerLabel: '林澄',
    authorityDevice: false,
  },
};

function person(
  id: string,
  displayName: string,
  account: string,
  role: OrgPerson['role'],
  extra: Partial<OrgPerson> = {},
): OrgPerson {
  return {
    id,
    displayName,
    account,
    role,
    source: 'Casdoor',
    sync: 'synced',
    isMe: false,
    machineIds: [],
    agentIds: [],
    ...extra,
  };
}

const MYSELF = (role: OrgPerson['role'], machineIds: readonly string[], agentIds: readonly string[]) =>
  person('p-me', '姚', 'yao@example.invalid', role, { isMe: true, machineIds, agentIds });

function machine(
  id: string,
  hostname: string,
  ownerLabel: string,
  status: OrgMachineRecord['status'],
  extra: Partial<OrgMachineRecord> = {},
): OrgMachineRecord {
  return {
    id,
    hostname,
    system: 'macOS 26.1',
    joinedAt: '10-21 09:30',
    status,
    current: false,
    ownerLabel,
    mine: ownerLabel === ME,
    ...extra,
  };
}

const M_THIS = machine('m-yao-mbp', 'yao-mbp', ME, 'online', { current: true, joinedAt: '10-20 18:02' });
const M_MINI = machine('m-yao-mini', 'yao-mini', ME, 'syncing', { joinedAt: '10-29 15:40' });
const M_LIN = machine('m-lin-mbp', 'lin-mbp', '林澄', 'online');
const M_CHEN = machine('m-chen-linux', 'chen-linux', '陈墨', 'dormant', {
  system: 'Ubuntu 24.04',
  dormant: { failures: 5, lastAttemptAt: '10-29 14:05' },
});
const M_XU = machine('m-xu-desktop', 'xu-desktop', '许沐', 'unknown', { system: 'Windows 11' });

function agent(
  id: string,
  name: string,
  ownerLabel: string,
  machineLabel: string,
  orgIds: readonly string[],
  extra: Partial<OrgAgentRecord> = {},
): OrgAgentRecord {
  return {
    id,
    name,
    ownerLabel,
    machineLabel,
    runState: 'running',
    orgIds,
    mine: ownerLabel === ME,
    ...extra,
  };
}

const A_SQUIRE = agent('agent-squire', 'Squire', ME, 'yao-mbp', [ORG_PERSONAL, ORG_TEAM, ORG_PARTNER], {
  chatTargetId: 'agent-squire',
});
const A_RESEARCHER = agent('agent-researcher', '研究者', ME, 'yao-mbp', [ORG_PERSONAL, ORG_TEAM], {
  chatTargetId: 'agent-researcher',
});
const A_WRITER = agent('agent-writer', '写作者', ME, 'yao-mbp', [ORG_PERSONAL, ORG_PARTNER], {
  chatTargetId: 'agent-writer',
});
const A_OPS = agent('agent-ops', '运营执行', ME, 'yao-mbp', [ORG_PERSONAL], { runState: 'stopped' });
const A_LIN = agent('agent-lin-review', '评审助手', '林澄', 'lin-mbp', [ORG_TEAM, ORG_PARTNER]);
const A_CHEN = agent('agent-chen-build', '构建守护', '陈墨', 'chen-linux', [ORG_TEAM], { runState: 'unknown' });

function file(
  path: string,
  modifiedBy: string,
  modifiedAt: string,
  version: number,
  content?: string,
): OrgFileEntry {
  const name = path.split('/').at(-1) ?? path;
  return content === undefined
    ? { path, name, kind: 'folder', modifiedBy, modifiedAt, version }
    : { path, name, kind: 'file', modifiedBy, modifiedAt, version, content };
}

const ONBOARDING = '# 新成员入门\n\n1. 安装 hyprial 并登录。\n2. 在“组织 → 邀请”里领取邀请。\n3. 把常用 Agent 加入 hyprial-dev。\n';

export const TEAM_CONFIG_V3: OrgConfig = {
  departments: [
    { id: 'd-eng', name: '研发部', lead: '姚', parent: null, members: ['姚', '陈墨'] },
    { id: 'd-gui', name: 'GUI 组', lead: '林澄', parent: 'd-eng', members: ['林澄', '周屿'] },
  ],
  lines: [
    {
      id: 'l-gui',
      name: 'GUI 初始化',
      lead: '林澄',
      coordinator: 'Squire',
      board: 'gui-init',
      repo: 'HyprialOS/harness-bridge',
    },
  ],
  roles: [
    { id: 'r-review', node: 'lin-mbp', role: '评审', serves: 'GUI 初始化' },
    { id: 'r-build', node: 'chen-linux', role: '构建', serves: '研发部' },
  ],
  routes: [
    { id: 'rt-1', kind: '部门', target: '研发部', scope: '全部成员' },
    { id: 'rt-2', kind: '业务线', target: 'GUI 初始化', scope: 'GUI 组' },
  ],
  norms: {
    autonomy: 'Agent 可以自行处理例行任务；涉及外部发布先问主人。',
    escalation: '阻塞超过 2 小时上报业务线负责人。',
    etiquette: '跨组织消息写明来意与期望回复时间。',
  },
};

const TEAM_CONFIG_V4: OrgConfig = {
  ...TEAM_CONFIG_V3,
  departments: [
    ...TEAM_CONFIG_V3.departments,
    { id: 'd-ops', name: '运营组', lead: '周屿', parent: null, members: ['周屿'] },
  ],
  routes: [...TEAM_CONFIG_V3.routes, { id: 'rt-3', kind: '部门', target: '运营组', scope: '运营组' }],
};

const ADOPTED_V3: ConfigVersion = {
  version: 3,
  publishedAt: '10-27 16:20',
  publisher: '姚',
  config: TEAM_CONFIG_V3,
};

/** 按小时分布 total 条消息（白天多、夜间为 0），合计正好等于 total，与参与者表一致。 */
function hourly(total: number): readonly number[] {
  const weights = Array.from({ length: 24 }, (_, hour) =>
    hour < 8 || hour > 22 ? 0 : (1 + Math.sin(hour / 2)) * (hour % 3 === 0 ? 2 : 1),
  );
  const sum = weights.reduce((acc, weight) => acc + weight, 0);
  const exact = weights.map((weight) => (total * weight) / sum);
  const counts = exact.map(Math.floor);
  const order = exact
    .map((value, hour) => ({ hour, rest: value - Math.floor(value) }))
    .sort((a, b) => b.rest - a.rest);
  const remaining = total - counts.reduce((acc, count) => acc + count, 0);
  for (let i = 0; i < remaining; i += 1) {
    counts[order[i].hour] += 1;
  }
  return counts;
}

/** 参与者的发出合计 = 收到合计 = 折线合计（51 × scale），Top 关系是其中的一部分。 */
function stats(scale: number): OrgStats {
  return {
    hourly: hourly(51 * scale),
    topPairs: [
      { from: 'Squire', to: '评审助手', count: 18 * scale },
      { from: '研究者', to: 'Squire', count: 11 * scale },
      { from: '评审助手', to: '构建守护', count: 6 * scale },
    ],
    participants: [
      { label: 'Squire', sent: 26 * scale, received: 20 * scale },
      { label: '评审助手', sent: 12 * scale, received: 21 * scale },
      { label: '研究者', sent: 11 * scale, received: 4 * scale },
      { label: '构建守护', sent: 2 * scale, received: 6 * scale },
    ],
  };
}

const EMPTY_FILES = { files: {}, trash: {}, history: {} } as const;

export function personalDetail(): OrgDetail {
  return {
    people: [MYSELF('owner', [M_THIS.id, M_MINI.id], [A_SQUIRE.id, A_RESEARCHER.id, A_WRITER.id, A_OPS.id])],
    agents: [A_SQUIRE, A_RESEARCHER, A_WRITER, A_OPS],
    machines: [M_THIS, M_MINI],
    spaces: [{ id: 'space-personal', name: '个人文件', health: 'healthy', writable: true }],
    files: {
      'space-personal': [file('notes', ME, '10-28 21:10', 1), file('notes/todo.md', ME, '10-29 08:15', 4, '- 整理组织页验收截图\n')],
    },
    trash: {},
    history: {},
    config: null,
    routes: null,
    stats: { 1: stats(1), 7: stats(4), 30: stats(12) },
    updatedAt: '16:05',
  };
}

export function teamDetail(): OrgDetail {
  return {
    people: [
      MYSELF('owner', [M_THIS.id, M_MINI.id], [A_SQUIRE.id, A_RESEARCHER.id]),
      person('p-lin', '林澄', 'lin@example.invalid', 'member', {
        machineIds: [M_LIN.id],
        agentIds: [A_LIN.id],
      }),
      person('p-chen', '陈墨', 'chen@example.invalid', 'member', {
        sync: 'syncing',
        machineIds: [M_CHEN.id],
        agentIds: [A_CHEN.id],
      }),
      person('p-zhou', '周屿', 'zhou@example.invalid', 'member', { sync: 'unknown' }),
    ],
    agents: [A_SQUIRE, A_RESEARCHER, A_LIN, A_CHEN],
    machines: [M_THIS, M_MINI, M_LIN, M_CHEN],
    spaces: [
      { id: 'space-docs', name: '团队文档', health: 'healthy', writable: true },
      { id: 'space-config', name: '组织配置', health: 'healthy', writable: true },
      { id: 'space-archive', name: '归档', health: 'unknown', writable: false },
    ],
    files: {
      'space-docs': [
        file('README.md', '林澄', '10-26 10:02', 2, '# hyprial-dev\n\n团队共享文档。组织文件对组织网络内可读，不要存放秘密。\n'),
        file('guides', '林澄', '10-26 10:00', 1),
        file('guides/onboarding.md', '林澄', '10-28 17:45', 3, ONBOARDING),
        file('meetings', ME, '10-27 09:00', 1),
        file('meetings/2026-10-28.md', ME, '10-28 11:30', 1, '## 周会\n\n- 组织页第一版进入验收。\n'),
      ],
      'space-config': [
        file('org-config.json', ME, '10-27 16:20', 3, JSON.stringify(TEAM_CONFIG_V3, null, 2)),
      ],
      'space-archive': [],
    },
    trash: {
      'space-docs': [{ id: 't-1', path: 'drafts/old-plan.md', deletedBy: '周屿', deletedAt: '10-25 19:20' }],
    },
    history: {
      'space-docs:guides/onboarding.md': [
        { version: 3, modifiedBy: '林澄', modifiedAt: '10-28 17:45', content: ONBOARDING },
        { version: 2, modifiedBy: ME, modifiedAt: '10-27 12:10', content: '# 新成员入门\n\n1. 安装 hyprial 并登录。\n' },
        { version: 1, modifiedBy: '林澄', modifiedAt: '10-26 10:00', content: '# 新成员入门\n' },
      ],
    },
    config: {
      adopted: ADOPTED_V3,
      candidate: {
        version: 4,
        publishedAt: '10-29 15:10',
        publisher: '姚',
        config: TEAM_CONFIG_V4,
        diff: ['新增部门：运营组（负责人 周屿）', '新增路由：部门 → 运营组'],
      },
      spaceId: 'space-config',
    },
    routes: TEAM_CONFIG_V3.routes,
    stats: { 1: stats(1), 7: stats(5), 30: stats(18) },
    updatedAt: '16:05',
  };
}

export function partnerDetail(): OrgDetail {
  return {
    people: [
      person('p-lin', '林澄', 'lin@example.invalid', 'owner', { machineIds: [M_LIN.id], agentIds: [A_LIN.id] }),
      MYSELF('member', [M_THIS.id], [A_SQUIRE.id, A_WRITER.id]),
      person('p-xu', '许沐', 'xu@example.invalid', 'member', { machineIds: [M_XU.id] }),
    ],
    agents: [A_LIN, A_SQUIRE, A_WRITER],
    machines: [M_LIN, M_THIS, M_XU],
    spaces: [{ id: 'space-lab', name: '实验记录', health: 'healthy', writable: false }],
    files: {
      'space-lab': [file('results.md', '林澄', '10-29 10:20', 6, '# 实验记录\n\n只读：你是成员，没有写权限。\n')],
    },
    trash: {},
    history: {},
    config: { adopted: null, candidate: null, spaceId: 'space-lab' },
    routes: [],
    stats: { 1: stats(1), 7: stats(2) },
    updatedAt: '15:58',
  };
}

/** 新建工作组织的初始明细：只有自己与这台电脑，还没有组织结构。 */
export function newOrgDetail(): OrgDetail {
  return {
    people: [MYSELF('owner', [M_THIS.id], [])],
    agents: [],
    machines: [M_THIS],
    spaces: [
      { id: 'space-docs', name: '团队文档', health: 'healthy', writable: true },
      { id: 'space-config', name: '组织配置', health: 'healthy', writable: true },
    ],
    ...EMPTY_FILES,
    config: { adopted: null, candidate: null, spaceId: 'space-config' },
    routes: [],
    stats: null,
    updatedAt: null,
  };
}

/** 加入他人组织后的初始明细：成员与机器以后端刷新为准，不在本地推断。 */
export function joinedOrgDetail(): OrgDetail {
  return {
    people: null,
    agents: null,
    machines: null,
    spaces: null,
    ...EMPTY_FILES,
    config: null,
    routes: null,
    stats: null,
    updatedAt: null,
  };
}

export const TEMPLATES: readonly OrgTemplate[] = [
  {
    id: 'tpl-small-team',
    name: '小团队',
    description: '一个部门、一条业务线，适合 3–8 人。',
    config: {
      departments: [{ id: 'd-core', name: '核心组', lead: '姚', parent: null, members: ['姚'] }],
      lines: [{ id: 'l-main', name: '主线', lead: '姚', coordinator: 'Squire', board: 'main', repo: '' }],
      roles: [],
      routes: [{ id: 'rt-core', kind: '部门', target: '核心组', scope: '全部成员' }],
      norms: { autonomy: '例行任务可自行处理。', escalation: '阻塞时找负责人。', etiquette: '写明来意。' },
    },
  },
  {
    id: 'tpl-rd',
    name: '研发部门',
    description: '研发部下设前端、后端两组，含评审与构建常驻角色。',
    config: TEAM_CONFIG_V3,
  },
];

const PLAN_ITEMS: readonly MigrationPlanItem[] = [
  { key: 'home', label: 'Agent home', value: '1.2 GB · 3,412 个文件' },
  { key: 'secrets', label: 'Agent 自有密钥', value: '2 项（迁移后在目标机器重新授权）' },
  { key: 'grants', label: '能力授权', value: '4 项，原样迁移' },
  { key: 'sessions', label: '会话与历史', value: '保留最近 30 天' },
  { key: 'links', label: '可以发送的对象', value: '2 条 link，迁移后保持' },
  { key: 'harness', label: 'harness 与模型', value: 'codex · 目标机器已安装' },
  {
    key: 'workspace',
    label: '工作目录',
    value: null,
    reason: '目标机器上的仓库路径未确认',
    owner: '目标方',
  },
];

export function inboundTicket(): MigrationTicket {
  return {
    id: 'mig-in-1',
    ticketNo: 'MT-2026-1029-07',
    version: 1,
    agentId: 'agent-lin-review',
    agentName: '评审助手',
    sourceMachine: 'lin-mbp',
    targetMachine: THIS_COMPUTER.hostname,
    startedAt: '10-29 15:52',
    status: 'awaiting-target',
    items: PLAN_ITEMS,
    rollbackAllowed: false,
  };
}

/** 迁入请求的其他阶段：迁移中（目标端可查看进度、可取消）与已完成（只能关闭）。 */
export function inboundRunningTicket(): MigrationTicket {
  return {
    ...inboundTicket(),
    id: 'mig-in-2',
    ticketNo: 'MT-2026-1029-05',
    agentId: 'agent-chen-build',
    agentName: '构建守护',
    sourceMachine: 'chen-linux',
    startedAt: '10-29 14:20',
    status: 'running',
    progress: {
      phase: '复制 Agent home',
      bytes: '0.5 / 1.2 GB',
      files: '1,380 / 3,412 个文件',
      rate: '24 MB/s',
      lastProgress: '16:02',
      retryReason: '网络短暂中断，已自动续传',
    },
  };
}

export function inboundCompletedTicket(): MigrationTicket {
  return {
    ...inboundTicket(),
    id: 'mig-in-3',
    ticketNo: 'MT-2026-1028-03',
    agentId: 'agent-writer',
    agentName: '写作者',
    sourceMachine: 'yao-mini',
    startedAt: '10-28 11:05',
    status: 'completed',
    progress: {
      phase: '完成',
      bytes: '1.2 / 1.2 GB',
      files: '3,412 / 3,412 个文件',
      rate: '—',
      lastProgress: '10-28 11:31',
    },
  };
}

export function outboundTicket(status: MigrationTicket['status']): MigrationTicket {
  return {
    id: 'mig-out-1',
    ticketNo: 'MT-2026-1029-09',
    version: 2,
    agentId: 'agent-researcher',
    agentName: '研究者',
    sourceMachine: THIS_COMPUTER.hostname,
    targetMachine: 'yao-mini',
    startedAt: '10-29 16:01',
    status,
    items: PLAN_ITEMS,
    rollbackAllowed: status === 'running',
  };
}

export const PLAN_FOR = (agentId: string, agentName: string, target: string, ticketNo: string): MigrationTicket => ({
  ...outboundTicket('awaiting-target'),
  id: `mig-plan-${agentId}`,
  ticketNo,
  agentId,
  agentName,
  targetMachine: target,
  version: 1,
});

/** 工作组织所有者视角的完整快照（大多数开发情形由它变形得到）。 */
export function ownerSnapshot(): OrgDomainSnapshot {
  return {
    status: 'ready',
    orgs: [ORGS[ORG_PERSONAL], ORGS[ORG_TEAM], ORGS[ORG_PARTNER]],
    currentOrgId: ORG_TEAM,
    nodeOwner: true,
    details: {
      [ORG_PERSONAL]: personalDetail(),
      [ORG_TEAM]: teamDetail(),
      [ORG_PARTNER]: partnerDetail(),
    },
    receivedInvites: [
      { id: 'inv-design', orgName: 'design-guild', inviterLabel: '周屿', acceptedAt: '10-29 09:12' },
      { id: 'inv-ops', orgName: 'ops-oncall', inviterLabel: '许沐', acceptedAt: '10-29 11:40' },
    ],
    inboundMigrations: [inboundTicket()],
    outboundMigrations: {},
    thisComputer: THIS_COMPUTER,
    templates: TEMPLATES,
    agentLinks: {
      'agent-squire': [
        {
          id: 'lk-1',
          holderAgentId: 'agent-squire',
          peerLabel: '评审助手（林澄）',
          state: 'active',
          inFlight: 1,
          lastUsedAt: '15:48',
        },
        {
          id: 'lk-2',
          holderAgentId: 'agent-squire',
          peerLabel: '构建守护（陈墨）',
          state: 'unknown',
          inFlight: 0,
          lastUsedAt: null,
        },
      ],
      'agent-researcher': [],
      'agent-writer': [
        {
          id: 'lk-3',
          holderAgentId: 'agent-writer',
          peerLabel: '评审助手（林澄）',
          state: 'active',
          inFlight: 0,
          lastUsedAt: '10-28 18:20',
        },
      ],
      'agent-ops': [],
    },
    refusedMessages: {
      'agent-squire': [],
      'agent-researcher': [
        {
          id: 'rf-1',
          agentId: 'agent-researcher',
          peerLabel: '构建守护（陈墨）',
          reason: '没有 link：本 Agent 未持有指向对端的 link',
          at: '15:31',
        },
      ],
      'agent-writer': [],
      'agent-ops': [],
    },
    chatRefusals: {
      'org-chat-squire-2': {
        code: 'NO_SHARED_ORG',
        peerLabel: '许沐',
        orgId: ORG_PARTNER,
        orgName: 'acme-lab',
        observation: false,
        inviteAccount: 'xu@example.invalid',
      },
      'org-chat-squire-1': {
        code: 'ORG_POLICY_DENIED',
        peerLabel: '构建守护（陈墨）',
        orgId: ORG_TEAM,
        orgName: 'hyprial-dev',
        observation: true,
      },
    },
    agentOrgs: {
      // Squire 不在 acme-lab：与许沐没有共同组织，聊天被拒提示才成立。
      'agent-squire': [ORG_PERSONAL, ORG_TEAM],
      'agent-researcher': [ORG_PERSONAL, ORG_TEAM],
      'agent-writer': [ORG_PERSONAL, ORG_TEAM, ORG_PARTNER],
      'agent-ops': [ORG_PERSONAL],
      'agent-lin-review': [ORG_TEAM, ORG_PARTNER],
      'agent-chen-build': [ORG_TEAM],
    },
    linkBypass: true,
  };
}
