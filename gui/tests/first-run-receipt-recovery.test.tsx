import { render, screen } from '@testing-library/react';
import { expect, it } from 'vitest';
import { projectAccountStatus } from '../src/features/account/impl/connected-account-actions';
import { FirstRunOnboarding } from '../src/features/onboarding';
import type { OnboardingStatusResult } from '../src/client/generated/types';

function status(overrides: Partial<OnboardingStatusResult> = {}): OnboardingStatusResult {
  return {
    authenticated: true,
    deviceReady: true,
    initialized: true,
    nextActions: [],
    ok: true,
    operations: [],
    owner: 'alice',
    phase: 'serving',
    ...overrides,
  };
}

function navStatus(id: string): string | null {
  return document.querySelector(`[data-step-nav="${id}"]`)?.getAttribute('data-status') ?? null;
}

const COMPLETE_SETUP = {
  keySaved: true,
  pendingAction: null,
} as const;

const STALE_SETUP = {
  keySaved: false,
  pendingAction: null,
} as const;

it('projects the P1 firstRun receipt without changing account/device', () => {
  const snapshot = projectAccountStatus(status({
    firstRun: {
      state: 'recorded',
      revision: 2,
      confirmed: ['configuration', 'reply'],
      updatedAt: '2026-11-18T00:00:00Z',
    },
  }));
  expect(snapshot.account).toEqual({ status: 'authenticated', owner: 'alice' });
  expect(snapshot.device).toEqual({ status: 'ready' });
  expect(snapshot.firstRun).toEqual({
    state: 'recorded',
    revision: 2,
    confirmed: ['configuration', 'reply'],
    updatedAt: '2026-11-18T00:00:00Z',
  });
});

it('treats a server without firstRun as unavailable, not as no history', () => {
  const snapshot = projectAccountStatus(status());
  expect(snapshot.firstRun).toEqual({
    state: 'unavailable',
    revision: 0,
    confirmed: [],
    updatedAt: null,
    skipped: false,
  });
});

it('restores a recorded configuration receipt as the conversation step', async () => {
  render(<FirstRunOnboarding actions={{
    getSetupState: async () => COMPLETE_SETUP,
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'alice' },
      device: { status: 'ready' },
      firstRun: {
        state: 'recorded',
        revision: 1,
        confirmed: ['configuration'],
        updatedAt: '2026-11-18T00:00:00Z',
      },
    }),
  }} />);
  await screen.findByRole('heading', { name: '首次对话' });
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'conversation');
  expect(screen.queryByRole('heading', { name: '创建与启动 Squire' })).not.toBeInTheDocument();
  expect(navStatus('account')).toBe('completed');
  expect(navStatus('device')).toBe('completed');
  expect(navStatus('runtime')).toBe('completed');
});

it('shows an unavailable receipt without treating it as no history', async () => {
  render(<FirstRunOnboarding actions={{
    authenticateAccount: async () => ({ status: 'unavailable' }),
    getAccountState: async () => ({
      account: { status: 'unavailable', message: '暂时无法读取账号信息，请刷新重试。' },
      device: { status: 'unknown', message: '设备状态尚未确认。' },
      firstRun: { state: 'unavailable', revision: 0, confirmed: [], updatedAt: null },
    }),
  }} />);
  expect(await screen.findByText(/无法读取首次设置记录/)).toBeInTheDocument();
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'account');
  expect(screen.queryByRole('button', { name: '继续到设备' })).not.toBeInTheDocument();
});

it('does not restore a recorded configuration before the device is ready', async () => {
  render(<FirstRunOnboarding actions={{
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'alice' },
      device: { status: 'not-ready' },
      firstRun: {
        state: 'recorded',
        revision: 1,
        confirmed: ['configuration'],
        updatedAt: '2026-11-18T00:00:00Z',
      },
    }),
  }} />);
  expect(await screen.findByLabelText('账号：账号已登录')).toBeInTheDocument();
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'account');
  expect(screen.queryByRole('heading', { name: '首次对话' })).not.toBeInTheDocument();
});

it('keeps state none as no history without restoring a step', async () => {
  render(<FirstRunOnboarding actions={{
    authenticateAccount: async () => ({ status: 'unauthenticated' }),
    getAccountState: async () => ({
      account: { status: 'unauthenticated' },
      device: { status: 'not-ready' },
      firstRun: { state: 'none', revision: 0, confirmed: [], updatedAt: null },
    }),
  }} />);
  expect(await screen.findByRole('button', { name: '登录 Hyprial' })).toBeInTheDocument();
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'account');
  expect(screen.queryByText(/首次设置记录/)).not.toBeInTheDocument();
});

