/**
 * Decision block (Task FM2, 2026-11-03):
 * - Review FM rejected a197c90f44: in the live composition an accepted+confirmed
 *   first message never completed first-run, because App forced first-run mode
 *   'unavailable' and the reducer dropped every first-run-* action. These
 *   App-level tests pin the fixed live composition: real LivePort chat contract,
 *   explicitly injected narrow auth/setup capabilities, completion only via
 *   accepted+confirmed sessions.send.
 * - The daemon boundary is the same fake LivePort used by tests/live; mock stays
 *   behind explicit test selection and nothing writes storage.
 */
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { App } from '../../src/App';
import { DEMO_BASELINE } from '../support/demo-baseline';
import { createNativeGuiCapabilities } from '../../src/app/capabilities';
import { LiveError, type LivePort } from '../../src/adapters/live';
import type { AuthClient, AuthState } from '../../src/platform/auth';
import type { SetupClient } from '../../src/platform/setup';

const TARGET_URI = 'agent:test-owner@hub:node:squire';
const MESSAGE_ID = 'msg-live-1';
const FIRST_MESSAGE = '第一条真实消息';

function fakeBackend(send: 'accept' | 'reject-definitive') {
  const calls: string[] = [];
  const sendRequests: { text: string; idempotencyKey: string }[] = [];
  const request = (params: Record<string, unknown>) => {
    const body = params.request as { text: string; idempotencyKey: string };
    return { text: body.text, idempotencyKey: body.idempotencyKey };
  };
  const port: LivePort = {
    call: vi.fn(async (operation: string, params: Record<string, unknown> = {}) => {
      calls.push(operation);
      if (operation === 'targets.list') {
        return { targets: [{ targetUri: TARGET_URI, status: 'online' }], preferredTargetUri: TARGET_URI };
      }
      if (operation === 'sessions.pending') return { requests: [] };
      if (operation === 'lark.adapters') return { adapters: [] };
      if (operation === 'sessions.create') {
        return { sessionId: 's-live', conversationId: 'c-live', targetUri: params.targetUri };
      }
      if (operation === 'sessions.history') return { events: [], nextCursor: null };
      if (operation === 'sessions.send') {
        if (send === 'reject-definitive') {
          throw new LiveError('target-unavailable', '服务明确拒绝了这条消息。', true, false);
        }
        const body = request(params);
        sendRequests.push(body);
        return { status: 'accepted', commandId: body.idempotencyKey, conversationId: 'c-live', messageId: MESSAGE_ID };
      }
      if (operation === 'sessions.send.confirm') {
        const body = request(params);
        return { status: 'accepted', commandId: body.idempotencyKey, conversationId: 'c-live', messageId: MESSAGE_ID };
      }
      throw new LiveError('unexpected-operation', `测试未实现的操作：${operation}`);
    }),
  };
  return { port, calls, sendRequests };
}

const signedOut: AuthState = {
  authenticated: false,
  owner: null,
  deviceReady: false,
  sidecarError: null,
  ready: false,
};
const authenticatedDevicePending: AuthState = {
  authenticated: true,
  owner: 'test-owner',
  deviceReady: false,
  sidecarError: null,
  ready: true,
};
const authenticatedReady: AuthState = {
  authenticated: true,
  owner: 'test-owner',
  deviceReady: true,
  sidecarError: null,
  ready: true,
};

function hostAuth(initial = signedOut): AuthClient {
  let state = initial;
  return {
    status: vi.fn(async () => state),
    login: vi.fn(async () => {
      state = authenticatedDevicePending;
      return { ok: true, state };
    }),
    device: vi.fn(async () => {
      state = authenticatedReady;
      return { ok: true, state };
    }),
    cancel: vi.fn(async () => ({ ok: true, cancelled: true })),
  };
}

function hostSetup(): SetupClient {
  return {
    status: vi.fn(async () => ({
      ok: true, keySaved: false, pendingAction: null, modelVerified: false as const,
    })),
    saveModelKey: vi.fn(async () => ({
      ok: true, saved: true, modelVerified: false as const,
    })),
  };
}

function renderLiveComposition(send: 'accept' | 'reject-definitive', initialPage?: 'first-run') {
  const backend = fakeBackend(send);
  const view = render(
    <App
      initialPage={initialPage}
      liveClient={backend.port} baseline={DEMO_BASELINE}
      capabilities={createNativeGuiCapabilities(hostAuth(initialPage ? signedOut : authenticatedReady), hostSetup())}
    />,
  );
  return { ...backend, view };
}

