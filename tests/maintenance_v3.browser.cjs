const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const {mkdirSync, mkdtempSync} = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
assert.ok(process.env.NETBOX_SOURCE, 'Set NETBOX_SOURCE to a local NetBox source checkout');
mkdirSync('.tmp', {recursive: true});
const root = mkdtempSync(path.resolve('.tmp/v3-browser-'));
const python = process.env.PYTHON || path.resolve(
  process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');

(async () => {
  let browser;
  const errors = [];
  const server = spawn(python, ['tests/support/maintenance_v3_fixture.py', root], {
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
    const list = `${url}/plugins/oa/maintenance/v3/`;
    await page.goto(list);
    await page.getByRole('heading', {name: '维护服务工单 v3', exact: true}).waitFor();
    assert.equal(await page.locator('table tbody tr').count(), 1);
    await page.screenshot({path: path.join(root, 'list-light.png'), fullPage: true});
    await page.locator('table tbody tr a').first().click();
    await page.getByRole('heading', {name: '维护服务工单', exact: true}).waitFor();
    assert.match(page.url(), /maintenance\/v3\/orders\/\d+/);
    assert.equal(await page.locator('script[src*="maintenance-inbox"]').count(), 0);
    assert.equal(await page.getByRole('button', {name: '提交资料', exact: true}).count(), 1);
    await page.screenshot({path: path.join(root, 'detail-light.png'), fullPage: true});
    await page.getByRole('link', {name: '材料', exact: true}).click();
    const upload = page.locator('#material-report [data-oa-upload]').first();
    await upload.click();
    const dialog = page.locator('.oa-upload-modal');
    await page.frameLocator('.oa-upload-modal iframe').locator('input[type=file]').waitFor();
    assert.equal(await dialog.evaluate(node => node.matches(':modal')), true);
    await page.screenshot({path: path.join(root, 'upload-modal.png')});
    await page.frameLocator('.oa-upload-modal iframe').getByRole('link', {name: '取消', exact: true}).click();
    await dialog.waitFor({state: 'hidden'});
    assert.equal(await upload.evaluate(node => node === document.activeElement), true);
    await page.locator('html').evaluate(node => node.setAttribute('data-bs-theme', 'dark'));
    await page.screenshot({path: path.join(root, 'materials-dark.png'), fullPage: true});
    await page.getByRole('link', {name: '审批记录', exact: true}).click();
    await page.getByRole('heading', {name: '办理记录与意见', exact: true}).waitFor();
    await page.screenshot({path: path.join(root, 'history.png'), fullPage: true});
    await page.setViewportSize({width: 390, height: 844});
    await page.getByRole('link', {name: '工单详情', exact: true}).click();
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    await page.screenshot({path: path.join(root, 'detail-mobile.png'), fullPage: true});
    await page.goto(`${list}orders/add/?scope=mine`);
    await page.getByRole('heading', {name: '新增维护服务工单', exact: true}).waitFor();
    await page.screenshot({path: path.join(root, 'create-mobile.png'), fullPage: true});
    await page.getByRole('link', {name: '取消', exact: true}).click();
    assert.match(page.url(), /maintenance\/v3\/\?scope=mine/);
    assert.deepEqual(errors, []);
    console.log(`V3 native template browser checks passed. Screenshots: ${root}`);
  } finally {
    if (browser) await browser.close();
    server.kill();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
