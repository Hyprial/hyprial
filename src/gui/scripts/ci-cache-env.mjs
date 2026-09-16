import { appendFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';

export function cacheTransport(env) {
  if (!env.ACTIONS_CACHE_URL) return { available: false };
  try {
    const url = new URL(env.ACTIONS_CACHE_URL);
    if (!['http:', 'https:'].includes(url.protocol)) return { available: false };
    const bypass = [...new Set([...(env.NO_PROXY || env.no_proxy || '').split(','), url.hostname])]
      .filter(Boolean).join(',');
    return { available: true, bypass };
  } catch { return { available: false }; }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const result = cacheTransport(process.env);
  appendFileSync(process.env.GITHUB_OUTPUT, `available=${result.available}\n`);
  if (result.available) {
    // Runner cache traffic stays local; do not send it through the internet
    // proxy. Never log cache URLs, runtime tokens, or the full environment.
    appendFileSync(process.env.GITHUB_ENV, `NO_PROXY=${result.bypass}\nno_proxy=${result.bypass}\n`);
    console.log('CI_CACHE runner endpoint configured; connectivity checked by restore');
  } else {
    console.log('::warning::Runner cache is not configured; browser installation will use the cold-download budget.');
  }
}
