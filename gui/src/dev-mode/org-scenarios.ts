import {
  buildOrgViewModel,
  type OrgViewModel,
} from '../pages/org/org-view-model';

/**
 * J5 development scenarios.
 *
 * Only the four mock scenes from R4 are selectable. Create and invite failures
 * have no scene number, so they are exercised by injected action plans in tests
 * instead of a new selector value.
 */
export const J5_SCENARIOS = [
  { value: 'baseline', label: '基线（无组织数据）' },
  { value: 'm11', label: 'M-11 组织已创建，无成员' },
  { value: 'm12', label: 'M-12 邀请待领取' },
  { value: 'm13', label: 'M-13 加入失败' },
  { value: 'm25', label: 'M-25 成员已加入' },
] as const;

export type J5Scenario = (typeof J5_SCENARIOS)[number]['value'];

export function isJ5Scenario(value: string): value is J5Scenario {
  return J5_SCENARIOS.some((option) => option.value === value);
}

export function buildOrgScenario(
  scenario: J5Scenario,
): OrgViewModel | undefined {
  switch (scenario) {
    case 'm11':
      return buildOrgViewModel('created-empty');
    case 'm12':
      return buildOrgViewModel('invite-pending');
    case 'm13':
      return buildOrgViewModel('join-failed');
    case 'm25':
      return buildOrgViewModel('member-joined');
    case 'baseline':
    default:
      return undefined;
  }
}
