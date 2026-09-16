import { createHash } from 'node:crypto';
import { createKanbanReader, loadKanbanEnvironment } from '../integration/kanban-gui.mjs';
import http from "node:http";
import { readFile, writeFile, rename, unlink } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";
import { createReader, runCli } from "./reader.mjs";
import { SOURCES } from "./sources.mjs";

const DIST = fileURLToPath(new URL("./dist/", import.meta.url));
const HEADERS = {
  "X-Content-Type-Options": "nosniff",
  "Referrer-Policy": "no-referrer",
  "Cache-Control": "no-store",
  "Content-Security-Policy":
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
};
const MIME = {
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
};

export async function publishLaunchInfo(filename, url) {
  if (!filename) return;
  if (!path.isAbsolute(filename))
    throw new Error("H2B_GUI_LAUNCH_INFO_FILE must be absolute");
  const temporary = `${filename}.${process.pid}.tmp`;
  try {
    await writeFile(
      temporary,
      JSON.stringify({ schema: process.env.HYPRIAL_GUI_LAUNCH_INFO_FILE ? "hyprial.gui-launch/v1" : "h2b.gui-launch/v1", app: "dashboard", url }) +
        "\n",
      { mode: 0o600, flag: "wx" },
    );
    await rename(temporary, filename);
  } finally {
    await unlink(temporary).catch(() => {});
  }
}

export function applicationLinks(status, hostname) {
  const record = status?.apps?.dsh;
  if (record?.state !== "running" || record.ok !== true) return { dsh: null };
  try {
    const url = new URL(record.url);
    if (
      url.protocol !== "http:" ||
      url.hostname !== hostname ||
      url.username ||
      url.password ||
      url.pathname !== "/" ||
      url.search ||
      url.hash
    )
      return { dsh: null };
    return { dsh: url.href };
  } catch {
    return { dsh: null };
  }
}

export function createDashboardServer({
  reader = createReader(),
  kanban = createKanbanReader(),
  dist = DIST,
  hostname = "127.0.0.1",
  readApps = () => runCli(["gui", "status", "--json"]),
} = {}) {
  const frames = new Map();
  let appsPending;
  let appsCache;
  let appsExpires = 0;
  return http.createServer(async (req, res) => {
    function json(status, value) {
      res.writeHead(status, {
        ...HEADERS,
        "Content-Type": "application/json; charset=utf-8",
      });
      res.end(req.method === "HEAD" ? undefined : JSON.stringify(value));
    }
    try {
      const url = new URL(req.url, `http://${req.headers.host}`);
      const allowedHosts =
        hostname === "127.0.0.1" ? ["127.0.0.1", "localhost"] : [hostname];
      if (
        !allowedHosts.includes(url.hostname) ||
        url.host !== req.headers.host ||
        req.headers["sec-fetch-site"] === "cross-site" ||
        (req.headers.origin && req.headers.origin !== url.origin)
      ) {
        return json(403, {
          error: { code: "ORIGIN_DENIED", message: "不允许跨站访问" },
        });
      }
      if (!["GET", "HEAD"].includes(req.method)) {
        res.setHeader("Allow", "GET, HEAD");
        return json(405, {
          error: { code: "GUI_READ_ONLY", message: "Dashboard 仅提供只读查询" },
        });
      }
      if (url.search)
        return json(400, {
          error: { code: "INVALID_ARGUMENT", message: "此接口不接受查询参数" },
        });
      const frameMatch = /^\/api\/kanban\/frame\/([a-f0-9]{64})$/.exec(url.pathname);
      if (frameMatch) {
        const frame = frames.get(frameMatch[1]);
        if (!frame) return json(404, { error: { code: "KANBAN_FRAME_EXPIRED", message: "看板快照已过期，请刷新。" } });
        res.writeHead(200, {
          ...HEADERS,
          "Content-Type": "text/html; charset=utf-8",
          "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox" + (frame.requiresScripts ? " allow-scripts; script-src 'unsafe-inline'" : ""),
        });
        return res.end(req.method === "HEAD" ? undefined : frame.html);
      }
      if (url.pathname === "/api/kanban/status" || url.pathname === "/api/kanban/board") {
        if (req.method === "HEAD") return json(200, {});
        try {
          if (url.pathname.endsWith("/status")) return json(200, await kanban.status());
          const { html, ...board } = await kanban.board();
          const digest = createHash('sha256').update(html).update(String(board.requiresScripts)).digest('hex');
          frames.delete(digest);
          frames.set(digest, { html, requiresScripts: board.requiresScripts });
          while (frames.size > 8) frames.delete(frames.keys().next().value);
          return json(200, { ...board, frameUrl: `/api/kanban/frame/${digest}` });
        } catch (error) {
          return json(503, { error: { code: error.code || "KANBAN_UNAVAILABLE", message: error.message || "无法读取本机看板" } });
        }
      }
      if (url.pathname === "/api/capabilities") {
        return json(200, {
          protocolVersion: 1,
          guiMode: "display",
          readOnly: true,
          sources: Object.entries(SOURCES).map(([id, spec]) => ({
            id,
            label: spec.label,
            section: spec.section,
            scope: spec.scope,
            intervalMs: spec.ttl,
          })),
        });
      }
      if (url.pathname === "/api/apps") {
        if (req.method === "HEAD") return json(200, {});
        if (!appsPending && Date.now() >= appsExpires) {
          appsPending = readApps()
            .then(
              (value) => {
                appsCache = value;
              },
              () => {
                appsCache = null;
              },
            )
            .finally(() => {
              appsExpires = Date.now() + 10000;
              appsPending = null;
            });
        }
        if (appsPending) await appsPending;
        return json(200, applicationLinks(appsCache, url.hostname));
      }
      const match = /^\/api\/sources\/([a-z]+)$/.exec(url.pathname);
      if (match && Object.hasOwn(SOURCES, match[1])) {
        // HEAD checks route availability without launching a CLI process.
        if (req.method === "HEAD") return json(200, {});
        return json(200, await reader.read(match[1]));
      }
      // Only the SPA entry and Vite assets are served. No DSH proxy, RPC,
      // arbitrary files, filesystem browsing, fallback API or WS upgrade.
      const asset = /^\/assets\/[A-Za-z0-9_-]+\.(js|css)$/.test(url.pathname);
      if (url.pathname !== "/" && url.pathname !== "/index.html" && !asset) {
        return json(404, {
          error: { code: "NOT_FOUND", message: "没有此入口" },
        });
      }
      const filename = asset
        ? path.join(dist, url.pathname.slice(1))
        : path.join(dist, "index.html");
      let body;
      try {
        body = await readFile(filename);
      } catch {
        return json(503, {
          error: {
            code: "BUILD_REQUIRED",
            message: "请先运行 npm run build:dashboard",
          },
        });
      }
      res.writeHead(200, {
        ...HEADERS,
        "Content-Type": MIME[path.extname(filename)],
      });
      res.end(req.method === "HEAD" ? undefined : body);
    } catch {
      json(400, { error: { code: "BAD_REQUEST", message: "无法处理请求" } });
    }
  });
}

