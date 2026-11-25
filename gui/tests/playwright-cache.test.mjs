// gui/tests/playwright-cache.test.mjs — scripts/ci/ci-playwright-cache.sh 的契约测试。
// 决策记录：
// - 范围：本文件只新增测试；runtime 实现（ci/gui-cache-runtime）与 workflow 接线
//   （ci/gui-cache-workflow）由兄弟工作包提供，实现缺失时本套测试必须 RED
//   （RED 基线 ci/gui-cache-acceleration 6fd998f57，证据见 PR 正文）。
// - 被测接口：bash scripts/ci/ci-playwright-cache.sh <fallback-dir>，cwd=repo 根。
// - seam 契约（与实现共享规范）：CI_PLAYWRIGHT_CACHE_PROC_MOUNTS 替代 /proc/mounts、
//   CI_PLAYWRIGHT_CACHE_OS_RELEASE 替代 /etc/os-release、CI_PLAYWRIGHT_CACHE_NODE /
//   CI_PLAYWRIGHT_CACHE_NPX 替代 node / npx；成功路径必须把 PLAYWRIGHT_BROWSERS_PATH
//   写入 GITHUB_ENV，并输出 CI_PLAYWRIGHT_CACHE dir=... persistent=yes|no ...
//   version=... cache=hit|miss|repaired|failed 摘要行。
// - fake node / fake npx 是运行时生成到仓库外 tmp 的有界测试替身：延迟上限 5s、
//   可失败、不触网、不读真实 npm/凭据；fake node 只拦截“像 chromium probe”的 JS
//   （同时含 launch 与 chromium），其余调用（如 node -p 读锁定版本）原样转交真实
//   node；probe JS 必须含 chromium launch、executablePath、headless、no-sandbox、
//   本地页面加载（goto/setContent）与 DOM 断言（textContent 等），缺一即拒绝
//   （exit 3），防止无意义 probe 冒充验证。
// - fake npx 的浏览器布局（chromium-*/chrome-linux/chrome + INSTALLATION_COMPLETE）
//   仅为 fake probe 的验证对象；真实 Chromium 用例使用真实布局（chrome-linux64），
//   两者不混用。
// - 版本隔离用最小 fixture repo（复制脚本 + 假 gui/package-lock.json + 指向真实
//   playwright-core 的 symlink）；其余用例读取真实 gui/package-lock.json 的锁定版本。
// - 坏 probe 场景通过“先安装成功、再破坏缓存目录（删 INSTALLATION_COMPLETE、去执行位）”
//   构造，对应实现的 repaired 路径；不伪造安装器行为。
// - 真实 Chromium 用例仅在 GUI_CACHE_REAL_CHROMIUM=1 执行，默认 NOT_RUN skip 不冒充
//   PASS；不依赖 backend：把 playwright-core chromium.executablePath() 上三级的
//   chromium* 目录复制进脚本判定的缓存目录，LD_LIBRARY_PATH=
//   /tmp/wp6-browser-libs/usr/lib/x86_64-linux-gnu，headless/no-sandbox 启动并做
//   本地 DOM 断言，同时断言 fake npx（fail 模式）零调用即缓存命中跳过安装。
// - apt wrapper 转发契约（ci/gui-cache-apt-tests 新增）：fake npx 在 CI_FAKE_NPX_APT=1
//   时经继承的 PATH 解析 apt-get，仅当解析结果与其 exec 目标都不在系统路径下
//   （即脚本注入的 wrapper 包着测试注入的 fake）才调用 update/install；否则只记录
//   refused，真实 apt-get 永不运行、不触网。fake apt-get（置于脚本子进程 PATH 最前，
//   即脚本的 real_apt_get）记录全部参数并有界成功；断言每次调用都带
//   -o Dir::State::lists / -o Dir::Cache::archives 指向版本缓存 apt 目录且 lists/archives
//   两个 partial 目录存在，证明 npx --with-deps 的子进程确实继承 PATH wrapper，
//   而非脚本只设置了无人消费的变量。
// - https sources 升级契约（ci/gui-cache-apt-https 新增，修 CI 冷安装卡 apt Get:）：
//   脚本生成主 one-line sourcelist（仅来自 sources.list）+ sourceparts 目录，
//   sources.list.d 的每个 .list/.sources 以同名同扩展名各自改写为 parts 中的
//   独立文件——两种 apt 格式不混拼（真实 apt-get 对拼接文件报
//   E: Type 'Types:' is not known，exit 100）。仅把 http://deb.debian.org /
//   http://security.debian.org 改为同源 https；fake apt-get 捕获实际
//   Dir::Etc::sourcelist 文件与 Dir::Etc::sourceparts 目录逐文件内容（只捕获
//   脚本传来的 fixture，不读真实配置）。fixture 同时含 http Debian、https
//   Debian（deb822）与自定义 http mirror；断言每次 apt 调用都带新 source
//   flags、main 无 deb822 段、parts 文件名/扩展名与源一致、Debian 源为 https、
//   自定义 mirror 原样、原 fixture 字节不变。所有用例经
//   CI_PLAYWRIGHT_CACHE_APT_ETC 指向 fixture（默认空目录 = 无可读 source 文件），
//   无真实 /etc/apt 参与；无可读 source 文件时脚本不得添加 Dir::Etc::* source flags。
// - scoped apt proxy 契约（ci/gui-cache-apt-proxy 新增）：apt 不自动采用大写
//   HTTP(S)_PROXY（apt-config 实测为空）导致直连慢；脚本仅对 Debian 官方主机
//   注入 Acquire::http(s)::Proxy::<host>——小写优先、回退大写、https 可回退
//   http 配置；不加全局 Proxy；NO_PROXY/no_proxy 匹配 debian.org 或 '*' 或无
//   代理值时零 proxy flags。baseEnv 剥离真实代理变量，fixture 用无效代理值。
// - 安装失败用例补 .invalid- quarantine 目录断言；codename 切换用例锁定 cache key
//   含 VERSION_CODENAME 的隔离行为。
// - 持久/易失 fixture（ci/gui-cache-volatile，修 #2651 CI 失败）：持久 fixture 的
//   mount 行使用非易失 fs（/dev/sdb1 ... ext4），不用 tmpfs 冒充持久卷；非持久
//   fixture 把 SUITE_TMP 本身写成 tmpfs 祖先 mount，测试临时根因此在任何环境都
//   落在易失祖先下——旧实现在 /home 测试目录下碰巧通过、在 CI /tmp 下误判持久
//   （persistent=yes vs 期望 no）。persistent=no 断言不放宽，且 summary 必须保留
//   实际 mount target 与 fs type（persistent=no 不得误报 mount=none）、给出含
//   volatile 的显式 warning；持久 fixture 同时锁定 fstype=ext4 出现在 summary。

