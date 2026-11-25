// 首设状态卡四态的真实 React 渲染断言（不依赖样式表文本）：
// article data-state、icon data-state、failed details 默认可见与 aria-describedby。
import { render, screen } from '@testing-library/react';
import { StatusCard } from '../src/components/primitives';
import { DeviceStep } from '../src/features/onboarding/impl/DeviceStep';

const CASES = ['pending', 'running', 'done', 'failed'] as const;

describe.each(CASES)('StatusCard state=%s real render', (state) => {
  it('exposes matching data-state on article and icon', () => {
    render(
      <StatusCard
        label="账号"
        value={`fixture-${state}`}
        hint="fixture 说明"
        state={state}
        tone={state === 'failed' ? 'danger' : 'neutral'}
      />,
    );
    const card = screen.getByRole('article', { name: `账号：fixture-${state}` });
    expect(card).toHaveAttribute('data-state', state);
    const icon = card.querySelector('.ui-status-card__icon');
    expect(icon).not.toBeNull();
    expect(icon).toHaveAttribute('data-state', state);
    expect(icon).toHaveAttribute('aria-hidden', 'true');
    // 状态值文本必须原样出现（不用标签替代值）
    expect(card.querySelector('.ui-status-card__value')?.textContent).toContain(`fixture-${state}`);
  });
});

describe('StatusCard failed details visibility', () => {
  it('shows the hint in an open details node and links it with aria-describedby', () => {
    render(
      <StatusCard
        label="当前设备"
        value="设备初始化未完成"
        hint="可以重试设备初始化。"
        tone="danger"
        state="failed"
      />,
    );
    const card = screen.getByRole('article', { name: '当前设备：设备初始化未完成' });
    const details = card.querySelector('details.ui-status-card__details') as HTMLDetailsElement;
    expect(details).not.toBeNull();
    expect(details.open).toBe(true);
    expect(details.querySelector('summary')?.textContent).toBe('查看说明');
    expect(details.textContent).toContain('可以重试设备初始化。');
    const describedBy = card.getAttribute('aria-describedby');
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy as string)).toBe(details);
  });

  it('keeps non-failed hints inline without details', () => {
    render(<StatusCard label="启动 Squire" value="已启动" hint="短说明" state="done" />);
    const card = screen.getByRole('article', { name: '启动 Squire：已启动' });
    expect(card.querySelector('details')).toBeNull();
    expect(card.textContent).toContain('短说明');
  });
});

describe('DeviceStep network card stays pending', () => {
  const baseProps = {
    deviceAttempt: { kind: 'idle' as const },
    onInitialize: () => {},
    onRetry: () => {},
    onBack: () => {},
    onContinue: () => {},
  };

  it('renders the network card as pending with a pending icon in the active branch', () => {
    render(<DeviceStep state="active" {...baseProps} />);
    const network = screen.getByRole('article', { name: /^网络：/ });
    expect(network).toHaveAttribute('data-state', 'pending');
    expect(network.querySelector('.ui-status-card__icon')).toHaveAttribute('data-state', 'pending');
    // 网络卡不得被写成完成或失败
    expect(network.getAttribute('data-state')).not.toBe('done');
    expect(network.getAttribute('data-state')).not.toBe('failed');
  });

  it('renders the network card as pending in the completed branch too', () => {
    render(<DeviceStep state="completed" {...baseProps} />);
    const network = screen.getByRole('article', { name: /^网络：/ });
    expect(network).toHaveAttribute('data-state', 'pending');
  });
});
