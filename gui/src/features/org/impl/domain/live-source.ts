/**
 * 真实组织数据源：org 生命周期 + 当前组织明细 + OrgFS 文件动作。
 *
 * 决策（org-members 第一批，2026-11-02）
 * - 第一批只有 org.list / org.fetch 已准入；后续批次已补齐下列方法。
 * - org.list 发布 authorityDevice（boolean|null；缺失按 null 处理，不推断）；ownerLabel 仍不发布。
 *   kind 优先使用 org.list 的个人/工作分类，ownerLabel 为 null（界面显示“状态未确认”），authorityDevice 只用后端值。
 *   `group-` 前缀按 CLI 的同一规则剥一次，只用于显示名；id 保留完整组织名。
 * - org.list 的 current 只用于首次本地选择。保留仍在列表里的旧选择，否则取 daemon 当前行或第一行；
 *   这不代表后端已设置当前组织；设置动作只更改 GUI 本地选择。
 * - org.fetch 是写：只在用户点“同步组织配置”时调用；超时或中断返回“结果未知”，
 *   不自动重试，也不把候选写成已采信。
 * - org.list 的主读路径把读取状态写进快照：请求前置 loading，成功置 ready，失败置 error
 *   并保留 readError。页面据此显示“正在读取”或错误原因与刷新入口，不把首次失败当成
 *   未接入或空数据；autoload 只消费该状态，不触发 org.fetch。
 * - 写动作按共享 ClientError 的 dispatch 语义分类：dispatch 为 unknown（损坏的响应、
 *   HTTP 拒绝、超时/中断等）说明结果未知，保留 code 并提示未自动重试；只有明确
 *   not-dispatched 的失败才是普通失败。请求返回后的结果校验失败
 *   （`OrgResultShapeError`）同样带稳定 code 与 unknown，不能降级成普通失败。
 *
 * 决策（org 生命周期第二批，2026-11-02）
 * - createOrg 按 CLI 的同一规则把用户输入补成 `group-<name>` 后调用 org.create；成功
 *   后先 refresh() 再读 org.list 的同一行作为返回值（role/state/机器数由列表补齐）。
 *   `published`/`devicePublished` 任一为 false 都按失败返回：组织空间可能已建，但设备
 *   记录没有发布，界面不得显示“创建成功”。dispatch=unknown 只报结果未知，不自动重试。
 *   2026-11-08：显式 tailnetInitialized=false 且两个发布标志为 null 时，只确认本地
 *   组织创建；发布未尝试，不改写标志，也不将它误报为响应格式未知或引导重复创建。
 * - createInvite 直接返回链接/命令/本地过期时间；链接是凭据，只存在调用方与对话框
 *   的临时状态里，不写 URL、localStorage、sessionStorage、日志或遥测。
 * - joinByLink 只在 org.join 成功返回后，按真实 steps 顺序回调 onStep，然后 refresh()
 *   让新成员身份进入列表；失败不回调任何步骤，也不把未知结果改写成成功。写操作从不
 *   自动重试。
 *
 * 决策（org 全方法接线，2026-11-02）
 * - refresh 除 org.list 外读取当前组织的 org.network/hosts/orgfs.spaces/members/status/ls/trash；
 *   单项失败保持 null/空，页面显示“状态未确认”，不把详情读取失败说成组织列表失败。
 * - 成员页的人来自 orgfs.members：space owner 标 owner，其余标 member；isMe 用
 *   org.network.self.owner 比较，machineIds 用 directory.owner 关联。读不到成员或
 *   无法确定 space owner 时 people 保持 null，不显示猜测的成员列表。
 * - leave/remove 先预览并保存 daemon 的 operationId/deviceIds/expectedRevision；确认时
 *   原样提交，过期统一返回 failed.code='stale'，不自动重放。
 * - repair/delete/publish 直接走 org.repair/org.delete/org.publish；publish 失败不显示
 *   “已重试成功”，delete 被 daemon 拒绝时如实显示错误。
 * - delete 是 org.delete 预览加确认两步（OC7），两步是两个用户动作：对话框打开时 previewDelete 发一次
 *   org.delete 读预览并展示；点确认时 deleteOrg 发一次带预览 operationId 的 org.delete，随后刷新列表。
 *   确认失败后对话框自动再读一次预览，这是恢复读取，不是第二个用户动作。预览已是
 *   complete 时刷新列表并按已删除处理。预览阶段失败时 daemon 什么都没写，按删除专用
 *   文案显示（AUTHORITY_REQUIRED 说明要到管理设备删除，不借用邀请文案）。确认阶段失败或阶段不是
 *   complete 时，删除可能已部分执行：先刷新列表，再提示“可能已部分执行、再次删除并确认会继续”；
 *   OPERATION_CONFLICT（子组织）在写入前拒绝，提示未删除。REMOVAL_CONFIRMATION_STALE 与
 *   AUTHORITY_REQUIRED 不断言删除结果：竞争或续删时组织可能已经删除，只提示列表已刷新、仍在列表中再删。
 * - OrgFS 文件动作走 orgfs.write/mkdir/move/remove/stat/restore（内容清除 2026-11-25 D4a 退场）；
 *   保存使用 orgfs.ls 学到的原始版本串，版本未知或已变化时拒绝保存。
 * - 2026-11-22（G1）：移除 link.*（link.add/link.drop/link.list）调用及 addLink/revokeLink 网关成员。
 * - claimInvite / renameOrg / 模板 / 配置采信 / 迁移 / Agent-org 等
 *   没有 daemon 方法的动作不在网关里定义，对应控件不渲染。
 * - 首次自动读取在发出前被拒绝时状态回到 unwired，导航入口与页面随之消失；用户发起的刷新保留原数据。
 *
 * 决策（Agent 目录与“我的”，owner 2026-11-21）
 * - refresh 只读一次 targets(kind=agent)，不再读 agent.list：一次用户动作不再扇出多个 daemon 读取
 *   再由 GUI 拼接。名称取 row.displayName（本机行是注册表显示名，远端行是发布的名字）；
 *   机器取 targetUri 的机器段；本机无组织的 Agent 也是 targets 行（无 orgs），agentOrgs 记为 []。
 * - “我的” = 本用户的 Agent，包含其它设备上的：Agent 主人（targetUri 的用户段）与
 *   org.network.self.owner 用 sameUser 比较。self 只在当前组织的明细读取里可得，所以 mine 在
 *   detailFor 里判定；self.owner 未知（读取失败/缺失/null/空串）时 mine 保持缺省（未判定），
 *   既不写 false 也不写 true，拓扑据此显示“状态未确认”。
 *   其它组织的目录行（orgAgentDirectory）没有明细读取，mine 缺省，不猜。
 * - 不增加“在本机”的设备规则：拓扑页签在 live 数据源不提供（method-access.ts 的
 *   LIVE_UNPROVIDED_TABS），且发送 link（link.*）正由另一条线从产品中移除；
 *   为隐藏且将被删除的功能建设设备规则不值得，所以 mine 只按用户判定。
 */
