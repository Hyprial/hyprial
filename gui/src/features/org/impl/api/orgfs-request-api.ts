/**
 * OrgFS MethodSpec → 组织文件视图的适配层。
 *
 * 决策（org 全方法接线，2026-11-02）
 * - 只用生成 client；不新增传输 DTO、不解析 HTTP envelope。
 * - 只映射现有文件页真正渲染的字段；`modifiedAt`/`history` 等 daemon 没有在
 *   NodeInfo 中提供的字段返回空/未知，不按文件名或本地时钟猜。
 * - 写动作收到响应后校验失败抛 `OrgResultShapeError`（dispatch unknown），
 *   调用方不得把它当成普通失败或自动重试。
 * - 彻底清除是两步：先按 trash 节点的当前版本生成 purge plan，再执行 plan；
 *   未本地执行时返回 pending，界面显示“已提交”，不显示“已清除”。
 * - 文件树始终请求 `orgfs.ls recursive:true`（#2495 起 daemon 支持），一次拿到
 *   全部可见后代；仍按 path 缩进渲染。
 */
import type { OrgfsRequestClient } from './backend';
import type {
  OrgfsLsResult,
  OrgfsPurgePlanParams,
  OrgfsPurgeResult,
  OrgfsRestoreResult,
  OrgfsSpacesResult,
  OrgfsStatResult,
  OrgfsStatusResult,
  OrgfsTrashResult,
  OrgfsWriteResult,
} from '../../../../client/generated/types';
import { OrgResultShapeError } from './org-request-api';

type OrgfsNode = NonNullable<OrgfsLsResult['nodes']>[number];
type OrgfsSpace = NonNullable<OrgfsSpacesResult['spaces']>[number];
export type OrgfsPurgeTarget = OrgfsPurgePlanParams['targets'][number];
export interface OrgfsHistoryEntry {
  readonly version: string;
  readonly author: string;
  readonly at: string;
  readonly node: OrgfsNode;
}

export type OrgfsNodeView = OrgfsNode;
export type OrgfsSpaceView = OrgfsSpace;

export interface OrgfsMemberView {
  readonly user: string;
  readonly mode: 'ro' | 'rw';
  readonly addedBy: string;
  readonly addedAt: string;
}

export interface OrgfsPurgeOutcome {
  readonly executed: boolean;
  readonly pending: readonly string[];
}

