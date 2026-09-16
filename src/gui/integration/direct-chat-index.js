// A direct chat belongs to a human owner and an exact four-part Agent URI.
// Session/carrier IDs and display labels never participate in target identity.
export function chatKeyOwner(key) {
  const actor = String(key).split('\n')[0].split(':');
  return actor.length === 4 && actor[0] === 'agent' ? actor[1] : '';
}
export function chatKeySession(key) {
  const ref = String(key).split('\n')[1] || '';
  return ref.startsWith('dsh-web:') ? ref.slice(8) : '';
}
export function chatWorkSessions(chat) {
  return [...new Set([...(Array.isArray(chat?.workSessionIds) ? chat.workSessionIds : []), chat?.workSessionId]
    .filter(id => typeof id === 'string' && id.length > 0 && id.length <= 4096))];
}
export function mergeChatMessages(chats) {
  const messages = new Map();
  for (const chat of chats) for (const message of chat.messages || []) {
    if (message && typeof message.id === 'string' && !messages.has(message.id)) messages.set(message.id, message);
  }
  return [...messages.values()].sort((a, b) => Number(a.time || 0) - Number(b.time || 0));
}
export function consolidateDirectChats(ledger, owner) {
  ledger.humanChatAliases ||= {};
  ledger.humanChatArchive ||= {};
  const groups = new Map();
  for (const [key, chat] of Object.entries(ledger.humanChats)) {
    if (chatKeyOwner(key) !== owner || !chatKeySession(key) || !/^agent:[^\s:]+:[^\s:]+:[^\s:]+$/.test(chat?.target || '')) continue;
    const group = groups.get(chat.target) || [];
    group.push([key, chat]);
    groups.set(chat.target, group);
  }
  let changed = false;
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    group.sort(([ak, a], [bk, b]) => (a.createdAt || Number.MAX_SAFE_INTEGER) - (b.createdAt || Number.MAX_SAFE_INTEGER) || ak.localeCompare(bk));
    const [canonicalKey, canonical] = group[0];
    // Keep every original snapshot, including messages outside the 100-item
    // working window. Aliases prevent old browser caches resurrecting copies.
    for (const [key, chat] of group) ledger.humanChatArchive[key] ||= structuredClone(chat);
    const workSessionIds = [...new Set(group.flatMap(([, chat]) => chatWorkSessions(chat)))];
    ledger.humanChats[canonicalKey] = {
      ...canonical, messages: mergeChatMessages(group.map(([, chat]) => chat)).slice(-100),
      workSessionIds, workSessionId: workSessionIds[0] || '',
      lastOpenedAt: Math.max(...group.map(([, chat]) => Number(chat.lastOpenedAt || 0)))
    };
    for (const [key] of group.slice(1)) {
      for (const [alias, dest] of Object.entries(ledger.humanChatAliases)) if (dest === key) ledger.humanChatAliases[alias] = canonicalKey;
      ledger.humanChatAliases[key] = canonicalKey;
      delete ledger.humanChats[key];
    }
    changed = true;
  }
  return changed;
}
export function directChatIndex(ledger, owner) {
  return Object.entries(ledger.humanChats).filter(([key]) => chatKeyOwner(key) === owner && chatKeySession(key)).map(([key, binding]) => ({
    sessionId: chatKeySession(key), binding,
    mergedSessionIds: Object.entries(ledger.humanChatAliases || {}).filter(([, dest]) => dest === key).map(([alias]) => chatKeySession(alias))
  }));
}
