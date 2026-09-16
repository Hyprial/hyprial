import { GUI_STYLE_CONTRACT, guiValidateTheme, guiValidateAppearance, guiStyleVariables, guiModuleAppearanceVariables } from '../shared/gui-style.mjs';
import { GUI_REFERENCE_AUTHORING } from './gui-reference-authoring.mjs';
import { mkdir, lstat, readFile, writeFile, rename, rm } from 'node:fs/promises';
import { join } from 'node:path';
import { randomUUID, createHash } from 'node:crypto';
import lockfile from 'proper-lockfile';

export const GUI_FEATURES = Object.freeze(['dsh.conversation', 'h2b.directChat', 'h2b.contacts', 'h2b.workflow', 'h2b.kanban', 'h2b.operations', 'h2b.routine']);
const MAX_DOCUMENT = 64 * 1024, MAX_STORE = 8 * 1024 * 1024;
const clone = value => JSON.parse(JSON.stringify(value));
function fail(code, message) { throw Object.assign(new Error(message), { code }); }
function assert(ok, message) { if (!ok) fail('GUI_INVALID_ARGUMENT', message); }
function record(value) { return !!value && typeof value === 'object' && !Array.isArray(value) && [Object.prototype, null].includes(Object.getPrototypeOf(value)); }
function keys(value, allowed) { assert(record(value), 'Expected an object'); for (const key of Object.keys(value)) assert(allowed.includes(key), `Unsupported field: ${key}`); }
function label(value, max = 160) { assert(typeof value === 'string' && value.trim().length > 0 && value.length <= max && !/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value), 'Expected bounded text'); }
function slug(value) { assert(typeof value === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(value), 'Invalid identifier'); }
function safeTree(value, depth = 0, maxDepth = 24) {
  assert(depth <= maxDepth, 'Document nesting is too deep');
  if (Array.isArray(value)) { assert(value.length <= 256, 'Array is too large'); for (const item of value) safeTree(item, depth + 1, maxDepth); }
  else if (value && typeof value === 'object') {
    assert(record(value), 'Only plain objects are accepted');
    for (const key of Object.keys(value)) { assert(!['__proto__', 'prototype', 'constructor'].includes(key), 'Unsafe key'); safeTree(value[key], depth + 1, maxDepth); }
  } else assert(value === null || ['string', 'boolean'].includes(typeof value) || (typeof value === 'number' && Number.isFinite(value)), 'Non-JSON value');
}
export function validateGuiDocument(input) {
  safeTree(input);
  assert(Buffer.byteLength(JSON.stringify(input)) <= MAX_DOCUMENT, 'GUI document exceeds 64 KiB');
  keys(input, ['schemaVersion', 'id', 'name', 'kind', 'navigation', 'pages', 'theme', 'layout']);
  assert([1, 2].includes(input.schemaVersion), 'Unsupported GUI schema'); slug(input.id); label(input.name);
  assert(['shell', 'page'].includes(input.kind), 'Invalid GUI kind');
  assert(Array.isArray(input.pages) && input.pages.length > 0 && input.pages.length <= 24, 'Expected 1–24 pages');
  if (input.kind === 'page') assert(input.pages.length === 1, 'A page package must contain one page');
  const pageIds = new Set(), instanceIds = new Set(); let count = 0;
  for (const page of input.pages) {
    keys(page, ['id', 'title', 'layout', 'appearance']); if (page.appearance !== undefined) guiValidateAppearance(page.appearance); slug(page.id); label(page.title); assert(!pageIds.has(page.id), 'Duplicate page'); pageIds.add(page.id);
    const nodeIds = new Set(); let nativeSeats = 0;
    function node(item, depth = 0) {
      assert(depth <= 12 && ++count <= 256, 'Layout is too complex');
      assert(record(item), 'Expected layout node');
      if (item.appearance !== undefined) guiValidateAppearance(item.appearance);
      slug(item.id); assert(!nodeIds.has(item.id), 'Duplicate node ID'); nodeIds.add(item.id);
      if (item.type === 'Text') { keys(item, ['type', 'id', 'text', 'appearance']); label(item.text, 4000); return; }
      if (item.type === 'Feature') {
        keys(item, ['type', 'id', 'feature', 'view', 'appearance', ...(input.schemaVersion === 2 ? ['instanceId', 'context', 'dataSource', 'actions'] : [])]); assert(GUI_FEATURES.includes(item.feature), 'Unknown feature');
        const view = item.view === undefined ? 'default' : item.view;
        if (input.schemaVersion === 1) {
          assert(item.view === undefined || ['default', 'launcher', 'readOnly'].includes(item.view), 'Unsupported feature view');
          assert(view !== 'readOnly' || item.feature === 'h2b.kanban', 'Only kanban supports the readOnly view');
          if (view === 'default') assert(++nativeSeats <= 1, 'A page has one native business seat; use launcher or supported readOnly views for other modules');
        } else {
          assert(GUI_V2_VIEWS[item.feature].includes(view), 'Unsupported feature view');
          slug(item.instanceId); assert(!instanceIds.has(item.instanceId), 'Duplicate module instanceId'); instanceIds.add(item.instanceId);
          if (item.feature === 'dsh.conversation' && view === 'default') assert(++nativeSeats <= 1, 'A page has one native Agent conversation owner');
          if (item.context !== undefined) {
            keys(item.context, ['sessionId', 'workflowId', 'runId', 'taskId', 'targetUri']);
            for (const [key, value] of Object.entries(item.context)) { label(value, key === 'targetUri' ? 1024 : 256); if (key !== 'targetUri') assert(/^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$/.test(value), 'Invalid context reference'); }
          }
          if (item.dataSource !== undefined) assert(item.dataSource === item.feature + '.' + view, 'Data source must match the catalog feature view');
          if (item.actions !== undefined) { assert(Array.isArray(item.actions) && item.actions.length <= 1 && item.actions.every(ref => ref === item.feature + '.open'), 'Unknown or duplicate module action reference'); }
        }
        return;
      }
      assert(['Stack', 'Grid', 'Split', 'Tabs'].includes(item.type), 'Unknown layout node');
      const extra = { Stack: ['gap'], Grid: ['gap', 'columns'], Split: ['gap', 'ratio'], Tabs: ['labels'] }[item.type];
      keys(item, ['type', 'id', 'children', 'appearance', ...extra]);
      assert(Array.isArray(item.children) && item.children.length > 0 && item.children.length <= 24, 'Expected bounded children');
      if (item.type === 'Split') assert(item.children.length === 2, 'Split requires two children');
      if (item.gap !== undefined) assert(Number.isInteger(item.gap) && item.gap >= 0 && item.gap <= 32, 'Invalid gap');
      if (item.columns !== undefined) assert(Number.isInteger(item.columns) && item.columns >= 1 && item.columns <= 4, 'Invalid columns');
      if (item.ratio !== undefined) assert(Number.isInteger(item.ratio) && item.ratio >= 20 && item.ratio <= 80, 'Invalid split ratio');
      if (item.labels !== undefined) { assert(Array.isArray(item.labels) && item.labels.length === item.children.length, 'Tab labels must match children'); item.labels.forEach(value => label(value)); }
      item.children.forEach(child => node(child, depth + 1));
    }
    node(page.layout);
  }
  assert(Array.isArray(input.navigation) && input.navigation.length <= 32, 'Expected navigation');
  const navIds = new Set();
  for (const entry of input.navigation) {
    keys(entry, ['id', 'label', 'feature', 'pageId']); slug(entry.id); label(entry.label); assert(!navIds.has(entry.id), 'Duplicate navigation ID'); navIds.add(entry.id);
    assert((entry.feature !== undefined) !== (entry.pageId !== undefined), 'Navigation requires exactly one target');
    if (entry.feature !== undefined) assert(GUI_FEATURES.includes(entry.feature), 'Unknown navigation feature');
    if (entry.pageId !== undefined) assert(pageIds.has(entry.pageId), 'Unknown navigation page');
  }
  if (input.theme !== undefined) guiValidateTheme(input.theme);
  if (input.layout !== undefined) {
    keys(input.layout, ['navigation', 'nativeSidebar', 'sidebarWidth', 'detailsWidth', 'workspaceRatio', 'objects']);
    if (input.layout.objects !== undefined) {
      keys(input.layout.objects, ['mode', 'width']);
      if (input.layout.objects.mode !== undefined) assert(['inline', 'collapsible', 'drawer'].includes(input.layout.objects.mode), 'Invalid object list display');
      if (input.layout.objects.width !== undefined) assert(Number.isInteger(input.layout.objects.width) && input.layout.objects.width >= 200 && input.layout.objects.width <= 480, 'Invalid object list width');
    }
    if (input.layout.navigation !== undefined) assert(['left', 'top', 'native'].includes(input.layout.navigation), 'Invalid navigation placement');
    if (input.layout.nativeSidebar !== undefined) assert(typeof input.layout.nativeSidebar === 'boolean', 'Invalid native sidebar visibility');
    if (input.layout.nativeSidebar === false) assert(input.layout.navigation === 'top', 'Collapsed native sidebar requires top navigation');
    for (const [key, min, max] of [['sidebarWidth', 200, 480], ['detailsWidth', 240, 640], ['workspaceRatio', 20, 80]]) {
      if (input.layout[key] !== undefined) assert(Number.isInteger(input.layout[key]) && input.layout[key] >= min && input.layout[key] <= max, 'Invalid workspace width');
    }
  }
  // Validate effective inherited page/node presentation in both possible runtime modes.
  for (const mode of ['light', 'dark']) {
    const variables = guiStyleVariables(input.theme || {}, mode);
    for (const page of input.pages) {
      const pageVariables = guiModuleAppearanceVariables(variables, page.appearance || {});
      function presentation(item, inherited) {
        const effective = guiModuleAppearanceVariables(inherited, item.appearance || {});
        (item.children || []).forEach(child => presentation(child, effective));
      }
      presentation(page.layout, pageVariables);
    }
  }
  return clone(input);
}
export const GUI_EXAMPLE = Object.freeze({ schemaVersion: 1, id: 'my-workspace', name: 'My workspace', kind: 'shell', navigation: [{ id: 'home', label: 'Home', pageId: 'home' }], pages: [{ id: 'home', title: 'Home', layout: { type: 'Feature', id: 'conversation', feature: 'dsh.conversation' } }], theme: { mode: 'system', density: 'comfortable' }, layout: { navigation: 'left', sidebarWidth: 280, detailsWidth: 360 } });
export const GUI_CATALOG = Object.freeze(GUI_FEATURES.map(id => Object.freeze({
  id, views: id === 'h2b.kanban' ? ['default', 'launcher', 'readOnly'] : ['default', 'launcher'],
  default: 'Uses the existing native business owner in the workspace; never creates a second controller. At most one default Feature per page, including Tabs.',
  launcher: 'Navigation entry that opens the existing business module; it mounts no business controller.',
  ...(id === 'h2b.kanban' ? { readOnly: 'Embeds the supported read-only kanban view; no task write actions or native controller clone.' } : {})
})));
export const GUI_CONTRACT = Object.freeze({
  schemaVersion: 1,
  document: { required: ['schemaVersion', 'id', 'name', 'kind', 'navigation', 'pages'], optional: ['theme', 'layout'], kind: ['shell', 'page'], pageKind: 'Exactly one page; shell supports 1–24 pages.', scope: 'Use shell for whole-GUI customization (one frame/navigation, no automatic personal-page title or return header): theme and layout apply across the workspace after user publication and application. Use page only for an explicitly requested standalone personal page: publishing installs it without changing the global theme or home. Preserve Agent conversation, Workflow and Kanban as complete modules with reachable navigation; keep other business and system entrances available. To promote an existing page, change kind to shell and add navigation without changing module instance IDs, content or styles. For a conversation-focused whole GUI, use layout.navigation=top and layout.nativeSidebar=false; the system bar can reopen the intact native three-level menu without discarding module state. Choose layout.navigation=top for authored horizontal navigation, left for authored vertical navigation, or native for the original four-app rail with its complete second/third-level menus. Native mode retains the shell theme and pages; its rail is not replaced by authored navigation entries. Keep those entries for switching back to an authored navigation mode. Left and native modes preserve the native sidebar. Global navigation visibility must never remove business object selection. Native conversation/contact/control pages retain their existing object browser, while authored full modules own their lists. Configure layout.objects mode and width for the shared business browser; do not replace full modules with fixed-object detail cards unless explicitly requested. Design the authored page heading explicitly with Text; do not duplicate shell navigation in page content. Home is a separate user profile preference: first shell activation defaults to its first page if no preference exists; same-workspace upgrades migrate page targets by stable page ID, and a removed startup page falls back to the new first page. Prefer following the current session; set context.sessionId only when a fixed, available session is explicitly requested. Never invent a session ID; change instanceId when changing context.' },
  identifiers: '1–64 characters: /^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/. Preserve document id on update. Page IDs unique per document; node IDs unique within each page. Selection uses pageId + nodeId.',
  page: { required: ['id', 'title', 'layout'], optional: ['appearance'] },
  navigation: 'Array of {id,label,feature} or {id,label,pageId}; exactly one target. Feature must be catalog ID; pageId must exist. Maximum 32 entries.',
  nodes: {
    common: 'Every node has type and stable id, plus optional appearance (see style.appearance). Maximum 256 total nodes; only fields listed for the node type are allowed.',
    Text: '{type,id,text}: bounded plain text, maximum 4000 characters; no HTML.',
    Feature: '{type,id,feature,view?}: view defaults to default. ONE default native business seat per page. launcher supports every feature; readOnly supports only h2b.kanban.',
    Stack: '{type,id,children,gap?}: children 1–24; gap integer 0–32.',
    Grid: '{type,id,children,gap?,columns?}: columns integer 1–4; gap integer 0–32.',
    Split: '{type,id,children,gap?,ratio?}: exactly two children; ratio integer 20–80; gap integer 0–32.',
    Tabs: '{type,id,children,labels?}: children 1–24; labels must match children. Hidden children remain mounted; the one-native-seat rule covers ALL tabs.'
  },
  theme: GUI_STYLE_CONTRACT.theme,
  style: GUI_STYLE_CONTRACT,
  layout: '{navigation?:left|top|native (left: authored vertical navigation; top: authored horizontal navigation; native: original four-app rail and second/third-level menus),nativeSidebar?:boolean (false requires top navigation; omitted: top hides native sidebar, left/native preserve it),objects?:{mode?:inline|collapsible|drawer,width?:integer 200–480} (business object lists remain available when global navigation is hidden; inline opens beside content, collapsible starts folded, drawer overlays content; narrow screens use a drawer),sidebarWidth?:integer 200–480,detailsWidth?:integer 240–640,workspaceRatio?:integer 20–80}',
  boundaries: 'Declarative presentation only. No arbitrary JavaScript, HTML, CSS, URLs, session IDs, RPC names, actions or authorization overrides. Maximum document 64 KiB. Agent edits its session-owned draft with baseRevision; user publishes and applies.'
});

