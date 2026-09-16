import { spawn } from "node:child_process";
import { SOURCES, projectSource } from "./sources.mjs";

export function runCli(
  argv,
  { executable = "h2b", timeoutMs = 8000, maxBytes = 1048576 } = {},
) {
  return new Promise((resolve, reject) => {
    const child = spawn(executable, argv, {
      shell: false,
      stdio: ["ignore", "pipe", "pipe"],
      windowsHide: true,
    });
    let size = 0;
    let failure;
    const stdout = [];
    const stderr = [];
    const stop = (code) => {
      failure ||= code;
      child.kill("SIGKILL");
    };
    const timer = setTimeout(() => stop("COMMAND_TIMEOUT"), timeoutMs);
    child.stdout.on("data", (chunk) => {
      size += chunk.length;
      if (size > maxBytes) stop("OUTPUT_TOO_LARGE");
      else stdout.push(chunk);
    });
    child.stderr.on("data", (chunk) => {
      size += chunk.length;
      if (size > maxBytes) stop("OUTPUT_TOO_LARGE");
      else stderr.push(chunk);
    });
    child.on("error", (error) => {
      clearTimeout(timer);
      reject(
        Object.assign(new Error("CLI 无法启动"), {
          code: error.code === "ENOENT" ? "CLI_NOT_FOUND" : "COMMAND_FAILED",
        }),
      );
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      let document;
      try {
        document = JSON.parse(Buffer.concat(stdout).toString("utf8"));
      } catch {}
      if (failure)
        return reject(Object.assign(new Error(failure), { code: failure }));
      if (code !== 0 || document?.ok === false) {
        const reason = document?.error?.code;
        const unsupported = /No such command|No such option/.test(
          Buffer.concat(stderr).toString("utf8"),
        );
        return reject(
          Object.assign(new Error("CLI 查询失败"), {
            code: unsupported
              ? "UNSUPPORTED_COMMAND"
              : reason || "COMMAND_FAILED",
          }),
        );
      }
      if (
        !document ||
        typeof document !== "object" ||
        Array.isArray(document)
      ) {
        return reject(
          Object.assign(new Error("CLI 未返回状态对象"), {
            code: "INVALID_RESPONSE",
          }),
        );
      }
      resolve(document);
    });
  });
}

const ERRORS = {
  CLI_NOT_FOUND: "未找到 H2B CLI",
  COMMAND_TIMEOUT: "状态查询超时",
  OUTPUT_TOO_LARGE: "状态响应超出大小限制",
  INVALID_RESPONSE: "当前 CLI 返回的状态结构无法识别",
  UNSUPPORTED_COMMAND: "当前 H2B 版本不支持此查询",
  DAEMON_UNAVAILABLE: "H2B Daemon 暂不可用",
  DAEMON_RESTORING: "H2B Daemon 正在恢复",
  COMMAND_FAILED: "状态查询失败",
};

export function createReader({
  run = runCli,
  now = Date.now,
  concurrency = 3,
} = {}) {
  const cache = new Map();
  const pending = new Map();
  const queue = [];
  let active = 0;
  function schedule(task) {
    return new Promise((resolve, reject) => {
      queue.push({ task, resolve, reject });
      drain();
    });
  }
  function drain() {
    while (active < concurrency && queue.length) {
      const job = queue.shift();
      active++;
      Promise.resolve()
        .then(job.task)
        .then(job.resolve, job.reject)
        .finally(() => {
          active--;
          drain();
        });
    }
  }
  return {
    async read(operation) {
      if (!Object.hasOwn(SOURCES, operation))
        throw Object.assign(new Error("Unknown source"), {
          code: "UNKNOWN_SOURCE",
        });
      const spec = SOURCES[operation];
      const previous = cache.get(operation);
      if (previous && now() < previous.retryAt) return previous;
      if (pending.has(operation)) return pending.get(operation);
      const request = schedule(async () => {
        const result = {
          operation,
          label: spec.label,
          scope: spec.scope,
          intervalMs: spec.ttl,
        };
        try {
          result.data = projectSource(operation, await run(spec.argv));
          result.updatedAt = now();
          result.error = null;
        } catch (error) {
          result.data = previous?.data ?? null;
          result.updatedAt = previous?.updatedAt ?? null;
          const code = Object.hasOwn(ERRORS, error.code)
            ? error.code
            : "COMMAND_FAILED";
          result.error = { code, message: ERRORS[code] };
        }
        result.checkedAt = now();
        result.retryAt =
          now() + (result.error ? Math.min(spec.ttl, 10000) : spec.ttl);
        cache.set(operation, result);
        return result;
      }).finally(() => pending.delete(operation));
      pending.set(operation, request);
      return request;
    },
  };
}
