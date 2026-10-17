// Public face of the browser App Core.
//
// Core is a static, dependency-free ESM module: sessions, history, progress and
// terminal state are projected from one injected connection port, identity is
// bound by the native/server composition root outside this module, and no
// daemon, runtime, harness or local process authority lives here. The real
// tailnet binding, node identity check (N-A2) and app adapter are NOT
// implemented in this slice.
export { CORE_TRANSPORT_KINDS } from './connection.mjs';
export { createChatCore } from './chat.mjs';
export { createDemoConnection } from './demo.mjs';
export { mountChat, startBrowserChat } from './browser.mjs';
