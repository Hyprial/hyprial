/**
 * org.list / org.fetch / org.create / org.invite / org.join：
 * MethodSpec 生成的 client，到组织视图模型的唯一适配层。
 *
 * 决策（org-members 第一批，2026-11-02）
 * - 只用 `gui/src/client/generated` 的业务方法；本文件不新增传输 DTO，不解析 HTTP
 *   envelope，也不自己 fetch。
 * - `org.list` 的 kind/current 由 daemon 返回；这里只校验形状，由 live source 映射。
 * - `org.fetch` 是写：超时或中断后结果未知，transport 不会自动重试；本文件如实返回
 *   计数与候选，不把未知结果改写成空候选。
 *
 * 决策（org 生命周期第二批，2026-11-02）
 * - `org.create` 映射为 `OrgCreateOutcome`：视图模型之外只保留 daemon 确实返回的
 *   `tailnetInitialized` / `published` / `devicePublished` / `existed`。隔离创建的
 *   两个发布标志为 null，表示未尝试发布，不能补成 true 或丢失。create 不返回 role/state/机器数：它们留 null/unknown，
 *   由随后的 org.list 刷新补齐，不按 “刚创建” 猜成 owner/active。
 * - `org.invite` 映射为现有 `InviteResult`：`command` → `joinCommand`，
 *   `expiresAt`（UTC ISO）→ 本地 hh:mm，`shortLink` 固定 null（后端没有短链）。
 *   `deepLink` / `invite` 只做形状校验，不进入视图模型。
 * - `org.join` 映射为 `OrgJoinOutcome`：`connect-inviter`/`join-org`/
 *   `register-device`/`connect-members` 依次映射到现有 `JoinStep`；未声明的 kind
 *   直接报格式错误，不静默丢弃。
 * - 邀请链接、deep link 和加入命令都是 bearer 凭据：本文件只做形状校验与映射，
 *   不打印、不记录、不写入任何存储。
 * - 写方法收到响应后校验失败时抛 `OrgResultShapeError`（稳定码
 *   `ORG_RESULT_INVALID`、dispatch=unknown）。此时写操作可能已生效，调用方不得
 *   把它降级成普通失败或自动重试。
 *
 * 决策（org 全方法接线，2026-11-02）
 * - 新增 network/hosts/top.snapshot 读取，以及 repair/leave/remove/delete/publish。
 * - leave/remove 的预览状态保存在本模块内：确认时只提交预览得到的 operationId 与
 *   deviceIds；预览换版（revision-confirmation）或确认过期抛 `OrgRemovalStaleError`，
 *   上层统一映射为 `failed.code='stale'`。
 * - `org.execute` 与 `org.join` 是同一 handler 的别名；GUI 只用 `org.join`，不重复
 *   暴露两个动作。
 */
import type { OrgRequestClient } from './backend';
import type {
  HostsResult,
  OrgCreateResult,
  OrgDeleteResult,
  OrgFetchResult,
  OrgInviteResult,
  OrgJoinResult,
  OrgLeaveResult,
  OrgListResult,
  OrgNetworkResult,
  OrgPublishResult,
  OrgRemoveResult,
  OrgRepairResult,
  TopSnapshotResult,
} from '../../../../client/generated/types';
import { orgNameFromId } from '../domain/selectors';
import type {
  InviteResult,
  JoinStep,
  OrgContextSyncResult,
  OrgDeletePreview,
  OrgSummary,
  RemovalPreview,
} from '../domain/types';

/**
 * 仅测试替身使用的旧 port 形状；生产适配器只接受公共 GeneratedClient 的
 * `OrgRequestClient` 注入，不在这里创建 transport。
 */
export interface OrgRequestPort {
  request(method: string, params: Record<string, unknown>): Promise<unknown>;
}

/**
 * 请求已经返回后的结果校验失败。
 *
 * 响应到达说明写请求可能已经执行；这与“没发出”或“后端明确拒绝”不同。
 * 保留稳定代码与 unknown 语义，让 live source/UI 提示先核对真实状态，
 * 而不是引导用户重复提交。
 */
