#!/usr/bin/env node

import { createHash, randomUUID } from 'node:crypto';
import { spawn } from 'node:child_process';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

const bridge = fileURLToPath(new URL('../h2b-session-bridge.mjs', import.meta.url));
const sessionId = `agent-task-e2e-${randomUUID()}`;
let connected = false;
let allowFrom = '';

function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  }
  return JSON.stringify(value);
}

function digest(value) {
  return 'sha256:' + createHash('sha256').update(canonical(value)).digest('hex');
}

function rpc(operation, body, extra = {}) {
  const request = { operation, sessionId, ...(body === undefined ? {} : { body }), ...extra };
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [bridge, 'rpc'], {
      env: {
        ...process.env,
        ...(allowFrom ? { H2B_DSH_DEMO_ALLOW_FROM: allowFrom } : {}),
      },
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8').on('data', (chunk) => { stdout += chunk; });
    child.stderr.setEncoding('utf8').on('data', (chunk) => { stderr += chunk; });
    child.once('error', reject);
    child.once('close', (code) => {
      let document;
      try { document = JSON.parse(stdout); }
      catch (error) {
        reject(new Error(`bridge returned invalid JSON (${code}): ${stderr || stdout}`, { cause: error }));
        return;
      }
      if (code !== 0 || document?.ok === false) {
        const failure = new Error(document?.error?.message || stderr || `bridge ${operation} failed`);
        failure.code = document?.error?.code || 'BRIDGE_ERROR';
        failure.details = document?.error?.details;
        reject(failure);
        return;
      }
      resolve(document);
    });
    child.stdin.end(JSON.stringify(request));
  });
}

function workflowRpc(operation, body) {
  return rpc(operation, body, { surface: 'mfu-workflow' });
}

async function main() {
  const connectedSession = await rpc('connect');
  connected = true;
  const actor = connectedSession.actor;
  const capabilities = await rpc('agent.task.capabilities', {
    protocolVersion: 1,
    namespace: 'mfu.agent-task.v1',
  });
  allowFrom = capabilities.serviceIdentity.actorUri;
  const workflowCapabilities = await workflowRpc('workflow.capabilities', {});
  if (workflowCapabilities.agentTask !== 'supported') throw new Error('MFU Workflow v2 adapter is unavailable');

  const suffix = Date.now().toString(36);
  const attemptId = `attempt-${suffix}`;
  const metadata = {
    schemaVersion: 'mfu.work-package/v1',
    businessCaseId: `case-${suffix}`,
    workItemId: 'dsh-h2b-real-e2e',
    attemptId,
  };
  const payload = {
    schemaVersion: 'mfu.work-package/v1',
    businessCaseId: metadata.businessCaseId,
    businessCaseReference: `E2E-${suffix}`,
    workItemId: metadata.workItemId,
    attemptId,
    objective: 'Verify the real DSH to H2B agent.task workflow and durable result loop.',
    workPackage: {
      instructions: 'Accept one structured progress event and one explicit final result.',
      inputContext: 'This is a controlled local integration verification.',
      expectedDeliverables: ['Durable H2B result projection'],
      acceptanceCriteria: ['Status becomes completed', 'Result is queryable by targetRef'],
      constraints: ['Do not modify a workspace'],
      collaborationInstructions: 'Use typed agent.task activity envelopes only.',
      workspace: { kind: 'none', reason: 'Transport integration verification needs no repository workspace.' },
      confirmedAt: new Date().toISOString(),
    },
    owner: actor,
    participants: [actor],
    reworkFeedback: null,
  };
  const targets = [{ targetRef: 'owner', target: actor, role: 'owner', delegates: [] }];
  const completion = { kind: 'result.submitted' };
  const externalRef = `mfu:${metadata.businessCaseId}:${metadata.workItemId}:${attemptId}`;
  const started = await workflowRpc('workflow.start', {
    protocolVersion: 1,
    namespace: 'mfu.agent-task.v1',
    externalRef,
    requestDigest: digest({ metadata, payload, targets, completion }),
    metadata,
    payload,
    targets,
    completion,
  });
  const target = started.targets.find((item) => item.targetRef === 'owner');
  if (!target?.conversationId) throw new Error('agent.task.start did not return the owner conversationId');

  await rpc('agent.task.observe', {
    schemaVersion: 'h2b.agent-task.event/v1',
    eventId: `progress-${suffix}`,
    runId: started.runId,
    targetRef: 'owner',
    conversationId: target.conversationId,
    kind: 'progress',
    at: new Date().toISOString(),
    payload: { summary: 'DSH reached the real H2B Workflow run.', percent: 50, phase: 'verify' },
  });

  const result = {
    schemaVersion: 'mfu.work-result/v1',
    attemptId,
    status: 'completed',
    summary: 'DSH and H2B completed the real agent.task workflow integration loop.',
    repository: '',
    branch: '',
    commits: [],
    pullRequests: [],
    artifacts: [{ kind: 'text', value: 'DSH→H2B agent.task real E2E passed' }],
    checks: [{ name: 'agent.task real E2E', status: 'passed', detail: 'start, observe, status and result used the live daemon.' }],
    blockers: [],
  };
  const artifactRefs = [{ kind: 'text', value: 'DSH→H2B agent.task real E2E passed' }];
  const resultRef = `result-${suffix}`;
  await rpc('agent.task.observe', {
    schemaVersion: 'h2b.agent-task.event/v1',
    eventId: `result-event-${suffix}`,
    runId: started.runId,
    targetRef: 'owner',
    conversationId: target.conversationId,
    kind: 'result.submitted',
    at: new Date().toISOString(),
    payload: {
      resultRef,
      resultDigest: digest({ result, artifactRefs }),
      result,
      artifactRefs,
    },
  });

  const status = await workflowRpc('workflow.status', { runId: started.runId });
  const delivered = await workflowRpc('workflow.result', { runId: started.runId, targetRef: 'owner' });
  const pending = await rpc('pending', undefined);
  for (const message of pending.messages || []) {
    await rpc('ack', undefined, { messageId: message.messageId });
  }

  if (status.state !== 'completed') throw new Error(`agent.task status is ${status.state}, expected completed`);
  const deliveredTarget = delivered.targets?.find((item) => item.targetRef === 'owner');
  if (deliveredTarget?.resultRef !== resultRef) throw new Error('agent.task resultRef did not round-trip');
  process.stdout.write(JSON.stringify({
    ok: true,
    adapter: 'mfu-workflow-v2',
    h2b: capabilities.namespace,
    serviceActor: capabilities.serviceIdentity.actorUri,
    dshActor: actor,
    runId: started.runId,
    conversationId: target.conversationId,
    state: status.state,
    resultRef: deliveredTarget.resultRef,
    acknowledgedDispatches: (pending.messages || []).length,
  }, null, 2) + '\n');
}

try {
  await main();
} finally {
  if (connected) {
    try { await rpc('disconnect'); } catch {}
  }
}
