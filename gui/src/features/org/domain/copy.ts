/**
 * 组织模块文案（交互设计第 11 节）。
 *
 * 规则：未知、失败、空、忽略不共用一句话；成功句只在后端返回成功时使用；
 * 统一称“邀请链接”，动作统一称“加入”。
 */
import type { MachineStatus, MigrationStatus, OrgKind, OrgLifecycle, OrgRole, OrgTab, SyncState } from './types';

/** 中日韩文字与全角标点。 */
const CJK = /[\u2e80-\u9fff\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]/;

function joinCjk(left: string, right: string): string {
  const head = left.endsWith(' ') ? left.slice(0, -1) : left;
  const tail = right.startsWith(' ') ? right.slice(1) : right;
  if (head === left && tail === right) return left + right;
  return CJK.test(head.slice(-1)) && CJK.test(tail.charAt(0)) ? head + tail : `${head} ${tail}`;
}

/**
 * 拼接带名字的中文句子：模板在插值两侧写空格，中文挨着中文时去掉，中文与英文、数字之间保留一个。
 * 例：“你和许沐没有共同组织。”“你和 Allen 没有共同组织。”
 */
export function zh(strings: TemplateStringsArray, ...values: readonly (string | number)[]): string {
  return values.reduce<string>(
    (sentence, value, index) => joinCjk(joinCjk(sentence, String(value)), strings[index + 1] ?? ''),
    strings[0] ?? '',
  );
}

export const ORG_COPY = {
  pageTitle: '组织',
  pageSubtitle: '组织是可选能力；没有加入任何组织不影响聊天与 Agent 使用。',
  unknown: '状态未确认',
  orgUnknown: '组织状态未确认',
  noCurrentOrg: '未设置当前组织',
  unavailable: '暂不可用',
  unavailableNotPlanned: '暂不可用（未立项）',
  processing: '处理中…',
  refresh: '刷新',
  submitted: '已提交，等待处理',
  personalName: '我的组织（个人）',
  currentOrg: '当前组织',
  setCurrent: '设为当前组织',
  switching: '切换中…',
  viewOrg: '查看组织',
  switcherLabel: '组织切换器',
  createOrg: '新建组织',
  joinOrg: '加入组织',
  pendingInvites: (count: number | null) =>
    count === null ? '待处理邀请' : `待处理邀请（${count}）`,
  currentChanged: (name: string) =>
    zh`当前组织已改为 ${name}。之后新建的 Agent 默认归入 ${name}；通信与运行中的 Agent 不受影响。`,
  askSetCurrent: (name: string) => zh`是否把 ${name} 设为当前组织？`,
  updatedAt: (time: string) => `更新于 ${time}`,
  noOrgsTitle: '还没有可用的组织。',
  guideTitle: '你还没有加入工作组织。',
  joinByLink: '用邀请链接加入',
  creatingWarning: '创建未完成',
  repair: '修复',
  inviteMember: '邀请成员',
  newAgentHere: '在此组织新建 Agent',
  newAgentHereShort: '在此组织新建',
  ownerAbilities: '你是所有者：可以邀请和移除成员，设置组织配置。',
  memberAbilities: '你是成员：可以查看成员、机器和组织文件。',
  personalAbilities: '这是你的个人组织：只有你本人和你的 Agent、电脑。',
  noOtherMembers: '还没有其他成员。',
  noAgents: '这个组织还没有 Agent。',
  approvalsUnavailable: '审批功能暂不可用。',
  sendMessage: '发消息',
  openInAgentPage: '在 Agent 页打开',
  removeMember: '移除成员',
  machineUnavailable: '机器网络暂不可用',
  noMachines: '还没有机器记录',
  retryNow: '立即重试',
  thisComputer: '这台电脑',
  inboundRequests: '迁入请求',
  noInbound: '没有等待处理的迁入请求。',
  migrationUnavailable: '迁移功能暂不可用',
  filesUnavailable: '文件功能暂不可用',
  filesSecretHint: '组织文件对组织网络内可读，不要存放秘密。',
  historyAnomaly: '依赖项状态未确认：该空间的历史记录异常，等待空间所有者修复',
  historyAnomalyReason: '该空间的历史记录异常，写操作和历史暂不可用。',
  spaceUnknownReason: '该空间状态未确认，刷新后再操作。',
  noWriteReason: '你没有该空间的写权限。',
  statsScope: '只统计本机能看到的通信；没有数据不代表没有活动。',
  statsUnavailable: '统计暂不可用',
  statsByTask: '按任务统计暂不可用（未立项）',
  linksLocalOnly: '只含本机 Agent 的链接',
  inviteSecretHint: '邀请链接含连接凭据，只私发给被邀请人，不要贴到群里。',
  copied: '已复制',
  copyFailed: '复制失败，请手动选择文本',
  copyLink: '复制邀请链接',
  copyCommand: '复制加入命令',
  shortLinkUnavailable: '短链接暂不可用',
  generateInvite: '生成邀请',
  generatingInvite: '正在生成邀请链接…',
  inviteGenerated: '对方用邀请链接或加入命令加入组织。',
  inviteFailed: (reason: string) => `邀请链接未生成：${reason}`,
  inviteOwnerOnly: '只有工作组织的所有者可以发出邀请。',
  invitePersonalHidden: '个人组织暂不支持发出邀请。',
  claimImpact: '加入后，组织成员可以和你的 Agent 通信，前提是组织配置允许。',
  claiming: (name: string) => zh`正在加入 ${name}…`,
  joined: (name: string) => zh`已加入 ${name}`,
  claimFailed: (name: string, reason: string) => zh`未加入 ${name}：${reason}`,
  skipped: (reason: string) => `已跳过：${reason}`,
  noReceivedInvites: '没有待处理的邀请。',
  joinFailed: (reason: string) => `加入未完成：${reason}`,
  inviteLinkLabel: '邀请链接',
  join: '加入',
  joinSteps: ['连接邀请人', '加入组织', '注册设备', '连接成员'] as const,
  renameImpact: '改名后，成员的组织列表和切换器都会显示新名称。',
  personalNoRename: '个人组织不能改名。',
  structureEmpty: '还没有设置组织结构。',
  importTemplate: '从模板导入',
  startBlank: '从空白开始',
  edit: '编辑',
  openInEditor: '在编辑器中打开',
  validate: '校验',
  publishCandidate: '发布候选',
  candidatePublished: '已发布候选，等待采信',
  adopt: '采信',
  viewInTopology: '在拓扑中查看',
  viewInFiles: '在文件中查看',
  admissionRule:
    '双方至少共享一个组织，且该组织的配置允许，才能通信。观察期内违反规则的消息仍放行；强制期才拒绝。',
  leaveOrg: '离开组织',
  deleteOrg: '删除组织',
  authorityCannotLeave: '这台电脑是该组织的 authority 设备，不能直接离开组织。',
  deleteDisabled: '当前版本不能删除组织。',
  typeNameToConfirm: (name: string) => `输入组织名“${name}”确认`,
  previewChanged: '预览已变化，请核对最新预览后再次确认。',
  agentLastOrg: '每个 Agent 至少属于一个组织。',
  approvalPending: '已提交申请，等待审批',
  agentJoinWorkUnavailable: '加入工作组织需要所有者审批，审批功能暂不可用。',
  orgDefaultNote: (name: string) => zh`默认归入当前组织 ${name}。`,
  noSharedOrg: (peer: string) => zh`你和 ${peer} 没有共同组织。`,
  policyDenied: (org: string) => zh`组织 ${org} 的通信设置不允许这条消息。`,
  observationNote: '观察期：本条已照常送达；进入强制期后，同类消息会被拒绝。原因：',
  refusalRetry: '修复后点下方“重试”重新发送；不会自动重发。',
  fixJoinOrg: (peer: string, agent = 'Agent') => zh`把 ${agent} 加入 ${peer} 所在的组织`,
  fixInvite: (peer: string) => zh`邀请 ${peer} 加入组织`,
  fixPolicy: (org: string) => zh`查看 ${org} 的通信权限`,
  inviteReminder: (count: number) => `你有 ${count} 条组织邀请`,
  inboundReminder: (count: number) => `有 ${count} 个迁入请求等待处理`,
} as const;