export const GUI_V2_VIEWS = Object.freeze({
  'dsh.conversation': ['default', 'launcher', 'list', 'summary'],
  'h2b.directChat': ['default', 'launcher', 'list'],
  'h2b.contacts': ['default', 'launcher', 'list', 'detail'],
  'h2b.workflow': ['default', 'launcher', 'list', 'runs', 'detail'],
  'h2b.kanban': ['default', 'launcher', 'readOnly', 'tasks', 'detail'],
  'h2b.routine': ['default', 'launcher', 'list'],
  'h2b.operations': ['default', 'launcher', 'summary']
});
export const GUI_V2_CATALOG = Object.freeze(GUI_FEATURES.map(id => ({ id, views: GUI_V2_VIEWS[id], dataSources: GUI_V2_VIEWS[id].map(view => id + '.' + view), actions: [id + '.open'], nativeExternalViews: id === 'dsh.conversation' ? ['default'] : [], context: ['sessionId', 'workflowId', 'runId', 'taskId', 'targetUri'] })));
export const GUI_V2_CONTRACT = Object.freeze({ ...GUI_CONTRACT, schemaVersion: 2,
  referenceAuthoring: GUI_REFERENCE_AUTHORING,
  nodes: { ...GUI_CONTRACT.nodes, Feature: '{type,id,instanceId,feature,view?,context?,dataSource?,actions?}. instanceId is globally unique within the document and stable across revisions. Multiple module instances compose; dsh.conversation/default has one external native owner per page. View defaults to default; use the v2 catalog. Changing feature, view or context requires a new instanceId.' },
  bindings: 'context contains bounded stable sessionId/workflowId/runId/taskId/targetUri references, never authority. Agents may only reference their own sessionId. dataSource is exactly feature.view; actions contains at most feature.open. Runtime resolves business references and permissions; no write-action authority is granted by a document.'
});
function featureNodes(document) {
  const result = [];
  function walk(node) { if (node.type === 'Feature') result.push(node); (node.children || []).forEach(walk); }
  document.pages.forEach(page => walk(page.layout)); return result;
}
export function migrateGuiDocument(document) {
  const value = validateGuiDocument(document);
  if (value.schemaVersion === 2) return value;
  value.schemaVersion = 2;
  for (const page of value.pages) {
    function walk(node) { if (node.type === 'Feature') node.instanceId = 'instance-' + createHash('sha256').update(value.id + '/' + page.id + '/' + node.id).digest('hex').slice(0, 24); (node.children || []).forEach(walk); }
    walk(page.layout);
  }
  return validateGuiDocument(value);
}
function instanceSignature(node) {
  return JSON.stringify({ feature: node.feature, view: node.view || 'default', context: Object.fromEntries(Object.entries(node.context || {}).sort(([a], [b]) => a.localeCompare(b))) });
}
function rememberInstances(draft, document) {
  if (document.schemaVersion !== 2) return;
  const history = [...(draft.instanceHistory || (draft.document.schemaVersion === 2 ? featureNodes(draft.document).map(node => ({ id: node.instanceId, signature: instanceSignature(node) })) : []))];
  for (const node of featureNodes(document)) {
    const signature = instanceSignature(node), old = history.find(item => item.id === node.instanceId);
    if (old) assert(old.signature === signature, 'Changing module identity or context requires a new instanceId');
    else history.push({ id: node.instanceId, signature });
  }
  assert(history.length <= 256, 'Module instance history is full; clone the draft to start a new lifetime');
  draft.instanceHistory = history;
}
function authoringDocument(document, context) {
  const value = validateGuiDocument(document);
  if (!context.trustedUser) for (const node of featureNodes(value)) if (node.context?.sessionId !== undefined && node.context.sessionId !== context.sessionId) fail('GUI_SESSION_MISMATCH', 'Module session reference belongs to another session');
  return value;
}

