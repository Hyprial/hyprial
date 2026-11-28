/**
 * OrgFS MethodSpec → 组织文件视图的适配层。
 *
 * 决策（org 全方法接线，2026-11-02）
 * - 只用生成 client；不新增传输 DTO、不解析 HTTP envelope。
 * - 只映射现有文件页真正渲染的字段；`modifiedAt`/`history` 等 daemon 没有在
 *   NodeInfo 中提供的字段返回空/未知，不按文件名或本地时钟猜。
 * - 写动作收到响应后校验失败抛 `OrgResultShapeError`（dispatch unknown），
 *   调用方不得把它当成普通失败或自动重试。
 * - 2026-11-25（orgfs-minimal D4a）：内容清除（orgfs.purge_plan/purge）退场，适配层不再提供
 *   purgePlan/purge。
 * - 2026-11-25（orgfs-minimal G）：daemon 的 `orgfs.remove` 已有可选 `permanent`（永久删除）。
 *   GUI 没有永久删除的设计，适配层的 `remove` 只做移入回收站，不发送 `permanent`。
 * - 2026-11-25（orgfs-minimal G）：`orgfs.restore` 不再有 `version`，只传节点；`recursive` 用
 *   daemon 默认值（true），文件夹恢复由 daemon 只带回与它同批删除的后代，GUI 不自己挑子项。
 * - 2026-11-25（orgfs-minimal G）：`listTrash` 去掉无人使用的 `limit`（只为已删除的旧目录恢复
 *   放大读取上限而存在），回收站列表用 daemon 默认上限。
 * - `orgfs.status` 结果（`space_id, synced, holders_online, content_pending,
 *   rejected_updates`）原样透传，只校验 `space_id`；界面目前不显示这些字段。
 * - 文件树始终请求 `orgfs.ls recursive:true`（#2495 起 daemon 支持），一次拿到
 *   全部可见后代；仍按 path 缩进渲染。
 */
import type { OrgfsRequestClient } from './backend';
import type {
  OrgfsLsResult,
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
export type OrgfsNodeView = OrgfsNode;
export type OrgfsSpaceView = OrgfsSpace;

export interface OrgfsMemberView {
  readonly user: string;
  readonly mode: 'ro' | 'rw';
  readonly addedBy: string;
  readonly addedAt: string;
}

export interface OrgfsRequestApi {
  listSpaces(): Promise<readonly OrgfsSpace[]>;
  listNodes(spaceId: string, path?: string): Promise<readonly OrgfsNode[]>;
  listTrash(spaceId: string): Promise<readonly OrgfsNode[]>;
  members(spaceId: string): Promise<readonly OrgfsMemberView[]>;
  status(spaceId: string): Promise<OrgfsStatusResult>;
  read(spaceId: string, path: string): Promise<{ readonly text: string | null; readonly version: string }>;
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
  restore(spaceId: string, node: string): Promise<OrgfsNode>;
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
    async listTrash(spaceId) {
      return checkNodes(await client.orgfs.trash({ spaceId }));
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
    async restore(spaceId, node) {
      return checkRestore(await client.orgfs.restore({ spaceId, node }));
    },
  };
}
