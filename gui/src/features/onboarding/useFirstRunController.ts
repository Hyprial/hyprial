import { useEffect, useMemo, useRef, useState } from 'react';
import type { StepStatus } from '../../components/primitives';
import { safeAuthorizationUrl } from './authorization-url';
import { createDemoActions, createUnavailableActions } from './actions';
import type {
  AccountActionResult, AccountProgress, AccountSnapshot, AttemptState, DeviceActionResult,
  FirstRunOnboardingProps, FirstRunStepId, SquirePreparationResult,
  SquirePreparationState, SetupSnapshot,
} from './types';

const INITIAL_COMPLETED: Readonly<Record<FirstRunStepId, boolean>> = {
  account: false,
  model: false,
  squire: false,
  conversation: false,
};

function findResult<T>(
  attempt: AttemptState<T>,
): T | null {
  return attempt.kind === 'settled' ? attempt.result : null;
}

/**
 * Owns first-run orchestration and late-result/cancellation guards.
 * Consumes injected capabilities only; it stores public readiness metadata,
 * never a model key. Views own layout, input values and DOM focus.
 */
export function useFirstRunController({
  scenario: requestedScenario = 'live', actions: providedActions,
  chatStatus = 'unavailable', conversationPreview, onExit,
}: FirstRunOnboardingProps) {
  const scenario = requestedScenario;
  const defaultUnavailable =
    providedActions === undefined && scenario === 'live';
  const actions = useMemo(
    () =>
      providedActions ??
      (scenario === 'demo'
        ? createDemoActions()
        : createUnavailableActions()),
    [providedActions, scenario],
  );

  const [activeStep, setActiveStep] = useState<FirstRunStepId>('account');
  const [completed, setCompleted] = useState(INITIAL_COMPLETED);
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
  const [preparation, setPreparation] = useState<AttemptState<SquirePreparationResult>>({ kind: 'idle' });
  const [preparationStages, setPreparationStages] = useState<SquirePreparationState>({ created: false, granted: false, started: false });
  const preparationId = useRef(0);
  const preparationRunning = useRef(false);
  const setupReadId = useRef(0);

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
    const attemptId = ++accountAttemptId.current;
    let disposed = false;
    const setupId = ++setupReadId.current;
    const restoreSetup = async () => {
      if (!actions.getSetupState || disposed || setupId !== setupReadId.current) return;
      try {
        const snapshot = await actions.getSetupState();
        if (disposed || setupId !== setupReadId.current) return;
        modelSavedRef.current = snapshot.keySaved === true;
        setModelSaved(snapshot.keySaved === true);
        setModelRestored(snapshot.keySaved === true);
        applySetupSnapshot(snapshot);
      } catch { /* Unknown state keeps the key gate closed; it never starts setup. */ }
    };
    const unsubscribe = actions.subscribeAccountProgress?.((progress) => {
      if (!disposed && accountRunning.current) {
        setAccountProgress(progress);
        const url = safeAuthorizationUrl(progress.verificationUri);
        const popup = authorizationPopup.current;
        if (url && popup) {
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
        void restoreSetup();
      }).catch(() => {
        if (!disposed && attemptId === accountAttemptId.current) {
          setAccountAttempt({ kind: 'settled', result: { status: 'unknown', message: '无法读取当前登录状态，请重试登录。' } });
        }
      });
    } else { void restoreSetup(); }
    return () => {
      setupReadId.current += 1;
      disposed = true;
      closeBlankAuthorizationPopup();
      accountAttemptId.current += 1;
      deviceAttemptId.current += 1;
      lastAccountRequest.current = false;
      unsubscribe?.();
      if (accountRunning.current || deviceRunning.current) void actions.cancelAccount?.().catch(() => {});
      accountRunning.current = false;
      deviceRunning.current = false;
      preparationId.current += 1;
      if (preparationRunning.current) void actions.cancelPreparation?.().catch(() => {});
      preparationRunning.current = false;
    };
  }, [actions]);

  const accountResult = findResult(accountAttempt);
  const deviceResult = findResult(deviceAttempt);
  const preparationResult = findResult(preparation);
  const accountAuthenticated = accountResult?.status === 'authenticated';
  const deviceReady = deviceResult?.status === 'ready';
  const squireStarted = preparationResult?.status === 'started' && preparationStages.created && (preparationStages.granted || preparationStages.grantRecorded) && preparationStages.started;

  const accountStepStatus: StepStatus = completed.account
    ? 'completed'
    : activeStep !== 'account'
      ? 'pending'
      : accountAttempt.kind === 'running' || deviceAttempt.kind === 'running'
        ? 'active'
        : accountResult?.status === 'failed' ||
            deviceResult?.status === 'failed'
          ? 'failed'
          : accountResult?.status === 'unknown' ||
                accountResult?.status === 'unavailable' ||
                deviceResult?.status === 'unknown' ||
                deviceResult?.status === 'unavailable'
            ? 'blocked'
            : 'active';

  const modelStepStatus: StepStatus = completed.model ? 'completed' : activeStep === 'model' ? 'active' : 'pending';
  const squireStepStatus: StepStatus = completed.squire ? 'completed'
    : activeStep !== 'squire' ? 'pending'
    : preparationResult?.status === 'failed' ? 'failed'
    : preparationResult?.status === 'unknown' || preparationResult?.status === 'unavailable' ? 'blocked' : 'active';

  const conversationStepStatus: StepStatus =
    activeStep === 'conversation' && conversationPreview ? 'active'
    : activeStep === 'conversation' && chatStatus === 'completed'
      ? 'completed'
      : activeStep !== 'conversation'
        ? 'pending'
        : chatStatus === 'unavailable'
          ? 'blocked'
          : 'active';

  function advance(from: FirstRunStepId, to: FirstRunStepId): void {
    setCompleted((current) => ({ ...current, [from]: true }));
    shouldFocusNextStep.current = true;
    setActiveStep(to);
  }

  async function runAccount(): Promise<void> {
    if (accountRunning.current) return;
    const attemptId = ++accountAttemptId.current;
    const previousAccount = findResult(accountAttempt);
    const previousDevice = findResult(deviceAttempt);
    lastAccountRequest.current = true;
    accountRunning.current = true;
    setAccountProgress(null);
    setAccountAttempt({ kind: 'running' });
    // Reserve only a new window during the user gesture; blocked popups fall back to the link.
    if (scenario === 'live' && actions.subscribeAccountProgress) {
      closeBlankAuthorizationPopup();
      try {
        const popup = window.open('about:blank', '_blank');
        if (popup) popup.opener = null;
        authorizationPopup.current = popup;
      } catch { /* The public progress link is the fallback. */ }
    }
    try {
      // The legacy callback shape is retained for callers; no credentials are collected.
      const result = await actions.authenticateAccount({ account: '', token: '' }, 'login');
      if (accountAttemptId.current !== attemptId) return;
      setAccountAttempt({ kind: 'settled', result });
      if (result.status === 'authenticated') {
        lastAccountRequest.current = false;
        if (actions.getAccountState) {
          try {
            const snapshot = await actions.getAccountState();
            if (accountAttemptId.current !== attemptId) return;
            setAccountAttempt({ kind: 'settled', result: snapshot.account });
            setDeviceAttempt({ kind: 'settled', result: snapshot.device });
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
      }
    } catch {
      if (accountAttemptId.current !== attemptId) return;
      setAccountAttempt({ kind: 'settled', result:
        previousAccount?.status === 'authenticated' || previousAccount?.status === 'unauthenticated'
          ? previousAccount
          : { status: 'unknown', message: '登录结果未知：无法联系账号服务。' },
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
          : { status: 'unknown', message: '设备操作已取消，状态未确认。' },
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
        setAccountAttempt({ kind: 'settled', result: snapshot.account });
        setDeviceAttempt({ kind: 'settled', result: snapshot.device });
        accountRunning.current = false;
        return true;
      }
      lastAccountRequest.current = true;
      setAccountAttempt({ kind: 'settled', result: {
        status: 'unknown', message: '取消后状态未确认，请重试确认取消；确认前不会再次登录。',
      } });
      return false;
    } catch {
      if (attemptId === accountAttemptId.current) {
        lastAccountRequest.current = true;
        setAccountAttempt({ kind: 'settled', result: {
          status: 'unknown', message: '取消后状态未确认，请重试确认取消；确认前不会再次登录。',
        } });
      }
      return false;
    }
  }

  function retryAccount(): void {
    void (async () => {
      if (accountRunning.current && !await cancelAccount()) return;
      await runAccount();
    })();
  }

  async function runDevice(): Promise<void> {
    const attemptId = ++deviceAttemptId.current;
    const previousDevice = findResult(deviceAttempt);
    deviceRunning.current = true;
    setDeviceAttempt({ kind: 'running' });
    try {
      const result = await actions.initializeDevice();
      if (attemptId !== deviceAttemptId.current) return;
      setDeviceAttempt({ kind: 'settled', result });
      if (actions.getAccountState) {
        try {
          const snapshot = await actions.getAccountState();
          if (attemptId !== deviceAttemptId.current) return;
          setAccountAttempt({ kind: 'settled', result: snapshot.account });
          setDeviceAttempt({ kind: 'settled', result: snapshot.device });
        } catch { /* Keep the explicit device result; it is not upgraded to ready. */ }
      }
    } catch {
      if (attemptId === deviceAttemptId.current) setDeviceAttempt({ kind: 'settled', result:
        previousDevice?.status === 'ready'
          ? previousDevice
          : { status: 'unknown', message: '设备初始化结果未知：无法联系设备服务。' },
      });
    } finally {
      if (attemptId === deviceAttemptId.current) deviceRunning.current = false;
    }
  }

  function continueFromAccount(): void {
    if (!accountAuthenticated || !deviceReady) {
      return;
    }
    accountAttemptId.current += 1;
    lastAccountRequest.current = false;
    advance('account', 'model');
  }

  function applySetupSnapshot(snapshot: SetupSnapshot): void {
    const stages = { created: snapshot.created, granted: snapshot.granted, grantRecorded: snapshot.grantRecorded, started: snapshot.started };
    setPreparationStages(stages);
    if (snapshot.created && (snapshot.granted || snapshot.grantRecorded) && snapshot.started && snapshot.compatible && !snapshot.pendingAction) {
      setPreparation({ kind: 'settled', result: { ...stages, status: 'started', message: '已恢复本机准备状态，仍需真实回复确认模型可用。' } });
    } else if (!snapshot.resumeAvailable && (snapshot.created || snapshot.pendingAction)) {
      setPreparation({ kind: 'settled', result: { ...stages, status: 'unknown', message: '已有 Squire 或上次操作需要核实。保留现有状态，不会自动覆盖、重复授权或重启。' } });
    }
  }

  function handleModelSavedChange(saved: boolean, restored = false): void {
    setupReadId.current += 1;
    modelSavedRef.current = saved;
    setModelSaved(saved);
    setModelRestored(restored);
  }

  function continueFromModel(): void {
    if (!modelSavedRef.current || preparationRunning.current) return;
    advance('model', 'squire');
    void runPreparation();
  }

  async function runPreparation(): Promise<void> {
    if (preparationRunning.current) return;
    const id = ++preparationId.current;
    setupReadId.current += 1;
    preparationRunning.current = true;
    setPreparation({ kind: 'running' });
    try {
      if (actions.getSetupState) {
        const snapshot = await actions.getSetupState();
        if (preparationId.current !== id) return;
        applySetupSnapshot(snapshot);
        const complete = snapshot.created && (snapshot.granted || snapshot.grantRecorded) && snapshot.started && snapshot.compatible && !snapshot.pendingAction;
        if (complete) return;
        if (!snapshot.resumeAvailable) {
          setPreparation({ kind: 'settled', result: { status: 'unknown', created: snapshot.created, granted: snapshot.granted, grantRecorded: snapshot.grantRecorded, started: snapshot.started,
            message: '上次操作或已有配置尚未核实，已保留当前状态。请先检查本机状态后重试，不会自动重复修改。' } });
          return;
        }
      }
      const result: SquirePreparationResult = actions.prepareSquire ? await actions.prepareSquire((event) => {
        if (preparationId.current !== id) return;
        setPreparationStages(current => ({ ...current, [event.stage]: true }));
      }) : { status: 'unavailable', created: false, granted: false, started: false, message: 'Squire 准备入口尚未接入，未创建或启动进程。' };
      if (preparationId.current !== id) return;
      setPreparationStages({ created: result.created, granted: result.granted, grantRecorded: result.grantRecorded, started: result.started });
      setPreparation({ kind: 'settled', result });
    } catch {
      if (preparationId.current !== id) return;
      setPreparation({ kind: 'settled', result: {
        status: 'unknown', created: false, granted: false, started: false,
        message: 'Squire 准备结果未知，请重试检查；未确认的步骤不会显示成功。',
      } });
    } finally {
      if (preparationId.current === id) preparationRunning.current = false;
    }
  }

  function continueFromSquire(): void {
    if (!squireStarted) {
      return;
    }
    advance('squire', 'conversation');
  }

  function exit(): void {
    closeBlankAuthorizationPopup();
    accountAttemptId.current += 1;
    lastAccountRequest.current = false;
    deviceAttemptId.current += 1;
    if (accountRunning.current || deviceRunning.current) void actions.cancelAccount?.().catch(() => {});
    accountRunning.current = false;
    deviceRunning.current = false;
    preparationId.current += 1;
    setupReadId.current += 1;
    if (preparationRunning.current) void actions.cancelPreparation?.().catch(() => {});
    preparationRunning.current = false;
    onExit?.();
  }

  return {
    scenario, defaultUnavailable, actions, activeStep, completed,
    accountProgress, accountAttempt, deviceAttempt,
    authorizing: accountRunning.current, canRetryAccount: lastAccountRequest.current,
    modelSaved, modelRestored, preparation, preparationStages,
    shouldFocusNextStep,
    stepStatuses: { account: accountStepStatus, model: modelStepStatus,
      squire: squireStepStatus, conversation: conversationStepStatus },
    runAccount, cancelAccount, retryAccount, runDevice, continueFromAccount,
    handleModelSavedChange, continueFromModel, runPreparation, continueFromSquire, exit,
  };
}
