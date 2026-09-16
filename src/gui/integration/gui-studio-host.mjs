import { createGuiStudio } from './gui-studio.mjs';

const OPERATIONS = new Set(['list', 'get', 'create', 'update', 'validate', 'preview', 'prepare-publish', 'publish', 'releases', 'export', 'import', 'profile', 'apply', 'restore', 'migrate', 'clone', 'rename', 'delete', 'install-page', 'remove-page', 'configure-profile']);
const AGENT_OPERATIONS = new Set(['list', 'get', 'create', 'update', 'validate', 'preview', 'prepare-publish', 'migrate']);

// Only Host callers supply context. Identity/authorization never comes from document JSON.
export function createGuiStudioHost(options) {
  const store = createGuiStudio(options);
  return async function handle(input, context) {
    if (!input || typeof input !== 'object' || Array.isArray(input) || !OPERATIONS.has(input.operation)) {
      throw Object.assign(new Error('Unsupported GUI Studio operation'), { code: 'UNSUPPORTED_OPERATION' });
    }
    if (Buffer.byteLength(JSON.stringify(input), 'utf8') > 262144) {
      throw Object.assign(new Error('GUI Studio request exceeds 256 KiB'), { code: 'BODY_TOO_LARGE' });
    }
    if (['trustedUser', 'source', 'context'].some(key => Object.hasOwn(input, key))) {
      throw Object.assign(new Error('GUI Studio authorization overrides are forbidden'), { code: 'GUI_ARGUMENT_REJECTED' });
    }
    if (context?.source !== undefined && !['agent', 'user'].includes(context.source)) {
      throw Object.assign(new Error('Unknown GUI Studio caller source'), { code: 'GUI_FORBIDDEN' });
    }
    const { operation, sessionId, ...args } = input;
    const identity = context?.source === 'agent'
      ? { sessionId: context.sessionId }
      : { trustedUser: true, sessionId };
    if (context?.source === 'agent' && (sessionId !== undefined || !identity.sessionId || !AGENT_OPERATIONS.has(operation))) {
      throw Object.assign(new Error('GUI Studio operation requires the user interface'), { code: 'GUI_FORBIDDEN' });
    }
    if (context?.source !== 'agent' && sessionId !== undefined && !['create', 'import', 'clone'].includes(operation)) {
      throw Object.assign(new Error('Session binding is only accepted when creating a draft'), { code: 'GUI_ARGUMENT_REJECTED' });
    }
    if (operation === 'validate') {
      const candidate = Object.hasOwn(args, 'document');
      const fields = candidate ? ['document'] : ['id', 'revision'];
      if (Object.keys(args).some(key => !fields.includes(key)) || fields.some(key => !Object.hasOwn(args, key))) {
        throw Object.assign(new Error('Validate requires either document or an exact id/revision pair'), { code: 'GUI_ARGUMENT_REJECTED' });
      }
    }
    if (operation === 'preview' || operation === 'prepare-publish' || (operation === 'validate' && args.id !== undefined)) {
      if (Object.keys(args).some(key => !['id', 'revision'].includes(key))) {
        throw Object.assign(new Error('Preview requires only a draft ID and exact revision'), { code: 'GUI_ARGUMENT_REJECTED' });
      }
      const draft = await store.dispatch('get', { id: args.id }, identity);
      if (!Number.isSafeInteger(args.revision) || draft.revision !== args.revision) {
        throw Object.assign(new Error('GUI changed; refresh and retry'), { code: 'GUI_REVISION_CONFLICT' });
      }
      const validation = await store.dispatch('validate', { document: draft.document }, identity);
      if (operation === 'validate') return { ...validation, id: draft.id, revision: draft.revision };
      return { draft, validation, mode: operation, requiresUserApply: true };
    }
    return store.dispatch(operation, args, identity);
  };
}

