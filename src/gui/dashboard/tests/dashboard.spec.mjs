import { test, expect } from "@playwright/test";

test("dashboard navigation, search, filter, pagination, read-only details and keyboard dismissal", async ({
  page,
}, info) => {
  const errors = [];
  const writes = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (!["GET", "HEAD"].includes(request.method())) writes.push(request.url());
  });
  await page.goto("/");
  await expect(page.getByText("13 / 13 数据源可用")).toBeVisible();
  await expect(
    page.getByRole("link", { name: "打开开发工作台" }),
  ).toHaveAttribute("href", "http://127.0.0.1:3180/");
  await expect(
    page.getByRole("heading", { name: "运行全景", exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("dashboard-desktop.png"),
    fullPage: true,
  });
  await page.getByRole("link", { name: "节点与 Agent", exact: true }).click();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(
    page.getByRole("cell", { name: "builder-17", exact: true }),
  ).toBeVisible();
  await page.getByRole("textbox", { name: "搜索Agent 身份" }).fill("builder-2");
  await expect(
    page.getByRole("cell", { name: "builder-2", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "筛选Agent 身份状态" })
    .selectOption("online");
  await expect(page.getByText("没有匹配的记录")).toBeVisible();
  await page
    .getByRole("combobox", { name: "筛选Agent 身份状态" })
    .selectOption("all");
  const detail = page.getByRole("button", {
    name: "查看 agent:hyprial:taipei-01:builder-2 详情",
    exact: true,
  });
  await detail.click();
  await expect(page.getByRole("dialog")).toContainText(
    "agent:hyprial:taipei-01:builder-2",
  );
  await expect(page.getByRole("dialog")).not.toContainText("DO-NOT-EXPOSE");
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(detail).toBeFocused();
  for (const name of ["运行与调度", "投递", "集成", "系统"]) {
    await page.getByRole("link", { name, exact: true }).click();
    await expect(
      page.getByRole("heading", { name, exact: true }),
    ).toBeVisible();
  }
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "系统", exact: true }),
  ).toBeVisible();
  expect(errors).toEqual([]);
  expect(writes).toEqual([]);
});

test("source failures preserve last successful data and never turn unknown into zero", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.getByText("13 / 13 数据源可用")).toBeVisible();
  await page.route("**/api/sources/hosts", (route) => route.abort());
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByText("12 / 13 数据源可用")).toBeVisible();
  await expect(page.getByText("taipei-01", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/数据过期 · 状态服务连接失败/).first(),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByText("12 / 13 数据源可用")).toBeVisible();
  const metric = page.locator(".metric").filter({ hasText: "在线节点" });
  await expect(metric.locator("strong")).toHaveText("—");
  await page.unroute("**/api/sources/hosts");
  await page.getByRole("button", { name: "刷新", exact: true }).click();
  await expect(page.getByText("13 / 13 数据源可用")).toBeVisible();
  await expect(metric.locator("strong")).toHaveText("3");
});

test("phone layout has no page overflow and all sections remain reachable", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(page.getByText("team-lark", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("dashboard-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("link", { name: "投递", exact: true }).click();
  await expect(page.getByRole("heading", { name: "待投递队列" })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("unconfirmed capabilities fail closed without querying sources", async ({
  page,
}) => {
  let queries = 0;
  page.on("request", (request) => {
    if (request.url().includes("/api/sources/")) queries++;
  });
  await page.route("**/api/capabilities", (route) =>
    route.fulfill({
      json: { protocolVersion: 1, guiMode: "develop", sources: [] },
    }),
  );
  await page.goto("/");
  await expect(page.getByRole("alert")).toHaveText("只读服务能力无法确认");
  expect(queries).toBe(0);
});
