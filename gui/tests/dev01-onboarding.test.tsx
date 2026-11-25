import { StrictMode } from 'react';
import {
  act,
  render,
  screen,
  waitFor,
  within,
} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest';
import { createDemoActions } from '../src/dev/demo-actions';
import {
  FirstRunOnboarding,
  type AccountActionResult,
  type AccountProgress,
  type AccountCredentialValues,
  type AccountEntryMode,
  type FirstRunActions,
  type FirstRunStepId,
} from '../src/features/onboarding';

const MODEL_TOKEN = 'dev01-model-token-never-stored';

const DEMO_SOURCE = {
  notice: {
    title: '隔离预览场景',
    body: '所有成功状态仅由本地隔离回调返回；不会登录真实账号、访问网络、配置真实 Agent 或发送聊天消息。',
  },
  label: '隔离预览 · 不访问网络、不写存储',
  savedKeyNote: '（隔离预览）',
};

type User = ReturnType<typeof userEvent.setup>;

function stepRegion(id: FirstRunStepId): HTMLElement {
  const element = document.querySelector<HTMLElement>(
    `[data-step="${id}"]`,
  );
  if (!element) {
    throw new Error(`missing step region: ${id}`);
  }
  return element;
}

function stepNavView(id: string): string | null {
  const element = document.querySelector<HTMLElement>(
    `[data-step-nav="${id}"]`,
  );
  if (!element) {
    throw new Error(`missing step navigation: ${id}`);
  }
  return element.getAttribute('data-view');
}

function stepNavStatus(id: string): string | null {
  const element = document.querySelector<HTMLElement>(
    `[data-step-nav="${id}"]`,
  );
  if (!element) {
    throw new Error(`missing step navigation: ${id}`);
  }
  return element.getAttribute('data-status');
}

function makeActions(
  overrides: Partial<FirstRunActions> = {},
): FirstRunActions {
  return {
    saveModelKey: async () => ({ status: 'saved' }),
    authenticateAccount: async () => ({ status: 'authenticated' }),
    initializeDevice: async () => ({ status: 'ready' }),
    ...overrides,
  };
}

async function completeAccount(user: User): Promise<void> {
  const account = stepRegion('account');
  await user.click(within(account).getByRole('button', { name: '登录 Hyprial' }));

  await waitFor(() =>
    expect(
      within(account).getByText('账号已登录'),
    ).toBeInTheDocument(),
  );

  await user.click(
    within(account).getByRole('button', { name: '继续到设备' }),
  );
  const device = stepRegion('device');
  await user.click(
    within(device).getByRole('button', { name: '初始化当前设备' }),
  );
  await waitFor(() =>
    expect(within(device).getByText(/已就绪/)).toBeInTheDocument(),
  );

  await user.click(
    within(device).getByRole('button', { name: '继续到运行配置' }),
  );
  await waitFor(() =>
    expect(
      screen.getByRole('heading', { name: '模型服务' }),
    ).toHaveFocus(),
  );
}

async function completeModel(user: User): Promise<void> {
  const model = stepRegion('model');
  await user.type(within(model).getByLabelText(/DeepSeek API Key/), MODEL_TOKEN);
  await user.click(within(model).getByRole('button', { name: '保存并继续' }));
  await waitFor(() => expect(screen.getByRole('heading', { name: '首次对话' })).toHaveFocus());
}

