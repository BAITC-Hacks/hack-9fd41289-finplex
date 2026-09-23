const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page() {
  const fields = {};
  const element = () => ({value: '', textContent: '', addEventListener() {}, replaceChildren() {}});
  const context = vm.createContext({
    window: {}, location: {port: '3100', hostname: 'localhost'},
    document: {getElementById: id => fields[id] ||= element(), createElement: element,
      documentElement: {dataset: {}}},
    // The normal page boot receives an empty supplier list; no network is used.
    fetch: async () => ({ok: true, json: async () => ({suppliers: []})}),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8'), context);
  fields.supplier.value = 'systeme';
  fields.safety.value = '0.5'; fields.budget.value = '0';
  return {fields, context, params: () => vm.runInContext('params()', context)};
}

test('blank means source terms, a number explicitly overrides, clearing restores', () => {
  const p = page();
  assert.equal(p.params().lead_time, null);
  p.fields.lead.value = '6';
  assert.equal(p.params().lead_time, 6);
  p.fields.lead.value = '';
  assert.equal(p.params().lead_time, null);
  p.fields.lead.value = '0';
  assert.equal(p.params().lead_time, 0); // Invalid input must reach server validation, not silently become automatic.
});

test('switching supplier returns to source terms without copying its fallback into override', async () => {
  const p = page();
  p.fields.lead.value = '6';
  vm.runInContext("json = async () => ({categories: [], warehouses: [], lead_time: 2, metadata: {as_of: '', version: '', products: 0, sources: [], warnings: []}})", p.context);
  await vm.runInContext('loadOptions()', p.context);
  assert.equal(p.params().lead_time, null);
  assert.match(p.fields.lead.title, /2 мес/);
});