it('does not restore a receipt when the status request fails', async () => {
  render(<FirstRunOnboarding actions={{
    authenticateAccount: async () => ({ status: 'unauthenticated' }),
    getAccountState: async () => { throw new Error('offline'); },
  }} />);
  expect(await screen.findByText('登录结果未知')).toBeInTheDocument();
  expect(screen.getAllByText('无法读取登录状态，请重试登录。').length).toBeGreaterThan(0);
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'account');
  expect(screen.queryByText(/首次设置记录/)).not.toBeInTheDocument();
});

it('keeps an unsupported-version receipt visible and does not restore a step', async () => {
  render(<FirstRunOnboarding actions={{
    authenticateAccount: async () => ({ status: 'authenticated' }),
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'alice' },
      device: { status: 'ready' },
      firstRun: {
        state: 'unsupported-version',
        revision: 0,
        confirmed: [],
        updatedAt: null,
      },
    }),
  }} />);
  expect(await screen.findByText(/首次设置记录版本不受支持/)).toBeInTheDocument();
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'account');
  expect(screen.queryByRole('heading', { name: '首次对话' })).not.toBeInTheDocument();
});

it('does not restore reply confirmation without its configuration receipt', async () => {
  render(<FirstRunOnboarding actions={{
    authenticateAccount: async () => ({ status: 'authenticated' }),
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'alice' },
      device: { status: 'ready' },
      firstRun: {
        state: 'recorded',
        revision: 2,
        confirmed: ['reply'],
        updatedAt: '2026-11-18T00:00:00Z',
      },
    }),
  }} />);
  expect(await screen.findByLabelText('账号：账号已登录')).toBeInTheDocument();
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'account');
  expect(screen.queryByRole('heading', { name: '首次对话' })).not.toBeInTheDocument();
});

it('restores a recorded reply receipt as the conversation step', async () => {
  render(<FirstRunOnboarding actions={{
    getSetupState: async () => COMPLETE_SETUP,
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'alice' },
      device: { status: 'ready' },
      firstRun: {
        state: 'recorded',
        revision: 2,
        confirmed: ['configuration', 'reply'],
        updatedAt: '2026-11-18T00:00:00Z',
      },
    }),
  }} />);
  await screen.findByRole('heading', { name: '首次对话' });
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'conversation');
  expect(navStatus('account')).toBe('completed');
  expect(navStatus('device')).toBe('completed');
  expect(navStatus('runtime')).toBe('completed');
  expect(navStatus('conversation')).toBe('blocked');
});

it('keeps a recorded configuration unconfirmed when current setup facts are stale', async () => {
  render(<FirstRunOnboarding actions={{
    getSetupState: async () => STALE_SETUP,
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'alice' },
      device: { status: 'ready' },
      firstRun: {
        state: 'recorded',
        revision: 1,
        confirmed: ['configuration'],
        updatedAt: '2026-11-18T00:00:00Z',
      },
    }),
  }} />);
  expect(await screen.findByText('历史运行配置待核对')).toBeInTheDocument();
  expect(navStatus('runtime')).not.toBe('completed');
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'model');
});

it('keeps a recorded configuration unconfirmed when the setup read fails', async () => {
  render(<FirstRunOnboarding actions={{
    getSetupState: async () => { throw new Error('offline'); },
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'alice' },
      device: { status: 'ready' },
      firstRun: {
        state: 'recorded',
        revision: 1,
        confirmed: ['configuration'],
        updatedAt: '2026-11-18T00:00:00Z',
      },
    }),
  }} />);
  expect(await screen.findByText('历史运行配置待核对')).toBeInTheDocument();
  expect(navStatus('runtime')).not.toBe('completed');
});

it('keeps a corrupt receipt visible and does not treat it as new history', async () => {
  render(<FirstRunOnboarding actions={{
    getAccountState: async () => ({
      account: { status: 'authenticated', owner: 'alice' },
      device: { status: 'ready' },
      firstRun: {
        state: 'corrupt',
        revision: 0,
        confirmed: [],
        updatedAt: null,
      },
    }),
  }} />);
  expect(await screen.findByText(/检测到损坏的首次设置记录/)).toBeInTheDocument();
  expect(document.querySelector('[data-active-step]')).toHaveAttribute('data-active-step', 'account');
});
