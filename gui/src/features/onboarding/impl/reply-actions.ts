/**
 * Decision record (web-auth-r3-p3-gui-20261118):
 * - The browser never chooses the credential, sender or reply association: it
 *   sends the Agent URI, the server derives the current config/grant, and the
 *   daemon's own attempt key is the message's idempotency key.  `message.send`
 *   is therefore the only send authority and `sendWithKey` keeps its one
 *   dispatch rule while writing no extra tab state.
 * - A lost send response is reconciled through
 *   `message.send.key_lookup`; a still-unknown outcome stays unknown and is
 *   never auto-resent.  Each attempt has one dispatch ticket per actions
 *   instance: a restored attempt is lookup-only, and every retry after the
 *   first dispatch is lookup-only.  `bind` records the durable request only
 *   after the send authority answers accepted.
 * - Confirmation is explicit: this module never calls `confirm` from a reply
 *   observer.  Only the user's click completes the loop, and only a response
 *   that proves this attempt id, operation key, request, receipt revision and
 *   both steps can complete it.
 * - Attempt/receipt shapes are validated here because the response is
 *   untrusted; a mismatch is unknown, not success.
 */

import { accessRefusal, methodRefusal, type MethodAccess, type MethodRefusal } from '../../../client';
import type {
  OnboardingFirstRunBeginParams,
  OnboardingFirstRunBeginResult,
  OnboardingFirstRunBindParams,
  OnboardingFirstRunBindResult,
  OnboardingFirstRunCancelParams,
  OnboardingFirstRunCancelResult,
  OnboardingFirstRunConfirmParams,
  OnboardingFirstRunConfirmResult,
} from '../../../client/generated/types';
import type { ChatSendApi } from '../../chat-channels';
import type {
  FirstRunAttemptView,
  FirstRunReceiptView,
  FirstRunReplyActions,
  ReplyAttemptResult,
  ReplyMessageView,
} from './types';

export const FIRST_RUN_REPLY_MESSAGE =
  '你好！这是首次连接确认消息，请回复本条消息。';

export const FIRST_RUN_REPLY_SEND_UNKNOWN =
  '发送结果未确认；不会自动重发。可稍后重新检查，或重新开始确认。';

const ATTEMPT_STATES = new Set([
  'pending',
  'bound',
  'confirmed',
  'cancelled',
  'superseded',
]);
const RECEIPT_STATES = new Set([
  'none',
  'recorded',
  'corrupt',
  'unsupported-version',
  'unavailable',
]);
const STEP = new Set(['configuration', 'reply']);

