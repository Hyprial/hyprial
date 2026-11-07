# Agent-home P20 native fixtures

These files are deliberately non-secret native-format inputs for the P20
capability probe.  Every credential-looking value is the literal
`P20_SENTINEL_NOT_A_SECRET`; it is intended only for local parser/source
selection tests and must never be sent to a vendor endpoint.

The probe copies these fixtures into a fresh temporary HOME/XDG/native-root
tree before invoking a real installed CLI.  It never points a tested CLI at
the operator's real HOME, config root, or credential store.
