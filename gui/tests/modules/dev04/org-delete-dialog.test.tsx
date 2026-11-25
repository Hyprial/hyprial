/**
 * 删除组织对话框（OC7）：打开时读取并展示预览，确认时带预览的 operationId；一个用户动作一个请求。
 *
 * 决策：
 * - 预览到达前与组织名不匹配时不能确认；只有 ok 才通知“已删除组织”并关闭。
 * - 预览已是 complete（别处已删完）直接按已删除关闭，不再确认。
 * - 确认失败显示网关文案、清空输入并重新读取预览，下一次确认绑定新的 operationId。
 * - 请求进行中不能取消；输入框在预览读取期间可用并获得焦点，确认失败后焦点回到输入框。
 * - 确认失败后的预览重读是恢复读取：重读成功提示核对，重读失败提示关闭后重开。
 * - 打开时的预览在 StrictMode 下也只发一次；对话框关闭后到达的应答不再关闭或通知。
 */
import { StrictMode } from 'react';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { DeleteOrgDialog } from '../../../src/features/org/impl/dialogs/SettingsDialogs';
import { ORG_COPY } from '../../../src/features/org/impl/domain/copy';
import type {
  OrgDeletePreview,
  OrgResult,
  OrgSummary,
} from '../../../src/features/org/impl/domain/types';
import type { WorkspaceApi } from '../../../src/features/org/impl/workspace/api';

const ORG: OrgSummary = {
  id: 'group-x',
  name: 'group-x',
  kind: 'work',
  role: 'owner',
  lifecycle: 'active',
  ownerLabel: null,
  authorityDevice: true,
  deviceCount: 2,
};

const PREVIEW: OrgDeletePreview = {
  operationId: 'op-1',
  phase: 'pending-confirmation',
  devices: ['laptop-a', 'desktop-b'],
  authority: 'laptop-a',
  irreversible: true,
};

function makeApi(
  previews: readonly OrgResult<OrgDeletePreview>[],
  confirm: () => Promise<OrgResult> = async () => ({ kind: 'ok', value: undefined }),
) {
  let next = 0;
  const previewDelete = vi.fn(async () => previews[Math.min(next++, previews.length - 1)]);
  const deleteOrg = vi.fn(confirm);
  const notify = vi.fn();
  const gateway = { previewDelete, deleteOrg };
  const api = { gateway, labelled: () => gateway, notify } as unknown as WorkspaceApi;
  return { api, previewDelete, deleteOrg, notify };
}