const FIELDS = { list: [], create: ['document'], get: ['id'], update: ['id', 'baseRevision', 'document'], validate: ['document'], publish: ['id', 'revision'], releases: [], export: ['id', 'releaseId'], import: ['document'], profile: [], apply: ['releaseId', 'baseRevision'], restore: ['baseRevision'], clone: ['id', 'baseRevision', 'name'], rename: ['id', 'baseRevision', 'name'], delete: ['id', 'baseRevision'], migrate: ['id', 'baseRevision'], 'install-page': ['releaseId', 'baseRevision'], 'remove-page': ['releaseId', 'baseRevision'], 'configure-profile': ['baseRevision', 'home', 'favorites', 'navigation'] };

function profileState(profile) {
  return { ...profile, pageReleaseIds: profile.pageReleaseIds || [], home: profile.home || null, favorites: profile.favorites || [], navigation: profile.navigation || { hiddenFeatures: [], orderedFeatures: [] } };
}
function validateProfileTarget(target, store) {
  keys(target, ['feature', 'releaseId', 'pageId']);
  if (target.feature !== undefined) { assert(Object.keys(target).length === 1 && GUI_FEATURES.includes(target.feature), 'Invalid profile feature target'); return; }
  assert(typeof target.releaseId === 'string' && typeof target.pageId === 'string', 'Expected release and page target');
  const release = store.releases.find(item => item.id === target.releaseId);
  assert(release && release.document.pages.some(page => page.id === target.pageId), 'Unknown profile page target');
  assert(target.releaseId === store.profile.releaseId || (store.profile.pageReleaseIds || []).includes(target.releaseId), 'Profile page target is not active or installed');
}
function validateProfilePreferences(profile, store) {
  assert(Array.isArray(profile.pageReleaseIds) && profile.pageReleaseIds.length <= 32 && new Set(profile.pageReleaseIds).size === profile.pageReleaseIds.length, 'Invalid installed pages');
  for (const id of profile.pageReleaseIds) assert(store.releases.some(r => r.id === id && r.document.kind === 'page'), 'Installed release must be a page');
  if (profile.home !== null) validateProfileTarget(profile.home, { ...store, profile });
  assert(Array.isArray(profile.favorites) && profile.favorites.length <= 24, 'Too many favorites');
  const seen = new Set();
  for (const favorite of profile.favorites) { validateProfileTarget(favorite, { ...store, profile }); const key = favorite.feature || favorite.releaseId + '/' + favorite.pageId; assert(!seen.has(key), 'Duplicate favorite'); seen.add(key); }
  keys(profile.navigation, ['hiddenFeatures', 'orderedFeatures']);
  for (const key of ['hiddenFeatures', 'orderedFeatures']) { const ids = profile.navigation[key] || []; assert(Array.isArray(ids) && ids.length <= GUI_FEATURES.length && new Set(ids).size === ids.length && ids.every(id => GUI_FEATURES.includes(id)), 'Invalid navigation preferences'); }
}
function retainAvailableTargets(profile) {
  const valid = target => target.feature || target.releaseId === profile.releaseId || profile.pageReleaseIds.includes(target.releaseId);
  if (profile.home && !valid(profile.home)) profile.home = null;
  profile.favorites = profile.favorites.filter(valid);
}

