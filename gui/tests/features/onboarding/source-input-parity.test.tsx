import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { FirstRunOnboarding } from '../../../src/features/onboarding';
import { createDemoActions } from '../../../src/dev/demo-actions';
import { ModelStep } from '../../../src/features/onboarding/impl/ModelStep';

const DEMO_SOURCE = {
  notice: {
    title: '隔离预览场景',
    body: '所有成功状态仅由本地隔离回调返回；不会登录真实账号、访问网络、创建真实 Squire 或发送聊天消息。',
  },
  label: '隔离预览 · 不访问网络、不写存储',
  savedKeyNote: '（隔离预览）',
};

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('first-run source metadata is composition input', () => {
  it('renders the injected isolation notice and demo actions in production mode', async () => {
    vi.stubEnv('DEV', false);
    const user = userEvent.setup();
    render(
      <FirstRunOnboarding
        scenario="isolated"
        actions={createDemoActions()}
        source={DEMO_SOURCE}
      />,
    );
    expect(screen.getByText('隔离预览场景')).toBeInTheDocument();
    expect(screen.getByText(/所有成功状态仅由本地隔离回调返回/)).toBeInTheDocument();
    expect(screen.getByText('隔离预览 · 不访问网络、不写存储')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '登录 Hyprial' }));
    expect(await screen.findByText('账号已登录')).toBeInTheDocument();
    expect(screen.getByText('隔离预览：账号确认仅由本地隔离回调返回。')).toBeInTheDocument();
  });

  it('renders identical content for identical inputs in dev and production mode', () => {
    const renderText = (dev: boolean): string => {
      vi.stubEnv('DEV', dev);
      const { container, unmount } = render(
        <FirstRunOnboarding
          scenario="isolated"
          actions={createDemoActions()}
          source={DEMO_SOURCE}
        />,
      );
      const text = container.textContent ?? '';
      unmount();
      return text;
    };
    expect(renderText(false)).toBe(renderText(true));
  });
});

describe('model step saved-key note is composition input', () => {
  const baseProps = {
    state: 'active' as const,
    saved: true,
    draft: '',
    onDraftChange: () => {},
    onBack: () => {},
    onSavedChange: () => {},
    onContinue: () => {},
  };

  it('renders the injected saved-key note in production mode', () => {
    vi.stubEnv('DEV', false);
    render(<ModelStep {...baseProps} savedKeyNote="（隔离预览）" />);
    expect(screen.getByText('已保存 DeepSeek Key（隔离预览）')).toBeInTheDocument();
  });

  it('keeps the plain saved value when no note is injected', () => {
    vi.stubEnv('DEV', false);
    render(<ModelStep {...baseProps} />);
    expect(screen.getByText('已保存 DeepSeek Key')).toBeInTheDocument();
  });

  it('renders the same saved value for identical inputs in dev and production mode', () => {
    const renderValue = (dev: boolean): string => {
      vi.stubEnv('DEV', dev);
      const { container, unmount } = render(<ModelStep {...baseProps} savedKeyNote="（隔离预览）" />);
      const text = container.textContent ?? '';
      unmount();
      return text;
    };
    expect(renderValue(false)).toBe(renderValue(true));
  });
});
