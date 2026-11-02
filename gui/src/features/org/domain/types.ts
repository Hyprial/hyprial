/**
 * 组织领域的展示模型与动作接口（交互设计 v1.0）。
 *
 * 这些类型只描述界面渲染所需的数据，不是传输协议，也不定义权限语义。
 * 权威在后端：成员以 Casdoor 为准，文件以 OrgFS 为准，通信与迁移以 daemon 为准。
 * `null` 一律表示“来源未提供”，界面显示“状态未确认”，不能当作 0 或空列表。
 */

export type OrgKind = 'personal' | 'work';
export type OrgRole = 'owner' | 'member';
/** `creating`、`partial` 表示创建未完成；`sync-problem` 为规划中的状态。 */
export type OrgLifecycle = 'active' | 'creating' | 'partial' | 'sync-problem';

export interface OrgSummary {
  readonly id: string;
  readonly name: string;
  readonly kind: OrgKind;
  readonly role: OrgRole;
  readonly lifecycle: OrgLifecycle;
  readonly ownerLabel: string;
  /** 本机是该组织的 authority 设备时不能直接离开组织。 */
  readonly authorityDevice: boolean;
}

export type SyncState = 'synced' | 'syncing' | 'unknown';

export interface OrgPerson {
  readonly id: string;
  readonly displayName: string;
  readonly account: string;
  readonly role: OrgRole;
  readonly source: string;
  readonly sync: SyncState;
  readonly isMe: boolean;
  readonly machineIds: readonly string[];
  readonly agentIds: readonly string[];
  /** 聊天页中对应的会话对象；没有时“发消息”只切换到聊天页。 */
  readonly chatTargetId?: string;
}

export type AgentRunState = 'running' | 'stopped' | 'unknown';

export interface OrgAgentRecord {
  readonly id: string;
  readonly name: string;
  readonly ownerLabel: string;
  readonly machineLabel: string;
  readonly runState: AgentRunState;
  readonly orgIds: readonly string[];
  /** 当前用户是该 Agent 的主人。 */
  readonly mine: boolean;
  readonly chatTargetId?: string;
}

/** 现有后端：在线、状态未知、同步中；#2078 规划另增离线、休眠。 */
export type MachineStatus = 'online' | 'unknown' | 'syncing' | 'offline' | 'dormant';

export interface OrgMachineRecord {
  readonly id: string;
  readonly hostname: string;
  readonly system: string;
  readonly joinedAt: string;
  readonly status: MachineStatus;
  readonly current: boolean;
  readonly ownerLabel: string;
  readonly mine: boolean;
  readonly dormant?: { readonly failures: number; readonly lastAttemptAt: string };
}

export interface ReceivedInvite {
  readonly id: string;
  readonly orgName: string;
  readonly inviterLabel: string;
  readonly acceptedAt: string;
  /** 数据源判定不再加入时的原因（本机已在该组织，或曾主动离开）；界面只显示，不自行推断。 */
  readonly skipReason?: string;
}

export type MigrationStatus =
  | 'awaiting-target'
  | 'verifying'
  | 'plan-conflict'
  | 'ready'
  | 'rejected'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled';

/** 迁移单 7 项之一；`value` 为 null 时显示“未知”、原因与责任方。 */
export interface MigrationPlanItem {
  readonly key: string;
  readonly label: string;
  readonly value: string | null;
  readonly reason?: string;
  readonly owner?: string;
}

export interface MigrationProgress {
  readonly phase: string;
  readonly bytes: string;
  readonly files: string;
  readonly rate: string;
  readonly lastProgress: string;
  readonly retryReason?: string;
}

export interface MigrationTicket {
  readonly id: string;
  readonly ticketNo: string;
  readonly version: number;
  readonly agentId: string;
  readonly agentName: string;
  readonly sourceMachine: string;
  readonly targetMachine: string;
  readonly startedAt: string;
  readonly status: MigrationStatus;
  readonly items: readonly MigrationPlanItem[];
  readonly progress?: MigrationProgress;
  /** 只有后端声明允许的阶段才显示“回滚”。 */
  readonly rollbackAllowed: boolean;
}