function renderFirstRun(backend: ReturnType<typeof fakeBackend>) {
  return render(
    <App
      initialPage="first-run"
      liveClient={backend.port}
      capabilities={createNativeGuiCapabilities(hostAuth(), hostSetup())}
    />,
  );
}

async function walkToConversationStep(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('button', { name: '登录 Hyprial' }));
  await user.click(await screen.findByRole('button', { name: '继续到设备' }));
  await user.click(await screen.findByRole('button', { name: '初始化当前设备' }));
  await user.click(await screen.findByRole('button', { name: '继续到运行配置' }));
  await screen.findByRole('heading', { name: '模型服务' });
  await user.type(screen.getByLabelText(/DeepSeek API Key/), 'live-test-key-not-real');
  await user.click(screen.getByRole('button', { name: '保存并继续' }));
  await screen.findByRole('heading', { name: '首次对话' });
}

describe('live composition: the first message completes first-run only when accepted+confirmed', () => {
  it('accepted sessions.send + sessions.send.confirm show the start page with 主导航', async () => {
    const user = userEvent.setup();
    const backend = renderLiveComposition('accept', 'first-run');
    await walkToConversationStep(user);
    const composer = screen.getByRole('textbox', { name: `给 ${TARGET_URI} 的消息` });
    await user.type(composer, FIRST_MESSAGE);
    await user.click(screen.getByRole('button', { name: '发送' }));

    await screen.findByRole('navigation', { name: '主导航' });
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(backend.calls).toContain('sessions.send');
    expect(backend.calls).toContain('sessions.send.confirm');
    expect(backend.sendRequests[0]).toEqual({ text: FIRST_MESSAGE, idempotencyKey: expect.any(String) });
    expect(screen.getByRole('heading', { name: '开始', level: 1 })).toBeInTheDocument();
  });

  it('a definitive send rejection stays on 首次对话, keeps the text and never completes', async () => {
    const user = userEvent.setup();
    const backend = renderLiveComposition('reject-definitive');
    backend.view.unmount();
    renderFirstRun(backend);
    await walkToConversationStep(user);
    const composer = screen.getByRole('textbox', { name: `给 ${TARGET_URI} 的消息` });
    await user.type(composer, '这条消息发送失败');
    await user.click(screen.getByRole('button', { name: '发送' }));

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/发送失败/);
    expect(composer).toHaveValue('这条消息发送失败');
    expect(screen.getByRole('heading', { name: '首次对话' })).toBeInTheDocument();
    expect(screen.queryByRole('navigation', { name: '主导航' })).not.toBeInTheDocument();
    expect(screen.queryByText(/本地首次设置流程已完成/)).not.toBeInTheDocument();
    expect(backend.calls).toContain('sessions.send');
    expect(backend.calls).not.toContain('sessions.send.confirm');
  });
});

describe('live composition: completion binds to this flow instance\'s own send request', () => {
  it('a chat send+confirm before opening first-run never completes it; the flow starts at the account step and only its own send completes once', async () => {
    const user = userEvent.setup();
    const backend = renderLiveComposition('accept');

    await user.click(await screen.findByRole('button', { name: '聊天' }));
    const chatComposer = screen.getByRole('textbox', { name: `给 ${TARGET_URI} 的消息` });
    await user.type(chatComposer, '早前在聊天页发出的消息');
    await user.click(screen.getByRole('button', { name: '发送' }));
    await screen.findByText('消息已被服务接收，回复以聊天记录为准。');

    backend.view.unmount();
    renderFirstRun(backend);
    expect(screen.getByRole('heading', { name: '登录' })).toBeInTheDocument();
    expect(
      document.querySelector('[aria-current="step"]')?.getAttribute('data-step-nav'),
    ).toBe('account');
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 25));
    });
    expect(screen.getByRole('heading', { name: '登录' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '开始', level: 1 })).not.toBeInTheDocument();
    expect(backend.sendRequests).toHaveLength(1);

    await walkToConversationStep(user);
    const composer = screen.getByRole('textbox', { name: `给 ${TARGET_URI} 的消息` });
    await user.type(composer, FIRST_MESSAGE);
    await user.click(screen.getByRole('button', { name: '发送' }));

    await screen.findByRole('navigation', { name: '主导航' });
    expect(screen.getByRole('heading', { level: 1, name: '开始' })).toBeInTheDocument();
    expect(backend.sendRequests.map((request) => request.text))
      .toEqual(['早前在聊天页发出的消息', FIRST_MESSAGE]);
    const [foreignKey, ownKey] = backend.sendRequests.map((request) => request.idempotencyKey);
    expect(foreignKey).not.toBe(ownKey);
    expect(backend.calls.filter((call) => call === 'sessions.send')).toHaveLength(2);
  });
});
