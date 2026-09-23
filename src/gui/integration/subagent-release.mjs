import { randomUUID } from 'node:crypto';

const handlers = new WeakMap();
const fail = (suffix, message) => { throw Object.assign(new Error(message), { code: `SUBAGENT_RELEASE_${suffix}` }); };
const header = agent => agent?.session?.header;
const status = agent => ['idle', 'running'].includes(agent?.status) ? agent.status : 'unknown';
const queued = agent => {
  const turn = agent?.inbox?.nextTurn;
  const step = agent?.inbox?.nextStep;
  return Array.isArray(turn) && Array.isArray(step) ? turn.length + step.length : null;
};

/** Human Host boundary only. Never register this handler as a model tool. */
export function subagentRelease(ctx, input) {
  if (!handlers.has(ctx)) handlers.set(ctx, createSubagentRelease(ctx));
  return handlers.get(ctx)(input);
}

export function createSubagentRelease(ctx, { now = Date.now, ttlMs = 60000, maxPreviews = 32, maxTargets = 512, timeoutMs = 30000 } = {}) {
  const previews = new Map();
  const inFlight = new Set();
  function parentFor(id) {
    const parent = ctx.agents.get(id);
    const meta = header(parent);
    if (!parent || !meta || parent.session.id !== id || meta.origin === 'subagent' || meta.parentSession != null) {
      fail('PARENT_UNAVAILABLE', 'An exact live top-level human parent is required');
    }
    return parent;
  }
  function liveTree(selected) {
    const live = ctx.agents.list();
    const ids = new Set(selected.keys());
    const tree = new Map();
    let changed;
    do {
      changed = false;
      for (const agent of live) {
        const id = agent.session?.id;
        if (!tree.has(id) && (ids.has(id) || ids.has(header(agent)?.parentSession))) {
          if (tree.size >= maxTargets) fail('LIMIT', 'Too many resident descendants for one preview');
          tree.set(id, agent);
          ids.add(id);
          changed = true;
        }
      }
    } while (changed);
    return tree;
  }
  function assertOwned(agent, parentId) {
    if (header(agent)?.origin !== 'subagent' || header(agent)?.parentSession !== parentId) {
      fail('OWNERSHIP_MISMATCH', 'Subagent ownership changed; request a new preview');
    }
  }
  return async function handle(input) {
    if (!input || typeof input !== 'object' || Array.isArray(input)) fail('INVALID_REQUEST', 'Expected an object');
    const allowed = input.operation === 'preview' ? ['operation', 'parentSessionId'] : ['operation', 'parentSessionId', 'previewId', 'confirmed'];
    if (!['preview', 'release'].includes(input.operation) || Object.keys(input).some(key => !allowed.includes(key)) || typeof input.parentSessionId !== 'string' || !input.parentSessionId || input.parentSessionId.length > 256) fail('INVALID_REQUEST', 'Invalid release request');
    // `subagents` is deliberately not declared in `inject`: cordis has no optional
    // dependency form, and a `{ required, optional }` object is read as two services
    // literally named "required"/"optional", which never activate the plugin.
    // Probe lazily instead; strict=false yields undefined when the service is absent.
    const service = ctx.get('subagents', false);
    if (!service || ['listChildren', 'listDescendants', 'drainContinuableChildren'].some(key => typeof service[key] !== 'function') || typeof ctx.agents?.get !== 'function' || typeof ctx.agents?.list !== 'function') {
      return { ok: false, available: false, error: { code: 'SUBAGENT_RELEASE_UNAVAILABLE', message: 'Official selected-child release service is unavailable' } };
    }
    for (const [id, preview] of previews) if (preview.expiresAt <= now()) previews.delete(id);
    const parent = parentFor(input.parentSessionId);
    if (input.operation === 'preview') {
      const directory = await service.listChildren(input.parentSessionId);
      if (!Array.isArray(directory) || directory.some(entry => entry.kind !== 'child')) fail('DIRECTORY_INCOMPLETE', 'Subagent directory is incomplete');
      const direct = directory.filter(entry => entry.mode === 'continuable');
      if (direct.length > maxTargets) fail('LIMIT', 'Too many subagents for one preview');
      const selected = new Map();
      const forest = new Map();
      const observed = new Map();
      const children = [];
      for (const entry of direct) {
        const agent = ctx.agents.get(entry.id);
        if (!agent) continue;
        assertOwned(agent, input.parentSessionId);
        observed.set(entry.id, agent);
        selected.set(entry.id, agent);
        forest.set(entry.id, { agent, parentId: input.parentSessionId });
        children.push({ id: entry.id, label: String(entry.label || entry.id).slice(0, 256), status: status(agent) });
      }
      for (const id of selected.keys()) {
        const descendants = await service.listDescendants(id);
        if (!Array.isArray(descendants) || descendants.some(entry => entry.kind !== 'child')) fail('DIRECTORY_INCOMPLETE', 'Subagent directory is incomplete');
        for (const entry of descendants) {
          const agent = ctx.agents.get(entry.id);
          if (!agent) continue;
          observed.set(entry.id, agent);
          if (observed.size > maxTargets) fail('LIMIT', 'Too many resident descendants for one preview');
          if (entry.mode !== 'continuable') continue;
          assertOwned(agent, entry.parentId);
          forest.set(entry.id, { agent, parentId: entry.parentId });
          if (forest.size > maxTargets) fail('LIMIT', 'Too many resident descendants for one preview');
        }
      }
      if (parentFor(input.parentSessionId) !== parent) fail('STALE_PREVIEW', 'Parent changed during preview');
      for (const [id, item] of forest) if (ctx.agents.get(id) !== item.agent) fail('STALE_PREVIEW', 'Subagents changed during preview');
      const tree = liveTree(selected);
      for (const [id, agent] of tree) if (observed.get(id) !== agent) fail('STALE_PREVIEW', 'Descendants changed during preview');
      const queueCounts = [...forest.values()].map(item => queued(item.agent));
      const counts = { direct: direct.length, resident: selected.size, running: [...forest.values()].filter(item => item.agent.status === 'running').length, descendants: forest.size - selected.size, queued: queueCounts.includes(null) ? null : queueCounts.reduce((a, b) => a + b, 0) };
      const previewId = selected.size ? randomUUID() : null;
      const expiresAt = previewId ? now() + ttlMs : null;
      if (previewId) {
        while (previews.size >= maxPreviews) previews.delete(previews.keys().next().value);
        previews.set(previewId, { parent, parentSessionId: input.parentSessionId, selected, forest, liveTree: tree, expiresAt });
      }
      return { ok: true, available: true, parentSessionId: input.parentSessionId, previewId, expiresAt, children, counts };
    }
    if (input.confirmed !== true || typeof input.previewId !== 'string') fail('CONFIRMATION_REQUIRED', 'A preview and explicit human confirmation are required');
    if (inFlight.has(parent)) fail('BUSY', 'A release is still settling for this parent; do not retry');
    const preview = previews.get(input.previewId);
    if (!preview || preview.parentSessionId !== input.parentSessionId || preview.parent !== parent) fail('STALE_PREVIEW', 'Preview missing, expired, consumed, or parent changed');
    for (const [id, agent] of liveTree(preview.selected)) {
      if (preview.liveTree.get(id) !== agent) fail('STALE_PREVIEW', 'Descendant scope changed; preview again');
    }
    // Validate the entire captured forest before invoking the only destructive primitive.
    for (const [id, item] of preview.forest) {
      const live = ctx.agents.get(id);
      if (live && live !== item.agent) fail('STALE_PREVIEW', 'A subagent resumed with a new identity; preview again');
      if (live) assertOwned(live, item.parentId);
    }
    const present = [...preview.selected.keys()].filter(id => ctx.agents.get(id));
    const absent = new Set([...preview.selected.keys()].filter(id => !ctx.agents.get(id)));
    previews.delete(input.previewId); // Single use even on timeout or partial disposal failure.
    let failure = false;
    let timedOut = false;
    let timer;
    try {
      if (present.length) {
        inFlight.add(parent);
        let draining;
        try {
          // Invoke synchronously after validation: no identity-check-to-drain await gap.
          draining = Promise.resolve(service.drainContinuableChildren(parent, present));
        } catch (error) { inFlight.delete(parent); throw error; }
        const settled = draining.then(
          value => { inFlight.delete(parent); return value; },
          error => { inFlight.delete(parent); throw error; }
        );
        await Promise.race([
          settled,
          new Promise((_, reject) => { timer = setTimeout(() => { timedOut = true; reject(new Error('release timeout')); }, timeoutMs); })
        ]);
      }
    } catch { failure = true; }
    finally { clearTimeout(timer); }
    const results = [...preview.selected].map(([id, agent]) => {
      const live = ctx.agents.get(id);
      if (!live) return { id, status: absent.has(id) ? 'already-released' : 'released' };
      if (live !== agent) return { id, status: 'unknown', observation: 'replaced' };
      return { id, status: timedOut ? 'unknown' : 'failed', observation: 'still-resident' };
    });
    const releasedCount = results.filter(item => item.status === 'released').length;
    const alreadyReleasedCount = results.filter(item => item.status === 'already-released').length;
    const failedCount = results.filter(item => item.status === 'failed').length;
    const unknownCount = results.filter(item => item.status === 'unknown').length;
    const descendantsRemainingCount = [...preview.forest.keys()].filter(id => !preview.selected.has(id) && ctx.agents.get(id)).length;
    // A thrown drain may have failed below a released direct child; never call that complete.
    const complete = !failure && !failedCount && !unknownCount && !descendantsRemainingCount;
    return { ok: true, parentSessionId: input.parentSessionId, results, releasedCount, alreadyReleasedCount, failedCount, unknownCount, descendantsRemainingCount, complete, outcome: timedOut || unknownCount ? 'unknown' : complete ? 'completed' : 'partial' };
  };
}
