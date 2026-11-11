const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

// Load every stylesheet in the real workbench's order, including V2 overrides.
const template = readFileSync('netbox_oa/templates/netbox_oa/maintenance/workbench_v2.html', 'utf8');
const styles = [...template.matchAll(/static '([^']+\.css)'/g)]
  .map(match => readFileSync(`netbox_oa/static/${match[1]}`, 'utf8')).join('\n');
const script = readFileSync('netbox_oa/static/netbox_oa/maintenance-inbox.js', 'utf8');
const python = process.env.PYTHON || path.resolve(
  process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');
const sidebar = execFileSync(python, ['-X', 'utf8', 'tests/support/inbox_activity_fixture.py'], {encoding: 'utf8'});
const netboxDist = process.env.NETBOX_SOURCE && path.join(process.env.NETBOX_SOURCE, 'netbox/project-static/dist');
const nativeStyles = netboxDist ? ['netbox-external.css', 'netbox.css']
  .map(file => readFileSync(path.join(netboxDist, file), 'utf8')).join('\n') : '';

(async () => {
  const browser = await chromium.launch({
    channel: process.env.BROWSER_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined),
    headless: true,
  });
  try {
    const page = await browser.newPage({viewport: {width: 1600, height: 1000}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    if (netboxDist) await page.route('https://oa.test/assets/**', route => route.fulfill({
      body: readFileSync(path.join(netboxDist, path.basename(new URL(route.request().url()).pathname))),
    }));
    await page.setContent(`<base href="https://oa.test/assets/"><style>${nativeStyles}</style>
      <style>*{box-sizing:border-box}body{margin:0;padding:20px}</style>
      <style>${styles}</style>
      <div class="oa-workbench oa-inbox oa-industrial oa-v2 oa-show-detail">
        <div class="oa-columns">
          <section class="oa-list"><div class="oa-order-list">工单列表</div></section>
          <main class="oa-detail">办理区</main>
          ${sidebar}
        </div>
      </div>`);
    await page.addScriptTag({content: script});
    await page.evaluate(() => document.fonts.ready);
    const measure = () => page.evaluate(() => Object.fromEntries(
      ['.oa-list', '.oa-detail', '.oa-audit'].map(selector => {
        const bounds = document.querySelector(selector).getBoundingClientRect();
        return [selector, {x: bounds.x, width: bounds.width}];
      })));
    const clickToggle = () => page.locator('[data-oa-audit-toggle]:visible').click();
    for (const width of [1600, 1200]) {
      await page.setViewportSize({width, height: 1000});
      // Explicitly expand first, including when responsive mode starts compact.
      if (await page.locator('.oa-audit-icons').isVisible()) await clickToggle();
      const wide = await measure();
      assert.equal(wide['.oa-audit'].width, 300);
      await clickToggle();
      const compact = await measure();
      assert.equal(compact['.oa-audit'].width, 50);
      assert.equal(compact['.oa-list'].width, wide['.oa-list'].width);
      assert.equal(compact['.oa-detail'].width - wide['.oa-detail'].width, 250);
      const geometry = await page.locator('.oa-audit-icons').evaluate(nav => {
        const records = nav.querySelector('.oa-audit-icon-records');
        const buttons = [...nav.querySelectorAll('.oa-audit-tools .btn')];
        const bounds = nav.getBoundingClientRect();
        const center = bounds.x + bounds.width / 2;
        return {
          overflowX: records.scrollWidth > records.clientWidth,
          scrolls: records.scrollHeight > records.clientHeight,
          nodes: [...records.querySelectorAll('a')].map(node => ({
            text: node.textContent.trim(), width: node.getBoundingClientRect().width,
            height: node.getBoundingClientRect().height,
            centerOffset: Math.abs(node.getBoundingClientRect().x + node.getBoundingClientRect().width / 2 - center),
          })),
          buttonsFit: buttons.every(button => button.getBoundingClientRect().width <= nav.clientWidth),
          buttonsStack: buttons[1].getBoundingClientRect().top >= buttons[0].getBoundingClientRect().bottom,
          buttonsCentered: buttons.every(button => Math.abs(button.getBoundingClientRect().x
            + button.getBoundingClientRect().width / 2 - center) < 1),
        };
      });
      assert.equal(geometry.overflowX, false);
      assert.equal(geometry.scrolls, true);
      assert.equal(geometry.buttonsFit, true);
      assert.equal(geometry.buttonsStack, true);
      assert.equal(geometry.buttonsCentered, true);
      assert.ok(geometry.nodes.every(node => node.text.length === 1 && node.width === 24 && node.height === 24));
      assert.ok(geometry.nodes.every(node => node.centerOffset < 1));
      // Removing the overflow must preserve the same centerline and gutter.
      await page.locator('.oa-audit-icon-records').evaluate(records => {
        for (const node of [...records.children].slice(2)) node.hidden = true;
      });
      assert.equal(await page.locator('.oa-audit-icon-records').evaluate(records => {
        const node = records.firstElementChild.getBoundingClientRect();
        const rail = records.closest('nav').getBoundingClientRect();
        return records.scrollHeight <= records.clientHeight
          && Math.abs(node.x + node.width / 2 - rail.x - rail.width / 2) < 1;
      }), true);
      await page.locator('.oa-audit-icon-records').evaluate(records => {
        for (const node of records.children) node.hidden = false;
      });
      assert.match(await page.locator('.oa-audit-event-icon').first().getAttribute('title'), /赵明.*完整办理意见.*EIP-123/);
      await page.locator('.oa-audit-event-icon').first().click();
      assert.equal(await page.locator('#activity').evaluate(node => node.matches(':modal')), true);
      await page.locator('#activity [data-oa-close]').click();
      assert.deepEqual(await measure(), compact);
      if (process.env.OA_ACTIVITY_SCREENSHOT && width === 1600) {
        await page.screenshot({path: process.env.OA_ACTIVITY_SCREENSHOT});
      }
      await clickToggle();
      assert.deepEqual(await measure(), wide);
    }
    await clickToggle();
    for (const width of [800, 600, 400]) {
      await page.setViewportSize({width, height: 1000});
      assert.equal((await measure())['.oa-audit'].width, 50);
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await clickToggle();
      assert.equal(await page.locator('.oa-audit-expanded').isVisible(), true);
      await clickToggle();
    }
    assert.deepEqual(errors, []);
    console.log('PASS: 50px V2 rail, 24px single icons, native buttons, scrolling, hover details, dialog, 250px reclaimed, repeat toggles and mobile');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