export const ORG_TAB_LABELS: Readonly<Record<OrgTab, string>> = {
  overview: '概览',
  members: '成员',
  invites: '邀请',
  machines: '机器',
  topology: '拓扑',
  stats: '统计',
  files: '文件',
  settings: '设置',
};

export const ORG_KIND_LABELS: Readonly<Record<OrgKind, string>> = {
  personal: '个人',
  work: '工作',
};

export const ORG_ROLE_LABELS: Readonly<Record<OrgRole, string>> = {
  owner: '所有者',
  member: '成员',
};

export const ORG_LIFECYCLE_LABELS: Readonly<Record<OrgLifecycle, string>> = {
  active: '正常',
  creating: '创建未完成',
  partial: '创建未完成',
  'sync-problem': '同步异常',
};

export const SYNC_LABELS: Readonly<Record<SyncState, string>> = {
  synced: '已同步',
  syncing: '同步中',
  unknown: '状态未确认',
};

export const MACHINE_STATUS_LABELS: Readonly<Record<MachineStatus, string>> = {
  online: '在线',
  unknown: '状态未知',
  syncing: '同步中',
  offline: '离线',
  dormant: '休眠',
};

export const MIGRATION_STATUS_LABELS: Readonly<Record<MigrationStatus, string>> = {
  'awaiting-target': '等待目标方同意',
  verifying: '待验证',
  'plan-conflict': '计划冲突',
  ready: '可以开始',
  rejected: '已拒绝',
  running: '迁移中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

/** 用邀请链接加入的失败原因与下一步（P06 异常表）。 */
export const JOIN_FAILURES: Readonly<
  Record<string, { readonly reason: string; readonly next: string; readonly retry: boolean }>
> = {
  expired: { reason: '邀请已过期（1 小时）', next: '请邀请人重新生成邀请链接', retry: false },
  used: { reason: '邀请已被使用', next: '请邀请人重新生成邀请链接', retry: false },
  invalid: { reason: '邀请无效或格式错误', next: '请邀请人重新生成邀请链接', retry: false },
  version: { reason: '邀请版本过旧', next: '请邀请人重新生成新版邀请链接', retry: false },
  removed: { reason: '你已被移出该组织', next: '如需加入，请联系组织所有者', retry: false },
  timeout: { reason: '超时', next: '可以重试', retry: true },
};
