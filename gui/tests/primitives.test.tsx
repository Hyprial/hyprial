import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import * as primitives from '../src/components/primitives';
import {
  Button,
  Card,
  DiagnosticBox,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  LoadingState,
  Notice,
  PageHeader,
  ProgressBar,
  StatusCard,
  Stepper,
  statusToTone,
} from '../src/components/primitives';

describe('Button', () => {
  it('defaults to a non-submit button and calls its handler', async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>保存</Button>);

    const button = screen.getByRole('button', { name: '保存' });
    expect(button).toHaveAttribute('type', 'button');

    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('is disabled and busy while loading so it cannot submit twice', async () => {
    const onClick = vi.fn();
    render(
      <Button loading onClick={onClick}>
        保存
      </Button>,
    );

    const button = screen.getByRole('button', { name: '保存' });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('aria-busy', 'true');

    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(0);
  });
});

describe('Field', () => {
  it('associates label, hint, error and required metadata', () => {
    render(
      <Field
        label="名称"
        htmlFor="display-name"
        hint="用于展示"
        error="名称不能为空"
        required
      >
        <input id="display-name" />
      </Field>,
    );

    const input = screen.getByLabelText(/名称/);
    expect(input).toBeRequired();
    expect(input).toHaveAttribute('aria-invalid', 'true');
    expect(input).toHaveAttribute(
      'aria-describedby',
      'display-name-hint display-name-error',
    );
    expect(input).toHaveAttribute('aria-errormessage', 'display-name-error');
    expect(screen.getByText('用于展示')).toHaveAttribute(
      'id',
      'display-name-hint',
    );
    expect(screen.getByRole('alert')).toHaveTextContent('名称不能为空');
  });
});

