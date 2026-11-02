import type { Dispatch } from 'react';
import type { StubOperations } from '../adapters/stub';
import type { AppAction } from '../state/types';
import type { LinkedSession, SessionAction } from '../dev-mode/session/session-types';
import { buildUnavailableTopologyModel, type TopologyModel } from '../features/network/topology-view-model';
import { buildUnavailableMachineSectionModel, type MachineSectionModel } from '../features/network/machine-view-model';
import type { OrgCopyHandler, OrgViewModel } from './org/org-view-model';
import { sessionReducer } from '../dev-mode/session/session-reducer';
import { legacyOrgSource } from '../dev-mode/legacy-org-source';
import { useMemo, useRef } from 'react';
import { OrgDomainProvider, UNWIRED_SOURCE, useOrgDomain } from '../features/org/domain/context';
import { OrgWorkspace } from '../features/org/workspace/OrgWorkspace';

interface OrgPageProps {
  readonly dispatch: Dispatch<AppAction>;
  readonly stub: StubOperations;
  /**
   * 旧的 J5 组合模型（开发定位层的 J5 场景与联动会话）。转换为共享组织工作区的数据源。
   */
  readonly model?: OrgViewModel;
  readonly session?: LinkedSession;
  readonly onRetryJoin?: () => void;
  readonly onCopy?: OrgCopyHandler;
  /** Optional J6 machine model; the product default stays unavailable. */
  readonly machineModel?: MachineSectionModel;
  /** Optional J7 topology model; the product default stays unavailable. */
  readonly topologyModel?: TopologyModel;
  /** Development session actions; absent in the product build. */
  readonly onSessionAction?: (action: SessionAction) => void;
}

/** All entry points render OrgWorkspace; legacy J5 is adapted as data only. */
export function OrgPage({ dispatch, model, machineModel, topologyModel, onSessionAction, session }: OrgPageProps) {
  const domain = useOrgDomain();
  const sessionRef = useRef(session); sessionRef.current = session;
  const source = useMemo(() => import.meta.env.DEV ? legacyOrgSource(model, onSessionAction, action => {
    const current = sessionRef.current;
    if (!current) return undefined;
    const next = sessionReducer(current, action);
    if (next === current) return '当前操作不可用';
    if (action.type === 'org-join' && next.firstRun.error) return next.firstRun.error;
    return undefined;
  }, session?.org?.name, () => sessionRef.current) : UNWIRED_SOURCE, [model, onSessionAction, session?.org?.name]);
  const view = <OrgWorkspace dispatch={dispatch} initialNotice={model?.join.state === 'failed' ? model.join.message : undefined} machineModel={machineModel ?? (domain.provided ? undefined : buildUnavailableMachineSectionModel())} topologyModel={topologyModel ?? (domain.provided ? undefined : buildUnavailableTopologyModel())} />;
  return domain.provided ? view : <OrgDomainProvider source={source} externalSnapshot={source.initial}>{view}</OrgDomainProvider>;
}