export interface MigrationCheck {
  readonly label: string;
  readonly ok: boolean;
  readonly detail: string;
}

export type SpaceHealth = 'unknown' | 'history-anomaly' | 'healthy';

export interface OrgSpace {
  readonly id: string;
  readonly name: string;
  readonly health: SpaceHealth;
  readonly writable: boolean;
}

export interface OrgFileEntry {
  readonly path: string;
  readonly name: string;
  readonly kind: 'file' | 'folder';
  readonly modifiedBy: string;
  readonly modifiedAt: string;
  readonly version: number;
  readonly content?: string;
}

export interface TrashEntry {
  readonly id: string;
  readonly path: string;
  readonly deletedBy: string;
  readonly deletedAt: string;
  /** 来源未提供时按文件显示。文件夹连同其中的内容一起进回收站，`childCount` 是其中的项数。 */
  readonly kind?: 'file' | 'folder';
  readonly childCount?: number;
}

export interface FileVersion {
  readonly version: number;
  readonly modifiedBy: string;
  readonly modifiedAt: string;
  readonly content: string;
}

export interface OrgDepartment {
  readonly id: string;
  readonly name: string;
  readonly lead: string;
  readonly parent: string | null;
  readonly members: readonly string[];
}

export interface OrgBusinessLine {
  readonly id: string;
  readonly name: string;
  readonly lead: string;
  readonly coordinator: string;
  readonly board: string;
  readonly repo: string;
}

export interface OrgResidentRole {
  readonly id: string;
  readonly node: string;
  readonly role: string;
  readonly serves: string;
}

export interface OrgRoute {
  readonly id: string;
  readonly kind: string;
  readonly target: string;
  /** 范围的语义尚未定义，按原文显示。 */
  readonly scope: string;
}

export interface OrgNorms {
  readonly autonomy: string;
  readonly escalation: string;
  readonly etiquette: string;
}

export interface OrgConfig {
  readonly departments: readonly OrgDepartment[];
  readonly lines: readonly OrgBusinessLine[];
  readonly roles: readonly OrgResidentRole[];
  readonly routes: readonly OrgRoute[];
  readonly norms: OrgNorms;
}

export interface ConfigVersion {
  readonly version: number;
  readonly publishedAt: string;
  readonly publisher: string;
  readonly config: OrgConfig;
}

export interface OrgConfigState {
  readonly adopted: ConfigVersion | null;
  readonly candidate: (ConfigVersion & { readonly diff: readonly string[] }) | null;
  /** 配置候选所在的空间，按文件页的空间状态显示。 */
  readonly spaceId: string;
}

export interface OrgTemplate {
  readonly id: string;
  readonly name: string;
  readonly description: string;
  readonly config: OrgConfig;
}

export interface ConfigValidation {
  readonly errors: readonly string[];
  readonly warnings: readonly string[];
}

export interface ImportPreview {
  readonly config: OrgConfig;
  readonly diff: readonly string[];
  readonly validation: ConfigValidation;
}

export interface AgentLink {
  readonly id: string;
  readonly holderAgentId: string;
  readonly peerLabel: string;
  readonly state: 'active' | 'unknown';
  readonly inFlight: number;
  readonly lastUsedAt: string | null;
}

export interface RefusedMessage {
  readonly id: string;
  readonly agentId: string;
  readonly peerLabel: string;
  readonly reason: string;
  readonly at: string;
}

export interface OrgStats {
  readonly hourly: readonly number[];
  readonly topPairs: readonly { readonly from: string; readonly to: string; readonly count: number }[];
  readonly participants: readonly {
    readonly label: string;
    readonly sent: number;
    readonly received: number;
  }[];
}

export type StatsWindow = 1 | 7 | 30;

/** 聊天中发送被拒的原因；只在后端返回时出现。 */
export interface ChatRefusal {
  readonly code: 'NO_SHARED_ORG' | 'ORG_POLICY_DENIED';
  readonly peerLabel: string;
  readonly orgId?: string;
  readonly orgName?: string;
  /** 观察期：消息照常发送，只提示按新规则将被拒绝。 */
  readonly observation: boolean;
  readonly inviteAccount?: string;
}