describe('删除组织对话框', () => {
  it('shows the preview on open and confirms with its operationId only after the name matches', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const { api, previewDelete, deleteOrg, notify } = makeApi([{ kind: 'ok', value: PREVIEW }]);
    render(<DeleteOrgDialog api={api} org={ORG} onClose={onClose} />);

    expect(await screen.findByText('desktop-b')).toBeTruthy();
    expect(screen.getByText('删除后不能恢复')).toBeTruthy();
    expect(previewDelete).toHaveBeenCalledTimes(1);
    const button = screen.getByRole('button', { name: '确认删除' });
    expect(button).toBeDisabled();
    await user.type(screen.getByLabelText(/输入组织名确认/), 'group-');
    expect(button).toBeDisabled();
    await user.type(screen.getByLabelText(/输入组织名确认/), 'x');
    await user.click(button);

    expect(deleteOrg).toHaveBeenCalledTimes(1);
    expect(deleteOrg).toHaveBeenCalledWith('group-x', 'op-1');
    expect(notify).toHaveBeenCalledWith('已删除组织：group-x');
    expect(onClose).toHaveBeenCalled();
  });

  it('closes as deleted when the preview already reports complete, without a confirm', async () => {
    const onClose = vi.fn();
    const { api, deleteOrg, notify } = makeApi([
      { kind: 'ok', value: { ...PREVIEW, phase: 'complete' } },
    ]);
    render(<DeleteOrgDialog api={api} org={ORG} onClose={onClose} />);

    await vi.waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(notify).toHaveBeenCalledWith('已删除组织：group-x');
    expect(deleteOrg).not.toHaveBeenCalled();
  });

  it('shows a refused preview and never enables the confirm', async () => {
    const { api, deleteOrg } = makeApi([
      { kind: 'failed', code: 'AUTHORITY_REQUIRED', message: '只能在该组织的管理设备上删除组织。' },
    ]);
    render(<DeleteOrgDialog api={api} org={ORG} onClose={() => undefined} />);

    expect(await screen.findByRole('alert')).toHaveTextContent('只能在该组织的管理设备上删除组织。');
    expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled();
    await userEvent.setup().type(screen.getByLabelText(/输入组织名确认/), 'group-x');
    expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled();
    expect(deleteOrg).not.toHaveBeenCalled();
  });

  it('flags a resumed deletion with its phase', async () => {
    const { api } = makeApi([{ kind: 'ok', value: { ...PREVIEW, phase: 'tailnet-pending' } }]);
    render(<DeleteOrgDialog api={api} org={ORG} onClose={() => undefined} />);

    expect(await screen.findByText(ORG_COPY.deleteResume('tailnet-pending'))).toBeTruthy();
  });

  it('keeps the failure, clears the name and rebinds to a fresh preview after a failed confirm', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const failure = ORG_COPY.deletePartial('组织网络响应超时。');
    const { api, previewDelete, deleteOrg, notify } = makeApi(
      [
        { kind: 'ok', value: PREVIEW },
        { kind: 'ok', value: { ...PREVIEW, operationId: 'op-2', phase: 'tailnet-pending' } },
      ],
      async () => ({ kind: 'failed', code: 'MEMBERSHIP_SIDECAR_TIMEOUT', message: failure }),
    );
    render(<DeleteOrgDialog api={api} org={ORG} onClose={onClose} />);

    await screen.findByText('desktop-b');
    await user.type(screen.getByLabelText(/输入组织名确认/), 'group-x');
    await user.click(screen.getByRole('button', { name: '确认删除' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(failure);
    expect(await screen.findByText(ORG_COPY.deleteResume('tailnet-pending'))).toBeTruthy();
    expect(previewDelete).toHaveBeenCalledTimes(2);
    expect(screen.getByLabelText(/输入组织名确认/)).toHaveValue('');
    expect(notify).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();

    deleteOrg.mockImplementation(async () => ({ kind: 'ok', value: undefined }));
    await user.type(screen.getByLabelText(/输入组织名确认/), 'group-x');
    await user.click(screen.getByRole('button', { name: '确认删除' }));
    expect(deleteOrg).toHaveBeenLastCalledWith('group-x', 'op-2');
    expect(onClose).toHaveBeenCalled();
  });

  it('cannot be cancelled while the confirm is in flight', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    let release: (value: OrgResult) => void = () => undefined;
    const { api } = makeApi(
      [{ kind: 'ok', value: PREVIEW }],
      () => new Promise<OrgResult>((resolve) => {
        release = resolve;
      }),
    );
    render(<DeleteOrgDialog api={api} org={ORG} onClose={onClose} />);

    await screen.findByText('desktop-b');
    await user.type(screen.getByLabelText(/输入组织名确认/), 'group-x');
    await user.click(screen.getByRole('button', { name: '确认删除' }));
    expect(screen.getByRole('button', { name: '取消' })).toBeDisabled();
    release({ kind: 'ok', value: undefined });
    await vi.waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('puts focus on the name input on open while the preview is still loading', async () => {
    let release: (value: OrgResult<OrgDeletePreview>) => void = () => undefined;
    const previewDelete = vi.fn(
      () => new Promise<OrgResult<OrgDeletePreview>>((resolve) => {
        release = resolve;
      }),
    );
    const gateway = { previewDelete, deleteOrg: vi.fn() };
    const api = { gateway, labelled: () => gateway, notify: vi.fn() } as unknown as WorkspaceApi;
    render(<DeleteOrgDialog api={api} org={ORG} onClose={() => undefined} />);

    const input = screen.getByLabelText(/输入组织名确认/);
    await vi.waitFor(() => expect(document.activeElement).toBe(input));
    expect(input).toBeEnabled();
    expect(screen.getByRole('status')).toHaveTextContent('正在读取预览');
    expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled();
    release({ kind: 'ok', value: PREVIEW });
    await screen.findByText('desktop-b');
    expect(document.activeElement).toBe(input);
  });

  it('sends the open-time preview once under StrictMode', async () => {
    const { api, previewDelete } = makeApi([{ kind: 'ok', value: PREVIEW }]);
    render(
      <StrictMode>
        <DeleteOrgDialog api={api} org={ORG} onClose={() => undefined} />
      </StrictMode>,
    );
    await screen.findByText('desktop-b');
    expect(previewDelete).toHaveBeenCalledTimes(1);
  });

  it('says the preview was re-read and returns focus to the input after a stale confirm', async () => {
    const user = userEvent.setup();
    const { api, previewDelete } = makeApi(
      [{ kind: 'ok', value: PREVIEW }, { kind: 'ok', value: { ...PREVIEW, operationId: 'op-2' } }],
      async () => ({ kind: 'failed', code: 'stale', message: ORG_COPY.deleteStale }),
    );
    render(<DeleteOrgDialog api={api} org={ORG} onClose={() => undefined} />);

    await screen.findByText('desktop-b');
    await user.type(screen.getByLabelText(/输入组织名确认/), 'group-x');
    await user.click(screen.getByRole('button', { name: '确认删除' }));

    expect(await screen.findByText(ORG_COPY.deletePreviewReread)).toBeTruthy();
    expect(screen.getByRole('alert')).toHaveTextContent(ORG_COPY.deleteStale);
    expect(previewDelete).toHaveBeenCalledTimes(2);
    await vi.waitFor(() => expect(document.activeElement).toBe(screen.getByLabelText(/输入组织名确认/)));
  });

  it('explains a failed re-read instead of leaving dead controls without a reason', async () => {
    const user = userEvent.setup();
    const failure = ORG_COPY.deletePartial('组织网络响应超时。');
    const { api } = makeApi(
      [
        { kind: 'ok', value: PREVIEW },
        { kind: 'failed', code: 'MEMBERSHIP_SIDECAR_TIMEOUT', message: '组织网络响应超时。' },
      ],
      async () => ({ kind: 'failed', code: 'MEMBERSHIP_SIDECAR_TIMEOUT', message: failure }),
    );
    render(<DeleteOrgDialog api={api} org={ORG} onClose={() => undefined} />);

    await screen.findByText('desktop-b');
    await user.type(screen.getByLabelText(/输入组织名确认/), 'group-x');
    await user.click(screen.getByRole('button', { name: '确认删除' }));

    await vi.waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent(ORG_COPY.deletePreviewRereadFailed),
    );
    expect(screen.getByRole('alert')).toHaveTextContent(failure);
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled();
  });

  it('closes when the preview is unavailable', async () => {
    const onClose = vi.fn();
    const { api } = makeApi([{ kind: 'unavailable', refusal: {} } as unknown as OrgResult<OrgDeletePreview>]);
    render(<DeleteOrgDialog api={api} org={ORG} onClose={onClose} />);
    await vi.waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it('does not close or notify again when the preview answers after the dialog was cancelled', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const notify = vi.fn();
    let release: (value: OrgResult<OrgDeletePreview>) => void = () => undefined;
    const previewDelete = vi.fn(
      () => new Promise<OrgResult<OrgDeletePreview>>((resolve) => {
        release = resolve;
      }),
    );
    const gateway = { previewDelete, deleteOrg: vi.fn() };
    const api = { gateway, labelled: () => gateway, notify } as unknown as WorkspaceApi;
    const view = render(<DeleteOrgDialog api={api} org={ORG} onClose={onClose} />);

    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(onClose).toHaveBeenCalledTimes(1);
    view.unmount();
    release({ kind: 'ok', value: { ...PREVIEW, phase: 'complete' } });
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(notify).not.toHaveBeenCalled();
  });
});
