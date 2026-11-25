import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { LeaveOrgDialog } from '../../../src/features/org/impl/dialogs/SettingsDialogs';
import { ORG_COPY } from '../../../src/features/org/impl/domain/copy';
import type {
  OrgResult,
  OrgSummary,
  RemovalPreview,
} from '../../../src/features/org/impl/domain/types';
import type { WorkspaceApi } from '../../../src/features/org/impl/workspace/api';

const ORG: OrgSummary = {
  id: 'group-x',
  name: 'group-x',
  kind: 'work',
  role: 'owner',
  lifecycle: 'active',
  ownerLabel: null,
  authorityDevice: null,
  deviceCount: 1,
};

const PREVIEW: RemovalPreview = {
  revision: 1,
  devices: ['this-device'],
  tailnet: 'tailnet-x',
  authority: 'this-device',
  impact: '本机将退出组织网络。',
};

const RAW_REFUSAL = 'the daemon refused the request';

function makeApi(
  preview: OrgResult<RemovalPreview>,
  leave: OrgResult = { kind: 'ok', value: undefined },
) {
  const leaveOrg = vi.fn(async () => leave);
  const notify = vi.fn();
  const gateway = { previewLeave: async () => preview, leaveOrg };
  const api = { gateway, labelled: () => gateway, notify } as unknown as WorkspaceApi;
  return { api, leaveOrg, notify };
}

describe('离开组织对话框：authority 拒绝', () => {
  it('预览返回 LAST_AUTHORITY_REFUSED 时显示 authority 说明，不显示原始 daemon 错误', async () => {
    const { api } = makeApi({
      kind: 'failed',
      code: 'LAST_AUTHORITY_REFUSED',
      message: RAW_REFUSAL,
    });
    render(<LeaveOrgDialog api={api} org={ORG} onClose={() => undefined} />);

    expect(await screen.findByText(ORG_COPY.authorityCannotLeave)).toBeTruthy();
    expect(screen.queryByText(RAW_REFUSAL)).toBeNull();
  });

  it('确认时后端才返回 LAST_AUTHORITY_REFUSED，同样显示 authority 说明', async () => {
    const user = userEvent.setup();
    const { api } = makeApi(
      { kind: 'ok', value: PREVIEW },
      { kind: 'failed', code: 'LAST_AUTHORITY_REFUSED', message: RAW_REFUSAL },
    );
    render(<LeaveOrgDialog api={api} org={ORG} onClose={() => undefined} />);

    await screen.findByText(PREVIEW.impact);
    await user.type(screen.getByLabelText(/输入组织名/), ORG.name);
    await user.click(screen.getByRole('button', { name: '确认离开' }));

    expect(await screen.findByText(ORG_COPY.authorityCannotLeave)).toBeTruthy();
    expect(screen.queryByText(RAW_REFUSAL)).toBeNull();
  });

  it('其它失败仍显示后端给的消息', async () => {
    const { api } = makeApi({
      kind: 'failed',
      code: 'ORG_FETCH_FAILED',
      message: 'preview is unavailable',
    });
    render(<LeaveOrgDialog api={api} org={ORG} onClose={() => undefined} />);

    expect(await screen.findByText('preview is unavailable')).toBeTruthy();
    expect(screen.queryByText(ORG_COPY.authorityCannotLeave)).toBeNull();
  });
});
