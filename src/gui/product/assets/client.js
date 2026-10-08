// Browser enhancement for the SSR shell. Navigation and the composer work
// without this script (links and the plain form POST); this layer upgrades the
// composer to an idempotent in-page send and polls session progress.
// It never sees or chooses an identity: the per-boot token from the URL is the
// only credential, and the server maps it to the trusted context.
(() => {
  const token = new URLSearchParams(location.search).get('token');
  if (!token) return;

  async function apiRequest(input) {
    const response = await fetch('/api/request', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
      body: JSON.stringify(input),
    });
    const body = await response.json();
    if (!body.ok) throw new Error(`${body.error.code}: ${body.error.message}`);
    return body.value;
  }

  const form = document.querySelector('[data-gui-composer]');
  if (form) {
    const textarea = form.querySelector('textarea[name="text"]');
    const button = form.querySelector('button[type="submit"]');
    let idempotencyKey = null;
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const sessionId = form.querySelector('input[name="sessionId"]').value;
      const text = textarea.value;
      if (!text.trim()) return;
      button.disabled = true;
      try {
        // One key per typed draft: a retry after a lost ACK re-sends the same
        // key, and the adapter deduplicates instead of double-sending.
        idempotencyKey = idempotencyKey || crypto.randomUUID();
        await apiRequest({
          service: 'sessions', operation: 'send', sessionId,
          args: { text, idempotencyKey },
        });
        textarea.value = '';
        idempotencyKey = null;
      } catch (error) {
        button.textContent = `发送失败：${error.message}`;
        setTimeout(() => { button.textContent = '发送'; }, 3000);
      } finally {
        button.disabled = false;
      }
    });
    textarea.addEventListener('input', () => { idempotencyKey = null; });
  }

  const current = document.querySelector('.gui-session-link[aria-current="page"]');
  if (current) {
    const sessionId = current.dataset.sessionId;
    let since = 0;
    const log = document.createElement('p');
    log.className = 'gui-progress-log';
    setInterval(async () => {
      try {
        const result = await apiRequest({ service: 'sessions', operation: 'events', sessionId, args: { since } });
        for (const entry of result.entries) {
          since = Math.max(since, entry.seq);
          log.textContent = `${entry.at} ${entry.tool_name}: ${entry.detail}`;
          if (!log.isConnected) document.querySelector('.gui-composer')?.before(log);
        }
      } catch { /* polling is best-effort; the next interval retries */ }
    }, 2000);
  }
})();