// Mirrored in the script-style Host, checked by a parity test (same pattern as Workflow tools).
export function installGuiStudioTools(ctx, handle) {
  if (!ctx.tools?.register) return;
  const string = { type: 'string' }, revision = { type: 'integer', minimum: 0 };
  const document = { type: 'object', description: 'Declarative GUI document from gui_context. For schemaVersion 2 read catalogV2 and contractV2: stable module instanceId, declared views and bounded context references. No executable code, resource URLs or business commands.' };
  const definitions = {
    context: { operation: 'get', properties: { id: string }, required: [], description: 'Design or customize a GUI from user text, hand-drawn sketches, prototypes, screenshots or style-reference images in this native conversation. Read your session drafts and module catalog; omit id to list, supply id to read an owned draft. Read catalogV2, contractV2.referenceAuthoring and contractV2.style before editing: map structure and style to supported modules, disclose gaps, preserve complete business controls and stable identities. Images stay in the native conversation; this tool neither uploads nor analyzes them. No access to another session draft or active profile.' },
    create: { operation: 'create', properties: { document }, required: ['document'], description: 'Create a GUI draft owned by this Agent session. Does not change the active GUI. Use the document schema and module catalog from gui_context.' },
    update: { operation: 'update', properties: { id: string, baseRevision: revision, document }, required: ['id', 'baseRevision', 'document'], description: 'Save a complete declarative GUI document against its authoritative baseRevision. Stale revisions are rejected. Layout and declared style only: follow contractV2.style, prefer a preset before overrides, validate both color modes and preserve business module identity, state and supported views. Never inject CSS or rebuild internal business controls.' },
    migrate: { operation: 'migrate', properties: { id: string, baseRevision: revision }, required: ['id', 'baseRevision'], description: 'Upgrade your own GUI draft to schemaVersion 2 with stable module instance IDs. Requires current baseRevision. Keeps published versions immutable and never applies or installs a GUI.' },
    validate: { operation: 'validate', properties: { id: string, revision, document }, required: [], oneOf: [{ required: ['document'], not: { anyOf: [{ required: ['id'] }, { required: ['revision'] }] } }, { required: ['id', 'revision'], not: { required: ['document'] } }], description: 'Validate either a candidate {document} without saving or an exact stored {id,revision}; never mix the two forms. Candidate validation checks schema, catalog, style and session references, not stored revision or instance history; create/update still enforce those rules. Use before saving a reference-based design, then validate the saved revision before preview. This is not a visual-fidelity check.' },
    preview: { operation: 'preview', properties: { id: string, revision }, required: ['id', 'revision'], description: 'Read a validated GUI preview document for the user to inspect. Does not apply it or alter the default GUI.' },
    prepare_publish: { operation: 'prepare-publish', properties: { id: string, revision }, required: ['id', 'revision'], description: 'Prepare the exact GUI revision for user review. Publication and application require user actions in GUI Studio; this tool never changes the active GUI.' }
  };
  const disposers = [];
  try {
    for (const [name, def] of Object.entries(definitions)) disposers.push(ctx.tools.register({
      name: 'h2b_gui_' + name,
      description: def.description,
      parameters: { type: 'object', properties: def.properties, required: def.required, additionalProperties: false, ...(def.oneOf ? { oneOf: def.oneOf } : {}) },
      output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
      async execute(args, exec) {
        const sessionId = exec?.agent?.session?.id;
        if (typeof sessionId !== 'string' || !sessionId) throw Object.assign(new Error('No DSH execution session'), { code: 'GUI_SESSION_REQUIRED' });
        if (exec?.signal?.aborted) throw new Error('GUI tool execution aborted');
        if (!args || typeof args !== 'object' || Array.isArray(args) || Object.keys(args).some(key => !Object.hasOwn(def.properties, key)) || def.required.some(key => !Object.hasOwn(args, key))) {
          throw Object.assign(new Error('GUI tool arguments rejected; session and authorization overrides are forbidden'), { code: 'GUI_ARGUMENT_REJECTED' });
        }
        return handle({ operation: name === 'context' && !args.id ? 'list' : def.operation, ...args }, { source: 'agent', sessionId });
      }
    }));
  } catch (error) { for (const dispose of disposers) dispose(); throw error; }
  ctx.on('dispose', () => { for (const dispose of disposers) dispose(); });
}
