import type { OrgDomainSnapshot, OrgGateway, OrgResult } from './types';

/**
 * 未接通时的快照与动作：所有列表为 null（显示“状态未确认”），
 * 所有动作立即在本地返回“暂不可用”，不发请求、不写存储。
 */
export const UNWIRED_SNAPSHOT: OrgDomainSnapshot = {
  status: 'unwired',
  orgs: null,
  currentOrgId: null,
  nodeOwner: false,
  details: {},
  receivedInvites: null,
  inboundMigrations: null,
  outboundMigrations: {},
  thisComputer: null,
  templates: null,
  agentLinks: null,
  refusedMessages: null,
  chatRefusals: {},
  agentOrgs: null,
  linkBypass: null,
};

function unavailable<T>(subject: string): Promise<OrgResult<T>> {
  return Promise.resolve({ kind: 'unavailable', message: `${subject}暂不可用` });
}

export function createUnwiredGateway(): OrgGateway {
  return {
    setCurrentOrg: () => unavailable('设置当前组织'),
    createOrg: () => unavailable('新建组织'),
    previewRepair: () => unavailable('修复'),
    repairOrg: () => unavailable('修复'),
    createInvite: () => unavailable('生成邀请'),
    claimInvite: () => unavailable('加入'),
    joinByLink: () => unavailable('加入'),
    refresh: () => unavailable('刷新'),
    previewRemoveMember: () => unavailable('移除成员'),
    removeMember: () => unavailable('移除成员'),
    previewLeave: () => unavailable('离开组织'),
    leaveOrg: () => unavailable('离开组织'),
    renameOrg: () => unavailable('改名'),
    listTemplates: () => unavailable('模板'),
    previewImport: () => unavailable('导入'),
    validateConfig: () => unavailable('校验'),
    publishCandidate: () => unavailable('发布'),
    adoptCandidate: () => unavailable('采信'),
    saveFile: () => unavailable('保存'),
    createEntry: () => unavailable('新建'),
    moveEntry: () => unavailable('移动'),
    deleteEntry: () => unavailable('删除'),
    restoreEntry: () => unavailable('恢复'),
    purgeEntry: () => unavailable('彻底清除'),
    retryMachine: () => unavailable('立即重试'),
    decideMigration: () => unavailable('迁移'),
    addLink: () => unavailable('添加'),
    revokeLink: () => unavailable('收回'),
    joinAgentOrg: () => unavailable('加入组织'),
    leaveAgentOrg: () => unavailable('移出'),
    checkMigration: () => unavailable('迁移'),
    planMigration: () => unavailable('迁移'),
    submitMigration: () => unavailable('迁移'),
    startMigration: () => unavailable('迁移'),
    cancelMigration: () => unavailable('迁移'),
    rollbackMigration: () => unavailable('迁移'),
  };
}
