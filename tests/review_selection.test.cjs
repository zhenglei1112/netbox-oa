const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-review.js', 'utf8');

function link(href, classes, current) {
  const attributes = new Map([['href', href]]);
  if (current) attributes.set('aria-current', current);
  const values = new Set(classes.split(' '));
  return {
    href: `https://oa.test/review/${href}`,
    classList: {
      contains: name => values.has(name),
      toggle(name, enabled) {if (enabled) values.add(name); else values.delete(name);},
    },
    hasAttribute: name => attributes.has(name),
    getAttribute: name => attributes.get(name) ?? null,
    setAttribute(name, value) {attributes.set(name, value);},
    removeAttribute(name) {attributes.delete(name);},
  };
}

function setup() {
  const links = [
    link('?material=notice', 'material-title selected', 'true'),
    link('?material=notice&document=1', 'btn material-file btn-primary selected', 'page'),
    link('?material=settlement', 'material-title'),
    link('?material=settlement&document=2', 'btn material-file btn-ghost-primary'),
    link('?material=settlement&document=3', 'btn material-file btn-ghost-primary'),
  ];
  let click;
  let view;
  let removed = 0;
  const newView = () => ({
    classList: {add() {}, remove() {}},
    querySelector: () => null,
    setAttribute() {}, removeAttribute() {}, remove() {removed++;},
  });
  view = newView();
  const navigation = {
    addEventListener(_, handler) {click = handler;},
    querySelectorAll: () => links,
  };
  const location = {href: links[1].href};
  let respond;
  let reject;
  const document = {
    title: '付款通知书',
    querySelector: selector => selector === 'aside nav' ? navigation : {
      append(element) {if (element.querySelector) view = element;},
      querySelector: () => view,
    },
    createElement: () => ({setAttribute() {}}),
    importNode: node => node,
  };
  runInNewContext(source, {
    document, location, URL, AbortController,
    history: {replaceState(_, __, url) {location.href = url.href;}},
    fetch: () => new Promise((resolve, fail) => {respond = resolve; reject = fail;}),
    DOMParser: class {
      parseFromString(selected) {
        return {
          title: '服务结算单',
          querySelector: () => newView(),
          querySelectorAll: () => links.map((item, index) => link(
            item.getAttribute('href'), '', index === 2 ? 'true' : index === selected ? 'page' : null,
          )),
        };
      }
    },
  });
  return {
    links, document,
    get removed() {return removed;},
    click(index) {
      return click({target: {closest: () => links[index]}, preventDefault() {}});
    },
    finish(selected) {respond({ok: true, text: async () => selected});},
    fail() {reject(new Error('加载失败'));},
  };
}

test('switching material and file keeps native button colors aligned with the displayed file', async () => {
  const page = setup();
  const switching = page.click(2);
  // Retain the previous selection until the replacement content is ready.
  assert.equal(page.links[1].classList.contains('btn-primary'), true);
  page.finish(3);
  await switching;
  assert.equal(page.links[1].classList.contains('btn-primary'), false);
  assert.equal(page.links[1].classList.contains('btn-ghost-primary'), true);
  assert.equal(page.links[1].getAttribute('aria-current'), null);
  assert.equal(page.links[3].classList.contains('btn-primary'), true);
  assert.equal(page.links[3].classList.contains('btn-ghost-primary'), false);
  assert.equal(page.links[3].getAttribute('aria-current'), 'page');
  assert.equal(page.links[2].classList.contains('selected'), true);
  assert.equal(page.links[2].classList.contains('btn-primary'), false);
  assert.equal(page.document.title, '服务结算单');
  assert.equal(page.removed, 1);

  const nextFile = page.click(4);
  page.finish(4);
  await nextFile;
  assert.equal(page.links[3].classList.contains('btn-primary'), false);
  assert.equal(page.links[3].classList.contains('btn-ghost-primary'), true);
  assert.equal(page.links[4].classList.contains('btn-primary'), true);
  assert.equal(page.links[4].getAttribute('aria-current'), 'page');
});

test('failed material loading preserves the displayed file and its selected button', async () => {
  const page = setup();
  const switching = page.click(2);
  page.fail();
  await switching;
  assert.equal(page.links[1].classList.contains('btn-primary'), true);
  assert.equal(page.links[1].getAttribute('aria-current'), 'page');
  assert.equal(page.links[3].classList.contains('btn-ghost-primary'), true);
  assert.equal(page.links[3].getAttribute('aria-current'), null);
  assert.equal(page.document.title, '付款通知书');
  assert.equal(page.removed, 0);
});
