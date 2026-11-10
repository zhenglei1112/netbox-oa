const assert = require('node:assert/strict');
const path = require('node:path');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const browser = await chromium.launch({channel: process.env.BROWSER_CHANNEL || 'msedge', headless: true});
  try {
    const page = await browser.newPage();
    const netbox = process.env.NETBOX_SOURCE;
    assert.ok(netbox, 'Set NETBOX_SOURCE to a local NetBox source checkout');
    const buttons = native => `
      <button class="btn btn-sm btn-outline-primary">F</button>
      <form style="display:inline"><button class="btn btn-sm btn-outline-primary">S</button></form>
      <form style="display:inline"><button class="btn btn-sm btn-outline-info">D</button></form>
      <span class="btn-group dropdown"><a class="btn btn-warning ${native ? 'btn-sm' : ''}">E</a>
        <a class="btn btn-warning dropdown-toggle ${native ? 'btn-sm' : 'dropdown-toggle-split'}"
          ${native ? 'style="padding-left:2px"' : ''}></a></span>`;
    const table = native => `<table class="table"><tbody>${['短名称', '筛选后不同长度的付款或服务工单名称'].map(text =>
      `<tr><td>${text}</td><td class="text-end text-nowrap noprint p-1">${native ? buttons(true) :
        `<div class="oa-contract-row-actions">${buttons(false)}</div>`}</td></tr>`).join('')}</tbody></table>`;
    await page.setContent(`<main class="p-3"><div class="card" id="payments"><h5 class="card-header">付款</h5>
      ${table(true)}</div><section class="card" id="oa-contract-orders">${table(false)}</section>
      <div class="card" id="contract_otn_faults">${table(true)}</div></main>`);
    await page.addStyleTag({path: path.join(netbox, 'netbox/project-static/dist/netbox.css')});
    await page.addStyleTag({path: path.resolve('netbox_oa/static/netbox_oa/contract-orders.css')});
    await page.addScriptTag({path: path.resolve('netbox_oa/static/netbox_oa/contract-orders.js')});
    async function check() {
      await page.waitForFunction(() => !document.querySelector('td.noprint .btn-sm'));
      const rows = await page.locator('td.noprint').evaluateAll(cells => cells.map(cell =>
        [...cell.querySelectorAll('.btn')].map(button => {
          const rect = button.getBoundingClientRect();
          return {x: rect.x, right: rect.right, y: rect.y, height: rect.height};
        })));
      for (const row of rows) {
        assert.equal(row.length, 5);
        row.forEach((button, index) => {
          assert.ok(Math.abs(button.x - rows[0][index].x) < 1, JSON.stringify(rows));
          assert.ok(Math.abs(button.height - rows[0][index].height) < 1);
          assert.ok(Math.abs(button.y - row[0].y) < 1, JSON.stringify(rows));
        });
      }
    }
    for (const width of [1920, 1440, 768, 390]) {
      await page.setViewportSize({width, height: 1000});
      for (const theme of ['light', 'dark']) {
        await page.locator('html').evaluate((node, theme) => node.dataset.bsTheme = theme, theme);
        await check();
        // Contract plugin replaces order rows and then adds btn-sm to its icons.
        await page.locator('#oa-contract-orders').evaluate((node, html) => {
          node.innerHTML = html;
          node.querySelectorAll('.btn').forEach(button => button.classList.add('btn-sm'));
        }, table(false));
        await page.locator('#contract_otn_faults').evaluate((node, html) => {
          node.outerHTML = `<div class="card" id="contract_otn_faults">${html}</div>`;
        }, table(true));
        await check();
        // Clearing filters restores native markup, which must align as well.
        await page.locator('#payments > div').evaluate((node, html) => node.innerHTML = html, table(true));
        await check();
      }
    }
    console.log('Action alignment passed: initial, filtered and cleared; four widths; light/dark.');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
