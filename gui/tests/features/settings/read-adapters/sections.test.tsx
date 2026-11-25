import { render, screen } from '@testing-library/react';
import { expect, it, vi } from 'vitest';
import userEvent from '@testing-library/user-event';
import { TransportError } from '../../../../src/client';
import { createGeneratedClient } from '../../../../src/client/generated';
import { LogsSection } from '../../../../src/features/settings/impl/logs/LogsSection';
import { createSettingsReadApis } from '../../../../src/features/settings/impl/read-adapters';
import { UpgradeStepsSection } from '../../../../src/features/settings/impl/UpgradeStepsSection';
import { UpgradeStatusSection } from '../../../../src/features/settings/impl/UpgradeStatusSection';

it('pages log records in both directions and resets to the first page on refresh', async () => {
  const user = userEvent.setup();
  const read = vi.fn(async (cursor?: string) => ({
    entries: [{ timestamp: '2026-11-12T12:00:00Z', level: 'info' as const, component: 'daemon' as const,
      event: cursor ? 'daemon.stopped' as const : 'daemon.started' as const }],
    nextCursor: cursor ? null : 'cursor-page-2', partial: false, partialReasons: [], skippedLines: 0,
    order: 'file-offset' as const, snapshotExpiresInSeconds: 300,
  }));
  render(<LogsSection api={{ read }} />);
  expect(await screen.findByText('服务启动')).toBeInTheDocument();
  expect(screen.queryByText(/此快照/)).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '下一页' }));
  expect(await screen.findByText('服务停止')).toBeInTheDocument();
  expect(screen.queryByText('服务启动')).not.toBeInTheDocument();
  expect(screen.getByText('第 2 页')).toBeInTheDocument();
  expect(read).toHaveBeenLastCalledWith('cursor-page-2', expect.any(AbortSignal));
  expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
  await user.click(screen.getByRole('button', { name: '上一页' }));
  expect(await screen.findByText('服务启动')).toBeInTheDocument();
  expect(screen.getByText('第 1 页')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: '下一页' }));
  await screen.findByText('第 2 页');
  await user.click(screen.getByRole('button', { name: '刷新日志事件摘要' }));
  expect(await screen.findByText('第 1 页')).toBeInTheDocument();
  expect(read).toHaveBeenLastCalledWith(undefined, expect.any(AbortSignal));
});

it('renders unknown log results through the generated adapter without retrying or showing an empty page', async () => {
  const invoke = vi.fn().mockRejectedValue(new TransportError('network', 'NETWORK_ERROR', 'lost', {
    method: 'log.page', requestId: 'log-read', status: null, dispatch: 'unknown',
    catalogDigest: 'test', attempts: 1, startedAt: 1, durationMs: 1,
  }));
  render(<LogsSection api={createSettingsReadApis(createGeneratedClient({ invoke })).logs} />);
  expect(await screen.findByText('日志查询结果未知')).toBeInTheDocument();
  expect(screen.queryByText('本页没有可显示的安全事件摘要。')).not.toBeInTheDocument();
  expect(invoke).toHaveBeenCalledTimes(1);
});

it('shows projected upgrade observations without exposing raw paths or error detail', async () => {
  const invoke = vi.fn().mockResolvedValue({ steps: [{ id: 'future-step', state: 'future-state',
    attempts: 1, updatedAt: null, detail: 'PRIVATE_ERROR_PATH', data: { path: 'PRIVATE_DATA' } }],
    legacyArgvFiles: ['PRIVATE_LEGACY_PATH'], legacyProcesses: [{ argv: 'PRIVATE_ARGV' }], pending: [] });
  render(<UpgradeStepsSection api={createSettingsReadApis(createGeneratedClient({ invoke })).upgradeStatus} />);
  expect((await screen.findByText('其他迁移步骤')).parentElement).toHaveTextContent('状态未知');
  expect(screen.getByText('旧启动配置文件数').parentElement).toHaveTextContent('旧启动配置文件数1');
  expect(document.body.textContent).not.toContain('PRIVATE_');
  expect(invoke).toHaveBeenCalledExactlyOnceWith('upgrade.steps.status', {}, {
    signal: expect.any(AbortSignal), retry: 'safe',
  });
});

it('retains recovery guidance when the upgrade status read is unknown', async () => {
  const status = vi.fn().mockRejectedValue(new TransportError('network', 'NETWORK_ERROR', 'lost', {
    method: 'autoupdate.status', requestId: 'upgrade-read', status: null, dispatch: 'unknown',
    catalogDigest: 'test', attempts: 1, startedAt: 1, durationMs: 1,
  }));
  render(<UpgradeStatusSection api={{ status }} />);
  expect(await screen.findByText('查询结果未知')).toBeInTheDocument();
  expect(screen.getByText('请刷新核对。')).toBeInTheDocument();
  expect(status).toHaveBeenCalledTimes(1);
});