export interface FirstRunReplyClient {
  readonly onboarding: {
    readonly firstRun: {
      begin(params: OnboardingFirstRunBeginParams): Promise<OnboardingFirstRunBeginResult>;
      bind(params: OnboardingFirstRunBindParams): Promise<OnboardingFirstRunBindResult>;
      cancel(params: OnboardingFirstRunCancelParams): Promise<OnboardingFirstRunCancelResult>;
      confirm(params: OnboardingFirstRunConfirmParams): Promise<OnboardingFirstRunConfirmResult>;
    };
  };
  readonly message: {
    history(params: { conversationId: string }): Promise<unknown>;
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === 'object' && !Array.isArray(value);
}

function text(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

function nullableText(value: unknown): string | null | undefined {
  if (value === null || value === undefined) return null;
  return typeof value === 'string' ? value : undefined;
}

function integer(value: unknown, minimum: number): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= minimum ? value : null;
}

export function attemptView(raw: unknown): FirstRunAttemptView | null {
  if (!isObject(raw)) return null;
  const state = raw.state;
  const kind = raw.credentialKind;
  const attemptId = text(raw.attemptId);
  const operationKey = text(raw.operationKey);
  const sendKey = text(raw.sendKey);
  const agentUri = text(raw.agentUri);
  const harness = text(raw.harness);
  const revision = integer(raw.revision, 1);
  const receiptRevision = integer(raw.receiptRevision, 0);
  const loginEpoch = integer(raw.loginEpoch, 1);
  const agentCreatedAtMs = integer(raw.agentCreatedAtMs, 0);
  const provider = nullableText(raw.provider);
  const model = nullableText(raw.model);
  const configRevision = nullableText(raw.configRevision);
  const requestMessageId = nullableText(raw.requestMessageId);
  const conversationId = nullableText(raw.conversationId);
  const replyMessageId = nullableText(raw.replyMessageId);
  if (
    typeof state !== 'string' || !ATTEMPT_STATES.has(state)
    || (kind !== 'key' && kind !== 'native')
    || attemptId === null || operationKey === null || sendKey === null
    || agentUri === null || harness === null
    || revision === null || receiptRevision === null || loginEpoch === null
    || agentCreatedAtMs === null
    || provider === undefined || model === undefined || configRevision === undefined
    || requestMessageId === undefined || conversationId === undefined
    || replyMessageId === undefined
  ) return null;
  return {
    attemptId, revision, state: state as FirstRunAttemptView['state'],
    operationKey, sendKey, receiptRevision, loginEpoch, agentUri, agentCreatedAtMs,
    provider, model, harness, configRevision,
    credentialKind: kind, requestMessageId, conversationId, replyMessageId,
  };
}

export function receiptView(raw: unknown): FirstRunReceiptView | null {
  if (!isObject(raw)) return null;
  const state = raw.state;
  const revision = integer(raw.revision, 0);
  const updatedAt = raw.updatedAt;
  const skipped = raw.skipped;
  const confirmed = raw.confirmed;
  if (
    typeof state !== 'string' || !RECEIPT_STATES.has(state)
    || revision === null || !Array.isArray(confirmed)
    || (updatedAt !== null && typeof updatedAt !== 'string')
    || (skipped !== undefined && typeof skipped !== 'boolean')
  ) return null;
  if (!confirmed.every(step => typeof step === 'string' && STEP.has(step))) return null;
  return {
    state: state as FirstRunReceiptView['state'],
    revision,
    confirmed: confirmed as FirstRunReceiptView['confirmed'],
    updatedAt: updatedAt as string | null,
    skipped: skipped === true,
  };
}

function unavailable(refusal: MethodRefusal, message: string): ReplyAttemptResult {
  return { status: 'unavailable', refusal, message };
}

function failure(error: unknown, fallback: string): ReplyAttemptResult {
  const refusal = methodRefusal(error);
  if (refusal) return unavailable(refusal, '当前界面无法调用该操作。');
  const dispatch = isObject(error) ? error.dispatch : undefined;
  if (dispatch === 'unknown') {
    return { status: 'unknown', message: `${fallback}结果未知；不会自动重试，请刷新状态核对。` };
  }
  const code = isObject(error) ? text(error.code) : null;
  return {
    status: 'failed',
    message: code ? `${fallback}失败：${code}` : `${fallback}失败。`,
  };
}

function newOperationKey(): string {
  const crypto_ = globalThis.crypto;
  if (crypto_ && typeof crypto_.randomUUID === 'function') return crypto_.randomUUID();
  return `opkey-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 14)}`;
}

function historyMessages(raw: unknown, target: string): readonly ReplyMessageView[] {
  if (!isObject(raw) || !Array.isArray(raw.entries)) return [];
  const messages: ReplyMessageView[] = [];
  for (const entry of raw.entries) {
    if (!isObject(entry)) continue;
    const id = text(entry.messageId);
    const body = text(entry.message);
    const sender = text(entry.from);
    if (id === null || body === null || sender === null) continue;
    messages.push({
      id,
      author: entry.messageClass === 'system'
        ? 'system'
        : sender === target ? 'agent' : sender.startsWith('user:') ? 'user' : 'system',
      text: body,
    });
  }
  return messages;
}

export function createFirstRunReplyActions(
  client: FirstRunReplyClient,
  chat: Pick<ChatSendApi, 'sendWithKey' | 'recoverKey'>,
  access?: MethodAccess,
): FirstRunReplyActions {
  const dispatched = new Set<string>();
  let pendingBegin: { key: string; agentUri: string } | null = null;

  function gate(method: string): ReplyAttemptResult | null {
    const refusal = accessRefusal(access, method);
    return refusal ? unavailable(refusal, '当前界面无法调用该操作。') : null;
  }

  async function bind(
    attempt: FirstRunAttemptView,
    conversationId: string,
    requestMessageId: string,
  ): Promise<ReplyAttemptResult> {
    const blocked = gate('onboarding.firstRun.bind');
    if (blocked) return blocked;
    try {
      const result = await client.onboarding.firstRun.bind({
        attemptId: attempt.attemptId,
        expectedRevision: attempt.revision,
        operationKey: attempt.operationKey,
        conversationId,
        requestMessageId,
      });
      const next = attemptView(result?.attempt);
      if (result?.ok !== true || next === null) {
        return { status: 'unknown', message: '发送已接受，但请求绑定结果无法确认；不会自动重发。' };
      }
      return { status: 'bound', attempt: next };
    } catch (error) {
      return failure(error, '绑定发送请求');
    }
  }

  const actions: FirstRunReplyActions = {
    async begin(agentUri: string): Promise<ReplyAttemptResult> {
      const blocked = gate('onboarding.firstRun.begin');
      if (blocked) return blocked;
      if (!agentUri) return { status: 'failed', message: '没有可确认的 Agent。' };
      const reuse = pendingBegin?.agentUri === agentUri ? pendingBegin.key : newOperationKey();
      pendingBegin = { key: reuse, agentUri };
      try {
        const result = await client.onboarding.firstRun.begin({
          operationKey: reuse,
          agentUri,
        });
        const attempt = attemptView(result?.attempt);
        if (result?.ok !== true || attempt === null) {
          return { status: 'unknown', message: '确认流程的创建结果无法确认；请刷新状态核对。' };
        }
        pendingBegin = null;
        return { status: 'begun', attempt };
      } catch (error) {
        const failed = failure(error, '创建真实回复确认');
        if (failed.status !== 'unknown') pendingBegin = null;
        return failed;
      }
    },

    async send(
      attempt: FirstRunAttemptView,
      message: string,
      options: { dispatch?: boolean } = {},
    ): Promise<ReplyAttemptResult> {
      if (attempt.state !== 'pending' && attempt.state !== 'bound') {
        return { status: 'failed', message: '当前确认流程不能发送消息。' };
      }
      if (attempt.requestMessageId && attempt.conversationId) {
        return bind(attempt, attempt.conversationId, attempt.requestMessageId);
      }
      if (!message.trim()) {
        return { status: 'failed', message: '确认消息不能为空。' };
      }
      const ticket = `${attempt.attemptId}\u0000${attempt.sendKey}`;
      // A previous click, refresh or rebuilt controller may already have
      // dispatched; the daemon key is the recovery fact, so an existing record
      // is bound instead of re-sent.
      const recovered = await chat.recoverKey(attempt.sendKey);
      if (recovered.status === 'accepted') {
        return bind(attempt, recovered.conversationId, recovered.messageId);
      }
      if (options.dispatch === false || dispatched.has(ticket)) {
        return { status: 'unknown', message: FIRST_RUN_REPLY_SEND_UNKNOWN };
      }
      dispatched.add(ticket);
      const outcome = await chat.sendWithKey({
        to: [attempt.agentUri],
        message,
        summary: '首次真实回复确认',
      }, attempt.sendKey);
      if (outcome.status === 'accepted') {
        return bind(attempt, outcome.conversationId, outcome.messageId);
      }
      if (outcome.status === 'unknown') {
        const retried = await chat.recoverKey(attempt.sendKey);
        if (retried.status === 'accepted') {
          return bind(attempt, retried.conversationId, retried.messageId);
        }
        return { status: 'unknown', message: FIRST_RUN_REPLY_SEND_UNKNOWN };
      }
      if (outcome.status === 'refused') {
        return { status: 'failed', message: outcome.notice };
      }
      if (outcome.status === 'not-started') {
        return outcome.reason === 'method-refused'
          ? unavailable({ code: outcome.code, source: outcome.source }, '当前界面无法发送确认消息。')
          : { status: 'failed', message: outcome.notice };
      }
      return { status: 'failed', message: '确认消息未发送。' };
    },

    async history(attempt: FirstRunAttemptView): Promise<readonly ReplyMessageView[]> {
      if (!attempt.conversationId) return [];
      try {
        const raw = await client.message.history({
          conversationId: attempt.conversationId,
        });
        return historyMessages(raw, attempt.agentUri);
      } catch {
        return [];
      }
    },
    async confirm(attempt: FirstRunAttemptView, receiptRevision: number): Promise<ReplyAttemptResult> {
      const blocked = gate('onboarding.firstRun.confirm');
      if (blocked) return blocked;
      if (attempt.state !== 'bound' && attempt.state !== 'confirmed') {
        return { status: 'failed', message: '请先发送确认消息并等待真实回复。' };
      }
      if (!attempt.requestMessageId || !attempt.conversationId) {
        return { status: 'failed', message: '当前确认流程缺少已绑定的请求。' };
      }
      try {
        const result = await client.onboarding.firstRun.confirm({
          attemptId: attempt.attemptId,
          expectedAttemptRevision: attempt.revision,
          expectedReceiptRevision: receiptRevision,
          operationKey: attempt.operationKey,
        });
        const next = attemptView(result?.attempt);
        const receipt = receiptView(result?.firstRun);
        if (result?.ok !== true || next === null || receipt === null
          || next.state !== 'confirmed'
          || next.attemptId !== attempt.attemptId
          || next.operationKey !== attempt.operationKey
          || next.agentUri !== attempt.agentUri
          || next.requestMessageId !== attempt.requestMessageId
          || next.replyMessageId === null
          || receipt.state !== 'recorded'
          || attempt.receiptRevision !== receiptRevision
          || receipt.revision !== receiptRevision + 1
          || next.receiptRevision !== receiptRevision
          || !receipt.confirmed.includes('configuration')
          || !receipt.confirmed.includes('reply')
          || result.requestMessageId !== attempt.requestMessageId
          || result.replyMessageId !== next.replyMessageId) {
          return { status: 'unknown', message: '确认结果无法验证；不会当作已完成。' };
        }
        return { status: 'confirmed', attempt: next, firstRun: receipt };
      } catch (error) {
        return failure(error, '确认真实回复');
      }
    },

    async cancel(attempt: FirstRunAttemptView): Promise<ReplyAttemptResult> {
      const blocked = gate('onboarding.firstRun.cancel');
      if (blocked) return blocked;
      try {
        const result = await client.onboarding.firstRun.cancel({
          attemptId: attempt.attemptId,
          expectedRevision: attempt.revision,
          operationKey: attempt.operationKey,
        });
        const next = attemptView(result?.attempt);
        if (result?.ok !== true || next === null) {
          return { status: 'unknown', message: '取消结果无法确认；请刷新状态核对。' };
        }
        return { status: 'cancelled', attempt: next };
      } catch (error) {
        return failure(error, '取消确认流程');
      }
    },
  };
  return Object.freeze(actions);
}
