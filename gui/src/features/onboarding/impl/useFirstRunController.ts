import { useEffect, useRef, useState } from 'react';
import { useNotifyUnavailable } from '../../../components/notifications';
import type { StepStatus } from '../../../components/primitives';
import { safeAuthorizationUrl } from './authorization-url';
import { modelKeyStatusCheck } from './model-save-result';
import type {
  AccountActionResult, AccountProgress, AccountSnapshot, AttemptState, DeviceActionResult,
  FirstRunActions, FirstRunAttemptView, FirstRunOnboardingProps, FirstRunReceiptView, FirstRunStepId,
  ModelKeyStatusCheck, ReplyAttemptResult, ReplyMessageView, RuntimeConfigurationResult, SetupSnapshot,
} from './types';
import { FIRST_RUN_REPLY_MESSAGE } from './reply-actions';

const NO_ACTIONS: FirstRunActions = Object.freeze({});

const INITIAL_COMPLETED: Readonly<Record<FirstRunStepId, boolean>> = {
  account: false,
  device: false,
  model: false,
  conversation: false,
};

const LOGIN_SUBJECT = '登录 Hyprial';
const DEVICE_SUBJECT = '初始化当前设备';

function findResult<T>(
  attempt: AttemptState<T>,
): T | null {
  return attempt.kind === 'settled' ? attempt.result : null;
}

/**
 * Owns first-run orchestration and late-result/cancellation guards.
 * Runtime writes carry a ticket (actions generation + attempt): pending and
 * unknown live here, so they survive step unmount/reopen, while a result from
 * an older actions generation is discarded instead of locking the new context.
 * Consumes injected capabilities only; it stores public readiness metadata,
 * never a model key. Views own layout, input values and DOM focus.
 */
