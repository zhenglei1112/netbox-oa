const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {runInNewContext} = require('node:vm');
const {test} = require('node:test');
const source = readFileSync('netbox_oa/static/netbox_oa/maintenance-billing.js', 'utf8');

function element(tag = 'div') {
  return {
    tag, dataset: {}, children: [], handlers: {}, value: '',
    append(...items) {for (const item of items) {item.parent = this; this.children.push(item);}},
    replaceChildren(...items) {this.children = []; this.append(...items);},
    remove() {this.parent.children.splice(this.parent.children.indexOf(this), 1);},
    addEventListener(name, handler) {this.handlers[name] = handler;},
    querySelectorAll(selector) {
      const all = this.children.flatMap(child => [child, ...child.querySelectorAll(selector)]);
      const key = selector === '[data-field]' ? 'field' : 'billingPrice';
      return all.filter(child => Object.hasOwn(child.dataset, key));
    },
    querySelector(selector) {return this.querySelectorAll(selector)[0];},
  };
}
function setup(initialValues = {}, extraRules = []) {
  const container = element();
  const form = element('form');
  form.action = 'https://oa.test/materials/7/billing/';
  container.closest = () => form;
  const payload = element(), status = element(), total = element(), add = element('button');
  form.querySelector = selector => ({
    '[name="lines"]': payload, '[data-billing-status]': status,
    '[data-billing-total]': total, '[data-billing-add]': add,
  })[selector];
  const timers = new Map(), requests = [];
  let timerId = 0;
  const initial = [{rule_key: 'services:B1', started_at: '2026-09-01T08:00:00+08:00',
    ended_at: '2026-09-01T12:00:00+08:00', quantity: '1', deduction: '0', ...initialValues}];
  runInNewContext(source, {
    document: {
      querySelector: () => container,
      createElement: element,
      getElementById: id => ({textContent: JSON.stringify(id === 'oa-billing-initial' ? initial
        : [{rule_key: 'services:B1', code: 'B1', name: '<施工盯防>', method: 'fixed',
          unit_price: '12000'}, ...extraRules])}),
    },
    URL, location: {href: form.action},
    FormData: class {constructor() {this.lines = payload.value;}},
    setTimeout(fn) {timers.set(++timerId, fn); return timerId;},
    clearTimeout(id) {timers.delete(id);},
    fetch(url, options) {
      return new Promise(resolve => requests.push({url, options,
        resolve(data, ok = true) {resolve({ok, json: async () => data});}}));
    },
  });
  return {container, payload, status, total, add, form, requests,
    preview() {const [id, callback] = [...timers].at(-1); timers.delete(id); return callback();}};
}
function response(total) {
  return {total, lines: [{rule: {method: 'fixed'}, unit_price: '12000', gross: '12000.00', deduction: '0.00', net: total}]};
}

test('editor submits rule references and input values, and displays authoritative server totals', async () => {
  const page = setup();
  const running = page.preview();
  const request = page.requests[0];
  const lines = JSON.parse(request.options.body.lines);
  assert.equal(lines[0].rule_key, 'services:B1');
  assert.equal(lines[0].started_at, '2026-09-01T08:00:00');
  assert.equal(lines[0].quantity, '1');
  assert.equal(lines[0].unit_price, undefined);
  assert.equal(new URL(request.url).searchParams.get('preview'), '1');
  request.resolve(response('11999.99'));
  await running;
  assert.equal(page.total.textContent, '11999.99');
  assert.equal(page.status.textContent, '');
  assert.equal(page.status.hidden, true);
  assert.ok(page.container.children[0].querySelector('[data-billing-price]').textContent
    .includes('11999.99'));
  assert.ok(!page.container.children[0].querySelector('[data-billing-price]').textContent
    .includes('核算单价'));
});

