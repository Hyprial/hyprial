import { useEffect, useRef, useState } from 'react';
import type { Dispatch } from 'react';
import type { StubOperations } from '../adapters/stub';
import {
  AGENT_NOT_FOUND,
  filterManagedAgents,
  getManagedAgent,
  selectManagedAgents,
  type AgentSeed,
  type ManagedAgentView,
} from '../state/agent-selectors';
import type { AppAction, AppState } from '../state/types';
import {
  createUnavailableAgentGateway,
  type AgentDraft,
  type AgentMutationGateway,
  type AgentMutationResult,
} from '../features/agents/AgentMutationGateway';
import {
  getAgentExtension,
  type AgentExtensionSeed,
  type AgentExtensionView,
} from '../features/agents/agent-extensions';
import { AgentDetail } from '../features/agents/AgentDetail';
import { AgentForm } from '../features/agents/AgentForm';
import { AgentList } from '../features/agents/AgentList';
import { AgentOrgPanel } from '../features/org/crosstab/AgentOrgPanel';
import { Card } from '../components/primitives';
import '../features/agents/agents.css';

interface ContactsPageProps {
  readonly unavailable?: boolean;
  readonly state: AppState;
  readonly dispatch: Dispatch<AppAction>;
  readonly stub: StubOperations;
  readonly seed?: AgentSeed;
  readonly gateway?: AgentMutationGateway;
  /**
   * dev-02 private dimensions (runtime, provider grants, preset marker).
   * Absent in the product build: every dimension then renders as unknown.
   */
  readonly extensions?: AgentExtensionSeed;
}