import { describe, expect, it, afterAll } from "vitest";
import { spawnSync, spawn } from "node:child_process";
import { createRequire } from "node:module";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..");
const SCRIPT = path.join(REPO_ROOT, "scripts", "ci", "ci-playwright-cache.sh");
const WORKFLOW = path.join(REPO_ROOT, ".forgejo", "workflows", "gate-gui.yml");
const GUI_DIR = path.join(REPO_ROOT, "gui");
const TMP_ROOT = process.env.GUI_CACHE_TEST_TMP_ROOT || os.tmpdir();
const REAL_LIBS = "/tmp/wp6-browser-libs/usr/lib/x86_64-linux-gnu";
const SUITE_TMP = fs.mkdtempSync(path.join(TMP_ROOT, "pwcache-tests-"));

const FAKE_NPX = `#!/usr/bin/env bash
set -u
log="\${CI_FAKE_NPX_LOG:-$(pwd)/fake-npx.log}"
mode="\${CI_FAKE_NPX_MODE:-good}"
delay="\${CI_FAKE_NPX_DELAY_S:-0}"
case "$delay" in ""|*[!0-9.]*) delay=0 ;; esac
case "$mode" in good|empty|fail) ;; *) mode=good ;; esac
awk -v d="$delay" 'BEGIN{exit !(d <= 5)}' || delay=5
wd=no
is_install=no
for a in "$@"; do
  [ "$a" = "install" ] && is_install=yes
  [ "$a" = "--with-deps" ] && wd=yes
done
if [ "$is_install" != "yes" ]; then
  printf 'npx\\tnoninstall\\t-\\t-\\t-\\t-\\t-\\t%s\\n' "$*" >> "$log"
  printf '1.63.0\\n'
  exit 0
fi
pbp="\${PLAYWRIGHT_BROWSERS_PATH:-}"
now() { if [ -r /proc/uptime ]; then awk '{printf "%d", $1 * 1000}' /proc/uptime; else date +%s%3N; fi; }
start=\$(now)
[ "$delay" != "0" ] && sleep "$delay"
eff="$mode"
created=no-pbp
if [ -n "$pbp" ]; then
  case "$eff" in
    empty)
      mkdir -p "$pbp"
      created=empty
      ;;
    good)
      mkdir -p "$pbp/chromium-fake-0001/chrome-linux"
      printf '#!/bin/sh\\necho "Chromium 999.0.0.0 fake"\\n' > "$pbp/chromium-fake-0001/chrome-linux/chrome"
      chmod 755 "$pbp/chromium-fake-0001/chrome-linux/chrome"
      : > "$pbp/chromium-fake-0001/INSTALLATION_COMPLETE"
      created=good
      ;;
    fail)
      created=fail
      ;;
  esac
fi
if [ "\${CI_FAKE_NPX_APT:-}" = "1" ]; then
  resolved="$(command -v apt-get 2>/dev/null || true)"
  case "$resolved" in
    /usr/*|/bin/*|/sbin/*|/usr/local/*|/snap/*)
      printf 'apt\\trefused-real-apt-get\\t%s\\n' "$resolved" >> "$log"
      ;;
    "")
      printf 'apt\\tnot-found\\n' >> "$log"
      ;;
    *)
      target="$(sed -n 's/^exec "\\([^"]*\\)".*/\\1/p' "$resolved" | head -1)"
      case "$target" in
        /usr/*|/bin/*|/sbin/*|/usr/local/*|/snap/*)
          printf 'apt\\trefused-wrapper-targets-real-apt\\t%s\\n' "$target" >> "$log"
          ;;
        "")
          printf 'apt\\trefused-unreadable-wrapper\\t%s\\n' "$resolved" >> "$log"
          ;;
        *)
          "$resolved" update
          "$resolved" install -y libnss3 libatk1.0-0
          ;;
      esac
      ;;
  esac
fi
end=$(now)
printf 'npx\\tinstall\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$wd" "$eff" "$created" "$start" "$end" "$pbp" >> "$log"
[ "$eff" = "fail" ] && exit 1
exit 0
`;

const FAKE_NODE = `#!/usr/bin/env bash
set -u
log="\${CI_FAKE_NODE_LOG:-$(pwd)/fake-node.log}"
mode="\${CI_FAKE_PROBE_MODE:-ok}"
case "$mode" in ok|fail-always) ;; *) mode=ok ;; esac
now() { if [ -r /proc/uptime ]; then awk '{printf "%d", $1 * 1000}' /proc/uptime; else date +%s%3N; fi; }
js=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-e" ]; then js="$a"; fi
  if [ "$a" = "-" ]; then js="$(cat)"; fi
  prev="$a"
done
if [ -z "$js" ]; then
  for a in "$@"; do
    case "$a" in
      *.js|*.mjs|*.cjs)
        if [ -f "$a" ]; then js="$(cat "$a")"; fi
        ;;
    esac
  done
fi
if [ -z "$js" ]; then
  exec "REAL_NODE" "$@"
fi
looks_like_probe=no
printf '%s\\n' "$js" | grep -q "launch" && printf '%s\\n' "$js" | grep -q "chromium" && looks_like_probe=yes
if [ "$looks_like_probe" != "yes" ]; then
  exec "REAL_NODE" "$@"
fi
missing=""
printf '%s\\n' "$js" | grep -q "chromium" || missing="$missing chromium"
printf '%s\\n' "$js" | grep -q "launch" || missing="$missing launch"
printf '%s\\n' "$js" | grep -q "executablePath" || missing="$missing executablePath"
printf '%s\\n' "$js" | grep -q "headless" || missing="$missing headless"
printf '%s\\n' "$js" | grep -q "no-sandbox" || missing="$missing no-sandbox"
printf '%s\\n' "$js" | grep -qE "setContent|goto" || missing="$missing page-content"
printf '%s\\n' "$js" | grep -qE "textContent|innerText|evaluate|getAttribute|innerHTML" || missing="$missing dom-assert"
if [ -n "$missing" ]; then
  printf 'node\\trejected\\tts:%s\\tmissing:%s\\n' "$(now)" "$missing" >> "$log"
  echo "fake-node: rejected non-meaningful probe; missing:$missing" >&2
  exit 3
fi
if [ "$mode" = "fail-always" ]; then
  printf 'node\\tprobe-fail\\tts:%s\\tmode=fail-always\\n' "$(now)" >> "$log"
  echo "fake-node: simulated chromium launch failure (fail-always)" >&2
  exit 1
fi
pbp="\${PLAYWRIGHT_BROWSERS_PATH:-}"
valid=no
if [ -n "$pbp" ] && [ -d "$pbp" ]; then
  for d in "$pbp"/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    case "$name" in
      chromium*)
        if [ -f "$d/INSTALLATION_COMPLETE" ] && [ -d "$d/chrome-linux" ]; then
          if find "$d/chrome-linux" -maxdepth 1 -type f -perm -u+x 2>/dev/null | grep -q .; then
            valid=yes
          fi
        fi
        ;;
    esac
  done
fi
if [ "$valid" != "yes" ]; then
  printf 'node\\tprobe-fail\\tts:%s\\tno-valid-browser\\n' "$(now)" >> "$log"
  echo "fake-node: simulated chromium launch failed; no valid browser under PLAYWRIGHT_BROWSERS_PATH=\${pbp:-<unset>}" >&2
  exit 1
fi
printf 'node\\tprobe-ok\\tts:%s\\tok\\n' "$(now)" >> "$log"
exit 0
`;

