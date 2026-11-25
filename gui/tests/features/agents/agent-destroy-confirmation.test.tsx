import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { contractExamples } from '../../../src/dev/contract-mock';
import type { AgentDestroyPreviewResult } from '../../../src/client/generated/types';
import { AgentDeleteDialog } from '../../../src/features/agents/impl/destroy/AgentDeleteDialog';

const preview = contractExamples('agent.destroy.preview').find(row => row.name === 'ready')!.result as unknown as AgentDestroyPreviewResult;

describe('Agent deletion confirmation', () => {
  it('loads the impact, cancellation never executes', async () => {
    const confirm = vi.fn();
    const close = vi.fn();
    render(<AgentDeleteDialog name="worker" load={async () => preview} confirm={confirm} onClose={close} />);
    expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled();
    await screen.findByText(preview.workspace.path);
    expect(screen.getByText(/worker-duty/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(close).toHaveBeenCalledOnce();
    expect(confirm).not.toHaveBeenCalled();
  });

  it('submits the preview operation only once', async () => {
    let finish!: () => void;
    const confirm = vi.fn(() => new Promise<void>(resolve => { finish = resolve; }));
    render(<AgentDeleteDialog name="worker" load={async () => preview} confirm={confirm} onClose={vi.fn()} />);
    await screen.findByText(preview.workspace.path);
    const button = screen.getByRole('button', { name: '确认删除' });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(confirm).toHaveBeenCalledExactlyOnceWith(preview.operationId);
    finish();
    await waitFor(() => expect(screen.getByRole('button', { name: '取消' })).toBeEnabled());
    expect(confirm).toHaveBeenCalledOnce();
  });

  it('expired or failed previews cannot execute', async () => {
    const confirm = vi.fn();
    render(<AgentDeleteDialog name="worker" load={async () => ({ ...preview, expiresAt: 1 })} confirm={confirm} onClose={vi.fn()} />);
    await screen.findByText(/预览已过期/);
    expect(screen.getByRole('button', { name: '确认删除' })).toBeDisabled();
    expect(confirm).not.toHaveBeenCalled();
  });
});
