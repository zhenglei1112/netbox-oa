const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-print.js', 'utf8');

function setup(decode, count = 2, responses = [true], embedded = false, seals = []) {
  const active = new Set(), handlers = {};
  const requests = [];
  const button = {dataset: {auditUrl: '/materials/7/print-event/', csrfToken: 'csrf-token'},
    addEventListener(_, fn) {handlers.click = fn;}, after(node) {handlers.feedback = node;}};
  const templates = Array.from({length: count}, () => ({
    content: {firstElementChild: {cloneNode() {
      return {querySelectorAll: () => [{decode}], remove() {active.delete(this);}};
    }}},
    before(code) {active.add(code);},
  }));
  let prints = 0;
  runInNewContext(source, {
    fetch(url, options) {
      requests.push({url, options});
      return Promise.resolve({ok: responses.length > 1 ? responses.shift() : responses[0]});
    },
    document: {
      getElementById: () => embedded ? null : button,
      body: {dataset: button.dataset, append(node) {handlers.feedback = node;}},
      querySelectorAll: selector => selector === '.print-code-template' ? templates : seals,
      createElement: () => ({setAttribute() {}, remove() {}}),
    },
    window: {
      oaNotify(text) {handlers.feedback = {textContent: text};},
      addEventListener(name, fn) {handlers[name] = fn;},
      print() {assert.equal(active.size, count); handlers.beforeprint(); prints++;},
    },
  });
  return {active, handlers, button, requests, prints: () => prints};
}

test('codes are added only on click, decoded before print and removed after printing', async () => {
  let resolve;
  const loading = new Promise(done => {resolve = done;});
  const app = setup(() => loading);
  assert.equal(app.active.size, 0);
  const pending = app.handlers.click();
  assert.equal(app.active.size, 2);
  assert.equal(app.prints(), 0);
  assert.equal(app.requests.length, 0);
  resolve();
  await pending;
  assert.equal(app.prints(), 1);
  assert.equal(app.requests.length, 1); // The button and beforeprint share one record.
  assert.equal(app.requests[0].url, '/materials/7/print-event/');
  assert.equal(app.requests[0].options.method, 'POST');
  assert.equal(app.requests[0].options.headers['X-CSRFToken'], 'csrf-token');
  app.handlers.afterprint();
  assert.equal(app.active.size, 0);
  await app.handlers.click();
  assert.equal(app.active.size, 2);
  assert.equal(app.prints(), 2);
  assert.equal(app.requests.length, 2);
  app.handlers.afterprint();
  assert.equal(app.active.size, 0);
});

test('print waits for the approved electronic seal to decode', async () => {
  let resolve;
  const loading = new Promise(done => {resolve = done;});
  const app = setup(() => Promise.resolve(), 2, [true], false, [{decode: () => loading}]);
  const pending = app.handlers.click();
  await Promise.resolve();
  assert.equal(app.prints(), 0);
  resolve();
  await pending;
  assert.equal(app.prints(), 1);
});

test('failed image decoding retains a usable retry button and does not print', async () => {
  const app = setup(() => Promise.reject(new Error('decode failed')));
  await app.handlers.click();
  assert.equal(app.active.size, 0);
  assert.equal(app.prints(), 0);
  assert.equal(app.requests.length, 0);
  assert.equal(app.button.disabled, false);
  assert.ok(app.handlers.feedback.textContent);
});

test('reports without verification codes still print', async () => {
  const app = setup(() => Promise.resolve(), 0);
  await app.handlers.click();
  assert.equal(app.prints(), 1);
});

test('browser print shortcuts record an initiation, with no duplicate before afterprint', async () => {
  const app = setup(() => Promise.resolve(), 0);
  await app.handlers.beforeprint();
  await app.handlers.beforeprint();
  assert.equal(app.requests.length, 1);
  app.handlers.afterprint();
  await app.handlers.beforeprint();
  assert.equal(app.requests.length, 2);
});

test('failed audit prevents button printing and allows a later retry', async () => {
  const app = setup(() => Promise.resolve(), 2, [false, true]);
  await app.handlers.click();
  assert.equal(app.prints(), 0);
  assert.equal(app.active.size, 0);
  assert.equal(app.button.disabled, false);
  assert.match(app.handlers.feedback.textContent, /操作记录保存失败/);
  await app.handlers.click();
  assert.equal(app.prints(), 1);
  assert.equal(app.requests.length, 2);
});

test('embedded HTML preview records browser printing without exposing a print button', async () => {
  const app = setup(() => Promise.resolve(), 0, [true], true);
  assert.equal(app.handlers.click, undefined);
  await app.handlers.beforeprint();
  assert.equal(app.requests.length, 1);
  assert.equal(app.active.size, 0);
});
