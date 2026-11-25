# Retired local GUI host contract

The optional `/api/gui/v1/live` and `/__gui/auth` Web transports, their Node/Python
workers, and the public `run_gui_backend` facade are retired. Current product
entrypoints do not load them; their old standalone acceptance runner has also
been removed. Historical results are not acceptance of the current request path.

The shared GUI now uses its generated HTTP client, served MethodSpec admission,
then daemon request handlers. The Node same-origin session boundary and Python
request HTTP boundary remain separate. Native setup/external lifecycle, shared
first-run views and injected controller ports remain available to their
existing consumers.

Current method coverage and missing capabilities are tracked in the
[GUI method ledger](../../docs/design/gui-v0.1-method-plan-2026-11-13.md).