export function useFirstRunController({
  scenario: requestedScenario = 'live', actions: providedActions,
  chatStatus = 'unavailable', conversation, onExit, resumeFromStatus = false,
  onComplete,
}: FirstRunOnboardingProps) {
  const scenario = requestedScenario;
  const actions = providedActions ?? NO_ACTIONS;
  const notifyUnavailable = useNotifyUnavailable();

  const [activeStep, setActiveStep] = useState<FirstRunStepId>('account');
  const [completed, setCompleted] = useState(INITIAL_COMPLETED);
  const [firstRun, setFirstRun] = useState<FirstRunReceiptView | null>(null);
  const [runtimeStale, setRuntimeStale] = useState(false);
  const [runtimeWritePending, setRuntimeWritePending] = useState(false);
  const [runtimeWriteUnknown, setRuntimeWriteUnknown] = useState<RuntimeConfigurationResult | null>(null);
  const [replyAttempt, setReplyAttemptState] = useState<FirstRunAttemptView | null>(null);
  const [replyAgentUri, setReplyAgentUri] = useState<string | null>(null);
  const [replyMessages, setReplyMessages] = useState<readonly ReplyMessageView[]>([]);
  const [replyBusy, setReplyBusy] = useState(false);
  const [replyNotice, setReplyNotice] = useState<string | null>(null);
  const [skipPending, setSkipPending] = useState(false);
  const [skipNotice, setSkipNotice] = useState<string | null>(null);
  const replyRun = useRef(0);
  const replyHistoryRun = useRef(0);
  const replyAttemptRef = useRef<FirstRunAttemptView | null>(null);
  const setReplyAttempt = (next: FirstRunAttemptView | null): void => {
    replyAttemptRef.current = next;
    setReplyAttemptState(next);
  };
  const firstRunRef = useRef<FirstRunReceiptView | null>(null);
  const [accountProgress, setAccountProgress] = useState<AccountProgress | null>(null);
  const [accountAttempt, setAccountAttempt] = useState<
    AttemptState<AccountActionResult>
  >({ kind: 'idle' });
  const [deviceAttempt, setDeviceAttempt] = useState<
    AttemptState<DeviceActionResult>
  >({ kind: 'idle' });
  const [modelSaved, setModelSaved] = useState(false);
  const modelSavedRef = useRef(false);
  const [modelRestored, setModelRestored] = useState(false);
  const [modelSavePending, setModelSavePending] = useState(false);
  const localSaveUnknown = useRef(false);
  const [modelDraft, setModelDraft] = useState('');
  const setupReadId = useRef(0);
  const restoreSetupRef = useRef<((guard?: () => boolean) => Promise<void>) | null>(null);
  const cancelledSnapshot = useRef<AccountSnapshot | null>(null);
  const actionsRef = useRef(actions);
  const actionGeneration = useRef(0);
  const runtimeAttempt = useRef(0);
  const runtimeWrite = useRef<{
    readonly generation: number;
    readonly attempt: number;
    readonly kind: 'pending' | 'unknown';
  } | null>(null);

  const accountAttemptId = useRef(0);
  const shouldFocusNextStep = useRef(false);
  const lastAccountRequest = useRef(false);
  const deviceAttemptId = useRef(0);
  const accountRunning = useRef(false);
  const deviceRunning = useRef(false);
  const authorizationPopup = useRef<Window | null>(null);
  function closeBlankAuthorizationPopup(): void {
    const popup = authorizationPopup.current;
    authorizationPopup.current = null;
    try {
      if (popup && !popup.closed && popup.location.href === 'about:blank') popup.close();
    } catch { /* A navigated authorization page belongs to the user. */ }
  }

  useEffect(() => {
    if (actionsRef.current !== actions) {
      actionsRef.current = actions;
      resetFlowForActions();
    }
    const attemptId = ++accountAttemptId.current;
    let disposed = false;
    const restoreSetup = async (guard?: () => boolean) => {
      if (!actions.getSetupState) {
        if (!actions.configureAgent && firstRunRef.current?.state === 'recorded') setRuntimeStale(true);
        return;
      }
      if (disposed || guard?.() === false) return;
      const readId = ++setupReadId.current;
      try {
        const snapshot = await actions.getSetupState();
        if (disposed || readId !== setupReadId.current || guard?.() === false) return;
        applyModelSnapshot(snapshot);
        applySetupSnapshot(snapshot);
      } catch {
        if (!disposed && !actions.configureAgent && firstRunRef.current?.state === 'recorded') {
          setRuntimeStale(true);
        }
      }
    };
    restoreSetupRef.current = restoreSetup;
    const unsubscribe = actions.subscribeAccountProgress?.((progress) => {
      if (!disposed && accountRunning.current) {
        setAccountProgress(progress);
        const url = safeAuthorizationUrl(progress.verificationUri);
        const popup = authorizationPopup.current;
        if (url && popup && !actions.openAuthorization) {
          authorizationPopup.current = null;
          try { if (!popup.closed) popup.location.replace(url); } catch { /* The visible link remains usable. */ }
        }
      }
    });
    if (actions.getAccountState) {
      setAccountAttempt({ kind: 'running' });
      void actions.getAccountState().then((snapshot) => {
        if (disposed || attemptId !== accountAttemptId.current) return;
        setAccountAttempt({ kind: 'settled', result: snapshot.account });
        setDeviceAttempt({ kind: 'settled', result: snapshot.device });
        applyFirstRun(snapshot);
        void restoreSetup();
      }).catch(() => {
        if (!disposed && attemptId === accountAttemptId.current) {
          setAccountAttempt({ kind: 'settled', result: { status: 'unknown', message: '无法读取登录状态，请重试登录。' } });
        }
      });
    } else { void restoreSetup(); }
    return () => {
      setupReadId.current += 1;
      restoreSetupRef.current = null;
      disposed = true;
      closeBlankAuthorizationPopup();
      accountAttemptId.current += 1;
      deviceAttemptId.current += 1;
      lastAccountRequest.current = false;
      unsubscribe?.();
      if (accountRunning.current || deviceRunning.current) void actions.cancelAccount?.().catch(() => {});
      accountRunning.current = false;
      deviceRunning.current = false;
    };
  }, [actions]);

  const accountResult = findResult(accountAttempt);
  const deviceResult = findResult(deviceAttempt);
  const accountAuthenticated = accountResult?.status === 'authenticated';
  const deviceReady = deviceResult?.status === 'ready';

  const accountStepStatus: StepStatus = completed.account
    ? 'completed'
    : activeStep !== 'account'
      ? 'pending'
      : accountAttempt.kind === 'running'
        ? 'active'
        : accountResult?.status === 'failed'
          ? 'failed'
          : accountResult?.status === 'unknown' || accountResult?.status === 'unavailable'
            ? 'blocked'
            : 'active';

  const deviceStepStatus: StepStatus = completed.device
    ? 'completed'
    : activeStep !== 'device'
      ? 'pending'
      : deviceAttempt.kind === 'running'
        ? 'active'
        : deviceResult?.status === 'failed'
          ? 'failed'
          : deviceResult?.status === 'unknown' || deviceResult?.status === 'unavailable'
            ? 'blocked'
            : 'active';

  const modelStepStatus: StepStatus = completed.model ? 'completed' : activeStep === 'model' ? 'active' : 'pending';

  const conversationStepStatus: StepStatus =
    completed.conversation ? 'completed'
    : activeStep !== 'conversation' ? 'pending'
    : actions.replyConfirmation ? 'active'
    : conversation ? 'active'
    : chatStatus === 'completed' ? 'completed'
    : chatStatus === 'unavailable' ? 'blocked'
    : 'active';

  function advance(from: FirstRunStepId, to: FirstRunStepId): void {
    setCompleted((current) => ({ ...current, [from]: true }));
    shouldFocusNextStep.current = true;
    setActiveStep(to);
  }

  async function runAccount(): Promise<void> {
    const authenticate = actions.authenticateAccount;
    if (!authenticate || accountRunning.current) return;
    const refusal = actions.loginRefusal?.();
    if (refusal) { notifyUnavailable(LOGIN_SUBJECT, refusal); return; }
    const attemptId = ++accountAttemptId.current;
    const previousAttempt = accountAttempt;
    const previousAccount = findResult(accountAttempt);
    const previousDevice = findResult(deviceAttempt);
    lastAccountRequest.current = true;
    accountRunning.current = true;
    setAccountProgress(null);
    setAccountAttempt({ kind: 'running' });
    // Reserve only a new window during the user gesture; blocked popups fall back to the link.
    // A native host owns the system-browser opener, so the WebView never pops a blank page.
    if (actions.subscribeAccountProgress && !actions.openAuthorization) {
      closeBlankAuthorizationPopup();
      try {
        const popup = window.open('about:blank', '_blank');
        if (popup) popup.opener = null;
        authorizationPopup.current = popup;
      } catch { /* The public progress link is the fallback. */ }
    }
    try {
      // The legacy callback shape is retained for callers; no credentials are collected.
      const result = await authenticate({ account: '', token: '' }, 'login');
      if (accountAttemptId.current !== attemptId) return;
      if (result.status === 'unavailable' && result.refusal) {
        lastAccountRequest.current = false;
        notifyUnavailable(LOGIN_SUBJECT, result.refusal);
        setAccountAttempt(previousAttempt.kind === 'running' ? { kind: 'idle' } : previousAttempt);
        return;
      }
      setAccountAttempt({ kind: 'settled', result });
      if (result.status === 'authenticated') {
        lastAccountRequest.current = false;
        if (actions.getAccountState) {
          try {
            const snapshot = await actions.getAccountState();
            if (accountAttemptId.current !== attemptId) return;
            setAccountAttempt({ kind: 'settled', result: snapshot.account });
            setDeviceAttempt({ kind: 'settled', result: snapshot.device });
            applyFirstRun(snapshot);
          } catch {
            if (accountAttemptId.current !== attemptId) return;
            // The login result is confirmed; only the device refresh failed.
            setDeviceAttempt({ kind: 'settled', result:
              previousDevice?.status === 'ready'
                ? previousDevice
                : { status: 'unknown', message: '账号已登录，但当前设备状态未能确认。请刷新状态或重试设备初始化。' },
            });
          }
        }
        await restoreSetupRef.current?.(() => accountAttemptId.current === attemptId);
      }
    } catch {
      if (accountAttemptId.current !== attemptId) return;
      setAccountAttempt({ kind: 'settled', result:
        previousAccount?.status === 'authenticated' || previousAccount?.status === 'unauthenticated'
          ? previousAccount
          : { status: 'unknown', message: '登录结果未知，无法联系账号服务。' },
      });
    } finally {
      if (accountAttemptId.current === attemptId) {
        accountRunning.current = false;
        closeBlankAuthorizationPopup();
      }
    }
  }

  async function cancelAccount(): Promise<boolean> {
    closeBlankAuthorizationPopup();
    const attemptId = ++accountAttemptId.current;
    const previousDevice = findResult(deviceAttempt);
    cancelledSnapshot.current = null;
    accountRunning.current = true;
    lastAccountRequest.current = false;
    deviceAttemptId.current += 1;
    deviceRunning.current = false;
    setAccountProgress(null);
    setAccountAttempt({ kind: 'running' });
    if (deviceAttempt.kind === 'running') {
      setDeviceAttempt({ kind: 'settled', result:
        previousDevice?.status === 'ready'
          ? previousDevice
          : { status: 'unknown', message: '已取消，设备状态未确认。' },
      });
    }
    try {
      if (!actions.cancelAccount) throw new Error('CANCEL_UNAVAILABLE');
      const result = await actions.cancelAccount();
      if (attemptId !== accountAttemptId.current) return false;
      let snapshot: AccountSnapshot | undefined;
      if (actions.getAccountState) {
        try { snapshot = await actions.getAccountState(); }
        catch { /* The cancellation remains unconfirmed. */ }
      }
      if (attemptId !== accountAttemptId.current) return false;
      // Only an explicit native acknowledgement plus a refreshed authoritative
      // state clears the account gate. Everything else stays fail-closed.
      if (result.ok === true && result.cancelled === true && snapshot) {
        cancelledSnapshot.current = snapshot;
        setAccountAttempt({ kind: 'settled', result: snapshot.account });
        setDeviceAttempt({ kind: 'settled', result: snapshot.device });
        applyFirstRun(snapshot);
        accountRunning.current = false;
        await restoreSetupRef.current?.(() => accountAttemptId.current === attemptId);
        return true;
      }
      lastAccountRequest.current = true;
      setAccountAttempt({ kind: 'settled', result: {
        status: 'unknown', message: '取消后状态未确认，请重试取消。',
      } });
      return false;
    } catch {
      if (attemptId === accountAttemptId.current) {
        lastAccountRequest.current = true;
        setAccountAttempt({ kind: 'settled', result: {
          status: 'unknown', message: '取消后状态未确认，请重试取消。',
        } });
      }
      return false;
    }
  }

  function retryAccount(): void {
    void (async () => {
      if (accountRunning.current) {
        if (!await cancelAccount()) return;
        const recovered = cancelledSnapshot.current;
        cancelledSnapshot.current = null;
        if (recovered?.account.status === 'authenticated') return;
      }
      await runAccount();
    })();
  }

  async function runDevice(): Promise<void> {
    const initialize = actions.initializeDevice;
    if (!initialize) return;
    const attemptId = ++deviceAttemptId.current;
    const previousAttempt = deviceAttempt;
    const previousDevice = findResult(deviceAttempt);
    deviceRunning.current = true;
    setDeviceAttempt({ kind: 'running' });
    try {
      const result = await initialize();
      if (attemptId !== deviceAttemptId.current) return;
      if (result.status === 'unavailable' && result.refusal) {
        notifyUnavailable(DEVICE_SUBJECT, result.refusal);
        setDeviceAttempt(previousAttempt.kind === 'running' ? { kind: 'idle' } : previousAttempt);
        return;
      }
      setDeviceAttempt({ kind: 'settled', result });
      if (actions.getAccountState) {
        try {
          const snapshot = await actions.getAccountState();
          if (attemptId !== deviceAttemptId.current) return;
          setAccountAttempt({ kind: 'settled', result: snapshot.account });
          setDeviceAttempt({ kind: 'settled', result: snapshot.account.status === 'authenticated'
            && snapshot.device.status !== 'ready' && ['failed', 'unavailable', 'unknown'].includes(result.status)
            ? result : snapshot.device });
          applyFirstRun(snapshot);
        } catch { /* Keep the explicit device result; it is not upgraded to ready. */ }
      }
    } catch {
      if (attemptId === deviceAttemptId.current) setDeviceAttempt({ kind: 'settled', result:
        previousDevice?.status === 'ready'
          ? previousDevice
          : { status: 'unknown', message: '设备初始化结果未知，无法联系设备服务。' },
      });
    } finally {
      if (attemptId === deviceAttemptId.current) deviceRunning.current = false;
    }
  }

  function continueFromAccount(): void {
    if (!accountAuthenticated) {
      return;
    }
    advance('account', 'device');
  }

  function continueFromDevice(): void {
    if (!deviceReady) {
      return;
    }
    advance('device', 'model');
  }

  function goBackFromDevice(): void {
    shouldFocusNextStep.current = true;
    setActiveStep('account');
  }

  function resetFlowForActions(): void {
    actionGeneration.current += 1;
    runtimeWrite.current = null;
    setActiveStep('account');
    setCompleted(INITIAL_COMPLETED);
    setFirstRun(null);
    firstRunRef.current = null;
    setRuntimeStale(false);
    setRuntimeWritePending(false);
    setRuntimeWriteUnknown(null);
    setAccountProgress(null);
    setAccountAttempt({ kind: 'idle' });
    setDeviceAttempt({ kind: 'idle' });
    localSaveUnknown.current = false;
    modelSavedRef.current = false;
    setModelSaved(false);
    setModelRestored(false);
    setModelSavePending(false);
    setModelDraft('');
    cancelledSnapshot.current = null;
    replyRun.current += 1;
    replyHistoryRun.current += 1;
    setReplyAttempt(null);
    setReplyAgentUri(null);
    setReplyMessages([]);
    setReplyBusy(false);
    setReplyNotice(null);
  }

  function applyFirstRun(snapshot: AccountSnapshot): void {
    if (resumeFromStatus) {
      const receipt = snapshot.firstRun ?? { state: 'unavailable', revision: 0, confirmed: [], updatedAt: null, skipped: false };
      setFirstRun(receipt);
      firstRunRef.current = receipt;
      if (snapshot.account.status !== 'authenticated') { setActiveStep('account'); return; }
      setCompleted(current => ({ ...current, account: true }));
      if (snapshot.device.status !== 'ready') { setActiveStep('device'); return; }
      setCompleted(current => ({ ...current, device: true }));
      if (receipt.state !== 'none' && receipt.state !== 'recorded') return;
      if (!snapshot.firstRunAttempt) setActiveStep('model');
    }
    if (snapshot.firstRunAttempt) {
      setReplyAttempt(snapshot.firstRunAttempt);
      setReplyAgentUri(snapshot.firstRunAttempt.agentUri);
      if (snapshot.firstRunAttempt.state === 'confirmed') {
        setCompleted((current) => ({ ...current, conversation: true }));
      }
      if (snapshot.firstRunAttempt.state === 'bound') {
        void loadReplyMessages(snapshot.firstRunAttempt);
      }
    } else {
      setReplyAttempt(null);
    }
    if (actions.replyConfirmation) {
      const confirmed = snapshot.firstRunAttempt?.state === 'confirmed';
      setCompleted((current) => current.conversation === confirmed
        ? current
        : { ...current, conversation: confirmed });
    }
    if (snapshot.firstRunAttempt && snapshot.account.status === 'authenticated'
      && snapshot.device.status === 'ready') {
      setCompleted((current) => ({
        ...current, account: true, device: true, model: true,
      }));
      shouldFocusNextStep.current = true;
      setActiveStep('conversation');
      return;
    }
    const receipt = snapshot.firstRun;
    if (!receipt) return;
    setFirstRun(receipt);
    firstRunRef.current = receipt;
    if (snapshot.account.status === 'authenticated' && snapshot.device.status === 'ready') {
      setCompleted((current) => ({ ...current, account: true, device: true }));
    }
    if (actions.configureAgent) return;
    if (
      receipt.state !== 'recorded'
      || snapshot.account.status !== 'authenticated'
      || snapshot.device.status !== 'ready'
    ) return;
    const confirmed = new Set(receipt.confirmed);
    if (confirmed.has('configuration')) {
      setActiveStep(confirmed.has('reply') ? 'conversation' : 'model');
    }
  }

  function applyModelSnapshot(snapshot: SetupSnapshot): void {
    const pendingSave = snapshot.pendingAction === 'save' || localSaveUnknown.current;
    const saved = snapshot.keySaved === true && !pendingSave;
    modelSavedRef.current = saved;
    setModelSaved(saved);
    setModelRestored(saved);
    setModelSavePending(pendingSave);
  }

  function applySetupSnapshot(snapshot: SetupSnapshot): void {
    if (snapshot.agentUri) setReplyAgentUri(snapshot.agentUri);
    if (actions.configureAgent) return;
    const receipt = firstRunRef.current;
    if (receipt?.state !== 'recorded') return;
    const complete = snapshot.keySaved === true && snapshot.pendingAction !== 'save';
    if (complete) {
      setRuntimeStale(false);
      setCompleted((current) => ({ ...current, model: true }));
      setActiveStep('conversation');
      return;
    }
    setRuntimeStale(true);
  }

  function handleModelSavedChange(saved: boolean, restored = false): void {
    setupReadId.current += 1;
    if (saved) {
      localSaveUnknown.current = false;
      setModelSavePending(false);
    }
    modelSavedRef.current = saved;
    setModelSaved(saved);
    setModelRestored(restored);
  }

  function handleModelSaveUnknown(): void {
    setupReadId.current += 1;
    localSaveUnknown.current = true;
    setModelSavePending(true);
  }

  async function checkModelKeyStatus(): Promise<ModelKeyStatusCheck> {
    if (!actions.getSetupState) {
      return { status: 'unavailable', message: '暂无法检查状态，请检查本机服务。' };
    }
    try {
      return modelKeyStatusCheck(await actions.getSetupState());
    } catch {
      return { status: 'unavailable', message: '无法读取 Key 保存状态，请检查本机服务。' };
    }
  }

  function continueFromModel(): void {
    if (!modelSavedRef.current) return;
    advance('model', 'conversation');
  }

  function goBackFromModel(): void {
    shouldFocusNextStep.current = true;
    setActiveStep('device');
  }

  function beginRuntimeWrite(): number | null {
    const active = runtimeWrite.current;
    if (active && active.generation === actionGeneration.current) return null;
    const attempt = ++runtimeAttempt.current;
    runtimeWrite.current = { generation: actionGeneration.current, attempt, kind: 'pending' };
    setRuntimeWritePending(true);
    setRuntimeWriteUnknown(null);
    return attempt;
  }

  function matchesRuntimeWrite(attempt: number): boolean {
    const active = runtimeWrite.current;
    return active !== null
      && active.generation === actionGeneration.current
      && active.attempt === attempt;
  }

  function markRuntimeWriteUnknown(attempt: number, result: RuntimeConfigurationResult): boolean {
    if (!matchesRuntimeWrite(attempt)) return false;
    runtimeWrite.current = {
      generation: actionGeneration.current, attempt, kind: 'unknown',
    };
    setRuntimeWritePending(false);
    setRuntimeWriteUnknown(result);
    return true;
  }

  function finishRuntimeWrite(attempt: number): boolean {
    if (!matchesRuntimeWrite(attempt)) return false;
    runtimeWrite.current = null;
    setRuntimeWritePending(false);
    return true;
  }

  function continueFromRuntimeConfiguration(result?: RuntimeConfigurationResult): void {
    if (result?.actorUri) setReplyAgentUri(result.actorUri);
    setCompleted((current) => ({
      ...current,
      model: true,
      account: current.account || accountAuthenticated,
      device: current.device || deviceReady,
    }));
    shouldFocusNextStep.current = true;
    setActiveStep('conversation');
  }

  async function loadReplyMessages(attempt: FirstRunAttemptView): Promise<void> {
    const reply = actions.replyConfirmation;
    if (!reply || !attempt.conversationId) return;
    const id = ++replyHistoryRun.current;
    const messages = await reply.history(attempt);
    if (id === replyHistoryRun.current
      && replyAttemptRef.current?.attemptId === attempt.attemptId) {
      setReplyMessages(messages);
    }
  }

  function refreshReplyMessages(): void {
    const attempt = replyAttempt;
    if (attempt?.state !== 'bound' || !attempt.conversationId) return;
    void loadReplyMessages(attempt);
  }

  async function runReplyConfirmation(): Promise<void> {
    const reply = actions.replyConfirmation;
    if (!reply || replyBusy) return;
    const id = ++replyRun.current;
    setReplyBusy(true);
    setReplyNotice(null);
    try {
      let attempt = replyAttempt;
      let dispatch = false;
      if (attempt !== null && (attempt.state === 'cancelled' || attempt.state === 'superseded')) {
        attempt = null;
        setReplyAttempt(null);
      }
      if (attempt === null) {
        if (!replyAgentUri) {
          setReplyNotice('没有可确认的 Agent，请先完成运行配置。');
          return;
        }
        const begun = await reply.begin(replyAgentUri);
        if (id !== replyRun.current) return;
        if (begun.status === 'unavailable' && begun.refusal) {
          notifyUnavailable('确认真实回复', begun.refusal);
          setReplyNotice('当前界面无法完成真实回复确认。');
          return;
        }
        if (begun.attempt === undefined) {
          setReplyNotice(begun.message ?? '无法开始真实回复确认；请刷新状态核对。');
          return;
        }
        attempt = begun.attempt;
        dispatch = true;
        setReplyAttempt(attempt);
        if (attempt.state === 'bound' && attempt.requestMessageId) {
          setReplyNotice('确认消息已提交；请等待真实回复后点击确认。');
          return;
        }
      }
      if (attempt.state !== 'pending' && attempt.state !== 'bound') {
        setReplyNotice('当前确认流程不能发送消息。');
        return;
      }
      const sent = await reply.send(attempt, FIRST_RUN_REPLY_MESSAGE, { dispatch });
      if (id !== replyRun.current) return;
      if (sent.attempt) setReplyAttempt(sent.attempt);
      if (sent.status === 'bound') {
        if (sent.attempt) void loadReplyMessages(sent.attempt);
        setReplyNotice('确认消息已提交；请等待该消息的真实回复，然后点击确认。');
        return;
      }
      if (sent.status === 'unavailable' && sent.refusal) {
        notifyUnavailable('确认真实回复', sent.refusal);
      }
      setReplyNotice(sent.message ?? '确认消息未发送。');
    } catch {
      if (id === replyRun.current) setReplyNotice('确认流程结果未知；不会自动重试，请刷新状态核对。');
    } finally {
      if (id === replyRun.current) setReplyBusy(false);
    }
  }

  async function confirmReply(): Promise<void> {
    const reply = actions.replyConfirmation;
    const attempt = replyAttempt;
    if (!reply || attempt === null || replyBusy) return;
    if (attempt.state !== 'bound' && attempt.state !== 'confirmed') {
      setReplyNotice('请先发送确认消息并等待真实回复。');
      return;
    }
    const id = ++replyRun.current;
    setReplyBusy(true);
    setReplyNotice(null);
    try {
      const result: ReplyAttemptResult = await reply.confirm(
        attempt, attempt.receiptRevision,
      );
      if (id !== replyRun.current) return;
      if (result.attempt) setReplyAttempt(result.attempt);
      if (result.firstRun) {
        setFirstRun(result.firstRun);
        firstRunRef.current = result.firstRun;
      }
      if (result.status === 'confirmed' && result.attempt?.state === 'confirmed') {
        setCompleted((current) => ({ ...current, conversation: true }));
        setReplyNotice('已确认真实回复，首次设置完成。');
        void loadReplyMessages(result.attempt);
        return;
      }
      if (result.status === 'unavailable' && result.refusal) {
        notifyUnavailable('确认真实回复', result.refusal);
      }
      setReplyNotice(result.message ?? '确认失败；不会当作已完成。');
    } catch {
      if (id === replyRun.current) setReplyNotice('确认结果未知；不会当作已完成，请刷新状态核对。');
    } finally {
      if (id === replyRun.current) setReplyBusy(false);
    }
  }

  async function cancelReplyConfirmation(): Promise<void> {
    const reply = actions.replyConfirmation;
    const attempt = replyAttempt;
    if (!reply || attempt === null || replyBusy) return;
    const id = ++replyRun.current;
    setReplyBusy(true);
    setReplyNotice(null);
    try {
      const result = await reply.cancel(attempt);
      if (id !== replyRun.current) return;
      if (result.status === 'cancelled' && result.attempt) {
        setReplyAttempt(result.attempt);
        setReplyNotice('已取消本次确认，可以重新开始。');
        return;
      }
      setReplyNotice(result.message ?? '取消结果未知；请刷新状态核对。');
    } catch {
      if (id === replyRun.current) setReplyNotice('取消结果未知；请刷新状态核对。');
    } finally {
      if (id === replyRun.current) setReplyBusy(false);
    }
  }

  function exit(): void {
    closeBlankAuthorizationPopup();
    accountAttemptId.current += 1;
    lastAccountRequest.current = false;
    deviceAttemptId.current += 1;
    if (accountRunning.current || deviceRunning.current) void actions.cancelAccount?.().catch(() => {});
    accountRunning.current = false;
    deviceRunning.current = false;
    setupReadId.current += 1;
    onExit?.();
  }

  async function skipFirstRun(): Promise<void> {
    if (!actions.skipFirstRun || skipPending) return;
    setSkipPending(true);
    setSkipNotice(null);
    try {
      const result = await actions.skipFirstRun(firstRunRef.current?.revision ?? firstRun?.revision ?? 0);
      if (result.firstRun) {
        setFirstRun(result.firstRun);
        firstRunRef.current = result.firstRun;
      }
      if (result.status === 'skipped' && result.firstRun?.skipped === true) {
        setCompleted({ account: true, device: true, model: true, conversation: true });
        onComplete?.();
        return;
      }
      if (result.status === 'unavailable' && result.refusal) {
        notifyUnavailable('跳过首次设置', result.refusal);
      }
      setSkipNotice(result.message ?? '跳过结果未确认，请刷新状态核对。');
    } catch {
      setSkipNotice('跳过结果未知，请刷新状态核对。');
    } finally {
      setSkipPending(false);
    }
  }

  return {
    scenario, actions, activeStep, completed, firstRun, skipPending, skipNotice,
    runtimeStale, runtimeWritePending, runtimeWriteUnknown,
    accountProgress, accountAttempt, deviceAttempt,
    authorizing: accountRunning.current, canRetryAccount: lastAccountRequest.current,
    modelSaved, modelRestored, modelSavePending, modelDraft,
    shouldFocusNextStep,
    stepStatuses: { account: accountStepStatus, device: deviceStepStatus, model: modelStepStatus,
      conversation: conversationStepStatus },
    runAccount, cancelAccount, retryAccount, runDevice, continueFromAccount,
    continueFromDevice, goBackFromDevice,
    handleModelSavedChange, handleModelSaveUnknown, checkModelKeyStatus, setModelDraft, continueFromModel,
    goBackFromModel, exit,
    skipFirstRun,
    continueFromRuntimeConfiguration,
    beginRuntimeWrite, markRuntimeWriteUnknown, finishRuntimeWrite,
    replyAttempt, replyAgentUri, replyMessages, replyBusy, replyNotice,
    runReplyConfirmation, refreshReplyMessages, confirmReply, cancelReplyConfirmation,
  };
}