export interface RemovalPreview {
  readonly revision: number;
  readonly devices: readonly string[];
  readonly tailnet: string;
  readonly authority: string;
  readonly impact: string;
}

/** 一个组织的明细；各字段为 null 时表示来源未提供。 */
export interface OrgDetail {
  readonly people: readonly OrgPerson[] | null;
  readonly agents: readonly OrgAgentRecord[] | null;
  readonly machines: readonly OrgMachineRecord[] | null;
  readonly spaces: readonly OrgSpace[] | null;
  readonly files: Readonly<Record<string, readonly OrgFileEntry[]>>;
  readonly trash: Readonly<Record<string, readonly TrashEntry[]>>;
  readonly history: Readonly<Record<string, readonly FileVersion[]>>;
  readonly config: OrgConfigState | null;
  readonly routes: readonly OrgRoute[] | null;
  readonly stats: Readonly<Partial<Record<StatsWindow, OrgStats>>> | null;
  readonly updatedAt: string | null;
}

export interface ThisComputer {
  readonly hostname: string;
  readonly system: string;
}

/**
 * 组织领域快照。`status: 'unwired'` 表示接口未接通：
 * 列表显示“状态未确认”，动作只在本地显示“暂不可用”。
 */
export interface OrgDomainSnapshot {
  readonly status: 'unwired' | 'ready';
  readonly orgs: readonly OrgSummary[] | null;
  readonly currentOrgId: string | null;
  /** 本节点 owner 才能采信配置，与组织角色无关。 */
  readonly nodeOwner: boolean;
  readonly details: Readonly<Record<string, OrgDetail>>;
  readonly receivedInvites: readonly ReceivedInvite[] | null;
  readonly inboundMigrations: readonly MigrationTicket[] | null;
  readonly outboundMigrations: Readonly<Record<string, MigrationTicket>>;
  readonly thisComputer: ThisComputer | null;
  readonly templates: readonly OrgTemplate[] | null;
  readonly agentLinks: Readonly<Record<string, readonly AgentLink[]>> | null;
  readonly refusedMessages: Readonly<Record<string, readonly RefusedMessage[]>> | null;
  readonly chatRefusals: Readonly<Record<string, ChatRefusal>>;
  /** Agent 页的 Agent id → 所属组织 id；只在后端提供时使用。 */
  readonly agentOrgs: Readonly<Record<string, readonly string[]>> | null;
  /** 链接管理只含本机 Agent 持有的 link。 */
  readonly linkBypass: boolean | null;
}

/** 动作结果。成功只在后端明确返回时出现。 */
export type OrgResult<T = undefined> =
  | { readonly kind: 'unavailable'; readonly message: string }
  | {
      readonly kind: 'failed';
      readonly message: string;
      readonly code?: string;
      /** 版本冲突时由后端给出对方版本，界面让用户选择后重新保存。 */
      readonly conflict?: FileSaveConflict;
    }
  | { readonly kind: 'skipped'; readonly message: string }
  | { readonly kind: 'submitted'; readonly message?: string }
  | { readonly kind: 'ok'; readonly value: T };

export interface InviteResult {
  readonly link: string;
  readonly joinCommand: string;
  /** 失效时间，本地时间 hh:mm；界面显示为“1 小时内有效（hh:mm 前）”。 */
  readonly expiresAt: string;
  /** 短链接在途；生成失败时为 null，长链接照常可用。 */
  readonly shortLink: string | null;
}

export type JoinStep = 'connect' | 'join' | 'register' | 'peers';

export interface FileSaveConflict {
  readonly theirs: string;
  readonly theirVersion: number;
}

export type MigrationDecision = 'accept' | 'reject';

/**
 * 组织动作接口。页面只通过它发起动作，不拼命令、不直接请求。
 * 未接通实现对每个动作立即返回 `unavailable`，不发任何请求。
 */
