const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const htmlPath = process.argv[2];
if (!htmlPath) {
  throw new Error("usage: node tests/ui_smoke.js <rendered-ui.html>");
}
const html = fs.readFileSync(path.resolve(htmlPath), "utf8");
const chromePath = "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";

async function checkViewport(browser, width, height, screenshotPath) {
  const page = await browser.newPage({ viewport: { width, height } });
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.route("http://ui.test/**", async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname === "/") {
      await route.fulfill({ status: 200, contentType: "text/html", body: html });
    } else if (url.pathname === "/api/accounts") {
      await route.fulfill({ json: { accounts: [{ id: "one", name: "主账号", status: "active", has_app_password: true }], count: 1 } });
    } else if (url.pathname === "/api/state") {
      await route.fulfill({ json: { running: false, creating: false, total_created: 0, today_created: 0 } });
    } else if (url.pathname === "/api/emails") {
      await route.fulfill({ json: { emails: [], count: 0 } });
    } else if (url.pathname === "/api/aliases") {
      await route.fulfill({ json: { ok: true, aliases: [], accounts: {}, failures: {} } });
    } else if (url.pathname === "/api/log-stream") {
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: "" });
    } else if (url.pathname.endsWith("/inbox-stream")) {
      await route.fulfill({ status: 200, contentType: "text/event-stream", body: 'data: {"type":"done","count":0}\n\n' });
    } else {
      await route.fulfill({ json: { ok: true } });
    }
  });

  await page.goto("http://ui.test/", { waitUntil: "domcontentloaded" });
  await page.click('[data-tab="inbox"]');
  await page.selectOption("#inboxAccount", "one");
  await page.waitForTimeout(100);
  await page.click("#btnInboxRefresh");
  await page.click("#btnInboxForce");
  await page.fill("#aliasSearchInput", "alias@icloud.com");
  await page.click("#btnInboxSearch");
  await page.click("#btnInboxAll");
  await page.click("#btnInboxSettings");
  await page.click("#appPwdModal .btn-outline");

  await page.click('[data-tab="emails"]');
  await page.click("#btnAliasSync");
  await page.getByRole("button", { name: "复制全部" }).click();
  await page.getByRole("button", { name: "CSV" }).click();
  await page.getByRole("button", { name: "导出已选 TXT" }).click();
  await page.click('[data-tab="batch"]');
  await page.click("#btnBatchExec");
  await page.click('[data-tab="logs"]');
  await page.getByRole("button", { name: "清屏" }).click();
  await page.click('[data-tab="inbox"]');

  const metrics = await page.evaluate(() => {
    const ids = [
      "inboxAccount", "inboxLimit", "aliasSearchInput", "btnInboxRefresh",
      "btnInboxForce", "btnInboxSearch", "btnInboxAll", "btnInboxSettings",
    ];
    return {
      innerWidth,
      scrollWidth: document.documentElement.scrollWidth,
      controls: ids.map((id) => {
        const element = document.getElementById(id);
        const rect = element.getBoundingClientRect();
        return { id, left: rect.left, right: rect.right, width: rect.width, height: rect.height };
      }),
    };
  });

  if (metrics.scrollWidth > metrics.innerWidth + 1) {
    throw new Error(`horizontal overflow at ${width}px: ${JSON.stringify(metrics)}`);
  }
  for (const control of metrics.controls) {
    if (control.left < -1 || control.right > metrics.innerWidth + 1 || control.width <= 0 || control.height <= 0) {
      throw new Error(`control outside viewport at ${width}px: ${JSON.stringify(control)}`);
    }
  }
  if (pageErrors.length) {
    throw new Error(`page errors at ${width}px: ${JSON.stringify(pageErrors)}`);
  }
  await page.screenshot({ path: screenshotPath, fullPage: true });
  await page.close();
  return metrics;
}

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: chromePath });
  try {
    const mobile = await checkViewport(browser, 390, 844, "D:\\Temp\\icloud-playwright-mobile.png");
    const desktop = await checkViewport(browser, 1440, 1000, "D:\\Temp\\icloud-playwright-desktop.png");
    console.log(JSON.stringify({ mobile, desktop }, null, 2));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error.stack || error);
  process.exit(1);
});
