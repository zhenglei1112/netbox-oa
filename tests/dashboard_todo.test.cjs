const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');
const source = readFileSync('netbox_oa/static/netbox_oa/dashboard-todo.js', 'utf8');

function setup(response) {
  let handler, reloads = 0;
  const notifications = [];
  const requests = [], classes = new Set(['d-none']);
  const error = {textContent: '', classList: {
    add: value => classes.add(value), remove: value => classes.delete(value),
  }};
  const checkbox = {checked: true, disabled: false, form: {action: '/quick-complete/'},
    closest: () => ({querySelector: () => error})};
  const context = {
    document: {addEventListener(name, fn) {assert.equal(name, 'change'); handler = fn;}},
    window: {oaNotify: text => notifications.push(text), location: {reload() {reloads++;}}},
    FormData: class {constructor(form) {this.form = form;}},
    fetch: async (...args) => {requests.push(args); return response;},
  };
  runInNewContext(source, context);
  return {checkbox, error, classes, requests, notifications, reloads: () => reloads,
    change: () => handler({target: {closest: () => checkbox}})};
}

test('checkbox posts with CSRF form data and refreshes after success', async () => {
  const page = setup({ok: true, json: async () => ({success: true})});
  const pending = page.change();
  assert.equal(page.checkbox.disabled, true);
  await page.change();
  assert.equal(page.requests.length, 1);
  await pending;
  assert.equal(page.reloads(), 1);
  const [url, options] = page.requests[0];
  assert.equal(url, '/quick-complete/');
  assert.equal(options.method, 'POST');
  assert.equal(options.credentials, 'same-origin');
  assert.equal(options.body.form, page.checkbox.form);
});

test('server rejection restores checkbox and shows the reason', async () => {
  const page = setup({ok: false, json: async () => ({error: '请先上传扫描件'})});
  await page.change();
  assert.equal(page.checkbox.checked, false);
  assert.equal(page.checkbox.disabled, false);
  assert.equal(page.notifications[0], '请先上传扫描件');
  assert.equal(page.classes.has('d-none'), true);
  assert.equal(page.reloads(), 0);
});

test('network and expired-login responses restore the control', async () => {
  const page = setup({json: async () => {throw new Error('连接失败');}});
  await page.change();
  assert.equal(page.checkbox.checked, false);
  assert.equal(page.checkbox.disabled, false);
  assert.equal(page.notifications[0], '连接失败');
});

test('disabled steps cannot submit', async () => {
  const page = setup({});
  page.checkbox.disabled = true;
  await page.change();
  assert.equal(page.requests.length, 0);
});
