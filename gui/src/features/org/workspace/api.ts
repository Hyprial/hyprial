import type { Dispatch } from 'react';
import type { AppAction } from '../../../state/types';
import type { OrgPermissions } from '../domain/selectors';
import type {
  AgentViewIntent,
  OrgDetail,
  OrgDomainSnapshot,
  OrgGateway,
  OrgResult,
  OrgSummary,
  OrgTab,
} from '../domain/types';

/** 子页签内部要打开的位置（分段、侧栏、预填）。 */
export interface SectionIntent {
  readonly segment?: string;
  readonly panel?: 'this-computer';
  readonly prefillAccount?: string;
}

/** 各子页签共用的上下文。 */
export interface WorkspaceApi {
  readonly snapshot: OrgDomainSnapshot;
  readonly gateway: OrgGateway;
  /** 当前查看的组织（不等于“当前组织”）。 */
  readonly org: OrgSummary | null;
  readonly detail: OrgDetail | null;
  readonly perms: OrgPermissions;
  readonly isCurrent: boolean;
  readonly intent: SectionIntent | null;
  readonly dispatch: Dispatch<AppAction>;
  notify(text: string): void;
  goTo(tab: OrgTab, intent?: SectionIntent): void;
  openCreateOrg(): void;
  setCurrent(orgId: string): Promise<void>;
  refresh(): Promise<void>;
  readonly refreshing: boolean;
  requestAgentView(intent: AgentViewIntent): void;
  /** 加入成功后询问是否设为当前组织，不自动切换。 */
  askSetCurrent(org: OrgSummary): void;
}

/** 把非成功结果转成一句提示；成功返回 null。 */
export function resultMessage(result: OrgResult<unknown>): string | null {
  switch (result.kind) {
    case 'unavailable':
    case 'failed':
    case 'skipped':
      return result.message;
    case 'submitted':
      return result.message ?? '已提交，等待处理';
    case 'ok':
      return null;
  }
}
