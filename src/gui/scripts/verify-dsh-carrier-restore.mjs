import assert from 'node:assert/strict';
import { resolve, join } from 'node:path';
import { pathToFileURL } from 'node:url';

// Real installed DSH controller; synthetic persistence/preset/agent boundaries.
// No user sessions, credentials, model calls or production writes.
export async function verifyDshCarrierRestore(runtime) {
  const base = join(resolve(runtime), 'node_modules/@deepseek-ai/dsh-api-session-controller/lib');
  const { SessionController } = await import(pathToFileURL(join(base, 'index.js')));
  const { ApiSessionAgentController } = await import(pathToFileURL(join(base, 'types/agent.js')));
  const agents = new Map(), order = [];
  let resumes = 0, observationsClosed = 0;
  const ctx = {
    typert: { lookups: { configure() {} }, contexts: { configureHost() {} } },
    sessions: { get() {} },
    agentDefaultModel: { currentSelection: () => ({ provider: 'saved-provider', model: 'saved-model' }) },
    sessionQuery: { async observeSession(id) { return {
      header: { id, cwd: '/synthetic-workspace' },
      projections: { values: { agentPreset: 'saved-custom-preset' } },
      [Symbol.dispose]() { observationsClosed++; },
    }; } },
    get(name) {
      assert.equal(name, 'agentPresets');
      return {
        async resolve(id) { assert.equal(id, 'saved-custom-preset'); return { id }; },
        async mount(agentCtx, id) {
          assert.equal(id, 'saved-custom-preset');
          order.push('mount'); agentCtx.mountedPreset = id;
        },
      };
    },
    agents: {
      get: id => agents.get(id),
      async resume(options) {
        resumes++;
        assert.deepEqual(options.agentOptions, { provider: 'saved-provider', model: 'saved-model' });
        assert.equal(typeof options.setup, 'function');
        const agent = { ctx: {}, session: { id: options.resumeSessionId, header: {} } };
        await options.setup(agent.ctx, agent);
        assert.equal(agent.ctx.mountedPreset, 'saved-custom-preset');
        assert.equal(agent.selectionInstalled, true);
        order.push('publish'); agents.set(agent.session.id, agent);
        return { agent };
      },
    },
  };
  const controller = new ApiSessionAgentController(ctx);
  // Model engine is a boundary here; retain and check actual composition ordering.
  controller.installSelection = agent => { order.push('selection'); agent.selectionInstalled = true; };
  const publicHost = { agents: controller };
  const resolveAgent = id => SessionController.prototype.resolveAgent.call(publicHost, id);
  const [a, b] = await Promise.all([resolveAgent('persisted'), resolveAgent('persisted')]);
  assert.ok(a.agent); assert.equal(a.agent, b.agent);
  assert.equal(resumes, 1); assert.equal(observationsClosed, 1);
  assert.deepEqual(order, ['selection', 'mount', 'publish']);
  assert.equal((await resolveAgent('persisted')).agent, a.agent);
  return { status: 'passed', checks: ['public-resolveAgent-result-shape', 'persisted-preset-composition', 'setup-before-publication', 'concurrent-resume-deduplication', 'observation-disposal'], limitation: 'Synthetic preset and agent boundaries; does not execute shell/file tools or send model requests.' };
}
if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  console.log(JSON.stringify(await verifyDshCarrierRestore(process.argv[2])));
}
