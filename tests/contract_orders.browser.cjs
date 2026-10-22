const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const {mkdirSync, mkdtempSync} = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
assert.ok(process.env.NETBOX_SOURCE, 'Set NETBOX_SOURCE to a local NetBox source checkout');
mkdirSync('.tmp', {recursive: true});
const root = mkdtempSync(path.resolve('.tmp/contract-browser-'));
const python = process.env.PYTHON || path.resolve(
  process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');

(async () => {
  let browser;
  const errors = [];
  const server = spawn(python, ['tests/support/contract_orders_fixture.py', root], {
    windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('Fixture startup timed out')), 30000);
      server.stdout.on('data', data => {
        const match = data.toString().match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) {clearTimeout(timer); resolve(match[0]);}
      });
      server.on('error', error => {clearTimeout(timer); reject(error);});
      server.on('exit', code => {clearTimeout(timer); reject(new Error(`Fixture exited: ${code}`));});
      server.stderr.on('data', data => process.stderr.write(data));
    });
    browser = await chromium.launch({
      channel: process.env.BROWSER_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined),
      headless: true,
    });
    const page = await browser.newPage({viewport: {width: 1440, height: 1000}, locale: 'zh-CN'});
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`${url}/?invoices_page=3`);
    await page.getByRole('heading', {name: '服务工单', exact: true}).waitFor();
    assert.ok(await page.locator('#oa-contract-orders').evaluate(node =>
      Boolean(node.compareDocumentPosition(document.querySelector('#payments')) &
        Node.DOCUMENT_POSITION_FOLLOWING)));
    assert.equal(await page.locator('#oa-contract-orders thead th').last().textContent(), '');
    const firstRow = page.locator('#oa-contract-orders tbody tr').first();
    assert.match(await firstRow.locator('td').nth(5).innerText(), /^\d{4}-\d{2}-\d{2}$/);
    const actionButtons = firstRow.locator('[data-oa-contract-open]');
    assert.equal(await actionButtons.first().innerText(), '');
    assert.match(await actionButtons.first().getAttribute('class'), /btn-outline-primary/);
    assert.match(await actionButtons.last().getAttribute('class'), /btn-outline-info/);
    const alignment = await actionButtons.first().evaluate(node => {
      const cell = node.closest('td').getBoundingClientRect();
      const group = node.parentElement.getBoundingClientRect();
      return Math.abs(cell.right - group.right);
    });
    assert.ok(alignment < 10);
    assert.equal(await page.locator('tbody tr').count(), 25);
    assert.equal(await page.locator('input[type=checkbox]').count(), 0);
    const next = page.getByRole('link', {name: '下一页', exact: true});
    assert.match(await next.getAttribute('href'), /invoices_page=3/);
    await Promise.all([page.waitForURL(/oa_orders-page=2/), next.click()]);
    await page.locator('tbody tr').first().waitFor();
    assert.equal(await page.locator('tbody tr').count(), 2);
    await page.screenshot({path: path.join(root, 'service-orders.png')});
    const flowButton = page.getByRole('link', {name: /查看 Y-.* 办理流程/});
    await flowButton.click();
    const dialog = page.locator('#oa-contract-dialog');
    const frame = page.frameLocator('#oa-contract-dialog iframe');
    await frame.locator('[data-oa-contract-ready]').waitFor();
    await frame.getByRole('heading', {name: '办理经过'}).waitFor();
    assert.equal(await frame.locator('form').count(), 0);
    assert.equal(await dialog.evaluate(node => node.matches(':modal')), true);
    await frame.getByRole('heading', {name: '办理经过'}).click();
    await page.keyboard.press('Escape');
    await dialog.waitFor({state: 'hidden'});
    assert.equal(await flowButton.evaluate(node => node === document.activeElement), true);

    await page.getByRole('link', {name: /查看 Y-.* 材料/}).click();
    await frame.locator('.document-view').waitFor();
    assert.equal(await frame.locator('[data-oa-upload], [data-material-remove]').count(), 0);
    assert.equal(await frame.getByText('返回审批', {exact: true}).count(), 0);
    await frame.locator('.material-title').filter({hasText: '服务报告'}).click();
    const preview = frame.locator('.review-document:not([hidden]) img.review-image');
    await preview.waitFor();
    assert.ok(await preview.evaluate(node => node.complete && node.naturalWidth > 0));
    assert.equal(await frame.locator('[data-oa-upload]').count(), 0);
    await page.locator('html').evaluate(node => node.setAttribute('data-bs-theme', 'dark'));
    await page.waitForFunction(() => document.querySelector('#oa-contract-dialog iframe')
      .contentDocument.documentElement.getAttribute('data-bs-theme') === 'dark');
    await page.screenshot({path: path.join(root, 'materials-dark.png')});
    await frame.locator('.material-file[data-file-type="electronic"]').first().click();
    const nested = frame.frameLocator('.review-document:not([hidden]) .review-native-preview');
    await nested.locator('body').click();
    await page.keyboard.press('Escape');
    await dialog.waitFor({state: 'hidden'});
    await page.setViewportSize({width: 390, height: 844});
    await flowButton.click();
    await frame.getByRole('heading', {name: '办理经过'}).waitFor();
    const bounds = await dialog.boundingBox();
    assert.ok(bounds.width <= 390 && bounds.height <= 844);
    await page.screenshot({path: path.join(root, 'flow-mobile.png')});
    await page.getByRole('button', {name: '关闭工单查看'}).click();
    const flowUrl = await flowButton.getAttribute('href');
    await page.route(`**${flowUrl}`, route => route.fulfill({status: 404, body: 'Not found'}));
    await flowButton.click();
    await page.getByRole('status').filter({hasText: '加载失败或无权查看'}).waitFor();
    await page.getByRole('button', {name: '关闭工单查看'}).click();
    assert.deepEqual(errors, []);
    console.log(`Contract browser checks passed; screenshots: ${root}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