// root must be supplied by the Host, never by a document or model argument.
export function createGuiStudio({ root, now = Date.now }) {
  const storePath = join(root, 'store.json');
  function user(context) { if (context.trustedUser !== true) fail('GUI_USER_REQUIRED', 'This operation requires the user interface'); }
  function session(context) { label(context.sessionId, 256); return context.sessionId; }
  function access(draft, context) { if (!context.trustedUser && draft.sessionId !== session(context)) fail('GUI_SESSION_MISMATCH', 'Draft belongs to another design session'); }
  function checkRevision(actual, expected) { if (!Number.isSafeInteger(expected) || expected !== actual) fail('GUI_REVISION_CONFLICT', 'GUI changed; refresh and retry'); }
  async function read() {
    let stat;
    try { stat = await lstat(storePath); } catch (error) { if (error.code === 'ENOENT') return { schemaVersion: 1, drafts: [], releases: [], profile: { revision: 0, releaseId: null } }; throw error; }
    if (!stat.isFile() || stat.size > MAX_STORE) fail('GUI_STORE_ERROR', 'Invalid GUI store');
    let store;
    try {
      store = JSON.parse(await readFile(storePath, 'utf8')); safeTree(store, 0, 32);
      assert(store.schemaVersion === 1 && Array.isArray(store.drafts) && Array.isArray(store.releases) && record(store.profile), 'Invalid GUI store');
      for (const draft of store.drafts) {
        slug(draft.id); label(draft.sessionId, 256); assert(Number.isSafeInteger(draft.revision) && draft.revision > 0, 'Invalid draft revision'); validateGuiDocument(draft.document);
        if (draft.instanceHistory !== undefined) {
          assert(Array.isArray(draft.instanceHistory) && draft.instanceHistory.length <= 256, 'Invalid module instance history');
          const seen = new Set();
          for (const item of draft.instanceHistory) { keys(item, ['id', 'signature']); slug(item.id); label(item.signature, 4096); assert(!seen.has(item.id), 'Duplicate instance history'); seen.add(item.id); }
          for (const node of featureNodes(draft.document)) if (draft.document.schemaVersion === 2) assert(draft.instanceHistory.some(item => item.id === node.instanceId && item.signature === instanceSignature(node)), 'Module instance history mismatch');
        }
      }
      for (const release of store.releases) {
        validateGuiDocument(release.document);
        assert(release.hash === hash(release.document) && release.id === `release-${release.hash}`, 'Release integrity error');
      }
      assert(Number.isSafeInteger(store.profile.revision) && store.profile.revision >= 0 && (store.profile.releaseId === null || store.releases.some(r => r.id === store.profile.releaseId)), 'Invalid profile');
      validateProfilePreferences(profileState(store.profile), store);
    } catch (error) { fail('GUI_STORE_ERROR', `Cannot load GUI store: ${error.message}`); }
    return store;
  }
  const hash = document => createHash('sha256').update(JSON.stringify(document)).digest('hex');
  async function dispatch(action, args = {}, context = {}) {
    assert(Object.hasOwn(FIELDS, action), 'Unknown GUI action'); safeTree(args, 0, 25); keys(args, FIELDS[action]);
    if (['publish', 'apply', 'restore', 'import', 'releases', 'profile', 'clone', 'rename', 'delete', 'install-page', 'remove-page', 'configure-profile'].includes(action) || (action === 'export' && args.releaseId !== undefined)) user(context);
    else if (!context.trustedUser) session(context);
    if (action === 'validate') return { valid: true, document: authoringDocument(args.document, context) };
    await mkdir(root, { recursive: true, mode: 0o700 });
    assert((await lstat(root)).isDirectory(), 'GUI root must be a real directory');
    let compromised;
    const releaseLock = await lockfile.lock(root, { lockfilePath: join(root, '.write-lock'), stale: 30000, update: 5000, retries: { retries: 30, minTimeout: 20, maxTimeout: 300 }, onCompromised(error) { compromised = error; } });
    try {
      const store = await read(); let result, changed = false;
      const getDraft = () => { slug(args.id); const draft = store.drafts.find(item => item.id === args.id); if (!draft) fail('GUI_NOT_FOUND', 'Draft not found'); access(draft, context); return draft; };
      const getRelease = () => { const item = store.releases.find(r => r.id === args.releaseId); if (!item) fail('GUI_NOT_FOUND', 'Release not found'); return item; };
      switch (action) {
        case 'list': result = { drafts: store.drafts.filter(d => context.trustedUser || d.sessionId === context.sessionId), catalog: GUI_CATALOG, contract: GUI_CONTRACT, example: GUI_EXAMPLE, catalogV2: GUI_V2_CATALOG, contractV2: GUI_V2_CONTRACT, exampleV2: migrateGuiDocument(GUI_EXAMPLE) }; break;
        case 'create': case 'import': {
          const document = authoringDocument(args.document, context);
          const draft = { id: `gui-${randomUUID()}`, sessionId: session(context), revision: 1, document, createdAt: now(), updatedAt: now() };
          rememberInstances(draft, document); assert(store.drafts.length < 128, 'Draft limit reached'); store.drafts.push(draft); result = draft; changed = true; break;
        }
        case 'get': result = { ...getDraft(), catalog: GUI_CATALOG, contract: GUI_CONTRACT, catalogV2: GUI_V2_CATALOG, contractV2: GUI_V2_CONTRACT }; break;
        case 'update': { const draft = getDraft(); checkRevision(draft.revision, args.baseRevision); const document = authoringDocument(args.document, context); assert(document.id === draft.document.id, 'Document identity cannot change'); assert(document.schemaVersion >= draft.document.schemaVersion, 'GUI schema cannot downgrade'); rememberInstances(draft, document); draft.document = document; draft.revision++; draft.updatedAt = now(); result = draft; changed = true; break; }
        case 'rename': { const draft = getDraft(); checkRevision(draft.revision, args.baseRevision); label(args.name); draft.document.name = args.name; draft.revision++; draft.updatedAt = now(); result = draft; changed = true; break; }
        case 'clone': {
          const source = getDraft(); checkRevision(source.revision, args.baseRevision);
          const document = clone(source.document); document.id = 'workspace-' + randomUUID(); document.name = args.name === undefined ? source.document.name.slice(0, 150) + ' copy' : args.name; label(document.name);
          const draft = { id: 'gui-' + randomUUID(), sessionId: context.sessionId || source.sessionId, revision: 1, document: validateGuiDocument(document), createdAt: now(), updatedAt: now() };
          label(draft.sessionId, 256); rememberInstances(draft, document); assert(store.drafts.length < 128, 'Draft limit reached'); store.drafts.push(draft); result = draft; changed = true; break;
        }
        case 'delete': { const draft = getDraft(); checkRevision(draft.revision, args.baseRevision); store.drafts = store.drafts.filter(item => item.id !== draft.id); result = { id: draft.id, deleted: true }; changed = true; break; }
        case 'migrate': { const draft = getDraft(); checkRevision(draft.revision, args.baseRevision); if (draft.document.schemaVersion === 1) { draft.document = migrateGuiDocument(draft.document); rememberInstances(draft, draft.document); draft.revision++; draft.updatedAt = now(); changed = true; } result = draft; break; }
        case 'publish': {
          const draft = getDraft(); checkRevision(draft.revision, args.revision); const digest = hash(draft.document), id = `release-${digest}`;
          let release = store.releases.find(r => r.id === id);
          if (!release) { assert(store.releases.length < 128, 'Release limit reached'); release = { id, hash: digest, draftId: draft.id, draftRevision: draft.revision, document: clone(draft.document), createdAt: now() }; store.releases.push(release); changed = true; }
          result = release; break;
        }
        case 'releases': result = { releases: store.releases }; break;
        case 'export': assert((args.id !== undefined) !== (args.releaseId !== undefined), 'Choose draft or release'); result = { document: args.id !== undefined ? getDraft().document : getRelease().document }; break;
        case 'profile': result = { ...profileState(store.profile), release: store.releases.find(r => r.id === store.profile.releaseId) || null, pages: (store.profile.pageReleaseIds || []).map(id => store.releases.find(r => r.id === id)) }; break;
        case 'apply': case 'restore': {
          checkRevision(store.profile.revision, args.baseRevision);
          const previous = store.releases.find(release => release.id === store.profile.releaseId);
          const next = action === 'restore' ? null : getRelease();
          const profile = profileState(store.profile);
          profile.revision++; profile.releaseId = next?.id || null;
          if (action === 'restore') {
            profile.home = null;
            profile.navigation = { hiddenFeatures: [], orderedFeatures: [] };
          } else if (next.document.kind === 'shell' && previous?.document.kind === 'shell' && previous.document.id === next.document.id) {
            // Published versions have distinct release IDs, but page IDs remain
            // stable within a workspace. Keep user destinations on that page.
            const migrateTarget = target => target?.releaseId === previous.id
              ? (next.document.pages.some(page => page.id === target.pageId) ? { ...target, releaseId: next.id } : null)
              : target;
            profile.home = migrateTarget(profile.home);
            profile.favorites = profile.favorites.map(migrateTarget).filter(Boolean);
          }
          retainAvailableTargets(profile);
          if (next?.document.kind === 'shell' && profile.home === null) profile.home = { releaseId: next.id, pageId: next.document.pages[0].id };
          validateProfilePreferences(profile, store);
          store.profile = profile; result = profile; changed = true; break;
        }
        case 'install-page': case 'remove-page': {
          checkRevision(store.profile.revision, args.baseRevision); const release = getRelease(); assert(release.document.kind === 'page', 'Only page releases can be installed');
          const profile = profileState(store.profile); profile.revision++;
          if (action === 'install-page') { if (!profile.pageReleaseIds.includes(release.id)) profile.pageReleaseIds.push(release.id); }
          else profile.pageReleaseIds = profile.pageReleaseIds.filter(id => id !== release.id);
          retainAvailableTargets(profile); validateProfilePreferences(profile, store); store.profile = profile; result = profile; changed = true; break;
        }
        case 'configure-profile': {
          checkRevision(store.profile.revision, args.baseRevision); const profile = profileState(store.profile); profile.revision++;
          for (const key of ['home', 'favorites', 'navigation']) if (Object.hasOwn(args, key)) profile[key] = clone(args[key]);
          validateProfilePreferences(profile, store); store.profile = profile; result = profile; changed = true; break;
        }
      }
      if (changed) {
        const bytes = JSON.stringify(store); assert(Buffer.byteLength(bytes) <= MAX_STORE, 'GUI store exceeds 8 MiB');
        const temp = `${storePath}.${randomUUID()}.tmp`;
        try { await writeFile(temp, bytes, { mode: 0o600, flag: 'wx' }); if (compromised) throw compromised; await rename(temp, storePath); }
        finally { await rm(temp, { force: true }); }
      }
      return clone(result);
    } finally { await releaseLock(); }
  }
  return { dispatch };
}
