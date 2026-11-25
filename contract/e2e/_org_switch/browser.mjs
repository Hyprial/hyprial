/**
 * E2E-032 browser steps: switch the viewed organization on the org page.
 *
 * Decision record (2026-11-21):
 * - Phases label every recorded daemon request: `open` (handoff and first
 *   paint), `org-open` (the org page and the first org's detail), `switch`
 *   (from the 查看组织 selection until the second org's detail is shown),
 *   `members` (成员 → 设备 opened, refresh button found) and `refresh` (one
 *   click on 成员's 刷新). Each phase ends on a positive barrier: an answered
 *   request and a visible DOM fact, never a fixed sleep.
 * - Which org is viewed first is read from the page (`data-org-viewed`), not
 *   assumed; the other id from the runner's `org.list` is the switch target.
 * - The org.network answer is summarised by shape only (array lengths, key
 *   names, the self owner presence), never by a machine name.
 * - The page is 1440 px wide, the width named by the acceptance row.
 */
import { recordDaemonRequests, openHandoff, runMain, waitForAnswer } from '../_gui_browser/page.mjs';

function shape(value) {
  if (Array.isArray(value)) return { length: value.length };
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, inner]) => [
      key,
      Array.isArray(inner) ? { length: inner.length } : inner === null ? null : typeof inner,
    ]));
  }
  return value === null ? null : typeof value;
}

function summarize(method, result) {
  if (method !== 'org.network' || result === null || typeof result !== 'object') return undefined;
  return {
    orgs: shape(result.orgs),
    unavailableOrgs: Array.isArray(result.unavailableOrgs) ? result.unavailableOrgs.length : null,
    dataProblems: Array.isArray(result.dataProblems)
      ? result.dataProblems.map((problem) => problem?.code ?? problem?.kind ?? typeof problem)
      : null,
    directory: shape(result.directory),
    selfKeys: result.self && typeof result.self === 'object' ? Object.keys(result.self).sort() : null,
  };
}

async function text(locator) {
  return ((await locator.textContent()) ?? '').replace(/\s+/g, ' ').trim();
}

await runMain(async (config, browser, result) => {
  const [first, second] = config.orgIds;
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  page.setDefaultTimeout(30_000);
  const recorder = recordDaemonRequests(page, config.origin, summarize);
  result.requests = recorder.entries;
  result.pageErrors = recorder.errors;
  result.boundary = recorder.boundary;
  const facts = {};
  result.facts = facts;

  await openHandoff(page, config.handoffPath, config.origin);
  facts.handoffConsumed = true;

  recorder.phase = 'org-open';
  await page.getByRole('button', { name: '组织' }).first().click();
  const section = page.locator('section[data-page="org"]');
  await section.waitFor();
  const selector = page.locator('#org-view-org');
  await selector.waitFor();
  const viewedBefore = await section.getAttribute('data-org-viewed');
  if (viewedBefore !== first && viewedBefore !== second) {
    throw new Error(`the page views an org the daemon did not list: ${viewedBefore}`);
  }
  const target = viewedBefore === first ? second : first;
  facts.viewedBefore = viewedBefore;
  facts.target = target;
  await waitForAnswer(
    recorder,
    (entry) => entry.method === 'org.network' && entry.org === viewedBefore,
    `org.network for the first viewed org ${viewedBefore}`,
  );
  facts.titleBefore = await text(page.locator('#org-page-title'));
  await selector.click();
  const options = page.getByRole('option');
  await options.first().waitFor();
  facts.selectorOptions = await options.allTextContents();

  recorder.phase = 'switch';
  const targetOption = options.filter({ hasNotText: facts.titleBefore });
  if ((await targetOption.count()) !== 1) {
    throw new Error(`expected exactly one other org in 查看组织, saw ${JSON.stringify(facts.selectorOptions)}`);
  }
  facts.targetLabel = await text(targetOption);
  await targetOption.click();
  await page.locator(`section[data-page="org"][data-org-viewed="${target}"]`).waitFor();
  await waitForAnswer(
    recorder,
    (entry) => entry.phase === 'switch' && entry.method === 'org.network' && entry.org === target,
    `org.network for the switched org ${target}`,
  );
  await page.locator('#org-page-title', { hasText: facts.targetLabel }).waitFor();
  facts.titleAfter = await text(page.locator('#org-page-title'));
  facts.liveNoticeAfterSwitch = await text(page.locator('.org-live'));

  recorder.phase = 'members';
  await page.getByRole('tab', { name: '成员', exact: true }).click();
  const members = page.locator('[data-org-section="members"]');
  await members.waitFor();
  await members.getByRole('tab', { name: '设备', exact: true }).click();
  const devicesPanel = members.locator('[role="tabpanel"]').first();
  await devicesPanel.waitFor();
  facts.devicesText = (await text(devicesPanel)).slice(0, 400);
  const refresh = members.locator('.org-section-header').getByRole('button', { name: '刷新' });
  await refresh.waitFor();
  facts.membersRefreshVisible = await refresh.isVisible();
  facts.membersRefreshEnabled = await refresh.isEnabled();
  facts.viewedInMembers = await section.getAttribute('data-org-viewed');
  facts.topLevelMachinesTab = await page.getByRole('tab', { name: '机器', exact: true }).count();

  recorder.phase = 'refresh';
  await refresh.click();
  await waitForAnswer(
    recorder,
    (entry) => entry.phase === 'refresh' && entry.method === 'org.list',
    'org.list after the members refresh',
  );
  await waitForAnswer(
    recorder,
    (entry) => entry.phase === 'refresh' && entry.method === 'org.network' && entry.org === target,
    `org.network for ${target} after the members refresh`,
  );
  facts.viewedAfterRefresh = await section.getAttribute('data-org-viewed');
  if (config.screenshotPath) await page.screenshot({ path: config.screenshotPath, fullPage: true });
  recorder.phase = 'closed';
});
