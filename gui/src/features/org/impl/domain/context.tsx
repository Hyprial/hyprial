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
import type { MethodRefusal } from '../../../../client';
import { useNotifyUnavailable } from '../../../../components/notifications';
import {
  UNWIRED_SNAPSHOT,
  createUnwiredGateway,
  reportingGateway,
  type OrgGuardKey,
} from './unwired';
import type {
  AgentViewIntent,
  OrgDomainSnapshot,
  OrgGateway,
  OrgTab,
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
  /** 真实数据源在挂载后读取一次主列表；org.fetch 永不自动调用。 */
  readonly autoload?: boolean;
  /** 动作在发出前就会被拒绝时的分类；没有目录信息的数据源不提供。 */
  readonly refusal?: (method: OrgGuardKey) => MethodRefusal | null;
  /** 子页签依赖的读取被目录拒绝时的分类；被拒绝的页签不渲染。没有目录信息的数据源不提供。 */
  readonly tabRefusal?: (tab: OrgTab) => MethodRefusal | null;
  /** 该数据源能提供子页签的数据；不提供此字段的数据源（开发数据、预览）提供全部页签。 */
  readonly provides?: (tab: OrgTab) => boolean;
}

export interface OrgDomainValue {
  /** 没有 provider 时为 false：各 tab 保持基线视图，组织相关的新入口都不渲染。 */
  readonly provided: boolean;
  readonly snapshot: OrgDomainSnapshot;
  readonly gateway: OrgGateway;
  /** 动作发出前的同步判断：被拒绝时返回分类，用于在打开对话框或表单之前就通知。 */
  refusal(method: OrgGuardKey): MethodRefusal | null;
  /** 子页签的读取被目录拒绝：返回分类，该页签不渲染，也没有指向它的入口。 */
  tabRefusal(tab: OrgTab): MethodRefusal | null;
  /** 页签渲染且可被链接：数据源提供它的数据，且读取没有被目录拒绝。 */
  tabShown(tab: OrgTab): boolean;
  /** 流程需要的方法任一被拒绝时通知并返回 false；调用方此时不打开对话框、不改任何状态。 */
  guard(subject: string, ...methods: readonly OrgGuardKey[]): boolean;
  /** 同一个网关，被拒绝时的通知主语是被按下的按钮文字（一个方法服务多个按钮时使用）。 */
  labelled(subject: string): OrgGateway;
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
  refusal: () => null,
  tabRefusal: () => null,
  tabShown: () => true,
  guard: () => true,
  labelled: () => createUnwiredGateway(),
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
  const sourceGeneration = useRef({ source, generation: 0 });
  if (sourceGeneration.current.source !== source) {
    sourceGeneration.current = { source, generation: sourceGeneration.current.generation + 1 };
  }
  const gateway = useMemo(() => {
    const generation = sourceGeneration.current.generation;
    return source.createGateway({
      get: () => snapshotRef.current,
      update: (next) => {
        if (generation !== sourceGeneration.current.generation) return;
        snapshotRef.current = next(snapshotRef.current);
        setSnapshot(snapshotRef.current);
      },
    });
  },
    [source],
  );
  useEffect(() => {
    if (externalSnapshot) { snapshotRef.current = externalSnapshot; setSnapshot(externalSnapshot); }
  }, [externalSnapshot]);
  const autoloaded = useRef<OrgGateway | null>(null);
  useEffect(() => {
    if (!source.autoload || autoloaded.current === gateway) return;
    autoloaded.current = gateway;
    void gateway.refresh(null);
  }, [source, gateway]);
  const [orgIntent, setOrgIntent] = useState<OrgViewIntent | null>(null);
  const [agentIntent, setAgentIntent] = useState<AgentViewIntent | null>(null);

  const clearOrgIntent = useCallback(() => setOrgIntent(null), []);
  const clearAgentIntent = useCallback(() => setAgentIntent(null), []);
  const [postLoginHandled, setPostLoginHandled] = useState(false);
  const markPostLoginHandled = useCallback(() => setPostLoginHandled(true), []);

  const refusal = useCallback(
    (method: OrgGuardKey) => source.refusal?.(method) ?? null,
    [source],
  );
  const tabRefusal = useCallback(
    (tab: OrgTab) => source.tabRefusal?.(tab) ?? null,
    [source],
  );
  const tabShown = useCallback(
    (tab: OrgTab) => (source.provides?.(tab) ?? true) && (source.tabRefusal?.(tab) ?? null) === null,
    [source],
  );
  const value = useMemo<OrgDomainValue>(
    () => ({
      provided: true,
      snapshot,
      gateway,
      refusal,
      tabRefusal,
      tabShown,
      guard: () => true,
      labelled: () => gateway,
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
      refusal,
      tabRefusal,
      tabShown,
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
  const value = useContext(OrgDomainContext);
  const notifyUnavailable = useNotifyUnavailable();
  const notifyRef = useRef(notifyUnavailable);
  notifyRef.current = notifyUnavailable;
  const reporting = useMemo(
    () => reportingGateway(value.gateway, (subject, detail) => notifyRef.current(subject, detail)),
    [value.gateway],
  );
  const { refusal } = value;
  const labelledCache = useRef<{ gateway: OrgGateway; items: Map<string, OrgGateway> } | null>(null);
  const labelled = useCallback(
    (subject: string): OrgGateway => {
      if (labelledCache.current?.gateway !== value.gateway) {
        labelledCache.current = { gateway: value.gateway, items: new Map() };
      }
      const { items } = labelledCache.current;
      const cached = items.get(subject);
      if (cached) return cached;
      const created = reportingGateway(value.gateway, (_subject, detail) => notifyRef.current(subject, detail));
      items.set(subject, created);
      return created;
    },
    [value.gateway],
  );
  const guard = useCallback(
    (subject: string, ...methods: readonly OrgGuardKey[]) => {
      for (const method of methods) {
        const refused = refusal(method);
        if (refused) {
          notifyRef.current(subject, { ...refused, source: refused.source ?? method });
          return false;
        }
      }
      return true;
    },
    [refusal],
  );
  return useMemo(() => ({ ...value, gateway: reporting, guard, labelled }), [value, reporting, guard, labelled]);
}

/** 显式提供的未接通数据源：其他 tab 也显示未接通状态。 */
export const UNWIRED_SOURCE: OrgDomainSource = {
  initial: UNWIRED_SNAPSHOT,
  createGateway: () => createUnwiredGateway(),
};
