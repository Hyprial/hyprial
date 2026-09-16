import { createDashboardServer } from "../server.mjs";
import { createReader } from "../reader.mjs";
import { SOURCES } from "../sources.mjs";
import { FIXTURES } from "./fixtures.mjs";

const reader = createReader({
  run: async (argv) => {
    const id = Object.keys(SOURCES).find(
      (id) => JSON.stringify(SOURCES[id].argv) === JSON.stringify(argv),
    );
    if (!id) throw new Error("Unexpected operation");
    return structuredClone(FIXTURES[id]);
  },
});
const server = createDashboardServer({
  reader,
  kanban: {
    status: async () => ({ state: 'configured', version: '0.1.6', lastSyncAt: '2026-09-10T03:00:52Z' }),
    board: async () => ({ taskCount: 2, requiresScripts: true, updatedAt: new Date().toISOString(), status: { state: 'configured', version: '0.1.6', lastSyncAt: '2026-09-10T03:00:52Z' },
      html: '<!doctype html><html><body><h1>共享任务示例</h1><button onclick="this.textContent=\'筛选已应用\'">筛选负责人</button><p id="isolation"></p><script>try { parent.document.body; document.getElementById("isolation").textContent="隔离失败"; } catch { document.getElementById("isolation").textContent="已隔离"; }</script></body></html>' }),
  },
  readApps: async () => ({
    apps: { dsh: { ok: true, state: "running", url: "http://127.0.0.1:3180" } },
  }),
});
server.listen(3181, "127.0.0.1");
for (const signal of ["SIGTERM", "SIGINT"])
  process.once(signal, () => {
    server.close();
    server.closeAllConnections();
  });
