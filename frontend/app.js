/* All imported text is rendered with textContent. Selection survives filtering/pages. */
const API = (window.PLANNER_API || (location.port === '3100' ? `http://${location.hostname}:8020` : '')).replace(/\/+$/, '');
const $ = id => document.getElementById(id);
const fmt = n => n == null ? 'не указана' : new Intl.NumberFormat('ru-RU', {maximumFractionDigits: 3}).format(n);
let plan = null, draft = null, lines = [], page = 0, busy = false, optionsRun = 0, calculationRun = 0, ordersRun = 0, optionsLoading = false, calculationController = null;
const DAYS_PER_MONTH = 30.4375;
const selected = new Set(), edits = new Map();
const PAGE_SIZE = 20;
function el(tag, text, cls) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (cls) e.className = cls; return e; }
function showError(error) { $('error').textContent = error.message || String(error); $('error').hidden = false; $('error').scrollIntoView?.({behavior:'smooth',block:'center'}); }
async function api(path, body, signal) {
  const headers = {'X-API-Key': $('apiKey').value};
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  const timeout = AbortSignal.timeout(path === '/api/orders' && body === undefined ? 15000 : 120000);
  const response = await fetch(API + path, {method: body === undefined ? 'GET' : 'POST', headers, cache:'no-store', signal:signal ? AbortSignal.any([signal, timeout]) : timeout, body: body === undefined ? undefined : JSON.stringify(body)});
  if (!response.ok) {
    if (response.status === 401) { $('connectionHelp').hidden=false; $('connectionHelp').open=true; $('apiCredentials').hidden=false; }
    const data = await response.json().catch(() => ({}));
    throw new Error(typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail || `Ошибка ${response.status}`));
  }
  return response;
}
async function json(path, body, signal) { return (await api(path, body, signal)).json(); }
async function guarded(fn) { $('error').hidden = true; try { await fn(); } catch (error) { showError(error); } }
function params() { return {supplier: $('supplier').value, lead_time: $('lead').value.trim() === '' ? null : Number($('lead').value) / DAYS_PER_MONTH, safety: Number($('safety').value) / DAYS_PER_MONTH, budget: Number($('budget').value), category: $('category').value || null, warehouse: $('warehouse').value || null}; }
function fillOptions(id, values, title) { const options = values.map(v => {const o = el('option', v); o.value = v; return o;}); const first = el('option', title); first.value = ''; $(id).replaceChildren(first, ...options); }
async function loadOptions() {
  const run = ++optionsRun, supplier = $('supplier').value;
  optionsLoading = true; $('calc').disabled = true;
  try {
    const result = await json(`/api/options?supplier=${encodeURIComponent(supplier)}`);
    if (run !== optionsRun) return;
    fillOptions('category', result.categories, 'Все категории'); fillOptions('warehouse', result.warehouses, 'Все доступные');
    $('lead').value = '';
    $('lead').title = `Из данных товара; если срок не указан — срок поставщика около ${Math.round(result.lead_time * DAYS_PER_MONTH)} дней. Введите дни для общего переопределения.`;
    renderMetadata(result.metadata);
  } finally { if (run === optionsRun) { optionsLoading = false; $('calc').disabled = busy; } }
}
function renderMetadata(meta) {
  $('dataCard').hidden = false; $('dataInfo').textContent = `Данные на ${meta.as_of}. Товаров: ${meta.products}. Файлов: ${meta.sources.length}.`;
  $('dataWarnings').textContent = meta.warnings.length ? `Есть ограничения данных (${meta.warnings.length}) — проверьте перед утверждением` : 'Предупреждений об источниках нет';
  $('warnings').replaceChildren(...meta.warnings.map(w => el('li', w)));
}
async function connect() {
  $('status').textContent = 'Подключаемся…';
  let result;
  try { result = await json('/api/suppliers'); }
  catch (error) { $('status').textContent = 'Не удалось подключиться'; $('status').className = 'status bad'; $('connectionHelp').hidden=false; throw error; }
  $('supplier').replaceChildren(...result.suppliers.map(s => { const o = el('option', s.name); o.value = s.key; return o; }));
  $('status').textContent = 'Приложение работает'; $('status').className = 'status ok';
  $('connectionHelp').hidden = true;
  if (result.suppliers.length) await loadOptions();
  else throw new Error('Нет полных наборов данных');
  await loadOrders();
}
function invalidateDraft() { draft = null; $('draftCard').hidden = true; $('export').disabled = true; }
function invalidatePlan() {
  calculationRun++; plan = null; lines = []; selected.clear(); edits.clear(); invalidateDraft();
  calculationController?.abort(); calculationController = null; busy = false;
  $('calc').disabled = optionsLoading; $('loading').hidden = true;
  for (const id of ['tableCard','kpiCard','chartCard']) $(id).hidden = true;
}
async function calculate() {
  if (busy) return;
  const run = ++calculationRun;
  calculationController = new AbortController();
  busy = true; $('calc').disabled = true; $('loading').hidden = false;
  try {
    const result = await json('/api/plans', params(), calculationController.signal);
    if (run !== calculationRun) return;
    plan = result; lines = result.lines; page = 0; selected.clear(); edits.clear(); invalidateDraft();
    for (const l of lines) edits.set(l.code, {quantity: l.recommended_qty, unit_cost: l.cost});
    renderMetadata(result.metadata);
    $('kpiCard').hidden = false; $('tableCard').hidden = false;
    $('kpis').replaceChildren(...[[result.orders_count, 'Позиций'], [result.deficit_count, 'Рисков дефицита (включая уже заказанные)'], [fmt(result.total_cost), 'Известная стоимость, ₸'], [result.unpriced_count, 'Без цены'], [result.within_budget, 'В бюджете']].map(([v,label]) => { const k = el('div', null, 'kpi'); k.append(el('div', v, 'v'), el('div', label, 'l')); return k; }));
    $('summary').textContent = result.ai_summary;
    $('whatifKpis').replaceChildren(); renderTable();
    $('chartCard').hidden = true;
    $('tableCard').scrollIntoView?.({behavior:'smooth',block:'start'});
  } catch (error) { if (run === calculationRun) throw error; }
  finally { if (run === calculationRun) { busy = false; calculationController = null; $('calc').disabled = optionsLoading; $('loading').hidden = true; } }
}
function selectionInfo() {
  let total = 0, missing = 0, invalid = 0;
  for (const code of selected) {
    const e = edits.get(code), l = lines.find(x => x.code === code);
    if (!(e.unit_cost >= 1)) missing++;
    else total += Math.round(e.quantity * e.unit_cost * 100) / 100;
    if (!(e.quantity >= l.min_qty) || Math.abs(e.quantity / l.moq - Math.round(e.quantity / l.moq)) > 1e-7) invalid++;
  }
  const over = plan && plan.parameters.budget > 0 && total > plan.parameters.budget;
  $('selection').textContent = !selected.size ? 'Отметьте товары галочками слева. Затем нажмите «Перейти к проверке».' : `Выбрано ${selected.size} · Сумма с известными ценами: ${fmt(total)} ₸.${missing ? ` Укажите цену не ниже 1 ₸ для ${missing} позиций.` : ''}${invalid ? ` Исправьте количество для ${invalid} позиций: соблюдайте минимум и кратность.` : ''}${over ? ' Превышен бюджет.' : ''}`;
  $('saveDraft').disabled = !selected.size || !!missing || !!invalid || over;
}
function renderTable() {
  const q = $('search').value.toLowerCase();
  const filtered = lines.filter(l => (!$('urgentOnly').checked || l.urgency === 'Высокая') && (!q || `${l.code} ${l.article} ${l.name}`.toLowerCase().includes(q)));
  const pages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE)); page = Math.min(page, pages - 1);
  const tbody = $('table').querySelector('tbody'); tbody.replaceChildren();
  for (const l of filtered.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE)) {
    const row = el('tr'); const choose = el('input'); choose.type = 'checkbox'; choose.checked = selected.has(l.code); choose.setAttribute('aria-label', `Выбрать ${l.code}`);
    choose.addEventListener('change', () => { choose.checked ? selected.add(l.code) : selected.delete(l.code); invalidateDraft(); selectionInfo(); });
    const c1 = el('td'); c1.append(choose);
    const code = el('td', `${l.code} / ${l.article || 'нет артикула'}`, 'mono');
    const name = el('td', `${l.name} (${l.unit})`); const chart = el('button', 'График', 'btn'); chart.addEventListener('click', () => drawChart(l)); name.append(el('br'), chart);
    const qtyCell = el('td'); const qty = el('input'); qty.type = 'number'; qty.min = l.min_qty; qty.step = l.moq; qty.value = edits.get(l.code).quantity; qty.setAttribute('aria-label', `Количество ${l.code}`);
    qty.addEventListener('input', () => { edits.get(l.code).quantity = Number(qty.value); invalidateDraft(); selectionInfo(); });
    qtyCell.append(qty, el('div', `Мин. ${fmt(l.min_qty)}, кратность ${fmt(l.moq)}`, 'tag'));
    const costCell = el('td'); const price = el('input'); price.type = 'number'; price.min = 1; price.step = 1; price.value = edits.get(l.code).unit_cost ?? ''; price.placeholder='От 1 ₸'; price.title='Минимум 1 ₸. Стрелки изменяют цену на 1 ₸.'; price.setAttribute('aria-label', `Цена ${l.code}`);
    price.addEventListener('input', () => { edits.get(l.code).unit_cost = price.value ? Number(price.value) : null; invalidateDraft(); selectionInfo(); }); costCell.append(price);
    const detail = el('td'); detail.append(el('span', l.urgency, 'badge ' + (l.urgency === 'Высокая' ? 'high' : 'mid')));
    if (!l.in_budget) detail.append(el('p', 'Не вошло в исходный бюджет / нет цены', 'tag'));
    const exp = el('details'); exp.append(el('summary', 'Расчёт и предупреждения'), el('p', l.reason), el('p', `Остаток на ${l.stock_as_of}. Наличный: ${fmt(l.free_stock)}; путь: ${fmt(l.in_transit)}; зачтено: ${fmt(l.eligible_transit)}.`));
    const warnings = el('ul'); warnings.append(...l.warnings.map(w => el('li', w))); exp.append(warnings); detail.append(exp);
    const cells = [c1, code, name, qtyCell, costCell, detail];
    cells.forEach((cell,i) => cell.setAttribute('data-label', ['В заказ','Код / артикул','Товар','Количество','Цена за единицу, ₸','Обоснование'][i]));
    row.append(...cells); tbody.append(row);
  }
  if (!filtered.length) {const row=el('tr'), cell=el('td',lines.length ? 'По этим условиям товаров нет. Измените поиск или снимите фильтр срочности.' : 'По этим данным пополнение не требуется. Проверьте дату остатков и предупреждения.'); cell.colSpan=6;row.append(cell);tbody.append(row);}
  $('pageInfo').textContent = `Страница ${page + 1}/${pages}. Найдено ${filtered.length} из ${lines.length}. Выбор сохраняется между страницами.`;
  $('prevPage').disabled = page === 0; $('nextPage').disabled = page + 1 === pages; selectionInfo();
}
function drawChart(l) {
  $('chartCard').hidden = false; $('chartCard').open = true; $('chartTitle').textContent = l.name;
  $('chartCard').scrollIntoView?.({behavior:'smooth',block:'start'});
  const svg = $('chart'); svg.replaceChildren();
  const future = l.forecast.filter((v,i) => l.forecast_periods[i] > l.periods[l.periods.length - 1]);
  const data = l.monthly, all = data.concat(future), max = Math.max(...all, 1), n = all.length;
  const x = i => 32 + i * 735 / Math.max(1,n-1), y = v => 202 - v / max * 160;
  function node(tag, attrs) { const e = document.createElementNS('http://www.w3.org/2000/svg',tag); for (const [k,v] of Object.entries(attrs)) e.setAttribute(k,v); svg.append(e); return e; }
  l.periods.forEach((p,i) => { if (l.confirmed_stockout.includes(p)) node('rect',{x:x(i)-5,y:25,width:10,height:177,fill:'#d4a96a',opacity:.4}); });
  node('path',{d:data.map((v,i)=>`${i?'L':'M'}${x(i)},${y(v)}`).join(' '),fill:'none',stroke:'#3977db','stroke-width':2});
  const clean = l.cleaned_monthly; node('path',{d:clean.map((v,i)=>`${i?'L':'M'}${x(i)},${y(v)}`).join(' '),fill:'none',stroke:'#708074','stroke-width':1});
  const f = [data[data.length-1],...future]; node('path',{d:f.map((v,i)=>`${i?'L':'M'}${x(data.length-1+i)},${y(v)}`).join(' '),fill:'none',stroke:'#c47333','stroke-width':2,'stroke-dasharray':'5 4'});
  node('text',{x:32,y:226,fill:'currentColor','font-size':12}).textContent = l.periods[0];
  node('text',{x:640,y:226,fill:'currentColor','font-size':12}).textContent = l.forecast_periods.at(-1);
  $('chartHint').textContent = 'Синий — факт, серый — очищенный/восстановленный спрос, пунктир — календарный прогноз. Выделены только подтверждённые stockout. Неполный последний месяц не используется для обучения.';
}
function showDraft(value) {
  draft = value; $('draftCard').hidden = false; $('verified').checked = false;
  $('draftTitle').textContent = value.status === 'approved' ? 'Заказ утверждён — скачайте файл' : '3. Проверьте и утвердите заказ';
  $('draftInfo').textContent = `Заказ ${value.id}, ${value.status === 'approved' ? 'утверждён' : 'черновик'}. Позиций: ${value.lines.length}, сумма ${fmt(value.total_cost)} ₸. ${value.note || ''}`;
  const table = el('table'), head = el('tr'); for (const label of ['Код / артикул','Товар','Количество','Цена','Сумма']) head.append(el('th',label)); table.append(head);
  for (const l of value.lines) {const r = el('tr'); for (const v of [`${l.code} / ${l.article}`,l.name,`${fmt(l.quantity)} ${l.unit}`,fmt(l.cost),fmt(l.order_cost)]) r.append(el('td',v)); table.append(r);}
  $('draftLines').replaceChildren(table); $('confirmation').hidden = value.status === 'approved'; $('export').disabled = value.status !== 'approved';
  $('draftCard').scrollIntoView?.({behavior:'smooth',block:'start'});
}
async function saveDraft() {
  const body = {plan_id:plan.plan_id, lines:[...selected].map(code => ({code,...edits.get(code)})),note:$('note').value};
  showDraft(await json('/api/orders',body)); await loadOrders();
}
async function approve() {
  if (!draft) throw new Error('Сначала сохраните черновик');
  showDraft(await json(`/api/orders/${draft.id}/approve`,{reviewer:$('reviewer').value,verified_inputs:$('verified').checked})); await loadOrders();
}
async function download() {
  if (!draft || draft.status !== 'approved') return;
  const blob = await (await api(`/api/orders/${draft.id}/export`)).blob(); const url = URL.createObjectURL(blob);
  const a = el('a'); a.href=url; a.download=`order-${draft.id}.csv`; a.click(); setTimeout(()=>URL.revokeObjectURL(url),1000);
}
async function loadOrders() {
  const run = ++ordersRun, button = $('refreshOrders'), message = $('ordersStatus');
  button.disabled = true; button.textContent = 'Обновляем…'; message.className='helper'; message.textContent='Загружаем сохранённые заказы…';
  try {
    const result = await json('/api/orders');
    if (run !== ordersRun) return;
    $('orders').replaceChildren(...result.orders.map(o => {
      const date=new Date(o.created_at).toLocaleString('ru-RU');
      const b=el('button',`${o.supplier_key === 'systeme' ? 'Systeme Electric' : o.supplier_key === 'iek' ? 'ИЭК' : o.supplier_key} · ${o.status === 'approved' ? 'Утверждён' : 'Черновик'} · ${fmt(o.total_cost)} ₸ · ${date} · Открыть`, 'btn order-item');
      b.addEventListener('click',()=>guarded(async()=>showDraft(await json(`/api/orders/${o.id}`)))); return b;
    }));
    if (!result.orders.length) $('orders').append(el('p','Заказов пока нет. Рассчитайте потребность, выберите товары и нажмите «Перейти к проверке» — появится сохранённый черновик.','empty-state'));
    message.textContent=`Обновлено в ${new Date().toLocaleTimeString('ru-RU')}. Показано заказов: ${result.orders.length}.`;
  } catch (error) {
    if (run !== ordersRun) return;
    message.className='err'; message.textContent=`Не удалось обновить список. ${error.message || error} Ранее загруженный список не обновлён. Проверьте подключение и повторите.`;
  } finally {
    if (run === ordersRun) { button.disabled=false; button.textContent='Обновить список'; }
  }
}
async function whatif() {
  if (!plan) return; $('whatifButton').disabled = true;
  const snapshot = plan.plan_id;
  try {const result=await json('/api/whatif',plan.parameters); if(plan?.plan_id !== snapshot) return; $('whatifKpis').replaceChildren(...result.scenarios.map(s=>el('p',`${s.title}: ${s.orders} поз., известная сумма ${fmt(s.cost)} ₸, без цены ${s.unpriced_count}, рисков ${s.deficit}, в бюджете ${s.within_budget}.`)));}
  finally {$('whatifButton').disabled=false;}
}
$('connect').addEventListener('click',()=>guarded(connect));
$('supplier').addEventListener('change',()=>{invalidatePlan();guarded(loadOptions);});
for (const id of ['category','warehouse','lead','safety','budget']) $(id).addEventListener('change',invalidatePlan);
$('calc').addEventListener('click',()=>guarded(calculate));
$('saveDraft').addEventListener('click',()=>guarded(saveDraft));
$('approve').addEventListener('click',()=>guarded(approve));
$('export').addEventListener('click',()=>guarded(download));
$('refreshOrders').addEventListener('click',()=>guarded(loadOrders));
$('whatifButton').addEventListener('click',()=>guarded(whatif));
for (const id of ['search','urgentOnly']) $(id).addEventListener('input',()=>{page=0;renderTable();});
$('prevPage').addEventListener('click',()=>{page--;renderTable();}); $('nextPage').addEventListener('click',()=>{page++;renderTable();});
$('selectBudget').addEventListener('click',()=>{for(const l of lines) if(l.in_budget && edits.get(l.code).unit_cost >= 1) selected.add(l.code); invalidateDraft();renderTable();});
$('clearSelection').addEventListener('click',()=>{selected.clear();invalidateDraft();renderTable();});
$('theme').addEventListener('click',()=>document.documentElement.dataset.theme = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
guarded(connect);