export interface OrgfsRequestApi {
  listSpaces(): Promise<readonly OrgfsSpace[]>;
  listNodes(spaceId: string, path?: string): Promise<readonly OrgfsNode[]>;
  listTrash(spaceId: string, limit?: number): Promise<readonly OrgfsNode[]>;
  members(spaceId: string): Promise<readonly OrgfsMemberView[]>;
  status(spaceId: string): Promise<OrgfsStatusResult>;
  read(spaceId: string, path: string): Promise<{ readonly text: string | null; readonly version: string }>;
  history(spaceId: string, path: string): Promise<readonly OrgfsHistoryEntry[]>;
  readAt(
    spaceId: string,
    node: string,
    version: string,
  ): Promise<{ readonly contentB64: string | null }>;
  stat(spaceId: string, node: string): Promise<OrgfsNode>;
  write(
    spaceId: string,
    path: string,
    text: string,
    baseVersion?: string,
    createOnly?: boolean,
  ): Promise<OrgfsNode>;
  mkdir(spaceId: string, path: string): Promise<OrgfsNode>;
  move(spaceId: string, from: string, to: string): Promise<OrgfsNode>;
  remove(spaceId: string, path: string): Promise<void>;
  restore(
    spaceId: string,
    node: string,
    version: string,
    recursive: boolean,
  ): Promise<OrgfsNode>;
  purgePlan(spaceId: string, targets: readonly OrgfsPurgeTarget[]): Promise<string>;
  purge(spaceId: string, planId: string): Promise<OrgfsPurgeOutcome>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function bad(shape: string): OrgResultShapeError {
  return new OrgResultShapeError(shape);
}

function checkNode(value: unknown): OrgfsNode {
  if (
    !isRecord(value) ||
    typeof value.node_id !== 'string' ||
    typeof value.path !== 'string' ||
    typeof value.name !== 'string' ||
    (value.kind !== 'blob' && value.kind !== 'dir' && value.kind !== 'doc') ||
    typeof value.modified_by !== 'string' ||
    typeof value.space_id !== 'string' ||
    typeof value.uri !== 'string' ||
    typeof value.version !== 'string'
  ) {
    throw bad('node');
  }
  return value as OrgfsNode;
}

function checkNodes(result: OrgfsLsResult | OrgfsTrashResult): readonly OrgfsNode[] {
  if (!isRecord(result) || !Array.isArray(result.nodes)) throw bad('nodes');
  return result.nodes.map(checkNode);
}

function checkSpace(value: unknown): OrgfsSpace {
  if (
    !isRecord(value) ||
    typeof value.space_id !== 'string' ||
    typeof value.name !== 'string' ||
    typeof value.owner !== 'string' ||
    typeof value.created_at !== 'string'
  ) {
    throw bad('space');
  }
  return value as OrgfsSpace;
}

function checkWrite(result: OrgfsWriteResult): OrgfsNode {
  return checkNode(result);
}

function checkRestore(result: OrgfsRestoreResult): OrgfsNode {
  return checkNode(result);
}

function checkStat(result: OrgfsStatResult): OrgfsNode {
  return checkNode(result);
}

export function createOrgfsRequestApi(client: OrgfsRequestClient): OrgfsRequestApi {
  return {
    async listSpaces() {
      const result = await client.orgfs.spaces({});
      if (!Array.isArray(result.spaces)) throw bad('spaces');
      return result.spaces.map(checkSpace);
    },
    async listNodes(spaceId, path) {
      return checkNodes(
        await client.orgfs.ls({
          spaceId,
          recursive: true,
          ...(path === undefined ? {} : { path }),
        }),
      );
    },
    async listTrash(spaceId, limit) {
      return checkNodes(
        await client.orgfs.trash({
          spaceId,
          ...(limit === undefined ? {} : { limit }),
        }),
      );
    },
    async members(spaceId) {
      const result = await client.orgfs.members({ spaceId });
      if (!isRecord(result) || !Array.isArray(result.members)) throw bad('members');
      return result.members.map((value) => {
        if (
          !isRecord(value) ||
          typeof value.user !== 'string' ||
          (value.mode !== 'ro' && value.mode !== 'rw') ||
          typeof value.added_by !== 'string' ||
          typeof value.added_at !== 'string'
        ) {
          throw bad('member');
        }
        return {
          user: value.user,
          mode: value.mode,
          addedBy: value.added_by,
          addedAt: value.added_at,
        };
      });
    },
    async status(spaceId) {
      const result = await client.orgfs.status({ spaceId });
      if (!isRecord(result) || typeof result.space_id !== 'string') throw bad('status');
      return result;
    },
    async read(spaceId, path) {
      const result = await client.orgfs.read({ spaceId, path });
      if (!isRecord(result) || !isRecord(result.node) || typeof result.node.version !== 'string') {
        throw bad('read');
      }
      return {
        text: typeof result.text === 'string' ? result.text : null,
        version: result.node.version,
      };
    },
    async history(spaceId, path) {
      const result = await client.orgfs.history({ spaceId, path });
      if (!isRecord(result) || !Array.isArray(result.history)) throw bad('history');
      return result.history.map((entry) => {
        if (
          !isRecord(entry) ||
          typeof entry.author !== 'string' ||
          typeof entry.at !== 'string' ||
          !isRecord(entry.node) ||
          typeof entry.node.version !== 'string'
        ) {
          throw bad('historyEntry');
        }
        return {
          version: entry.node.version,
          author: entry.author,
          at: entry.at,
          node: checkNode(entry.node),
        };
      });
    },
    async readAt(spaceId, node, version) {
      const result = await client.orgfs.readAt({ spaceId, node, version });
      if (!isRecord(result)) throw bad('readAt');
      return {
        contentB64: typeof result.contentB64 === 'string' ? result.contentB64 : null,
      };
    },
    async stat(spaceId, node) {
      return checkStat(await client.orgfs.stat({ spaceId, node }));
    },
    async write(spaceId, path, text, baseVersion, createOnly) {
      return checkWrite(
        await client.orgfs.write({
          spaceId,
          path,
          text,
          ...(baseVersion === undefined ? {} : { baseVersion }),
          ...(createOnly === undefined ? {} : { createOnly }),
        }),
      );
    },
    async mkdir(spaceId, path) {
      return checkWrite(await client.orgfs.mkdir({ spaceId, path }));
    },
    async move(spaceId, from, to) {
      return checkWrite(
        await client.orgfs.move({ spaceId, sourcePath: from, destinationPath: to }),
      );
    },
    async remove(spaceId, path) {
      const result = await client.orgfs.remove({ spaceId, path });
      if (!isRecord(result) || result.ok !== true) throw bad('remove');
    },
    async restore(spaceId, node, version, recursive) {
      return checkRestore(
        await client.orgfs.restore({ spaceId, node, version, recursive }),
      );
    },
    async purgePlan(spaceId, targets) {
      const result = await client.orgfs.purgePlan({ spaceId, targets: [...targets] });
      if (!isRecord(result) || typeof result.plan_id !== 'string') {
        throw bad('purgePlan');
      }
      return result.plan_id;
    },
    async purge(spaceId, planId): Promise<OrgfsPurgeOutcome> {
      const result: OrgfsPurgeResult = await client.orgfs.purge({ spaceId, planId });
      if (
        !isRecord(result) ||
        typeof result.executed_locally !== 'boolean' ||
        !Array.isArray(result.pending)
      ) {
        throw bad('purge');
      }
      return {
        executed: result.executed_locally,
        pending: [...result.pending],
      };
    },
  };
}
