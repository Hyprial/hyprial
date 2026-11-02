import type { ChatSeed } from '../../state/types';
import type { FirstRunState } from '../../state/types';
import type { AgentSeed } from '../../state/agent-selectors';
import type { FixtureAgent } from '../../fixtures/demo';
import type { ConnectionSummary } from '../../state/gui-view-model';
import type { OrgViewModel } from '../../pages/org/org-view-model';
import type { MachineSectionModel } from '../../features/network/machine-view-model';
import type { TopologyModel } from '../../features/network/topology-view-model';
import type { ModelServicesSectionModel } from '../../features/connections/model-services-view-model';
import type { LinkedSession } from './session-types';
import { sessionStepId } from './session-reducer';

const SQUIRE_ID = 'agent-squire';
export const FEISHU_CHANNEL_ID = 'channel-feishu';

function firstRunStatus(session: LinkedSession): FirstRunState['status'] {
  if (session.firstRun.status === 'completed') {
    return 'complete';
  }
  if (session.firstRun.error) {
    return 'failed';
  }
  if (session.firstRun.pending) {
    return 'pending';
  }
  return 'idle';
}

export function selectFirstRunState(session: LinkedSession): FirstRunState {
  return {
    mode: session.firstRun.mode,
    step: sessionStepId(session.firstRun.step),
    status: firstRunStatus(session),
    provider: session.firstRun.provider,
    modelMethod: session.firstRun.modelMethod,
    progress: session.firstRun.progress,
    error: session.firstRun.error,
  };
}

const CHANNEL_OPEN_ID = 'channel-linked-open';

export function selectConnections(session: LinkedSession): readonly ConnectionSummary[] {
  // Always present so a newly created Agent can render its channel state in the
  // same commit that adds it.
  const hasUnconnected = true;
  return [
    ...session.modelServices.map((service) => ({
      id: service.id,
      kind: 'model' as const,
      label: service.label,
      detail: '已连接',
      status: 'connected' as const,
      usedBy: session.agents.filter(
        (agent) => agent.modelServiceId === service.id,
      ).length,
    })),
    ...(session.messageConnection === 'connected'
      ? [
          {
            id: FEISHU_CHANNEL_ID,
            kind: 'adapter' as const,
            label: '消息连接',
            detail: '已连接',
            status: 'connected' as const,
            usedBy: 1,
          },
        ]
      : []),
    ...(hasUnconnected
      ? [
          {
            id: CHANNEL_OPEN_ID,
            kind: 'adapter' as const,
            label: '消息连接',
            detail: '未配置',
            status: 'unconfigured' as const,
            usedBy: 0,
          },
        ]
      : []),
  ];
}

export function selectAgentSeed(session: LinkedSession): AgentSeed {
  const connections = selectConnections(session);
  const modelLabel =
    session.modelServices[0]?.label ?? '未绑定模型服务';
  return {
    connections,
    organizations: session.org ? [{ id: session.org.id, label: session.org.name }] : [],
    agents: session.agents.map((agent) => ({
      id: agent.id,
      displayName: agent.name,
      role: agent.role,
      avatarTone: agent.id === SQUIRE_ID ? 'green' : 'lilac',
      modelServiceId: agent.modelServiceId ?? undefined,
      channelServiceId:
        session.messageConnection === 'connected' &&
        session.messageConnectionAgentId === agent.id
          ? FEISHU_CHANNEL_ID
          : CHANNEL_OPEN_ID,
      organizationId: session.org?.id ?? null,
      modelLabel,
      capabilities: [],
    })),
  };
}

export function selectChatSeed(session: LinkedSession): ChatSeed {
  return {
    availableTargetId: SQUIRE_ID,
    sendMode: 'reply',
    replyText: '收到，我会继续整理。',
    threads: {
      [SQUIRE_ID]: session.messages.map((message, index) => ({
        id: message.id,
        targetId: SQUIRE_ID,
        author: message.author === 'me' ? ('user' as const) : ('assistant' as const),
        text: message.text,
        delivery: 'sent' as const,
        createdAt: undefined,
        /** keep deterministic order */
        ...(index === -1 ? {} : {}),
      })),
    },
  };
}

function hasJoined(session: LinkedSession): boolean {
  return session.members.some(
    (member) => member.identity === session.activeViewer,
  );
}

function ownerIsOwner(session: LinkedSession): boolean {
  return session.members.some(
    (member) => member.identity === 'owner' && member.role === 'owner',
  );
}

