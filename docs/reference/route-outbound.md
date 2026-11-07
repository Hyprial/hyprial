# Proactive route outbound: `route:<adapter>:<route>`

`message.send` accepts `route:<adapter>:<route>` targets. The daemon resolves
the adapter against the local `channels.json` gateway list and the route
against that gateway's configured routes, then posts the text to the route's
`nativeId` (a Lark chat id) with `receive_id_type=chat_id`. Fanout routes
expand to their direct members; each member is its own delivery entry with
its own derived idempotency key and provider uuid.

Resolution is sender-local configuration only: no zenoh hop, no receiver
profile. `route:` reaches a *conversation space*; `user:<owner>` reaches a
*person* through the receiver's profile. The two resolution domains do not
overlap, and a route that happens to point at a DM chat is an explicit
choice of which app speaks.

Permission is delegated to the platform: an app can post exactly to the
chats it belongs to. Every failure names its cause:

| condition | code |
| --- | --- |
| adapter not in `channels.json` | `ROUTE_ADAPTER_UNCONFIGURED` (lists configured adapters) |
| route not under that adapter | `ROUTE_NOT_CONFIGURED` (lists configured routes) |
| fanout member not a direct route | `ROUTE_FANOUT_MEMBER_INVALID` |
| Lark 230002, app not in the chat | `ROUTE_APP_NOT_IN_CHAT` (names adapter, route, chat_id, remedy) |
| Lark 230006, bot ability disabled | `ROUTE_APP_BOT_ABILITY_DISABLED` |
| Lark 232011, operator not in chat | `ROUTE_OPERATOR_NOT_IN_CHAT` |
| Lark 230027 with structured missing scopes | `ROUTE_APP_SCOPE_REQUIRED` (returns the official administrator link) |
| any other Lark rejection | `ROUTE_SEND_FAILED` (carries the raw code) |

Address schemes are an allowlist at the send entry: a bare actor name,
`agent:<owner>:<machine>:<agent>`, `user:<owner>`, and
`route:<adapter>:<route>` are deliverable; anything else carrying a scheme —
including not-yet-implemented future forms and typos — is rejected with
`UNSUPPORTED_TARGET` instead of queueing silently.

Fanout is fail-fast: if a later member is rejected, earlier members have
already posted, and the operation raises. A retry reuses the same business
idempotency keys, so already-posted members dedupe inside Lark's provider
uuid window.

## Future: the agent→adapter binding layer (design only, not implemented)

Approved direction: every agent binds 1:1 to one Feishu app. The layer adds
ergonomics and policy without replacing explicit addressing.

- **Storage**: a new `agentAdapters` section in `desired-state.json` (the
  daemon-owned, versioned, migrated store), keyed by the sender's canonical
  actor identity as the daemon already normalizes it for delivery keys —
  today the bare node name behind the `agent:<node>` display form, and the
  four-segment `agent:<owner>:<machine>:<agent>` URI once identity
  convergence lands. Binding writes and lookups must pass through that same
  normalization, so one agent never holds two binding entries under
  different spellings. No new file: one more file would be one more place
  that can drift out of sync. `channels.json` routes remain the only
  routing truth; the binding only picks an adapter for the shorthand and
  authorizes explicit picks.
- **Shorthand**: `route:<route>` (two segments) resolves through the
  sender's binding. The full form stays canonical — logs and delivery
  entries always record the resolved `route:<adapter>:<route>` so a
  shorthand never appears in diagnostics. An unbound sender using the
  shorthand fails loudly, naming the missing binding and the command that
  sets it.
- **Overreach**: a bound agent explicitly addressing a different adapter is
  rejected with `ROUTE_ADAPTER_NOT_BOUND`, naming the sender, the bound
  adapter, and the attempted adapter. Whether *unbound* agents may use the
  explicit form is the policy knob for the follow-up card (trusted-intranet
  default: yes; orgs can tighten).
- **Provider-connector agents** (kanban-watcher class): the daemon now
  registers every live streaming connector on the network with the same
  liveliness token and inbox endpoint a peer node holds, so these are
  ordinary registered actors, not a special "unattached" class. They bind
  exactly like user agents — `hyprial agent bind-adapter <actor> <adapter>`
  writing the desired-state section — and `doctor` can check a binding
  against the live registration. No new identity plumbing: the binding key
  is the sender identity already on every send, and `send.received` logs
  already record it, so adapter usage stays auditable per agent.
- **Trap avoidance** ("new state must not become the next silent failure"):
  absence of the binding section means today's behavior, full form keeps
  working; `doctor` flags bindings that reference unconfigured adapters;
  every binding-driven refusal ships with its remedy in the message.
