import type { AgentDraft, AgentMutationGateway } from '../../features/agents';
import { buildManagedAgent } from '../../state/agent-selectors';
import type { SessionAction } from './session-types';
import { selectConnections } from './session-selectors';
import type { LinkedSession } from './session-types';

/**
 * Session-backed Agent gateway: create writes to the session; it defines no
 * update or delete, so the page does not render those controls.
 */
export function createSessionAgentGateway(
  getSession: () => LinkedSession,
  dispatch: (action: SessionAction) => void,
): AgentMutationGateway {
  return {
    async create(draft: AgentDraft) {
      const session = getSession();
      // Predict the id the reducer will assign, so the page can render the new
      // card without waiting for a second render.
      const id = `agent-${session.seq.agent + 1}`;
      const record = {
        id,
        displayName: draft.displayName,
        role: draft.role,
        avatarTone: 'lilac' as const,
        modelServiceId: draft.modelServiceId || undefined,
        channelServiceId: 'channel-linked-open',
        organizationId: draft.organizationId,
        modelLabel:
          session.modelServices.find(
            (service) => service.id === draft.modelServiceId,
          )?.label ?? '模型服务',
        capabilities: [],
      };
      dispatch({ type: 'agent-create', draft });
      return {
        kind: 'created',
        agent: buildManagedAgent(
          record,
          selectConnections(session),
          session.org ? [{ id: session.org.id, label: session.org.name }] : [],
        ),
      };
    },
  };
}
