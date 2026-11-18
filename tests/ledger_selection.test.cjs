const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');
const source = readFileSync('netbox_oa/static/netbox_oa/workorder-ledger.js', 'utf8');

function setup() {
  const handlers = {}, requests = [], pending = [], timers = new Map();
  const blocks = Array.from({length: 6}, () => ({textContent: 'initial'}));
  const attrs = {};
  let selected = [], all = false, timer = 0;
  const totals = {
    isConnected: true, querySelectorAll: () => blocks,
    setAttribute: (key, value) => { attrs[key] = value; },
    removeAttribute: key => { delete attrs[key]; },
  };
  const list = {
    closest: () => ({querySelector: () => all ? {} : null}),
    querySelectorAll: () => selected.map(value => ({value: String(value)})),
  };
  runInNewContext(source, {
    document: {
      getElementById: id => ({'oa-ledger-totals': totals, object_list: list,
        quicksearch: {value: 'current search'}}[id]),
      addEventListener: (name, fn) => { handlers[name] = fn; },
    },
    location: {href: 'http://localhost/ledger/?province=广东&page=2'},
    URL, AbortController,
    setTimeout: fn => { timers.set(++timer, fn); return timer; },
    clearTimeout: id => timers.delete(id),
    fetch: (url, options) => {
      requests.push({url, options});
      return new Promise(resolve => pending.push(resolve));
    },
    DOMParser: class {
      parseFromString(html) {
        return {getElementById: () => ({querySelectorAll: () =>
          Array.from({length: 6}, () => ({textContent: html}))})};
      }
    },
  });
  return {
    blocks, attrs, requests,
    select(ids, global = false) { selected = ids; all = global;
      handlers.change({target: {matches: () => true}}); },
    settle: () => handlers['htmx:afterSettle'](),
    async flush() { const jobs = [...timers.values()]; timers.clear();
      jobs.forEach(fn => fn()); await Promise.resolve(); },
    async respond(index, text, ok = true) {
      pending[index]({ok, text: async () => text});
      await new Promise(resolve => setImmediate(resolve));
    },
  };
}

test('selection requests retain filters and send checked IDs; clearing restores all pages', async () => {
  const page = setup();
  page.select([3, 7]); await page.flush();
  const params = page.requests[0].url.searchParams;
  assert.deepEqual(params.getAll('_selected'), ['3', '7']);
  assert.equal(params.get('province'), '广东');
  assert.equal(params.get('q'), 'current search');
  assert.equal(params.get('_ledger_totals'), '1');
  await page.respond(0, 'selected totals');
  assert.equal(page.blocks[0].textContent, 'selected totals');
  page.select([]); await page.flush();
  assert.deepEqual(page.requests[1].url.searchParams.getAll('_selected'), []);
  await page.respond(1, 'all pages');
  assert.equal(page.blocks[0].textContent, 'all pages');
  assert.equal(page.attrs['aria-busy'], undefined);
});

test('global select-all and table swaps recalculate without stale responses winning', async () => {
  const page = setup();
  page.select([1]); await page.flush();
  page.select([1], true); await page.flush();
  assert.equal(page.requests[1].url.searchParams.get('_ledger_all'), '1');
  assert.deepEqual(page.requests[1].url.searchParams.getAll('_selected'), []);
  assert.equal(page.requests[0].options.signal.aborted, true);
  await page.respond(1, 'latest');
  await page.respond(0, 'stale');
  assert.equal(page.blocks[0].textContent, 'latest');
  page.settle(); await page.flush();
  assert.equal(page.requests.length, 3);
});

test('failed requests leave visible totals in place and mark them as needing refresh', async () => {
  const page = setup();
  page.select([1]); await page.flush();
  await page.respond(0, '', false);
  assert.equal(page.blocks[0].textContent, 'initial');
  assert.match(page.attrs.title, /统计更新失败/);
  assert.equal(page.attrs['aria-busy'], undefined);
});