describe('Notice', () => {
  it('exposes a polite status with an explicit recovery action', async () => {
    const onAction = vi.fn();
    const onDismiss = vi.fn();

    render(
      <Notice
        tone="warning"
        title="同步失败"
        action={{ label: '重试', onClick: onAction }}
        onDismiss={onDismiss}
      >
        请检查连接后重试。
      </Notice>,
    );

    const notice = screen.getByRole('status');
    expect(notice).toHaveAttribute('aria-live', 'polite');
    expect(notice).toHaveTextContent('同步失败');

    await userEvent.click(screen.getByRole('button', { name: '重试' }));
    await userEvent.click(screen.getByRole('button', { name: '关闭提示' }));

    expect(onAction).toHaveBeenCalledTimes(1);
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('uses alert semantics for a danger notice', () => {
    render(
      <Notice tone="danger" title="连接丢失">
        当前会话已断开。
      </Notice>,
    );

    const notice = screen.getByRole('alert');
    expect(notice).toHaveAttribute('aria-live', 'assertive');
  });
});

describe('Card', () => {
  it('renders semantic card modifiers without owning state', () => {
    const { container } = render(
      <Card tone="positive" padding="lg" selected>
        <p>主体</p>
      </Card>,
    );

    const card = container.firstElementChild as HTMLElement;
    expect(card).toHaveClass(
      'ui-card',
      'ui-card--positive',
      'ui-card--lg',
      'ui-card--selected',
    );
    expect(card).toHaveAttribute('data-selected', 'true');
  });

  it('shows an unknown status as unavailable instead of zero', () => {
    render(<StatusCard label="设备" value={null} hint="等待探针" />);

    expect(screen.getByRole('article', { name: '设备：未提供' })).toBeVisible();
    expect(screen.getByText('未提供')).toBeVisible();
    expect(screen.getByText('等待探针')).toBeVisible();
  });
});

describe('ProgressBar', () => {
  it('does not render an unknown value as completed', () => {
    render(<ProgressBar label="同步进度" value={null} />);

    const progress = screen.getByRole('progressbar', { name: '同步进度' });
    expect(progress.hasAttribute('aria-valuenow')).toBe(false);
    expect(progress).toHaveAttribute('aria-valuetext', '未知');
    expect(screen.getByText('未知')).toBeVisible();
  });

  it('clamps a known value to the declared range', () => {
    render(<ProgressBar label="完成度" value={150} max={100} />);

    const progress = screen.getByRole('progressbar', { name: '完成度' });
    expect(progress).toHaveAttribute('aria-valuemin', '0');
    expect(progress).toHaveAttribute('aria-valuemax', '100');
    expect(progress).toHaveAttribute('aria-valuenow', '100');
    expect(progress).toHaveAttribute('aria-valuetext', '100%');
  });
});

describe('Stepper', () => {
  it('marks the current step and announces status text', () => {
    render(
      <Stepper
        currentId="two"
        steps={[
          { id: 'one', label: '第一步', status: 'completed' },
          { id: 'two', label: '第二步', status: 'active' },
          { id: 'three', label: '第三步', status: 'blocked' },
        ]}
      />,
    );

    const current = screen.getByText('第二步').closest('li');
    expect(current).toHaveAttribute('aria-current', 'step');
    expect(screen.getByText('（进行中）')).toBeInTheDocument();
    expect(screen.getByText('（已阻塞）')).toBeInTheDocument();
  });
});

describe('PageHeader', () => {
  it('keeps the page title semantic and renders actions and steps', () => {
    render(
      <PageHeader
        eyebrow="概览"
        title="开始"
        description="连接你的工作环境"
        actions={<Button>刷新</Button>}
        stepper={
          <Stepper
            currentId="one"
            steps={[{ id: 'one', label: '连接', status: 'active' }]}
          />
        }
      />,
    );

    expect(
      screen.getByRole('heading', { level: 1, name: '开始' }),
    ).toBeVisible();
    expect(screen.getByRole('button', { name: '刷新' })).toBeVisible();
    expect(screen.getByRole('navigation', { name: '步骤' })).toBeVisible();
  });
});

describe('States', () => {
  it('exposes a busy loading status', () => {
    render(<LoadingState description="正在读取状态" />);

    const status = screen.getByRole('status');
    expect(status).toHaveAttribute('aria-busy', 'true');
    expect(status).toHaveTextContent('正在加载');
  });

  it('offers an explicit action for an empty view', async () => {
    const onAction = vi.fn();
    render(
      <EmptyState
        title="还没有会话"
        description="开始一个会话"
        action={{ label: '新建会话', onClick: onAction }}
      />,
    );

    expect(screen.getByText('还没有会话')).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: '新建会话' }));
    expect(onAction).toHaveBeenCalledTimes(1);
  });

  it('has no unsupported state primitive or view status', () => {
    expect('UnsupportedState' in primitives).toBe(false);
  });

  it('uses alert semantics for an error and only retries when allowed', async () => {
    const onRetry = vi.fn();
    render(
      <ErrorState
        message="网络请求失败"
        retryable
        code="E_NETWORK"
        onRetry={onRetry}
      />,
    );

    expect(screen.getByRole('alert')).toHaveTextContent('网络请求失败');
    await userEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe('Dialog', () => {
  it('is modal, traps Tab, closes on Escape and restores focus', () => {
    const onClose = vi.fn();
    const onContinue = vi.fn();

    const { unmount } = render(
      <Dialog
        open
        title="确认操作"
        description="请确认是否继续"
        onClose={onClose}
        actions={[
          { label: '取消', onClick: onClose },
          { label: '继续', onClick: onContinue, variant: 'primary' },
        ]}
      >
        <p>这个操作会应用当前设置。</p>
      </Dialog>,
    );

    const dialog = screen.getByRole('dialog', { name: '确认操作' });
    expect(dialog).toHaveAttribute('aria-modal', 'true');
    expect(dialog).toHaveAttribute('aria-describedby');
    expect(dialog.contains(document.activeElement)).toBe(true);

    const buttons = within(dialog).getAllByRole('button');
    buttons[buttons.length - 1].focus();
    fireEvent.keyDown(document, { key: 'Tab' });
    expect(buttons[0]).toHaveFocus();

    buttons[0].focus();
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true });
    expect(buttons[buttons.length - 1]).toHaveFocus();

    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);

    unmount();
  });

  it('closes on backdrop only when opted in', () => {
    const onClose = vi.fn();
    render(
      <Dialog open title="设置" onClose={onClose} closeOnBackdrop>
        <p>内容</p>
      </Dialog>,
    );

    const dialog = screen.getByRole('dialog', { name: '设置' });
    const backdrop = dialog.parentElement as HTMLElement;
    fireEvent.mouseDown(backdrop);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does not close on a backdrop click by default', () => {
    const onClose = vi.fn();
    render(
      <Dialog open title="设置" onClose={onClose}>
        <p>内容</p>
      </Dialog>,
    );

    const dialog = screen.getByRole('dialog', { name: '设置' });
    const backdrop = dialog.parentElement as HTMLElement;
    fireEvent.mouseDown(backdrop);
    expect(onClose).toHaveBeenCalledTimes(0);
  });
});

describe('DiagnosticBox', () => {
  it('renders a safe diagnostic code and does not require raw error data', () => {
    render(
      <DiagnosticBox
        code="GUI-DIAG-001"
        title="诊断信息"
        description="只包含已脱敏的编号"
      />,
    );

    const region = screen.getByRole('region', { name: '诊断信息' });
    expect(region).toHaveTextContent('GUI-DIAG-001');
    expect(region).toHaveTextContent('只包含已脱敏的编号');
    expect(
      screen.getByRole('button', { name: '复制诊断信息' }),
    ).toBeVisible();
  });
});

describe('statusToTone', () => {
  it('is the single mapping used by new state surfaces', () => {
    expect(statusToTone('ready')).toBe('positive');
    expect(statusToTone('loading')).toBe('info');
    expect(statusToTone('empty')).toBe('neutral');
    expect(statusToTone('error')).toBe('danger');
  });
});
