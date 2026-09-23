// Дашборд рекомендованных заказов. API относительный (Docker) или :8020 (локально).
const API = (window.PLANNER_API !== undefined ? window.PLANNER_API
  : (location.port === "3100" && location.hostname === "localhost" ? "http://localhost:8020" : "")
).replace(/\/+$/, "");
const $ = (id) => document.getElementById(id);
const money = (n) => new Intl.NumberFormat("ru-RU").format(Math.round(n || 0));
let LINES = [];

// --- тема ---
$("theme").addEventListener("click", () => {
  const el = document.documentElement;
  const dark = el.getAttribute("data-theme") === "dark";
  el.setAttribute("data-theme", dark ? "light" : "dark");
  $("theme").textContent = dark ? "🌙" : "☀️";
  if (LINES.length) drawChart(LINES[0]);
});

// --- скрыть подсказку-инструкцию ---
$("hintClose").addEventListener("click", () => { $("hintBanner").hidden = true; });

// --- всплывающие подсказки (ⓘ) ---
const tooltip = $("tooltip");
document.addEventListener("mouseover", (e) => {
  const el = e.target.closest(".help");
  if (!el) return;
  tooltip.textContent = el.getAttribute("data-tip") || "";
  tooltip.hidden = false;
  const r = el.getBoundingClientRect();
  tooltip.style.left = Math.min(r.left, window.innerWidth - 280) + "px";
  tooltip.style.top = (r.bottom + 6) + "px";
});
document.addEventListener("mouseout", (e) => {
  if (e.target.closest(".help")) tooltip.hidden = true;
});

async function checkHealth() {
  const s = $("status");
  try {
    await (await fetch(`${API}/health`)).json();
    s.textContent = "Сервер на связи";
    s.className = "status ok";
  } catch {
    s.textContent = "Нет связи с сервером";
    s.className = "status bad";
  }
}

async function loadSuppliers() {
  try {
    const d = await (await fetch(`${API}/api/suppliers`)).json();
    const sel = $("supplier"); sel.innerHTML = "";
    (d.suppliers || []).forEach(s => {
      const o = document.createElement("option"); o.value = s.key; o.textContent = s.name; sel.appendChild(o);
    });
  } catch {}
}

async function calc() {
  $("error").hidden = true;
  $("loading").hidden = false;
  const supplier = $("supplier").value || "systeme";
  const lead = $("lead").value;
  const budget = $("budget").value || 0;
  try {
    const r = await fetch(`${API}/api/plan?supplier=${supplier}&lead_time=${lead}&budget=${budget}&limit=400`);
    const d = await r.json();
    $("loading").hidden = true;
    if (!r.ok) { showError("Не удалось рассчитать. Попробуйте ещё раз или обратитесь к администратору."); return; }
    render(d);
    loadWhatif(supplier);
  } catch {
    $("loading").hidden = true;
    showError("Нет связи с сервером расчёта. Проверьте, что сервис запущен.");
  }
}

async function loadWhatif(supplier) {
  try {
    const d = await (await fetch(`${API}/api/whatif?supplier=${supplier}`)).json();
    $("whatifCard").hidden = false;
    const seg = $("whatif"); seg.innerHTML = "";
    const kpis = $("whatifKpis");
    d.scenarios.forEach((s, i) => {
      const b = document.createElement("button");
      b.textContent = s.title; if (i === 0) b.classList.add("active");
      b.addEventListener("click", () => {
        seg.querySelectorAll("button").forEach(x => x.classList.remove("active"));
        b.classList.add("active");
        kpis.innerHTML = `
          <div class="kpi"><div class="v">${s.orders}</div><div class="l">Позиций к заказу</div></div>
          <div class="kpi danger"><div class="v">⚠ ${s.deficit}</div><div class="l">Срочных</div></div>
          <div class="kpi"><div class="v">${money(s.cost)} ₸</div><div class="l">Сумма закупки</div></div>`;
      });
      seg.appendChild(b);
    });
    seg.querySelector("button")?.click();
  } catch {}
}
function showError(m){ const e=$("error"); e.textContent=m; e.hidden=false; }

