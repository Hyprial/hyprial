# Message status query contract (`message.status`)

`message.status` is a read-only IPC projection of the existing delivery-status
authority. It is bound to the daemon request catalog and is intentionally not
an HTTP method: the HTTP projection has no equivalent trusted caller identity
or delivery-status mesh context in this slice.

The request accepts the existing `actor`/`from` identity spellings, optional
`messageId`, `sessionRef`, a non-empty-string `onBehalfOf`, and the bounded
`timeoutSeconds` mesh-query hint. An explicit `onBehalfOf: null` is invalid;
callers without proxy authority must omit the field. Omitting `timeoutSeconds` uses the 2-second default; an
explicit value must be finite and satisfy `0 < timeoutSeconds <= 10`, otherwise
the request returns `INVALID_ARGUMENT`. The response contains `records` and the complete
`diagnostics` projection; when a message selector is present it additionally
contains `messageId` and one of `fetched`, `expired`, `refused`, `pending`, or
`unknown`, plus `holdExpiresAtMs` for a locally held message.

The two CLI projections, `message status` and `message delivery status`, keep
their existing arguments and output and both call this same method. Other
`message.*` methods remain on the legacy router until their own contract
migrations.
