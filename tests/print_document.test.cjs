const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-print.js', 'utf8');

function setup(decode, count = 2) {
  const active = new Set(), handlers = {};
  const button = {addEventListener(_, fn) {handlers.click = fn;}, after(node) {handlers.feedback = node;}};
  const templates = Array.from({length: count}, () => ({
    content: {firstElementChild: {cloneNode() {
      return {querySelectorAll: () => [{decode}], remove() {active.delete(this);}};
    }}},
    before(code) {active.add(code);},
  }));
  let prints = 0;
  runInNewContext(source, {
    document: {
      getElementById: () => button,
      querySelectorAll: () => templates,
      createElement: () => ({setAttribute() {}, remove() {}}),
    },
    window: {
      addEventListener(name, fn) {handlers[name] = fn;},
      print() {assert.equal(active.size, count); prints++;},
    },
  });
  return {active, handlers, button, prints: () => prints};
}

test('codes are added only on click, decoded before print and removed after printing', async () => {
  let resolve;
  const loading = new Promise(done => {resolve = done;});
  const app = setup(() => loading);
  assert.equal(app.active.size, 0);
  const pending = app.handlers.click();
  assert.equal(app.active.size, 2);
  assert.equal(app.prints(), 0);
  resolve();
  await pending;
  assert.equal(app.prints(), 1);
  app.handlers.afterprint();
  assert.equal(app.active.size, 0);
  await app.handlers.click();
  assert.equal(app.active.size, 2);
  assert.equal(app.prints(), 2);
  app.handlers.afterprint();
  assert.equal(app.active.size, 0);
});

test('failed image decoding retains a usable retry button and does not print', async () => {
  const app = setup(() => Promise.reject(new Error('decode failed')));
  await app.handlers.click();
  assert.equal(app.active.size, 0);
  assert.equal(app.prints(), 0);
  assert.equal(app.button.disabled, false);
  assert.ok(app.handlers.feedback.textContent);
});

test('reports without verification codes still print', async () => {
  const app = setup(() => Promise.resolve(), 0);
  await app.handlers.click();
  assert.equal(app.prints(), 1);
});
