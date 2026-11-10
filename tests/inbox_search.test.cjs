const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-inbox.js', 'utf8');

function setup({action = false, taskCompleted = false, responseOk = true} = {}) {
  const handlers = {}, timers = new Map(), replacements = [], requests = [], historyCalls = [];
  const requestOptions = [], loadingStates = [];
  let submitted = 0, timerId = 0;
  const node = () => ({scrollTop: 0, classList: {add() {}, toggle() {}},
    querySelector: () => null, querySelectorAll: () => [],
    replaceWith() {throw new Error('Handling content must remain in place');}});
  const sections = {'.oa-order-list': node(), '.oa-detail': node(), '.oa-audit': node(),
    '.oa-tabs': node(), '.oa-list-footer': node(), '.oa-feedback': node()};
  const loading = {hidden: true};
  sections['[data-oa-navigation-loading]'] = loading;
  const auditScroll = node();
  sections['.oa-audit'].querySelector = selector => selector === '.oa-audit-scroll' ? auditScroll : null;
  for (const key of ['.oa-order-list', '.oa-tabs', '.oa-list-footer']) {
    sections[key].replaceWith = () => replacements.push(key);
  }
  let root = {...node(), dataset: {detailUrl: '/workbench/?tab=todo&selected=7'},
    querySelector: selector => sections[selector] || null,
    setAttribute() {}, removeAttribute() {}};
  const form = {action: 'http://localhost/workbench/',
    matches: selector => selector.includes('.oa-filters'), requestSubmit() {submitted++;}};
  const input = {isConnected: true, form, matches: selector => selector.includes('[name="q"]')};
  const next = {...node(), dataset: {listUrl: '/workbench/?tab=todo&q=2026',
    detailUrl: '/workbench/?tab=todo&selected=8'},
    setAttribute() {}, removeAttribute() {},
    querySelector: selector => action
      ? selector === '[data-oa-task-completed]' ? (taskCompleted ? {} : null)
        : sections[selector] || null
      : {replacement: selector}};
  if (action) {
    root.replaceWith = () => { root = next; replacements.push('root'); };
    sections['.oa-detail'].setAttribute = () => {};
    sections['.oa-detail'].focus = () => replacements.push('notice-focus');
  }
  runInNewContext(source, {
    document: {querySelector: () => root, documentElement: {classList: {toggle() {}}},
      addEventListener(name, fn) {(handlers[name] ||= []).push(fn);}},
    window: {addEventListener() {}, scrollTo() {}, scrollY: 0,
      matchMedia: () => ({matches: false})},
    history: {state: {oaList: 0}, replaceState(...args) {historyCalls.push(args);}, pushState() {}},
    location: {href: 'http://localhost/workbench/?tab=todo&selected=7'},
    setTimeout(fn) {timers.set(++timerId, fn); return timerId;},
    clearTimeout(id) {timers.delete(id);},
    URL, URLSearchParams, AbortController,
    FormData: class { *[Symbol.iterator]() {yield ['tab', 'todo']; yield ['q', '2026'];} },
    fetch: async (url, options) => {
      loadingStates.push(loading.hidden);
      requests.push(url); requestOptions.push(options);
      return {ok: responseOk, status: responseOk ? 200 : 400, text: async () => 'page'};
    },
    DOMParser: class {parseFromString() {return {querySelector: () => next};}},
  });
  if (action) {
    for (const section of [...Object.values(sections), auditScroll]) section.scrollTop = 200;
  }
  return {input, form, timers, sections, replacements, requests, requestOptions, historyCalls,
    loading, loadingStates,
    auditScroll,
    submitted: () => submitted,
    async dispatch(name, extra = {}) {
      for (const fn of handlers[name] || []) await fn({target: input, ...extra});
    },
    flush() {const pending = [...timers.values()]; timers.clear(); pending.forEach(fn => fn());},
  };
}

test('group navigation shows the standard loading overlay until the response completes', async () => {
  const page = setup({action: true});
  const link = {href: 'http://localhost/workbench/?tab=done',
    hasAttribute: name => name === 'data-oa-nav'};
  await page.dispatch('click', {
    target: {closest: selector => selector.includes('a[data-oa-nav]') ? link : null},
    preventDefault() {},
  });
  // The delegated click starts navigation without awaiting its promise.
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(page.loadingStates, [false]);
  assert.equal(page.loading.hidden, true);
  assert.ok(page.replacements.includes('root'));
});

