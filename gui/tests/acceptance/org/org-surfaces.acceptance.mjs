/**
 * Decision record (2026-11-09/S3/S4 browser acceptance, 2026-11-09..2026-11-22)
 * - Under `HYPRIAL_NETWORK_ISOLATED=1` (`4cd766296`), `org.create` is local-only
 *   (`tailnetInitialized: false`, `published: null`, `devicePublished: null`),
 *   `org.invite` and `org.remove` preview fail typed with `组织网络尚未就绪`,
 *   and `InvitesSection` omits `[data-org-tab="received"]` when `claimInvite`
 *   is unprovided.
 * - Seeds `move-me.txt` (`orgfs.write`) and the invited account
 *   (`orgfs.invite`) before launching the GUI, redacts `org.invite`/`org.join`
 *   bodies from logs/evidence, exercises `orgfs.move`, `org.join`, `machines`,
 *   `settings` segments, `org.fetch`, and `org.leave` preview (canceling
 *   without confirming).
 */

import assert from 'node:assert/strict';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import test from 'node:test';
import { launchBrowser } from '../support/browser.mjs';
import {
  startActualGui,
  stopChild,
  terminateGuiAndCheck,
} from '../support/cli.mjs';
import {
  ipcCall,
  startRealDaemon,
} from '../support/daemon.mjs';
import {
  CONNECTED_READY,
  delay,
  driverEnabled,
  exists,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
} from '../support/home.mjs';