export interface OrgGateway {
  setCurrentOrg(orgId: string): Promise<OrgResult>;
  createOrg(name: string): Promise<OrgResult<OrgSummary>>;
  previewRepair(orgId: string): Promise<OrgResult<readonly string[]>>;
  repairOrg(orgId: string): Promise<OrgResult>;
  createInvite(orgId: string, account: string): Promise<OrgResult<InviteResult>>;
  claimInvite(inviteId: string): Promise<OrgResult<OrgSummary>>;
  joinByLink(
    link: string,
    onStep: (step: JoinStep) => void,
  ): Promise<OrgResult<OrgSummary>>;
  refresh(orgId: string | null): Promise<OrgResult>;
  previewRemoveMember(orgId: string, personId: string): Promise<OrgResult<RemovalPreview>>;
  removeMember(orgId: string, personId: string, revision: number): Promise<OrgResult>;
  previewLeave(orgId: string): Promise<OrgResult<RemovalPreview>>;
  leaveOrg(orgId: string, revision: number): Promise<OrgResult>;
  renameOrg(orgId: string, name: string): Promise<OrgResult>;
  listTemplates(): Promise<OrgResult<readonly OrgTemplate[]>>;
  previewImport(
    orgId: string,
    source: { readonly templateId: string } | { readonly fileName: string; readonly text: string },
  ): Promise<OrgResult<ImportPreview>>;
  validateConfig(orgId: string, config: OrgConfig): Promise<OrgResult<ConfigValidation>>;
  publishCandidate(orgId: string, config: OrgConfig): Promise<OrgResult>;
  adoptCandidate(orgId: string, version: number): Promise<OrgResult>;
  saveFile(
    orgId: string,
    spaceId: string,
    path: string,
    content: string,
    baseVersion: number,
  ): Promise<OrgResult<number>>;
  createEntry(
    orgId: string,
    spaceId: string,
    path: string,
    kind: 'file' | 'folder',
  ): Promise<OrgResult>;
  moveEntry(orgId: string, spaceId: string, from: string, to: string): Promise<OrgResult>;
  deleteEntry(orgId: string, spaceId: string, path: string): Promise<OrgResult>;
  restoreEntry(orgId: string, spaceId: string, trashId: string): Promise<OrgResult>;
  purgeEntry(orgId: string, spaceId: string, trashId: string): Promise<OrgResult>;
  retryMachine(orgId: string, machineId: string): Promise<OrgResult>;
  decideMigration(ticketId: string, decision: MigrationDecision): Promise<OrgResult>;
  addLink(agentId: string, peer: string): Promise<OrgResult>;
  revokeLink(agentId: string, linkId: string): Promise<OrgResult>;
  joinAgentOrg(agentId: string, orgId: string): Promise<OrgResult>;
  leaveAgentOrg(agentId: string, orgId: string): Promise<OrgResult>;
  checkMigration(
    agentId: string,
    machineId: string,
  ): Promise<OrgResult<readonly MigrationCheck[]>>;
  planMigration(agentId: string, machineId: string): Promise<OrgResult<MigrationTicket>>;
  submitMigration(ticketId: string): Promise<OrgResult>;
  startMigration(ticketId: string): Promise<OrgResult>;
  cancelMigration(ticketId: string): Promise<OrgResult>;
  rollbackMigration(ticketId: string): Promise<OrgResult>;
}

/** 组织 tab 的子页签。 */
export type OrgTab =
  | 'overview'
  | 'members'
  | 'invites'
  | 'machines'
  | 'topology'
  | 'stats'
  | 'files'
  | 'settings';

/** 从其他 tab 进入组织 tab 时要打开的位置。 */
export interface OrgViewIntent {
  readonly tab: OrgTab;
  readonly segment?: string;
  readonly panel?: 'this-computer';
  readonly prefillAccount?: string;
  readonly orgId?: string;
}

/** 从组织相关入口进入 Agent 页时要预填的内容。 */
export interface AgentViewIntent {
  readonly createInOrgId?: string;
  readonly joinOrgId?: string;
  readonly agentId?: string;
  readonly section?: 'orgs' | 'comm' | 'migration';
  readonly prefillPeer?: string;
}
