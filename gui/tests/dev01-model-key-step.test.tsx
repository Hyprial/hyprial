import { useState } from 'react';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ModelStep } from '../src/features/onboarding/impl/ModelStep';
import type { ModelKeySaveResult } from '../src/features/onboarding/impl/types';

interface HarnessProps {
  readonly saved?: boolean;
  readonly restored?: boolean;
  readonly onSave?: (key: string) => Promise<ModelKeySaveResult>;
  readonly onCheckStatus?: () => Promise<
    | { readonly status: 'checked'; readonly keySaved: boolean | null; readonly message: string }
    | { readonly status: 'unavailable'; readonly message: string }
  >;
  readonly onSavedChange?: (saved: boolean, restored?: boolean) => void;
  readonly onContinue?: () => void;
  readonly onBack?: () => void;
  readonly withoutSave?: boolean;
}

function ModelHarness({ saved = false, restored = false, onSave = async () => ({ status: 'saved' as const }), onCheckStatus, onSavedChange, onContinue, onBack, withoutSave = false }: HarnessProps) {
  const [draft, setDraft] = useState('');
  return (
    <ModelStep
      state="active"
      saved={saved}
      restored={restored}
      draft={draft}
      onDraftChange={setDraft}
      onBack={onBack ?? (() => {})}
      onSave={withoutSave ? undefined : onSave}
      onCheckStatus={onCheckStatus}
      onSavedChange={onSavedChange ?? (() => {})}
      onContinue={onContinue ?? (() => {})}
    />
  );
}

