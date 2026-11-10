const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-review-maintenance.js', 'utf8');

function setup({confirmed = true, ok = true} = {}) {
  let handler;
  const calls = [];
  const button = {disabled: false};
  const form = {
    dataset: {materialName: '服务报告 第 1 份'}, action: '/materials/1/remove/',
    querySelector: () => button,
  };
  const location = {
    href: 'https://oa.test/review/?embed=1&material=report&document=1',
    replace: url => calls.push({url}),
  };
  const window = {oaConfirm: async () => confirmed, oaNotify: message => calls.push({message})};
  window.addEventListener = () => {};
  window.parent = window;
  runInNewContext(source, {
    window, location, URL,
    document: {addEventListener: (_, callback) => { handler = callback; }},
    FormData: class {constructor(value) { this.form = value; }},
    fetch: async (url, options) => {
      calls.push({url, options});
      return {ok, json: async () => ({error: '当前节点不能删除材料。'})};
    },
  });
  return {calls, button, async submit() {
    await handler({target: {closest: () => form}, preventDefault() {}});
  }};
}

test('cancel deletion leaves the file and page unchanged', async () => {
  const state = setup({confirmed: false});
  await state.submit();
  assert.deepEqual(state.calls, []);
  assert.equal(state.button.disabled, false);
});

test('confirmed deletion posts CSRF form and reloads without the removed document', async () => {
  const state = setup();
  await state.submit();
  assert.equal(state.calls[0].options.method, 'POST');
  assert.equal(state.calls[0].options.credentials, 'same-origin');
  assert.ok(state.calls[0].options.body.form);
  assert.equal(state.calls[1].url, 'https://oa.test/review/?embed=1&material=report');
});

test('rejected deletion shows the reason and permits retry', async () => {
  const state = setup({ok: false});
  await state.submit();
  assert.equal(state.calls[1].message, '当前节点不能删除材料。');
  assert.equal(state.button.disabled, false);
});

function menuSetup(trigger, size, viewport = {width: 280, height: 400}) {
  const handlers = {};
  const menu = {style: {}, getBoundingClientRect: () => size};
  const details = {
    open: true, matches: () => true,
    querySelector: selector => selector === 'summary'
      ? {getBoundingClientRect: () => trigger} : menu,
  };
  const document = {
    documentElement: {clientWidth: viewport.width, clientHeight: viewport.height},
    querySelectorAll: () => [details],
    addEventListener: (type, callback) => { handlers[type] = callback; },
  };
  runInNewContext(source, {document, window: {addEventListener() {}}});
  handlers.toggle({target: details});
  return {handlers, details, menu};
}

test('menu near the bottom opens above its trigger within the viewport', () => {
  const {menu} = menuSetup({right: 270, top: 350, bottom: 390}, {width: 160, height: 120});
  assert.equal(menu.style.left, '110px');
  assert.equal(menu.style.top, '222px');
});

test('menu near the left edge stays visible and fits below the trigger', () => {
  const {menu} = menuSetup({right: 80, top: 10, bottom: 50}, {width: 160, height: 120});
  assert.equal(menu.style.left, '8px');
  assert.equal(menu.style.top, '58px');
});

test('scrolling the directory closes the floating menu; scrolling its content does not', () => {
  const {handlers, details} = menuSetup(
    {right: 270, top: 350, bottom: 390}, {width: 160, height: 120},
  );
  handlers.scroll({target: {closest: () => ({})}});
  assert.equal(details.open, true);
  handlers.scroll({target: {closest: () => null}});
  assert.equal(details.open, false);
});
