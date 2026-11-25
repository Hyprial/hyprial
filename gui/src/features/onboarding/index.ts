/**
 * Decision record (GUI deep modules B4, 2026-11-08):
 * - Entry semantics are unchanged from the pre-impl layout; implementation
 *   files moved under impl/ and this entry keeps re-exporting the same names.
 * - devicePresentation / networkPresentation joined the public surface because
 *   SettingsPage consumes them through the entry instead of impl paths.
 */

export { unknownModelKeySaveResult, modelKeyStatusCheck } from './impl/model-save-result';
export { FirstRunOnboarding } from './impl/FirstRunOnboarding';
export { useFirstRunConversation } from './impl/useFirstRunConversation';
export type { AccountCredentialValues } from '../../components/forms';
export type {
  SetupSnapshot,
  ModelKeySaveResult,
  ModelKeyStatusCheck,
  AccountActionResult,
  AccountLogoutResult,
  AccountProgress,
  AccountSnapshot,
  AccountEntryMode,
  AttemptState,
  ChatStatus,
  DeviceActionResult,
  FirstRunActions,
  FirstRunAttemptState,
  FirstRunAttemptView,
  FirstRunOnboardingProps,
  FirstRunConversationRuntime,
  FirstRunReceiptState,
  FirstRunReceiptView,
  FirstRunSkipResult,
  FirstRunReplyActions,
  FirstRunScenario,
  FirstRunStepId,
  ReplyAttemptResult,
  ReplyMessageView,
  RuntimeConfigurationResult,
  RuntimeConfigurationStage,
  StepViewState,
} from './impl/types';

export { createAccountActions } from './impl/auth-actions';
export { createFirstRunReplyActions, FIRST_RUN_REPLY_MESSAGE } from './impl/reply-actions';
export { attemptView as firstRunAttemptView } from './impl/reply-actions';
export { firstRunWizardOffered } from './impl/wizard-ports';
export { safeAuthorizationUrl } from './impl/authorization-url';
export { AccountProgressPanel } from './impl/AccountProgressPanel';
export { accountPresentation, devicePresentation, networkPresentation } from './impl/presentation';