import {
  ClientError,
  DomainError,
  methodRefusal,
  type MethodAccess,
  EnvelopeError,
  HttpError,
  TransportError,
} from '../../../../client';
import type { OrgStore, OrgDomainSource } from '../domain/context';
import { UNWIRED_SNAPSHOT } from '../domain/unwired';
import { orgMethodRefusal, orgTabProvided, orgTabRefusal } from '../api/method-access';
import { ORG_COPY, ORG_DELETE_REASONS, ORG_FAILURE_REASONS } from '../domain/copy';
import { orgNameFromId, peopleFromMembers, sameUser } from '../domain/selectors';
import { orgIdFromName } from '../domain/validation';
import type {
  OrgDetail,
  OrgAgentRecord,
  OrgFileEntry,
  OrgGateway,
  OrgLifecycle,
  OrgMachineRecord,
  OrgRepairApplied,
  OrgResult,
  OrgRole,
  OrgSpace,
  OrgSummary,
  TrashEntry,
} from './types';
import type { OrgNetworkResult } from '../../../../client/generated/types';
import { networkViewFor } from './network/network-view';
import {
  createOrgfsRequestApi,
  type OrgfsMemberView,
  type OrgfsNodeView,
  type OrgfsSpaceView,
} from '../api/orgfs-request-api';
import {
  OrgRemovalStaleError,
  OrgDeletePendingError,
  OrgResultShapeError,
  createOrgRequestApi,
  type OrgCreateOutcome,
  type OrgJoinOutcome,
} from '../api/org-request-api';
import type { OrgBackendClient } from '../api/backend';

function roleOf(value: string | null): OrgRole | null {
  return value === 'owner' || value === 'member' ? value : null;
}

function lifecycleOf(state: string): OrgLifecycle {
  switch (state) {
    case 'active':
    case 'legacy-active':
      return 'active';
    case 'creating':
      return 'creating';
    case 'partial':
      return 'partial';
    default:
      return 'unknown';
  }
}

