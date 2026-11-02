import type {
  FirstRunStep,
  ModelConnectionMethod,
} from '../../state/types';
import type { LinkedSession, SessionAction, Viewer } from './session-types';
import {
  EXTERNAL_INVITE_CODE,
  INVALID_CODE,
  OWNER_INVITE_CODE,
} from './session-types';

const STEP_IDS: readonly FirstRunStep[] = [
  'account',
  'model',
  'squire',
  'conversation',
];

const SQUIRE_ID = 'agent-squire';
const SQUIRE_NAME = 'Squire';
const SQUIRE_ROLE = '个人助理 · 统一入口';
const OWNER_NAME = '创建者';
const INVITEE_NAME = '受邀成员';
const FIRST_MESSAGE = '你好，Squire';
const SQUIRE_REPLY = '你好，我是 Squire。今天的安排已经整理好了。';

const MODEL_LABELS: Readonly<Record<'service-a' | 'service-b', string>> = {
  'service-a': '模型服务 A',
  'service-b': '模型服务 B',
};

function stepNumber(step: FirstRunStep): 1 | 2 | 3 | 4 {
  return (STEP_IDS.indexOf(step) + 1) as 1 | 2 | 3 | 4;
}

function stepId(step: number): FirstRunStep {
  return STEP_IDS[step - 1] ?? 'account';
}

function touch(session: LinkedSession): number {
  return session.revision + 1;
}

function nextId(prefix: string, value: number): string {
  return `${prefix}-${value}`;
}

function withResult(
  session: LinkedSession,
  patch: Partial<LinkedSession>,
  result: string,
): LinkedSession {
  return { ...session, ...patch, revision: touch(session), lastResult: result };
}

