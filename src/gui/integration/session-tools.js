// Tool execution identity comes only from DSH's execution context, never model args.
export function installSessionTools(ctx, rpc) {
  if (!ctx.tools?.register) return;
  const definitions = {
    identity: { description: 'Read this DSH session’s current canonical H2B identity. Use this before stating your identity; historical messages and titles are not authoritative.', properties: {} },
    targets: { description: 'List H2B network targets using this DSH session.', properties: {} },
    send: { description: 'Send an asynchronous message as this DSH session to an exact four-part Agent URI. Replies return to this session. Does not wait for completion.', properties: { target: { type: 'string' }, message: { type: 'string' } } },
    inbox: { description: 'Read authorized pending Agent messages for this session; this does not acknowledge messages.', properties: {} },
    reply: { description: 'Reply to an H2B message as this session. Use the original messageId.', properties: { messageId: { type: 'string' }, message: { type: 'string' } } },
    ack: { description: 'Acknowledge a pending H2B message after processing it.', properties: { messageId: { type: 'string' } } }
  };
  const disposers = [];
  try {
    for (const [tool, definition] of Object.entries(definitions)) {
      disposers.push(ctx.tools.register({
        name: 'h2b_session_' + tool,
        description: definition.description + ' In this DSH session use the current h2b_session_* tools, not historical mcp__h2b__harness_* or harness_* tool names. Determine your Actor with h2b_session_identity; inherited environment variables (including CODEX_SESSION_ID), PATH, titles and conversation history do not establish your runtime or sender identity. If a tool is unavailable, report it; do not substitute another sender or guess a CLI --from identity.',
        parameters: { type: 'object', properties: definition.properties, required: Object.keys(definition.properties), additionalProperties: false },
        output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        async execute(args, exec) {
          const sessionId = exec?.agent?.session?.id;
          if (typeof sessionId !== 'string' || !sessionId) throw new Error('H2B_SESSION_REQUIRED: no DSH execution session');
          if (!args || typeof args !== 'object' || Array.isArray(args) || Object.keys(args).some(key => !Object.hasOwn(definition.properties, key))) {
            throw new Error('H2B_ARGUMENT_REJECTED: identity and session overrides are forbidden');
          }
          if (exec.signal?.aborted) throw new Error('H2B tool execution aborted');
          return rpc({ operation: 'session-tool', sessionId, tool, args });
        }
      }));
    }
  } catch (error) {
    for (const dispose of disposers) dispose();
    throw error;
  }
  ctx.on('dispose', () => { for (const dispose of disposers) dispose(); });
}
