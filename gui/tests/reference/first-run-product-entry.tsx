// 测试专用产品入口（PR-B 返修）：用产品 App/AppShell → FirstRunPage 路由渲染，
// firstRun capability 由 test-only fixture 提供 empty-home 数据并按真实 action 推进。
// ?account=unknown 测试开关：authenticateAccount 解析为 unknown（message=无法联系账号服务）
// 且不写入登录状态；默认（无该查询参数）保持成功 fixture 不变。
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { useEffect, useReducer } from 'react';
import { AppShell } from '../../src/app/AppShell';
import { GuiCapabilitiesProvider } from '../../src/app/capabilities';
import { appReducer } from '../../src/state/appReducer';
import { createFixtureState } from '../../src/dev/fixtures/fixture-state';
import type { FirstRunActions } from '../../src/features/onboarding/impl/types';
import type { GuiCapabilities } from '../../src/app/capabilities';
import '../../src/styles/tokens.css';
import '../../src/styles/app.css';
import '../../src/styles/workspace-pages.css';
import '../../src/styles/shadcn.css';
import { DEMO_BASELINE } from '../support/demo-baseline';

type AuthState = { authenticated: boolean; owner: string | null; deviceReady: boolean; sidecarError: string | null; ready: boolean };

const signedOut: AuthState = { authenticated: false, owner: null, deviceReady: false, sidecarError: null, ready: true };
const loggedIn: AuthState = { ...signedOut, authenticated: true, owner: 'fixture-owner' };
const deviceReady: AuthState = { ...loggedIn, deviceReady: true };

let auth: AuthState = signedOut;

const firstRun: FirstRunActions = {
  async getAccountState() {
    return {
      account: auth.authenticated ? { status: 'authenticated', owner: auth.owner ?? undefined } : { status: 'unauthenticated' },
      device: auth.deviceReady ? { status: 'ready' } : { status: 'not-ready' },
    };
  },
  async getSetupState() {
    return {
      keySaved: false, pendingAction: null,
    };
  },
  async authenticateAccount() {
    if (new URLSearchParams(window.location.search).get('account') === 'unknown') {
      return { status: 'unknown', message: '无法联系账号服务' };
    }
    auth = loggedIn;
    return { status: 'authenticated', owner: 'fixture-owner' };
  },
  async initializeDevice() {
    auth = deviceReady;
    return { status: 'ready' };
  },
  async saveModelKey() { return { status: 'saved' }; },
  async cancelAccount() { return { ok: true, cancelled: true }; },
};

/**
 * 产品外壳直接挂载：AppShell → FirstRunPage（'first-run' 路由），
 * firstRunScenario 取 'live'，来源说明由 capabilities 输入决定，不检查构建模式。
 * firstRun capabilities 用 test-only fixture 提供 empty-home 与真实 action。
 */
// 初始状态是产品的 settings 页；测试脚本通过参考页事件进入 first-run，
// 不在 SettingsPage 恢复已移除的按钮，也不通过刷新丢失参考状态。
const initialSettingsState = { ...createFixtureState(DEMO_BASELINE, 'live'), page: 'settings' as const };

function ProductFirstRunEntry() {
  const [state, dispatch] = useReducer(appReducer, initialSettingsState);
  useEffect(() => {
    const openFirstRun = () => dispatch({ type: 'navigate', page: 'first-run' });
    window.addEventListener('hyprial-test-open-first-run', openFirstRun);
    return () => window.removeEventListener('hyprial-test-open-first-run', openFirstRun);
  }, []);
  return (
    <AppShell
      firstRunScenario="live"
      state={state}
      dispatch={dispatch}
    />
  );
}

// 完整 GuiCapabilities：除 firstRun 外的字段在产品类型里都是可选的，直接构造即可，无需断言。
const capabilities: GuiCapabilities = { firstRun };

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <GuiCapabilitiesProvider value={capabilities}>
      <ProductFirstRunEntry />
    </GuiCapabilitiesProvider>
  </StrictMode>,
);