export function parseOptions(args, env = process.env) {
  let host = env.H2B_DASHBOARD_HOST || "127.0.0.1";
  let port = env.H2B_DASHBOARD_PORT || "3081";
  for (let i = 0; i < args.length; i += 2) {
    if (!["--host", "--port"].includes(args[i]) || !args[i + 1])
      throw new Error(
        "用法：npm run start:dashboard -- [--host 127.0.0.1] [--port 3081]",
      );
    if (args[i] === "--host") host = args[i + 1];
    else port = args[i + 1];
  }
  if (!/^\d+$/.test(String(port)) || Number(port) < 1 || Number(port) > 65535)
    throw new Error("端口必须在 1–65535 之间");
  // A concrete address also defines the Host allowlist; never trust arbitrary
  // DNS Host headers. Network publication/auth belongs at an operator proxy.
  if (
    !/^(?:\d{1,3}\.){3}\d{1,3}$/.test(host) ||
    host.split(".").some((part) => Number(part) > 255) ||
    host === "0.0.0.0"
  )
    throw new Error("host 必须是明确的 IPv4 地址（默认 127.0.0.1）");
  return { host, port: Number(port) };
}

if (
  process.argv[1] &&
  pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url
) {
  try {
    const { host, port } = parseOptions(process.argv.slice(2));
    await readFile(path.join(DIST, "index.html"));
    const kanbanEnv = await loadKanbanEnvironment();
    const server = createDashboardServer({ hostname: host, kanban: createKanbanReader({ env: kanbanEnv }) });
    server.on("error", (error) => {
      console.error(`Dashboard 启动失败：${error.code || "SERVER_ERROR"}`);
      process.exitCode = 1;
    });
    server.listen(port, host, async () => {
      const url = `http://${host}:${port}`;
      try {
        await publishLaunchInfo(process.env.H2B_GUI_LAUNCH_INFO_FILE, url);
        console.log(`H2B Dashboard · 只读展示：${url}`);
      } catch (error) {
        console.error(`Dashboard launch information failed: ${error.message}`);
        process.exitCode = 1;
        server.close();
        server.closeAllConnections();
      }
    });
    for (const signal of ["SIGINT", "SIGTERM"])
      process.once(signal, () => {
        server.close();
        server.closeAllConnections();
      });
  } catch (error) {
    console.error(
      error.code === "ENOENT"
        ? "请先运行 npm run build:dashboard"
        : error.message,
    );
    process.exitCode = 1;
  }
}
