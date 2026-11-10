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
  const notifications = [];
  runInNewContext(source, {document, window: {oaNotify: text => notifications.push(text)},
    setTimeout(callback) {timers.set(++sequence, callback); return sequence;},
    clearTimeout(id) {timers.delete(id);},
  });
  const link = {href: '/contracts/1/orders/2/flow/',
    getAttribute() {return '查看工单流程';}, focus() {this.focused = true;},
  };
  const open = () => document.click({target: {closest() {return link;}}, preventDefault() {}});
  return {dialog, frame, status, title, button, link, timers, open, notifications};
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
  assert.match(ui.notifications.at(-1), /加载失败/);
  assert.equal(ui.frame.hidden, true);
  ui.button.click(); ui.open();
  assert.match(ui.status.textContent, /正在加载/);
  [...ui.timers.values()][0]();
  assert.match(ui.notifications.at(-1), /加载失败/);
  ui.button.click(); ui.open();
  ui.frame.contentDocument.querySelector = () => ({});
  ui.frame.load();
  assert.equal(ui.frame.hidden, false);
});


test('clearing repeated linked filters restores original payment nodes and native handlers', async () => {
  class Node {
    constructor() {this.childNodes = []; this.listeners = {};}
    get children() {return this.childNodes;}
    append(...nodes) {
      for (const node of nodes) {
        if (node.parent) node.parent.childNodes.splice(node.parent.childNodes.indexOf(node), 1);
        this.childNodes.push(node); node.parent = this;
      }
    }
    replaceChildren(...nodes) {this.childNodes = []; this.append(...nodes.flatMap(
      node => node.fragment ? [...node.childNodes] : [node]));}
    set innerHTML(value) {this.childNodes = [new Node()]; this.html = value;}
    get innerHTML() {return this.html || '';}
    addEventListener(name, callback) {this.listeners[name] = callback;}
    setAttribute() {}
    removeAttribute() {}
    after() {}
    before() {}
    replaceWith() {}
    querySelector() {return null;}
    querySelectorAll() {return [];}
  }
  const section = new Node(); section.dataset = {};
  const header = new Node();
  header.childNodes = [{nodeType: 3, textContent: '付款'}];
  header.querySelectorAll = () => [];
  const card = new Node();
  header.closest = selector => selector === '.card' ? card : null;
  const nativeTable = new Node();
  let nativeRequests = 0;
  nativeTable.addEventListener('click', () => {nativeRequests++;});
  card.append(header, nativeTable);
  const document = new Node();
  document.getElementById = id => id === 'oa-contract-orders' ? section : null;
  document.querySelectorAll = () => [header];
  document.createElement = () => new Node();
  document.createDocumentFragment = () => Object.assign(new Node(), {fragment: true});
  class DOMParser {
    parseFromString() {return {body: {innerHTML: '<div>filtered payments</div>'},
      querySelector: selector => selector === '[data-oa-payment-results]' ? {} : null};}
  }
  let fail = false;
  runInNewContext(source, {document, window: {oaNotify: text => notifications.push(text)}, URL, AbortController, DOMParser,
    MutationObserver: class {observe() {}},
    window: {location: {href: 'https://example.test/contracts/1/'}, alert() {}},
    fetch: async () => ({ok: !fail, text: async () => 'filtered'}),
  });
  const results = card.children.find(node => node !== header);
  const attributes = new Map([['data-oa-payment-url', '/orders/1/payments/']]);
  let spinning = false;
  const filterButton = {
    getAttribute: name => attributes.get(name) || null,
    setAttribute: (name, value) => attributes.set(name, value),
    removeAttribute: name => attributes.delete(name),
    querySelector: () => ({classList: {toggle(name, value) {spinning = value;}}}),
  };
  const event = clear => ({preventDefault() {}, target: {closest(selector) {
    if (selector === '[data-oa-payment-clear]') return clear ? {} : null;
    if (selector === '[data-oa-payment-filter]') return clear ? null : filterButton;
    return null;
  }}});
  for (let cycle = 0; cycle < 2; cycle++) {
    const request = section.listeners.click(event(false));
    assert.equal(attributes.get('aria-disabled'), 'true');
    assert.equal(attributes.has('disabled'), true);
    assert.equal(spinning, true);
    await request;
    assert.equal(attributes.has('disabled'), false);
    assert.equal(attributes.has('aria-busy'), false);
    assert.equal(spinning, false);
    assert.notEqual(results.children[0], nativeTable);
    await card.listeners.click(event(true));
    assert.equal(results.children[0], nativeTable);
    results.children[0].listeners.click();
  }
  assert.equal(nativeRequests, 2);
  fail = true;
  await section.listeners.click(event(false));
  assert.equal(attributes.has('disabled'), false);
  assert.equal(spinning, false);
});