describe('dev01 first-run onboarding', () => {
  beforeEach(() => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    localStorage.clear();
    sessionStorage.clear();
  });

  it('renders the formal live path without ports as inert steps and no wording', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);

    try {
      const { container } = render(<FirstRunOnboarding />);

      expect(container.querySelector('[data-scenario="live"]')).not.toBeNull();
      expect(screen.queryByRole('button', { name: '登录 Hyprial' })).toBeNull();
      expect(document.body.textContent).not.toMatch(/当前服务不可用|账号服务不可用/);
      expect(screen.getByRole('img', { name: 'Hyprial' })).toHaveClass('dev01-v17-brand-mark');
      expect(container.querySelector('.dev01-v17-brand-wordmark')).toBeNull();
      expect(document.querySelectorAll('[data-step]')).toHaveLength(1);
      expect(document.querySelector('.first-run-grid')).toBeNull();
      expect(
        screen.getByRole('heading', { name: '登录' }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole('heading', { name: '模型服务' }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('heading', { name: '创建与启动 Squire' }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('heading', { name: '首次对话' }),
      ).not.toBeInTheDocument();
      expect(stepNavView('account')).toBe('active');
      expect(stepNavView('runtime')).toBe('locked');
      expect(stepNavView('conversation')).toBe('locked');

      expect(stepNavView('account')).toBe('active');
      expect(screen.queryByText(/已确认/)).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: /跳过/ }),
      ).not.toBeInTheDocument();

      expect(fetchSpy).not.toHaveBeenCalled();
      expect(localStorage.length).toBe(0);
      expect(sessionStorage.length).toBe(0);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('keeps a failed account on step one and retries without collecting credentials', async () => {
    const responses: AccountActionResult[] = [
      { status: 'failed', message: '授权未完成。', retryable: true },
      { status: 'authenticated', message: '账号已验证。' },
    ];
    const authenticateAccount = vi.fn(
      async (
        _values: AccountCredentialValues,
        _mode: AccountEntryMode,
      ): Promise<AccountActionResult> => {
        const next = responses.shift();
        if (!next) {
          throw new Error('unexpected account call');
        }
        return next;
      },
    );
    const actions = makeActions({ authenticateAccount });
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const user = userEvent.setup();

    try {
      render(<FirstRunOnboarding actions={actions} />);
      const account = stepRegion('account');
          await user.click(
        within(account).getByRole('button', { name: '登录 Hyprial' }),
      );

      expect(
        (await within(account).findAllByText('登录失败')).length,
      ).toBeGreaterThan(0);
      expect(
        within(account).getByText('授权未完成。'),
      ).toBeInTheDocument();
      expect(within(account).queryByRole('textbox')).not.toBeInTheDocument();
      expect(within(account).queryByLabelText(/访问令牌/)).not.toBeInTheDocument();
      expect(stepNavView('account')).toBe('active');
      expect(stepNavView('runtime')).toBe('locked');
      expect(authenticateAccount).toHaveBeenCalledTimes(1);
      expect(authenticateAccount.mock.calls[0]).toEqual([
        { account: '', token: '' },
        'login',
      ]);

      await user.click(
        within(account).getByRole('button', { name: '重试登录' }),
      );

      expect(
        await within(account).findByText('账号已登录'),
      ).toBeInTheDocument();
      expect(authenticateAccount).toHaveBeenCalledTimes(2);
      expect(authenticateAccount.mock.calls[1]).toEqual(
        authenticateAccount.mock.calls[0],
      );
      await user.click(
        within(account).getByRole('button', { name: '继续到设备' }),
      );
      expect(
        within(stepRegion('device')).getByRole('button', {
          name: '初始化当前设备',
        }),
      ).toBeInTheDocument();
      expect(stepNavView('runtime')).toBe('locked');
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(localStorage.length).toBe(0);
      expect(sessionStorage.length).toBe(0);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('restores account and device from the host without starting login', async () => {
    const authenticateAccount = vi.fn();
    const actions = makeActions({ authenticateAccount, getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'person@example.invalid' },
      device: { status: 'ready' },
    }) });
    render(<FirstRunOnboarding actions={actions} />);
    expect(await screen.findByText('当前账号：person@example.invalid')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '继续到设备' })).toBeInTheDocument();
    expect(authenticateAccount).not.toHaveBeenCalled();
    expect(screen.queryByLabelText(/访问令牌/)).not.toBeInTheDocument();
  });

  it('restores under StrictMode without starting login or cancelling unrelated status reads', async () => {
    const authenticateAccount = vi.fn();
    const cancelAccount = vi.fn(async () => ({ ok: true, cancelled: true }));
    const actions = makeActions({ authenticateAccount, cancelAccount, getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'strict@example.invalid' }, device: { status: 'ready' },
    }) });
    render(<StrictMode><FirstRunOnboarding actions={actions} /></StrictMode>);
    expect(await screen.findByText('当前账号：strict@example.invalid')).toBeInTheDocument();
    expect(authenticateAccount).not.toHaveBeenCalled();
    expect(cancelAccount).not.toHaveBeenCalled();
  });

  it('cancels an owned device operation on unmount and ignores its late result', async () => {
    let resolveDevice!: (value: {status: 'ready'}) => void;
    const cancelAccount = vi.fn(async () => ({ ok: true, cancelled: true }));
    const actions = makeActions({ cancelAccount,
      initializeDevice: () => new Promise(resolve => { resolveDevice = resolve; }),
    });
    const user = userEvent.setup();
    const view = render(<FirstRunOnboarding actions={actions} />);
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    await user.click(await screen.findByRole('button', { name: '继续到设备' }));
    await user.click(await screen.findByRole('button', { name: '初始化当前设备' }));
    view.unmount();
    expect(cancelAccount).toHaveBeenCalledTimes(1);
    await act(async () => resolveDevice({ status: 'ready' }));
    render(<FirstRunOnboarding />);
    expect(screen.queryByText(/已就绪/)).not.toBeInTheDocument();
  });

  it('cancels host authorization and ignores its late result and progress', async () => {
    let resolveLogin!: (result: AccountActionResult) => void;
    let progress!: (event: AccountProgress) => void;
    const cancelAccount = vi.fn(async () => ({ ok: true, cancelled: true }));
    const actions = makeActions({ cancelAccount,
      getAccountState: async () => ({
        account: { status: 'unauthenticated' },
        device: { status: 'not-ready' },
      }),
      authenticateAccount: () => new Promise((resolve) => { resolveLogin = resolve; }),
      subscribeAccountProgress: (listener) => { progress = listener; return () => {}; },
    });
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions} />);
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    act(() => progress({ event: 'device', verificationUri: 'https://auth.example.invalid/device', userCode: 'ABCD' }));
    expect(screen.getByText('授权码：ABCD')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '打开授权页' })).toHaveAttribute('rel', 'noopener noreferrer');
    await user.click(screen.getByRole('button', { name: '取消登录' }));
    await act(async () => { resolveLogin({ status: 'authenticated' }); });
    act(() => progress({ event: 'device', userCode: 'STALE' }));
    expect(cancelAccount).toHaveBeenCalledTimes(1);
    expect(screen.queryByText(/已确认/)).not.toBeInTheDocument();
    expect(screen.queryByText(/授权码/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toBeEnabled();
  });

  it('opens an owned authorization tab from the login click and navigates once', async () => {
    let progress!: (event: AccountProgress) => void;
    const replace = vi.fn();
    const popup = { opener: window, closed: false, location: { href: 'about:blank', replace }, close: vi.fn() };
    vi.mocked(window.open).mockReturnValue(popup as unknown as Window);
    const actions = makeActions({
      authenticateAccount: () => new Promise(() => {}),
      subscribeAccountProgress: listener => { progress = listener; return () => {}; },
    });
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions} />);
    expect(window.open).not.toHaveBeenCalled();
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    expect(window.open).toHaveBeenCalledWith('about:blank', '_blank');
    expect(popup.opener).toBeNull();
    act(() => progress({ event: 'device', verificationUri: 'javascript:alert(1)' }));
    expect(replace).not.toHaveBeenCalled();
    act(() => progress({ event: 'device', verificationUri: 'https://auth.example.invalid/device' }));
    expect(replace).toHaveBeenCalledWith('https://auth.example.invalid/device');
    act(() => progress({ event: 'device', verificationUri: 'https://auth.example.invalid/other' }));
    expect(replace).toHaveBeenCalledTimes(1);
  });

  it('does not render unsafe authorization links', async () => {
    let progress!: (event: AccountProgress) => void;
    const actions = makeActions({
      authenticateAccount: () => new Promise(() => {}),
      subscribeAccountProgress: (listener) => { progress = listener; return () => {}; },
    });
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={actions} />);
    await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
    for (const uri of ['javascript:alert(1)', 'http://auth.example.invalid', 'https://user:secret@example.invalid']) {
      act(() => progress({ event: 'device', verificationUri: uri }));
      expect(screen.queryByRole('link')).not.toBeInTheDocument();
    }
  });

  it('ignores an old status response after unmount', async () => {
    let resolveState!: (value: {account: AccountActionResult; device: {status: 'ready'}}) => void;
    const getAccountState = vi.fn(() => new Promise<{account: AccountActionResult; device: {status: 'ready'}}>((resolve) => { resolveState = resolve; }));
    const user = userEvent.setup();
    const view = render(<FirstRunOnboarding actions={makeActions({ getAccountState })} />);
    view.unmount();
    await act(async () => resolveState({ account: { status: 'authenticated', owner: 'stale' }, device: { status: 'ready' } }));
    render(<FirstRunOnboarding actions={makeActions()} />);
    expect(screen.queryByText(/stale/)).not.toBeInTheDocument();
    await user.tab();
    expect(screen.getByRole('button', { name: '登录 Hyprial' })).toHaveFocus();
  });

  it('runs the explicit isolated demo path without requests, storage or chat input', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const onOpenChat = vi.fn();
    const onComplete = vi.fn();
    const user = userEvent.setup();

    try {
      render(
        <FirstRunOnboarding
          scenario="isolated"
          actions={createDemoActions()}
          source={DEMO_SOURCE}
          chatStatus="ready"
          onOpenChat={onOpenChat}
          onComplete={onComplete}
        />,
      );

      expect(
        document.querySelector('[data-scenario="isolated"]'),
      ).not.toBeNull();
      expect(screen.getByText('隔离预览场景')).toBeInTheDocument();

      await completeAccount(user);
      expect(stepNavView('account')).toBe('completed');
      expect(stepNavView('device')).toBe('completed');
      expect(stepNavView('runtime')).toBe('active');

      await completeModel(user);
      expect(stepNavView('runtime')).toBe('completed');
      expect(stepNavView('conversation')).toBe('active');
      expect(screen.queryByRole('heading', { name: '创建与启动 Squire' })).not.toBeInTheDocument();

      const conversation = stepRegion('conversation');
      expect(
        within(conversation).getByText('可进入 · 未开始'),
      ).toBeInTheDocument();
      expect(
        within(conversation).queryByRole('textbox'),
      ).not.toBeInTheDocument();
      expect(
        within(conversation).queryByRole('button', { name: /发送/ }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: /跳过/ }),
      ).not.toBeInTheDocument();

      await user.click(
        within(conversation).getByRole('button', { name: '打开聊天' }),
      );
      expect(onOpenChat).toHaveBeenCalledTimes(1);
      expect(onComplete).not.toHaveBeenCalled();
      expect(fetchSpy).not.toHaveBeenCalled();
      expect(localStorage.length).toBe(0);
      expect(sessionStorage.length).toBe(0);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it('hands a verified first conversation to the start-page callback', async () => {
    const onComplete = vi.fn();
    const user = userEvent.setup();

    render(
      <FirstRunOnboarding
        scenario="isolated"
        actions={createDemoActions()}
        source={DEMO_SOURCE}
        chatStatus="completed"
        onComplete={onComplete}
      />,
    );

    await completeAccount(user);
    await completeModel(user);

    const conversation = stepRegion('conversation');
    expect(
      within(conversation).getByText('已完成（回调确认）'),
    ).toBeInTheDocument();
    expect(onComplete).not.toHaveBeenCalled();
    expect(within(conversation).queryByRole('textbox')).not.toBeInTheDocument();

    await user.click(
      within(conversation).getByRole('button', { name: '进入开始页' }),
    );
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('keeps failed key saves on model step and clears the secret', async () => {
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={makeActions({
      saveModelKey: async () => ({ status: 'failed', message: '保存失败。' }),
    })} />);
    await completeAccount(user);
    const model = stepRegion('model');
    expect(within(model).queryByRole('combobox')).not.toBeInTheDocument();
    expect(within(model).queryByRole('button', { name: '使用账号授权并校验' })).not.toBeInTheDocument();
    await user.type(within(model).getByLabelText(/DeepSeek API Key/), MODEL_TOKEN);
    await user.click(within(model).getByRole('button', { name: '保存并继续' }));
    expect(await screen.findByText('Key 保存未确认')).toBeInTheDocument();
    expect(within(model).getByLabelText(/DeepSeek API Key/)).toHaveValue('');
    expect(within(model).getByRole('button', { name: '保存并继续' })).toBeDisabled();
    expect(screen.queryByRole('heading', { name: '首次对话' })).not.toBeInTheDocument();
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });

  it('shows the supported runtime configuration and default model', async () => {
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={makeActions()} />);
    await completeAccount(user);
    const model = stepRegion('model');
    expect(within(model).getByText('运行配置')).toBeInTheDocument();
    expect(within(model).getByText('Pi · DeepSeek')).toBeInTheDocument();
    expect(within(model).getByText(/模型 deepseek-flash（默认）/)).toBeInTheDocument();
    expect(within(model).queryByLabelText('模型')).not.toBeInTheDocument();
  });

  it('saves and advances directly from model to conversation without Squire step', async () => {
    const saveModelKey = vi.fn(async () => ({ status: 'saved' as const }));
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={makeActions({ saveModelKey })} />);
    await completeAccount(user);
    await completeModel(user);
    expect(saveModelKey).toHaveBeenCalledExactlyOnceWith(MODEL_TOKEN);
    expect(screen.getByRole('heading', { name: '首次对话' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '创建与启动 Squire' })).not.toBeInTheDocument();
    expect(stepNavView('runtime')).toBe('completed');
    expect(stepNavView('conversation')).toBe('active');
    expect(document.body.textContent).not.toMatch(/聊天能力尚未接入|聊天服务未连接/);
  });

  it('item 3 keeps only task copy on the account step (no authorization background paragraph)', async () => {
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={makeActions()} />);
    const account = stepRegion('account');
    expect(
      within(account).queryByText(
        '在官方授权页完成登录；无需在这里填写密码或访问令牌。账号注册由授权页提供。',
      ),
    ).not.toBeInTheDocument();
    await completeAccount(user);
    expect(
      within(account).getByRole('button', { name: '继续到设备' }),
    ).toBeEnabled();
  });

  it('item 5 drops the instance-storage footnote from the model step', async () => {
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={makeActions()} />);
    await completeAccount(user);
    const model = stepRegion('model');
    expect(
      within(model).queryByText(/Key 将安全保存在当前实例中/),
    ).not.toBeInTheDocument();
    await user.type(within(model).getByLabelText(/DeepSeek API Key/), MODEL_TOKEN);
    expect(
      within(model).queryByText(/Key 将安全保存在当前实例中/),
    ).not.toBeInTheDocument();
  });

  it('item 4 returns from the model step to the device step and keeps the entered key', async () => {
    const user = userEvent.setup();
    render(<FirstRunOnboarding actions={makeActions()} />);
    await completeAccount(user);
    const model = stepRegion('model');
    await user.type(within(model).getByLabelText(/DeepSeek API Key/), MODEL_TOKEN);
    await user.click(
      within(model).getByRole('button', { name: '返回上一步' }),
    );
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: '设备' })).toHaveFocus(),
    );
    expect(stepNavView('account')).toBe('completed');
    expect(stepNavView('device')).toBe('completed');
    expect(stepNavStatus('runtime')).toBe('pending');
    expect(stepNavView('runtime')).toBe('locked');
    await user.click(
      within(stepRegion('device')).getByRole('button', {
        name: '继续到运行配置',
      }),
    );
    await waitFor(() =>
      expect(screen.getByRole('heading', { name: '模型服务' })).toHaveFocus(),
    );
    expect(
      within(stepRegion('model')).getByLabelText(/DeepSeek API Key/),
    ).toHaveValue(MODEL_TOKEN);
  });
});
