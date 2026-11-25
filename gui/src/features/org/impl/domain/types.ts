/**
 * 组织领域的展示模型与动作接口（交互设计 v1.0）。
 *
 * 这些类型只描述界面渲染所需的数据，不是传输协议，也不定义权限语义。
 * 权威在后端：成员以 Casdoor 为准，文件以 OrgFS 为准，通信与迁移以 daemon 为准。
 * `null` 一律表示“来源未提供”，界面显示“状态未确认”，不能当作 0 或空列表。
 */

import type { MethodRefusal } from '../../../../client';

export type OrgKind = 'work';
export type OrgRole = 'owner' | 'member';
/** `creating`、`partial` 表示创建未完成；`sync-problem` 为规划中的状态；`unknown` 为来源未提供。 */
export type OrgLifecycle = 'active' | 'creating' | 'partial' | 'sync-problem' | 'unknown';

/**
 * 组织列表行。`null` 表示来源未提供（例如 org.list 不给所有者与本机 authority 标记），
 * 界面显示“状态未确认”，不能当作 false 或空字符串。
 */
export interface OrgSummary {
  readonly id: string;
  readonly name: string;
  readonly kind: OrgKind;
  readonly role: OrgRole | null;
  readonly lifecycle: OrgLifecycle;
  readonly ownerLabel: string | null;
  /** 本机是该组织的 authority 设备时不能直接离开组织。 */
  readonly authorityDevice: boolean | null;
  /** org.list 的 devices；未连接来源时为 null。 */
  readonly deviceCount: number | null;
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
  /** 当前用户是该 Agent 的主人（含其它设备上的）；当前用户未能确定时缺省，表示未判定，不是 false。 */
  readonly mine?: boolean;
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

export interface OrgNetworkView {
  readonly panes: Readonly<Record<'directory' | 'mappedPeers' | 'sidecar', boolean>>;
  readonly devices: readonly {
    readonly id: string;
    readonly name: string;
    readonly owner: string;
    readonly presence: string;
    readonly updatedAt: string;
    readonly current: boolean;
  }[] | null;
  readonly mappedPeers: readonly {
    readonly peer: string;
    readonly endpoint: string | null;
    readonly dialed: boolean;
  }[] | null;
  readonly sidecar: {
    readonly policyMode: string | null;
    readonly lastPollAtMs: number | null;
    readonly lastPollOk: boolean | null;
    readonly slotsFree: number | null;
    readonly slotsTotal: number | null;
    readonly restartRequired: boolean | null;
  } | null;
  readonly warnings: readonly string[];
  readonly selfDeviceId: string | null;
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
  /** 仅用于显示/历史；保存必须用 `versionKey` 的原始不透明版本。 */
  readonly version: number;
  /** 与 `content` 同一读取时刻的原始版本串；未知时不能保存。 */
  readonly versionKey?: string;
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
  /** read_at 失败时为 error，不能把空白当成真实空正文；调用前即被拒绝时为 refused，不呈现为失败。 */
  readonly contentStatus?: 'ready' | 'error' | 'refused';
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
  readonly peerUri: string;
  readonly peerLabel: string;
  readonly orgId?: string;
  readonly orgName?: string;
  /** 观察期：消息照常发送，只提示按新规则将被拒绝。 */
  readonly observation: boolean;
  readonly inviteAccount?: string;
}

/** org.delete 预览（OC7）：确认时必须原样带回 operationId；phase 非 pending-confirmation 表示续删。 */
export interface OrgDeletePreview {
  readonly operationId: string;
  readonly phase: string;
  readonly devices: readonly string[];
  readonly authority: string | null;
  readonly irreversible: boolean;
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
  readonly network?: OrgNetworkView | null;
  readonly spaces: readonly OrgSpace[] | null;
  readonly files: Readonly<Record<string, readonly OrgFileEntry[]>>;
  readonly trash: Readonly<Record<string, readonly TrashEntry[]>>;
  /** 分项读取状态；error 时不能把空数组显示成“没有文件/回收站为空”。 */
  readonly filesStatus?: 'ready' | 'error';
  readonly trashStatus?: 'ready' | 'error';
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

/** 组织配置候选摘要；只在 org.fetch 成功后出现。 */
export interface OrgContextCandidate {
  readonly path: string;
  readonly version: number;
  readonly publisher: string;
  readonly receivedAt: string;
  readonly source: string;
}

/** org.fetch 的真实返回并集：mesh 与 orgfs 两种形状。 */
export interface OrgContextSyncResult {
  readonly requestedSources: readonly string[];
  readonly responseCount: number;
  readonly receivedCount: number;
  readonly rejectedCount: number;
  readonly candidates: readonly OrgContextCandidate[];
  readonly source?: string;
  readonly refreshed?: boolean;
  readonly spaceId?: string;
  readonly deprecatedSourceTarget?: boolean;
  readonly sourceTargetHint?: string;
}

/**
 * 组织列表的读取状态。`unwired` 表示数据源未接入；`loading` 与 `error` 都表示已接入但
 * 本次读取尚未完成或失败，页面不能把它当成 ready，也不能当成空数据。
 */
export type OrgReadStatus = 'unwired' | 'loading' | 'ready' | 'error';

/**
 * 组织领域快照。`status` 描述列表读取状态；`readError` 只在 `status: 'error'` 时给出
 * 可读原因。旧数据在失败时保留，但页面必须显示错误原因与刷新入口。
 */
export interface OrgDomainSnapshot {
  readonly status: OrgReadStatus;
  readonly readError: string | null;
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
  readonly chatRefusals: Readonly<Record<string, ChatRefusal>>;
  /** Agent 页的 Agent id → 所属组织 id；只在后端提供时使用。 */
  readonly agentOrgs: Readonly<Record<string, readonly string[]>> | null;
  readonly orgAgentDirectory?: Readonly<Record<string, readonly OrgAgentRecord[] | null>>;
  readonly orgAgentDirectoryStatus?: Readonly<Record<string, 'ok' | 'partial' | 'unavailable'>>;
}

/** 动作结果。成功只在后端明确返回时出现。 */
export type OrgResult<T = undefined> =
  | { readonly kind: 'unavailable'; readonly refusal?: MethodRefusal }
  | {
      readonly kind: 'failed';
      readonly message: string;
      readonly code?: string;
      /**
       * 写请求可能已生效（超时、中断、响应损坏或返回结果无法校验）。
       * 界面不展示失败步骤、不自动重试，先提示刷新/核对真实状态。
       */
      readonly unknown?: boolean;
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

/** 保存成功后的版本：数值只用于显示/历史，`versionKey` 是下一次保存的原始版本。 */
export interface OrgSaveResult {
  readonly version: number;
  readonly versionKey: string;
}

export type MigrationDecision = 'accept' | 'reject';

/** 修复预览中的一个候选；repairable=false 表示只能人工处理。 */
export interface OrgRepairCandidate {
  readonly org: string;
  readonly reason: string;
  readonly repairable: boolean;
}

/** 实际执行的修复；published/devicePublished 任一为 false 都代表仍未完成。 */
export interface OrgRepairApplied {
  readonly org: string;
  readonly published: boolean;
  readonly devicePublished: boolean;
}

/**
 * 组织动作接口。页面只通过它发起动作，不拼命令、不直接请求。
 * 数据源没有实现的动作不定义（可选成员），对应的控件不渲染；数据源有实现但被拒绝时，
 * 动作返回 `unavailable`（可带 refusal），由 useOrgDomain 的网关统一发出通知。
 */
export interface OrgGateway {
  setCurrentOrg?(orgId: string): Promise<OrgResult>;
  createOrg(name: string): Promise<OrgResult<OrgSummary>>;
  previewDelete(orgId: string): Promise<OrgResult<OrgDeletePreview>>;
  deleteOrg(orgId: string, operationId: string): Promise<OrgResult>;
  previewRepair(orgId: string): Promise<OrgResult<readonly OrgRepairCandidate[]>>;
  /** 返回本次实际执行的修复；空数组表示没有执行任何修复。 */
  repairOrg(orgId: string): Promise<OrgResult<readonly OrgRepairApplied[]>>;
  createInvite(orgId: string, account: string): Promise<OrgResult<InviteResult>>;
  claimInvite?(inviteId: string): Promise<OrgResult<OrgSummary>>;
  joinByLink(
    link: string,
    onStep: (step: JoinStep) => void,
  ): Promise<OrgResult<OrgSummary>>;
  refresh(orgId: string | null): Promise<OrgResult>;
  /**
   * 只读取指定组织的明细并合并缓存；不改变当前组织或列表。
   * 切换查看对象时由页面显式调用，绝不自动调用 org.fetch。
   */
  loadOrgDetail(orgId: string): Promise<OrgResult>;
  /** 显式动作：调用 org.fetch 暂存候选，页面加载时不自动调用。 */
  syncContext(): Promise<OrgResult<OrgContextSyncResult>>;
  previewRemoveMember(orgId: string, personId: string): Promise<OrgResult<RemovalPreview>>;
  removeMember(orgId: string, personId: string, revision: number): Promise<OrgResult>;
  previewLeave(orgId: string): Promise<OrgResult<RemovalPreview>>;
  leaveOrg(orgId: string, revision: number): Promise<OrgResult>;
  renameOrg?(orgId: string, name: string): Promise<OrgResult>;
  listTemplates?(): Promise<OrgResult<readonly OrgTemplate[]>>;
  previewImport?(
    orgId: string,
    source: { readonly templateId: string } | { readonly fileName: string; readonly text: string },
  ): Promise<OrgResult<ImportPreview>>;
  validateConfig?(orgId: string, config: OrgConfig): Promise<OrgResult<ConfigValidation>>;
  publishCandidate?(orgId: string, config: OrgConfig): Promise<OrgResult>;
  adoptCandidate?(orgId: string, version: number): Promise<OrgResult>;
  saveFile(
    orgId: string,
    spaceId: string,
    path: string,
    content: string,
    baseVersion: number,
    versionKey?: string,
  ): Promise<OrgResult<OrgSaveResult>>;
  createEntry(
    orgId: string,
    spaceId: string,
    path: string,
    kind: 'file' | 'folder',
  ): Promise<OrgResult>;
  moveEntry(orgId: string, spaceId: string, from: string, to: string): Promise<OrgResult>;
  deleteEntry(orgId: string, spaceId: string, path: string): Promise<OrgResult>;
  loadHistory(
    orgId: string,
    spaceId: string,
    path: string,
  ): Promise<OrgResult<readonly FileVersion[]>>;
  restoreEntry(orgId: string, spaceId: string, trashId: string): Promise<OrgResult>;
  retryMachine(orgId: string, machineId: string): Promise<OrgResult>;
  decideMigration?(ticketId: string, decision: MigrationDecision): Promise<OrgResult>;
  joinAgentOrg?(agentId: string, orgId: string): Promise<OrgResult>;
  leaveAgentOrg?(agentId: string, orgId: string): Promise<OrgResult>;
  checkMigration?(
    agentId: string,
    machineId: string,
  ): Promise<OrgResult<readonly MigrationCheck[]>>;
  planMigration?(agentId: string, machineId: string): Promise<OrgResult<MigrationTicket>>;
  submitMigration?(ticketId: string): Promise<OrgResult>;
  startMigration?(ticketId: string): Promise<OrgResult>;
  cancelMigration?(ticketId: string): Promise<OrgResult>;
  rollbackMigration?(ticketId: string): Promise<OrgResult>;
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
  readonly joinOrgId?: string;
  readonly agentId?: string;
  readonly section?: 'orgs' | 'migration';
}