function render(d) {
  LINES = d.lines || [];
  // KPI с пояснениями простым языком
  const budgetKpi = d.budget > 0
    ? `<div class="kpi"><div class="v">${d.within_budget} из ${d.orders_count}</div><div class="l">Влезло в бюджет</div>
         <div class="hint-mini">Остальные отложены — не хватило бюджета</div></div>`
    : `<div class="kpi"><div class="v">${d.transfers}</div><div class="l">Можно взять с другого склада</div>
         <div class="hint-mini">Не заказывать — переместить со своего склада</div></div>`;
  $("kpiCard").hidden = false;
  $("kpis").innerHTML = `
    <div class="kpi"><div class="v">${d.orders_count}</div><div class="l">Позиций к заказу</div>
      <div class="hint-mini">Столько товаров стоит пополнить</div></div>
    <div class="kpi danger"><div class="v">⚠ ${d.deficit_count}</div><div class="l">Срочно — риск закончиться</div>
      <div class="hint-mini">Остатка не хватит до следующей поставки</div></div>
    <div class="kpi"><div class="v">${d.excess_count}</div><div class="l">Затоварено</div>
      <div class="hint-mini">Запас избыточен — заказывать не нужно</div></div>
    <div class="kpi"><div class="v">${money(d.total_cost)} ₸</div><div class="l">Сумма закупки</div>
      <div class="hint-mini">Ориентировочная стоимость заказа</div></div>
    <div class="kpi"><div class="v">${d.spike_items}</div><div class="l">Отсеяно разовых продаж</div>
      <div class="hint-mini">Крупные разовые сделки не завышают заказ</div></div>
    ${budgetKpi}`;

  $("chartCard").hidden = false;
  if (LINES.length) drawChart(LINES[0]);

  $("tableCard").hidden = false;
  drawTable();
  updateApprove();
}

