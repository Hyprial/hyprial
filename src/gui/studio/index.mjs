import { createStudioCore } from './core.mjs';
import { createMemoryPersistence } from './memory-persistence.mjs';
import { createFsPersistence } from './fs-persistence.mjs';

const agentOperations = new Set(['list', 'get', 'create', 'update', 'validate', 'preview', 'prepare-publish', 'migrate']);
const operations = new Set(['list', 'get', 'create', 'update', 'validate', 'preview', 'prepare-publish', 'publish', 'releases', 'export', 'import', 'profile', 'apply', 'restore', 'migrate', 'clone', 'rename', 'delete', 'install-page', 'remove-page', 'configure-profile']);
const fail = (code, message) => { throw Object.assign(new Error(message), { code }); };
const validText = value => typeof value === 'string' && value.trim().length > 0;

function trustedIdentity(context) {
  if (!context || !['user', 'agent'].includes(context.source) || !validText(context.principalId)) fail('GUI_FORBIDDEN', 'A trusted Studio caller context is required');
  if (context.source === 'agent' && !validText(context.sessionRef)) fail('GUI_FORBIDDEN', 'Agent session context is required');
  if (context.sessionRef !== undefined && !validText(context.sessionRef)) fail('GUI_FORBIDDEN', 'Invalid Studio session context');
  return { trustedUser: context.source === 'user', sessionId: context.sessionRef };
}

export function createStudio(options = {}) {
  const persistence = options.persistence === undefined || options.persistence === 'memory'
    ? createMemoryPersistence(options.seed ?? null)
    : options.persistence === 'fs'
      ? (validText(options.root) ? createFsPersistence(options.root) : fail('GUI_INVALID_ARGUMENT', 'fs persistence requires a root'))
      : fail('GUI_INVALID_ARGUMENT', 'Unsupported Studio persistence adapter');
  const core = createStudioCore({ persistence, now: options.now ?? Date.now });

  async function invoke(request, trustedContext) {
    const identity = trustedIdentity(trustedContext);
    if (!request || typeof request !== 'object' || Array.isArray(request) || !operations.has(request.operation)) fail('UNSUPPORTED_OPERATION', 'Unsupported GUI Studio operation');
    if (Buffer.byteLength(JSON.stringify(request), 'utf8') > 262144) fail('BODY_TOO_LARGE', 'GUI Studio request exceeds 256 KiB');
    if (['trustedUser', 'source', 'context', 'principalId', 'sessionRef'].some(key => Object.hasOwn(request, key))) fail('GUI_ARGUMENT_REJECTED', 'GUI Studio authorization overrides are forbidden');
    const { operation, sessionId, ...args } = request;
    if (trustedContext.source === 'agent' && (!agentOperations.has(operation) || sessionId !== undefined)) fail('GUI_FORBIDDEN', 'GUI Studio operation requires the user interface');
    if (sessionId !== undefined && (trustedContext.source !== 'user' || !['create', 'import', 'clone'].includes(operation))) fail('GUI_ARGUMENT_REJECTED', 'Session binding is only accepted when creating a draft');
    if (trustedContext.source === 'user' && sessionId !== undefined) identity.sessionId = sessionId;
    if (['create', 'import', 'clone'].includes(operation) && !validText(identity.sessionId)) fail('GUI_SESSION_REQUIRED', 'A design session is required to create a draft');
    if (operation === 'validate') {
      const candidate = Object.hasOwn(args, 'document');
      const fields = candidate ? ['document'] : ['id', 'revision'];
      if (Object.keys(args).some(key => !fields.includes(key)) || fields.some(key => !Object.hasOwn(args, key))) fail('GUI_ARGUMENT_REJECTED', 'Validate requires either document or an exact id/revision pair');
    }
    if (operation === 'preview' || operation === 'prepare-publish' || (operation === 'validate' && args.id !== undefined)) {
      if (Object.keys(args).some(key => !['id', 'revision'].includes(key))) fail('GUI_ARGUMENT_REJECTED', 'Preview requires only a draft ID and exact revision');
      const draft = await core.dispatch('get', { id: args.id }, identity);
      if (!Number.isSafeInteger(args.revision) || draft.revision !== args.revision) fail('GUI_REVISION_CONFLICT', 'GUI changed; refresh and retry');
      const validation = await core.dispatch('validate', { document: draft.document }, identity);
      if (operation === 'validate') return { ...validation, id: draft.id, revision: draft.revision };
      return { draft, validation, mode: operation, requiresUserApply: true };
    }
    return core.dispatch(operation, args, identity);
  }
  return { invoke, async close() {} };
}

