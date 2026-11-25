/**
 * Decision record (O-U1/O-U2/O-U3/S3/S4 browser acceptance, 2026-11-05..2026-11-22)
 * - O-U1 (`orgfs.write` drop): after dropping the write response, the path
 *   dialog remains open showing `状态未确认`, never auto-retries, and the
 *   daemon write is confirmed applied via `orgfs.ls`.
 * - O-U2 (`orgfs.restore` drop): dropping the first restore response of a
 *   folder batch renders `部分恢复` (`已确认 0/2 项恢复` / `当前第 1 项结果未知` /
 *   `剩余 1 项未尝试` / `不要重试已确认项`), disables re-confirm (`blockOnFailure`),
 *   and never replays `orgfs.restore`.
 * - O-U3 (`orgfs.ls` drop armed after `orgfs.write`): keeps the write success
 *   notice (`已新建 ...`), shows `文件列表读取失败，无法判断是否为空；刷新后重试。`,
 *   disables `新建`, never replays `orgfs.write`, and recovers on manual `刷新`.
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

async function runOrgWriteUnknownBrowser(handoffPath, launchUrl, origin, proxy, unique) {
  const browser = await launchBrowser();
  const writeRequests = [];
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1366, height: 900 } });
    page.setDefaultTimeout(20_000);
    page.on('pageerror', (error) => errors.push(error.message));
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.origin === origin && url.pathname === '/daemon/request/orgfs.write') {
        writeRequests.push(request.postData() ?? '');
      }
    });
    await page.goto(launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
    await page.getByRole('button', { name: '组织' }).first().click();
    await page.getByText('还没有可用的组织。').waitFor();
    await page.getByRole('button', { name: '新建组织' }).first().click();
    const orgDialog = page.getByRole('dialog', { name: '新建组织' });
    await orgDialog.waitFor();
    await orgDialog.getByLabel('组织名称').fill(unique);
    await orgDialog.getByRole('button', { name: '创建' }).click();
    const successDialog = page.getByRole('dialog', { name: '组织已创建' });
    await successDialog.waitFor();
    await successDialog.getByRole('button', { name: '暂不' }).click();
    await delay(1_000);
    await page.getByRole('tab', { name: '文件' }).click();
    await delay(1_500);
    const newButton = page.getByRole('button', { name: '新建' }).first();
    await newButton.waitFor();
    await newButton.click();
    const newDialog = page.getByRole('dialog', { name: '新建' });
    await newDialog.waitFor();
    const path = `unknown-${unique}.txt`;
    await newDialog.getByLabel('路径').fill(path);
    await newDialog.getByRole('button', { name: '新建' }).click();
    const unknownError = newDialog.getByText('状态未确认', { exact: true });
    await unknownError.waitFor({ timeout: 30_000 });
    const errorText = await unknownError.innerText();
    const screenshot = join(tmpdir(), `org-write-unknown-${process.pid}.png`);
    await page.screenshot({ path: screenshot, fullPage: true });
    assert.equal(await page.getByText(`已新建 ${path}`).count(), 0);
    const writes = proxy.posts.filter((entry) => entry.method === 'orgfs.write');
    assert.equal(writes.length, 1);
    assert.equal(proxy.dropped.length, 1);
    assert.equal(proxy.dropped[0].method, 'orgfs.write');
    await delay(3_000);
    assert.equal(proxy.posts.filter((entry) => entry.method === 'orgfs.write').length, 1);
    await newDialog.getByRole('button', { name: '取消' }).click();
    assert.deepEqual(errors, []);
    return { path, errorText, writeRequests: writeRequests.length, screenshot };
  } finally {
    await browser.close();
  }
}

async function runOrgFolderPartialRestoreBrowser(handoffPath, launchUrl, origin, proxy, ipcSocket, unique) {
  const browser = await launchBrowser();
  const methodRequests = [];
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
    await page.goto(launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
    await page.getByRole('button', { name: '组织' }).first().click();
    await page.getByText('还没有可用的组织。').waitFor();
    await page.getByRole('button', { name: '新建组织' }).first().click();
    const orgDialog = page.getByRole('dialog', { name: '新建组织' });
    await orgDialog.waitFor();
    await orgDialog.getByLabel('组织名称').fill(unique);
    await orgDialog.getByRole('button', { name: '创建' }).click();
    const successDialog = page.getByRole('dialog', { name: '组织已创建' });
    await successDialog.waitFor();
    await successDialog.getByRole('button', { name: '暂不' }).click();
    await delay(1_000);
    await page.getByRole('tab', { name: '文件' }).click();
    await delay(1_500);
    const folder = `dir-${unique}`;
    const oldPath = `${folder}/old.txt`;
    const keepPath = `${folder}/keep.txt`;
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
    const listed = await ipcCall(ipcSocket, 'org.list', {});
    const spaceId = listed.orgs.find((org) => org.org === `group-${unique}`)?.spaceId ?? null;
    assert.ok(spaceId, 'org.list must expose the space id');
    await ipcCall(ipcSocket, 'orgfs.remove', { spaceId, path: oldPath });
    await page.getByRole('button', { name: `文件夹 ${folder}/` }).click();
    await page.getByRole('button', { name: '删除', exact: true }).click();
    const folderDelete = page.getByRole('dialog', { name: `删除 ${folder}？` });
    await folderDelete.waitFor();
    await folderDelete.getByRole('button', { name: '删除', exact: true }).click();
    await page.getByText(`已移到回收站：${folder}`).waitFor();
    await delay(500);
    await page.getByRole('tab', { name: '回收站' }).click();
    const trashRow = page.locator('li.org-row').filter({
      has: page.getByText(`${folder}/`, { exact: true }),
    });
    await trashRow.waitFor();
    await trashRow.getByRole('button', { name: '恢复', exact: true }).click();
    const restoreDialog = page.getByRole('dialog', { name: `恢复 ${folder}？` });
    await restoreDialog.waitFor();
    await restoreDialog.getByRole('button', { name: '恢复', exact: true }).click();
    const alert = restoreDialog.locator('[role="alert"]');
    await alert.waitFor({ timeout: 30_000 });
    const message = await alert.innerText();
    const screenshot = join(tmpdir(), `org-folder-partial-${process.pid}.png`);
    await page.screenshot({ path: screenshot, fullPage: true });
    assert.equal(await page.getByText(`已恢复：${folder}`).count(), 0);
    const restores = methodRequests.filter((entry) => entry.method === 'orgfs.restore').length;
    assert.equal(restores, 1, 'partial restore must not re-send the failed item');
    assert.equal(proxy.dropped.length, 1);
    assert.equal(proxy.dropped[0].method, 'orgfs.restore');
    assert.equal(
      await restoreDialog.getByRole('button', { name: '恢复', exact: true }).isDisabled(),
      true,
      'blockOnFailure must disable confirming again',
    );
    await delay(2_000);
    assert.equal(methodRequests.filter((entry) => entry.method === 'orgfs.restore').length, 1);
    await restoreDialog.getByRole('button', { name: '取消' }).click();
    assert.deepEqual(errors, []);
    return { folder, oldPath, keepPath, message, restores, screenshot };
  } finally {
    await browser.close();
  }
}

async function runOrgRefreshFailureBrowser(handoffPath, launchUrl, origin, proxy, unique) {
  const browser = await launchBrowser();
  const methodRequests = [];
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
    await page.goto(launchUrl, { waitUntil: 'networkidle' });
    await page.locator(CONNECTED_READY).waitFor();
    await waitFor(async () => !(await exists(handoffPath)), 'consumed handoff removal', 10_000);
    await page.getByRole('button', { name: '组织' }).first().click();
    await page.getByText('还没有可用的组织。').waitFor();
    await page.getByRole('button', { name: '新建组织' }).first().click();
    const orgDialog = page.getByRole('dialog', { name: '新建组织' });
    await orgDialog.waitFor();
    await orgDialog.getByLabel('组织名称').fill(unique);
    await orgDialog.getByRole('button', { name: '创建' }).click();
    const successDialog = page.getByRole('dialog', { name: '组织已创建' });
    await successDialog.waitFor();
    await successDialog.getByRole('button', { name: '暂不' }).click();
    await delay(1_000);
    await page.getByRole('tab', { name: '文件' }).click();
    await delay(1_500);
    const path = `refresh-${unique}.txt`;
    await page.getByRole('button', { name: '新建' }).first().click();
    const newDialog = page.getByRole('dialog', { name: '新建' });
    await newDialog.waitFor();
    await newDialog.getByLabel('路径').fill(path);
    await newDialog.getByRole('button', { name: '新建' }).click();
    await page.getByText(`已新建 ${path}`).waitFor();
    await page.getByText('文件列表读取失败，无法判断是否为空；刷新后重试。').waitFor();
    const screenshot = join(tmpdir(), `org-refresh-failure-${process.pid}.png`);
    await page.screenshot({ path: screenshot, fullPage: true });
    const newButton = page.getByRole('button', { name: '新建' }).first();
    assert.equal(await newButton.isDisabled(), true);
    assert.match((await newButton.getAttribute('title')) ?? '', /文件列表读取失败/);
    assert.equal(await page.getByRole('button', { name: path, exact: true }).count(), 0);
    assert.equal(methodRequests.filter((entry) => entry.method === 'orgfs.write').length, 1);
    assert.equal(proxy.dropped.length, 1);
    assert.equal(proxy.dropped[0].method, 'orgfs.ls');
    await delay(2_000);
    assert.equal(methodRequests.filter((entry) => entry.method === 'orgfs.write').length, 1);
    await page.getByRole('button', { name: '刷新' }).first().click();
    await page.getByRole('button', { name: path, exact: true }).waitFor();
    assert.deepEqual(errors, []);
    return { path, manualRefreshRecovered: true, screenshot };
  } finally {
    await browser.close();
  }
}

test('actual CLI real-daemon orgfs.write drops one response without retry', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('org-write-unknown', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-org-write-unknown-', async (root) => {
    let daemon = null;
    let gui = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root, {
        throughProxy: true,
        dropMethod: 'orgfs.write',
      });
      gui = await startActualGui({ root, driver: 'daemon' });
      const unique = `ui-unknown-${process.pid}-${Date.now()}`;
      const result = await runOrgWriteUnknownBrowser(
        gui.handoffPath,
        gui.launchUrl,
        gui.origin,
        daemon.proxy,
        unique,
      );
      const listed = await ipcCall(daemon.socketPath, 'org.list', {});
      const entry = listed.orgs.find((org) => org.org === `group-${unique}`);
      assert.ok(entry, 'the org must exist after the GUI create step');
      const ls = await ipcCall(daemon.socketPath, 'orgfs.ls', { spaceId: entry.spaceId });
      assert.ok(
        ls.nodes.some((node) => node.path === result.path),
        'the lost-response write must have applied server-side',
      );
      assert.equal(daemon.proxy.dropped.length, 1);
      assert.equal(daemon.proxy.dropped[0].method, 'orgfs.write');
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(
        `PASS org-write-unknown writes=1 dropped=1 error=${JSON.stringify(result.errorText)} listed=true\n`,
      );
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close();
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});

test('actual CLI real-daemon folder restore keeps a partial unknown batch without retry', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('org-folder-partial', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-org-folder-partial-', async (root) => {
    let daemon = null;
    let gui = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root, {
        throughProxy: true,
        dropMethod: 'orgfs.restore',
      });
      gui = await startActualGui({ root, driver: 'daemon' });
      const unique = `ui-partial-${process.pid}-${Date.now()}`;
      const result = await runOrgFolderPartialRestoreBrowser(
        gui.handoffPath,
        gui.launchUrl,
        gui.origin,
        daemon.proxy,
        daemon.socketPath,
        unique,
      );
      assert.match(result.message, /已确认 0\/2 项恢复/);
      assert.match(result.message, /当前第 1 项结果未知/);
      assert.match(result.message, /剩余 1 项未尝试/);
      assert.match(result.message, /不要重试已确认项/);
      const listed = await ipcCall(daemon.socketPath, 'org.list', {});
      const entry = listed.orgs.find((org) => org.org === `group-${unique}`);
      assert.ok(entry, 'the org must exist after the GUI create step');
      const rootLs = await ipcCall(daemon.socketPath, 'orgfs.ls', { spaceId: entry.spaceId });
      assert.ok(
        rootLs.nodes.some((node) => node.path === result.folder),
        'the first (folder) restore applied server-side before its response was dropped',
      );
      const childLs = await ipcCall(daemon.socketPath, 'orgfs.ls', {
        spaceId: entry.spaceId,
        path: result.folder,
      });
      assert.equal(childLs.nodes.length, 0, 'the unattempted child must remain deleted');
      const trash = await ipcCall(daemon.socketPath, 'orgfs.trash', { spaceId: entry.spaceId });
      const trashPaths = trash.nodes.map((node) => node.path);
      assert.ok(trashPaths.includes(result.keepPath), 'the unattempted live child stays in trash');
      assert.ok(trashPaths.includes(result.oldPath), 'the old child stays in trash');
      assert.equal(
        daemon.proxy.posts.filter((entryPost) => entryPost.method === 'orgfs.restore').length,
        1,
        'the partial restore must not be replayed',
      );
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(
        `PASS org-folder-partial restores=1 message=${JSON.stringify(result.message)}\n`,
      );
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close();
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});

test('actual CLI real-daemon org refresh failure keeps the write result and needs manual refresh', { timeout: 300_000 }, async () => {
  if (!driverEnabled('daemon')) {
    recordNotRun('org-refresh-failure', 'filtered by HYPRIAL_ACCEPTANCE_DRIVERS');
    return;
  }
  await withRoot('hyprial-gui-org-refresh-fail-', async (root) => {
    let daemon = null;
    let gui = null;
    try {
      await prepareRoot(root);
      daemon = await startRealDaemon(root, {
        throughProxy: true,
        dropMethod: 'orgfs.ls',
        armAfterMethod: 'orgfs.write',
      });
      gui = await startActualGui({ root, driver: 'daemon' });
      const unique = `ui-refresh-${process.pid}-${Date.now()}`;
      const result = await runOrgRefreshFailureBrowser(
        gui.handoffPath,
        gui.launchUrl,
        gui.origin,
        daemon.proxy,
        unique,
      );
      const listed = await ipcCall(daemon.socketPath, 'org.list', {});
      const entry = listed.orgs.find((org) => org.org === `group-${unique}`);
      assert.ok(entry, 'the org must exist after the GUI create step');
      const ls = await ipcCall(daemon.socketPath, 'orgfs.ls', { spaceId: entry.spaceId });
      assert.ok(
        ls.nodes.some((node) => node.path === result.path),
        'the write must remain applied after the refresh failure',
      );
      assert.equal(
        daemon.proxy.posts.filter((entryPost) => entryPost.method === 'orgfs.write').length,
        1,
        'the write must not be replayed',
      );
      await terminateGuiAndCheck(gui, { daemon });
      process.stdout.write(
        `PASS org-refresh-failure writes=1 dropped=orgfs.ls manualRefresh=${result.manualRefreshRecovered}\n`,
      );
    } finally {
      if (gui !== null && gui.child.exitCode === null) await stopChild(gui.child);
      if (daemon !== null && daemon.proxy !== null) await daemon.proxy.close();
      if (daemon !== null) await stopChild(daemon.child);
    }
  });
});
