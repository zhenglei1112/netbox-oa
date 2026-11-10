const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-review.js', 'utf8');

function element(dataset = {}, classes = '') {
  const attrs = new Map();
  const values = new Set(classes.split(' '));
  return {dataset, children: [], style: {}, clientWidth: 800,
    classList: {contains: name => values.has(name), add: name => values.add(name), remove: name => values.delete(name),
      toggle(name, on) {if (on) values.add(name); else values.delete(name);}},
    setAttribute: (name, value) => attrs.set(name, value),
    getAttribute: name => attrs.get(name) ?? null,
    hasAttribute: name => attrs.has(name), removeAttribute: name => attrs.delete(name),
    append(...children) {this.children.push(...children);},
    replaceChildren(...children) {this.children = children;},
    addEventListener(name, fn) {this[name] = fn;},
    querySelector: () => null,
    getBoundingClientRect: () => ({top: 0, bottom: 40}),
  };
}
function setup(types = ['download', 'download', 'download'], selection = {}) {
  const stream = element({selected: '1', material: 'order', orderNumber: 'Y-001', ...selection});
  stream.clientHeight = 600;
  stream.scrollTop = 0;
  stream.scrollHeight = 1000;
  stream.getBoundingClientRect = () => ({top: 0, bottom: 600});
  stream.focus = () => {stream.focused = true;};
  const navigation = element();
  navigation.clientHeight = 600;
  navigation.getBoundingClientRect = () => ({top: 0, bottom: 600});
  const articles = types.map((type, i) => {
    const article = element({document: String(i + 1), material: i === 0 ? 'order' : 'report',
      type,
      title: `Material ${i + 1}`, source: `/file/${i + 1}`});
    article.content = element();
    article.querySelector = () => article.content;
    article.hidden = article.dataset.document !== stream.dataset.selected;
    return article;
  });
  const files = articles.map(article => {
    const link = element({}, 'material-file');
    link.href = `https://oa.test/review/?material=${article.dataset.material}&document=${article.dataset.document}`;
    return link;
  });
  const titles = ['order', 'report', 'empty'].map(material => {
    const link = element({}, 'material-title');
    link.href = `https://oa.test/review/?material=${material}`;
    return link;
  });
  const rows = files.map(file => {
    const link = element({}, 'material-row-link');
    link.href = file.href;
    link.closest = () => ({querySelector: () => file.classList.contains('selected') ? file : null});
    return link;
  });
  stream.querySelectorAll = () => articles;
  navigation.querySelectorAll = selector => ({'a.material-file': files, 'a.material-title': titles,
    'a.material-row-link': rows})[selector];
  stream.parentElement = element();
  const resources = [];
  const readyMessages = [];
  const document = {title: '', querySelector: selector => selector === 'aside nav' ? navigation : stream,
    createDocumentFragment: () => element(),
    createElement(tag) {const node = element(); node.tagName = tag.toUpperCase(); resources.push(node); return node;}};
  const location = {href: 'https://oa.test/review/?embed=1&material=order&document=1'};
  runInNewContext(source, {document, location, URL,
    history: {state: {}, replaceState(_, __, url) {location.href = url.href;}},
    setTimeout: () => 1, clearTimeout() {},
    window: {parent: {postMessage(message) {readyMessages.push(message);}}, addEventListener() {}},
  });
  return {articles, files, titles, rows, stream, document, location, resources, readyMessages,
    async settle() {await Promise.resolve(); await Promise.resolve(); await Promise.resolve();},
    async click(link, extra = {}) {
      let prevented = false;
      await navigation.click({target: {closest: () => link},
        preventDefault() {prevented = true;}, ...extra});
      return prevented;
    },

  };
}

test('an empty document category does not fall back to a different category', async () => {
  const page = setup(undefined, {selected: '', category: 'sealed'});
  await page.settle();
  assert.ok(page.articles.every(article => article.hidden));
  assert.ok(page.files.every(link => !link.hasAttribute('aria-current')));
  await page.click(page.files[0]);
  await page.settle();
  assert.equal(page.articles[0].hidden, false);
});

