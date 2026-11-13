const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-billing-modal.js', 'utf8');
const embedded = readFileSync('netbox_oa/static/netbox_oa/maintenance-billing-embed.js', 'utf8');

function setup(withAmount = true) {
  const elements = [], events = {}, notices = [], modals = [];
  let reloads = 0;
  const amount = {value: '100.00', changes: [], dispatchEvent(event) {this.changes.push(event.type);}};
  const summary = {innerHTML: '<section>原明细</section>'};
  const form = {querySelector: selector => selector === '[data-billing-summary]' ? summary : amount,
    serviceEffect: 'passed'};
  const trigger = {href: '/billing/manage/?return_to=acceptance', dataset: {},
    isConnected: true, closest: () => withAmount ? form : null, focus() {this.focused = true;}};
  const element = tag => {
    const node = {tag, attrs: {}, style: {}, children: [], events: {}, contentWindow: {},
      offsetHeight: 54,
      setAttribute(key, value) {this.attrs[key] = value;},
      append(...items) {this.children.push(...items);},
      addEventListener(name, handler) {this.events[name] = handler;},
    };
    elements.push(node); return node;
  };
  const window = {innerHeight: 1000, oaNotify(...args) {notices.push(args);},
    addEventListener(name, handler) {events[name] = handler;},
    Modal: class {
      constructor(root, options) {this.root = root; this.options = options; modals.push(this);}
      show() {this.visible = true; this.root.events['shown.bs.modal']?.();}
      hide() {this.visible = false; this.root.events['hidden.bs.modal']?.();}
    },
  };
  runInNewContext(source, {window, URL, Event: class {constructor(type) {this.type = type;}},
    location: {href: 'https://oa.test/acceptance/', origin: 'https://oa.test', reload() {reloads++;}},
    document: {body: {append() {}}, createElement: element,
      addEventListener(name, handler) {events[name] = handler;}, querySelector: () => ({}),
    },
  });
  const open = () => {
    let prevented = false;
    events.click({target: {closest: () => trigger}, preventDefault() {prevented = true;}});
    return prevented;
  };
  return {window, events, elements, notices, modals, amount, summary, form, trigger, open,
    reloads: () => reloads,
    frame: () => elements.find(item => item.tag === 'iframe'),
    message(data, origin = 'https://oa.test', otherSource) {
      events.message({origin, source: otherSource || this.frame().contentWindow, data});
    },
  };
}

test('maintenance opens a native Bootstrap modal with backdrop and a direct embedded editor', () => {
  const page = setup();
  assert.equal(page.open(), true);
  assert.equal(page.modals[0].options.backdrop, true);
  assert.equal(page.modals[0].options.focus, true);
  assert.equal(page.modals[0].visible, true);
  assert.equal(page.frame().src,
    'https://oa.test/billing/manage/?return_to=acceptance&embed=1');
  assert.equal(page.elements.find(item => item.tag === 'h2').textContent, '维护计费明细');
  const loading = page.elements.find(item => item.className === 'oa-upload-loading oa-billing-loading');
  assert.equal(loading.hidden, false);
  assert.equal(loading.children[0].className, 'mdi mdi-loading mdi-spin text-primary');
  assert.equal(page.frame().style.visibility, 'hidden');
  page.message({type: 'oa-billing-layout', height: 520});
  assert.equal(loading.hidden, true);
  assert.equal(page.frame().style.visibility, '');
  assert.equal(page.frame().style.height, '520px');
  page.message({type: 'oa-billing-layout', height: 2000});
  assert.equal(page.frame().style.height, '844px');
});

test('loading resets on reopening and iframe error pages can finish loading without layout messages', () => {
  const page = setup(); page.open();
  const loading = page.elements.find(item => item.className === 'oa-upload-loading oa-billing-loading');
  page.message({type: 'oa-billing-layout', height: 520}, 'https://other.test');
  assert.equal(loading.hidden, false);
  page.frame().contentDocument = {URL: 'about:blank'};
  page.frame().events.load();
  assert.equal(loading.hidden, false);
  page.frame().contentDocument.URL = 'https://oa.test/billing/manage/?embed=1';
  page.frame().events.load();
  assert.equal(loading.hidden, true);
  page.message({type: 'oa-billing-cancel'});
  page.open();
  assert.equal(loading.hidden, false);
  assert.equal(page.frame().style.visibility, 'hidden');
});

test('saving updates the acceptance amount and table while preserving unsubmitted fields', () => {
  const page = setup(); page.open();
  page.message({type: 'oa-billing-saved', total: '3800.00', summaryHtml: '<section>新明细</section>'});
  assert.equal(page.amount.value, '3800.00');
  assert.equal(page.summary.innerHTML, '<section>新明细</section>');
  assert.deepEqual(page.amount.changes, ['change']);
  assert.equal(page.form.serviceEffect, 'passed');
  assert.equal(page.modals[0].visible, false);
  assert.equal(page.frame().src, 'about:blank');
  assert.equal(page.reloads(), 0);
  assert.equal(page.trigger.focused, true);
});

test('cancellation and unrelated messages cannot change the acceptance amount', () => {
  const page = setup(); page.open();
  page.message({type: 'oa-billing-saved', total: '99.00', summaryHtml: '外部消息'}, 'https://other.test');
  page.message({type: 'oa-billing-saved', total: '99.00', summaryHtml: '其他窗口'}, 'https://oa.test', {});
  page.message({type: 'oa-billing-saved', total: 'NaN'});
  assert.equal(page.amount.value, '100.00');
  assert.equal(page.modals[0].visible, true);
  page.message({type: 'oa-billing-cancel'});
  assert.equal(page.amount.value, '100.00');
  assert.equal(page.summary.innerHTML, '<section>原明细</section>');
  assert.equal(page.modals[0].visible, false);
  assert.equal(page.reloads(), 0);
});

test('successful maintenance refreshes a standalone overview after closing', () => {
  const page = setup(false); page.open();
  page.message({type: 'oa-billing-saved', total: '99.00'});
  assert.equal(page.reloads(), 1);
});

test('embedded cancellation, responsive height and saved amount use same-origin parent messages', () => {
  const sent = [], events = {};
  let resized;
  const page = {getBoundingClientRect: () => ({height: 520})};
  const cancel = {addEventListener(name, handler) {events[name] = handler;}};
  const window = {parent: {postMessage(data, origin) {sent.push({data, origin});}},
    addEventListener() {}};
  const context = {window, location: {origin: 'https://oa.test'},
    document: {querySelector: selector => ({
      '[data-billing-cancel]': cancel, '[data-billing-editor]': page,
    })[selector] || null},
    ResizeObserver: class {constructor(callback) {resized = callback;} observe() {}},
  };
  runInNewContext(embedded, context);
  assert.equal(sent[0].data.height, 520);
  resized();
  let prevented = false;
  events.click({preventDefault() {prevented = true;}});
  assert.equal(prevented, true);
  assert.equal(sent.at(-1).data.type, 'oa-billing-cancel');
  assert.equal(sent.at(-1).origin, 'https://oa.test');
  context.document.querySelector = selector => ({
    '[data-billing-saved]': {dataset: {total: '3800.00'}},
    '#oa-billing-summary-result': {textContent: JSON.stringify('<section>已保存明细</section>')},
  })[selector] || null;
  runInNewContext(embedded, context);
  assert.equal(sent.at(-1).data.type, 'oa-billing-saved');
  assert.equal(sent.at(-1).data.total, '3800.00');
  assert.equal(sent.at(-1).data.summaryHtml, '<section>已保存明细</section>');
});