const string = { type: 'string' };
const revision = { type: 'integer', minimum: 0 };
const document = { type: 'object', description: 'Declarative GUI document from gui_context. For schemaVersion 2 read catalogV2 and contractV2: stable module instanceId, declared views and bounded context references. No executable code, resource URLs or business commands.' };
const definitions = {
  context: { operation: 'get', properties: { id: string }, required: [], description: 'Design or customize a GUI from user text, hand-drawn sketches, prototypes, screenshots or style-reference images in this native conversation. Read your session drafts and module catalog; omit id to list, supply id to read an owned draft. Read catalogV2, contractV2.referenceAuthoring and contractV2.style before editing: map structure and style to supported modules, disclose gaps, preserve complete business controls and stable identities. Images stay in the native conversation; this tool neither uploads nor analyzes them. No access to another session draft or active profile.' },
  create: { operation: 'create', properties: { document }, required: ['document'], description: 'Create a GUI draft owned by this Agent session. Does not change the active GUI. Use the document schema and module catalog from gui_context.' },
  update: { operation: 'update', properties: { id: string, baseRevision: revision, document }, required: ['id', 'baseRevision', 'document'], description: 'Save a complete declarative GUI document against its authoritative baseRevision. Stale revisions are rejected. Layout and declared style only: follow contractV2.style, prefer a preset before overrides, validate both color modes and preserve business module identity, state and supported views. Never inject CSS or rebuild internal business controls.' },
  migrate: { operation: 'migrate', properties: { id: string, baseRevision: revision }, required: ['id', 'baseRevision'], description: 'Upgrade your own GUI draft to schemaVersion 2 with stable module instance IDs. Requires current baseRevision. Keeps published versions immutable and never applies or installs a GUI.' },
  validate: { operation: 'validate', properties: { id: string, revision, document }, required: [], oneOf: [{ required: ['document'], not: { anyOf: [{ required: ['id'] }, { required: ['revision'] }] } }, { required: ['id', 'revision'], not: { required: ['document'] } }], description: 'Validate either a candidate {document} without saving or an exact stored {id,revision}; never mix the two forms. Candidate validation checks schema, catalog, style and session references, not stored revision or instance history; create/update still enforce those rules. Use before saving a reference-based design, then validate the saved revision before preview. This is not a visual-fidelity check.' },
  preview: { operation: 'preview', properties: { id: string, revision }, required: ['id', 'revision'], description: 'Read a validated GUI preview document for the user to inspect. Does not apply it or alter the default GUI.' },
  prepare_publish: { operation: 'prepare-publish', properties: { id: string, revision }, required: ['id', 'revision'], description: 'Prepare the exact GUI revision for user review. Publication and application require user actions in GUI Studio; this tool never changes the active GUI.' },
};

export function registerStudioTools(registry, studio) {
  if (!registry?.register || !studio?.invoke) fail('GUI_INVALID_ARGUMENT', 'A Studio registry and instance are required');
  const disposers = [];
  try {
    for (const [name, def] of Object.entries(definitions)) {
      disposers.push(registry.register({
        name: `hyprial_gui_${name}`,
        description: def.description,
        parameters: { type: 'object', properties: def.properties, required: def.required, additionalProperties: false, ...(def.oneOf ? { oneOf: def.oneOf } : {}) },
        output: { schema: {}, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
        async execute(args, trustedContext) {
          if (trustedContext?.signal?.aborted) throw new Error('GUI tool execution aborted');
          if (!args || typeof args !== 'object' || Array.isArray(args) || Object.keys(args).some(key => !Object.hasOwn(def.properties, key)) || def.required.some(key => !Object.hasOwn(args, key))) fail('GUI_ARGUMENT_REJECTED', 'GUI tool arguments rejected; session and authorization overrides are forbidden');
          const operation = name === 'context' && !args.id ? 'list' : def.operation;
          return studio.invoke({ operation, ...args }, trustedContext);
        },
      }));
    }
  } catch (error) { for (const dispose of disposers.reverse()) dispose(); throw error; }
  let disposed = false;
  return () => { if (disposed) return; disposed = true; for (const dispose of disposers.reverse()) dispose(); };
}
