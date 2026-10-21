const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-upload-modal.js', 'utf8');

function setup(embedded = false) {
  const elements = [], handlers = {}, notices = [];
  let delegated;
  const element = tag => {
    const callbacks = {};
    const item = {tag, contentWindow: {}, open: false, style: {}, offsetHeight: 52,
      setAttribute() {}, append() {},
      addEventListener(name, handler) {callbacks[name] = handler;},
      showModal() {this.open = true;},
      close() {this.open = false; callbacks.close?.();},
    };
    elements.push(item);
    return item;
  };
  const window = {innerHeight: 1000, addEventListener(name, handler) {handlers[name] = handler;}};
  window.parent = embedded ? {oaOpenUpload(href) {delegated = href;}} : window;
  runInNewContext(source, {
    URL, window,
    location: {href: 'https://oa.test/workbench/', origin: 'https://oa.test'},
    CustomEvent: class {constructor(type) {this.type = type;}},
    document: {
      createElement: element, body: {append() {}},
      documentElement: {classList: {add() {}, remove() {}}},
      addEventListener(name, handler) {handlers[name] = handler;},
      querySelector: () => ({}),
      dispatchEvent(event) {notices.push(event.type);},
    },
  });
  return {window, handlers, notices, elements,
    delegated: () => delegated,
    dialog: () => elements.find(item => item.tag === 'dialog'),
    frame: () => elements.find(item => item.tag === 'iframe'),
  };
}

test('upload opens a modal and loads the embedded form without navigating', () => {
  const page = setup();
  let prevented = false;
  page.handlers.click({target: {closest: () => ({href: '/upload/1/'})},
    preventDefault() {prevented = true;}});
  assert.equal(prevented, true);
  assert.equal(page.dialog().open, true);
  assert.equal(page.frame().src, 'https://oa.test/upload/1/?embed=1');
});

test('upload from a preview iframe delegates to the containing window', () => {
  const page = setup(true);
  page.window.oaOpenUpload('/upload/1/');
  assert.equal(page.delegated(), 'https://oa.test/upload/1/');
  assert.equal(page.dialog(), undefined);
});

test('cancel closes the modal while completion also refreshes current materials', () => {
  const page = setup();
  for (const type of ['oa-upload-cancel', 'oa-upload-complete']) {
    page.window.oaOpenUpload('/upload/1/');
    page.handlers.message({origin: 'https://oa.test', source: page.frame().contentWindow,
      data: {type}});
    assert.equal(page.dialog().open, false);
  }
  assert.deepEqual(page.notices, ['oa-upload-complete']);
});

test('messages from unrelated frames and origins cannot close the modal', () => {
  const page = setup();
  page.window.oaOpenUpload('/upload/1/');
  page.handlers.message({origin: 'https://other.test', source: page.frame().contentWindow,
    data: {type: 'oa-upload-complete'}});
  page.handlers.message({origin: 'https://oa.test', source: {},
    data: {type: 'oa-upload-complete'}});
  assert.equal(page.dialog().open, true);
  assert.deepEqual(page.notices, []);
});

test('modal uses the material heading, has no close button, and fits its content', () => {
  const page = setup();
  page.window.oaOpenUpload('/upload/1/');
  const layout = height => page.handlers.message({
    origin: 'https://oa.test', source: page.frame().contentWindow,
    data: {type: 'oa-upload-layout', title: 'Y-GD20260024 · 服务结算单', height},
  });
  layout(360);
  assert.equal(page.elements.find(item => item.tag === 'strong').textContent,
    'Y-GD20260024 · 服务结算单');
  assert.equal(page.elements.some(item => item.tag === 'button'), false);
  assert.equal(page.frame().style.height, '360px');
  assert.equal(page.dialog().style.height, '414px');
  layout(2000);
  assert.equal(page.frame().style.height, '846px');
  assert.equal(page.dialog().style.height, '900px');
  page.window.innerHeight = 600;
  page.handlers.resize();
  assert.equal(page.frame().style.height, '486px');
  assert.equal(page.dialog().style.height, '540px');
  layout(240);
  assert.equal(page.dialog().style.height, '294px');
});
