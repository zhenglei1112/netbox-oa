const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const {runInNewContext} = require('node:vm');

const source = readFileSync(join(__dirname, '../netbox_oa/static/netbox_oa/maintenance-contract.js'), 'utf8');

function page(region, preserveEmpty = false, fixedNumber = false, initialContract = '1') {
  const listeners = {};
  const fields = {
    'oa-contract-options': {textContent: JSON.stringify({
      '1': {supplier: '服务单位一', service_region_ids: [1], contract_type_snapshot: {code: 'W'}},
      '2': {supplier: '框架合同单位', service_region_ids: [], contract_type_snapshot: {code: 'K'}},
      '3': {supplier: '多省框架单位', service_region_ids: [1, 2], contract_type_snapshot: {code: 'K'}},
    })},
    id_contract_id: {value: initialContract, addEventListener: (name, callback) => {listeners[name] = callback;}},
    id_supplier: {value: ''},
    id_number: {value: fixedNumber ? 'Y-GD2611W03B' : 'Y-GD261103B',
      dataset: {numberBase: 'Y-GD261103B', numberFixed: fixedNumber ? '1' : '0'}},
    id_service_region: {
      dataset: {oaPreserveEmpty: preserveEmpty ? '1' : ''},
      options: ['1', '2', '3'].map(value => ({value, selected: region.includes(value)})),
      get selectedOptions() {return this.options.filter(option => option.selected);},
      dispatchEvent() {},
    },
  };
  runInNewContext(source, {document: {getElementById: id => fields[id]}, Event: class {}});
  return {fields, change: value => {fields.id_contract_id.value = value; listeners.change();}};
}

test('autofills the contract region when the contract has a single region and the order has none', () => {
  const {fields} = page([]);
  assert.deepEqual(fields.id_service_region.selectedOptions.map(option => option.value), ['1']);
  assert.equal(fields.id_supplier.value, '服务单位一');
});

test('does not autofill regions when the contract has multiple regions', () => {
  const {fields, change} = page([], false, false, '3');
  assert.equal(fields.id_service_region.selectedOptions.length, 0);
  assert.equal(fields.id_supplier.value, '多省框架单位');
  change('1');
  assert.deepEqual(fields.id_service_region.selectedOptions.map(option => option.value), ['1']);
  change('3');
  assert.equal(fields.id_service_region.selectedOptions.length, 0);
});

test('preserves a manually entered region when editing or redisplaying a form', () => {
  const {fields} = page(['3']);
  assert.deepEqual(fields.id_service_region.selectedOptions.map(option => option.value), ['3']);
  assert.equal(fields.id_supplier.value, '服务单位一');
});

test('does not silently replace cleared or unresolvable saved regions on redisplay', () => {
  const {fields, change} = page([], true);
  assert.equal(fields.id_service_region.selectedOptions.length, 0);
  change('1');
  assert.deepEqual(fields.id_service_region.selectedOptions.map(option => option.value), ['1']);
});

test('switching contracts refreshes defaults and lets a framework region be entered manually', () => {
  const {fields, change} = page(['3']);
  change('2');
  assert.equal(fields.id_supplier.value, '框架合同单位');
  assert.equal(fields.id_service_region.selectedOptions.length, 0);
  fields.id_service_region.options[2].selected = true;
  assert.deepEqual(fields.id_service_region.selectedOptions.map(option => option.value), ['3']);
  change('1');
  assert.deepEqual(fields.id_service_region.selectedOptions.map(option => option.value), ['1']);
});

test('draft number preview changes only the type character and preserves the reserved month and tail', () => {
  const {fields, change} = page([]);
  assert.equal(fields.id_number.value, 'Y-GD2611W03B');
  change('2');
  assert.equal(fields.id_number.value, 'Y-GD2611K03B');
  change('');
  assert.equal(fields.id_number.value, 'Y-GD261103B');
});

test('submitted number remains fixed when the contract changes during correction', () => {
  const {fields, change} = page([], false, true);
  change('2');
  assert.equal(fields.id_number.value, 'Y-GD2611W03B');
  assert.equal(fields.id_supplier.value, '框架合同单位');
});