const FAKE_APT = `#!/usr/bin/env bash
set -u
log="\${CI_FAKE_APT_LOG:-$(pwd)/fake-apt.log}"
capture="\${CI_FAKE_APT_CAPTURE:-}"
printf 'apt\\t%s\\t%s\\n' "$(date +%s%3N)" "$*" >> "$log"
if [ -n "$capture" ]; then
  sl=""
  sp=""
  for a in "$@"; do
    case "$a" in
      Dir::Etc::sourcelist=*) sl="\${a#Dir::Etc::sourcelist=}" ;;
      Dir::Etc::sourceparts=*) sp="\${a#Dir::Etc::sourceparts=}" ;;
    esac
  done
  {
    printf 'flags-sourcelist=%s\\n' "\${sl:-<none>}"
    if [ -n "$sl" ] && [ -r "$sl" ]; then
      cat -- "$sl"
    fi
    printf 'flags-sourceparts=%s\\n' "\${sp:-<none>}"
    if [ -n "$sp" ] && [ -d "$sp" ]; then
      printf 'sourceparts-entries=%s\\n' "$(find "$sp" -mindepth 1 2>/dev/null | wc -l | tr -d ' ')"
      find "$sp" -maxdepth 1 -type f 2>/dev/null | sort | while IFS= read -r pf; do
        printf 'part-file=%s\\n' "$(basename "$pf")"
        cat -- "$pf"
        printf 'part-file-end\\n'
      done
    fi
    printf 'call-end\\n'
  } >> "$capture"
fi
exit 0
`;

function newCase(id) {
  const dir = fs.mkdtempSync(path.join(SUITE_TMP, `${id}-`));
  const c = { id, dir };
  for (const [key, name] of Object.entries({
    cacheRoot: "ci-cache",
    fallback: "fallback",
    runnerTemp: "runner-temp",
    githubEnv: "github_env",
    mounts: "mounts",
    osRelease: "os-release",
    npxLog: "npx.log",
    nodeLog: "node.log",
    aptLog: "apt.log",
    aptCapture: "apt-capture.log",
    fakeBin: "bin",
    aptEtc: "apt-etc",
  })) {
    c[key] = path.join(dir, name);
  }
  for (const p of [c.cacheRoot, c.fallback, c.runnerTemp, c.fakeBin, c.aptEtc]) fs.mkdirSync(p, { recursive: true });
  for (const p of [c.githubEnv, c.npxLog, c.nodeLog, c.aptLog, c.aptCapture]) fs.writeFileSync(p, "");
  return c;
}

function fakeNpxScript() {
  return FAKE_NPX;
}

function fakeNodeScript() {
  return FAKE_NODE.replace("REAL_NODE", process.execPath);
}

function fakeAptScript() {
  return FAKE_APT;
}

function writeFakes(c, opts = {}) {
  c.npxMode = opts.npxMode || "good";
  c.probeMode = opts.probeMode || "ok";
  c.npxDelayS = opts.npxDelayS || "0";
  c.fakeNpx = path.join(c.dir, "fake-npx");
  c.fakeNode = path.join(c.dir, "fake-node");
  c.fakeApt = path.join(c.fakeBin, "apt-get");
  fs.writeFileSync(c.fakeNpx, fakeNpxScript(), { mode: 0o755 });
  fs.writeFileSync(c.fakeNode, fakeNodeScript(), { mode: 0o755 });
  fs.writeFileSync(c.fakeApt, fakeAptScript(), { mode: 0o755 });
  const mountLine = `/dev/sdb1 ${c.cacheRoot} ext4 rw,relatime 0 0`;
  const mounts = opts.mounts === "mounted"
    ? `proc /proc proc defaults 0 0\n${mountLine}\n`
    : `proc /proc proc defaults 0 0\ntmpfs ${SUITE_TMP} tmpfs rw,nosuid,nodev 0 0\n`;
  fs.writeFileSync(c.mounts, mounts);
  writeOsRelease(c, "bookworm");
  return c;
}

function writeOsRelease(c, codename) {
  fs.writeFileSync(
    c.osRelease,
    `ID=debian\nVERSION_ID=12\nVERSION_CODENAME=${codename}\nPRETTY_NAME="Debian GNU/Linux 12 (${codename})"\n`,
  );
}

function baseEnv() {
  const env = {};
  for (const [key, value] of Object.entries(process.env)) {
    if (value === undefined) continue;
    if (key === "NODE_OPTIONS" || key.startsWith("CI_PLAYWRIGHT_CACHE_") || key.startsWith("CI_FAKE_")) continue;
    if (key === "HTTP_PROXY" || key === "HTTPS_PROXY" || key === "http_proxy" || key === "https_proxy") continue;
    if (key === "NO_PROXY" || key === "no_proxy") continue;
    env[key] = value;
  }
  env.PATH = `${path.dirname(process.execPath)}:${env.PATH || ""}`;
  return env;
}

function buildEnv(c, extraEnv = {}) {
  const env = {
    ...baseEnv(),
    CI_CACHE_ROOT: c.cacheRoot,
    GITHUB_ENV: c.githubEnv,
    RUNNER_TEMP: c.runnerTemp,
    CI_PLAYWRIGHT_CACHE_PROC_MOUNTS: c.mounts,
    CI_PLAYWRIGHT_CACHE_OS_RELEASE: c.osRelease,
    CI_PLAYWRIGHT_CACHE_APT_ETC: c.aptEtc,
    CI_PLAYWRIGHT_CACHE_NODE: c.fakeNode,
    CI_PLAYWRIGHT_CACHE_NPX: c.fakeNpx,
    CI_FAKE_NPX_LOG: c.npxLog,
    CI_FAKE_NODE_LOG: c.nodeLog,
    CI_FAKE_APT_LOG: c.aptLog,
    CI_FAKE_APT_CAPTURE: c.aptCapture,
    CI_FAKE_NPX_MODE: c.npxMode,
    CI_FAKE_PROBE_MODE: c.probeMode,
    CI_FAKE_NPX_DELAY_S: c.npxDelayS,
  };
  for (const [key, value] of Object.entries(extraEnv)) {
    if (value === null || value === undefined) delete env[key];
    else env[key] = value;
  }
  return env;
}

