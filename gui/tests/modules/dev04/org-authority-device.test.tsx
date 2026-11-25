import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeAll, describe, expect, it } from 'vitest';
import { FixtureApp } from '../../../src/dev/FixtureApp';
import { OrgDomainProvider, type OrgStore } from '../../../src/features/org/impl/domain/context';
import { ORG_COPY } from '../../../src/features/org/impl/domain/copy';
import type { OrgDomainSnapshot } from '../../../src/features/org/impl/domain/types';
import { createOrgLiveSourceForPort } from '../../support/org-backend';
import { openOrgTab, openRail, stubRadixPointerApis } from './org-render';

beforeAll(stubRadixPointerApis);

function fakePort(result: (method: string) => unknown) {
  const calls: string[] = [];
  return {
    calls,
    async request(method: string) {
      calls.push(method);
      return result(method);
    },
  };
}

function memoryStore(initial: OrgDomainSnapshot): OrgStore {
  let current = initial;
  return {
    get: () => current,
    update: (next) => {
      current = next(current);
    },
  };
}

function row(org: string, authorityDevice?: boolean | null) {
  return {
    org,
    spaceId: `space-${org}`,
    role: 'owner',
    devices: 1,
    state: 'active',
    ...(authorityDevice === undefined ? {} : { authorityDevice }),
  };
}

async function renderSettings(authorityDevice?: boolean | null) {
  const port = fakePort(() => ({ orgs: [row('group-x', authorityDevice)], leftOrgs: [] }));
  const source = createOrgLiveSourceForPort(port);
  const user = userEvent.setup();
  render(
    <OrgDomainProvider source={source}>
      <FixtureApp dataMode="fixture" firstRunMode="complete" />
    </OrgDomainProvider>,
  );
  await openRail(user, '组织');
  await openOrgTab(user, '设置');
  await user.click(await screen.findByRole('tab', { name: '危险操作' }));
  return { port, user };
}

describe('org.list authorityDevice 映射', () => {
  it('offers organization deletion when the owner has permission', async () => {
    const { port } = await renderSettings(false);

    expect(screen.getByRole('button', { name: ORG_COPY.deleteOrg })).toBeEnabled();
    expect(port.calls).not.toContain('org.delete');
  });

  it('true/false/null 原样映射，字段缺失保持 null 不推断', async () => {
    const port = fakePort(() => ({
      orgs: [
        row('group-true', true),
        row('group-false', false),
        row('group-null', null),
        row('group-missing'),
      ],
      leftOrgs: [],
    }));
    const source = createOrgLiveSourceForPort(port);
    const store = memoryStore(source.initial);

    await source.createGateway(store).refresh(null);

    expect(store.get().orgs?.map((org) => org.authorityDevice)).toEqual([
      true,
      false,
      null,
      null,
    ]);
    expect(port.calls).not.toContain('org.leave');
  });

  it('authorityDevice=true 时预禁用离开组织，且不发起 org.leave', async () => {
    const { port } = await renderSettings(true);

    const leave = await screen.findByRole('button', { name: ORG_COPY.leaveOrg });
    expect(leave).toBeDisabled();
    expect(screen.getByText(ORG_COPY.authorityCannotLeave)).toBeTruthy();
    expect(port.calls).not.toContain('org.leave');
  });

  it('authorityDevice=false 时不禁用，也不显示 authority 说明', async () => {
    await renderSettings(false);

    const leave = await screen.findByRole('button', { name: ORG_COPY.leaveOrg });
    expect(leave).not.toBeDisabled();
    expect(screen.queryByText(ORG_COPY.authorityCannotLeave)).toBeNull();
  });

  it('authorityDevice=null 时保持状态未确认，不推断为 false 或 true', async () => {
    await renderSettings(null);

    const leave = await screen.findByRole('button', { name: ORG_COPY.leaveOrg });
    expect(leave).not.toBeDisabled();
    expect(screen.queryByText(ORG_COPY.authorityCannotLeave)).toBeNull();
  });
});