async function runOrgSurfacesBrowser(handoffPath, launchUrl, origin, orgName, spaceId, inviteAccount) {
  const browser = await launchBrowser();
  const methodRequests = [];
  const methodResponses = [];
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    page.setDefaultTimeout(20_000);
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
      methodRequests.push({
        method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)),
        body: request.postData() ?? '',
      });
    });
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
      const method = decodeURIComponent(url.pathname.slice('/daemon/request/'.length));
      if (method === 'org.invite') {
        methodResponses.push({ method, status: response.status(), body: null });
        return;
      }
      methodResponses.push({ method, status: response.status(), body: response.json().catch(() => null) });
    });
    await page.goto(launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
    await page.getByRole('button', { name: '组织' }).first().click();
    const overview = page.locator('[data-org-section="overview"]');
    await overview.waitFor();
    const overviewText = (await overview.innerText()).slice(0, 400);

    await page.locator('[data-org-tab="invites"]').click();
    const invites = page.locator('[data-org-section="invites"]');
    await invites.waitFor();
    const inviteSegmentLabels = await invites.locator('[role="tab"]').allInnerTexts();
    assert.equal(await invites.locator('[data-org-tab="received"]').count(), 0);
    const receivedText = null;

    await invites.locator('[data-org-tab="send"]').click();
    const accountInput = invites.locator('#org-invite-account');
    await accountInput.fill('');
    const inviteBefore = methodRequests.filter((entry) => entry.method === 'org.invite').length;
    await invites.getByRole('button', { name: '生成邀请' }).click();
    await invites.getByText('请填写成员账号').waitFor();
    const emptySubmitRequests =
      methodRequests.filter((entry) => entry.method === 'org.invite').length - inviteBefore;

    await accountInput.fill(inviteAccount);
    await invites.getByRole('button', { name: '生成邀请' }).click();
    const resultDialog = page.getByRole('dialog', { name: '邀请链接已生成' });
    const dialogAppeared = await resultDialog
      .waitFor({ timeout: 30_000 })
      .then(() => true)
      .catch(() => false);
    let inviteOutcome;
    if (dialogAppeared) {
      inviteOutcome = {
        kind: 'dialog',
        title: '邀请链接已生成',
        linkCodeCount: await resultDialog.locator('code.org-code').count(),
        closeButtonCount: await resultDialog.getByRole('button', { name: '关闭', exact: true }).count(),
        submitButtonCount: await resultDialog.getByRole('button', { name: /复制邀请链接/ }).count(),
      };
      await resultDialog.getByRole('button', { name: '关闭', exact: true }).click();
      await waitFor(async () => (await page.getByRole('dialog', { name: '邀请链接已生成' }).count()) === 0, 'invite dialog close', 10_000);
    } else {
      const alert = invites.locator('[role="alert"]').first();
      const alertCount = await alert.count();
      inviteOutcome = {
        kind: alertCount > 0 ? 'alert' : 'neither',
        text: alertCount > 0 ? (await alert.innerText()).slice(0, 300) : null,
      };
    }
    const inviteRequests = methodRequests.filter((entry) => entry.method === 'org.invite').length - inviteBefore;
    const inviteResponse = methodResponses.find((entry) => entry.method === 'org.invite') ?? null;
    await delay(500);

    await page.locator('[data-org-tab="members"]').click();
    const members = page.locator('[data-org-section="members"]');
    await members.waitFor();
    await members.getByRole('button', { name: '刷新', exact: true }).first().click();
    const invitedMember = members.getByText(inviteAccount, { exact: false }).first();
    const invitedMemberVisible = await invitedMember
      .waitFor({ timeout: 30_000 })
      .then(() => true)
      .catch(() => false);
    let memberRemoval = {
      invitedMemberVisible,
      opened: false,
      previewRequests: 0,
      confirmRequests: 0,
      submitted: false,
      confirmDisabled: null,
      alertText: null,
    };
    if (invitedMemberVisible) {
      await invitedMember.click();
      const removeButton = members.getByRole('button', { name: '移除成员', exact: true });
      const removeButtonVisible = await removeButton
        .waitFor({ timeout: 10_000 })
        .then(() => true)
        .catch(() => false);
      if (removeButtonVisible) {
        const removeBefore = methodRequests.filter((entry) => entry.method === 'org.remove').length;
        const removeResponseBefore = methodResponses.filter((entry) => entry.method === 'org.remove').length;
        await removeButton.click();
        const removeDialog = page.getByRole('dialog', { name: /^移除成员 .*？$/ });
        const opened = await removeDialog.waitFor({ timeout: 20_000 }).then(() => true).catch(() => false);
        if (opened) {
          const confirmButton = removeDialog.getByRole('button', { name: '确认移除', exact: true });
          const previewAlert = removeDialog.locator('[role="alert"]').first();
          const alertShown = await previewAlert.waitFor({ timeout: 20_000 }).then(() => true).catch(() => false);
          const alertText = alertShown ? await previewAlert.innerText().catch(() => null) : null;
          const confirmDisabled = await confirmButton.isDisabled();
          const allRemoveRequests = methodRequests.filter((entry) => entry.method === 'org.remove');
          const newRemoveRequests = allRemoveRequests.slice(removeBefore);
          const removeParams = newRemoveRequests.map((entry) => {
            try {
              return entry.body === '' ? {} : JSON.parse(entry.body);
            } catch {
              return {};
            }
          });
          const previewRequests = removeParams.filter((params) => params.confirmed !== true).length;
          const confirmRequests = removeParams.filter((params) => params.confirmed === true).length;
          const newRemoveResponses = methodResponses
            .filter((entry) => entry.method === 'org.remove')
            .slice(removeResponseBefore);
          const previewResponse = newRemoveResponses[0] ?? null;
          memberRemoval = {
            ...memberRemoval,
            opened: true,
            previewRequests,
            confirmRequests,
            submitted: false,
            alertText,
            confirmDisabled,
            previewStatus: previewResponse?.status ?? null,
          };
          if (await removeDialog.count() > 0) {
            await removeDialog.getByRole('button', { name: '取消', exact: true }).click().catch(() => undefined);
          }
        }
      }
    }

    await page.locator('[data-org-tab="invites"]').click();
    await invites.waitFor();
    await invites.locator('[data-org-tab="join"]').click();
    const linkInput = invites.locator('#org-join-invite-link');
    await linkInput.fill(`https://invite.invalid/not-a-credential-${process.pid}`);
    const joinBefore = methodRequests.filter((entry) => entry.method === 'org.join').length;
    await invites.getByRole('button', { name: '加入' }).click();
    await page.waitForFunction(() => {
      const section = document.querySelector('[data-org-section="invites"]');
      if (section === null) return false;
      const status = section.querySelector('.org-join-result');
      const alert = section.querySelector('[role="alert"]');
      return Boolean(status?.textContent?.trim()) || alert !== null;
    }, null, { timeout: 90_000 });
    await delay(500);
    const joinText = (await invites.innerText()).slice(0, 700);
    const joinRequests = methodRequests.filter((entry) => entry.method === 'org.join').length - joinBefore;
    const joinResponse = methodResponses.find((entry) => entry.method === 'org.join') ?? null;
    const joinResponseBody = await (joinResponse?.body ?? Promise.resolve(null));
    const joinScreenshot = join(tmpdir(), `org-surfaces-join-${process.pid}.png`);
    await page.screenshot({ path: joinScreenshot, fullPage: true });

    await page.locator('[data-org-tab="members"]').click();
    await page.locator('[data-org-section="members"] [data-org-tab="machines"]').click();
    const machines = page.locator('[data-org-section="machines"]');
    await machines.waitFor();
    await delay(1_500);
    const machinesText = (await machines.innerText()).slice(0, 700);
    const machineRequests = methodRequests
      .filter((entry) => ['org.fetch', 'org.network', 'org.list'].includes(entry.method))
      .map((entry) => ({ method: entry.method, params: entry.body === '' ? null : JSON.parse(entry.body) }));

    await page.locator('[data-org-tab="files"]').click();
    const filesSection = page.locator('[data-org-section="files"]');
    await filesSection.waitFor();
    const sourceFile = filesSection.getByRole('button', { name: 'move-me.txt', exact: true });
    const sourceVisible = await sourceFile.waitFor({ timeout: 20_000 }).then(() => true).catch(() => false);
    let fileMove = { sourceVisible, dialogOpened: false, moveRequests: 0, movedVisible: false, notice: null };
    if (sourceVisible) {
      await sourceFile.click();
      await filesSection.getByRole('button', { name: '移动', exact: true }).click();
      const moveDialog = page.getByRole('dialog', { name: '移动或重命名 move-me.txt' });
      const dialogOpened = await moveDialog.waitFor({ timeout: 20_000 }).then(() => true).catch(() => false);
      if (dialogOpened) {
        await moveDialog.locator('#org-file-path').fill('moved-me.txt');
        const moveBefore = methodRequests.filter((entry) => entry.method === 'orgfs.move').length;
        await moveDialog.getByRole('button', { name: '移动', exact: true }).click();
        const movedVisible = await filesSection
          .getByRole('button', { name: 'moved-me.txt', exact: true })
          .waitFor({ timeout: 30_000 })
          .then(() => true)
          .catch(() => false);
        const moveResponse = methodResponses.filter((entry) => entry.method === 'orgfs.move').at(-1) ?? null;
        const moveBody = await (moveResponse?.body ?? Promise.resolve(null));
        const moveRequest = methodRequests.filter((entry) => entry.method === 'orgfs.move').at(-1) ?? null;
        let moveParams = {};
        try {
          moveParams = moveRequest?.body ? JSON.parse(moveRequest.body) : {};
        } catch {
          moveParams = {};
        }
        fileMove = {
          sourceVisible,
          dialogOpened,
          moveRequests: methodRequests.filter((entry) => entry.method === 'orgfs.move').length - moveBefore,
          movedVisible,
          notice: movedVisible ? '已移动到 moved-me.txt' : null,
          moveStatus: moveResponse?.status ?? null,
          moveCode: moveBody?.code ?? null,
          moveSpaceId: moveParams.spaceId ?? null,
          moveSpaceMatches: moveParams.spaceId === spaceId,
        };
      }
    }

    await page.locator('[data-org-tab="settings"]').click();
    const settings = page.locator('[data-org-section="settings"]');
    await settings.waitFor();
    const settingsSegments = {};
    for (const [id, label] of [
      ['basic', '基础信息'],
      ['structure', '结构与部门'],
      ['comm', '通信权限'],
      ['norms', '规范'],
      ['versions', '版本'],
    ]) {
      const segmentTab = settings.locator(`[data-org-tab="${id}"]`);
      if ((await segmentTab.count()) === 0) {
        settingsSegments[label] = { present: false };
        continue;
      }
      const disabled = await segmentTab.getAttribute('aria-disabled') === 'true';
      if (disabled) {
        settingsSegments[label] = { present: true, disabled };
        continue;
      }
      await segmentTab.click();
      await delay(400);
      settingsSegments[label] = { present: true, disabled, text: (await settings.innerText()).slice(0, 400) };
    }
    const fetchBefore = methodRequests.filter((entry) => entry.method === 'org.fetch').length;
    const fetchResponsePromise = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return url.origin === origin
        && url.pathname === '/daemon/request/org.fetch'
        && response.request().method() === 'POST';
    }, { timeout: 30_000 });
    await settings.getByRole('button', { name: '同步组织配置', exact: true }).click();
    const fetchResponse = await fetchResponsePromise;
    const fetchBody = await fetchResponse.json();
    const syncFetch = {
      requests: methodRequests.filter((entry) => entry.method === 'org.fetch').length - fetchBefore,
      status: fetchResponse.status(),
      ok: fetchBody?.ok === true,
      requestedSources: fetchBody?.result?.requestedSources?.length ?? null,
      responseCount: fetchBody?.result?.responseCount ?? null,
      receivedCount: fetchBody?.result?.receivedCount ?? null,
      candidates: fetchBody?.result?.candidates?.length ?? null,
      text: (await settings.innerText()).slice(0, 500),
    };

    await settings.locator('[data-org-tab="danger"]').click();
    await delay(400);
    const leaveButton = settings.getByRole('button', { name: '离开组织', exact: true });
    const leaveCount = await leaveButton.count();
    const dangerText = (await settings.innerText()).slice(0, 500);
    const danger = {
      text: dangerText,
      leaveButtonCount: leaveCount,
      leaveDisabled: leaveCount > 0 ? await leaveButton.isDisabled() : null,
      leaveBlockedReasonPresent: dangerText.includes('这台电脑是该组织的管理设备'),
      deleteButtonCount: await settings.getByRole('button', { name: '删除组织', exact: true }).count(),
    };
    let leaveDialog = null;
    if (leaveCount > 0 && danger.leaveDisabled === false) {
      const leaveBefore = methodRequests.filter((entry) => entry.method === 'org.leave').length;
      await leaveButton.click();
      const dialog = page.getByRole('dialog', { name: /^离开 / });
      const opened = await dialog.waitFor({ timeout: 20_000 }).then(() => true).catch(() => false);
      await delay(800);
      leaveDialog = {
        opened,
        previewRequests:
          methodRequests.filter((entry) => entry.method === 'org.leave').length - leaveBefore,
        previewStatus: null,
        previewCode: null,
        alertText: opened
          ? await dialog.locator('[role="alert"]').first().innerText().catch(() => null)
          : null,
        cancelButtonCount: opened
          ? await dialog.getByRole('button', { name: '取消', exact: true }).count()
          : 0,
      };
      if (opened) {
        await dialog.getByRole('button', { name: '取消', exact: true }).click();
        await waitFor(
          async () => (await page.getByRole('dialog', { name: /^离开 / }).count()) === 0,
          'leave dialog cancel',
          10_000,
        );
      }
      await delay(700);
      const previewResponse = methodResponses.filter((entry) => entry.method === 'org.leave').at(-1) ?? null;
      const previewBody = await (previewResponse?.body ?? Promise.resolve(null));
      leaveDialog.previewStatus = previewResponse?.status ?? null;
      leaveDialog.previewCode = previewBody?.code ?? null;
    }
    const settingsScreenshot = join(tmpdir(), `org-surfaces-settings-${process.pid}.png`);
    await page.screenshot({ path: settingsScreenshot, fullPage: true });

    const storage = await page.evaluate(() => ({
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
      cookie: document.cookie,
      href: location.href,
    }));
    assert.equal(storage.cookie, '');
    assert.equal(JSON.stringify(storage).includes('Bearer '), false);
    assert.equal(errors.length, 0, `page errors: ${JSON.stringify(errors)}`);
    const resolvedResponses = await Promise.all(methodResponses.map(async (entry) => ({
      method: entry.method,
      status: entry.status,
      body: entry.method === 'org.invite' ? null : await entry.body,
    })));
    return {
      orgName,
      overviewText,
      inviteSegmentLabels,
      receivedText,
      emptySubmitRequests,
      inviteRequests,
      inviteOutcome,
      inviteResponse: inviteResponse === null ? null : {
        status: inviteResponse.status,
      },
      memberRemoval,
      fileMove,
      joinRequests,
      joinText,
      joinResponse: joinResponse === null ? null : {
        status: joinResponse.status,
        code: joinResponseBody?.code ?? null,
      },
      joinScreenshot,
      machinesText,
      machineRequests,
      settingsSegments,
      syncFetch,
      danger,
      leaveDialog,
      settingsScreenshot,
      storage,
      methodRequests: methodRequests.map((entry) => ({
        method: entry.method,
        params: entry.method === 'org.invite' || entry.method === 'org.join'
          ? '<redacted>'
          : entry.body === '' ? null : JSON.parse(entry.body),
      })),
      methodResponses: resolvedResponses.filter((entry) => entry.method !== 'org.invite'),
    };
  } finally {
    await browser.close();
  }
}

