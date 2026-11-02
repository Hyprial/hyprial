import { useState } from 'react';
import { Notice } from '../../components/primitives';
import { ChatPage } from '../../pages/ChatPage';
import { appReducer, createFixtureState } from '../../state/appReducer';
import type { AppState } from '../../state/types';

// Deliberately in-memory: ChatPage only dispatches into this private reducer.
function initialState(text: string): AppState {
  const base = createFixtureState();
  return {
    ...base, page: 'chat', infoCollapsed: true, connections: [],
    targets: [{ id: 'agent-squire', kind: 'agent', displayName: 'Squire', role: '会话界面预览',
      availability: 'needs-connection', avatarTone: 'green', modelLabel: '模型可用性未验证', capabilities: [] }],
    activeTargetId: 'agent-squire', drafts: {},
    threads: { 'agent-squire': [{ id: 'preview-first-message', targetId: 'agent-squire', author: 'user', text, delivery: 'not-sent' }] },
  };
}

/** Reuses the shared chat page with private state, without a real chat capability. */
export function ConversationHandoffPreview({ text }: { readonly text: string }) {
  const [state, setState] = useState(() => initialState(text));
  return <div data-conversation-preview="true">
    <Notice tone="info" title="会话界面模拟">
      下方文字只保留在当前页面内存中，尚未发送。没有生成 Squire 回复，首次设置未标记完成；刷新后此文字会清除。这里的不可发送／需要连接提示属于模拟对象，不代表实际 Key 丢失或 Squire 停止。
    </Notice>
    <ChatPage state={state} dispatch={(action) => setState(current => appReducer(current, action))} />
    {state.notice && <Notice tone="warning" title="仅供预览">此操作未接线，没有执行任何操作。</Notice>}
  </div>;
}
