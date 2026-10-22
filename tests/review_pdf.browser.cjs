const {spawn} = require('node:child_process');
const assert = require('node:assert/strict');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const {mkdirSync, mkdtempSync} = require('node:fs');
const path = require('node:path');
mkdirSync(path.resolve('.tmp'), {recursive: true});
const root = mkdtempSync(path.resolve('.tmp/native-preview-browser-'));
const python = process.env.PYTHON || path.resolve(
  process.platform === 'win32' ? '.venv/Scripts/python.exe' : '.venv/bin/python');

(async () => {
  let browser;
  const errors = [], requests = [];
  const server = spawn(python, ['tests/support/review_pdf_fixture.py', root], {
    windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  try {
    const url = await new Promise((resolve, reject) => {
      server.stdout.on('data', data => {
        const match = data.toString().match(/http:\/\/127\.0\.0\.1:\d+/);
        if (match) resolve(match[0]);
      });
      server.on('error', reject);
      server.on('exit', code => reject(new Error(`Fixture exited: ${code}`)));
      server.stderr.on('data', data => process.stderr.write(data));
    });
    browser = await chromium.launch({
      channel: process.env.BROWSER_CHANNEL || (process.platform === 'win32' ? 'msedge' : undefined),
      headless: true,
    });
    const page = await browser.newPage({viewport: {width: 1500, height: 1000}, locale: 'zh-CN'});
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => requests.push(request.url()));
    const fixture = await browser.newPage();
    await fixture.setContent('<style>@page{size:A4;margin:25mm}section{break-after:page}</style>'
      + [1, 2, 3].map(n => `<section><h1>Native preview page ${n}</h1>`
        + `<p>Searchable original material content ${n}</p></section>`).join(''));
    await fixture.pdf({path: path.join(root, 'sample.pdf'), outline: true, tagged: true});
    await fixture.close();
    await page.goto(url);
    await page.waitForSelector('[data-document="1"].review-loaded:not([hidden])');
    const selected = () => page.locator('.material-file[aria-current]').getAttribute('href');
    const frame = page.locator('.review-native-preview');
    assert.equal(await frame.getAttribute('src'), '/sample.pdf');
    assert.ok(await frame.evaluate(node => node.clientHeight > 600));
    const response = await page.request.get(`${url}/sample.pdf`);
    assert.match(response.headers()['content-type'], /application\/pdf/);
    assert.ok((await response.body()).subarray(0, 5).equals(Buffer.from('%PDF-')));

    // Real browser wheel and keyboard events never change the selected material.
    const bounds = await frame.boundingBox();
    await page.mouse.move(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    for (const delta of [100000, 100000, 120, -100000, -100000, -120]) {
      await page.mouse.wheel(0, delta);
      assert.match(await selected(), /document=1/);
    }
    for (const key of ['PageDown', 'End', 'PageUp', 'Home', 'Space']) {
      await page.keyboard.press(key);
      assert.match(await selected(), /document=1/);
    }
    // Native PDF controls initialize independently of the outer iframe load event.
    await page.waitForTimeout(1500);
    await page.screenshot({path: path.join(root, 'native-preview.png')});

    await page.locator('.material-file[href*="document=3"]').click();
    await page.waitForSelector('[data-document="3"].review-loaded:not([hidden])');
    assert.match(await selected(), /document=3/);
    assert.equal(await frame.count(), 1);
    await page.locator('.material-file[href*="document=4"]').click();
    await page.waitForSelector('[data-document="4"].review-loaded:not([hidden]) .review-image');
    assert.match(await selected(), /document=4/);
    await page.locator('.material-file[href*="document=2"]').click();
    await page.waitForSelector('[data-document="2"].review-loaded:not([hidden])');
    assert.equal(await frame.getAttribute('src'), '/form.html');
    assert.ok(await frame.evaluate(node => node.clientHeight > 600));
    await page.frameLocator('.review-native-preview').locator('h1').waitFor();
    assert.match(await selected(), /document=2/);

    // Cancelling a pending image load restores manual PDF navigation cleanly.
    let stalled;
    await page.route('**/sample.png', route => {stalled = route;});
    const request = page.waitForRequest('**/sample.png');
    await page.locator('.material-file[href*="document=4"]').click();
    await request;
    await page.locator('.material-file[href*="document=1"]').click();
    await page.waitForFunction(() => document.querySelector('.material-file[aria-current]')
      ?.href.includes('document=1'));
    await stalled.abort().catch(() => {});
    await page.unroute('**/sample.png');
    assert.equal(await frame.count(), 1);
    assert.equal(requests.some(url => /pdfjs|pdf\.worker|viewer\.html/.test(url)), false);
    assert.deepEqual(errors, []);
    console.log('PASS: native PDF embedding, no automatic wheel/keyboard switching, manual directory selection, image/form previews and load cancellation.');
    console.log('Browser artifacts:', root);
  } finally {
    await browser?.close();
    server.kill();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