function reasonFor(error: unknown): string {
  if (error instanceof DomainError) {
    return ORG_FAILURE_REASONS[error.code] ?? error.message;
  }
  if (error instanceof TransportError) {
    return ORG_COPY.transportReasons[error.kind] ?? error.message;
  }
  if (error instanceof HttpError) {
    return ORG_COPY.httpFailed(error.status);
  }
  if (error instanceof EnvelopeError) {
    return ORG_COPY.responseUnreadable;
  }
  return error instanceof Error ? error.message : ORG_COPY.unknown;
}

function unknownOutcome(error: unknown): boolean {
  return (
    (error instanceof ClientError && error.dispatch === 'unknown') ||
    error instanceof OrgResultShapeError
  );
}

function errorCode(error: unknown): string | undefined {
  if (error instanceof ClientError) return error.code;
  if (error instanceof OrgResultShapeError) return error.code;
  return undefined;
}

type OrgFailure = Extract<OrgResult<never>, { readonly kind: 'failed' | 'unavailable' }>;

function failed(error: unknown): OrgFailure {
  const refusal = methodRefusal(error);
  if (refusal !== null) return { kind: 'unavailable', refusal };
  const code = errorCode(error);
  const unknown = unknownOutcome(error);
  return {
    kind: 'failed',
    message: reasonFor(error),
    ...(code === undefined ? {} : { code }),
    ...(unknown ? { unknown: true as const } : {}),
  };
}

function withMessage(failure: OrgFailure, message: string): OrgResult<never> {
  return failure.kind === 'unavailable' ? failure : { ...failure, message };
}

function unknownFailure(error: unknown, message: string): OrgResult<never> {
  const code = errorCode(error);
  return {
    kind: 'failed',
    ...(code === undefined ? {} : { code }),
    message,
    unknown: true,
  };
}

function isStale(error: unknown): boolean {
  return (
    error instanceof OrgRemovalStaleError ||
    (error instanceof ClientError && error.code === 'REMOVAL_CONFIRMATION_STALE')
  );
}

const REFUSED_BEFORE_WRITE: ReadonlySet<string> = new Set(['AUTHORITY_REQUIRED', 'OPERATION_CONFLICT']);

function deleteFailure(error: unknown): OrgFailure {
  const reason = error instanceof ClientError ? ORG_DELETE_REASONS[error.code] : undefined;
  if (reason === undefined || !(error instanceof ClientError)) return failed(error);
  const message = error.code === 'OPERATION_CONFLICT' ? `${reason}${error.message}` : reason;
  return { kind: 'failed', code: error.code, message };
}

function deleteConfirmFailure(error: unknown): OrgFailure {
  if (error instanceof OrgDeletePendingError) {
    return {
      kind: 'failed',
      message: ORG_COPY.deletePartial(ORG_COPY.deletePhase(error.phase)),
    };
  }
  if (isStale(error)) return { kind: 'failed', code: 'stale', message: ORG_COPY.deleteStale };
  const failure = deleteFailure(error);
  if (failure.kind === 'unavailable') return failure;
  if (error instanceof ClientError && REFUSED_BEFORE_WRITE.has(error.code)) return failure;
  return {
    ...failure,
    message: ORG_COPY.deletePartial(failure.message),
    ...(unknownOutcome(error) ? { unknown: true as const } : {}),
  };
}

function staleFailure(): OrgResult<never> {
  return { kind: 'failed', code: 'stale', message: ORG_COPY.previewChanged };
}

