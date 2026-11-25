// @vitest-environment node
/*
 * Decision (2026-11-15, PR 1f/F6): outside CI a machine without the pinned
 * Chromium skips this file; with CI set the same absence fails the test, so the
 * browser acceptance can never be reported as passed without having run.
 * CI counts as set only for a value other than empty, 0 or false.
 */
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  findBrowserExecutable,
  runBrowserAcceptance,
} from './dev01-onboarding-browser.mjs';

const hasBrowser = findBrowserExecutable() !== null;
const mustRun = !['', '0', 'false'].includes((process.env.CI ?? '').trim().toLowerCase());

describe.skipIf(!hasBrowser && !mustRun)(
  'dev01 onboarding browser acceptance',
  () => {
    it('checks 375/760/1440, focus, demo flow and request/storage probes', async () => {
      if (!hasBrowser) {
        throw new Error('pinned Chromium is missing (PLAYWRIGHT_BROWSERS_PATH / DEV01_CHROMIUM_EXECUTABLE)');
      }
      const report = await runBrowserAcceptance({
        screenshotDir:
          process.env.DEV01_SCREENSHOT_DIR ??
          resolve(process.cwd(), 'artifacts/dev01-onboarding-browser'),
      });

      expect(report.viewports.map((viewport) => viewport.width)).toEqual([
        375, 760, 1440,
      ]);
      for (const viewport of report.viewports) {
        expect(viewport.keyboardOrder.length).toBeGreaterThan(0);
        expect(viewport.interactions).toContain('back-to-account');
        expect(viewport.interactions).toContain('refresh-to-account');
        expect(viewport.layout).toMatchObject({
          cardCount: 1,
          activeCardCount: 1,
          gridCount: 0,
          navCurrentCount: 1,
          activeStep: 'account',
        });
        expect(viewport.layout.navLabels).toEqual([
          '1 · 登录',
          '2 · 设备',
          '3 · 运行配置',
          '4 · 对话',
        ]);
        expect(viewport.layout.logo).toMatchObject({
          alt: 'Hyprial',
          width: 40,
        });
        expect(viewport.probe).toEqual({
          fetch: 0,
          xhr: 0,
          storageWrites: 0,
          localStorageLength: 0,
          sessionStorageLength: 0,
        });
      }
    }, 180_000);
  },
);