export function sessionReducer(
  session: LinkedSession,
  action: SessionAction,
): LinkedSession {
  switch (action.type) {
    case 'first-run-select-provider': {
      if (session.firstRun.status !== 'in_progress') {
        return session;
      }
      return {
        ...session,
        revision: touch(session),
        firstRun: { ...session.firstRun, provider: action.provider },
      };
    }

    case 'first-run-select-model-method': {
      if (session.firstRun.status !== 'in_progress') {
        return session;
      }
      return {
        ...session,
        revision: touch(session),
        firstRun: { ...session.firstRun, modelMethod: action.method },
      };
    }

    case 'first-run-submit': {
      if (
        session.firstRun.status === 'completed' ||
        stepId(session.firstRun.step) !== action.step
      ) {
        return session;
      }
      return {
        ...session,
        revision: touch(session),
        firstRun: {
          ...session.firstRun,
          pending: true,
          error: null,
          progress: 0,
          accountLabel:
            action.step === 'account'
              ? (session.firstRun.accountLabel ?? '演示账号')
              : session.firstRun.accountLabel,
        },
      };
    }

    case 'first-run-progress': {
      if (session.firstRun.status === 'completed') {
        return session;
      }
      return {
        ...session,
        revision: touch(session),
        firstRun: {
          ...session.firstRun,
          progress: Math.min(100, Math.max(0, action.value)),
        },
      };
    }

    case 'first-run-fail': {
      if (session.firstRun.status === 'completed') {
        return session;
      }
      return {
        ...session,
        revision: touch(session),
        firstRun: {
          ...session.firstRun,
          pending: false,
          error: action.message,
          progress: 0,
        },
      };
    }

    case 'first-run-retry':
      return {
        ...session,
        revision: touch(session),
        firstRun: { ...session.firstRun, pending: false, error: null, progress: 0 },
      };

    case 'first-run-back': {
      if (session.firstRun.status === 'completed' || session.firstRun.step === 1) {
        return session;
      }
      return {
        ...session,
        revision: touch(session),
        firstRun: {
          ...session.firstRun,
          step: (session.firstRun.step - 1) as 1 | 2 | 3 | 4,
          error: null,
          progress: 0,
        },
      };
    }

    case 'first-run-succeed': {
      if (session.firstRun.status === 'completed') {
        return session;
      }
      if (stepId(session.firstRun.step) !== action.step) {
        return session;
      }

      if (action.step === 'account') {
        return {
          ...session,
          revision: touch(session),
          firstRun: { ...session.firstRun, pending: false, step: 2, error: null, progress: 0 },
        };
      }

      if (action.step === 'model') {
        const provider = session.firstRun.provider ?? 'service-a';
        const label = MODEL_LABELS[provider];
        const existing = session.modelServices.find(
          (service) => service.id === 'model-1',
        );
        const skipSeed = session.modelServiceSeed === 'empty';
        return {
          ...session,
          revision: touch(session),
          modelServices: existing || skipSeed
            ? session.modelServices
            : [
                {
                  id: 'model-1',
                  label,
                  state: 'connected',
                  isDefault: true,
                  method: session.firstRun.modelMethod ?? 'account',
                },
              ],
          firstRun: {
            ...session.firstRun,
            pending: false,
            step: 3,
            modelServiceId: skipSeed ? null : 'model-1',
            error: null,
            progress: 0,
          },
        };
      }

      if (action.step === 'squire') {
        const hasSquire = session.agents.some((agent) => agent.id === SQUIRE_ID);
        return {
          ...session,
          revision: touch(session),
          agents: hasSquire
            ? session.agents
            : [
                {
                  id: SQUIRE_ID,
                  name: SQUIRE_NAME,
                  role: SQUIRE_ROLE,
                  modelServiceId: session.firstRun.modelServiceId ?? 'model-1',
                  state: 'idle',
                },
                ...session.agents,
              ],
          firstRun: {
            ...session.firstRun,
            pending: false,
            step: 4,
            squireReady: true,
            error: null,
            progress: 0,
          },
        };
      }

      const machineSeq = session.seq.machine + 1;
      const messageSeq = session.seq.message + 1;
      const messages = action.previewOnly || session.messages.some((message) => message.id === 'msg-1')
        ? session.messages
        : [
            {
              id: nextId('msg', messageSeq),
              agentId: SQUIRE_ID,
              author: 'me' as const,
              text: FIRST_MESSAGE,
              state: 'sent' as const,
            },
            {
              id: nextId('msg', messageSeq + 1),
              agentId: SQUIRE_ID,
              author: 'squire' as const,
              text: SQUIRE_REPLY,
              state: 'sent' as const,
            },
            ...session.messages,
          ];
      const machines = session.machines.some((machine) => machine.isThisComputer)
        ? session.machines
        : [
            {
              id: nextId('machine', machineSeq),
              label: session.devices.local.host,
              os: session.devices.local.os,
              joinedAt: '2026-10-20 09:30',
              isThisComputer: true,
              status: 'online' as const,
            },
            ...session.machines,
          ];
      return {
        ...session,
        revision: touch(session),
        messages,
        machines,
        seq: {
          ...session.seq,
          message: action.previewOnly ? session.seq.message : Math.max(session.seq.message + 2, messageSeq + 1),
          machine: Math.max(session.seq.machine + 1, machineSeq),
        },
        firstRun: {
          ...session.firstRun,
          pending: false,
          step: 4,
          status: 'completed',
          error: null,
          progress: 100,
        },
        lastResult: action.previewOnly ? '模拟首次设置：草稿待发送，真实回复未确认' : '首次设置已完成',
      };
    }

    case 'model-add': {
      if (session.firstRun.status !== 'completed') {
        return session;
      }
      if (!action.label.trim()) {
        return session;
      }
      if (session.modelAddMode === 'failure') {
        // The section announces the failure once; the dev toolbar stays silent.
        return { ...session, revision: touch(session) };
      }
      const index = session.modelServices.length + 1;
      const id = `model-${index}`;
      const alreadyDefault = session.modelServices.some((service) => service.isDefault);
      return {
        ...session,
        revision: touch(session),
        modelServices: [
          ...session.modelServices,
          {
            id,
            label: action.label.trim(),
            state: 'connected',
            isDefault: !alreadyDefault,
            method: action.method,
          },
        ],
        lastResult: '模型服务添加已完成。',
      };
    }

    case 'model-set-default': {
      if (!session.modelServices.some((service) => service.id === action.serviceId)) {
        return session;
      }
      return {
        ...session,
        revision: touch(session),
        modelServices: session.modelServices.map((service) => ({
          ...service,
          isDefault: service.id === action.serviceId,
        })),
        lastResult: '默认模型服务已更新。',
      };
    }

    case 'feishu-authorize': {
      const agentExists = session.agents.some(
        (agent) => agent.id === action.agentId,
      );
      const authorizationFails =
        session.flowScenarios.feishu !== 'ready' &&
        session.flowScenarios.feishu !== 'unavailable';
      if (!agentExists || authorizationFails) {
        return { ...session, revision: touch(session) };
      }
      return {
        ...session,
        revision: touch(session),
        messageConnection: 'connected',
        messageConnectionAgentId: action.agentId,
        lastResult: '飞书连接已完成。',
      };
    }

    case 'chat-channel-set': {
      if (session.messageConnection !== 'connected') {
        return session;
      }
      return {
        ...session,
        revision: touch(session),
        chatChannel: action.channel,
      };
    }

    case 'flow-feishu-set':
      return {
        ...session,
        revision: touch(session),
        flowScenarios: { ...session.flowScenarios, feishu: action.value },
        messageConnection:
          action.value === 'unavailable' ? 'not-connected' : session.messageConnection,
        messageConnectionAgentId:
          action.value === 'unavailable' ? null : session.messageConnectionAgentId,
        chatChannel:
          action.value === 'unavailable' ? 'in-app' : session.chatChannel,
      };

    case 'agent-create': {
      if (session.firstRun.status !== 'completed') {
        return session;
      }
      if (!action.draft.displayName.trim() || !action.draft.role.trim()) {
        return session;
      }
      if (
        session.agents.some(
          (agent) =>
            agent.name === action.draft.displayName &&
            agent.role === action.draft.role,
        )
      ) {
        return session;
      }
      const seq = session.seq.agent + 1;
      return {
        ...session,
        revision: touch(session),
        seq: { ...session.seq, agent: seq },
        agents: [
          ...session.agents,
          {
            id: nextId('agent', seq),
            name: action.draft.displayName,
            role: action.draft.role,
            modelServiceId: action.draft.modelServiceId || null,
            state: 'idle',
          },
        ],
        lastResult: 'Agent 创建已完成。',
      };
    }

    case 'org-create': {
      const name = action.name.trim();
      if (session.org || !name) {
        return session;
      }
      return withResult(
        session,
        {
          org: { id: 'org-1', name, inviteCode: OWNER_INVITE_CODE },
          members: [
            {
              id: 'member-owner',
              identity: 'owner',
              name: OWNER_NAME,
              role: 'owner',
              state: 'joined',
            },
          ],
          communication: 'available',
        },
        '已创建组织',
      );
    }

    case 'org-invite': {
      const owner = session.members.find(
        (member) => member.identity === 'owner' && member.role === 'owner',
      );
      if (!session.org || session.activeViewer !== 'owner' || !owner) {
        return session;
      }
      if (session.invites.some((invite) => invite.code === OWNER_INVITE_CODE)) {
        return session;
      }
      return withResult(
        session,
        {
          invites: [
            ...session.invites,
            {
              code: OWNER_INVITE_CODE,
              identity: 'invitee',
              state: 'pending',
            },
          ],
        },
        '已生成邀请',
      );
    }

    case 'org-join': {
      const code = action.code.trim();
      const viewer: Viewer = session.activeViewer;
      if (session.members.some((member) => member.identity === viewer)) {
        return session;
      }
      if (
        code === EXTERNAL_INVITE_CODE && session.org !== null
      ) {
        return {
          ...session,
          revision: touch(session),
          firstRun: { ...session.firstRun, error: INVALID_CODE },
        };
      }
      if (code !== EXTERNAL_INVITE_CODE && code !== OWNER_INVITE_CODE) {
        return {
          ...session,
          revision: touch(session),
          firstRun: { ...session.firstRun, error: INVALID_CODE },
        };
      }
      if (code === OWNER_INVITE_CODE && session.org?.inviteCode !== OWNER_INVITE_CODE) {
        return {
          ...session,
          revision: touch(session),
          firstRun: { ...session.firstRun, error: INVALID_CODE },
        };
      }
      const org =
        session.org ??
        (code === EXTERNAL_INVITE_CODE
          ? { id: 'org-external', name: '外部组织', inviteCode: EXTERNAL_INVITE_CODE }
          : { id: 'org-1', name: '设计组', inviteCode: OWNER_INVITE_CODE });
      return withResult(
        session,
        {
          org,
          members: [
            ...session.members,
            {
              id: `member-${viewer}`,
              identity: viewer,
              name: viewer === 'owner' ? OWNER_NAME : INVITEE_NAME,
              role: 'member',
              state: 'joined',
            },
          ],
          invites: session.invites.map((invite) =>
            invite.code === code ? { ...invite, state: 'accepted' as const } : invite,
          ),
          communication: 'available',
          firstRun: { ...session.firstRun, error: null },
        },
        viewer === 'owner' ? '已加入组织' : '已加入组织',
      );
    }

    case 'viewer-set': {
      if (session.activeViewer === action.viewer) {
        return session;
      }
      return withResult(
        session,
        { activeViewer: action.viewer },
        action.viewer === 'invitee' ? '已切换为受邀成员' : '已切换为创建者',
      );
    }

    case 'machine-add': {
      if (!session.org) {
        return session;
      }
      const seq = session.seq.machine + 1;
      return withResult(
        session,
        {
          seq: { ...session.seq, machine: seq },
          machines: [
            ...session.machines,
            {
              id: nextId('machine', seq),
              label: `第二台电脑`,
              os: 'macOS',
              joinedAt: '2026-10-21 14:05',
              isThisComputer: false,
              status: 'online',
            },
          ],
        },
        '已加入第二台电脑',
      );
    }

    case 'communication-set':
      return {
        ...session,
        revision: touch(session),
        communication: action.value,
      };

    case 'result-clear':
      return { ...session, lastResult: null };

    default:
      return session;
  }
}

/** Exposed for selectors that map steps both ways. */
export const SESSION_STEP_IDS = STEP_IDS;
export function sessionStepNumber(step: FirstRunStep): 1 | 2 | 3 | 4 {
  return stepNumber(step);
}
export function sessionStepId(step: number): FirstRunStep {
  return stepId(step);
}
export function modelLabelOf(
  provider: ModelConnectionMethod | 'service-a' | 'service-b' | null,
): string {
  return provider === 'service-b' ? MODEL_LABELS['service-b'] : MODEL_LABELS['service-a'];
}