function clockNow(): string {
  const now = new Date();
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

function fileKind(kind: OrgfsNodeView['kind']): OrgFileEntry['kind'] {
  return kind === 'dir' ? 'folder' : 'file';
}

function fileVersion(version: string): number {
  const parsed = Number.parseInt(version, 10);
  return Number.isFinite(parsed) && parsed >= 0 ? parsed : 0;
}

interface LoadedContent {
  readonly text: string;
  readonly version: string;
}

interface TrashMeta {
  readonly version: string;
  readonly kind: 'blob' | 'dir' | 'doc';
  readonly blobHash: string | null;
  /** 目录恢复时用路径找出同次删除的子项，选择删除前快照。 */
  readonly path: string;
}

function fileEntry(node: OrgfsNodeView, loaded?: LoadedContent): OrgFileEntry {
  return {
    path: node.path,
    name: node.name,
    kind: fileKind(node.kind),
    modifiedBy: node.modified_by,
    modifiedAt: '',
    version: fileVersion(node.version),
    ...(loaded === undefined
      ? {}
      : { content: loaded.text, versionKey: loaded.version }),
  };
}

function trashEntry(node: OrgfsNodeView, trash: readonly OrgfsNodeView[]): TrashEntry {
  return {
    id: node.node_id,
    path: node.path,
    deletedBy: node.modified_by,
    deletedAt: '',
    kind: fileKind(node.kind),
    ...(node.kind === 'dir'
      ? {
          childCount: trash.filter(
            (candidate) => candidate.path.startsWith(`${node.path}/`),
          ).length,
        }
      : {}),
  };
}

function machineStatus(
  presence: string | undefined,
  hostStatus: 'online' | 'unknown' | undefined,
): OrgMachineRecord['status'] {
  if (hostStatus === 'online' || presence === 'online') return 'online';
  if (presence === 'offline') return 'offline';
  return 'unknown';
}

function detailFor(
  org: string,
  spaceId: string | null,
  network: OrgNetworkResult | null,
  hosts: readonly {
    readonly nodeId: string;
    readonly status: 'online' | 'unknown';
  }[],
  spaces: readonly OrgfsSpaceView[],
  members: readonly OrgfsMemberView[] | null,
  agents: readonly OrgAgentRecord[] | null,
  nodes: readonly OrgfsNodeView[],
  contents: ReadonlyMap<string, LoadedContent>,
  trash: readonly OrgfsNodeView[],
  filesStatus: 'ready' | 'error',
  trashStatus: 'ready' | 'error',
  healthy: boolean,
): OrgDetail {
  const hostStatus = new Map(hosts.map((host) => [host.nodeId, host.status]));
  const rows = network?.directory?.[org] ?? [];
  // A failed org.network read is unknown, not an empty machine list: the
  // section renders its refresh/unknown block instead of claiming zero.
  const machines: OrgMachineRecord[] | null =
    network === null
      ? null
      : rows.map((row) => ({
          id: row.deviceId,
          hostname: row.machineFingerprint || row.deviceId,
          system: '',
          joinedAt: row.firstPublishedAt ?? row.updatedAt,
          status: machineStatus(row.presence, hostStatus.get(row.deviceId)),
          current: network.self?.deviceId === row.deviceId,
          ownerLabel: row.owner,
          mine: network.self?.owner === row.owner,
        }));
  const orgSpace: OrgSpace | null =
    spaceId === null
      ? null
      : {
          id: spaceId,
          name: spaces.find((space) => space.space_id === spaceId)?.name ?? spaceId,
          health: healthy ? 'healthy' : 'unknown',
          writable: healthy,
        };
  const spaceOwner =
    spaceId === null
      ? null
      : (spaces.find((space) => space.space_id === spaceId)?.owner ?? null);
  const selfOwner = network?.self?.owner ?? null;
  const placedAgents =
    agents === null || selfOwner === null || selfOwner === ''
      ? agents
      : agents.map((agent) => ({ ...agent, mine: sameUser(agent.ownerLabel, selfOwner) }));
  const people = peopleFromMembers({
    members,
    spaceOwner,
    devices: rows,
    selfOwner,
    agents,
  });
  return {
    people,
    agents: placedAgents,
    machines,
    network: networkViewFor(org, network),
    spaces: orgSpace === null ? [] : [orgSpace],
    files:
      spaceId === null || filesStatus === 'error'
        ? {}
        : { [spaceId]: nodes.map((node) => fileEntry(node, contents.get(node.path))) },
    trash:
      spaceId === null || trashStatus === 'error'
        ? {}
        : { [spaceId]: trash.map((node) => trashEntry(node, trash)) },
    filesStatus,
    trashStatus,
    config: null,
    routes: null,
    stats: null,
    updatedAt: clockNow(),
  };
}

export function createOrgLiveSource(client: OrgBackendClient, access?: MethodAccess): OrgDomainSource {
  return {
    initial: { ...UNWIRED_SNAPSHOT, status: 'loading' },
    autoload: true,
    refusal: (method) => orgMethodRefusal(access, method),
    tabRefusal: (tab) => orgTabRefusal(access, tab),
    provides: orgTabProvided,
    createGateway(store: OrgStore): OrgGateway {
      const api = createOrgRequestApi(client);
      const orgfs = createOrgfsRequestApi(client);
      const spaceByOrg = new Map<string, string>();
      const rawVersions = new Map<string, string>();
      const trashMeta = new Map<string, TrashMeta>();
      const agentsByOrg = new Map<string, readonly OrgAgentRecord[] | null>();
      // Per-org generation for switch-triggered detail reads; a late response
      // from an older selection must not overwrite a newer one.
      const detailLoadGeneration = new Map<string, number>();

      interface DetailRead {
        readonly detail: OrgDetail;
        readonly versions: ReadonlyMap<string, string>;
        readonly trash: ReadonlyMap<string, TrashMeta>;
      }

      /** 只提交被接受的那次读取，避免旧响应污染版本/回收站元数据。 */
      function commitAux(spaceId: string | null, read: DetailRead): void {
        if (spaceId !== null) {
          for (const key of [...trashMeta.keys()]) {
            if (key.startsWith(`${spaceId}:`)) trashMeta.delete(key);
          }
          for (const [key, value] of read.trash) trashMeta.set(key, value);
        }
        for (const [key, value] of read.versions) rawVersions.set(key, value);
      }

      /** 当前组织的只读明细；单项失败保持 null/空，由页面显示“状态未确认”。 */
      async function loadDetail(org: string, agents: readonly OrgAgentRecord[] | null): Promise<DetailRead> {
        const spaceId = spaceByOrg.get(org) ?? null;
        const versions = new Map<string, string>();
        const trashEntries = new Map<string, TrashMeta>();
        const [networkR, hostsR, spacesR, membersR, statusR, nodesR, trashR] =
          await Promise.allSettled([
            api.network(org),
            api.hosts(),
            orgfs.listSpaces(),
            spaceId === null ? Promise.resolve(null) : orgfs.members(spaceId),
            spaceId === null ? Promise.resolve(null) : orgfs.status(spaceId),
            spaceId === null ? Promise.resolve([]) : orgfs.listNodes(spaceId),
            spaceId === null ? Promise.resolve([]) : orgfs.listTrash(spaceId),
          ]);
        const network = networkR.status === 'fulfilled' ? networkR.value : null;
        const hosts = hostsR.status === 'fulfilled' ? hostsR.value.hosts : [];
        const spaces = spacesR.status === 'fulfilled' ? spacesR.value : [];
        const members = membersR.status === 'fulfilled' ? membersR.value : null;
        const nodes = nodesR.status === 'fulfilled' ? nodesR.value : [];
        const trash = trashR.status === 'fulfilled' ? trashR.value : [];
        const contents = new Map<string, LoadedContent>();
        if (spaceId !== null) {
          for (const node of nodes) {
            versions.set(`${spaceId}:${node.path}`, node.version);
          }
          for (const node of trash) {
            trashEntries.set(`${spaceId}:${node.node_id}`, {
              version: node.version,
              kind: node.kind,
              blobHash: node.blob_hash,
              path: node.path,
            });
          }
          // Bound the eager text load; later entries stay unloaded and the
          // editor refuses to save them until a refresh/read succeeds.
          await Promise.all(
            nodes
              .filter(
                (node) =>
                  node.kind !== 'dir' && (node.size === null || node.size <= 262_144),
              )
              .slice(0, 50)
              .map(async (node) => {
                try {
                  const read = await orgfs.read(spaceId, node.path);
                  versions.set(`${spaceId}:${node.path}`, read.version);
                  if (read.text !== null) {
                    contents.set(node.path, { text: read.text, version: read.version });
                  }
                } catch {
                  // Content stays unknown; the editor refuses to save it.
                }
              }),
          );
        }
        const healthy =
          statusR.status === 'fulfilled' &&
          statusR.value !== null &&
          spacesR.status === 'fulfilled' &&
          nodesR.status === 'fulfilled' &&
          trashR.status === 'fulfilled';
        return {
          detail: detailFor(
            org,
            spaceId,
            network,
            hosts,
            spaces,
            members,
            agents,
            nodes,
            contents,
            trash,
            nodesR.status === 'fulfilled' ? 'ready' : 'error',
            trashR.status === 'fulfilled' ? 'ready' : 'error',
            healthy,
          ),
          versions,
          trash: trashEntries,
        };
      }

      async function refresh(orgId: string | null): Promise<OrgResult> {
        const refusal = orgMethodRefusal(access, 'refresh');
        if (refusal !== null) return { kind: 'unavailable', refusal };
        const before = store.get();
        store.update((state) => ({ ...state, status: 'loading', readError: null }));
        try {
          const { orgs: rows } = await api.listOrgs();
          if (rows.some((row) => row.kind !== undefined && row.kind !== 'work')) {
            throw new OrgResultShapeError('kind');
          }
          spaceByOrg.clear();
          for (const row of rows) {
            if (row.spaceId !== null) spaceByOrg.set(row.org, row.spaceId);
          }
          const orgs: OrgSummary[] = rows.map((row) => ({
            id: row.org,
            name: orgNameFromId(row.org),
            kind: 'work',
            role: roleOf(row.role),
            lifecycle: lifecycleOf(row.state),
            ownerLabel: null,
            authorityDevice: row.authorityDevice ?? null,
            deviceCount: row.devices,
          }));
          const networkTargets = await client.targets({ kind: 'agent' }).catch(() => null);
          const sharedTargets = networkTargets?.targets ?? [];
          const agentOrgs: Record<string, string[]> = {};
          agentsByOrg.clear();
          const directoryStatus: Record<string, 'ok' | 'partial' | 'unavailable'> = {};
          for (const row of sharedTargets) {
            if (row.agentId) agentOrgs[row.agentId] = [...(row.orgs ?? [])];
          }
          for (const org of orgs) {
            if (networkTargets !== null) {
              agentsByOrg.set(org.id, sharedTargets.filter((row) => row.orgs?.includes(org.id) && row.agentId).map((row) => {
                const parts = row.targetUri.split(':');
                return {
                  id: row.agentId!,
                  name: row.displayName || row.targetUri,
                  ownerLabel: `user:${parts[1] ?? ''}`,
                  machineLabel: parts[2] ?? ORG_COPY.unknown,
                  runState: 'unknown' as const,
                  orgIds: [org.id],
                };
              }));
              directoryStatus[org.id] = networkTargets.orgDirectoryStatus?.[org.id] ?? 'partial';
            } else {
              agentsByOrg.set(org.id, null);
              directoryStatus[org.id] = 'unavailable';
            }
          }
          const current = store.get();
          const keep = current.currentOrgId && orgs.some((org) => org.id === current.currentOrgId);
          const target =
            orgId !== null && orgs.some((org) => org.id === orgId)
              ? orgId
              : keep
                ? current.currentOrgId
                : (rows.find((row) => row.current === true)?.org ?? orgs[0]?.id ?? null);
          // A refresh and a switch-triggered detail read share one generation
          // per org.  Only the newest read may land, including its version
          // and trash metadata; a late older response is dropped whole.
          let accepted: DetailRead | null = null;
          if (target !== null) {
            const generation = (detailLoadGeneration.get(target) ?? 0) + 1;
            detailLoadGeneration.set(target, generation);
            const read = await loadDetail(target, agentsByOrg.get(target) ?? null);
            if (detailLoadGeneration.get(target) === generation) accepted = read;
          }
          if (accepted !== null && target !== null) {
            commitAux(spaceByOrg.get(target) ?? null, accepted);
          }
          store.update((state) => {
            const details = { ...state.details };
            const listed = new Set(orgs.map((org) => org.id));
            for (const key of Object.keys(details)) {
              if (!listed.has(key)) delete details[key];
            }
            if (accepted !== null && target !== null) details[target] = accepted.detail;
            return {
              ...state,
              status: 'ready',
              readError: null,
              orgs,
              agentOrgs: networkTargets !== null ? agentOrgs : null,
              orgAgentDirectory: Object.fromEntries(agentsByOrg),
              orgAgentDirectoryStatus: directoryStatus,
              currentOrgId: target,
              details,
            };
          });
          return { kind: 'ok', value: undefined };
        } catch (error) {
          const refused = methodRefusal(error);
          if (refused !== null) {
            const initialLoad = before.status === 'loading';
            store.update((state) => ({
              ...state,
              status: initialLoad ? 'unwired' : before.status,
              readError: before.readError,
            }));
            return { kind: 'unavailable', refusal: refused };
          }
          const reason = reasonFor(error);
          store.update((state) => ({ ...state, status: 'error', readError: reason }));
          return withMessage(failed(error), ORG_COPY.refreshFailed(reason));
        }
      }

      /** 刷新后的列表中读回刚创建/加入的组织；读取失败时退回写动作返回的摘要。 */
      function listedOrg(fallback: OrgSummary): OrgSummary {
        return store.get().orgs?.find((org) => org.id === fallback.id) ?? fallback;
      }

      function nodeRef(nodeId: string): string {
        return nodeId.startsWith('id:') || nodeId.startsWith('orgfs:')
          ? nodeId
          : `id:${nodeId}`;
      }

      /** 写动作返回后的结果校验失败按 unknown 处理；预览过期统一 stale。 */
      function writeFailure(error: unknown): OrgResult<never> {
        if (isStale(error)) return staleFailure();
        if (unknownOutcome(error)) return unknownFailure(error, ORG_COPY.unknown);
        return failed(error);
      }

      async function applyRemoval(
        action: () => Promise<{ readonly phase: string; readonly complete: boolean }>,
        orgId: string,
      ): Promise<OrgResult> {
        try {
          const outcome = await action();
          await refresh(orgId);
          if (outcome.complete) return { kind: 'ok', value: undefined };
          return {
            kind: 'submitted',
            message: `已提交，等待处理（阶段：${outcome.phase}）。`,
          };
        } catch (error) {
          return writeFailure(error);
        }
      }

      return {
        refresh,
        async setCurrentOrg(orgId) {
          const current = store.get();
          if (!current.orgs?.some((org) => org.id === orgId)) {
            return { kind: 'failed', message: '组织不在当前列表中，请刷新后重试。' };
          }
          store.update((state) => ({ ...state, currentOrgId: orgId }));
          return { kind: 'ok', value: undefined };
        },
        async loadOrgDetail(orgId) {
          const generation = (detailLoadGeneration.get(orgId) ?? 0) + 1;
          detailLoadGeneration.set(orgId, generation);
          try {
            const read = await loadDetail(orgId, agentsByOrg.get(orgId) ?? null);
            if (detailLoadGeneration.get(orgId) !== generation) {
              return { kind: 'ok', value: undefined };
            }
            commitAux(spaceByOrg.get(orgId) ?? null, read);
            store.update((state) => ({
              ...state,
              details: { ...state.details, [orgId]: read.detail },
            }));
            return { kind: 'ok', value: undefined };
          } catch (error) {
            return withMessage(failed(error), ORG_COPY.refreshFailed(reasonFor(error)));
          }
        },
        async syncContext() {
          try {
            const value = await api.fetchContext({ timeoutSeconds: 2, requestMode: 'neighbors' });
            return { kind: 'ok', value };
          } catch (error) {
            if (methodRefusal(error) !== null) return failed(error);
            if (unknownOutcome(error)) {
              const code = errorCode(error);
              return {
                kind: 'failed',
                ...(code === undefined ? {} : { code }),
                message: ORG_COPY.syncConfigUnknown,
                unknown: true,
              };
            }
            return withMessage(failed(error), ORG_COPY.syncConfigFailed(reasonFor(error)));
          }
        },
        async createOrg(name) {
          let outcome: OrgCreateOutcome;
          try {
            outcome = await api.createOrg(orgIdFromName(name));
          } catch (error) {
            if (unknownOutcome(error)) return unknownFailure(error, ORG_COPY.createUnknown);
            return failed(error);
          }
          await refresh(null);
          const org = listedOrg(outcome.org);
          const localOnly = outcome.tailnetInitialized === false &&
            outcome.published === null && outcome.devicePublished === null;
          if (!localOnly && (!outcome.published || !outcome.devicePublished)) {
            return { kind: 'failed', message: ORG_COPY.createNotPublished };
          }
          return { kind: 'ok', value: org };
        },
        async previewDelete(orgId) {
          try {
            const preview = await api.previewDelete(orgId);
            if (preview.phase === 'complete') await refresh(null);
            return { kind: 'ok', value: preview };
          } catch (error) {
            if (error instanceof ClientError && error.code === 'ORG_ALREADY_DELETED') {
              await refresh(null);
            }
            if (unknownOutcome(error)) return unknownFailure(error, ORG_COPY.unknown);
            return deleteFailure(error);
          }
        },
        async deleteOrg(orgId, operationId) {
          try {
            await api.confirmDelete(orgId, operationId);
          } catch (error) {
            await refresh(null);
            return deleteConfirmFailure(error);
          }
          await refresh(null);
          return { kind: 'ok', value: undefined };
        },
        async previewRepair(orgId) {
          try {
            return { kind: 'ok', value: await api.previewRepair(orgId) };
          } catch (error) {
            return writeFailure(error);
          }
        },
        async repairOrg(orgId) {
          let repaired: readonly OrgRepairApplied[];
          try {
            repaired = await api.repair(orgId);
          } catch (error) {
            return writeFailure(error);
          }
          await refresh(orgId);
          // An empty list means the daemon executed no automatic repair; the
          // dialog must not turn that into "修复已完成".
          return { kind: 'ok', value: repaired };
        },
        async createInvite(orgId, account) {
          try {
            return { kind: 'ok', value: await api.invite(orgId, account) };
          } catch (error) {
            if (unknownOutcome(error)) return unknownFailure(error, ORG_COPY.inviteUnknown);
            return failed(error);
          }
        },
        async joinByLink(link, onStep) {
          let outcome: OrgJoinOutcome;
          try {
            outcome = await api.join(link);
          } catch (error) {
            if (unknownOutcome(error)) return unknownFailure(error, ORG_COPY.joinUnknown);
            return failed(error);
          }
          for (const step of outcome.steps) onStep(step);
          await refresh(null);
          return { kind: 'ok', value: listedOrg(outcome.org) };
        },
        async previewLeave(orgId) {
          try {
            return { kind: 'ok', value: await api.previewLeave(orgId) };
          } catch (error) {
            return writeFailure(error);
          }
        },
        async leaveOrg(orgId, revision) {
          return applyRemoval(() => api.leave(orgId, revision), orgId);
        },
        async previewRemoveMember(orgId, personId) {
          try {
            return { kind: 'ok', value: await api.previewRemove(orgId, personId) };
          } catch (error) {
            return writeFailure(error);
          }
        },
        async removeMember(orgId, personId, revision) {
          return applyRemoval(() => api.remove(orgId, personId, revision), orgId);
        },
        async retryMachine(orgId) {
          let result: { readonly published: boolean; readonly orgfsPublished: boolean };
          try {
            result = await api.publish();
          } catch (error) {
            return writeFailure(error);
          }
          await refresh(orgId);
          if (!result.published || !result.orgfsPublished) {
            return {
              kind: 'failed',
              message: '本机组织记录未全部发布，请查看本机服务状态后重试。',
            };
          }
          return { kind: 'ok', value: undefined };
        },
        async saveFile(orgId, spaceId, path, content, _baseVersion, versionKey) {
          if (versionKey === undefined || versionKey.length === 0) {
            return {
              kind: 'failed',
              code: 'ORG_VERSION_UNKNOWN',
              message: '当前文件内容没有绑定原始版本，请刷新后再保存。',
            };
          }
          const bound = rawVersions.get(`${spaceId}:${path}`);
          if (bound !== versionKey) {
            return {
              kind: 'failed',
              code: 'ORG_VERSION_STALE',
              message: '文件版本已变化，请刷新后再保存。',
            };
          }
          try {
            const node = await orgfs.write(spaceId, path, content, versionKey);
            await refresh(orgId);
            return {
              kind: 'ok',
              value: { version: fileVersion(node.version), versionKey: node.version },
            };
          } catch (error) {
            return writeFailure(error);
          }
        },
        async createEntry(orgId, spaceId, path, kind) {
          try {
            if (kind === 'folder') await orgfs.mkdir(spaceId, path);
            else await orgfs.write(spaceId, path, '', undefined, true);
          } catch (error) {
            return writeFailure(error);
          }
          await refresh(orgId);
          return { kind: 'ok', value: undefined };
        },
        async moveEntry(orgId, spaceId, from, to) {
          try {
            await orgfs.move(spaceId, from, to);
          } catch (error) {
            return writeFailure(error);
          }
          await refresh(orgId);
          return { kind: 'ok', value: undefined };
        },
        async deleteEntry(orgId, spaceId, path) {
          try {
            await orgfs.remove(spaceId, path);
          } catch (error) {
            return writeFailure(error);
          }
          await refresh(orgId);
          return { kind: 'ok', value: undefined };
        },
        async restoreEntry(orgId, spaceId, trashId) {
          const meta = trashMeta.get(`${spaceId}:${trashId}`);
          if (meta === undefined) {
            return {
              kind: 'failed',
              code: 'ORG_TRASH_UNKNOWN',
              message: '回收站信息已变化，请刷新后再恢复。',
            };
          }
          if (meta.kind === 'dir') {
            return {
              kind: 'failed',
              code: 'ORG_FOLDER_RESTORE_UNSUPPORTED',
              message: ORG_COPY.folderRestoreUnsupported,
            };
          }
          try {
            await orgfs.restore(spaceId, nodeRef(trashId), meta.version, false);
          } catch (error) {
            return writeFailure(error);
          }
          await refresh(orgId);
          return { kind: 'ok', value: undefined };
        },
      };
    },
  };
}
