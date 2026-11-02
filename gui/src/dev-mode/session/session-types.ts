import type {
  AgentDraft,
} from '../../features/agents/AgentMutationGateway';
import type {
  FirstRunMode,
  FirstRunStep,
  ModelConnectionMethod,
  ModelProvider,
} from '../../state/types';

/** Development-only demo identities. */
export type Viewer = 'owner' | 'invitee';

export interface SessionAgent {
  readonly id: string;
  readonly name: string;
  readonly role: string;
  readonly modelServiceId: string | null;
  readonly state: 'idle' | 'working';
}

export interface SessionMessage {
  readonly id: string;
  readonly agentId: string;
  readonly author: 'me' | 'squire';
  readonly text: string;
  readonly state: 'sent' | 'failed';
}

export interface SessionMember {
  readonly id: string;
  readonly identity: Viewer;
  readonly name: string;
  readonly role: 'owner' | 'member';
  readonly state: 'joined';
}

export interface SessionInvite {
  readonly code: string;
  readonly identity: Viewer;
  readonly state: 'pending' | 'accepted';
}

export interface SessionMachine {
  readonly id: string;
  readonly label: string;
  readonly os: string;
  readonly joinedAt: string;
  readonly isThisComputer: boolean;
  readonly status: 'online';
}

export interface LinkedSession {
  readonly revision: number;
  readonly activeViewer: Viewer;
  readonly firstRun: {
    readonly step: 1 | 2 | 3 | 4;
    readonly status: 'in_progress' | 'completed';
    /** Mirrors the product page's pending state so its step timers run. */
    readonly pending: boolean;
    readonly accountLabel: string | null;
    readonly modelServiceId: string | null;
    readonly squireReady: boolean;
    readonly provider: ModelProvider | null;
    readonly modelMethod: ModelConnectionMethod | null;
    readonly mode: FirstRunMode;
    readonly progress: number;
    readonly error: string | null;
  };
  readonly modelServices: readonly {
    readonly id: string;
    readonly label: string;
    readonly state: 'connected';
    readonly isDefault: boolean;
    readonly method: 'account' | 'api-key';
  }[];
  /** Development-only switch for the add-failure regression. */
  readonly modelAddMode: 'ready' | 'failure';
  /** Development-only seed: the first run may produce no model service. */
  readonly modelServiceSeed: 'default' | 'empty';
  /** Single source for the Feishu message connection (J4). */
  readonly messageConnection: 'not-connected' | 'connected';
  readonly messageConnectionAgentId: string | null;
  readonly flowScenarios: { readonly feishu: 'ready' | 'authorize-failure' | 'unavailable' };
  /** Chat channel; switching is only valid while the connection is connected. */
  readonly chatChannel: 'in-app' | 'feishu';
  readonly agents: readonly SessionAgent[];
  readonly messages: readonly SessionMessage[];
  readonly org: { readonly id: string; readonly name: string; readonly inviteCode: string } | null;
  readonly members: readonly SessionMember[];
  readonly invites: readonly SessionInvite[];
  readonly devices: { readonly local: { readonly id: string; readonly host: string; readonly os: string } };
  readonly machines: readonly SessionMachine[];
  readonly communication: 'available' | 'unavailable';
  readonly seq: { readonly agent: number; readonly message: number; readonly machine: number };
  readonly lastResult: string | null;
}

export type SessionAction =
  | { readonly type: 'first-run-select-provider'; readonly provider: ModelProvider }
  | { readonly type: 'first-run-select-model-method'; readonly method: ModelConnectionMethod }
  | { readonly type: 'first-run-submit'; readonly step: FirstRunStep }
  | { readonly type: 'first-run-progress'; readonly value: number }
  | { readonly type: 'first-run-succeed'; readonly step: FirstRunStep; readonly previewOnly?: boolean }
  | { readonly type: 'first-run-fail'; readonly step: FirstRunStep; readonly message: string }
  | { readonly type: 'first-run-retry' }
  | { readonly type: 'first-run-back' }
  | {
      readonly type: 'model-add';
      readonly label: string;
      readonly method: 'account' | 'api-key';
      /** Never stored in the session; only the component holds it transiently. */
      readonly credential?: string;
    }
  | { readonly type: 'model-set-default'; readonly serviceId: string }
  | { readonly type: 'feishu-authorize'; readonly agentId: string }
  | { readonly type: 'chat-channel-set'; readonly channel: 'in-app' | 'feishu' }
  | { readonly type: 'flow-feishu-set'; readonly value: 'ready' | 'authorize-failure' | 'unavailable' }
  | { readonly type: 'agent-create'; readonly draft: AgentDraft }
  | { readonly type: 'org-create'; readonly name: string }
  | { readonly type: 'org-invite' }
  | { readonly type: 'org-join'; readonly code: string }
  | { readonly type: 'viewer-set'; readonly viewer: Viewer }
  | { readonly type: 'machine-add' }
  | { readonly type: 'communication-set'; readonly value: 'available' | 'unavailable' }
  | { readonly type: 'result-clear' };

export const EXTERNAL_INVITE_CODE = 'GUI-LINK-001';
export const OWNER_INVITE_CODE = 'GUI-ORG-001';
export const INVALID_CODE = '邀请码无效';