test('selecting an order shows the loading overlay while refreshing its detail', async () => {
  const page = setup({action: true});
  for (const selector of ['.oa-detail', '.oa-audit']) {
    page.sections[selector].replaceWith = () => page.replacements.push(selector);
  }
  const link = {href: 'http://localhost/workbench/?tab=todo&selected=8',
    hasAttribute: name => name === 'data-oa-select'};
  await page.dispatch('click', {
    target: {closest: selector => selector.includes('a[data-oa-select]') ? link : null},
    preventDefault() {},
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(page.loadingStates, [false]);
  assert.equal(page.loading.hidden, true);
  assert.equal(page.requestOptions[0].headers['X-OA-Navigation'], 'detail');
  assert.ok(page.replacements.includes('.oa-detail'));
  assert.ok(page.replacements.includes('.oa-audit'));
  assert.ok(!page.replacements.includes('root'));
});

test('typing is debounced and clearing uses the same automatic search', async () => {
  const page = setup();
  await page.dispatch('input');
  await page.dispatch('input');
  assert.equal(page.timers.size, 1);
  page.flush();
  assert.equal(page.submitted(), 1);
  await page.dispatch('input');
  page.flush();
  assert.equal(page.submitted(), 2);
});

test('delegated events ignore non-element targets forwarded by browser scripts', async () => {
  const page = setup();
  for (const target of [null, {}, {nodeType: 9}, {nodeType: 3}]) {
    for (const name of ['keydown', 'click', 'close', 'change', 'input',
      'compositionstart', 'compositionend', 'submit']) {
      await page.dispatch(name, {target, key: 'ArrowRight',
        preventDefault() {assert.fail('Unrelated events must not be cancelled');}});
    }
  }
  assert.equal(page.timers.size, 0);
  assert.equal(page.requests.length, 0);
});

test('Chinese composition does not submit until composition ends', async () => {
  const page = setup();
  await page.dispatch('input');
  await page.dispatch('compositionstart');
  await page.dispatch('input', {isComposing: true});
  assert.equal(page.timers.size, 0);
  await page.dispatch('compositionend');
  page.flush();
  assert.equal(page.submitted(), 1);
});

test('search only refreshes the list and retains selection, scope and handling content', async () => {
  const page = setup();
  await page.dispatch('submit', {target: page.form, preventDefault() {}});
  assert.equal(page.requests.length, 1);
  assert.equal(page.requestOptions[0].headers['X-OA-Navigation'], 'search');
  const url = new URL(page.requests[0]);
  assert.equal(url.searchParams.get('tab'), 'todo');
  assert.equal(url.searchParams.get('q'), '2026');
  assert.equal(url.searchParams.get('selected'), '7');
  assert.deepEqual(page.replacements, ['.oa-order-list', '.oa-tabs', '.oa-list-footer']);
  assert.equal(page.sections['.oa-detail'].inert, false);
});
const handlingForm = () => ({action: 'http://localhost/tasks/7/complete/',
  matches: selector => selector.includes('data-oa-action'), querySelectorAll: () => []});

test('successful handling selects the returned todo and resets list/detail scroll with feedback', async () => {
  const page = setup({action: true, taskCompleted: true});
  await page.dispatch('submit', {target: handlingForm(), preventDefault() {}});
  assert.equal(page.requestOptions[0].method, 'POST');
  assert.deepEqual(page.replacements, ['root', 'notice-focus']);
  assert.equal(page.sections['.oa-order-list'].scrollTop, 0);
  assert.equal(page.sections['.oa-detail'].scrollTop, 0);
  assert.equal(page.auditScroll.scrollTop, 0);
  assert.ok(page.historyCalls.some(call => call[2] === '/workbench/?tab=todo&selected=8'));
});

test('failed handling keeps scroll and does not show completion feedback', async () => {
  const page = setup({action: true, responseOk: false});
  await page.dispatch('submit', {target: handlingForm(), preventDefault() {}});
  assert.deepEqual(page.replacements, ['root']);
  assert.equal(page.sections['.oa-order-list'].scrollTop, 200);
  assert.equal(page.sections['.oa-detail'].scrollTop, 200);
});
