# Chat channels GUI v2 follow-up plan

Base: `feat/gui-chat-channels` at `36d12dda6`; target: a stacked PR against that branch while #2279 remains open.

Delivery is split into reviewable PRs, as authorized by the owner. The first v2 PR covers step 4 and the adapter error status. The planned step 1 lookup switch was withdrawn after independent review: the HTTP lookup is bound to `user:<owner>`, while the existing live host can send as a fenced agent. The next message PR must prove that the send and lookup capabilities share the same sender and session authority before switching reconciliation. Message write migration in steps 2 and 3 remains a separate method review because those handlers carry sender fencing, dispatch, consumption and receipt semantics. An external Feishu user authorization method and the agents authority fixture are not yet present in this base.

1. Stabilize the existing read slice: wire `message.send.lookup` into the existing reconciliation path, retain drafts and unknown state, and cover the real generated client contract.
2. Migrate the existing message send/reply/read/ack handlers through MethodSpec and Registry without changing their IPC authority or widening browser identity. Declare write methods as mutating, never retry, and use H-03; keep consuming reads mutating. Generate OpenAPI and client from the declarations.
3. Connect the chat workspace to generated message methods through a trusted host capability. Keep an explicit unavailable state where the host cannot provide the session and target authority. Consume the agents fixture when available.
4. Expose adapter start/stop/reload only through their real lifecycle handlers and H-03. Keep Feishu authorization unavailable until a real daemon method exists.
5. For each behavior, make a focused test fail before implementation, then run focused tests, generation checks, GUI verify, Python checks, structural/module gates and diff check. Record isolated daemon/HTTP/IPC and external authorization as PASS, UNKNOWN or BLOCKED. Ask codex-daemon-request for independent method review before merge.

No new business REST routes, database access, fake handlers or automatic mutation retries. C01/C02 stay IPC-only.
