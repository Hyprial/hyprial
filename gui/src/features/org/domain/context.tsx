import {
  createContext,
  useCallback,
  useEffect,
  useContext,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { UNWIRED_SNAPSHOT, createUnwiredGateway } from './unwired';
import type {
  AgentViewIntent,
  OrgDomainSnapshot,
  OrgGateway,
  OrgViewIntent,
} from './types';

/** 组织数据的读写入口；只给数据源实现（适配器或开发数据）使用。 */
export interface OrgStore {
  get(): OrgDomainSnapshot;
  update(next: (snapshot: OrgDomainSnapshot) => OrgDomainSnapshot): void;
}

/** 组合入口提供的数据源：初始快照与动作实现。 */
export interface OrgDomainSource {
  readonly initial: OrgDomainSnapshot;
  readonly createGateway: (store: OrgStore) => OrgGateway;
}

export interface OrgDomainValue {
  /** 没有 provider 时为 false：各 tab 保持基线视图，组织相关的新入口都不渲染。 */
  readonly provided: boolean;
  readonly snapshot: OrgDomainSnapshot;
  readonly gateway: OrgGateway;
  readonly orgIntent: OrgViewIntent | null;
  readonly agentIntent: AgentViewIntent | null;
  requestOrgView(intent: OrgViewIntent): void;
  requestAgentView(intent: AgentViewIntent): void;
  clearOrgIntent(): void;
  clearAgentIntent(): void;
  /** 本次打开 GUI 是否已处理过登录后的邀请确认（只提示一次）。 */
  readonly postLoginHandled: boolean;
  markPostLoginHandled(): void;
}

const noop = () => undefined;

const DEFAULT_VALUE: OrgDomainValue = {
  provided: false,
  snapshot: UNWIRED_SNAPSHOT,
  gateway: createUnwiredGateway(),
  orgIntent: null,
  agentIntent: null,
  requestOrgView: noop,
  requestAgentView: noop,
  clearOrgIntent: noop,
  clearAgentIntent: noop,
  postLoginHandled: true,
  markPostLoginHandled: noop,
};

const OrgDomainContext = createContext<OrgDomainValue>(DEFAULT_VALUE);

/**
 * 组织数据的注入点。由组合入口（测试、开发预览、将来的适配器）包在 App 外层，
 * 不需要修改壳层。产品入口不包时，各页面按未接通处理。
 */
export function OrgDomainProvider({
  source,
  children,
  externalSnapshot,
}: {
  readonly source: OrgDomainSource;
  readonly externalSnapshot?: OrgDomainSnapshot;
  readonly children: ReactNode;
}) {
  const [snapshot, setSnapshot] = useState(source.initial);
  const snapshotRef = useRef(source.initial);
  const gateway = useMemo(() =>
    source.createGateway({
      get: () => snapshotRef.current,
      update: (next) => {
        snapshotRef.current = next(snapshotRef.current);
        setSnapshot(snapshotRef.current);
      },
    }),
    [source],
  );
  useEffect(() => {
    if (externalSnapshot) { snapshotRef.current = externalSnapshot; setSnapshot(externalSnapshot); }
  }, [externalSnapshot]);
  const [orgIntent, setOrgIntent] = useState<OrgViewIntent | null>(null);
  const [agentIntent, setAgentIntent] = useState<AgentViewIntent | null>(null);

  const clearOrgIntent = useCallback(() => setOrgIntent(null), []);
  const clearAgentIntent = useCallback(() => setAgentIntent(null), []);
  const [postLoginHandled, setPostLoginHandled] = useState(false);
  const markPostLoginHandled = useCallback(() => setPostLoginHandled(true), []);

  const value = useMemo<OrgDomainValue>(
    () => ({
      provided: true,
      snapshot,
      gateway,
      orgIntent,
      agentIntent,
      requestOrgView: setOrgIntent,
      requestAgentView: setAgentIntent,
      clearOrgIntent,
      clearAgentIntent,
      postLoginHandled,
      markPostLoginHandled,
    }),
    [
      snapshot,
      gateway,
      orgIntent,
      agentIntent,
      clearOrgIntent,
      clearAgentIntent,
      postLoginHandled,
      markPostLoginHandled,
    ],
  );

  return (
    <OrgDomainContext.Provider value={value}>{children}</OrgDomainContext.Provider>
  );
}

export function useOrgDomain(): OrgDomainValue {
  return useContext(OrgDomainContext);
}

/** 显式提供的未接通数据源：其他 tab 也显示未接通状态。 */
export const UNWIRED_SOURCE: OrgDomainSource = {
  initial: UNWIRED_SNAPSHOT,
  createGateway: () => createUnwiredGateway(),
};
