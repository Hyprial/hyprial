import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { RepairDialog } from '../../../src/features/org/impl/dialogs/RepairDialog';
import type {
  OrgRepairApplied,
  OrgRepairCandidate,
  OrgResult,
} from '../../../src/features/org/impl/domain/types';
import type { WorkspaceApi } from '../../../src/features/org/impl/workspace/api';

const ORG = 'group-x';

function makeApi(
  preview: OrgResult<readonly OrgRepairCandidate[]>,
  repair: OrgResult<readonly OrgRepairApplied[]>,
) {
  const repairOrg = vi.fn(async () => repair);
  const notify = vi.fn();
  const api = {
    gateway: { previewRepair: async () => preview, repairOrg },
    notify,
  } as unknown as WorkspaceApi;
  return { api, repairOrg, notify };
}

describe('修复对话框', () => {
  it('全部候选都需要人工时禁用确认，不执行 repair', async () => {
    const { api, repairOrg } = makeApi(
      {
        kind: 'ok',
        value: [{ org: ORG, reason: 'creator-intent-missing', repairable: false }],
      },
      { kind: 'ok', value: [] },
    );
    render(<RepairDialog api={api} orgId={ORG} onClose={() => undefined} />);

    expect(await screen.findByText(/没有可自动修复的项/)).toBeTruthy();
    expect(screen.getByText(/creator-intent-missing（需人工）/)).toBeTruthy();
    expect(screen.getByRole('button', { name: '确认修复' })).toBeDisabled();
    expect(repairOrg).not.toHaveBeenCalled();
  });

  it('没有候选时只说明无需修复，不显示“需人工”警告', async () => {
    const { api } = makeApi({ kind: 'ok', value: [] }, { kind: 'ok', value: [] });
    render(<RepairDialog api={api} orgId={ORG} onClose={() => undefined} />);

    expect(await screen.findByText('没有需要修复的项。')).toBeTruthy();
    expect(screen.queryByText(/没有可自动修复的项/)).toBeNull();
    expect(screen.getByRole('button', { name: '确认修复' })).toBeDisabled();
  });

  it('apply 返回 repaired=[] 时不提示“修复已完成”', async () => {
    const user = userEvent.setup();
    const { api, notify } = makeApi(
      {
        kind: 'ok',
        value: [{ org: ORG, reason: 'space-missing', repairable: true }],
      },
      { kind: 'ok', value: [] },
    );
    render(<RepairDialog api={api} orgId={ORG} onClose={() => undefined} />);

    await screen.findByText(/space-missing（可自动修复）/);
    await user.click(screen.getByRole('button', { name: '确认修复' }));

    expect(notify).toHaveBeenCalledWith('没有执行自动修复；1 项仍需人工处理。');
    expect(notify).not.toHaveBeenCalledWith('修复已完成。');
  });

  it('确实修复了候选后才提示完成', async () => {
    const user = userEvent.setup();
    const { api, notify } = makeApi(
      {
        kind: 'ok',
        value: [{ org: ORG, reason: 'space-missing', repairable: true }],
      },
      { kind: 'ok', value: [{ org: ORG, published: true, devicePublished: true }] },
    );
    render(<RepairDialog api={api} orgId={ORG} onClose={() => undefined} />);

    await screen.findByText(/space-missing（可自动修复）/);
    await user.click(screen.getByRole('button', { name: '确认修复' }));

    expect(notify).toHaveBeenCalledWith('修复已完成。');
  });

  it('部分 repaired 条目发布未完成时逐项提示', async () => {
    const user = userEvent.setup();
    const { api, notify } = makeApi(
      {
        kind: 'ok',
        value: [
          { org: ORG, reason: 'space-missing', repairable: true },
          { org: ORG, reason: 'device-missing', repairable: true },
        ],
      },
      {
        kind: 'ok',
        value: [
          { org: ORG, published: true, devicePublished: true },
          { org: ORG, published: true, devicePublished: false },
        ],
      },
    );
    render(<RepairDialog api={api} orgId={ORG} onClose={() => undefined} />);

    await screen.findByText(/space-missing（可自动修复）/);
    await user.click(screen.getByRole('button', { name: '确认修复' }));

    expect(notify).toHaveBeenCalledWith('已修复 2 项，但 1 项发布未完成；仍需处理。');
    expect(notify).not.toHaveBeenCalledWith('修复已完成。');
  });

  it('repaired 但发布未完成时不得声称修复完成', async () => {
    const user = userEvent.setup();
    const { api, notify } = makeApi(
      {
        kind: 'ok',
        value: [{ org: ORG, reason: 'space-missing', repairable: true }],
      },
      { kind: 'ok', value: [{ org: ORG, published: false, devicePublished: false }] },
    );
    render(<RepairDialog api={api} orgId={ORG} onClose={() => undefined} />);

    await screen.findByText(/space-missing（可自动修复）/);
    await user.click(screen.getByRole('button', { name: '确认修复' }));

    expect(notify).toHaveBeenCalledWith('已修复 1 项，但 1 项发布未完成；仍需处理。');
    expect(notify).not.toHaveBeenCalledWith('修复已完成。');
  });
});
