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
  $("theme").textContent = dark ? "🌙 Тема" : "☀️ Тема";
  if (LINES.length) drawChart(LINES[0]); // перерисовать под тему
});

// --- периодичность (сегмент-контрол) ---
$("period").querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
  $("period").querySelectorAll("button").forEach(x => x.classList.remove("active"));
  b.classList.add("active");
}));

async function checkHealth() {
  try {
    const d = await (await fetch(`${API}/health`)).json();
    $("status").textContent = "API: ok · " + (d.llm_configured ? "ИИ подключён" : "ИИ выключен (шаблон)");
  } catch { $("status").textContent = "API недоступен"; }
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
  const supplier = $("supplier").value || "systeme";
  const lead = $("lead").value;
  try {
    const r = await fetch(`${API}/api/plan?supplier=${supplier}&lead_time=${lead}&limit=400`);
    const d = await r.json();
    if (!r.ok) { showError(d.error ? `${d.error.code}: ${d.error.message}` : "Ошибка"); return; }
    render(d);
  } catch { showError("Не удалось связаться с API. Проверьте, что бэкенд запущен."); }
}
function showError(m){ const e=$("error"); e.textContent=m; e.hidden=false; }

function render(d) {
  LINES = d.lines || [];
  // KPI
  $("kpis").innerHTML = `
    <div class="kpi"><div class="v">${d.orders_count}</div><div class="l">Артикулов к заказу</div></div>
    <div class="kpi danger"><div class="v">⚠ ${d.deficit_count}</div><div class="l">Риск дефицита</div></div>
    <div class="kpi"><div class="v">${d.excess_count}</div><div class="l">Избыточный запас</div></div>
    <div class="kpi"><div class="v">${money(d.total_cost)} ₸</div><div class="l">Сумма заказа</div></div>
    <div class="kpi"><div class="v">${d.spike_items}</div><div class="l">Исключено разовых заказов</div></div>`;

  // График по топ-позиции
  $("chartCard").hidden = false;
  if (LINES.length) drawChart(LINES[0]);

  // Таблица
  $("tableCard").hidden = false;
  drawTable();
  updateApprove();
}

// --- SVG график: факт (сплошная) + прогноз (пунктир) + зона stockout ---
function drawChart(line) {
  const svg = $("chart");
  const W = 800, H = 220, pad = 30;
  const data = (line.monthly || []).map(Number);
  $("chartTitle").textContent = "Спрос и прогноз — " + (line.name || line.code);
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
      ${supplier} · ${items.length} позиций · ${money(items.reduce((a,l)=>a+l.order_cost,0))} ₸</td>`;
    gr.addEventListener("click", () => { isCol?collapsed.delete(supplier):collapsed.add(supplier); drawTable(); });
    tbody.appendChild(gr);
    if (isCol) return;

    items.forEach((l) => {
      const idx = LINES.indexOf(l);
      const tr = document.createElement("tr"); tr.className = "row-main";
      const badge = l.urgency==="Высокая"?"high":l.urgency==="Средняя"?"mid":"low";
      tr.innerHTML = `
        <td><input type="checkbox" class="chk" data-i="${idx}"></td>
        <td class="mono">${l.code}</td>
        <td>${l.name}</td>
        <td class="qty">${Math.round(l.recommended_qty)}</td>
        <td class="tag">${l.reason_tag}</td>
        <td><span class="badge ${badge}">${l.urgency}</span></td>`;
      tbody.appendChild(tr);

      const dr = document.createElement("tr"); dr.className = "row-detail"; dr.hidden = true;
      const d = l.detail || {};
      dr.innerHTML = `<td></td><td colspan="5"><div class="detail-grid">
        <div>Базовый спрос: <b>${Math.round(d.base_demand_month)} шт/мес</b></div>
        <div>С учётом сезона/роста: <b>${Math.round(d.seasonal_demand_month)} шт/мес</b></div>
        <div>Свободный остаток: <b>${Math.round(d.free_stock)}</b></div>
        <div>Товар в пути: <b>${Math.round(d.in_transit)}</b></div>
        <div>Целевой запас: <b>${Math.round(d.target_stock)}</b></div>
        <div>Покрытие остатком: <b>${d.coverage_months} мес</b></div>
        <div>Исключено выбросов: <b>${Math.round(d.spike_removed_units)} шт (${d.spike_count})</b></div>
        <div>Компенсация дефицита: <b>+${Math.round(d.stockout_adj_units)}</b></div>
        <div>MOQ: <b>${d.moq}</b></div>
      </div><div class="tag" style="margin-top:6px">${l.reason}</div></td>`;
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
  btn.textContent = `Утвердить выбранные (${n})`;
  btn.disabled = n === 0;
}
$("approve").addEventListener("click", () => {
  const n = $("table").querySelectorAll(".chk:checked").length;
  alert(`Утверждено позиций: ${n}. Заказ НЕ отправлен поставщику автоматически — требуется ручная отправка.`);
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