export function selectOrgModel(session: LinkedSession): OrgViewModel {
  const joined = hasJoined(session);
  const isOwner = session.activeViewer === 'owner';
  const org = session.org;
  const pendingInvite = session.invites.find(
    (invite) => invite.state === 'pending',
  );

  const showCreate = org === null && isOwner;
  const showJoin = !joined;
  const showInvite = org !== null && isOwner && ownerIsOwner(session);
  const showMembers = org !== null && session.members.length > 0;

  return {
    scene: org ? 'member-joined' : 'created-empty',
    statusLabel: org ? '已创建' : '未连接',
    createEnabled: showCreate,
    inviteEnabled: showInvite,
    joinEnabled: showJoin,
    members: session.members.map((member) => ({
      id: member.id,
      name: member.name,
      status: 'joined' as const,
    })),
    membersEmptyText: showMembers ? '还没有其他成员' : null,
    showMembers,
    showCreate,
    showInvite,
    showJoin,
    invitation: pendingInvite
      ? {
          state: 'pending',
          label: '待领取',
          note: '已加入后自动更新',
          link: pendingInvite.code,
          code: pendingInvite.code,
        }
      : { state: 'none' },
    join:
      session.firstRun.error === '邀请码无效'
        ? { state: 'failed', message: '邀请码无效', draft: '' }
        : { state: 'idle', draft: '' },
    unavailableNote: null,
    actions: {
      create: showCreate ? 'success' : 'unavailable',
      invite: showInvite ? 'success' : 'unavailable',
      join: showJoin ? 'success' : 'unavailable',
    },
  };
}

export function selectMachineModel(session: LinkedSession): MachineSectionModel {
  return {
    scene: session.org ? 'linked' : 'hidden',
    title: '机器',
    machines: session.machines.map((machine) => ({
      id: machine.id,
      hostname: machine.label,
      system: machine.os,
      joinedAt: machine.joinedAt,
      connection: 'online' as const,
      current: machine.isThisComputer,
    })),
    emptyText: session.org && session.machines.length === 0 ? '还没有机器记录' : null,
    unavailableNote: null,
    currentLabel: '这台电脑',
  };
}

export function selectTopologyModel(session: LinkedSession): TopologyModel {
  const nodes = [
    ...session.members.map((member) => ({
      id: `member-${member.id}`,
      kind: 'member' as const,
      label: member.name,
      meta: '成员 · 已加入',
    })),
    ...session.machines.map((machine) => ({
      id: `machine-${machine.id}`,
      kind: 'machine' as const,
      label: machine.label,
      meta: `${machine.os} · 在线`,
    })),
  ];
  const available = session.communication === 'available' && nodes.length > 0;
  const member = nodes.find((node) => node.kind === 'member');
  const machine = nodes.find((node) => node.kind === 'machine');
  const edge =
    available && member && machine
      ? {
          id: 'edge-1',
          fromId: member.id,
          toId: machine.id,
          status: 'completed' as const,
        }
      : null;

  return {
    dataMode: available ? 'full' : 'structure-only',
    nodes,
    edges: edge ? [edge] : [],
    emptyMessage: null,
    unavailableMessage: null,
    monitor: available
      ? {
          mode: 'linked',
          title: '最近通信记录',
          sample:
            member && machine
              ? {
                  fromLabel: member.label,
                  toLabel: machine.label,
                  status: 'completed' as const,
                  observedAt: '2026-10-20 09:35',
                }
              : null,
        }
      : {
          mode: 'unavailable',
          title: '最近通信记录',
          sample: null,
        },
  };
}

/** J10 section model: rows carry the Agent count and the default flag. */
export function selectModelServicesModel(session: LinkedSession): ModelServicesSectionModel {
  return {
    addMode: session.modelAddMode === 'failure' ? 'failure' : 'ready',
    services: session.modelServices.map((service) => ({
      id: service.id,
      label: service.label,
      connected: service.state === 'connected',
      isDefault: service.isDefault,
      agentCount: session.agents.filter(
        (agent) => agent.modelServiceId === service.id,
      ).length,
      method: service.method,
    })),
  };
}

/** J4 projections: one source, two read-only views. */
export function selectMessageConnection(session: LinkedSession): {
  readonly state: 'not-connected' | 'connected' | 'unavailable';
  readonly agentId: string | null;
  readonly authorizeOutcome: 'success' | 'failure';
} {
  return {
    state:
      session.flowScenarios.feishu === 'unavailable'
        ? 'unavailable'
        : session.messageConnection,
    agentId: session.messageConnectionAgentId,
    authorizeOutcome:
      session.flowScenarios.feishu === 'ready' ||
      session.flowScenarios.feishu === 'unavailable'
        ? 'success'
        : 'failure',
  };
}

export function selectChatChannel(
  session: LinkedSession,
): 'in-app' | 'feishu' {
  return session.messageConnection === 'connected'
    ? session.chatChannel
    : 'in-app';
}

/**
 * Chat targets projected from the session. An Agent is sendable only when its
 * model service is connected, so the linked session and the product fixtures
 * never disagree about availability.
 */
export function selectChatTargets(session: LinkedSession): readonly FixtureAgent[] {
  const connected = new Set(
    session.modelServices
      .filter((service) => service.state === 'connected')
      .map((service) => service.id),
  );
  return session.agents.map((agent) => ({
    id: agent.id,
    kind: 'agent' as const,
    displayName: agent.name,
    role: agent.role,
    availability:
      agent.modelServiceId !== null && connected.has(agent.modelServiceId)
        ? ('available' as const)
        : ('needs-connection' as const),
    avatarTone: agent.id === SQUIRE_ID ? ('green' as const) : ('lilac' as const),
    modelServiceId: agent.modelServiceId ?? undefined,
    modelLabel:
      session.modelServices.find(
        (service) => service.id === agent.modelServiceId,
      )?.label ?? '未绑定模型服务',
    capabilities: [],
  }));
}