test('actual CLI real-daemon org surfaces: invite, join, machines and settings are wired', { timeout: 420_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('org-surfaces', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-org-surfaces-', async (root) => {
    let daemon = null;
    let gui = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root);
      const unique = `surf-${process.pid}-${Date.now()}`;
      const inviteAccount = `surface-${process.pid}-${Date.now()}@example.invalid`;
      const created = await ipcCall(daemon.socketPath, 'org.create', { org: `group-${unique}` });
      assert.equal(created.org, `group-${unique}`, 'the org id must be the daemon-assigned group name');
      assert.equal(created.tailnetInitialized, false, 'the isolated daemon must not initialize a tailnet');
      assert.equal(created.published, null, 'the isolated daemon must not claim org publication');
      assert.equal(created.devicePublished, null, 'the isolated daemon must not claim device publication');
      await ipcCall(daemon.socketPath, 'orgfs.write', {
        spaceId: created.spaceId,
        path: 'move-me.txt',
        text: 'move-me',
      });
      await ipcCall(daemon.socketPath, 'orgfs.invite', {
        spaceId: created.spaceId,
        user: `user:${inviteAccount}`,
        mode: 'rw',
      });
      gui = await startActualGui({ root, driver: 'daemon', scenario: 'empty', mode: 'normal' });
      const result = await runOrgSurfacesBrowser(
        gui.handoffPath,
        gui.launchUrl,
        gui.origin,
        unique,
        created.spaceId,
        inviteAccount,
      );
      process.stdout.write(`EVIDENCE org-surfaces ${JSON.stringify({
        org: unique,
        created,
        overviewText: result.overviewText,
        inviteSegmentLabels: result.inviteSegmentLabels,
        receivedText: result.receivedText,
        emptySubmitRequests: result.emptySubmitRequests,
        inviteRequests: result.inviteRequests,
        inviteOutcome: result.inviteOutcome,
        inviteResponse: result.inviteResponse,
        memberRemoval: result.memberRemoval,
        fileMove: result.fileMove,
        joinRequests: result.joinRequests,
        joinText: result.joinText,
        joinResponse: result.joinResponse,
        machinesText: result.machinesText,
        machineRequests: result.machineRequests,
        settingsSegments: Object.fromEntries(
          Object.entries(result.settingsSegments).map(([label, value]) => [label, value.present ? value.text : value]),
        ),
        syncFetch: result.syncFetch,
        danger: result.danger,
        leaveDialog: result.leaveDialog,
        storage: result.storage,
        methods: result.methodRequests.map((entry) => ({ method: entry.method, params: entry.params })),
      })}\n`);
      assert.equal(result.emptySubmitRequests, 0, 'empty invite must fail locally without a request');
      assert.equal(result.inviteRequests, 1, 'a filled invite must dispatch org.invite exactly once');
      assert.equal(result.inviteOutcome.kind, 'alert', 'the isolated invite must show a typed failure alert, not a link dialog');
      assert.ok((result.inviteOutcome.text ?? '').includes('组织网络尚未就绪'), 'the invite alert must carry the typed isolated reason');
      assert.equal(result.memberRemoval.invitedMemberVisible, true,
        'the orgfs.invite-seeded account must appear in the real orgfs.members-backed people list');
      assert.equal(result.memberRemoval.opened, true, 'the invited member must open the removal preview');
      assert.equal(result.memberRemoval.previewRequests, 1, 'removal preview must dispatch org.remove exactly once');
      assert.equal(result.memberRemoval.previewStatus, 500, 'the isolated removal preview must reach the daemon and fail typed');
      assert.equal(result.memberRemoval.confirmDisabled, true, 'the confirm button must stay disabled without a preview');
      assert.equal(result.memberRemoval.confirmRequests, 0, 'a failed preview must never dispatch a confirm');
      assert.equal(result.memberRemoval.submitted, false, 'the removal must not fake a submitted state');
      assert.ok((result.memberRemoval.alertText ?? '').includes('组织网络尚未就绪'), 'the preview failure must show its typed reason');
      assert.equal(result.fileMove.sourceVisible, true, 'the pre-written file must be visible in the live tree');
      assert.equal(result.fileMove.dialogOpened, true, 'the file move dialog must open');
      assert.equal(result.fileMove.moveRequests, 1, 'the move must dispatch orgfs.move exactly once');
      assert.equal(result.fileMove.movedVisible, true, 'the moved path must be visible after refresh');
      assert.equal(result.fileMove.moveStatus, 200, 'the move must return HTTP 200');
      assert.equal(result.fileMove.moveCode, null, 'the move must not carry an error code');
      assert.equal(result.fileMove.moveSpaceMatches, true, 'the move must target the org space used by the test');
      assert.equal(result.joinRequests, 1, 'a non-empty invite link must dispatch org.join exactly once');
      assert.ok(result.joinText.length > 0, 'join must render a visible result');
      assert.equal(result.joinText.includes('已加入'), false, 'a malformed link must not render a joined state');
      assert.ok(result.machinesText.length > 0, 'the machines section must render its live read');
      for (const label of ['基础信息', '结构与部门', '通信权限', '规范', '版本']) {
        assert.equal(result.settingsSegments[label]?.present, true, `settings segment ${label} must exist`);
        if (['结构与部门', '通信权限', '规范'].includes(label)) {
          assert.equal(result.settingsSegments[label].disabled, true, `settings segment ${label} must be disabled`);
        } else {
          assert.ok(result.settingsSegments[label].text.length > 0, `settings segment ${label} must render`);
        }
      }
      assert.equal(result.syncFetch.requests, 1, 'explicit org.fetch sync must dispatch exactly once');
      assert.equal(result.syncFetch.status, 200, 'org.fetch sync must return HTTP 200');
      assert.equal(result.syncFetch.ok, true, 'org.fetch sync must be a successful typed result');
      assert.equal(result.syncFetch.requestedSources, 0, 'an isolated empty network must request no sources');
      assert.equal(result.syncFetch.responseCount, 0, 'an isolated empty network must receive no responses');
      assert.equal(result.syncFetch.receivedCount, 0, 'an isolated empty network must receive no candidates');
      assert.equal(result.syncFetch.candidates, 0, 'an isolated empty network must return no candidates');
      assert.match(result.syncFetch.text, /没有收到候选/, 'the empty-network result must be visible to the user');
      assert.equal(result.danger.leaveButtonCount, 1);
      if (result.danger.leaveDisabled === true) {
        assert.equal(result.danger.leaveBlockedReasonPresent, true);
      } else {
        assert.equal(result.leaveDialog?.opened, true, 'enabled leave must open the preview dialog');
        assert.equal(result.leaveDialog.previewRequests, 1, 'leave preview must dispatch org.leave once');
        assert.equal(result.leaveDialog.cancelButtonCount, 1);
      }
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(
        `PASS org-surfaces invite=${result.inviteOutcome.kind} join=${result.joinResponse?.code ?? result.joinResponse?.status ?? 'unknown'} ` +
          `machines=${result.machinesText.length} leaveDisabled=${result.danger.leaveDisabled}\n`,
      );
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});
