const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const nativeStyles = process.env.NETBOX_SOURCE ? ['netbox-external.css', 'netbox.css']
  .map(file => readFileSync(path.join(process.env.NETBOX_SOURCE, 'netbox/project-static/dist', file), 'utf8'))
  .join('\n') : '';
const template = readFileSync('netbox_oa/templates/netbox_oa/maintenance/workbench_v2.html', 'utf8');
const styles = [...template.matchAll(/static '([^']+\.css)'/g)]
  .map(match => readFileSync(`netbox_oa/static/${match[1]}`, 'utf8')).join('\n');
const python = process.env.PYTHON || path.resolve('.venv/Scripts/python.exe');
const render = collapsed => execFileSync(python, ['-X', 'utf8', '-c', `
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'tests.settings'
import django
django.setup()
from pathlib import Path
from django.template import Template, Context
source = Path('netbox_oa/templates/netbox_oa/maintenance/workbench_v2.html').read_text(encoding='utf8')
source = source[source.index('      <section class="oa-v2-guide'):source.index('      <div class="oa-v2-workspace')]
print(Template(source).render(Context({
    'guide_collapsed': ${collapsed ? 'True' : 'False'}, 'csrf_token': 'test-token',
    'detail': {'current': {'name': '工单复核'}, 'status_label': '待你审批', 'waiting_for': '工单初审', 'waiting': '7分钟'},
    'guide': {'title': '核对工单内容，签署审核意见', 'steps': ['审阅服务事项、服务范围、合同及申请人签名。', '完成本环节签名并填写意见。']},
})))
`], {encoding: 'utf8'});

(async () => {
  const browser = await chromium.launch({channel: 'msedge', headless: true});
  try {
    const page = await browser.newPage({viewport: {width: 1400, height: 900}});
    const errors = [], saves = [];
    let fail = false;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://oa.test/**', async route => {
      if (route.request().method() === 'POST') {
        saves.push(route.request().postData());
        return route.fulfill({status: fail ? 500 : 200, contentType: 'application/json', body: '{}'});
      }
      return route.fulfill({body: ''});
    });
    await page.goto('https://oa.test/');
    const load = async collapsed => {
      await page.setContent(`<style>${nativeStyles}</style><style>${styles}</style><style>
        *{box-sizing:border-box}body{margin:0}.oa-detail{height:700px;overflow:auto}
        .oa-inbox{--oa-border:#dce5ef;--oa-accent:teal;--oa-soft:#cee}
        </style><div class="oa-inbox oa-v2"><div class="oa-order-list" hidden></div>
        <aside class="oa-audit" hidden><div class="oa-audit-scroll"></div></aside>
        <main class="oa-detail">${render(collapsed)}
        <div style="height:650px">事项说明</div></main></div>`);
      await page.addScriptTag({content: readFileSync('netbox_oa/static/netbox_oa/maintenance-inbox.js', 'utf8')});
    };
    await load(false);
    const toggle = page.locator('[data-oa-guide-toggle]');
    const content = page.locator('#oa-guide-content');
    const box = () => page.locator('.oa-v2-guide').boundingBox();
    const expanded = await box();
    for (let i = 0; i < 3; i++) {
      await toggle.click();
      await page.waitForFunction(() => !document.querySelector('[data-oa-guide-toggle]').disabled);
      assert.equal(await content.isVisible(), false);
      assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
      const collapsed = await box();
      assert.equal(collapsed.x, expanded.x);
      assert.equal(collapsed.width, expanded.width);
      assert.ok(collapsed.height < expanded.height);
      await toggle.click();
      await page.waitForFunction(() => !document.querySelector('[data-oa-guide-toggle]').disabled);
      assert.equal(await content.isVisible(), true);
      assert.equal((await box()).width, expanded.width);
    }
    assert.ok(saves[0].includes('true'));
    assert.ok(saves[1].includes('false'));
    // NetBox boosted navigation executes page scripts again without a full reload.
    await page.addScriptTag({content: readFileSync('netbox_oa/static/netbox_oa/maintenance-inbox.js', 'utf8')});
    const beforeReentry = saves.length;
    await toggle.click();
    await page.waitForFunction(() => !document.querySelector('[data-oa-guide-toggle]').disabled);
    assert.equal(saves.length - beforeReentry, 1, 'reentering the workbench must not duplicate click handlers');
    await toggle.click();
    await page.waitForFunction(() => !document.querySelector('[data-oa-guide-toggle]').disabled);
    fail = true;
    await toggle.click();
    await page.waitForFunction(() => !document.querySelector('[data-oa-guide-toggle]').disabled);
    assert.equal(await content.isVisible(), true, 'failed saves restore the previous mode');
    // A fresh page receives the saved mode directly from the server.
    await page.reload();
    await load(true);
    assert.equal(await content.isVisible(), false);
    await page.setViewportSize({width: 390, height: 800});
    assert.equal(await toggle.getAttribute('aria-expanded'), 'false');
    assert.equal(await page.locator('.oa-v2-guide-heading').evaluate(el => el.scrollWidth <= el.clientWidth), true);
    assert.deepEqual(errors, []);
    console.log('Guide collapse: repeated toggles, stable width, persistence rendering, mobile and save failure passed.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