export function ContactsPage({
  unavailable = false,
  state,
  dispatch,
  seed,
  gateway,
  extensions,
}: ContactsPageProps) {
  const baseAgents = selectManagedAgents(state, seed);
  const [localAgents, setLocalAgents] = useState<
    readonly ManagedAgentView[] | null
  >(null);
  const agents = localAgents ?? baseAgents;
  const [gatewayClient] = useState<AgentMutationGateway>(
    () => gateway ?? createUnavailableAgentGateway(),
  );
  const [pending, setPending] = useState(false);
  const [formMessage, setFormMessage] = useState<
    { tone: 'error' | 'status'; text: string } | null
  >(null);
  const [createdNotice, setCreatedNotice] = useState<string | null>(null);
  /** Page-local extension overrides for agents created in this session. */
  const [localExtensions, setLocalExtensions] = useState<
    Record<string, Partial<AgentExtensionView>>
  >({});
  const mergedExtensions: Record<string, Partial<AgentExtensionView>> = {
    ...extensions,
    ...localExtensions,
  };
  const listHeadingRef = useRef<HTMLHeadingElement>(null);
  const notFoundTitleRef = useRef<HTMLHeadingElement>(null);
  const [focusListWhenMounted, setFocusListWhenMounted] = useState(false);
  // 所有者/主机筛选是表现层维度：留在页面本地，不入全局状态。
  const [ownerFilter, setOwnerFilter] = useState('');
  const [hostFilter, setHostFilter] = useState('');

  const organizations = seed?.organizations ?? [];
  // 选项取自全量数据（而非当前过滤结果），避免选中后被自己的过滤器困住。
  const ownerOptions = [...new Set(agents.map((agent) => agent.ownerLabel))];
  const hostOptions = [...new Set(agents.map((agent) => agent.host))];
  const afterDimensions = agents.filter(
    (agent) =>
      (ownerFilter === '' || agent.ownerLabel === ownerFilter) &&
      (hostFilter === '' || agent.host === hostFilter),
  );
  const visibleAgents = filterManagedAgents(
    afterDimensions,
    state.agentQuery,
    state.agentFilter,
  );

  const panel = state.agentPanel;
  const detailAgent =
    panel.kind === 'detail' ? getManagedAgent(agents, panel.agentId) : undefined;
  const extensionViews = Object.fromEntries(
    agents.map((agent) => [
      agent.id,
      getAgentExtension(mergedExtensions, agent.id),
    ]),
  );

  function navigateToConnections(): void {
    dispatch({ type: 'navigate', page: 'connections' });
  }

  useEffect(() => {
    if (panel.kind === 'detail' && !detailAgent) {
      notFoundTitleRef.current?.focus();
    }
  }, [panel, detailAgent]);

  // Focus the list title only once the list has really mounted.
  useEffect(() => {
    if (focusListWhenMounted && panel.kind === 'list') {
      listHeadingRef.current?.focus();
      setFocusListWhenMounted(false);
    }
  }, [focusListWhenMounted, panel.kind]);

  function backToList(): void {
    setFormMessage(null);
    setFocusListWhenMounted(true);
    dispatch({ type: 'close-agent-panel' });
  }

  function applyResult(result: AgentMutationResult, draft?: AgentDraft): void {
    if (result.kind === 'created') {
      setLocalAgents([
        ...agents.filter((agent) => agent.id !== result.agent.id),
        result.agent,
      ]);
      if (draft?.preferredHarness) {
        setLocalExtensions((current) => ({
          ...current,
          [result.agent.id]: {
            preferredHarness: draft.preferredHarness ?? undefined,
          },
        }));
      }
      setFormMessage(null);
      setCreatedNotice('Agent 创建已完成。');
      setFocusListWhenMounted(true);
      dispatch({ type: 'close-agent-panel' });
      return;
    }
    if (result.kind === 'updated') {
      setLocalAgents(
        agents.map((agent) =>
          agent.id === result.agent.id ? result.agent : agent,
        ),
      );
    }
  }

  async function createAgent(draft: AgentDraft): Promise<void> {
    setPending(true);
    const result = await gatewayClient.create(draft);
    setPending(false);
    applyResult(result, draft);
    if (result.kind === 'failed' || result.kind === 'unknown') {
      setFormMessage({ tone: 'error', text: result.message });
    }
    if (result.kind === 'unavailable') {
      setFormMessage({ tone: 'error', text: result.message });
    }
  }

  async function updateAgent(
    agentId: string,
    draft: AgentDraft,
  ): Promise<AgentMutationResult> {
    const result = await gatewayClient.update(agentId, draft);
    applyResult(result);
    return result;
  }

  async function deleteAgent(agentId: string): Promise<AgentMutationResult> {
    const result = await gatewayClient.delete(agentId);
    if (result.kind === 'deleted') {
      setLocalAgents(agents.filter((agent) => agent.id !== result.agentId));
      setFocusListWhenMounted(true);
      dispatch({ type: 'close-agent-panel' });
    }
    return result;
  }

  return (
    <section className="page page-contacts" data-page="contacts">
      {panel.kind === 'list' ? (
        <h1 className="page-title">Agent</h1>
      ) : (
        <>
          {/* 页面标识保持 h1「Agent」（视觉隐藏），可见头是统一面包屑：
              父级回列表，当前级随子视图变化（详情名 / 创建 / 详情）。 */}
          <h1 className="sr-only">Agent</h1>
          <nav className="page-breadcrumb" aria-label="面包屑">
            <ol>
              <li>
                <a
                  href="#agents"
                  onClick={(event) => {
                    event.preventDefault();
                    backToList();
                  }}
                >
                  Agent
                </a>
              </li>
              <li aria-current="page">
                {panel.kind === 'create'
                  ? '创建 Agent'
                  : detailAgent
                    ? detailAgent.displayName
                    : '详情'}
              </li>
            </ol>
          </nav>
        </>
      )}

      {createdNotice && panel.kind === 'list' && (
        <p className="agent-form-message" role="status">
          {createdNotice}
        </p>
      )}

      {panel.kind === 'create' && (
        <AgentForm
          mode="create"
          initial={{
            displayName: '',
            role: '',
            modelServiceId: '',
            organizationId: null,
          }}
          organizations={organizations}
          pending={pending}
          message={formMessage}
          onSubmit={(draft) => void createAgent(draft)}
          onCancel={() => {
            setFormMessage(null);
            backToList();
          }}
        />
      )}

      {panel.kind === 'detail' && detailAgent && (
        <AgentDetail
          agent={detailAgent}
          extension={extensionViews[detailAgent.id]}
          organizations={organizations}
          onNavigateConnections={navigateToConnections}
          onSave={(draft) => updateAgent(detailAgent.id, draft)}
          onDelete={() => deleteAgent(detailAgent.id)}
          onStartChat={
            detailAgent.availability === 'available' &&
            state.targets.some((target) => target.id === detailAgent.id)
              ? () =>
                  dispatch({ type: 'open-chat', targetId: detailAgent.id })
              : undefined
          }
          orgSections={
            <AgentOrgPanel
              agentId={detailAgent.id}
              agentName={detailAgent.displayName}
              dispatch={dispatch}
            />
          }
        />
      )}

      {panel.kind === 'detail' && !detailAgent && (
        <Card
          as="section"
          className="card agent-panel agent-not-found"
          role="status"
          aria-labelledby="agent-not-found-title"
        >
          <h2 id="agent-not-found-title" ref={notFoundTitleRef} tabIndex={-1}>
            {AGENT_NOT_FOUND}
          </h2>
          <p className="copy">该 Agent 可能已被删除，或列表状态已变化。</p>
        </Card>
      )}

      {panel.kind === 'list' && (
        <AgentList unavailable={unavailable}
          agents={visibleAgents}
          total={agents.length}
          query={state.agentQuery}
          filter={state.agentFilter}
          extensions={extensionViews}
          headingRef={listHeadingRef}
          ownerFilter={ownerFilter}
          hostFilter={hostFilter}
          ownerOptions={ownerOptions}
          hostOptions={hostOptions}
          onOwnerFilter={setOwnerFilter}
          onHostFilter={setHostFilter}
          onCreate={() => {
            setFormMessage(null);
            setCreatedNotice(null);
            dispatch({ type: 'open-agent-create' });
          }}
          onOpen={(agentId) =>
            dispatch({ type: 'open-agent-detail', agentId })
          }
          onQuery={(value) => dispatch({ type: 'set-agent-query', value })}
          onFilter={(filter) => dispatch({ type: 'set-agent-filter', filter })}
          onClearFilters={() => {
            setOwnerFilter('');
            setHostFilter('');
            dispatch({ type: 'clear-agent-filters' });
          }}
        />
      )}
    </section>
  );
}
