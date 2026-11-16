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
function setup(initialValues = {}, extraRules = [], routineAllowed = false) {
  const container = element();
  container.dataset.routineMaintenanceAllowed = routineAllowed ? '1' : '0';
  const form = element('form');
  form.action = 'https://oa.test/materials/7/billing/';
  container.closest = () => form;
  const payload = element(), status = element(), total = element(), add = element('button');
  const refresh = element('button'), save = element('button');
  save.disabled = false;
  form.querySelector = selector => ({
    '[name="lines"]': payload, '[data-billing-status]': status,
    '[data-billing-total]': total, '[data-billing-add]': add,
    '[data-billing-refresh]': refresh, '[data-billing-save]': save,
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
  return {container, payload, status, total, add, form, requests, refresh, save,
    preview() {const [id, callback] = [...timers].at(-1); timers.delete(id); return callback();}};
}
function response(total) {
  return {total, lines: [{rule: {method: 'fixed'}, unit_price: '12000', gross: '12000.00', deduction: '0.00', net: total}]};
}

test('routine checkbox is limited to eligible contracts and serializes boolean selections', async () => {
  const regular = setup();
  assert.ok(!regular.container.children[0].querySelectorAll('[data-field]')
    .some(field => field.dataset.field === 'routine_maintenance'));
  const page = setup({routine_maintenance: true}, [], true);
  const checkbox = page.container.children[0].querySelectorAll('[data-field]')
    .find(field => field.dataset.field === 'routine_maintenance');
  assert.equal(checkbox.type, 'checkbox');
  assert.equal(checkbox.checked, true);
  assert.equal(checkbox.parent.children[1].textContent, '日常代维');
  assert.equal(JSON.parse(page.payload.value)[0].routine_maintenance, true);
  const running = page.preview();
  page.requests[0].resolve(response('0.00'));
  await running;
  assert.equal(page.total.textContent, '0.00');
  checkbox.checked = false;
  page.container.handlers.change();
  assert.equal(JSON.parse(page.payload.value)[0].routine_maintenance, false);
  page.add.handlers.click();
  const added = page.container.children[1].querySelectorAll('[data-field]')
    .find(field => field.dataset.field === 'routine_maintenance');
  assert.equal(added.checked, false);
  added.checked = true;
  page.form.handlers.submit();
  assert.equal(JSON.parse(page.payload.value)[1].routine_maintenance, true);
});

test('editor submits rule references and input values, and displays authoritative server totals', async () => {
  const page = setup();
  const running = page.preview();
  const request = page.requests[0];
  const lines = JSON.parse(request.options.body.lines);
  assert.equal(lines[0].rule_key, 'services:B1');
  assert.equal(lines[0].started_at, '2026-09-01');
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
      .startsWith('核算单价：12000 元；小计：'));
  }
});

test('service dates are optional date controls and preserve empty inputs', () => {
  for (const initial of [{}, {started_at: '', ended_at: ''}]) {
    const page = setup(initial);
    const fields = page.container.children[0].querySelectorAll('[data-field]');
    for (const key of ['started_at', 'ended_at']) {
      const field = fields.find(input => input.dataset.field === key);
      assert.equal(field.type, 'date');
      assert.ok(!field.required);
      assert.equal(field.value, initial[key] === '' ? '' : '2026-09-01');
      field.value = '';
    }
    page.form.handlers.submit();
    const saved = JSON.parse(page.payload.value)[0];
    assert.equal(saved.started_at, '');
    assert.equal(saved.ended_at, '');
  }
});

test('material options show name/specification while service labels retain their codes', () => {
  const page = setup({rule_key: 'materials:M1'}, [
    {rule_key: 'materials:M1', code: 'M1', group: 'materials', name: '光缆',
      specification: 'GYTA-48B1.3 48芯光缆', method: 'fixed', unit_price: '100'},
    {rule_key: 'materials:M2', code: 'M2', group: 'materials', name: '光缆',
      specification: 'GYTA-96B1.3 96芯光缆', method: 'fixed', unit_price: '200'},
    {rule_key: 'materials:M3', code: 'M3', group: 'materials', name: '接头盒',
      specification: ' ', method: 'fixed', unit_price: '300'},
  ]);
  const select = page.container.children[0].querySelectorAll('[data-field]')
    .find(field => field.dataset.field === 'rule_key');
  assert.equal(select.children[1].textContent, 'B1 · <施工盯防>');
  assert.equal(select.children[2].textContent, '光缆/GYTA-48B1.3 48芯光缆');
  assert.equal(select.children[3].textContent, '光缆/GYTA-96B1.3 96芯光缆');
  assert.equal(select.children[4].textContent, '接头盒');
  assert.equal(select.value, 'materials:M1');
  assert.equal(JSON.parse(page.payload.value)[0].rule_key, 'materials:M1');
});

test('refresh reloads rules while preserving current entries and discards old previews', async () => {
  const page = setup({routine_maintenance: true, started_at: '', ended_at: ''}, [], true);
  const oldPreview = page.preview();
  const refreshing = page.refresh.handlers.click();
  assert.equal(page.refresh.disabled, true);
  assert.equal(page.save.disabled, true);
  const request = page.requests[1];
  assert.equal(new URL(request.url).searchParams.get('refresh_pricing'), '1');
  assert.equal(request.options.cache, 'no-store');
  const quantity = page.container.children[0].querySelectorAll('[data-field]')
    .find(field => field.dataset.field === 'quantity');
  quantity.value = '3';
  request.resolve({rules: [{rule_key: 'services:B1', code: 'B1', name: '施工盯防',
    method: 'fixed', unit_price: '500'}], routine_maintenance_allowed: true, pricing_error: ''});
  await refreshing;
  const saved = JSON.parse(page.payload.value)[0];
  assert.equal(saved.quantity, '3');
  assert.equal(saved.started_at, '');
  assert.equal(saved.ended_at, '');
  assert.equal(saved.routine_maintenance, true);
  const spec = page.container.children[0].querySelectorAll('[data-field]')
    .find(field => field.dataset.field === 'tier_index');
  assert.equal(spec.parent.children.at(-1).value, '500');
  page.requests[0].resolve(response('999.00'));
  await oldPreview;
  assert.equal(page.total.textContent, '—');
  assert.equal(page.refresh.disabled, false);
  assert.equal(page.save.disabled, false);
});

test('failed refresh preserves inputs and permits retry', async () => {
  const page = setup({quantity: '3'});
  const refreshing = page.refresh.handlers.click();
  page.requests[0].resolve({error: '刷新失败'}, false);
  await refreshing;
  assert.equal(page.status.textContent, '刷新失败');
  assert.equal(JSON.parse(page.payload.value)[0].quantity, '3');
  assert.equal(page.refresh.disabled, false);
  assert.equal(page.save.disabled, false);
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
