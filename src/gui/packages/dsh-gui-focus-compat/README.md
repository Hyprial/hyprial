# Native GUI design focus admission

This audited compatibility patch adds an explicit extension to the pinned DSH
`@deepseek-ai/dsh-client-ui-conversation@0.1.5-rc.2` native composer. It does not
replace `session.prompt`, intercept DOM input, or change the host protocol.

Before `ConversationController.sendSession` prepares attachments or creates a
submission echo, it awaits the Cordis event `gui-design/before-send` with:

```js
{ sessionId, text, contextText: '', signal }
```

The GUI editor registers a listener only for its lifetime. A listener must check
the open editor, editing mode, and bound design session before changing anything.
It captures focus synchronously on admission, may await a draft save, then sets
`contextText`. A nonempty context is prepended to the original text; original
attachments, delivery mode, cancellation signal, and request identity stay in
the native pipeline. A preparation failure rejects before the native submission
echo, allowing the input machine to retain the user's original input and files.

`conversation.guiDesignContextVersion === 1` advertises this extension. No
listener means no added context. Programmatic `session.prompt` calls do not pass
this seam; GUI's explicit design-request action already owns its own context.

Apply `node scripts/dsh-gui-focus-compat.mjs <candidate-runtime>` only to an
isolated runtime. Both pinned version and complete input/output hashes are
checked before writes. The release verifier applies this patch and runs
`verify-dsh-gui-focus.mjs`, which executes the actual native send method with
real Cordis event dispatch and a substituted host prompt boundary, without
sending any model request.
