const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-upload.js', 'utf8');

function setup(withSources = false, withUpdates = false) {
  const handlers = {};
  let displayed = [];
  const mappings = [];
  const sourceContainer = {hidden: false};
  const original = withSources ? {
    value: '1', options: [
      {value: '1', textContent: '原件一（待上传用印件）'},
      {value: '2', textContent: '原件二（待上传用印件）'},
    ],
    cloneNode: () => ({name: 'source_document_ids', setAttribute() {}}),
    closest: () => sourceContainer,
  } : null;
  const revisionContainer = {hidden: true};
  const revision = withUpdates ? {
    value: '11', options: [{value: '11'}, {value: '22'}],
    cloneNode: () => ({name: 'replaces_document_ids', setAttribute() {}}),
    closest: () => revisionContainer,
  } : null;
  const mode = withUpdates ? {value: 'new', addEventListener(_, handler) {
    handlers.mode = handler;
  }} : null;
  const input = {multiple: true, files: [], validity: '',
    setCustomValidity(message) {this.validity = message;}, reportValidity() {},
    addEventListener(name, handler) {handlers[name] = handler;}};
  const drop = {
    querySelector: () => input,
    classList: {add() {}, remove() {}},
    addEventListener(name, handler) {handlers[name] = handler;},
  };
  runInNewContext(source, {
    window: {oaNotify() {}},
    document: {
      querySelector: name => ['[data-upload-cancel]', '[data-upload-heading]',
        '[data-upload-form]', '[data-upload-loading]'].includes(name) ? null
        : name === '.oa-upload-mode select' ? mode
        : name === '.oa-upload-replacement select' ? revision
        : name === '.oa-upload-source select' ? original : name === '.oa-upload-drop' ? drop : {
        replaceChildren(...children) {displayed = children.map(child => child.textContent);},
      },
      createElement: () => ({append(choice) {mappings.push(choice);}}),
    },
    DataTransfer: class {
      files = [];
      items = {add: file => this.files.push(file)};
    },
  });
  return {input, handlers, names: () => displayed, mappings, original, sourceContainer,
    mode, revision, revisionContainer};
}

function setupSubmission() {
  const handlers = {};
  const button = {disabled: false};
  const loading = {hidden: true};
  const attrs = {};
  const form = {
    querySelector(selector) {assert.equal(selector, 'button[type="submit"]'); return button;},
    addEventListener(name, handler) {handlers[name] = handler;},
    setAttribute(name, value) {attrs[name] = value;},
    removeAttribute(name) {delete attrs[name];},
  };
  runInNewContext(source, {
    document: {querySelector: selector => ({
      '[data-upload-form]': form, '[data-upload-loading]': loading,
    })[selector] || null},
    window: {addEventListener(name, handler) {handlers[name] = handler;}},
  });
  return {handlers, button, loading, attrs};
}

test('upload shows the spinner, blocks duplicate submission and recovers on returning to the page', () => {
  const page = setupSubmission();
  let prevented = 0;
  const event = {preventDefault() {prevented += 1;}};
  page.handlers.submit(event);
  assert.equal(page.loading.hidden, false);
  assert.equal(page.button.disabled, true);
  assert.equal(page.attrs['aria-busy'], 'true');
  assert.equal(prevented, 0);
  page.handlers.submit(event);
  assert.equal(prevented, 1);
  page.handlers.pageshow();
  assert.equal(page.loading.hidden, true);
  assert.equal(page.button.disabled, false);
  assert.equal(page.attrs['aria-busy'], undefined);
  page.handlers.submit(event);
  assert.equal(page.loading.hidden, false);
  assert.equal(prevented, 1);
});

test('a cancelled form submission does not enter the uploading state', () => {
  const page = setupSubmission();
  page.handlers.submit({defaultPrevented: true});
  assert.equal(page.loading.hidden, true);
  assert.equal(page.button.disabled, false);
  assert.equal(page.attrs['aria-busy'], undefined);
});

test('selected filename is displayed as text and clears previous errors', () => {
  const page = setup();
  page.input.validity = 'previous error';
  page.input.files = [{name: '<scan>.pdf'}];
  page.handlers.change();
  assert.deepEqual(page.names(), ['<scan>.pdf']);
  assert.equal(page.input.validity, '');
});

test('dropping one file selects it', () => {
  const page = setup();
  let prevented = false;
  page.handlers.drop({
    preventDefault() {prevented = true;},
    dataTransfer: {files: [{name: 'one.pdf'}]},
  });
  assert.equal(prevented, true);
  assert.deepEqual(page.names(), ['one.pdf']);
  assert.equal(page.input.files.length, 1);
});

test('dropping multiple files selects all dropped files', () => {
  const page = setup();
  page.input.files = [{name: 'existing.pdf'}];
  page.handlers.change();
  page.handlers.drop({
    preventDefault() {},
    dataTransfer: {files: [{name: 'one.pdf'}, {name: 'two.pdf'}]},
  });
  assert.deepEqual(page.names(), ['one.pdf', 'two.pdf']);
  assert.equal(page.input.files.length, 2);
  assert.equal(page.input.validity, '');
});

test('each selected sealed file gets its own source selector', () => {
  const page = setup(true);
  page.input.files = [{name: 'one.pdf'}, {name: 'two.pdf'}];
  page.handlers.change();
  assert.deepEqual(page.mappings.map(choice => choice.value), ['1', '2']);
  assert.ok(page.mappings.every(choice => choice.name === 'source_document_ids'));
  assert.equal(page.original.disabled, true);
  assert.equal(page.sourceContainer.hidden, true);
});

test('new uploads have no replacement links and updates select a target per file', () => {
  const page = setup(false, true);
  page.input.files = [{name: 'one.pdf'}, {name: 'two.pdf'}];
  page.handlers.change();
  assert.equal(page.mappings.length, 0);
  assert.equal(page.revision.disabled, true);
  page.mode.value = 'replace';
  page.handlers.mode();
  assert.deepEqual(page.mappings.map(choice => choice.value), ['11', '22']);
  assert.ok(page.mappings.every(choice => choice.name === 'replaces_document_ids'));
});

test('single-file company form rejects multiple dropped files', () => {
  const page = setup();
  page.input.multiple = false;
  page.handlers.drop({preventDefault() {},
    dataTransfer: {files: [{name: 'one.pdf'}, {name: 'two.pdf'}]}});
  assert.equal(page.input.files.length, 0);
  assert.equal(page.input.validity, '此类材料每次只能上传一个文件。');
});
