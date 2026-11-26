/** Product App/rail journey using contract example references; no backend or vendor claim. */
import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";
import { chromium } from "playwright-core";
import { startGuiServer } from "../../../../scripts/serve.mjs";
import { browserExecutable } from "../../../support/browser.mjs";
const root = resolve(import.meta.dirname, "../../../..");
process.env.HYPRIAL_GUI_HOME = root;
const server = await startGuiServer({
  mode: "development",
  host: "127.0.0.1",
  port: 0,
  strictPort: true,
});
const browser = await chromium.launch({ executablePath: browserExecutable() });
const evidence =
  process.env.HYPRIAL_BROWSER_EVIDENCE_DIR || "/private/tmp/pr5-browser";
try {
  await mkdir(evidence, { recursive: true });
  for (const width of [1440, 390, 375, 760]) {
    const page = await browser.newPage({ viewport: { width, height: 900 } });
    const errors = [],
      consoleErrors = [],
      failedRequests = [],
      external = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (m) => {
      if (m.type() === "error") consoleErrors.push(m.text());
    });
    page.on("requestfailed", (r) => failedRequests.push(r.url()));
    page.on("request", (r) => {
      const u = new URL(r.url());
      if (
        u.protocol.startsWith("http") &&
        u.origin !== new URL(server.url).origin
      )
        external.push(u.origin);
    });
    await page.goto(
      new URL("tests/features/connections/model-service/entry.html", server.url)
        .href,
    );
    await page.locator("main.main#main-content").waitFor();
    await page
      .getByRole("button", { name: "我的账号与 Agent 设置菜单" })
      .click();
    await page
      .getByRole("menuitem", { name: "Agent 设置", exact: true })
      .click();
    await page.getByRole("button", { name: "添加连接", exact: true }).click();
    await page.getByLabel("服务地址来源", { exact: true }).click();
    await page
      .getByRole("option", { name: "OpenAI 官方地址", exact: true })
      .click();
    assert.equal(
      await page.getByLabel("服务地址", { exact: true }).inputValue(),
      "https://api.openai.com/v1",
    );
    await page.getByLabel("服务地址来源", { exact: true }).click();
    await page.getByRole("option", { name: "自定义地址", exact: true }).click();
    await page
      .getByLabel("服务地址", { exact: true })
      .fill("https://model.example/v1");
    await page.getByLabel("名称", { exact: true }).fill("example-custom");
    await page.getByLabel("Key", { exact: true }).fill("synthetic-example");
    await page.getByLabel("测试模型 ID", { exact: true }).focus();
    await page.keyboard.press("Tab");
    assert.equal(
      await page
        .getByRole("dialog")
        .evaluate((el) => el.contains(document.activeElement)),
      true,
    );
    const saveBox = await page
      .getByRole("button", { name: "保存", exact: true })
      .boundingBox();
    assert.ok(saveBox && saveBox.y >= 0 && saveBox.y + saveBox.height <= 900);
    await page.screenshot({
      path: resolve(evidence, `after-add-${width}.png`),
    });
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await page
      .getByText("连接和 Key 已保存；尚未验证模型，也未启动 Agent。")
      .waitFor();
    const row = page
      .getByRole("list", { name: "凭据列表" })
      .locator("li")
      .filter({ has: page.getByText("example-custom", { exact: true }) });
    await row.getByRole("button", { name: "查看详情", exact: true }).click();
    await row
      .getByRole("button", { name: "测试已保存连接", exact: true })
      .click();
    await page.getByLabel("测试模型 ID", { exact: true }).fill(" hy4-flash");
    assert.equal(
      await page.getByRole("button", { name: "测试", exact: true }).isEnabled(),
      false,
    );
    await page.getByLabel("测试模型 ID", { exact: true }).fill("hy4-flash");
    await page.getByRole("button", { name: "测试", exact: true }).click();
    await page.getByText(/本次模型测试通过/).waitFor();
    await page.screenshot({
      path: resolve(evidence, `after-test-${width}.png`),
    });
    await page.keyboard.press("Escape");
    assert.equal(await page.getByRole("dialog").count(), 0);
    assert.equal(
      await row
        .getByRole("button", { name: "测试已保存连接", exact: true })
        .evaluate((el) => el === document.activeElement),
      true,
    );
    await row.getByRole("button", { name: "更换 Key", exact: true }).click();
    await page.getByLabel("新 Key", { exact: true }).fill("synthetic-example");
    await page.getByRole("button", { name: "保存", exact: true }).click();
    await page.getByText(/Key 已更换，连接版本已更新/).waitFor();
    await page
      .locator(".credentials-section")
      .getByRole("button", { name: "刷新", exact: true })
      .click();
    await page.getByText("example-custom", { exact: true }).waitFor();
    await page.getByRole("button", { name: "开始", exact: true }).click();
    await page
      .getByRole("button", { name: "我的账号与 Agent 设置菜单" })
      .click();
    await page
      .getByRole("menuitem", { name: "Agent 设置", exact: true })
      .click();
    assert.equal(await page.getByLabel("Key", { exact: true }).count(), 0);
    assert.equal(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      true,
    );
    assert.deepEqual(errors, []);
    assert.deepEqual(consoleErrors, []);
    assert.deepEqual(failedRequests, []);
    assert.deepEqual(external, []);
    console.log(
      JSON.stringify({
        width,
        errors,
        consoleErrors,
        failedRequests,
        external,
        driver: "canonical-contract-mock",
        passed: true,
      }),
    );
    await page.close();
  }
} finally {
  await browser.close();
  await server.close();
}
