import { createServer } from 'node:http';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

// Playwright 1.63 uses builds/cft; npmmirror keeps Chrome for Testing at
// chrome-for-testing. Redirect only downloads, retaining the native installer.
export function downloadDestination(path, attempt) {
  if (/^\/builds\/cft\/[\d.]+\/(linux64|linux-arm64|mac-x64|mac-arm64)\/chrome-headless-shell-\1\.zip$/.test(path)) {
    return attempt === 1
      ? `https://cdn.npmmirror.com/binaries/chrome-for-testing/${path.slice('/builds/cft/'.length)}`
      : `https://cdn.playwright.dev${path}`;
  }
  if (/^\/builds\/ffmpeg\/\d+\/ffmpeg-(linux|mac)(-arm64)?\.zip$/.test(path)) {
    return attempt === 1
      ? `https://cdn.npmmirror.com/binaries/playwright${path}`
      : `https://cdn.playwright.dev/dbazure/download/playwright${path}`;
  }
  return null;
}

export function mirrorServer() {
  const attempts = new Map();
  return createServer((request, response) => {
    const path = request.url;
    const attempt = (attempts.get(path) || 0) + 1;
    const destination = downloadDestination(path, attempt);
    if (!destination) { response.writeHead(404).end(); return; }
    attempts.set(path, attempt);
    console.log(`CI_BROWSER_SOURCE source=${attempt === 1 ? 'npmmirror' : 'official-fallback'} attempt=${attempt}`);
    response.writeHead(302, { Location: destination }).end();
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const server = mirrorServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const host = `http://127.0.0.1:${server.address().port}`;
  const bypass = [process.env.NO_PROXY || process.env.no_proxy, '127.0.0.1', 'localhost', '.npmmirror.com', 'npmmirror.com'].filter(Boolean).join(',');
  const child = spawn(process.execPath, [fileURLToPath(new URL('../dashboard/node_modules/playwright/cli.js', import.meta.url)), 'install', '--only-shell', 'chromium', ...process.argv.slice(2)], {
    stdio: 'inherit',
    env: { ...process.env, PLAYWRIGHT_DOWNLOAD_HOST: host, PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST: host, NO_PROXY: bypass, no_proxy: bypass },
  });
  for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal));
  child.on('error', error => { console.error(error.message); server.close(); process.exitCode = 1; });
  child.on('exit', code => { server.close(); process.exitCode = code ?? 1; });
}