test('tiered and duration calculations retain the effective unit price', async () => {
  for (const method of ['tiered', 'duration']) {
    const page = setup();
    const running = page.preview();
    const data = response('12000.00');
    data.lines[0].rule.method = method;
    page.requests[0].resolve(data);
    await running;
    assert.ok(page.container.children[0].querySelector('[data-billing-price]').textContent
      .startsWith('核算单价：12000 元；服务价格：'));
  }
});

test('late previews cannot overwrite calculations after inputs change', async () => {
  const page = setup();
  const first = page.preview();
  const quantity = page.container.children[0].querySelectorAll('[data-field]')
    .find(input => input.dataset.field === 'quantity');
  quantity.value = '2';
  page.container.handlers.input();
  const second = page.preview();
  page.requests[1].resolve(response('24000.00'));
  await second;
  page.requests[0].resolve(response('12000.00'));
  await first;
  assert.equal(page.total.textContent, '24000.00');
});

test('removing and adding items updates the JSON and failed calculations show a reason', async () => {
  const page = setup();
  const remove = page.container.children[0].children[0].children.at(-1);
  remove.handlers.click();
  assert.deepEqual(JSON.parse(page.payload.value), []);
  page.add.handlers.click();
  assert.equal(JSON.parse(page.payload.value).length, 1);
  const running = page.preview();
  page.requests[0].resolve({error: '请选择所属合同中的计费条目。'}, false);
  await running;
  assert.equal(page.status.textContent, '请选择所属合同中的计费条目。');
  assert.equal(page.status.hidden, false);
  assert.equal(page.total.textContent, '—');
});

test('specifications select contract tiers, restore legacy values and reset when rules change', () => {
  const rule = {rule_key: 'services:A4', code: 'A4', name: '光缆修复', method: 'tiered', tiers: [
    {label: '48芯及以下', min: '0', max: '48', min_inclusive: true, max_inclusive: true,
      unit_price: '7000'},
    {label: '48芯以上', min: '48', max: null, min_inclusive: false, max_inclusive: false,
      unit_price: '7500'},
  ]};
  for (const values of [{specification: '49'}, {tier_index: '1'}]) {
    const page = setup({rule_key: rule.rule_key, ...values}, [rule]);
    const fields = page.container.children[0].querySelectorAll('[data-field]');
    const spec = fields.find(field => field.dataset.field === 'tier_index');
    const select = fields.find(field => field.dataset.field === 'rule_key');
    assert.equal(spec.tag, 'select');
    assert.equal(spec.required, true);
    assert.equal(spec.value, '1');
    assert.equal(spec.children[1].textContent, '48芯及以下（7000 元）');
    assert.equal(JSON.parse(page.payload.value)[0].tier_index, '1');
    assert.equal(JSON.parse(page.payload.value)[0].specification, undefined);
    select.value = 'services:B1'; select.handlers.change();
    assert.equal(spec.disabled, true);
    assert.equal(spec.hidden, true);
    assert.equal(spec.parent.children[0].textContent, '单价（元）');
    const price = spec.parent.children.at(-1);
    assert.equal(price.value, '12000');
    assert.equal(price.readOnly, true);
    assert.equal(price.disabled, true);
    assert.equal(price.hidden, false);
    assert.equal(price.dataset.field, undefined);
    const rowFields = spec.parent.parent.children;
    assert.equal(rowFields[3], spec.parent);
    assert.equal(rowFields[4].children[1].dataset.field, 'quantity');
    assert.equal(spec.value, '');
    select.value = rule.rule_key; select.handlers.change();
    assert.equal(spec.disabled, false);
    assert.equal(spec.hidden, false);
    assert.equal(price.hidden, true);
    assert.equal(spec.parent.children[0].textContent, '规格值');
    assert.equal(spec.value, '');
  }
  const page = setup({rule_key: rule.rule_key, specification: 'not-a-number'}, [rule]);
  assert.equal(JSON.parse(page.payload.value)[0].tier_index, '');
});
