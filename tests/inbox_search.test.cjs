const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-inbox.js', 'utf8');

function setup() {
  const handlers = {}, timers = new Map(), replacements = [], requests = [], historyCalls = [];
  const requestOptions = [];
  let submitted = 0, timerId = 0;
  const node = () => ({scrollTop: 0, classList: {add() {}, toggle() {}},
    querySelector: () => null, querySelectorAll: () => [],
    replaceWith() {throw new Error('Handling content must remain in place');}});
  const sections = {'.oa-order-list': node(), '.oa-detail': node(), '.oa-audit': node(),
    '.oa-tabs': node(), '.oa-list-footer': node(), '.oa-feedback': node()};
  const auditScroll = node();
  sections['.oa-audit'].querySelector = selector => selector === '.oa-audit-scroll' ? auditScroll : null;
  for (const key of ['.oa-order-list', '.oa-tabs', '.oa-list-footer']) {
    sections[key].replaceWith = () => replacements.push(key);
  }
  const root = {...node(), dataset: {detailUrl: '/workbench/?tab=todo&selected=7'},
    querySelector: selector => sections[selector] || null,
    setAttribute() {}, removeAttribute() {}};
  const form = {action: 'http://localhost/workbench/',
    matches: selector => selector.includes('.oa-filters'), requestSubmit() {submitted++;}};
  const input = {isConnected: true, form, matches: selector => selector.includes('[name="q"]')};
  const next = {...node(), dataset: {listUrl: '/workbench/?tab=todo&q=2026'},
    querySelector: selector => ({replacement: selector})};
  runInNewContext(source, {
    document: {querySelector: () => root, documentElement: {classList: {toggle() {}}},
      addEventListener(name, fn) {(handlers[name] ||= []).push(fn);}},
    window: {addEventListener() {}, scrollTo() {}, scrollY: 0},
    history: {state: {oaList: 0}, replaceState(...args) {historyCalls.push(args);}, pushState() {}},
    location: {href: 'http://localhost/workbench/?tab=todo&selected=7'},
    setTimeout(fn) {timers.set(++timerId, fn); return timerId;},
    clearTimeout(id) {timers.delete(id);},
    URL, URLSearchParams, AbortController,
    FormData: class { *[Symbol.iterator]() {yield ['tab', 'todo']; yield ['q', '2026'];} },
    fetch: async (url, options) => {
      requests.push(url); requestOptions.push(options);
      return {ok: true, text: async () => 'page'};
    },
    DOMParser: class {parseFromString() {return {querySelector: () => next};}},
  });
  return {input, form, timers, sections, replacements, requests, requestOptions, historyCalls,
    submitted: () => submitted,
    async dispatch(name, extra = {}) {
      for (const fn of handlers[name] || []) await fn({target: input, ...extra});
    },
    flush() {const pending = [...timers.values()]; timers.clear(); pending.forEach(fn => fn());},
  };
}

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
