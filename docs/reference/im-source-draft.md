# Multi-platform adapter source draft

This draft carries the reviewed multi-IM library implementations onto current
dev. It adds source, versioned value contracts, protocol tests and optional SDK
extras. Node activation remains the current dev implementation: the configured
adapter runtime still enables Lark only.

| Source family | Variants / implementation | Optional SDK extra |
| --- | --- | --- |
| Lark / Feishu | Existing gateway runtime, unchanged | Existing dependencies |
| Weixin | iLink pull/HTTPS, codec and onboarding | None |
| DingTalk | Stream SDK transport, codec and onboarding | `adapter-dingtalk` |
| WeCom | Separate bot and application runtimes | `adapter-wecom` |
| QQ | Official-bot gateway/HTTP/media wire implementation | None |
| Slack | Socket Mode, HTTP/media and codec | None |
| Telegram | Bot polling/webhook, HTTP/media and codec | None |
| Discord | Gateway, REST/media and codec | None |
| WhatsApp | Linked-device runtime and supervised Tryx worker boundary | Exact Tryx artifact remains external |
| iMessage | macOS text-DM reader/sender implementation | OS permissions at runtime |
| Matrix | Sync/E2EE/codec and production SDK bridge | `adapter-matrix` |
| Mail | Standard IMAP + SMTP/MIME implementation | None |

These are 12 source families / 13 variants (WeCom has two). Package import does
not start a worker, connect a platform, require an optional SDK, or install an
organization/claim authority. SDK-specific tests are explicitly skipped when
their extra is absent; they run when the extra is installed.

## Module placement

- Platform runtimes and shared contracts/channel/transport/worker helpers stay in
  `hyprial.daemon.impl.adapters`.
- Immutable identity/receipt/authorization projections are in
  `hyprial.identity.impl.adapter_contracts`, consumed through `hyprial.identity`.
  They do not create a registry, SQLite store, organization issuer or actor.
- Current schema 3, `AdapterSpec`, adapter/harness separation, identity registry,
  single-writer state persistence, Lark composition, CLI and GUI layout remain
  the dev versions. The old identity migration and application composition were
  not copied over them.

The imported lease/receipt types describe signed bytes; they are not by
themselves an authenticated node authorization decision. A future runtime must
bind them to the current verified organization and persistence authorities.

## Validation

```sh
uv sync --extra test --extra adapter-dingtalk --extra adapter-matrix --extra adapter-wecom
uv run pytest tests/daemon/adapters/source_intake tests/identity/adapter_contracts
uv run pytest tests/daemon/adapters/common tests/daemon/adapters/weixin \
  tests/daemon/adapters/dingtalk tests/daemon/adapters/wecom \
  tests/daemon/adapters/telegram tests/daemon/adapters/slack \
  tests/daemon/adapters/discord tests/daemon/adapters/qq \
  tests/daemon/adapters/matrix tests/daemon/adapters/mail \
  tests/daemon/adapters/imessage tests/daemon/adapters/whatsapp
```

These isolated protocol/unit/SDK checks do not establish live account support.
The exact-wheel WhatsApp test requires the separately supplied reviewed Tryx
artifact. No unlicensed QQ QR implementation or alternate cloud substitute is
included.

## Follow-up owned by R&D

1. Extend current adapter configuration and `AdapterSpec` activation with explicit
   availability rather than restoring the old harness-based adapter state.
2. Compose selected libraries through current daemon identity, authorization,
   inbox/outbox and persistence ports; do not add a second active authority.
3. Adapt production factories and onboarding for each selected platform. The old
   application wiring and its frozen-parent tests remain available at the source
   commit for reference, not active in this draft.
4. Keep `UA-427` remote-registrar settlement, Tryx distribution, per-platform
   credentials/OS permissions and live validation as explicit follow-up work.

Source provenance and measured gates are recorded in
`docs/notes/full-adapter-draft-2026-10-29/`.