describe('first-run DeepSeek key persistence', () => {
  it('does not retain or redisplay a key when the save response is lost', async () => {
    const onSavedChange = vi.fn();
    const onContinue = vi.fn();
    const user = userEvent.setup();
    render(<ModelHarness onSave={async () => { throw new Error('private diagnostic'); }} onSavedChange={onSavedChange} onContinue={onContinue} />);
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'private-key');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText(/保存请求可能已发出/)).toBeInTheDocument();
    expect(screen.queryByText('Key 保存结果未确认，请重新输入并重试。')).not.toBeInTheDocument();
    expect(screen.getByLabelText(/DeepSeek API Key/)).toHaveValue('');
    expect(screen.queryByText(/private diagnostic|private-key/)).not.toBeInTheDocument();
    expect(onSavedChange).not.toHaveBeenCalledWith(true);
    expect(onContinue).not.toHaveBeenCalled();
  });

  it('ignores save completion after leaving the page', async () => {
    let finish!: (result: ModelKeySaveResult) => void;
    const onSavedChange = vi.fn();
    const user = userEvent.setup();
    const view = render(<ModelHarness onSave={() => new Promise(resolve => { finish = resolve; })} onSavedChange={onSavedChange} />);
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'private-key');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(screen.getByLabelText(/DeepSeek API Key/)).toHaveValue('');
    view.unmount();
    await act(async () => finish({ status: 'saved' }));
    expect(onSavedChange).not.toHaveBeenCalledWith(true);
  });
  it('guards duplicate saves and continues only once after saved confirmation', async () => {
    let finish!: (result: ModelKeySaveResult) => void;
    const onContinue = vi.fn();
    const onSave = vi.fn(() => new Promise<ModelKeySaveResult>(resolve => { finish = resolve; }));
    const onSavedChange = vi.fn();
    const user = userEvent.setup();
    render(<ModelHarness onSave={onSave} onSavedChange={onSavedChange} onContinue={onContinue} />);
    const input = screen.getByLabelText(/DeepSeek API Key/);
    expect(input).toBeRequired();
    expect(input).toHaveAttribute('aria-describedby', 'dev01-deepseek-key-hint');
    await user.type(input, 'one-save');
    await user.click(screen.getByRole('button', { name: '显示 Key' }));
    expect(input).toHaveAttribute('type', 'text');
    await user.dblClick(screen.getByRole('button', { name: '保存并继续' }));
    expect(onSave).toHaveBeenCalledExactlyOnceWith('one-save');
    expect(screen.getByRole('button', { name: '正在保存…' })).toBeDisabled();
    expect(onContinue).not.toHaveBeenCalled();
    await act(async () => finish({ status: 'saved' }));
    expect(onSavedChange).toHaveBeenLastCalledWith(true);
    expect(onContinue).toHaveBeenCalledTimes(1);
  });

  it('offers the step back action without clearing the draft', async () => {
    const onBack = vi.fn();
    const user = userEvent.setup();
    render(<ModelHarness onBack={onBack} />);
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'kept-draft');
    expect(screen.getByRole('button', { name: '返回上一步' })).toBeEnabled();
    await user.click(screen.getByRole('button', { name: '返回上一步' }));
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/DeepSeek API Key/)).toHaveValue('kept-draft');
    expect(screen.queryByText(/Key 将安全保存在当前实例中/)).not.toBeInTheDocument();
  });

  it('shows saved metadata without a key field and preserves old provenance when replacement is rejected', async () => {
    const onSavedChange = vi.fn(); const onContinue = vi.fn();
    const onSave = vi.fn(async () => ({ status: 'failed' as const, message: '已有授权不支持直接更换。' }));
    const user = userEvent.setup();
    render(<ModelHarness saved restored onSave={onSave} onSavedChange={onSavedChange} onContinue={onContinue} />);
    expect(screen.getByText('已保存 DeepSeek Key')).toBeInTheDocument();
    expect(screen.queryByLabelText(/DeepSeek API Key/)).not.toBeInTheDocument();
    expect(onSave).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '更换 Key' }));
    expect(onSavedChange).toHaveBeenLastCalledWith(false);
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'replacement');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText('已有授权不支持直接更换。')).toBeInTheDocument();
    expect(screen.getByLabelText(/DeepSeek API Key/)).toHaveValue('');
    expect(onContinue).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(onSavedChange).toHaveBeenLastCalledWith(true, true);
    expect(screen.getByText('已保存 DeepSeek Key')).toBeInTheDocument();
    expect(screen.queryByLabelText(/DeepSeek API Key/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '继续' }));
    expect(onContinue).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledTimes(1);
  });

  it('does not restore the old key after canceling an unconfirmed replacement', async () => {
    const onSavedChange = vi.fn();
    const onContinue = vi.fn();
    const user = userEvent.setup();
    render(<ModelHarness saved restored onSavedChange={onSavedChange} onContinue={onContinue}
      onSave={async () => ({ status: 'unknown', message: '保存请求可能已发出，但未收到可确认的响应。不会自动重试；请先只读检查当前实例状态，确认前不要重复保存。' })} />);
    await user.click(screen.getByRole('button', { name: '更换 Key' }));
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'replacement');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText(/保存请求可能已发出/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    expect(onSavedChange).not.toHaveBeenCalledWith(true, true);
    expect(onSavedChange).not.toHaveBeenCalledWith(true);
    expect(onSavedChange).toHaveBeenLastCalledWith(false);
    expect(onContinue).not.toHaveBeenCalled();
  });

  it('keeps an earlier unknown replacement after a later explicit rejection and cancel', async () => {
    const onSavedChange = vi.fn();
    const onContinue = vi.fn();
    const onCheckStatus = vi.fn(async () => ({
      status: 'checked' as const,
      keySaved: false,
      message: '本机状态仍有待确认的保存；不会据此判定本次输入已保存。',
    }));
    const onSave = vi.fn()
      .mockResolvedValueOnce({
        status: 'unknown' as const,
        message: '保存请求可能已发出，但未收到可确认的响应。不会自动重试；请先只读检查当前实例状态，确认前不要重复保存。',
      })
      .mockResolvedValueOnce({ status: 'failed' as const, message: '第二次保存被明确拒绝。' });
    const user = userEvent.setup();
    render(<ModelHarness saved restored onSave={onSave} onCheckStatus={onCheckStatus}
      onSavedChange={onSavedChange} onContinue={onContinue} />);
    await user.click(screen.getByRole('button', { name: '更换 Key' }));
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'first-replacement');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText(/保存请求可能已发出/)).toBeInTheDocument();
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'second-replacement');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText('第二次保存被明确拒绝。')).toBeInTheDocument();
    expect(screen.getByText('此前保存结果未知')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '检查状态' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '取消' }));
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    expect(onSavedChange).not.toHaveBeenCalledWith(true, true);
    expect(onSavedChange).toHaveBeenLastCalledWith(false);
    expect(onContinue).not.toHaveBeenCalled();
    expect(onSave).toHaveBeenCalledTimes(2);
  });

  it('offers a read-only status check after an unknown save without claiming the new key', async () => {
    const onSavedChange = vi.fn();
    const onContinue = vi.fn();
    const onCheckStatus = vi.fn(async () => ({
      status: 'checked' as const,
      keySaved: true,
      message: '本机状态报告已保存某个 Key；无法确认是否为本次输入，页面不会据此判定本次保存成功，也不会自动重试。',
    }));
    const user = userEvent.setup();
    render(<ModelHarness saved restored onSavedChange={onSavedChange} onContinue={onContinue}
      onCheckStatus={onCheckStatus}
      onSave={async () => ({ status: 'unknown', message: '保存请求可能已发出，但未收到可确认的响应。不会自动重试；请先只读检查当前实例状态，确认前不要重复保存。' })} />);
    await user.click(screen.getByRole('button', { name: '更换 Key' }));
    await user.type(screen.getByLabelText(/DeepSeek API Key/), 'replacement');
    await user.click(screen.getByRole('button', { name: '保存并继续' }));
    await screen.findByText(/保存请求可能已发出/);
    await user.click(screen.getByRole('button', { name: '检查状态' }));
    expect(onCheckStatus).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/无法确认是否为本次输入/)).toBeInTheDocument();
    expect(screen.queryByText('已保存 DeepSeek Key')).not.toBeInTheDocument();
    expect(onSavedChange).not.toHaveBeenCalledWith(true);
    expect(onContinue).not.toHaveBeenCalled();
  });

});

it('renders no save form when the first-run save capability is absent', () => {
  const onContinue = vi.fn();
  const onSavedChange = vi.fn();
  render(<ModelHarness withoutSave onContinue={onContinue} onSavedChange={onSavedChange} />);
  expect(screen.queryByLabelText(/DeepSeek API Key/)).toBeNull();
  expect(screen.queryByRole('button', { name: '保存并继续' })).toBeNull();
  expect(screen.getByRole('button', { name: '返回上一步' })).toBeEnabled();
  expect(document.body.textContent).not.toMatch(/尚未接入|暂不|不可用/);
  expect(onContinue).not.toHaveBeenCalled();
  expect(onSavedChange).not.toHaveBeenCalledWith(true);
});
