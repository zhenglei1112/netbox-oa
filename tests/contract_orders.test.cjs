const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const source = readFileSync('netbox_oa/static/netbox_oa/contract-orders.js', 'utf8');

function setup() {
  const events = target => Object.assign(target, {
    addEventListener(name, callback) {this[name] = callback;},
    removeEventListener(name) {delete this[name];},
  });
  const frame = events({hidden: true, getAttribute() {return this.src;},
    removeAttribute() {this.src = '';},
    contentDocument: events({querySelector() {return {};}, querySelectorAll() {return [];}}),
  });
  const status = {}, title = {}, button = events({});
  const dialog = events({dataset: {}, open: false,
    querySelector(selector) {
      return {'iframe': frame, '[data-oa-contract-status]': status,
        '#oa-contract-dialog-title': title, '[data-oa-contract-close]': button}[selector];
    },
    showModal() {this.open = true;},
  });
  // Keep close() separate from the close event callback.
  const listeners = {};
  dialog.addEventListener = (name, callback) => {listeners[name] = callback;};
  dialog.close = () => {dialog.open = false; listeners.close();};
  const document = events({getElementById(id) {
    return id === 'oa-contract-dialog' ? dialog : null;
  }});
  const timers = new Map(); let sequence = 0;
  runInNewContext(source, {document,
    setTimeout(callback) {timers.set(++sequence, callback); return sequence;},
    clearTimeout(id) {timers.delete(id);},
  });
  const link = {href: '/contracts/1/orders/2/flow/',
    getAttribute() {return '查看工单流程';}, focus() {this.focused = true;},
  };
  const open = () => document.click({target: {closest() {return link;}}, preventDefault() {}});
  return {dialog, frame, status, title, button, link, timers, open};
}

test('loads on demand, opens one modal, closes from iframe Escape and restores focus', () => {
  const ui = setup();
  assert.equal(ui.frame.src, undefined);
  ui.open();
  assert.equal(ui.dialog.open, true);
  assert.equal(ui.frame.hidden, true);
  assert.equal(ui.title.textContent, '查看工单流程');
  ui.frame.load();
  assert.equal(ui.frame.hidden, false);
  assert.equal(ui.status.hidden, true);
  ui.frame.contentDocument.keydown({key: 'Escape', preventDefault() {}});
  assert.equal(ui.dialog.open, false);
  assert.equal(ui.link.focused, true);
  assert.equal(ui.frame.src, '');
  assert.equal(ui.timers.size, 0);
});

test('Escape in a dynamically loaded nested form closes the modal and releases listeners', () => {
  const ui = setup(); ui.open(); ui.frame.load();
  const nested = {querySelectorAll() {return [];},
    addEventListener(name, callback) {this[name] = callback;},
    removeEventListener(name) {delete this[name];},
  };
  ui.frame.contentDocument.load({target: {tagName: 'IFRAME', contentDocument: nested}});
  nested.keydown({key: 'Escape', preventDefault() {}});
  assert.equal(ui.dialog.open, false);
  assert.equal(ui.link.focused, true);
  assert.equal(nested.keydown, undefined);
});

test('permission/error pages and timeouts show failure; reopening retries', () => {
  const ui = setup(); ui.open();
  ui.frame.contentDocument.querySelector = () => null;
  ui.frame.load();
  assert.match(ui.status.textContent, /加载失败/);
  assert.equal(ui.frame.hidden, true);
  ui.button.click(); ui.open();
  assert.match(ui.status.textContent, /正在加载/);
  [...ui.timers.values()][0]();
  assert.match(ui.status.textContent, /加载失败/);
  ui.button.click(); ui.open();
  ui.frame.contentDocument.querySelector = () => ({});
  ui.frame.load();
  assert.equal(ui.frame.hidden, false);
});
