import { existsSync } from 'node:fs';
import { chromium } from 'playwright-core';

export function isStaticFavicon(request, origin) {
  const url = new URL(request.url());
  return request.method() === 'GET' && url.origin === origin && url.pathname === '/favicon.svg' && url.search === '';
}

/** Explicit CI override, installed system Chrome, then Playwright's Chromium. */
export function browserExecutable() {
  const configured = process.env.HYPRIAL_BROWSER_EXECUTABLE;
  const candidates = configured ? [configured] : [
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    chromium.executablePath(),
  ];
  const found = candidates.find(path => existsSync(path));
  if (!found) throw new Error('Install Chromium or set HYPRIAL_BROWSER_EXECUTABLE to an installed browser');
  return found;
}
