// PAC assignment wakeups never enter the peer-reply or Feishu-broadcast paths.
export function installPacCarrier(ctx, rpc) {
  if (!ctx.agents || !ctx.on || !ctx.interval) return;
  let polling = false, stopped = false, lastWarning = 0;
  const histories = new WeakMap();
  function history(session) {
    if (typeof session?.snapshotEvents !== 'function' || !Number.isSafeInteger(session.seq)) throw new Error('PAC carrier requires snapshotEvents');
    let index = histories.get(session);
    if (!index || index.cursor > session.seq || (index.cursor && session.eventAt && session.eventAt(index.cursor - 1) !== index.last)) index = { cursor: 0, ids: new Set(), active: new Set() };
    const end = session.seq, events = session.snapshotEvents(index.cursor, end);
    if (!Array.isArray(events) || events.length !== end - index.cursor || events.some((e, i) => e?.seq !== index.cursor + i || !e.data)) throw new Error('PAC incomplete session snapshot');
    for (const event of events) {
      if (event.type === 'turn/start') index.active.add(event.data.turn);
      if (event.type === 'turn/end') index.active.delete(event.data.turn);
      if (event.type === 'user/message') index.ids.add(event.data.id);
    }
    index.cursor = end; index.last = events.at(-1) || index.last;
    histories.set(session, index); return index;
  }
  async function poll() {
    if (polling || stopped) return;
    polling = true;
    try {
      const result = await rpc({ operation: 'pac-poll' });
      const selected = new Set();
      const currentIds = new Set((result.jobs || []).map(j => j.messageId));
      for (const sessionId of result.sessionIds || []) {
        const agent = ctx.agents.get(sessionId);
        for (const pending of [...(agent?.inbox?.nextTurn || []), ...(agent?.inbox?.nextStep || [])]) {
          if (pending.source?.kind === 'plugin' && pending.source.plugin === 'dsh-pac' && !currentIds.has(pending.id)) agent.inbox.remove(pending.id);
        }
      }
      for (const job of result.jobs || []) {
        if (stopped || selected.has(job.sessionId)) continue;
        const agent = ctx.agents.get(job.sessionId);
        if (!agent) continue; // Never create/open another session.
        const log = history(agent.session);
        const queued = [...(agent.inbox?.nextTurn || []), ...(agent.inbox?.nextStep || [])].some(m => m.id === job.messageId);
        if (log.ids.has(job.messageId) || queued) {
          if (job.received) continue;
          await rpc({ operation: 'pac-received', sessionId: job.sessionId, graphId: job.graphId, nodeId: job.nodeId, messageId: job.messageId });
          continue;
        }
        if (agent.status === 'running' || log.active.size || job.reserved || job.received || agent.inbox?.nextTurn?.length || agent.inbox?.nextStep?.length) continue;
        const reserved = await rpc({ operation: 'pac-reserve', sessionId: job.sessionId, graphId: job.graphId, nodeId: job.nodeId, expectedToken: job.expectedToken, messageId: job.messageId });
        if (!reserved.accepted) continue;
        selected.add(job.sessionId);
        // Reservation precedes followup. An ambiguous crash is visible as reserved;
        // never enqueue again automatically and risk repeating external work.
        await agent.followup(Object.freeze({ id: job.messageId, role: 'user', source: Object.freeze({ kind: 'plugin', plugin: 'dsh-pac', form: 'notice', summary: 'PAC: ' + job.nodeId }), content: [Object.freeze({ type: 'text', text: job.prompt })] }));
      }
    } catch (error) {
      if (Date.now() - lastWarning > 30000) { lastWarning = Date.now(); console.warn('[dsh-pac]', error.message); }
    } finally { polling = false; }
  }
  ctx.on('dispose', () => { stopped = true; });
  ctx.interval(poll, 3000);
  void poll();
}
