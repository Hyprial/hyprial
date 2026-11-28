/*
 * Decision record (unavailable copy to notifications, PR 1f/F2, 2026-11-14):
 * - The unwired gateway is only the value of a shell without an org source (the
 *   default context and the signed-out shell). No page is reachable through it:
 *   the 组织 destination is hidden and the workspace renders nothing, so its
 *   answers carry no text and no invented refusal code.
 * - A gateway that does not implement an action does not define it (optional
 *   members of OrgGateway) and the control is not rendered. An action that is
 *   defined but refused returns `unavailable` with the classified refusal; the
 *   reporting wrapper below turns exactly that result kind into one
 *   notification whose subject is the visible label of the control.
 * - Self-started reads (`loadOrgDetail`) are never reported: the user did not
 *   operate anything. The autoload read of the provider uses the raw gateway.
 */
import type { MethodRefusal } from '../../../../client';
import type { UnavailableDetail } from '../../../../components/notifications';
import type { OrgDomainSnapshot, OrgGateway, OrgResult } from './types';

export const UNWIRED_SNAPSHOT: OrgDomainSnapshot = {
  status: 'unwired',
  readError: null,
  orgs: null,
  currentOrgId: null,
  nodeOwner: false,
  details: {},
  receivedInvites: null,
  inboundMigrations: null,
  outboundMigrations: {},
  thisComputer: null,
  templates: null,
  chatRefusals: {},
  agentOrgs: null,
};

export type OrgGatewayMethod = keyof OrgGateway;
export type OrgGuardKey = OrgGatewayMethod | 'createFolder' | 'createFile';

export const ORG_ACTION_SUBJECTS: Readonly<Record<OrgGatewayMethod, string>> = {
  setCurrentOrg: '设为当前组织',
  createOrg: '创建',
  previewDelete: '删除组织',
  deleteOrg: '确认删除',
  previewRepair: '修复',
  repairOrg: '确认修复',
  createInvite: '生成邀请',
  claimInvite: '加入',
  joinByLink: '加入',
  refresh: '刷新',
  loadOrgDetail: '组织详情',
  syncContext: '同步组织配置',
  previewRemoveMember: '移除成员',
  removeMember: '确认移除',
  previewLeave: '离开组织',
  leaveOrg: '确认离开',
  renameOrg: '确认改名',
  listTemplates: '从模板导入',
  previewImport: '下一步：预览',
  validateConfig: '校验',
  publishCandidate: '发布候选',
  adoptCandidate: '确认采信',
  saveFile: '保存',
  createEntry: '新建',
  moveEntry: '移动',
  deleteEntry: '删除',
  restoreEntry: '恢复',
  retryMachine: '立即重试',
  decideMigration: '确认同意',
  joinAgentOrg: '加入组织',
  leaveAgentOrg: '移出',
  checkMigration: '下一步：检查',
  planMigration: '下一步：核对计划',
  submitMigration: '发起',
  startMigration: '开始迁移',
  cancelMigration: '确认取消迁移',
  rollbackMigration: '确认回滚',
};

const SELF_STARTED_READS: ReadonlySet<OrgGatewayMethod> = new Set(['loadOrgDetail']);

function unavailable(): Promise<OrgResult<never>> {
  return Promise.resolve({ kind: 'unavailable' });
}

export function createUnwiredGateway(): OrgGateway {
  return {
    createOrg: unavailable,
    previewDelete: unavailable,
    deleteOrg: unavailable,
    previewRepair: unavailable,
    repairOrg: unavailable,
    createInvite: unavailable,
    joinByLink: unavailable,
    refresh: unavailable,
    loadOrgDetail: unavailable,
    syncContext: unavailable,
    previewRemoveMember: unavailable,
    removeMember: unavailable,
    previewLeave: unavailable,
    leaveOrg: unavailable,
    saveFile: unavailable,
    createEntry: unavailable,
    moveEntry: unavailable,
    deleteEntry: unavailable,
    restoreEntry: unavailable,
    retryMachine: unavailable,
  };
}

export type NotifyOrgUnavailable = (subject: string, detail?: UnavailableDetail) => void;

export function reportingGateway(gateway: OrgGateway, notify: NotifyOrgUnavailable): OrgGateway {
  const wrapped: Record<string, unknown> = {};
  for (const key of Object.keys(gateway) as OrgGatewayMethod[]) {
    const action = gateway[key] as ((...args: unknown[]) => Promise<OrgResult<unknown>>) | undefined;
    if (typeof action !== 'function') continue;
    wrapped[key] = SELF_STARTED_READS.has(key)
      ? action.bind(gateway)
      : async (...args: unknown[]): Promise<OrgResult<unknown>> => {
          const result = await action.apply(gateway, args);
          if (result.kind === 'unavailable') {
            const refusal: MethodRefusal | undefined = result.refusal;
            notify(ORG_ACTION_SUBJECTS[key], { ...refusal, source: refusal?.source ?? key });
          }
          return result;
        };
  }
  return wrapped as unknown as OrgGateway;
}