export const ORG_RESULT_INVALID = 'ORG_RESULT_INVALID';

export class OrgResultShapeError extends Error {
  readonly code = ORG_RESULT_INVALID;
  readonly dispatch = 'unknown' as const;

  constructor(shape: string) {
    super(`组织请求返回格式不正确：${shape}`);
    this.name = 'OrgResultShapeError';
  }
}

/**
 * 确认删除已提交但 daemon 报告的阶段不是 `complete`。
 *
 * 删除可能已部分执行（OrgFS 已标记、tailnet 待关闭等），UI 不能报成功，
 * 也不能当作没发生；提示用户刷新核对当前阶段。
 */
export class OrgDeletePendingError extends Error {
  readonly phase: string;

  constructor(phase: string) {
    super(`删除尚未完成（阶段：${phase}）；请刷新核对当前状态，不要重复提交。`);
    this.name = 'OrgDeletePendingError';
    this.phase = phase;
  }
}

export interface OrgFetchOptions {
  readonly timeoutSeconds?: number;
  readonly requestMode?: 'neighbors';
  readonly from?: string;
}

/** create 的视图结果：组织摘要 + daemon 明确返回的发布标志。 */
export interface OrgCreateOutcome {
  readonly org: OrgSummary;
  readonly tailnetInitialized: boolean;
  readonly published: boolean | null;
  readonly devicePublished: boolean | null;
  readonly existed: boolean;
}

/** join 的视图结果：组织摘要 + 按执行顺序映射后的步骤。 */
export interface OrgJoinOutcome {
  readonly org: OrgSummary;
  readonly steps: readonly JoinStep[];
}

/** org.repair 的候选；repairable=false 只能人工处理。 */
export interface OrgRepairCandidate {
  readonly org: string;
  readonly reason: string;
  readonly repairable: boolean;
}

/** 本次实际执行的修复；publish/devicePublished 为 false 表示仍未完成。 */
export interface OrgRepairApplied {
  readonly org: string;
  readonly published: boolean;
  readonly devicePublished: boolean;
}

export interface OrgRequestApi {
  listOrgs(): Promise<OrgListResult>;
  fetchContext(options?: OrgFetchOptions): Promise<OrgContextSyncResult>;
  createOrg(org: string): Promise<OrgCreateOutcome>;
  invite(org: string, account: string): Promise<InviteResult>;
  join(link: string): Promise<OrgJoinOutcome>;
  network(org: string): Promise<OrgNetworkResult>;
  hosts(): Promise<HostsResult>;
  topSnapshot(): Promise<TopSnapshotResult>;
  previewRepair(org: string | null): Promise<readonly OrgRepairCandidate[]>;
  repair(org: string | null): Promise<readonly OrgRepairApplied[]>;
  previewLeave(org: string): Promise<RemovalPreview>;
  leave(org: string, revision: number): Promise<RemovalOutcome>;
  previewRemove(org: string, user: string): Promise<RemovalPreview>;
  remove(org: string, user: string, revision: number): Promise<RemovalOutcome>;
  previewDelete(org: string): Promise<OrgDeletePreview>;
  confirmDelete(org: string, operationId: string): Promise<void>;
  publish(): Promise<{ readonly published: boolean; readonly orgfsPublished: boolean }>;
}

/** 预览已换版或确认目标不再是同一批设备。 */
export class OrgRemovalStaleError extends Error {
  readonly code = 'stale' as const;

  constructor() {
    super('移除确认已变化，请重新预览后再次确认。');
    this.name = 'OrgRemovalStaleError';
  }
}

/** `org.leave`/`org.remove` 的真实阶段；complete 之外都按“已提交”展示。 */
export interface RemovalOutcome {
  readonly phase: string;
  readonly complete: boolean;
}

interface RemovalPreviewState {
  readonly operationId: string;
  readonly deviceIds: readonly string[];
  readonly revision: number;
  readonly tailnet: string;
  readonly authority: string;
}

