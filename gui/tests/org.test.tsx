import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { FixtureApp } from '../src/dev/FixtureApp';
import { buildOrgViewModel } from '../src/pages/org/org-view-model';

async function openOrg(scene?: Parameters<typeof buildOrgViewModel>[0]) {
  const user = userEvent.setup();
  render(<FixtureApp dataMode="fixture" orgModel={scene ? buildOrgViewModel(scene) : undefined} />);
  await user.click(screen.getByRole('button', { name: '组织' }));
  return { user };
}

describe('shared organization workspace through product routes', () => {
  it('has no organization entry and no workspace when the preview has no org model', () => {
    render(<FixtureApp dataMode="fixture" />);
    expect(screen.queryByRole('button', { name: '组织' })).toBeNull();
    expect(document.querySelector('.org-workspace')).toBeNull();
  });
  it.each(['created-empty', 'invite-pending', 'join-failed', 'member-joined'] as const)('adapts %s without changing the product view', async scene => {
    const { user } = await openOrg(scene);
    expect(document.querySelector('.org-workspace')).not.toBeNull();
    await user.click(screen.getByRole('tab', { name: '成员' }));
    if (scene === 'member-joined') expect(screen.getByText('新成员一')).toBeInTheDocument();
    else expect(screen.queryByText('新成员一')).toBeNull();
  });
  it('retains invalid input and does not claim creation success', async () => {
    const { user } = await openOrg('created-empty');
    await user.click(screen.getByRole('button', { name: /^组织切换/  }));
    await user.click(screen.getByRole('menuitem', { name: '新建组织' }));
    const dialog = screen.getByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));
    expect(within(dialog).getByText('请填写组织名称')).toBeInTheDocument();
    const input = within(dialog).getByLabelText(/^组织名称/);
    await user.type(input, 'design-team');
    await user.click(within(dialog).getByRole('button', { name: '创建' }));
    expect(await within(dialog).findByText(/组织创建未完成：/)).toBeInTheDocument();
    expect(input).toHaveValue('design-team');
  });
});
