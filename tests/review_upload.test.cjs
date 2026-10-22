const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-review.js', 'utf8');

function setup(embedded, triggerAvailable = true) {
  let handler;
  let opened = 0;
  let prevented = false;
  let selector;
  const navigation = {querySelectorAll: () => [], addEventListener(_, callback) { handler = callback; }};
  const main = {dataset: {}, querySelectorAll: () => [], parentElement: {append() {}}, addEventListener() {}};
  const window = {addEventListener() {}};
  window.parent = embedded ? {postMessage() {}, document: {querySelector(value) {
    selector = value;
    return triggerAvailable ? {click() { opened++; }} : null;
  }}} : window;
  runInNewContext(source, {
    window, location: {origin: 'https://oa.test'},
    IntersectionObserver: class {observe() {}},
    ResizeObserver: class {observe() {}},
    document: {
      querySelector: value => value === 'aside nav' ? navigation : main,
      createElement: () => ({setAttribute() {}}),
    },
  });
  return {
    async click() {
      await handler({
        target: {closest: () => ({
          hasAttribute: name => name === 'data-review-upload',
          dataset: {reviewUpload: 'report'},
        })},
        preventDefault() { prevented = true; },
      });
      return {opened, prevented, selector};
    },
  };
}

test('embedded upload follows its standalone page link', async () => {
  const result = await setup(true).click();
  assert.equal(result.opened, 0);
  assert.equal(result.prevented, false);
  assert.equal(result.selector, undefined);
});

test('standalone upload retains normal navigation to the upload panel', async () => {
  const result = await setup(false).click();
  assert.equal(result.opened, 0);
  assert.equal(result.prevented, false);
});

test('embedded upload retains its fallback link when the parent panel is unavailable', async () => {
  const result = await setup(true, false).click();
  assert.equal(result.opened, 0);
  assert.equal(result.prevented, false);
});
