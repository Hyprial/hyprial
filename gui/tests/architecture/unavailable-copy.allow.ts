/*
 * Decision record (unavailable copy to notifications, PR 1f/F0, 2026-11-14):
 * - STATE: runtime state or error text of a request that was actually made, a
 *   label naming a scenario, or a legitimate label that merely matches the
 *   vocabulary (for example a 暂不 button); it stays on the page.
 * - NOTIFICATION: text that reaches the user through the notification
 *   mechanism (toast and center, push-notice) as the result of a user
 *   operation, such as the template of components/notifications.tsx or the
 *   logout notice. Text that a page renders inline (static note, form or
 *   section message, dialog result, returned view state) is not an
 *   operation result in this sense and is PENDING_* until the feature-area PR
 *   raises it through the mechanism.
 * - STATE entries for the first-run wizard (the AccountStep unavailable notice
 *   titles and the presentation.ts account and device service lines) are tied
 *   to real *_UNAVAILABLE answers of operations the user performed; the
 *   no-port fallback factory no longer exists (PR 1f/F6).
 * - ELSEWHERE: removed by another PR; an entry here may be absent without
 *   failing. The daemon-request PR deleted adapters/stub.ts and its entry.
 * - LOGIC_NOT_COPY: a vocabulary match that is program logic, not text shown
 *   to anyone (state/appReducer.ts tests a notice text for 暂不可用).
 * - PENDING_*: the buckets of static copy that the rule removes. Every one of
 *   them is empty (PR 1f/F6, 2026-11-15) and stays empty: a new hit is STATE
 *   or NOTIFICATION with a reason, or it is not written. The PENDING_* exports
 *   stay because the vitest gate and scripts/ci/check_gui_unavailable_copy.mjs
 *   read them; PENDING_DISABLED and PENDING_COMPONENTS count always-disabled
 *   controls and placeholder components per file.
 * - What enforces shrinking: scripts/ci/check_gui_unavailable_copy.mjs
 *   compares this file with the one at the base branch in CI and fails when
 *   any bucket gains an entry or a count (a string removed from a PENDING_*
 *   map may be re-bucketed). The vitest gate next to this file enforces only
 *   that the maps match the scan of gui/src exactly (no unregistered, stale
 *   or duplicate entry); on its own it cannot stop an entry being added.
 */
type CopyMap = Readonly<Record<string, readonly string[]>>;
type CountMap = Readonly<Record<string, number>>;

export const STATE: CopyMap = {
  "app/backend/connection.ts": [
    "is unavailable in the pinned catalog",
  ],
  "app/backend/session-watch.ts": [
    "the view session is unavailable",
  ],
  "client/transport.ts": [
    "catalog unavailable",
    "unsupported envelope schemaVersion",
  ],
  "components/primitives/Card.tsx": [
    "未提供",
  ],
  "dev/DevScenarioSelector.tsx": [
    "模型不可用",
  ],
  "dev/mock/agent-provider.ts": [
    "Synthetic login example is unavailable",
  ],
  "dev/org/scenarios.ts": [
    "数据源未接通",
  ],
  "dev/SessionToolbar.tsx": [
    "切换通信为不可用",
  ],
  "dev/topology-scenarios.ts": [
    "M-16 通信数据不可用",
  ],
  "features/account/impl/useAccountSettings.ts": [
    "账号服务不可用，请检查本机服务后刷新状态。",
  ],
  "features/agent-chat/impl/AgentTargetField.tsx": [
    "聊天对象目录不可用。",
  ],
  "features/agent-chat/impl/api/chat-controller.ts": [
    "消息已被服务接收，正在确认本机恢复记录，暂不能发送下一条。",
  ],
  "features/agent-chat/impl/ChatComposer.tsx": [
    "发送结果未知，请等待确认，暂不能重新发送。",
  ],
  "features/agent-provider/impl/login-files.ts": [
    "Credential file size is unavailable.",
  ],
  "features/agents/impl/agent-extensions.ts": [
    "状态不可用",
  ],
  "features/agents/impl/api/agent-writes.ts": [
    "托管转移不可用，停止未完成。",
    "本机不支持该 provider 的原生目录形态，启动未执行。",
    "生命周期管理器不可用，未执行停止。",
    "该 Agent 生命周期形态不支持此停止操作。",
  ],
  "features/agents/impl/api/index.ts": [
    "未提供",
  ],
  "features/chat-channels/impl/daemon-api/chat-send.ts": [
    "不支持向该目标发送，本次未发送。",
  ],
  "features/chat-channels/impl/MessageSessionsSection.tsx": [
    "可用性：不可用",
    "未提供错误信息",
  ],
  "features/onboarding/impl/AccountStep.tsx": [
    "账号服务不可用",
  ],
  "features/onboarding/impl/ConversationStep.tsx": [
    "无法确认聊天服务状态，暂不能发送。",
  ],
  "features/onboarding/impl/presentation.ts": [
    "不可用 · 未初始化",
    "暂未取得登录结果",
    "设备初始化不可用，可重试。",
    "设备未就绪，暂不能继续。",
    "账号服务不可用",
    "账号服务不可用，可重试。",
  ],
  "features/org/impl/dialogs/CreateOrgDialog.tsx": [
    "暂不",
  ],
  "features/org/impl/dialogs/FileDialogs.tsx": [
    "文件内容没有读回，暂不能编辑或保存；请刷新后重试。",
  ],
  "features/org/impl/dialogs/MigrationPlanView.tsx": [
    "原因未提供",
  ],
  "features/org/impl/domain/copy.ts": [
    "组织服务暂时不可用。",
    "组织身份服务不可用。",
    "该空间的历史记录异常，写操作和历史暂不可用。",
  ],
  "features/org/impl/sections/FilesSection.tsx": [
    "文件列表读取失败，暂不能判断是否为空；请刷新后重试。",
    "该版本内容读取失败，正文不可用。",
  ],
  "features/org/impl/workspace/OrgWorkspace.tsx": [
    "暂不设置",
  ],
  "features/settings/impl/logs/LogsSection.tsx": [
    "日志目录不可用",
  ],
  "features/settings/impl/UpgradeStatusSection.tsx": [
    "升级状态暂未就绪",
  ],
  "features/settings/impl/useLocalManagement.ts": [
    "服务正在恢复，暂未执行状态查询。请稍后手动刷新；尚未确认服务就绪。",
    "服务状态查询暂未就绪，本次查询未执行。请稍后手动刷新。",
  ],
};

export const NOTIFICATION: CopyMap = {
  "components/notifications.tsx": [
    "暂不可用。",
  ],
  "state/appReducer.ts": [
    "已清空当前会话。账号登出暂不可用。",
  ],
};

export const ELSEWHERE: CopyMap = {
};

export const LOGIC_NOT_COPY: CopyMap = {
  "state/appReducer.ts": [
    "暂不可用",
  ],
};

export const PENDING_SHELL: CopyMap = {
};

export const PENDING_AGENTS: CopyMap = {
};

export const PENDING_ORG: CopyMap = {};

export const PENDING_CHAT: CopyMap = {};

export const PENDING_CONNECTIONS: CopyMap = {};

export const PENDING_SETTINGS: CopyMap = {
};

export const PENDING_REMAINDER: CopyMap = {};

export const PENDING_DISABLED: CountMap = {};

export const PENDING_COMPONENTS: CountMap = {};
