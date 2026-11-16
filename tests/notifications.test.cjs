const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');
const source = readFileSync('netbox_oa/static/netbox_oa/notifications.js', 'utf8');

function setup(parent) {
  const shown = [], events = {}, scripts = [];
  class Element {
    constructor() {
      this.children = []; this.attrs = {}; this.dataset = {}; this.style = {}; this.events = {};
    }
    append(...elements) {this.children.push(...elements);}
    setAttribute(key, value) {this.attrs[key] = value;}
    hasAttribute(key) {return key in this.attrs;}
    addEventListener(name, handler) {this.events[name] = handler;}
    matches() {return false;}
    querySelector() {return null;}
    closest() {return null;}
    remove() {}
  }
  const body = new Element();
  const document = {
    body, head: {append(script) {scripts.push(script);}}, readyState: 'complete',
    currentScript: {dataset: {netboxJs: '/static/netbox.js'}},
    querySelector: () => null, querySelectorAll: () => [],
    createElement: () => new Element(),
    addEventListener(name, callback) {events[name] = callback;},
  };
  const window = {location: {origin: 'https://oa.test'}, Toast: class {
    constructor(element, options) {this.element = element; this.options = options;}
    show() {shown.push(this.element);}
    dispose() {}
    hide() {this.element.events['hidden.bs.toast']?.();}
  }};
  window.parent = parent || window;
  runInNewContext(source, {window, document, location: window.location,
    MutationObserver: class {observe() {}}, Date, Map, WeakMap});
  return {window, document, shown, events, scripts, Element};
}

test('uses native NetBox Toast, bottom right placement, safe text and semantic severity', () => {
  const app = setup();
  app.window.oaNotify('<img src=x onerror=alert(1)>', 'danger');
  assert.equal(app.shown.length, 1);
  const toast = app.shown[0];
  assert.equal(toast.attrs.role, 'alert');
  assert.equal(toast.children[0].className, 'toast-header text-bg-danger');
  assert.equal(toast.children[1].textContent, '<img src=x onerror=alert(1)>');
  assert.match(app.document.body.children[0].className, /bottom-0 end-0/);
  app.window.oaNotify('<img src=x onerror=alert(1)>', 'danger');
  assert.equal(app.shown.length, 1, 'Repeated polling/errors must not flood the screen');
});

test('embedded upload/review notifications appear in the same-origin parent', () => {
  const forwarded = [];
  const app = setup({location: {origin: 'https://oa.test'},
    oaNotify: (...args) => forwarded.push(args)});
  app.window.oaNotify('上传成功', 'success');
  assert.deepEqual(forwarded, [['上传成功', 'success']]);
  assert.equal(app.shown.length, 0);
});

test('server validation errors and native message fragments are consumed after AJAX navigation', () => {
  const app = setup();
  const error = new app.Element();
  error.textContent = '请先完成本环节手写签名后再提交。';
  app.window.oaNotifyPage({querySelectorAll: () => [error]});
  assert.equal(error.hidden, true);
  assert.equal(app.shown[0].children[1].textContent, error.textContent);
  app.window.oaNotifyPage({querySelectorAll: () => [error]});
  assert.equal(app.shown.length, 1);
});

test('browser validation is routed to a toast instead of a separate browser bubble', () => {
  const app = setup();
  let prevented = false;
  app.events.invalid({preventDefault() {prevented = true;},
    target: {validationMessage: '请选择一个文件。'}});
  assert.equal(prevented, true);
  assert.equal(app.shown[0].children[1].textContent, '请选择一个文件。');
});

test('standalone pages load the installed NetBox JavaScript and then show queued messages', () => {
  const app = setup();
  const Toast = app.window.Toast;
  delete app.window.Toast;
  app.window.oaNotify('请先书写完整名字。');
  assert.equal(app.scripts[0].src, '/static/netbox.js');
  app.window.Toast = Toast;
  app.scripts[0].onload();
  assert.equal(app.shown[0].children[1].textContent, '请先书写完整名字。');
});

test('missing service effect identifies the required field instead of a generic browser message', () => {
  const app = setup();
  app.events.invalid({preventDefault() {}, target: {
    validity: {valueMissing: true}, dataset: {requiredMessage: '请选择验收结论。'},
    validationMessage: '请在列表中选择一项。',
  }});
  assert.equal(app.shown[0].children[1].textContent, '请选择验收结论。');
});

test('custom required prompts do not hide other validation errors', () => {
  const app = setup();
  app.events.invalid({preventDefault() {}, target: {
    validity: {valueMissing: false}, dataset: {requiredMessage: '请选择验收结论。'},
    validationMessage: '请选择有效选项。',
  }});
  assert.equal(app.shown[0].children[1].textContent, '请选择有效选项。');
});

test('deletion confirmation uses a native toast and resolves only after an explicit choice', async () => {
  const app = setup();
  let resolved = false;
  const result = app.window.oaConfirm('确定删除这份材料？').then(value => {resolved = true; return value;});
  await Promise.resolve();
  assert.equal(resolved, false);
  const actions = app.shown[0].children[1].children[0];
  assert.equal(actions.children[1].className, 'btn btn-danger');
  actions.children[1].events.click();
  assert.equal(await result, true);
  const cancelled = app.window.oaConfirm('确定删除另一份材料？');
  app.shown[1].children[1].children[0].children[0].events.click();
  assert.equal(await cancelled, false);
});
