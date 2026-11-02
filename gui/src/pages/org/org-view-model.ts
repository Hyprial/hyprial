/**
 * J5 organization journey view model.
 *
 * The organization slice renders from a deterministic model so the dev-mode
 * scenarios (and later previews) can pick a state through props. The default
 * model keeps the current "unavailable" behavior: no org API is wired, and no
 * action may claim success.
 */

export type OrgScene =
  | 'unavailable'
  | 'created-empty'
  | 'invite-pending'
  | 'join-failed'
  | 'member-joined';

export type OrgMemberStatus = 'joined';

export interface OrgMember {
  readonly id: string;
  readonly name: string;
  readonly status: OrgMemberStatus;
}

export interface OrgInvitationState {
  readonly state: 'none' | 'pending';
  readonly label?: string;
  readonly note?: string;
  readonly link?: string;
  readonly code?: string;
}

export type OrgCopyKind = 'link' | 'code';

/** Copy feedback is expressed through this callback; no adapter is added. */
export type OrgCopyHandler = (
  kind: OrgCopyKind,
  text: string,
) => boolean | Promise<boolean>;

export interface OrgJoinState {
  readonly state: 'idle' | 'failed';
  readonly message?: string;
  readonly draft: string;
}

/**
 * How a form submit resolves in this composition.
 *
 * `unavailable` keeps the button disabled: no interface exists. `failure`
 * resolves to the local failure message and keeps every input. `pending` never
 * resolves; tests use it to prove duplicate submits are blocked. No outcome
 * fabricates a success state.
 */
export type OrgActionOutcome = 'unavailable' | 'failure' | 'pending' | 'success';

export interface OrgActionPlan {
  readonly create: OrgActionOutcome;
  readonly invite: OrgActionOutcome;
  readonly join: OrgActionOutcome;
}

export interface OrgViewModel {
  readonly scene: OrgScene;
  readonly statusLabel: string;
  readonly createEnabled: boolean;
  readonly inviteEnabled: boolean;
  readonly joinEnabled: boolean;
  readonly members: readonly OrgMember[];
  readonly membersEmptyText: string | null;
  /** False before an organization exists, so no member card is rendered. */
  readonly showMembers: boolean;
  /** Section visibility for the permission matrix. */
  readonly showCreate: boolean;
  readonly showInvite: boolean;
  readonly showJoin: boolean;
  readonly invitation: OrgInvitationState;
  readonly join: OrgJoinState;
  readonly unavailableNote: string | null;
  readonly actions: OrgActionPlan;
}

const JOIN_FAILED_MESSAGE = '加入组织未完成。请检查邀请链接后重试。';

export const ORG_VALIDATION_MESSAGE: Readonly<
  Record<'create' | 'invite' | 'join', string>
> = {
  create: '请填写组织名称',
  invite: '请填写组织和成员账号',
  join: '请填写邀请码',
};

export const ORG_FAILURE_MESSAGE: Readonly<
  Record<'create' | 'invite' | 'join', string>
> = {
  create: '组织创建未完成。请检查组织名称后重试。',
  invite: '邀请未生成。请检查组织和成员账号后重试。',
  join: JOIN_FAILED_MESSAGE,
};

const ENABLED_PLAN: OrgActionPlan = {
  create: 'failure',
  invite: 'failure',
  join: 'failure',
};

const BASE: Omit<
  OrgViewModel,
  'scene' | 'statusLabel' | 'showMembers' | 'showCreate' | 'showInvite' | 'showJoin' | 'actions'
> = {
  createEnabled: false,
  inviteEnabled: false,
  joinEnabled: false,
  members: [],
  membersEmptyText: null,
  invitation: { state: 'none' },
  join: { state: 'idle', draft: '' },
  unavailableNote: null,
};

function withPlan(
  plan: Partial<OrgActionPlan> | undefined,
  fallback: OrgActionPlan,
): OrgActionPlan {
  return { ...fallback, ...plan };
}

function enabled(plan: OrgActionPlan) {
  return {
    createEnabled: plan.create !== 'unavailable',
    inviteEnabled: plan.invite !== 'unavailable',
    joinEnabled: plan.join !== 'unavailable',
  };
}

export function buildOrgViewModel(
  scene: OrgScene,
  planOverride?: Partial<OrgActionPlan>,
): OrgViewModel {
  switch (scene) {
    case 'created-empty': {
      const actions = withPlan(planOverride, ENABLED_PLAN);
      return {
        ...BASE,
        ...enabled(actions),
        scene,
        statusLabel: '已创建',
        showMembers: true,
        showCreate: actions.create !== 'unavailable',
        showInvite: actions.invite !== 'unavailable',
        showJoin: actions.join !== 'unavailable',
        membersEmptyText: '还没有其他成员',
        actions,
      };
    }
    case 'invite-pending': {
      const actions = withPlan(planOverride, ENABLED_PLAN);
      return {
        ...BASE,
        ...enabled(actions),
        scene,
        statusLabel: '邀请待领取',
        showMembers: true,
        showCreate: actions.create !== 'unavailable',
        showInvite: actions.invite !== 'unavailable',
        showJoin: actions.join !== 'unavailable',
        invitation: {
          state: 'pending',
          label: '待领取',
          note: '邀请创建成功',
          link: 'invite-link-placeholder',
          code: 'invite-code-placeholder',
        },
        membersEmptyText: '还没有其他成员',
        actions,
      };
    }
    case 'join-failed': {
      const actions = withPlan(planOverride, ENABLED_PLAN);
      return {
        ...BASE,
        ...enabled(actions),
        scene,
        statusLabel: '加入未完成',
        showMembers: false,
        showCreate: false,
        showInvite: false,
        showJoin: true,
        join: {
          state: 'failed',
          message: JOIN_FAILED_MESSAGE,
          draft: 'invite-code-kept',
        },
        actions,
      };
    }
    case 'member-joined': {
      const actions = withPlan(planOverride, ENABLED_PLAN);
      return {
        ...BASE,
        ...enabled(actions),
        scene,
        statusLabel: '成员已加入',
        showMembers: true,
        showCreate: actions.create !== 'unavailable',
        showInvite: actions.invite !== 'unavailable',
        showJoin: actions.join !== 'unavailable',
        members: [{ id: 'member-one', name: '新成员一', status: 'joined' }],
        membersEmptyText: null,
        actions,
      };
    }
    case 'unavailable':
    default: {
      const actions = withPlan(planOverride, {
        create: 'unavailable',
        invite: 'unavailable',
        join: 'unavailable',
      });
      return {
        ...BASE,
        ...enabled(actions),
        scene: 'unavailable',
        statusLabel: '暂不可用',
        showMembers: false,
        showCreate: false,
        showInvite: false,
        showJoin: false,
        unavailableNote: '暂不可用',
        actions,
      };
    }
  }
}