function runScript(c, opts = {}) {
  const r = spawnSync("bash", [opts.scriptPath || SCRIPT, opts.fallbackOverride || c.fallback], {
    env: buildEnv(c, opts.extraEnv || {}),
    cwd: opts.cwd || REPO_ROOT,
    encoding: "utf8",
    timeout: opts.timeoutMs || 60000,
  });
  const code = typeof r.status === "number" ? r.status : -1;
  const stdout = r.stdout || "";
  const stderr = r.stderr || "";
  return { code, stdout, stderr, all: `${stdout}\n${stderr}` };
}

function runScriptAsync(c, opts = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn("bash", [opts.scriptPath || SCRIPT, opts.fallbackOverride || c.fallback], {
      env: buildEnv(c, opts.extraEnv || {}),
      cwd: opts.cwd || REPO_ROOT,
    });
    let stdout = "";
    let stderr = "";
    const timer = setTimeout(() => child.kill("SIGKILL"), opts.timeoutMs || 120000);
    child.stdout.on("data", (d) => { stdout += d; });
    child.stderr.on("data", (d) => { stderr += d; });
    child.on("error", (e) => { clearTimeout(timer); reject(e); });
    child.on("close", (code) => {
      clearTimeout(timer);
      const cs = typeof code === "number" ? code : -1;
      resolve({ code: cs, stdout, stderr, all: `${stdout}\n${stderr}` });
    });
  });
}

function resetLogs(c) {
  for (const p of [c.githubEnv, c.npxLog, c.nodeLog, c.aptLog, c.aptCapture]) fs.writeFileSync(p, "");
}

function githubEnvValue(file, key) {
  if (!fs.existsSync(file)) return null;
  const line = fs.readFileSync(file, "utf8").split("\n").find((l) => l.startsWith(`${key}=`));
  return line ? line.slice(key.length + 1).trim() : null;
}

function npxInstallLog(c) {
  if (!fs.existsSync(c.npxLog)) return [];
  return fs
    .readFileSync(c.npxLog, "utf8")
    .split("\n")
    .filter((l) => l.includes("\t"))
    .map((l) => l.split("\t"))
    .filter((cols) => cols[0] === "npx" && cols[1] === "install")
    .map((cols) => ({
      wd: cols[2],
      eff: cols[3],
      created: cols[4],
      start: Number(cols[5]),
      end: Number(cols[6]),
      pbp: cols[7],
    }));
}

function nodeLog(c) {
  if (!fs.existsSync(c.nodeLog)) return [];
  return fs
    .readFileSync(c.nodeLog, "utf8")
    .split("\n")
    .filter((l) => l.includes("\t"))
    .map((l) => l.split("\t"))
    .filter((cols) => cols[0] === "node")
    .map((cols) => ({
      event: cols[1],
      ts: Number(String(cols[2]).replace(/^ts:/, "")) || 0,
      detail: cols[3] || "",
    }));
}

function aptCallLog(c) {
  if (!fs.existsSync(c.aptLog)) return [];
  return fs
    .readFileSync(c.aptLog, "utf8")
    .split("\n")
    .filter((l) => l.includes("\t"))
    .map((l) => l.split("\t"))
    .filter((cols) => cols[0] === "apt")
    .map((cols) => ({
      ts: Number(cols[1]) || 0,
      args: cols.slice(2).join("\t"),
    }));
}

function writeAptFixture(c) {
  fs.mkdirSync(path.join(c.aptEtc, "sources.list.d"), { recursive: true });
  const files = {
    [path.join(c.aptEtc, "sources.list")]: [
      "deb http://deb.debian.org/debian bookworm main",
      "deb http://security.debian.org/debian-security bookworm-security main",
      "",
    ].join("\n"),
    [path.join(c.aptEtc, "sources.list.d", "debian.sources")]: [
      "Types: deb",
      "URIs: https://deb.debian.org/debian",
      "Suites: trixie",
      "Components: main",
      "",
      "",
    ].join("\n"),
    [path.join(c.aptEtc, "sources.list.d", "custom.list")]: [
      "deb http://mirrors.example.com/debian bookworm main",
      "",
    ].join("\n"),
  };
  for (const [p, content] of Object.entries(files)) fs.writeFileSync(p, content);
  c.aptEtcFiles = Object.keys(files);
  c.aptEtcBytes = c.aptEtcFiles.map((p) => fs.readFileSync(p));
  return c;
}

function aptCaptureCalls(c) {
  if (!fs.existsSync(c.aptCapture)) return [];
  return fs
    .readFileSync(c.aptCapture, "utf8")
    .split("call-end\n")
    .filter((b) => b.trim().length > 0)
    .map((block) => {
      const lines = block.split("\n").filter((l) => l.length > 0);
      const value = (prefix) => (lines.find((l) => l.startsWith(prefix)) || "").slice(prefix.length);
      const slIdx = lines.findIndex((l) => l.startsWith("flags-sourcelist="));
      const spIdx = lines.findIndex((l) => l.startsWith("flags-sourceparts="));
      const entriesIdx = lines.findIndex((l) => l.startsWith("sourceparts-entries="));
      const main = lines.slice(slIdx + 1, spIdx === -1 ? lines.length : spIdx);
      const partFiles = [];
      let current = null;
      for (let i = entriesIdx + 1; i < lines.length; i++) {
        const l = lines[i];
        if (l.startsWith("part-file=")) {
          current = { name: l.slice("part-file=".length), content: [] };
          partFiles.push(current);
        } else if (l === "part-file-end") {
          current = null;
        } else if (current) {
          current.content.push(l);
        }
      }
      return {
        flagsSourceList: value("flags-sourcelist="),
        flagsSourceParts: value("flags-sourceparts="),
        entries: value("sourceparts-entries="),
        main,
        partFiles,
      };
    });
}

function isValidBrowserLayout(pbp) {
  if (!pbp || !fs.existsSync(pbp)) return false;
  for (const name of fs.readdirSync(pbp)) {
    if (!/^chromium/i.test(name)) continue;
    const d = path.join(pbp, name);
    if (!fs.existsSync(path.join(d, "INSTALLATION_COMPLETE"))) continue;
    const binDir = path.join(d, "chrome-linux");
    if (!fs.existsSync(binDir)) continue;
    let hasExec = false;
    for (const f of fs.readdirSync(binDir)) {
      try {
        if (fs.statSync(path.join(binDir, f)).mode & 0o111) hasExec = true;
      } catch {}
    }
    if (hasExec) return true;
  }
  return false;
}

function corruptBrowserLayout(pbp) {
  let corrupted = 0;
  for (const name of fs.readdirSync(pbp)) {
    if (!/^chromium/i.test(name)) continue;
    const d = path.join(pbp, name);
    fs.rmSync(path.join(d, "INSTALLATION_COMPLETE"), { force: true });
    const binDir = path.join(d, "chrome-linux");
    if (fs.existsSync(binDir)) {
      for (const f of fs.readdirSync(binDir)) fs.chmodSync(path.join(binDir, f), 0o644);
    }
    corrupted += 1;
  }
  return corrupted;
}

