/**
 * Decision record (M1/S1/S3/S4 browser acceptance, 2026-11-05..2026-11-22)
 * - `local-org-members` verifies isolated `org.create` nullable publication
 *   (`tailnetInitialized: false`, `published: null`, `devicePublished: null`),
 *   owner member projection (`所有者`, `我`, `已同步`, `没有 Agent`), and
 *   idempotent duplicate `org.create`.
 * - `org-create` exercises `org.create`, file write/delete/restore, folder
 *   batch delete/restore (ensuring previously deleted `old.txt` stays in trash
 *   while `keep.txt` is restored and listed via recursive `orgfs.ls`),
 *   invalid-name rejection (O-W2), duplicate idempotent create (O-W2),
 *   no fake purge control (O-W8), `orgfs.members`-backed people & hidden
 *   `统计`/`拓扑` tabs in live mode (O-W9), and the OC7 two-step
 *   `org.delete` (preview, then a confirm bound to its `operationId`, ending
 *   in `complete` and dropping the org from the daemon `org.list`). Daemon
 *   state checked after the run is captured just before that delete.
 * - `contract-mock` leg of `org-create` is `NOT_RUN` because Contract Mock
 *   `empty` `onboarding.status` returns `authenticated=false`.
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
  OWNER,
  delay,
  driverEnabled,
  exists,
  prepareRoot,
  recordNotRun,
  waitFor,
  withRoot,
} from '../support/home.mjs';

async function runOrgCreateBrowser(
  handoffPath, launchUrl, origin, secrets = [], label = 'driver', expectCreateSuccess = false,
  daemonSocket = null,
) {
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
        headers: request.headers(),
      });
    });
    page.on('response', (response) => {
      const url = new URL(response.url());
      if (url.origin !== origin || !url.pathname.startsWith('/daemon/request/')) return;
      methodResponses.push({
        method: decodeURIComponent(url.pathname.slice('/daemon/request/'.length)),
        status: response.status(),
        body: response.json().catch(() => null),
      });
    });
    await page.goto(launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
    await page.getByRole('button', { name: '组织' }).first().click();
    await page.getByText('还没有可用的组织。').waitFor();
    await page.getByRole('button', { name: '新建组织' }).first().click();
    const dialog = page.getByRole('dialog', { name: '新建组织' });
    await dialog.waitFor();
    const unique = `ui-${process.pid}-${Date.now()}`;
    await dialog.getByLabel('组织名称').fill(unique);
    await dialog.getByRole('button', { name: '创建' }).click();
    let failureText = null;
    if (expectCreateSuccess) {
      const successDialog = page.getByRole('dialog', { name: '组织已创建' });
      await successDialog.waitFor();
      await successDialog.getByRole('button', { name: '暂不' }).click();
      await delay(2_000);
    } else {
      await page.waitForFunction(() => {
        const alert = document.querySelector('[role="alert"]');
        return alert?.textContent?.includes('组织创建未完成') ?? false;
      });
      await delay(1_000);
      failureText = await page.locator('[role="alert"]').first().innerText();
    }
    const createRequests = methodRequests.filter((entry) => entry.method === 'org.create');
    const listRequests = methodRequests.filter((entry) => entry.method === 'org.list');
    let fileWrite = null;
    if (label === 'daemon' || expectCreateSuccess) {
      if (!expectCreateSuccess) await dialog.getByRole('button', { name: '取消' }).click();
      await page.getByRole('tab', { name: '文件' }).click();
      await delay(2_000);
      await page.screenshot({ path: join(tmpdir(), `org-files-state-${process.pid}.png`), fullPage: true });
      process.stdout.write(`FILES-STATE ${JSON.stringify({
        text: (await page.locator('body').innerText()).slice(0, 2_000),
        newCount: await page.getByRole('button', { name: '新建' }).count(),
        tabCount: await page.getByRole('tab', { name: '文件' }).count(),
        responses: await Promise.all(methodResponses.map(async (entry) => ({
          method: entry.method,
          status: entry.status,
          body: await entry.body,
        }))),
      })}\n`);
      const newButton = page.getByRole('button', { name: '新建' }).first();
      await newButton.waitFor();
      if (await newButton.isDisabled()) {
        fileWrite = {
          attempted: false,
          reason: await page.getByText(/文件列表读取失败|暂不可写|空间状态未确认/).first().innerText().catch(() => 'disabled'),
        };
      } else {
        await newButton.click();
        const newDialog = page.getByRole('dialog', { name: '新建' });
        await newDialog.waitFor();
        const path = `verify-${unique}.txt`;
        await newDialog.getByLabel('路径').fill(path);
        await newDialog.getByRole('button', { name: '新建' }).click();
        await page.getByText(`已新建 ${path}`).waitFor();
        await delay(500);
        fileWrite = {
          attempted: true,
          path,
          writeRequests: methodRequests.filter((entry) => entry.method === 'orgfs.write').length,
          listSpaces: methodRequests.filter((entry) => entry.method === 'orgfs.spaces').length,
          listNodes: methodRequests.filter((entry) => entry.method === 'orgfs.ls').length,
        };
        await page.getByRole('button', { name: path, exact: true }).click();
        await page.getByRole('button', { name: '删除', exact: true }).click();
        const deleteDialog = page.getByRole('dialog', { name: `删除 ${path}？` });
        await deleteDialog.waitFor();
        await deleteDialog.getByRole('button', { name: '删除', exact: true }).click();
        await page.getByText(`已移到回收站：${path}`).waitFor();
        await page.getByRole('tab', { name: '回收站' }).click();
        await page.getByText(path, { exact: true }).waitFor();
        await page.getByRole('button', { name: '恢复', exact: true }).click();
        const restoreDialog = page.getByRole('dialog', { name: `恢复 ${path}？` });
        await restoreDialog.waitFor();
        await restoreDialog.getByRole('button', { name: '恢复', exact: true }).click();
        await page.getByText(`已恢复：${path}`).waitFor();
        await delay(500);
        fileWrite.deleteRequests = methodRequests.filter((entry) => entry.method === 'orgfs.remove').length;
        fileWrite.restoreRequests = methodRequests.filter((entry) => entry.method === 'orgfs.restore').length;
        await page.locator('[role="tablist"][aria-label="文件分组"]')
          .getByRole('tab', { name: '文件' }).click();
        await page.getByRole('button', { name: path, exact: true }).waitFor();
        fileWrite.restoredListed = true;
      }
    }
    let folderRestore = null;
    if (label === 'daemon' && fileWrite?.attempted && daemonSocket !== null) {
      const folder = `dir-${unique}`;
      const oldPath = `${folder}/old.txt`;
      const keepPath = `${folder}/keep.txt`;
      const baseRemoveRequests = methodRequests.filter((entry) => entry.method === 'orgfs.remove').length;
      const baseRestoreRequests = methodRequests.filter((entry) => entry.method === 'orgfs.restore').length;
      const listed = await ipcCall(daemonSocket, 'org.list', {});
      const orgEntry = listed.orgs.find((org) => org.org === `group-${unique}`);
      assert.ok(orgEntry?.spaceId, 'daemon org.list must expose the org space id for folder checks');
      const folderSpaceId = orgEntry.spaceId;
      await page.getByRole('button', { name: '新建' }).first().click();
      const folderDialog = page.getByRole('dialog', { name: '新建' });
      await folderDialog.waitFor();
      await folderDialog.getByRole('radio', { name: '文件夹' }).check();
      await folderDialog.getByLabel('路径').fill(folder);
      await folderDialog.getByRole('button', { name: '新建' }).click();
      await page.getByText(`已新建 ${folder}`).waitFor();
      await delay(400);
      for (const child of [oldPath, keepPath]) {
        await page.getByRole('button', { name: `文件夹 ${folder}/` }).click();
        await page.getByRole('button', { name: '新建' }).first().click();
        const childDialog = page.getByRole('dialog', { name: '新建' });
        await childDialog.waitFor();
        await childDialog.getByLabel('路径').fill(child);
        await childDialog.getByRole('button', { name: '新建' }).click();
        await page.getByText(`已新建 ${child}`).waitFor();
        await delay(300);
      }
      await ipcCall(daemonSocket, 'orgfs.remove', { spaceId: folderSpaceId, path: oldPath });
      const beforeFolderDelete = await ipcCall(daemonSocket, 'orgfs.ls', {
        spaceId: folderSpaceId,
        path: folder,
      });
      const beforePaths = beforeFolderDelete.nodes.map((node) => node.path);
      assert.equal(beforePaths.includes(keepPath), true, 'live child must exist before folder delete');
      assert.equal(beforePaths.includes(oldPath), false, 'old child must be deleted before folder delete');
      await page.getByRole('button', { name: `文件夹 ${folder}/` }).click();
      await page.getByRole('button', { name: '删除', exact: true }).click();
      const folderDelete = page.getByRole('dialog', { name: `删除 ${folder}？` });
      await folderDelete.waitFor();
      await folderDelete.getByRole('button', { name: '删除', exact: true }).click();
      await page.getByText(`已移到回收站：${folder}`).waitFor();
      await delay(500);
      const trashAfterDelete = await ipcCall(daemonSocket, 'orgfs.trash', {
        spaceId: folderSpaceId,
        limit: 1_000,
      });
      const trashAfterDeletePaths = trashAfterDelete.nodes.map((node) => node.path);
      await page.getByRole('tab', { name: '回收站' }).click();
      const trashRow = page.locator('li.org-row').filter({
        has: page.getByText(`${folder}/`, { exact: true }),
      });
      await trashRow.waitFor();
      await trashRow.getByRole('button', { name: '恢复', exact: true }).click();
      const folderRestoreDialog = page.getByRole('dialog', { name: `恢复 ${folder}？` });
      await folderRestoreDialog.waitFor();
      await folderRestoreDialog.getByRole('button', { name: '恢复', exact: true }).click();
      await page.getByText(`已恢复：${folder}`).waitFor();
      await delay(700);
      await page.locator('[role="tablist"][aria-label="文件分组"]')
        .getByRole('tab', { name: '文件' }).click();
      const folderRowVisible = await page
        .getByRole('button', { name: `文件夹 ${folder}/` })
        .waitFor({ timeout: 5_000 })
        .then(() => true)
        .catch(() => false);
      const afterRestore = await ipcCall(daemonSocket, 'orgfs.ls', {
        spaceId: folderSpaceId,
        path: folder,
      });
      const afterPaths = afterRestore.nodes.map((node) => node.path);
      const trashAfterRestore = await ipcCall(daemonSocket, 'orgfs.trash', {
        spaceId: folderSpaceId,
        limit: 1_000,
      });
      const trashAfterRestorePaths = trashAfterRestore.nodes.map((node) => node.path);
      folderRestore = {
        attempted: true,
        folder,
        oldPath,
        keepPath,
        oldDeletedBeforeBatch: !beforePaths.includes(oldPath),
        folderRowVisible,
        keepVisible: afterPaths.includes(keepPath),
        oldVisible: afterPaths.includes(oldPath),
        trashAfterDeletePaths,
        trashAfterRestorePaths,
        guiNestedChildButtons: await page.getByRole('button', { name: /keep\.txt/ }).count(),
        mkdirRequests: methodRequests.filter((entry) => entry.method === 'orgfs.mkdir').length,
        removeRequests:
          methodRequests.filter((entry) => entry.method === 'orgfs.remove').length - baseRemoveRequests,
        restoreRequests:
          methodRequests.filter((entry) => entry.method === 'orgfs.restore').length - baseRestoreRequests,
      };
    }
    const restoreScreenshot = join(tmpdir(), `org-restore-${label}-${process.pid}.png`);
    if (label === 'daemon') {
      await page.screenshot({ path: restoreScreenshot, fullPage: true });
    }
    let invalidCreate = null;
    let duplicateCreate = null;
    if (label === 'daemon' && expectCreateSuccess) {
      const beforeInvalid = methodRequests.filter((entry) => entry.method === 'org.create').length;
      await page.getByRole('button', { name: /组织切换器/ }).click();
      await page.getByRole('menuitem', { name: '新建组织' }).click();
      const invalidDialog = page.getByRole('dialog', { name: '新建组织' });
      await invalidDialog.waitFor();
      await invalidDialog.getByLabel('组织名称').fill('Bad_Name!');
      await invalidDialog.getByRole('button', { name: '创建' }).click();
      const invalidError = await invalidDialog
        .getByText('只能使用小写字母、数字和连字符')
        .innerText();
      invalidCreate = {
        error: invalidError,
        createRequests:
          methodRequests.filter((entry) => entry.method === 'org.create').length - beforeInvalid,
      };
      await invalidDialog.getByRole('button', { name: '取消' }).click();
      const beforeDuplicate = methodRequests.filter((entry) => entry.method === 'org.create').length;
      await page.getByRole('button', { name: /组织切换器/ }).click();
      await page.getByRole('menuitem', { name: '新建组织' }).click();
      const duplicateDialog = page.getByRole('dialog', { name: '新建组织' });
      await duplicateDialog.waitFor();
      await duplicateDialog.getByLabel('组织名称').fill(unique);
      await duplicateDialog.getByRole('button', { name: '创建' }).click();
      const duplicateSuccess = page.getByRole('dialog', { name: '组织已创建' });
      const duplicateOutcome = await duplicateSuccess
        .waitFor({ timeout: 15_000 })
        .then(() => 'created')
        .catch(async () => {
          const alert = duplicateDialog.locator('[role="alert"]');
          return (await alert.count()) > 0 ? 'alert' : 'neither';
        });
      let duplicateDialogText = null;
      if (duplicateOutcome === 'created') {
        await duplicateSuccess.getByRole('button', { name: '暂不' }).click();
      } else {
        duplicateDialogText = await duplicateDialog.innerText().catch(() => '');
        await duplicateDialog.getByRole('button', { name: '取消' }).click().catch(() => {});
      }
      await delay(500);
      const duplicateResponses = methodResponses.filter((entry) => entry.method === 'org.create');
      const duplicateEntry = duplicateResponses.at(-1) ?? null;
      duplicateCreate = {
        createRequests:
          methodRequests.filter((entry) => entry.method === 'org.create').length - beforeDuplicate,
        status: duplicateEntry?.status ?? null,
        response: await (duplicateEntry?.body ?? Promise.resolve(null)),
        outcome: duplicateOutcome,
        dialogText: duplicateDialogText,
      };
    }
    let purgeSemantics = null;
    if (label === 'daemon' && expectCreateSuccess) {
      await page.locator('[role="tablist"][aria-label="组织子页签"]')
        .getByRole('tab', { name: '文件', exact: true }).click();
      await page.locator('[role="tablist"][aria-label="文件分组"]')
        .getByRole('tab', { name: '回收站' }).click();
      const purgeButton = page.getByRole('button', { name: '彻底清除' }).first();
      const purgeVisible = (await purgeButton.count()) === 1;
      purgeSemantics = { visible: purgeVisible };
      await page.locator('[role="tablist"][aria-label="文件分组"]')
        .getByRole('tab', { name: '文件' }).click();
    }
    let unavailableReads = null;
    if (label === 'daemon' && expectCreateSuccess) {
      await page.getByRole('tab', { name: '成员' }).click();
      const membersText = await page.locator('[data-org-section="members"]').innerText().catch(() => '');
      const statsTabCount = await page.getByRole('tab', { name: '统计' }).count();
      const topologyTabCount = await page.getByRole('tab', { name: '拓扑' }).count();
      unavailableReads = {
        members: membersText.slice(0, 300),
        statsTabCount,
        topologyTabCount,
        membersHasUnknown: membersText.includes('状态未确认'),
        membersShowsOwner: membersText.includes('browser-m1-owner'),
      };
    }
    let deletion = null;
    let beforeDelete = null;
    if (label === 'daemon' && daemonSocket !== null) {
      const listed = await ipcCall(daemonSocket, 'org.list', {});
      const entry = listed.orgs.find((org) => org.org === `group-${unique}`) ?? null;
      beforeDelete = {
        listed,
        fileRead: entry && fileWrite?.path
          ? await ipcCall(daemonSocket, 'orgfs.read', { spaceId: entry.spaceId, path: fileWrite.path, waitSeconds: 0 })
          : null,
        rootListing: entry ? await ipcCall(daemonSocket, 'orgfs.ls', { spaceId: entry.spaceId }) : null,
      };
    }
    if (label === 'daemon') {
      await page.getByRole('tab', { name: '设置' }).first().click();
      await page.getByRole('tab', { name: '危险操作' }).click();
      await page.getByRole('button', { name: '删除组织', exact: true }).first().click();
      const deleteDialog = page.getByRole('dialog', { name: `删除 ${unique}` });
      await deleteDialog.waitFor();
      await deleteDialog.getByLabel('输入组织名确认').fill(unique);
      await deleteDialog.getByRole('button', { name: '确认删除', exact: true }).click();
      const settled = await Promise.race([
        deleteDialog.waitFor({ state: 'detached' }).then(() => 'closed'),
        deleteDialog.locator('[role="alert"]').waitFor().then(() => 'alert'),
      ]);
      await delay(500);
      const deleteResponses = methodResponses.filter((entry) => entry.method === 'org.delete');
      const deleteRequests = methodRequests.filter((entry) => entry.method === 'org.delete');
      const deleteBodies = await Promise.all(deleteResponses.map((entry) => entry.body));
      deletion = {
        settled,
        alertText: settled === 'alert' ? await deleteDialog.locator('[role="alert"]').innerText() : null,
        params: deleteRequests.map((entry) => JSON.parse(entry.body || 'null')),
        statuses: deleteResponses.map((entry) => entry.status),
        phases: deleteBodies.map((body) => body?.result?.phase ?? null),
        codes: deleteBodies.map((body) => body?.error?.code ?? body?.code ?? null),
        operationIds: deleteBodies.map((body) => body?.result?.operationId ?? null),
        daemonListedAfter: daemonSocket === null
          ? null
          : (await ipcCall(daemonSocket, 'org.list', {})).orgs.some((org) => org.org === `group-${unique}`),
      };
    }
    const storage = await page.evaluate(() => ({
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
      cookie: document.cookie,
      text: document.body.textContent ?? '',
    }));
    assert.equal(storage.cookie, '');
    assert.equal(JSON.stringify(storage).includes('Bearer '), false);
    for (const secret of secrets) assert.equal(JSON.stringify(storage).includes(secret), false);
    for (const entry of methodRequests) {
      assert.equal((entry.headers.authorization ?? null) === null, true);
      assert.equal(JSON.stringify(entry.headers).includes('Bearer '), false);
    }
    assert.deepEqual(errors, []);
    const screenshot = join(tmpdir(), `org-create-${label}-${process.pid}.png`);
    await page.screenshot({ path: screenshot, fullPage: true });
    const resolvedResponses = (await Promise.all(methodResponses.map(async (entry) => ({
      ...entry,
      body: await entry.body,
    }))));
    return {
      unique,
      methodRequests,
      methodResponses: resolvedResponses,
      createRequests,
      listRequests,
      failureText,
      created: expectCreateSuccess,
      fileWrite,
      folderRestore,
      invalidCreate,
      duplicateCreate,
      purgeSemantics,
      unavailableReads,
      deletion,
      beforeDelete,
      restoreScreenshot,
      screenshot,
    };
  } finally {
    await browser.close();
  }
}

test('isolated daemon local organization creation preserves nullable publication and lists its member', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('local-org-members', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-local-org-members-', async (root) => {
    let daemon = null;
    let gui = null;
    let browser = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root);
      gui = await startActualGui({ root, driver: 'daemon', mode: 'dev' });
      browser = await launchBrowser();
      const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
      page.setDefaultTimeout(20_000);
      const errors = [];
      page.on('pageerror', (error) => errors.push(error.message));
      await page.goto(gui.launchUrl, { waitUntil: 'domcontentloaded' });
      await page.locator(CONNECTED_READY).waitFor();
      await waitFor(async () => !(await exists(gui.handoffPath)), 'consumed handoff removal', 10_000);
      await page.getByRole('button', { name: '组织', exact: true }).first().click();
      await page.getByText('还没有可用的组织。').waitFor();
      await page.getByRole('button', { name: '新建组织', exact: true }).first().click();
      const name = `local-${process.pid}-${Date.now()}`;
      const create = async (existed) => {
        const dialog = page.getByRole('dialog', { name: '新建组织', exact: true });
        await dialog.getByLabel('组织名称').fill(name);
        const responsePromise = page.waitForResponse((response) =>
          new URL(response.url()).pathname === '/daemon/request/org.create');
        await dialog.getByRole('button', { name: '创建', exact: true }).click();
        const response = await responsePromise;
        assert.equal(response.status(), 200);
        const body = await response.json();
        assert.equal(body.ok, true);
        assert.equal(body.result.existed, existed);
        assert.equal(body.result.tailnetInitialized, false);
        assert.equal(body.result.published, null);
        assert.equal(body.result.devicePublished, null);
        const success = page.getByRole('dialog', { name: '组织已创建', exact: true });
        await success.waitFor();
        await success.getByRole('button', { name: '暂不', exact: true }).click();
        return body.result;
      };
      const created = await create(false);
      await page.getByRole('tab', { name: '成员', exact: true }).click();
      const members = page.locator('[data-org-section="members"]');
      const owner = members.getByRole('button', { name: `查看成员 ${OWNER}`, exact: true });
      await owner.waitFor();
      assert.match(await owner.innerText(), /所有者/);
      assert.match(await owner.innerText(), /我/);
      await owner.click();
      const membersText = await members.innerText();
      assert.match(membersText, /已同步/);
      assert.match(membersText, /在本组织的 Agent\s+没有 Agent/);
      const backendMembers = await ipcCall(daemon.socketPath, 'orgfs.members', { spaceId: created.spaceId });
      assert.equal(backendMembers.members.some((member) => member.user === `user:${OWNER}`), true);
      await page.getByRole('button', { name: /组织切换器/ }).click();
      await page.getByRole('menuitem', { name: '新建组织', exact: true }).click();
      const duplicate = await create(true);
      assert.equal(duplicate.spaceId, created.spaceId);
      const listed = await ipcCall(daemon.socketPath, 'org.list', {});
      assert.equal(listed.orgs.filter((org) => org.org === created.org).length, 1);
      assert.deepEqual(errors, []);
      process.stdout.write('PASS local-org-members: nullable publication, owner projection, unknown agents, idempotent create\n');
      await browser.close();
      browser = null;
      await terminateGuiAndCheck(gui, { daemon });
    } finally {
      if (browser !== null) await browser.close();
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});

test('actual CLI org.create reaches the shared client for both drivers without a fake success', { timeout: 420_000 }, async () => {
  const evidence = [];
  const requestedDrivers = process.env.HYPRIAL_ORG_CREATE_DRIVERS?.split(',').filter(Boolean);
  const drivers = [];
  for (const driver of ['contract-mock', 'daemon']) {
    if (!driverEnabled(driver)) {
      recordNotRun(`org-create ${driver}`, 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
      continue;
    }
    if (requestedDrivers !== undefined && !requestedDrivers.includes(driver)) {
      recordNotRun(`org-create ${driver}`, 'filtered by HYPRIAL_ORG_CREATE_DRIVERS');
      continue;
    }
    if (driver === 'contract-mock') {
      recordNotRun(
        'org-create contract-mock',
        'contract-mock empty onboarding.status returns authenticated=false so AppShell renders LoginRequiredPage instead of OrgPage',
      );
      continue;
    }
    drivers.push(driver);
  }
  if (drivers.length === 0) return;
  for (const driver of drivers) {
    await withRoot(`hyprial-gui-org-create-${driver}-`, async (root) => {
      let daemon = null;
      let gui = null;
      try {
        await prepareRoot(root);
        const expectSuccess = driver === 'daemon';
        if (driver === 'daemon') {
          daemon = await startRealDaemon(root);
        }
        const mode = process.env.HYPRIAL_ORG_CREATE_MODE === 'normal' ? 'normal' : 'dev';
        gui = await startActualGui({ root, driver, scenario: 'empty', mode });
        const result = await runOrgCreateBrowser(
          gui.handoffPath, gui.launchUrl, gui.origin, gui.secrets, driver, expectSuccess,
          daemon?.socketPath ?? null,
        );
        assert.equal(result.createRequests.length, 1, `${driver}: org.create must be dispatched once`);
        assert.equal(result.createRequests[0].body.includes(`"org":"group-${result.unique}"`), true);
        if (expectSuccess) {
          assert.equal(result.created, true);
          assert.equal(result.failureText, null);
        } else {
          assert.equal(result.created, false);
          assert.match(result.failureText, /组织创建未完成/);
          assert.equal(result.failureText.includes('组织已创建'), false);
        }
        assert.ok(result.listRequests.length >= 1, `${driver}: org.list must refresh after create`);
        let listedEntry = null;
        let fileRead = null;
        let createResponse = result.methodResponses.find((entry) => entry.method === 'org.create') ?? null;
        if (driver === 'contract-mock') {
          assert.notEqual(createResponse, null);
          assert.equal(createResponse.status, 409, 'mock org.create must be a typed 409');
          assert.equal(createResponse.body.code, 'MOCK_SCENARIO_MISSING');
        }
        if (driver === 'daemon') {
          assert.equal(expectSuccess, true);
          assert.notEqual(result.beforeDelete, null, 'daemon state must be captured before the delete step');
          const listed = result.beforeDelete.listed;
          listedEntry = listed.orgs.find((org) => org.org === `group-${result.unique}`) ?? null;
          assert.notEqual(listedEntry, null, 'the daemon org.list must contain the created organization');
          assert.notEqual(createResponse, null);
          assert.equal(createResponse.status, 200);
          assert.equal(createResponse.body?.result?.tailnetInitialized, false);
          assert.equal(createResponse.body?.result?.published, null);
          assert.equal(createResponse.body?.result?.devicePublished, null);
          assert.equal(listedEntry.state, 'active');

          assert.notEqual(result.fileWrite, null, 'daemon must attempt the file write flow');
          assert.equal(result.fileWrite.attempted, true, 'daemon must attempt the file write flow');
          assert.equal(result.fileWrite.writeRequests, 1);
          assert.equal(result.fileWrite.deleteRequests, 1);
          assert.equal(result.fileWrite.restoreRequests, 1);
          assert.equal(result.fileWrite.restoredListed, true);
          fileRead = result.beforeDelete.fileRead;

          assert.notEqual(result.folderRestore, null, 'daemon must attempt the folder restore flow');
          assert.equal(result.folderRestore.attempted, true, 'daemon must attempt the folder restore flow');
          assert.equal(result.folderRestore.oldDeletedBeforeBatch, true);
          assert.equal(result.folderRestore.folderRowVisible, true, 'restored folder must be listed in the GUI root');
          assert.equal(result.folderRestore.keepVisible, true, 'live child must be restored with the folder');
          assert.equal(result.folderRestore.oldVisible, false, 'previously deleted child must not be revived');
          assert.equal(
            result.folderRestore.trashAfterDeletePaths.includes(result.folderRestore.folder),
            true,
            'folder must enter trash',
          );
          assert.equal(
            result.folderRestore.trashAfterDeletePaths.includes(result.folderRestore.keepPath),
            true,
            'live child must enter the same deletion batch',
          );
          assert.equal(
            result.folderRestore.trashAfterDeletePaths.includes(result.folderRestore.oldPath),
            true,
            'old child is re-marked by the folder deletion batch',
          );
          assert.equal(
            result.folderRestore.trashAfterRestorePaths.includes(result.folderRestore.folder),
            false,
            'restored folder must leave trash',
          );
          assert.equal(
            result.folderRestore.trashAfterRestorePaths.includes(result.folderRestore.keepPath),
            false,
            'restored live child must leave trash',
          );
          assert.equal(
            result.folderRestore.trashAfterRestorePaths.includes(result.folderRestore.oldPath),
            true,
            'old child must stay in trash',
          );
          assert.equal(result.folderRestore.removeRequests, 1, 'GUI must issue exactly one folder remove');
          assert.equal(result.folderRestore.restoreRequests, 2, 'GUI must restore folder and live child, not the old child');
          assert.equal(
            result.folderRestore.guiNestedChildButtons,
            1,
            'GUI must list the restored nested child via recursive orgfs.ls',
          );
          const rootListing = result.beforeDelete.rootListing;
          const rootPaths = rootListing.nodes.map((node) => node.path);
          assert.ok(rootPaths.includes(result.folderRestore.folder), 'restored folder must be listed at root');

          assert.notEqual(result.invalidCreate, null, 'daemon must run the invalid-name case');
          assert.equal(result.invalidCreate.createRequests, 0, 'invalid name must not reach org.create');
          assert.equal(result.invalidCreate.error, '只能使用小写字母、数字和连字符');

          assert.notEqual(result.duplicateCreate, null, 'daemon must run the duplicate-create case');
          assert.equal(result.duplicateCreate.createRequests, 1);
          assert.equal(result.duplicateCreate.status, 200);
          assert.equal(result.duplicateCreate.response?.result?.existed, true);
          assert.equal(result.duplicateCreate.response?.result?.tailnetInitialized, false);
          assert.equal(result.duplicateCreate.response?.result?.published, null);
          assert.equal(result.duplicateCreate.response?.result?.devicePublished, null);
          assert.equal(result.duplicateCreate.response?.result?.spaceId, listedEntry.spaceId);
          assert.equal(
            result.duplicateCreate.outcome,
            'created',
            'idempotent duplicate create must report success, not a publish failure',
          );
          const orgsAfterDuplicate = result.beforeDelete.listed;
          assert.equal(
            orgsAfterDuplicate.orgs.filter((org) => org.org === `group-${result.unique}`).length,
            1,
            'duplicate create must not mint a second space',
          );

          assert.notEqual(result.purgeSemantics, null, 'daemon must exercise the purge semantics check');
          assert.equal(result.purgeSemantics.visible, false, 'no purge control is offered');

          assert.notEqual(result.unavailableReads, null, 'daemon must exercise the unavailable read models');
          assert.equal(
            result.unavailableReads.membersHasUnknown,
            false,
            'people must render from orgfs.members instead of the unknown block',
          );
          assert.equal(
            result.unavailableReads.membersShowsOwner,
            true,
            'the org owner must appear in the rendered member list',
          );
          assert.equal(result.unavailableReads.statsTabCount, 0, 'unprovided stats tab must not render in live mode');
          assert.equal(result.unavailableReads.topologyTabCount, 0, 'unprovided topology tab must not render in live mode');

          assert.notEqual(result.deletion, null);
          assert.equal(result.deletion.settled, 'closed', 'a completed delete closes the dialog');
          assert.equal(result.deletion.alertText, null);
          assert.deepEqual(
            result.deletion.statuses,
            [200, 200],
            JSON.stringify({ params: result.deletion.params, phases: result.deletion.phases }),
          );
          assert.deepEqual(result.deletion.phases, ['pending-confirmation', 'complete']);
          assert.deepEqual(result.deletion.codes, [null, null]);
          assert.deepEqual(result.deletion.params[0], { org: `group-${result.unique}` });
          assert.deepEqual(result.deletion.params[1], {
            org: `group-${result.unique}`,
            confirmed: true,
            operationId: result.deletion.operationIds[0],
          });
          assert.equal(result.deletion.daemonListedAfter, false, 'the daemon org.list must drop the deleted org');
        }
        evidence.push({
          driver,
          unique: result.unique,
          createRequests: result.createRequests.length,
          listRequests: result.listRequests.length,
          failureText: result.failureText,
          createResponse,
          listedEntry,
          fileWrite: result.fileWrite,
          folderRestore: result.folderRestore,
          invalidCreate: result.invalidCreate,
          duplicateCreate: result.duplicateCreate,
          purgeSemantics: result.purgeSemantics,
          unavailableReads: result.unavailableReads,
          fileRead,
          screenshot: result.screenshot,
        });
        process.stdout.write(`EVIDENCE org-create ${driver} ${JSON.stringify({
          unique: result.unique,
          failureText: result.failureText,
          createResponse,
          listedEntry,
          fileWrite: result.fileWrite,
          folderRestore: result.folderRestore,
          invalidCreate: result.invalidCreate,
          duplicateCreate: result.duplicateCreate,
          purgeSemantics: result.purgeSemantics,
          unavailableReads: result.unavailableReads,
          fileRead,
          deletion: result.deletion,
          deleteResponse: result.methodResponses.find((entry) => entry.method === 'org.delete') ?? null,
        })}\n`);
        await terminateGuiAndCheck(gui, { daemon });
        process.stdout.write(
          `PASS org-create ${driver} create=${result.createRequests.length} list=${result.listRequests.length} failure=${JSON.stringify(result.failureText)}\n`,
        );
      } finally {
        if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
        if (daemon !== null) await stopChild(daemon.child);
      }
    });
  }
  assert.equal(evidence.length, drivers.length);
});
