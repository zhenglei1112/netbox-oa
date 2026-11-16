const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const script = readFileSync('netbox_oa/static/netbox_oa/maintenance-upload.js', 'utf8');

test('embedded upload reports its material heading and follows content height changes', () => {
  const messages = [];
  let changed;
  let height = 380;
  const page = {dataset: {uploadHeading: 'Y-GD261103B · 服务验收申请单'},
    getBoundingClientRect: () => ({height})};
  const input = {files: [], setCustomValidity() {}, addEventListener() {}};
  const drop = {querySelector: () => input, addEventListener() {}};
  const selected = {replaceChildren() {}};
  runInNewContext(script, {
    document: {
      querySelector: selector => ({
        '.oa-upload-drop': drop, '.oa-upload-selected': selected, '[data-upload-heading]': page,
      })[selector] || null,
    },
    window: {parent: {postMessage(data, origin) {messages.push({data, origin});}},
      addEventListener() {}},
    location: {origin: 'https://oa.test'},
    ResizeObserver: class {
      constructor(callback) {changed = callback;}
      observe(element) {assert.equal(element, page);}
    },
  });
  assert.equal(messages[0].data.title, 'Y-GD261103B · 服务验收申请单');
  assert.equal(messages[0].data.height, 380);
  assert.equal(messages[0].origin, 'https://oa.test');
  height = 560;
  changed();
  assert.equal(messages[1].data.height, 560);
});