function pinnedVersion() {
  const lock = JSON.parse(fs.readFileSync(path.join(GUI_DIR, "package-lock.json"), "utf8"));
  const entry = lock.packages && lock.packages["node_modules/playwright-core"];
  if (entry && entry.version) return entry.version;
  if (Array.isArray(lock.dependencies)) {
    const dep = lock.dependencies.find((d) => d && d.name === "playwright-core");
    if (dep && dep.version) return dep.version;
  }
  throw new Error("playwright-core version not found in gui/package-lock.json");
}

function writeFixtureLock(fixtureDir, version) {
  const lock = {
    name: "gui-fixture",
    version: "0.0.0",
    lockfileVersion: 3,
    requires: true,
    packages: {
      "": { name: "gui-fixture", version: "0.0.0", dependencies: {} },
      "node_modules/playwright-core": {
        version,
        resolved: `https://registry.npmjs.org/playwright-core/-/playwright-core-${version}.tgz`,
        dev: true,
      },
    },
  };
  fs.writeFileSync(path.join(fixtureDir, "gui", "package-lock.json"), `${JSON.stringify(lock, null, 2)}\n`);
}

function makeFixtureRepo(id, version) {
  const dir = path.join(SUITE_TMP, `${id}-repo`);
  fs.mkdirSync(path.join(dir, "scripts", "ci"), { recursive: true });
  fs.mkdirSync(path.join(dir, "gui", "node_modules"), { recursive: true });
  fs.copyFileSync(SCRIPT, path.join(dir, "scripts", "ci", "ci-playwright-cache.sh"));
  fs.chmodSync(path.join(dir, "scripts", "ci", "ci-playwright-cache.sh"), 0o755);
  writeFixtureLock(dir, version);
  const realCore = path.join(GUI_DIR, "node_modules", "playwright-core");
  if (!fs.existsSync(path.join(realCore, "index.mjs"))) {
    throw new Error(`real playwright-core missing under ${realCore} (run npm --prefix gui ci first)`);
  }
  fs.symlinkSync(realCore, path.join(dir, "gui", "node_modules", "playwright-core"), "dir");
  return { dir, script: path.join(dir, "scripts", "ci", "ci-playwright-cache.sh") };
}

function jobBlock(text, job) {
  const lines = text.split("\n");
  const start = lines.findIndex((l) => new RegExp(`^  ${job}:\\s*$`).test(l));
  if (start === -1) return null;
  let end = lines.length;
  for (let i = start + 1; i < lines.length; i++) {
    if (/^ {2}[A-Za-z][A-Za-z0-9_-]*:\s*(#.*)?$/.test(lines[i])) {
      end = i;
      break;
    }
  }
  return lines.slice(start, end).join("\n");
}

afterAll(() => {
  fs.rmSync(SUITE_TMP, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 });
}, 120000);

