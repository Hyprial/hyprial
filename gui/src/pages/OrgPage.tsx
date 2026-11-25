import type { Dispatch } from 'react';
import type { AppAction } from '../state/types';
import type { MachineSectionModel, TopologyModel } from '../features/network';
import type { OrgCopyHandler, OrgViewModel } from './org/org-view-model';
import { useGuiCapabilities } from '../app/capabilities';
import {
  OrgDomainProvider,
  OrgWorkspace,
  UNWIRED_SOURCE,
  useOrgDomain,
} from '../features/org';

interface OrgPageProps {
  readonly dispatch: Dispatch<AppAction>;
  /**
   * 旧的 J5 组合模型（开发定位层的 J5 场景与联动会话）。转换为共享组织工作区的数据源。
   */
  readonly model?: OrgViewModel;
  readonly onRetryJoin?: () => void;
  readonly onCopy?: OrgCopyHandler;
  /** Optional J6 machine model supplied by the composition. */
  readonly machineModel?: MachineSectionModel;
  /** Optional J7 topology model supplied by the composition. */
  readonly topologyModel?: TopologyModel;
}

/** All entry points render OrgWorkspace; legacy J5 is adapted as data only. */
export function OrgPage({ dispatch, model, machineModel, topologyModel }: OrgPageProps) {
  const domain = useOrgDomain();
  const capabilities = useGuiCapabilities();
  const source = capabilities.orgSource ?? UNWIRED_SOURCE;
  const liveSource = capabilities.orgSource !== undefined && capabilities.orgSourceKind === 'host';
  const view = (
    <OrgWorkspace
      dispatch={dispatch}
      initialNotice={model?.join.state === 'failed' ? model.join.message : undefined}
      machineModel={liveSource ? undefined : machineModel}
      topologyModel={liveSource ? undefined : topologyModel}
    />
  );
  return domain.provided ? view : <OrgDomainProvider source={source} externalSnapshot={source.initial}>{view}</OrgDomainProvider>;
}
