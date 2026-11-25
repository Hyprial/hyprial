import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';

const appCss = readFileSync(
  resolve(process.cwd(), 'src/styles/app.css'),
  'utf8',
);
const tokensCss = readFileSync(
  resolve(process.cwd(), 'src/styles/tokens.css'),
  'utf8',
);
const devIndexCss = readFileSync(
  resolve(process.cwd(), 'src/dev/dev-module-index.css'),
  'utf8',
);
const indexHtml = readFileSync(resolve(process.cwd(), 'index.html'), 'utf8');

function rule(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = source.match(new RegExp(`^\\s*${escaped}\\s*\\{([^}]*)\\}`, 'ms'));
  expect(match, `missing CSS rule for ${selector}`).not.toBeNull();
  return match?.[1] ?? '';
}

describe('safe R1 fixed-frame layout contract', () => {
  it('declares a bundled relative favicon for web and native packaging', () => {
    expect(indexHtml).toContain('<link rel="icon" href="./favicon.svg" />');
    expect(readFileSync(resolve(process.cwd(), 'public/favicon.svg'), 'utf8')).toContain("viewBox='0 0 32 32'");
  });

  it('caps the floating module index inside the tools panel at 1440', () => {
    const moduleDock = rule(devIndexCss, '.dev-module-dock');
    const moduleIndex = rule(devIndexCss, '.dev-module-index');
    const moduleEntry = rule(devIndexCss, '.dev-module-entry');

    expect(moduleDock).toMatch(/min-width\s*:\s*0\s*;/i);
    expect(moduleIndex).toMatch(/max-width\s*:\s*100%\s*;/i);
    expect(moduleIndex).toMatch(/overflow-x\s*:\s*auto/i);
    expect(moduleIndex).not.toMatch(/min\(760px/i);
    expect(moduleIndex).not.toMatch(/scroll-snap-type/i);
    expect(devIndexCss).toMatch(/\.dev-module-index:focus-visible/i);
    expect(moduleEntry).toMatch(/flex\s*:\s*0\s+0\s+auto\s*;/i);
  });

  it('keeps the floating layer out of the product layout rules', () => {
    const layer = rule(devIndexCss, '.dev-tools-layer');

    expect(layer).toMatch(/position\s*:\s*fixed\s*;/i);
    expect(layer).toMatch(/pointer-events\s*:\s*none\s*;/i);
    expect(devIndexCss).toMatch(/env\(safe-area-inset-bottom/i);
    expect(devIndexCss).not.toMatch(/\.(?:app|window|shell|main|content)\s*[,{\s]/);
    expect(devIndexCss).not.toMatch(/\.dev-preview-frame/);
  });

  it('keeps the scenario selectors inside the floating panel bounds', () => {
    const selector = rule(devIndexCss, '.dev-scenario select');

    expect(selector).toMatch(/min-width\s*:\s*0\s*;/i);
    expect(selector).toMatch(/max-width\s*:\s*220px\s*;/i);
    expect(devIndexCss).toMatch(/\.dev-scenario select:focus-visible/i);
  });

  it('uses a white page background without the blue-purple prototype wash', () => {
    const body = rule(tokensCss, 'body');

    expect(body).toMatch(/background(?:-color)?\s*:\s*(?:#fff(?:fff)?|white|var\(--ui-ground\))\s*;/i);
    expect(body).not.toMatch(/radial-gradient|linear-gradient/i);
  });

  it('keeps the app frame fixed and scrolls content inside at every width', () => {
    const app = rule(appCss, '.app');
    const windowFrame = rule(appCss, '.window');
    const shell = rule(appCss, '.shell');
    const main = rule(appCss, '.main');
    const content = rule(appCss, '.content');

    expect(app).toMatch(/height\s*:\s*100dvh\s*;/i);
    expect(app).toMatch(/overflow\s*:\s*hidden\s*;/i);
    expect(windowFrame).toMatch(/height\s*:\s*(?:100%|100dvh)\s*;/i);
    expect(windowFrame).toMatch(/overflow\s*:\s*hidden\s*;/i);
    expect(shell).toMatch(/height\s*:\s*100%\s*;/i);
    expect(main).toMatch(/min-height\s*:\s*0\s*;/i);
    expect(main).toMatch(/overflow\s*:\s*hidden\s*;/i);
    expect(content).toMatch(/overflow-y\s*:\s*auto\s*;/i);
  });

  it('does not replace the fixed frame with page growth at the 760px layout', () => {
    expect(appCss).toMatch(/@media\s*\(max-width:\s*979px\)/i);
    const narrowRules = appCss.slice(
      appCss.search(/@media\s*\(max-width:\s*979px\)/i),
    );

    expect(narrowRules).not.toMatch(
      /\.(?:app|window|shell|main)\s*\{[^}]*(?:height\s*:\s*auto|min-height\s*:\s*100dvh)/is,
    );
  });

  it('lets chat fill the main workspace without a card frame', () => {
    const contentWide = rule(appCss, '.content-wide');
    const chatPage = rule(appCss, '.page-chat');
    const chatLayout = rule(appCss, '.chat-layout');

    expect(contentWide).toMatch(/overflow\s*:\s*hidden\s*;/i);
    expect(chatPage).toMatch(/height\s*:\s*100%\s*;/i);
    expect(chatPage).toMatch(/overflow\s*:\s*hidden\s*;/i);
    expect(chatLayout).toMatch(/width\s*:\s*100%\s*;/i);
    expect(chatLayout).toMatch(/height\s*:\s*100%\s*;/i);
    expect(chatLayout).toMatch(/margin\s*:\s*0\s*;/i);
    expect(chatLayout).toMatch(/border\s*:\s*0\s*;/i);
    expect(chatLayout).toMatch(/border-radius\s*:\s*0\s*;/i);
  });
});