describe("ci-playwright-cache.sh", () => {
  it("exists at scripts/ci/ci-playwright-cache.sh", () => {
    expect(fs.existsSync(SCRIPT), `${SCRIPT} missing`).toBe(true);
  });

  it("provisions once into the persistent cache, then a valid cache skips npx entirely", () => {
    const c = writeFakes(newCase("provision"), { npxMode: "good", mounts: "mounted" });
    const r1 = runScript(c, { timeoutMs: 90000 });
    expect(r1.code, r1.all).toBe(0);
    expect(r1.all).toContain("persistent=yes");
    expect(r1.all).toContain(`mount=${c.cacheRoot}`);
    expect(r1.all).toMatch(/fstype=ext4\b/);
    expect(r1.all).toMatch(/cache=miss\b/);
    const pbp1 = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(pbp1, "PLAYWRIGHT_BROWSERS_PATH missing from GITHUB_ENV").toBeTruthy();
    expect(pbp1.startsWith(c.cacheRoot), `expected under cache root, got ${pbp1}`).toBe(true);
    expect(pbp1).toContain(pinnedVersion());
    expect(isValidBrowserLayout(pbp1), `invalid browser layout at ${pbp1}`).toBe(true);
    resetLogs(c);
    const r2 = runScript(c, { timeoutMs: 90000 });
    expect(r2.code, r2.all).toBe(0);
    expect(r2.all).toMatch(/cache=hit\b/);
    expect(npxInstallLog(c), "cache hit must not invoke npx").toHaveLength(0);
    expect(githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH")).toBe(pbp1);
    expect(nodeLog(c).some((e) => e.event === "probe-ok"), "no probe ran on cache hit").toBe(true);
  }, 120000);

  it("exports PLAYWRIGHT_BROWSERS_PATH to GITHUB_ENV as an existing valid browser dir", () => {
    const c = writeFakes(newCase("github-env"), { npxMode: "good", mounts: "mounted" });
    const r = runScript(c, { timeoutMs: 90000 });
    expect(r.code, r.all).toBe(0);
    const raw = fs.readFileSync(c.githubEnv, "utf8");
    expect(raw).toMatch(/^PLAYWRIGHT_BROWSERS_PATH=\S+\n/m);
    const pbp = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(fs.statSync(pbp).isDirectory(), `${pbp} is not a directory`).toBe(true);
    expect(isValidBrowserLayout(pbp)).toBe(true);
  }, 120000);

  it("reinstalls with --with-deps and re-verifies after a failed probe (repaired)", () => {
    const c = writeFakes(newCase("repair"), { npxMode: "good", mounts: "mounted" });
    const r1 = runScript(c, { timeoutMs: 90000 });
    expect(r1.code, r1.all).toBe(0);
    const pbp = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(pbp).toBeTruthy();
    expect(corruptBrowserLayout(pbp), "nothing to corrupt").toBeGreaterThanOrEqual(1);
    expect(isValidBrowserLayout(pbp), "corruption did not take effect").toBe(false);
    resetLogs(c);
    const r2 = runScript(c, { timeoutMs: 120000 });
    expect(r2.code, r2.all).toBe(0);
    expect(r2.all).toMatch(/cache=repaired\b/);
    const installs = npxInstallLog(c);
    expect(installs.length, "expected a reinstall").toBeGreaterThanOrEqual(1);
    const fails = nodeLog(c).filter((e) => e.event === "probe-fail");
    expect(fails.length, "expected a failed probe before reinstall").toBeGreaterThanOrEqual(1);
    const firstFailTs = Math.min(...fails.map((e) => e.ts));
    const wdInstalls = installs.filter((i) => i.wd === "yes" && i.start + 20 >= firstFailTs);
    expect(wdInstalls.length, "no --with-deps install after probe failure").toBeGreaterThanOrEqual(1);
    const wdEnd = Math.max(...wdInstalls.map((i) => i.end));
    const okAfter = nodeLog(c).filter((e) => e.event === "probe-ok" && e.ts + 20 >= wdEnd);
    expect(okAfter.length, "no successful second verification after reinstall").toBeGreaterThanOrEqual(1);
    expect(githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH")).toBeTruthy();
  }, 150000);

  it("exits non-zero when the cache is empty and the install fails", () => {
    const c = writeFakes(newCase("installfail"), { npxMode: "fail", mounts: "mounted" });
    const r = runScript(c, { timeoutMs: 120000 });
    expect(r.code, `expected failure, got ${r.code}: ${r.all}`).toBe(1);
    expect(r.all).toMatch(/cache=failed\b/);
    expect(githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH"), "failed run must not export PLAYWRIGHT_BROWSERS_PATH").toBeNull();
    const quarantined = fs
      .readdirSync(c.cacheRoot)
      .filter((name) => name.startsWith(".invalid-") && name.includes(pinnedVersion()));
    expect(quarantined.length, `no .invalid- quarantine dir under ${c.cacheRoot}: ${r.all}`).toBeGreaterThanOrEqual(1);
  }, 150000);

  it("fails bounded when the probe never recovers after reinstall", () => {
    const c = writeFakes(newCase("probefail"), { npxMode: "good", probeMode: "fail-always", mounts: "mounted" });
    const r = runScript(c, { timeoutMs: 120000 });
    expect(r.code, `expected non-zero exit, got ${r.code}: ${r.all}`).toBeGreaterThan(0);
    expect(r.all).toMatch(/cache=failed\b/);
    expect(npxInstallLog(c).length).toBeGreaterThanOrEqual(1);
    expect(nodeLog(c).some((e) => e.event === "probe-fail")).toBe(true);
  }, 150000);

  it("reports persistent=no for volatile (tmpfs) ancestor mounts and uses the job-local fallback, then reuses it", () => {
    const c = writeFakes(newCase("fallback"), { npxMode: "good", mounts: "plain" });
    const r1 = runScript(c, { timeoutMs: 90000 });
    expect(r1.code, r1.all).toBe(0);
    expect(r1.all).toContain("persistent=no");
    expect(r1.all, "summary must keep the actual ancestor mount, not mount=none").toContain(`mount=${SUITE_TMP}`);
    expect(r1.all).toMatch(/fstype=tmpfs\b/);
    expect(r1.all).toContain("::warning::");
    expect(r1.all, "writable-but-volatile fallback must warn explicitly").toMatch(/volatile/i);
    const pbp1 = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(pbp1.startsWith(c.fallback), `expected under fallback dir, got ${pbp1}`).toBe(true);
    expect(isValidBrowserLayout(pbp1)).toBe(true);
    resetLogs(c);
    const r2 = runScript(c, { timeoutMs: 90000 });
    expect(r2.code, r2.all).toBe(0);
    expect(r2.all).toMatch(/cache=hit\b/);
    expect(npxInstallLog(c), "second run must not reinstall").toHaveLength(0);
    expect(githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH")).toBe(pbp1);
  }, 120000);

  it("serializes concurrent provisioning so install windows never overlap", async () => {
    const c = writeFakes(newCase("flock"), { npxMode: "good", npxDelayS: "0.8", mounts: "mounted" });
    const runs = await Promise.all(
      [0, 1, 2].map((i) =>
        runScriptAsync(c, {
          fallbackOverride: path.join(c.dir, `fb-${i}`),
          extraEnv: { GITHUB_ENV: path.join(c.dir, `gh-${i}`) },
          timeoutMs: 120000,
        }),
      ),
    );
    for (const [i, r] of runs.entries()) {
      expect(r.code, `run ${i} failed: ${r.all}`).toBe(0);
      expect(githubEnvValue(path.join(c.dir, `gh-${i}`), "PLAYWRIGHT_BROWSERS_PATH")).toBeTruthy();
    }
    const installs = npxInstallLog(c);
    expect(installs.length, "expected at least one install").toBeGreaterThanOrEqual(1);
    const sorted = [...installs].sort((a, b) => a.start - b.start);
    for (let i = 1; i < sorted.length; i++) {
      expect(sorted[i].start - sorted[i - 1].end, "install windows overlap: flock missing").toBeGreaterThanOrEqual(-30);
    }
  }, 180000);

  it("isolates the cache when the locked playwright-core version changes", () => {
    const fixture = makeFixtureRepo("version-iso", "1.64.0");
    const c = writeFakes(newCase("version-iso"), { npxMode: "good", mounts: "mounted" });
    const r1 = runScript(c, { cwd: fixture.dir, scriptPath: fixture.script, timeoutMs: 90000 });
    expect(r1.code, r1.all).toBe(0);
    const pbp1 = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(pbp1, "first provision failed").toBeTruthy();
    expect(pbp1).toContain("1.64.0");
    writeFixtureLock(fixture.dir, "1.65.0");
    resetLogs(c);
    const r2 = runScript(c, { cwd: fixture.dir, scriptPath: fixture.script, timeoutMs: 90000 });
    expect(r2.code, r2.all).toBe(0);
    expect(npxInstallLog(c).length, "version change must not reuse the old cache").toBeGreaterThanOrEqual(1);
    const pbp2 = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(pbp2).toContain("1.65.0");
    expect(pbp2).not.toContain("1.64.0");
    expect(pbp2).not.toBe(pbp1);
  }, 150000);

  it("forwards apt state dirs and https Debian sources to npx subprocesses through the PATH wrapper", () => {
    const c = writeAptFixture(writeFakes(newCase("apt-wrapper"), { npxMode: "good", mounts: "mounted" }));
    const r = runScript(c, {
      timeoutMs: 90000,
      extraEnv: {
        CI_FAKE_NPX_APT: "1",
        PATH: `${c.fakeBin}:${baseEnv().PATH}`,
      },
    });
    expect(r.code, r.all).toBe(0);
    expect(r.all).toMatch(/cache=miss\b/);
    const pbp = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(pbp, "PLAYWRIGHT_BROWSERS_PATH missing").toBeTruthy();
    const versionDir = path.dirname(pbp);
    const calls = aptCallLog(c);
    expect(calls.length, `expected apt calls through the wrapper, got ${JSON.stringify(calls)}`).toBeGreaterThanOrEqual(2);
    const listsFlag = `-o Dir::State::lists=${path.join(versionDir, "apt", "lists")}`;
    const archivesFlag = `-o Dir::Cache::archives=${path.join(versionDir, "apt", "archives")}`;
    for (const call of calls) {
      expect(call.args, `apt call missing state-dir flags: ${call.args}`).toContain(listsFlag);
      expect(call.args, `apt call missing archive flags: ${call.args}`).toContain(archivesFlag);
      expect(call.args, `apt call missing https sourcelist flag: ${call.args}`).toContain("Dir::Etc::sourcelist=");
      expect(call.args, `apt call missing sourceparts flag: ${call.args}`).toContain("Dir::Etc::sourceparts=");
    }
    expect(calls.some((call) => call.args.endsWith("update")), "apt-get update never reached the wrapper").toBe(true);
    expect(calls.some((call) => call.args.includes("install")), "apt-get install never reached the wrapper").toBe(true);
    expect(fs.existsSync(path.join(versionDir, "apt", "lists", "partial")), "apt lists/partial dir missing").toBe(true);
    expect(fs.existsSync(path.join(versionDir, "apt", "archives", "partial")), "apt archives/partial dir missing").toBe(true);
    const caps = aptCaptureCalls(c);
    expect(caps.length, "capture count must match apt calls").toBe(calls.length);
    for (const cap of caps) {
      expect(cap.flagsSourceList, "sourcelist flag must point at a generated file, not /etc/apt").not.toContain("/etc/apt");
      expect(cap.flagsSourceList).not.toBe("<none>");
      expect(cap.flagsSourceParts).not.toBe("<none>");
      expect(cap.main.join("\n"), "main sourcelist must stay one-line format (no deb822 stanza keys)").not.toMatch(/^Types:/m);
      expect(cap.main.join("\n")).toContain("deb https://deb.debian.org/debian bookworm main");
      expect(cap.main.join("\n")).toContain("deb https://security.debian.org/debian-security bookworm-security main");
      expect(cap.entries, "each fixture part source must land as its own file in sourceparts").toBe("2");
      const names = cap.partFiles.map((p) => p.name).sort();
      expect(names, "part files must keep name and extension").toEqual(["custom.list", "debian.sources"]);
      const deb822 = cap.partFiles.find((p) => p.name === "debian.sources");
      expect(deb822.content.join("\n")).toContain("URIs: https://deb.debian.org/debian");
      expect(deb822.content.join("\n")).not.toContain("http://deb.debian.org");
      const custom = cap.partFiles.find((p) => p.name === "custom.list");
      expect(custom.content.join("\n")).toContain("deb http://mirrors.example.com/debian bookworm main");
      const allContent = [cap.main.join("\n"), ...cap.partFiles.map((p) => p.content.join("\n"))].join("\n");
      expect(allContent).not.toContain("http://deb.debian.org");
      expect(allContent).not.toContain("http://security.debian.org");
    }
    for (const [i, p] of c.aptEtcFiles.entries()) {
      expect(fs.readFileSync(p).equals(c.aptEtcBytes[i]), `fixture ${p} was modified`).toBe(true);
    }
  }, 120000);

  it("keeps apt source flags unchanged when no readable apt source files exist", () => {
    const runNoSources = (id, removeAptEtc) => {
      const c = writeFakes(newCase(id), { npxMode: "good", mounts: "mounted" });
      if (removeAptEtc) fs.rmSync(c.aptEtc, { recursive: true, force: true });
      const r = runScript(c, {
        timeoutMs: 90000,
        extraEnv: {
          CI_FAKE_NPX_APT: "1",
          PATH: `${c.fakeBin}:${baseEnv().PATH}`,
        },
      });
      expect(r.code, r.all).toBe(0);
      return { label: removeAptEtc ? "missing-dir" : "empty-dir", calls: aptCallLog(c) };
    };
    const results = [
      runNoSources("apt-nosources-missing", true),
      runNoSources("apt-nosources-empty", false),
    ];
    for (const { label, calls } of results) {
      expect(calls.length, `${label}: expected apt calls`).toBeGreaterThanOrEqual(2);
      for (const call of calls) {
        expect(call.args, `${label}: no source flags allowed without sources: ${call.args}`).not.toContain("Dir::Etc::sourcelist=");
        expect(call.args, `${label}: no source flags allowed without sources: ${call.args}`).not.toContain("Dir::Etc::sourceparts=");
      }
    }
  }, 120000);

  it("scopes apt proxy to Debian official hosts from standard proxy env vars", () => {
    const FIXTURE_PROXY = "http://fixture-proxy.invalid:3128";
    const runApt = (id, extraEnv) => {
      const c = writeFakes(newCase(id), { npxMode: "good", mounts: "mounted" });
      const r = runScript(c, {
        timeoutMs: 90000,
        extraEnv: { CI_FAKE_NPX_APT: "1", PATH: `${c.fakeBin}:${baseEnv().PATH}`, ...extraEnv },
      });
      expect(r.code, r.all).toBe(0);
      return aptCallLog(c).map((call) => call.args);
    };
    const expectScopedProxy = (label, argsList, proxy) => {
      expect(argsList.length, `${label}: apt calls`).toBeGreaterThanOrEqual(2);
      expect(argsList[0], `${label}: state cache flags must survive`).toContain("Dir::State::lists=");
      for (const args of argsList) {
        for (const scheme of ["http", "https"]) {
          for (const host of ["deb.debian.org", "security.debian.org"]) {
            expect(args, `${label}: missing scoped ${scheme} proxy for ${host}: ${args}`).toContain(
              `Acquire::${scheme}::Proxy::${host}=${proxy}`,
            );
          }
        }
        expect(args, `${label}: global proxy must stay unset: ${args}`).not.toMatch(/Acquire::(http|https)::Proxy=(?!:)/);
        const hosts = [...args.matchAll(/Acquire::[a-z]+::Proxy::([^=]+)=/g)].map((m) => m[1]);
        for (const host of hosts) {
          expect(["deb.debian.org", "security.debian.org"], `${label}: unexpected proxied host ${host}`).toContain(host);
        }
      }
    };
    expectScopedProxy("uppercase", runApt("apt-proxy-upper", { HTTP_PROXY: FIXTURE_PROXY, HTTPS_PROXY: FIXTURE_PROXY }), FIXTURE_PROXY);
    expectScopedProxy("lowercase-priority", runApt("apt-proxy-lower", {
      HTTP_PROXY: "http://uppercase-unused.invalid:1",
      HTTPS_PROXY: "http://uppercase-unused.invalid:1",
      http_proxy: FIXTURE_PROXY,
      https_proxy: FIXTURE_PROXY,
    }), FIXTURE_PROXY);
    expectScopedProxy("https-falls-back-to-http", runApt("apt-proxy-http-only", { http_proxy: FIXTURE_PROXY }), FIXTURE_PROXY);
    const bare = runApt("apt-proxy-none", {});
    const noproxy = runApt("apt-proxy-noproxy", { HTTP_PROXY: FIXTURE_PROXY, HTTPS_PROXY: FIXTURE_PROXY, NO_PROXY: "debian.org" });
    const star = runApt("apt-proxy-noproxy-star", { http_proxy: FIXTURE_PROXY, no_proxy: "*" });
    for (const [label, argsList] of [["no-proxy-env", bare], ["noproxy-host", noproxy], ["noproxy-star", star]]) {
      for (const args of argsList) {
        expect(args, `${label}: no Debian scoped proxy allowed: ${args}`).not.toContain("Proxy::deb.debian.org=");
        expect(args, `${label}: no security scoped proxy allowed: ${args}`).not.toContain("Proxy::security.debian.org=");
        expect(args, `${label}: no global proxy allowed: ${args}`).not.toMatch(/Acquire::(http|https)::Proxy=(?!:)/);
      }
    }
  }, 150000);

  it("isolates the cache when the OS codename changes", () => {
    const c = writeFakes(newCase("codename-iso"), { npxMode: "good", mounts: "mounted" });
    const r1 = runScript(c, { timeoutMs: 90000 });
    expect(r1.code, r1.all).toBe(0);
    const pbp1 = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(pbp1, "first provision failed").toBeTruthy();
    expect(pbp1).toContain("bookworm");
    writeOsRelease(c, "trixie");
    resetLogs(c);
    const r2 = runScript(c, { timeoutMs: 90000 });
    expect(r2.code, r2.all).toBe(0);
    expect(npxInstallLog(c).length, "codename change must not reuse the old cache").toBeGreaterThanOrEqual(1);
    const pbp2 = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(pbp2).toContain("trixie");
    expect(pbp2).not.toBe(pbp1);
  }, 150000);

});

const realEnabled = process.env.GUI_CACHE_REAL_CHROMIUM === "1";
const realCase = realEnabled ? it : it.skip;
realCase(
  "real chromium: cache hit skips install and launches a real browser [NOT_RUN unless GUI_CACHE_REAL_CHROMIUM=1]",
  { timeout: 300000 },
  async () => {
    expect(fs.existsSync(REAL_LIBS), `real run requires ${REAL_LIBS}`).toBe(true);
    const requireFromGui = createRequire(path.join(GUI_DIR, "package.json"));
    const { chromium } = requireFromGui("playwright-core");
    const exe = chromium.executablePath();
    expect(fs.existsSync(exe), `real browser missing at ${exe}`).toBe(true);
    const sourceRoot = path.dirname(path.dirname(path.dirname(exe)));
    const sourceDirs = fs
      .readdirSync(sourceRoot)
      .filter((n) => /^chromium/i.test(n))
      .map((n) => path.join(sourceRoot, n));
    expect(sourceDirs.length, `no chromium* dirs under ${sourceRoot}`).toBeGreaterThanOrEqual(1);
    const c = writeFakes(newCase("real"), { npxMode: "good", mounts: "plain" });
    const discovery = runScript(c, { timeoutMs: 90000 });
    expect(discovery.code, discovery.all).toBe(0);
    const pbp = githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH");
    expect(pbp).toBeTruthy();
    for (const child of fs.readdirSync(pbp)) fs.rmSync(path.join(pbp, child), { recursive: true, force: true });
    for (const src of sourceDirs) fs.cpSync(src, path.join(pbp, path.basename(src)), { recursive: true });
    resetLogs(c);
    const real = runScript(c, {
      timeoutMs: 180000,
      extraEnv: {
        CI_PLAYWRIGHT_CACHE_NODE: null,
        CI_FAKE_NPX_MODE: "fail",
        TMPDIR: c.runnerTemp,
        LD_LIBRARY_PATH: REAL_LIBS + (process.env.LD_LIBRARY_PATH ? `:${process.env.LD_LIBRARY_PATH}` : ""),
      },
    });
    expect(real.code, `real run failed: ${real.all}`).toBe(0);
    expect(real.all).toMatch(/cache=hit\b/);
    expect(npxInstallLog(c)).toHaveLength(0);
    expect(githubEnvValue(c.githubEnv, "PLAYWRIGHT_BROWSERS_PATH")).toBe(pbp);
    const previousTmpdir = process.env.TMPDIR;
    const previousLdLibraryPath = process.env.LD_LIBRARY_PATH;
    process.env.TMPDIR = c.runnerTemp;
    process.env.LD_LIBRARY_PATH = REAL_LIBS + (previousLdLibraryPath ? `:${previousLdLibraryPath}` : "");
    const browser = await chromium.launch({ headless: true, args: ["--no-sandbox", "--disable-dev-shm-usage"] });
    try {
      const page = await browser.newPage();
      await page.setContent('<html><body><div id="probe-ok">real-ok</div></body></html>');
      expect(await page.textContent("#probe-ok")).toBe("real-ok");
    } finally {
      await browser.close();
      process.env.TMPDIR = previousTmpdir;
      process.env.LD_LIBRARY_PATH = previousLdLibraryPath;
    }
  },
);

describe("gate-gui.yml static contract", () => {
  const workflowText = () => fs.readFileSync(WORKFLOW, "utf8");

  it("keeps Provision pinned Chromium on ci-playwright-cache.sh with a job-local fallback and no direct npx install", () => {
    const text = workflowText();
    expect(text).toContain("Provision pinned Chromium");
    expect(text).toMatch(/bash\s+scripts\/ci\/ci-playwright-cache\.sh\s+"?\$\{RUNNER_TEMP/);
    expect(text, "workflow must not run npx playwright install directly").not.toMatch(/npx[^\n]*playwright[^\n]*\binstall\b/);
  });

  it("gui-unit persists $CI_CACHE_ROOT/npm before npm ci", () => {
    const block = jobBlock(workflowText(), "gui-unit");
    expect(block, "gui-unit job not found").toBeTruthy();
    expect(block).toContain("Persist the npm cache");
    expect(block).toContain("CI_CACHE_ROOT/npm");
    const persistIdx = block.indexOf("Persist the npm cache");
    const ciIdx = block.search(/npm\s+--prefix\s+gui\s+ci\b/);
    expect(ciIdx, "npm ci step missing in gui-unit").toBeGreaterThanOrEqual(0);
    expect(persistIdx, "npm cache persist must run before npm ci").toBeLessThan(ciIdx);
    expect(block).toMatch(/ci-playwright-cache\.sh\s+"?\$\{RUNNER_TEMP/);
  });

  it("release-package persists $CI_CACHE_ROOT/npm before npm ci", () => {
    const block = jobBlock(workflowText(), "release-package");
    expect(block, "release-package job not found").toBeTruthy();
    expect(block).toContain("Persist the npm cache");
    expect(block).toContain("CI_CACHE_ROOT/npm");
    const persistIdx = block.indexOf("Persist the npm cache");
    const ciIdx = block.search(/npm\s+--prefix\s+gui\s+ci\b/);
    expect(ciIdx, "npm ci step missing in release-package").toBeGreaterThanOrEqual(0);
    expect(persistIdx, "npm cache persist must run before npm ci").toBeLessThan(ciIdx);
  });
});