// --- SVG график: факт (сплошная) + прогноз (пунктир) + зона stockout ---
function drawChart(line) {
  const svg = $("chart");
  const W = 800, H = 220, pad = 30;
  const data = (line.monthly || []).map(Number);
  $("chartTitle").textContent = line.name || line.code;
  if (!data.length) { svg.innerHTML = ""; $("chartHint").textContent = ""; return; }

  const seas = line.detail?.seasonal_demand_month || (data.reduce((a,b)=>a+b,0)/data.length);
  const forecast = [seas, seas, seas]; // прогноз на 3 периода вперёд
  const all = data.concat(forecast);
  const max = Math.max(...all, 1);
  const n = all.length;
  const x = (i) => pad + (i * (W - 2*pad) / (n - 1));
  const y = (v) => H - pad - (v * (H - 2*pad) / max);

  const css = getComputedStyle(document.documentElement);
  const cFact = css.getPropertyValue("--chart-fact").trim();
  const cFore = css.getPropertyValue("--chart-forecast").trim();
  const cSO = css.getPropertyValue("--stockout").trim();

  // зоны stockout (месяцы с 0 продаж внутри активного периода)
  let firstActive = data.findIndex(v => v > 0); if (firstActive < 0) firstActive = 0;
  let soRects = "";
  for (let i = firstActive; i < data.length; i++) {
    if (data[i] === 0) {
      soRects += `<rect x="${x(i)-6}" y="${pad}" width="12" height="${H-2*pad}" fill="${cSO}"/>`;
    }
  }
  const factPath = data.map((v,i)=>`${i?"L":"M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const foreStart = data.length - 1;
  const forePath = [data[data.length-1], ...forecast]
    .map((v,i)=>`${i?"L":"M"}${x(foreStart+i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");

  svg.innerHTML = `${soRects}
    <path d="${factPath}" fill="none" stroke="${cFact}" stroke-width="2"/>
    <path d="${forePath}" fill="none" stroke="${cFore}" stroke-width="2" stroke-dasharray="5,4"/>`;
  const soCount = data.slice(firstActive).filter(v=>v===0).length;
  $("chartHint").textContent = soCount
    ? `Затенённые зоны — ${soCount} мес. дефицита: спрос был занижен из-за отсутствия товара.`
    : "Дефицита в истории не зафиксировано.";
}

// --- таблица по группам поставщиков ---
const collapsed = new Set();
function drawTable() {
  const q = ($("search").value || "").toLowerCase();
  const urgentOnly = $("urgentOnly").checked;
  const rows = LINES
    .filter(l => !urgentOnly || l.urgency === "Высокая")
    .filter(l => !q || (l.name + l.code).toLowerCase().includes(q));

  // группировка по поставщику
  const groups = {};
  rows.forEach(l => (groups[l.supplier] = groups[l.supplier] || []).push(l));

  const tbody = $("table").querySelector("tbody");
  tbody.innerHTML = "";
  Object.entries(groups).forEach(([supplier, items]) => {
    const gid = "g_" + supplier.replace(/\W/g, "");
    const isCol = collapsed.has(supplier);
    const gr = document.createElement("tr"); gr.className = "group-row";
    gr.innerHTML = `<td colspan="6"><span class="caret">${isCol?"▶":"▼"}</span>
      Поставщик: ${supplier} · ${items.length} поз. · на ${money(items.reduce((a,l)=>a+l.order_cost,0))} ₸</td>`;
    gr.addEventListener("click", () => { isCol?collapsed.delete(supplier):collapsed.add(supplier); drawTable(); });
    tbody.appendChild(gr);
    if (isCol) return;

    items.forEach((l) => {
      const idx = LINES.indexOf(l);
      const tr = document.createElement("tr"); tr.className = "row-main";
      if (l.in_budget === false) tr.classList.add("approved"); // приглушить вне бюджета
      const badge = l.urgency==="Высокая"?"high":l.urgency==="Средняя"?"mid":"low";
      const transfer = l.transfer_from
        ? ` <span class="badge low">↺ со склада «${l.transfer_from}» ${Math.round(l.transfer_qty)}</span>` : "";
      const qtyCell = l.recommended_qty > 0
        ? `<td class="qty">${Math.round(l.recommended_qty)}</td>`
        : `<td class="qty" style="color:var(--muted)">0 (перемещение)</td>`;
      tr.innerHTML = `
        <td><input type="checkbox" class="chk" data-i="${idx}"></td>
        <td class="mono">${l.code}</td>
        <td>${l.name}</td>
        ${qtyCell}
        <td class="tag">${l.reason_tag}${transfer}</td>
        <td><span class="badge ${badge}">${l.urgency}</span></td>`;
      tbody.appendChild(tr);

      const dr = document.createElement("tr"); dr.className = "row-detail"; dr.hidden = true;
      const d = l.detail || {};
      dr.innerHTML = `<td></td><td colspan="5"><div class="detail-grid">
        <div>Продаётся в среднем: <b>${Math.round(d.base_demand_month)} шт/мес</b></div>
        <div>Прогноз с учётом сезона: <b>${Math.round(d.seasonal_demand_month)} шт/мес</b></div>
        <div>Сейчас на складе: <b>${Math.round(d.free_stock)} шт</b></div>
        <div>Уже едет (в пути): <b>${Math.round(d.in_transit)} шт</b></div>
        <div>Нужный запас: <b>${Math.round(d.target_stock)} шт</b></div>
        <div>Хватит на: <b>${d.coverage_months} мес</b></div>
        <div>Отсеяно разовых продаж: <b>${Math.round(d.spike_removed_units)} шт</b></div>
        <div>Добавлено за дефицит: <b>+${Math.round(d.stockout_adj_units)} шт</b></div>
        <div>Мин. партия заказа: <b>${d.moq}</b></div>
      </div><div class="tag" style="margin-top:6px">📝 ${l.reason}</div></td>`;
      tbody.appendChild(dr);

      // клик по строке — детали; клик по чекбоксу — не разворачивать график
      tr.addEventListener("click", (e) => {
        if (e.target.classList.contains("chk")) return;
        dr.hidden = !dr.hidden;
        drawChart(l); // показать график по выбранной позиции
      });
    });
  });

  tbody.querySelectorAll(".chk").forEach(c => c.addEventListener("change", updateApprove));
}

function updateApprove() {
  const n = $("table").querySelectorAll(".chk:checked").length;
  const btn = $("approve");
  btn.textContent = `Сформировать заказ (${n})`;
  btn.disabled = n === 0;
}
$("approve").addEventListener("click", () => {
  const n = $("table").querySelectorAll(".chk:checked").length;
  alert(`Заказ сформирован по ${n} позиц. \n\nВажно: заказ НЕ отправлен поставщику автоматически. ` +
        `Проверьте список и отправьте вручную через вашу систему.`);
});

function exportCsv() {
  if (!LINES.length) return;
  const head = ["Артикул","Поставщик","Количество","Обоснование","Срочность"];
  const rows = LINES.map(l => [l.code, `"${l.supplier}"`, Math.round(l.recommended_qty),
    `"${l.reason.replace(/"/g,'""')}"`, l.urgency].join(","));
  const blob = new Blob(["\uFEFF"+[head.join(","),...rows].join("\n")], {type:"text/csv;charset=utf-8"});
  const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = "orders.csv"; a.click();
}

$("calc").addEventListener("click", calc);
$("export").addEventListener("click", exportCsv);
$("search").addEventListener("input", drawTable);
$("urgentOnly").addEventListener("change", drawTable);

checkHealth();
loadSuppliers();
