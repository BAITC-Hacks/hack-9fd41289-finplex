const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function page() {
  const fields = {};
  const element = () => ({value: '', textContent: '', children: [], scrolls: 0, scrollIntoView() {this.scrolls++;}, setAttribute() {}, addEventListener() {}, replaceChildren(...items) {this.children=items;}, append(...items) {this.children.push(...items);}});
  const context = vm.createContext({
    window: {}, location: {port: '3100', hostname: 'localhost'}, AbortSignal, AbortController,
    document: {getElementById: id => fields[id] ||= element(), createElement: element, createElementNS: element,
      documentElement: {dataset: {}}},
    // The normal page boot receives an empty supplier list; no network is used.
    fetch: async () => ({ok: true, json: async () => ({suppliers: []})}),
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../app.js'), 'utf8'), context);
  fields.supplier.value = 'systeme';
  fields.safety.value = '15'; fields.budget.value = '0';
  return {fields, context, params: () => vm.runInContext('params()', context)};
}

test('both export formats follow approval and are disabled when draft is invalidated', () => {
  const p = page();
  vm.runInContext("showDraft({id:'x', status:'draft', lines:[], total_cost:0})", p.context);
  assert.equal(p.fields.exchange.disabled, true);
  assert.equal(p.fields.export.disabled, true);
  vm.runInContext("showDraft({id:'x', status:'approved', lines:[], total_cost:0})", p.context);
  assert.equal(p.fields.exchange.disabled, false);
  assert.equal(p.fields.export.disabled, false);
  vm.runInContext('invalidateDraft()', p.context);
  assert.equal(p.fields.exchange.disabled, true);
});

test('blank means source terms, a number explicitly overrides, clearing restores', () => {
  const p = page();
  assert.equal(p.params().lead_time, null);
  p.fields.lead.value = '6';
  assert.equal(p.params().lead_time, 6 / 30.4375);
  assert.equal(p.params().safety, 15 / 30.4375);
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
  assert.match(p.fields.lead.title, /61 дней/);
});

test('refresh shows an empty result and always unlocks after a failed request', async () => {
  const p=page();
  vm.runInContext('json = async () => ({orders: []})',p.context);
  await vm.runInContext('loadOrders()',p.context);
  assert.match(p.fields.orders.children[0].textContent,/Заказов пока нет/);
  assert.match(p.fields.ordersStatus.textContent,/Показано заказов: 0/);
  assert.equal(p.fields.refreshOrders.disabled,false);
  vm.runInContext('json = async () => {throw new Error("Нет связи")}',p.context);
  await vm.runInContext('loadOrders()',p.context);
  assert.match(p.fields.ordersStatus.textContent,/Не удалось обновить/);
  assert.equal(p.fields.refreshOrders.disabled,false);
  assert.match(p.fields.orders.children[0].textContent,/Заказов пока нет/);
});

test('an older refresh cannot overwrite newer results or unlock its button', async () => {
  const p=page();
  vm.runInContext('var resolvers=[]; json = () => new Promise(resolve => resolvers.push(resolve)); var first=loadOrders(); var second=loadOrders();',p.context);
  assert.equal(p.fields.refreshOrders.disabled,true);
  await vm.runInContext('resolvers[0]({orders:[]}); first',p.context);
  assert.equal(p.fields.refreshOrders.disabled,true);
  await vm.runInContext('resolvers[1]({orders:[]}); second',p.context);
  assert.equal(p.fields.refreshOrders.disabled,false);
});

test('changing a filter cancels a calculation and enables immediate recalculation', async () => {
  const p=page();
  vm.runInContext('json = (path, body, signal) => new Promise((resolve,reject) => signal.addEventListener("abort",()=>reject(new Error("aborted")))); var old=calculate();',p.context);
  assert.equal(p.fields.calc.disabled,true);
  vm.runInContext('invalidatePlan()',p.context);
  assert.equal(p.fields.calc.disabled,false);
  assert.equal(p.fields.loading.hidden,true);
  vm.runInContext('var newer=calculate()',p.context);
  await vm.runInContext('old',p.context);
  assert.equal(p.fields.calc.disabled,true); // Old completion cannot unlock a newer request.
  vm.runInContext('invalidatePlan()',p.context);
  await vm.runInContext('newer',p.context);
  assert.equal(p.fields.calc.disabled,false);
});

test('opening a chart reveals and scrolls to the selected product', () => {
  const p=page();
  vm.runInContext('drawChart({name:"Test product", monthly:[10,20], cleaned_monthly:[10,18], periods:["2025-01","2025-02"], forecast:[22], forecast_periods:["2025-03"], confirmed_stockout:[]})',p.context);
  assert.equal(p.fields.chartCard.hidden,false);
  assert.equal(p.fields.chartCard.open,true);
  assert.equal(p.fields.chartCard.scrolls,1);
  assert.equal(p.fields.chartTitle.textContent,'Test product');
  assert.equal(p.fields.chart.children.length,5);
});

test('missing requisites block saving until the manager supplies both fields', () => {
  const p=page();
  vm.runInContext('lines=[{code:"A",min_qty:1,moq:1}]; plan={parameters:{budget:0}}; selected.add("A"); edits.set("A",{quantity:2,unit_cost:10,article:"",unit:""}); selectionInfo()',p.context);
  assert.equal(p.fields.saveDraft.disabled,true);
  assert.match(p.fields.selection.textContent,/артикул и единицу/);
  vm.runInContext('edits.get("A").article="SUP-A"; edits.get("A").unit="шт"; selectionInfo()',p.context);
  assert.equal(p.fields.saveDraft.disabled,false);
  vm.runInContext('edits.get("A").article=" "; selectionInfo()',p.context);
  assert.equal(p.fields.saveDraft.disabled,true);
});

test('recommendation renders fields for missing article and unit', () => {
  const p=page();
  vm.runInContext('document.getElementById("table").querySelector=()=>document.getElementById("tbody"); lines=[{code:"A",name:"Product",article:"",unit:"не указана",min_qty:1,moq:1,in_budget:true,urgency:"Высокая",warnings:[]}]; plan={parameters:{budget:0}}; edits.set("A",{quantity:1,unit_cost:10,article:"",unit:""}); renderTable()',p.context);
  const cells=p.fields.tbody.children[0].children;
  assert.equal(cells[1].children.find(x=>x.type==='text').placeholder,'Артикул поставщика');
  assert.equal(cells[2].children.find(x=>x.type==='text').placeholder,'Единица измерения');
});
