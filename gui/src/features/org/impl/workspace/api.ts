import type { Dispatch } from 'react';
import type { AppAction } from '../../../../state/types';
import type { MethodRefusal } from '../../../../client';
import type { OrgPermissions } from '../domain/selectors';
import type { OrgGuardKey } from '../domain/unwired';
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
  /** 数据源没有“设为当前组织”时不定义，相关控件不渲染。 */
  setCurrent?(orgId: string): Promise<void>;
  /** 流程需要的方法被拒绝时通知并返回 false；此时不得打开对话框或改变状态。 */
  guard(subject: string, ...methods: readonly OrgGuardKey[]): boolean;
  refusal(method: OrgGuardKey): MethodRefusal | null;
  /** 子页签由数据源提供且读取没有被目录拒绝；其余页签不渲染，也不能从别处链接进去。 */
  tabAvailable(tab: OrgTab): boolean;
  /** 同一个网关，被拒绝时通知的主语是被按下的按钮文字。 */
  labelled(subject: string): OrgGateway;
  refresh(): Promise<void>;
  readonly refreshing: boolean;
  requestAgentView(intent: AgentViewIntent): void;
  /** 加入成功后询问是否设为当前组织，不自动切换。 */
  askSetCurrent(org: OrgSummary): void;
}

/** 把非成功结果转成一句提示；成功与 `unavailable`（已由网关通知）返回 null。 */
export function resultMessage(result: OrgResult<unknown>): string | null {
  switch (result.kind) {
    case 'unavailable':
      return null;
    case 'failed':
    case 'skipped':
      return result.message;
    case 'submitted':
      return result.message ?? '已提交，等待处理';
    case 'ok':
      return null;
  }
}