test('preview leaves wheel and keyboard scrolling entirely to the browser', async () => {
  const page = setup();
  await page.settle();
  assert.equal(page.stream.wheel, undefined);
  assert.equal(page.stream.keydown, undefined);
  assert.equal(page.files[0].getAttribute('aria-current'), 'page');
});

test('PDF and electronic forms embed their original URLs without inspecting frame contents', async () => {
  for (const type of ['pdf', 'form']) {
    const page = setup(['download', type, 'download']);
    await page.settle();
    const switching = page.click(page.files[1]);
    const iframe = page.resources.find(node => node.tagName === 'IFRAME');
    assert.equal(iframe.src, '/file/2');
    assert.equal(iframe.className, 'review-native-preview');
    Object.defineProperty(iframe, 'contentDocument', {get() {throw new Error('Native viewer is opaque');}});
    iframe.onload();
    await switching;
    assert.equal(page.files[1].getAttribute('aria-current'), 'page');
    assert.equal(page.rows[1].getAttribute('aria-current'), 'true');
    assert.equal(page.titles[1].getAttribute('aria-current'), 'true');
    assert.equal(new URL(page.location.href).searchParams.get('embed'), '1');
    assert.deepEqual(page.articles.map(article => article.hidden), [true, false, true]);
    assert.equal(page.articles[0].classList.contains('review-loaded'), false);
  }
});

test('clicking the directory switches the single visible document and empty groups retain it', async () => {
  const page = setup();
  await page.settle();
  await page.click(page.titles[1]);
  assert.equal(page.files[1].getAttribute('aria-current'), 'page');
  assert.equal(page.stream.focused, true);
  await page.click(page.files[2]);
  assert.deepEqual(page.articles.map(article => article.hidden), [true, true, false]);
  assert.equal(page.document.title, '审阅 · Material 3 · Y-001');
  await page.click(page.titles[2]);
  assert.equal(page.files[2].getAttribute('aria-current'), 'page');
  assert.equal(await page.click(page.files[0], {ctrlKey: true}), false);
  assert.equal(await page.click(null), false);
});

test('failed target load preserves the current document and selection until retry succeeds', async () => {
  const page = setup(['download', 'image', 'download']);
  await page.settle();
  const switching = page.click(page.files[1]);
  const image = page.resources.find(node => node.tagName === 'IMG');
  image.onerror();
  await switching;
  assert.equal(page.files[0].getAttribute('aria-current'), 'page');
  assert.deepEqual(page.articles.map(article => article.hidden), [false, true, true]);
  const feedback = page.stream.parentElement.children[0];
  const retrying = feedback.children[0].click();
  page.resources.filter(node => node.tagName === 'IMG').at(-1).onload();
  await retrying;
  assert.equal(page.files[1].getAttribute('aria-current'), 'page');
  assert.deepEqual(page.articles.map(article => article.hidden), [true, false, true]);
});

test('abandoned loads are cancelled and late results cannot replace a newer selection', async () => {
  const page = setup(['download', 'image', 'image']);
  await page.settle();
  const earlier = page.click(page.files[1]);
  const earlierOnLoad = page.resources.find(node => node.tagName === 'IMG').onload;
  const later = page.click(page.files[2]);
  const images = page.resources.filter(node => node.tagName === 'IMG');
  images[1].onload();
  await later;
  assert.equal(images[0].onload, null);
  earlierOnLoad(); // A previously queued load event still cannot change the selection.
  await earlier;
  assert.equal(page.files[2].getAttribute('aria-current'), 'page');
  assert.deepEqual(page.articles.map(article => article.hidden), [true, true, false]);
});

test('repeated clicks on the same loading document retain its successful result', async () => {
  const page = setup(['download', 'image', 'download']);
  await page.settle();
  const first = page.click(page.files[1]);
  const repeated = page.click(page.files[1]);
  page.resources.find(node => node.tagName === 'IMG').onload();
  await Promise.all([first, repeated]);
  assert.equal(page.files[1].getAttribute('aria-current'), 'page');
  assert.equal(page.articles[1].classList.contains('review-loaded'), true);
});

test('directory announces readiness before the first PDF finishes loading', () => {
  const page = setup(['pdf']);
  assert.equal(page.readyMessages[0].type, 'oa-review-ready');
  assert.equal(page.articles[0].classList.contains('review-loaded'), false);
});
