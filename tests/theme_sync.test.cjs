const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const script = readFileSync('netbox_oa/static/netbox_oa/maintenance-theme.js', 'utf8');

function setup({embedded = false, blocked = false, dark = false} = {}) {
  let theme;
  let parentTheme = 'dark';
  let parentChanged;
  let preferenceChanged;
  const root = {getAttribute: () => theme, setAttribute: (_, value) => { theme = value; }};
  const media = {matches: dark, addEventListener: (_, callback) => {preferenceChanged = callback;}};
  const window = {matchMedia: () => media, addEventListener() {}};
  window.parent = embedded ? {document: {documentElement: {getAttribute: () => parentTheme}}} : window;
  if (blocked) Object.defineProperty(window.parent, 'document', {get() {throw new Error('Cross origin');}});
  runInNewContext(script, {
    document: {documentElement: root}, window,
    MutationObserver: class {
      constructor(callback) {parentChanged = callback;}
      observe() {}
      disconnect() {}
    },
  });
  return {
    get theme() {return theme;},
    setParent(value) {parentTheme = value; parentChanged();},
    setPreference(value) {media.matches = value; preferenceChanged();},
  };
}

test('embedded review and upload follow parent theme, including later changes', () => {
  const page = setup({embedded: true});
  assert.equal(page.theme, 'dark');
  page.setParent('light');
  assert.equal(page.theme, 'light');
  page.setPreference(true);
  assert.equal(page.theme, 'light');
  page.setParent('dark');
  assert.equal(page.theme, 'dark');
});

test('standalone and inaccessible parent pages follow system preference', () => {
  for (const options of [{}, {embedded: true, blocked: true}]) {
    const page = setup(options);
    assert.equal(page.theme, 'light');
    page.setPreference(true);
    assert.equal(page.theme, 'dark');
    page.setPreference(false);
    assert.equal(page.theme, 'light');
  }
});