const JOIN_STEP_BY_KIND: Readonly<Record<string, JoinStep>> = {
  'connect-inviter': 'connect',
  'join-org': 'join',
  'register-device': 'register',
  'connect-members': 'peers',
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function bad(shape: string): OrgResultShapeError {
  return new OrgResultShapeError(shape);
}

function isCount(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0;
}

function checkList(result: OrgListResult): OrgListResult {
  if (!isRecord(result) || !Array.isArray(result.orgs) || !Array.isArray(result.leftOrgs)) {
    throw bad('orgs/leftOrgs');
  }
  for (const row of result.orgs) {
    if (!isRecord(row) || typeof row.org !== 'string' || row.org.length === 0) {
      throw bad('org');
    }
    if (row.spaceId !== null && typeof row.spaceId !== 'string') throw bad('spaceId');
    if (row.role !== null && typeof row.role !== 'string') throw bad('role');
    if (!isCount(row.devices)) throw bad('devices');
    if (typeof row.state !== 'string' || row.state.length === 0) throw bad('state');
    if (row.kind !== undefined && row.kind !== 'work') throw bad('kind');
    if (row.current !== undefined && typeof row.current !== 'boolean') throw bad('current');
    if (
      row.authorityDevice !== undefined &&
      row.authorityDevice !== null &&
      typeof row.authorityDevice !== 'boolean'
    ) {
      throw bad('authorityDevice');
    }
  }
  if (result.leftOrgs.some((org) => typeof org !== 'string')) throw bad('leftOrgs');
  return result;
}

function checkFetch(result: OrgFetchResult): OrgContextSyncResult {
  if (!isRecord(result) || !Array.isArray(result.requestedSources)) throw bad('requestedSources');
  if (!Array.isArray(result.candidates)) throw bad('candidates');
  for (const field of ['responseCount', 'receivedCount', 'rejectedCount'] as const) {
    if (!isCount(result[field])) throw bad(field);
  }
  const candidates = result.candidates.map((candidate) => {
    if (
      !isRecord(candidate) ||
      typeof candidate.path !== 'string' ||
      typeof candidate.publisher !== 'string' ||
      typeof candidate.receivedAt !== 'string' ||
      typeof candidate.source !== 'string' ||
      !isCount(candidate.version)
    ) {
      throw bad('candidate');
    }
    return {
      path: candidate.path,
      version: candidate.version,
      publisher: candidate.publisher,
      receivedAt: candidate.receivedAt,
      source: candidate.source,
    };
  });
  if (result.source !== undefined && result.source !== 'orgfs') throw bad('source');
  if (result.refreshed !== undefined && typeof result.refreshed !== 'boolean') throw bad('refreshed');
  if (result.spaceId !== undefined && typeof result.spaceId !== 'string') throw bad('spaceId');
  if (
    result.deprecatedSourceTarget !== undefined &&
    typeof result.deprecatedSourceTarget !== 'boolean'
  ) {
    throw bad('deprecatedSourceTarget');
  }
  if (result.sourceTargetHint !== undefined && typeof result.sourceTargetHint !== 'string') {
    throw bad('sourceTargetHint');
  }
  return {
    requestedSources: [...result.requestedSources],
    responseCount: result.responseCount,
    receivedCount: result.receivedCount,
    rejectedCount: result.rejectedCount,
    candidates,
    ...(result.source === undefined ? {} : { source: result.source }),
    ...(result.refreshed === undefined ? {} : { refreshed: result.refreshed }),
    ...(result.spaceId === undefined ? {} : { spaceId: result.spaceId }),
    ...(result.deprecatedSourceTarget === undefined
      ? {}
      : { deprecatedSourceTarget: result.deprecatedSourceTarget }),
    ...(result.sourceTargetHint === undefined
      ? {}
      : { sourceTargetHint: result.sourceTargetHint }),
  };
}

function checkSpace(value: unknown, shape: string): void {
  if (
    !isRecord(value) ||
    typeof value.spaceId !== 'string' ||
    value.spaceId.length === 0 ||
    typeof value.name !== 'string' ||
    value.name.length === 0 ||
    typeof value.owner !== 'string' ||
    value.owner.length === 0 ||
    typeof value.createdAt !== 'string' ||
    value.createdAt.length === 0
  ) {
    throw bad(shape);
  }
}

function createdOrg(org: string): OrgSummary {
  return {
    id: org,
    name: orgNameFromId(org),
    kind: 'work',
    role: null,
    lifecycle: 'unknown',
    ownerLabel: null,
    authorityDevice: null,
    deviceCount: null,
  };
}

function checkCreate(result: OrgCreateResult): OrgCreateOutcome {
  if (!isRecord(result) || typeof result.org !== 'string' || result.org.length === 0) {
    throw bad('org');
  }
  checkSpace(result.space, 'space');
  checkSpace(result.aclSpace, 'aclSpace');
  if (result.spaceId !== result.space.spaceId) throw bad('spaceId');
  if (result.aclSpaceId !== result.aclSpace.spaceId) throw bad('aclSpaceId');
  if (typeof result.existed !== 'boolean' || typeof result.aclExisted !== 'boolean') {
    throw bad('existed');
  }
  if (result.casdoorGroup !== 'not-configured' && result.casdoorGroup !== 'verified') {
    throw bad('casdoorGroup');
  }
  if (typeof result.tailnetInitialized !== 'boolean') throw bad('tailnetInitialized');
  if (
    (result.published !== null && typeof result.published !== 'boolean') ||
    (result.devicePublished !== null && typeof result.devicePublished !== 'boolean')
  ) {
    throw bad('published');
  }
  return {
    org: createdOrg(result.org),
    tailnetInitialized: result.tailnetInitialized,
    published: result.published,
    devicePublished: result.devicePublished,
    existed: result.existed,
  };
}

function checkMember(value: unknown, shape: string): void {
  if (
    !isRecord(value) ||
    typeof value.spaceId !== 'string' ||
    value.spaceId.length === 0 ||
    typeof value.user !== 'string' ||
    value.user.length === 0 ||
    (value.mode !== 'ro' && value.mode !== 'rw')
  ) {
    throw bad(shape);
  }
}

function localClock(expiresAt: string): string {
  const when = new Date(expiresAt);
  if (Number.isNaN(when.getTime())) throw bad('expiresAt');
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${pad(when.getHours())}:${pad(when.getMinutes())}`;
}

function checkInvite(result: OrgInviteResult): InviteResult {
  if (!isRecord(result)) throw bad('invite');
  for (const field of [
    'org',
    'spaceId',
    'aclSpaceId',
    'user',
    'link',
    'deepLink',
    'command',
    'invite',
    'expiresAt',
  ] as const) {
    if (typeof result[field] !== 'string' || result[field].length === 0) throw bad(field);
  }
  checkMember(result.member, 'member');
  checkMember(result.aclMember, 'aclMember');
  return {
    link: result.link,
    joinCommand: result.command,
    expiresAt: localClock(result.expiresAt),
    shortLink: null,
  };
}

function checkJoin(result: OrgJoinResult): OrgJoinOutcome {
  if (
    !isRecord(result) ||
    typeof result.org !== 'string' ||
    result.org.length === 0 ||
    typeof result.spaceId !== 'string' ||
    result.spaceId.length === 0
  ) {
    throw bad('org');
  }
  if (!Array.isArray(result.steps)) throw bad('steps');
  const steps = result.steps.map((step) => {
    if (!isRecord(step) || step.ok !== true || typeof step.detail !== 'string') {
      throw bad('step');
    }
    const mapped = typeof step.kind === 'string' ? JOIN_STEP_BY_KIND[step.kind] : undefined;
    if (!mapped) throw bad('step');
    return mapped;
  });
  return { org: createdOrg(result.org), steps };
}

function checkNetwork(result: OrgNetworkResult): OrgNetworkResult {
  if (!isRecord(result)) throw bad('network');
  // The daemon has a peerKey-only variant; the org view only consumes the
  // full projection, so require the fields the UI maps.
  if (
    !Array.isArray(result.orgs) ||
    !Array.isArray(result.unavailableOrgs) ||
    !Array.isArray(result.dataProblems) ||
    !isRecord(result.directory) ||
    !isRecord(result.self)
  ) {
    throw bad('network');
  }
  return result;
}

function checkHosts(result: HostsResult): HostsResult {
  if (!isRecord(result) || !Array.isArray(result.hosts)) throw bad('hosts');
  for (const host of result.hosts) {
    if (
      !isRecord(host) ||
      typeof host.nodeId !== 'string' ||
      (host.status !== 'online' && host.status !== 'unknown')
    ) {
      throw bad('host');
    }
  }
  return result;
}

function checkTopSnapshot(result: TopSnapshotResult): TopSnapshotResult {
  if (
    !isRecord(result) ||
    !isRecord(result.daemon) ||
    !Array.isArray(result.actors) ||
    !Array.isArray(result.stranded) ||
    !isRecord(result.quota)
  ) {
    throw bad('top.snapshot');
  }
  return result;
}

function checkRepair(result: OrgRepairResult): readonly OrgRepairCandidate[] {
  if (!isRecord(result) || !Array.isArray(result.candidates)) throw bad('repair');
  return result.candidates.map((candidate) => {
    if (
      !isRecord(candidate) ||
      typeof candidate.org !== 'string' ||
      typeof candidate.reason !== 'string' ||
      typeof candidate.repairable !== 'boolean'
    ) {
      throw bad('repairCandidate');
    }
    return {
      org: candidate.org,
      reason: candidate.reason,
      repairable: candidate.repairable,
    };
  });
}

function removalPreview(result: OrgLeaveResult | OrgRemoveResult): RemovalPreviewState {
  if (
    !isRecord(result) ||
    typeof result.operationId !== 'string' ||
    result.operationId.length === 0 ||
    !Array.isArray(result.deviceIds) ||
    result.deviceIds.some((device) => typeof device !== 'string') ||
    typeof result.expectedRevision !== 'number' ||
    typeof result.tailnetId !== 'string' ||
    typeof result.authorityDeviceId !== 'string'
  ) {
    throw bad('removalPreview');
  }
  return {
    operationId: result.operationId,
    deviceIds: [...result.deviceIds],
    revision: result.expectedRevision,
    tailnet: result.tailnetId,
    authority: result.authorityDeviceId,
  };
}

function previewView(state: RemovalPreviewState): RemovalPreview {
  return {
    revision: state.revision,
    devices: [...state.deviceIds],
    tailnet: state.tailnet,
    authority: state.authority,
    impact: `将影响 ${state.deviceIds.length} 台设备；确认后提交，不自动重试。`,
  };
}

function removalOutcome(result: OrgLeaveResult | OrgRemoveResult): RemovalOutcome {
  if (!isRecord(result) || typeof result.phase !== 'string') throw bad('phase');
  return { phase: result.phase, complete: result.phase === 'complete' };
}

function deletePhase(result: OrgDeleteResult): { readonly operationId: string; readonly phase: string } {
  if (
    !isRecord(result) ||
    typeof result.operationId !== 'string' ||
    result.operationId.length === 0 ||
    typeof result.phase !== 'string'
  ) {
    throw bad('delete');
  }
  return { operationId: result.operationId, phase: result.phase };
}

function deletePreview(result: OrgDeleteResult): OrgDeletePreview {
  const { operationId, phase } = deletePhase(result);
  const devices = result.deviceIds;
  if (!Array.isArray(devices) || devices.some((device) => typeof device !== 'string')) {
    throw bad('delete');
  }
  if (typeof result.irreversible !== 'boolean') throw bad('delete');
  return {
    operationId,
    phase,
    devices: [...devices],
    authority: typeof result.authorityDeviceId === 'string' ? result.authorityDeviceId : null,
    irreversible: result.irreversible,
  };
}

function checkPublish(result: OrgPublishResult): {
  readonly published: boolean;
  readonly orgfsPublished: boolean;
} {
  if (
    !isRecord(result) ||
    typeof result.published !== 'boolean' ||
    typeof result.orgfsPublished !== 'boolean'
  ) {
    throw bad('publish');
  }
  return { published: result.published, orgfsPublished: result.orgfsPublished };
}

export function createOrgRequestApi(client: OrgRequestClient): OrgRequestApi {
  const previews = new Map<string, RemovalPreviewState>();
  const keyOf = (org: string, user?: string): string => `${org}\u0000${user ?? ''}`;

  async function executeRemoval(
    key: string,
    revision: number,
    invoke: (state: RemovalPreviewState) => Promise<OrgLeaveResult | OrgRemoveResult>,
  ): Promise<RemovalOutcome> {
    const state = previews.get(key);
    if (!state || state.revision !== revision) throw new OrgRemovalStaleError();
    let result: OrgLeaveResult | OrgRemoveResult;
    try {
      result = await invoke(state);
    } catch (error) {
      const code = isRecord(error) && typeof error.code === 'string' ? error.code : undefined;
      if (code === 'REMOVAL_CONFIRMATION_STALE' || code === 'REVISION_CONFLICT') {
        previews.delete(key);
        throw new OrgRemovalStaleError();
      }
      throw error;
    }
    if (result.phase === 'revision-confirmation') {
      previews.set(key, removalPreview(result));
      throw new OrgRemovalStaleError();
    }
    return removalOutcome(result);
  }

  return {
    async listOrgs() {
      return checkList(await client.org.list({}));
    },
    async fetchContext(options = {}) {
      const timeoutSeconds = options.timeoutSeconds ?? 2;
      return checkFetch(
        await client.org.fetch({
          timeoutSeconds,
          requestMode: options.requestMode ?? 'neighbors',
          ...(options.from === undefined ? {} : { from: options.from }),
        }),
      );
    },
    async createOrg(org) {
      return checkCreate(await client.org.create({ org }));
    },
    async invite(org, account) {
      return checkInvite(await client.org.invite({ org, user: account }));
    },
    async join(link) {
      return checkJoin(await client.org.join({ link }));
    },
    async network(org) {
      return checkNetwork(await client.org.network({ org }));
    },
    async hosts() {
      return checkHosts(await client.hosts({}));
    },
    async topSnapshot() {
      return checkTopSnapshot(await client.top.snapshot({}));
    },
    async previewRepair(org) {
      const result = await client.org.repair({
        apply: false,
        ...(org === null ? {} : { org }),
      });
      return checkRepair(result);
    },
    async repair(org) {
      const result = await client.org.repair({
        apply: true,
        ...(org === null ? {} : { org }),
      });
      if (!Array.isArray(result.repaired)) throw bad('repaired');
      return result.repaired.map((entry) => {
        if (
          !isRecord(entry) ||
          typeof entry.org !== 'string' ||
          typeof entry.published !== 'boolean' ||
          typeof entry.devicePublished !== 'boolean'
        ) {
          throw bad('repaired');
        }
        return {
          org: entry.org,
          published: entry.published,
          devicePublished: entry.devicePublished,
        };
      });
    },
    async previewLeave(org) {
      const key = keyOf(org);
      const state = removalPreview(await client.org.leave({ org }));
      previews.set(key, state);
      return previewView(state);
    },
    async leave(org, revision) {
      return executeRemoval(keyOf(org), revision, (state) =>
        client.org.leave({
          org,
          confirmed: true,
          operationId: state.operationId,
          deviceIds: [...state.deviceIds],
        }),
      );
    },
    async previewRemove(org, user) {
      const key = keyOf(org, user);
      const state = removalPreview(await client.org.remove({ org, user }));
      previews.set(key, state);
      return previewView(state);
    },
    async remove(org, user, revision) {
      return executeRemoval(keyOf(org, user), revision, (state) =>
        client.org.remove({
          org,
          user,
          confirmed: true,
          operationId: state.operationId,
          deviceIds: [...state.deviceIds],
        }),
      );
    },
    async previewDelete(org) {
      return deletePreview(await client.org.delete({ org }));
    },
    async confirmDelete(org, operationId) {
      const result = deletePhase(await client.org.delete({ org, confirmed: true, operationId }));
      if (result.phase !== 'complete') throw new OrgDeletePendingError(result.phase);
    },
    async publish() {
      return checkPublish(await client.org.publish({}));
    },
  };
}
