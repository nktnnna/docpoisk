// DocSearch — фронтенд без сборки и зависимостей. Анализ документов — в analysis.js.
const $ = (s) => document.querySelector(s);
const API = { ingest: "/api/ingest", chat: "/api/chat", analysis: "/api/analysis" };

const state = {
  history: [],          // [{role, content}] для контекста диалога
  selected: new Set(),  // doc_id выбранных документов (пусто = все)
  busy: false,
  knownJobs: new Set(),
  docs: [],             // все документы базы знаний
  openDoc: null,        // doc_id, открытый в карточке базы знаний
  canOpen: false,       // сервер умеет открыть файл в программе по умолчанию (настольное приложение)
  graph: null,          // реквизиты и связи документов: {docs, links, types}
  kbMode: "table",      // таблица, цепочки «договор → доп. соглашения, письма» или переписка
  kbLimit: 200,         // сколько строк таблицы показано (дальше — «Показать ещё»)
  tab: "chat",
};

// ---------- тема ----------
const svg = (body) => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
const THEMES = {
  auto: [svg('<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/>'), "Тема: как в системе"],
  light: [svg('<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>'), "Тема: светлая"],
  dark: [svg('<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>'), "Тема: тёмная"],
};
function applyTheme(t) {
  if (t === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  const [icon, title] = THEMES[t];
  $("#themeToggle").innerHTML = icon; $("#themeToggle").title = title + " (нажмите, чтобы сменить)";
  try { t === "auto" ? localStorage.removeItem("theme") : localStorage.setItem("theme", t); } catch {}
}
let theme = "auto";
try { theme = localStorage.getItem("theme") || "auto"; } catch {}
if (!THEMES[theme]) theme = "auto";
applyTheme(theme);
$("#themeToggle").addEventListener("click", () => {
  const order = ["auto", "light", "dark"];
  theme = order[(order.indexOf(theme) + 1) % 3]; applyTheme(theme);
});

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

// ---------- статус моделей ----------
async function refreshStatus() {
  const el = $("#status");
  try {
    const h = await api(`${API.ingest}/health`);
    $("#formats").textContent = h.formats.map((f) => f.slice(1).toUpperCase()).join(", ");
    $("#scanInbox").title = `Проиндексировать всё из папки ${h.inbox}`;
    state.canOpen = !!h.can_open;
    const m = h.models;
    if (!m.llm) {
      el.innerHTML = `<b>Ollama не запущена.</b> Установите её с ollama.com (один раз) и откройте — затем перезапустите DocSearch.<br><span class="muted">${esc(m.error || "")}</span>`;
      el.className = "status bad"; return false;
    }
    const parts = [m.llm, m.embed].map((x) => `${x.ready ? "✓" : "✗"} ${esc(x.name)}${!x.ready && x.pull ? ` (${esc(x.pull)})` : ""}`);
    if (m.ok) { el.innerHTML = "Модели готовы: " + parts.join(" · "); el.className = "status"; return true; }
    const pulling = [m.llm, m.embed].some((x) => x.pull && !/^(готово|ошибка)/.test(x.pull));
    el.innerHTML = `Не хватает моделей: ${parts.join(" · ")}<br>` +
      (pulling ? "Скачиваю… (нужен интернет только сейчас)" : `<button class="link" id="pullModels">Скачать модели (≈3 ГБ, один раз)</button>`);
    el.className = "status bad";
    $("#pullModels")?.addEventListener("click", async () => { await api(`${API.ingest}/models/pull`, { method: "POST" }); refreshStatus(); });
    return false;
  } catch (e) {
    el.textContent = "Сервер не отвечает: " + e.message; el.className = "status bad";
    return false;
  }
}

// ---------- документы ----------
async function refreshDocs() {
  let docs = [];
  try { docs = await api(`${API.ingest}/documents`); } catch { return; }
  state.docs = docs;
  renderKb();
  refreshGraph();
  $("#docCount").textContent = docs.length ? `(${docs.length})` : "";
  $("#kbCount").textContent = docs.length ? `(${docs.length})` : "";
  const ids = new Set(docs.map((d) => d.doc_id));
  for (const id of [...state.selected]) if (!ids.has(id)) state.selected.delete(id);
  const outdated = docs.filter((d) => d.outdated).length;
  $("#reindex").hidden = !outdated;
  $("#reindex").textContent = `Обновить индекс старых документов (${outdated}) — поиск станет точнее`;
  if (!docs.length) { ul.innerHTML = `<li class="docs-empty">Пока ничего не загружено</li>`; updateScopeHint(); return; }
  renderSidebarDocs();
  updateScopeHint();
}

// Список слева: при тысячах писем рисуем только первые SIDEBAR_LIMIT подходящих под фильтр
const SIDEBAR_LIMIT = 150;
function renderSidebarDocs() {
  const needle = $("#docFilter").value.trim().toLowerCase();
  const all = state.docs.filter((d) => !needle || d.filename.toLowerCase().includes(needle));
  // отмеченные — всегда видны, иначе галочку не снять
  const shown = [...all.filter((d) => state.selected.has(d.doc_id)), ...all.filter((d) => !state.selected.has(d.doc_id))].slice(0, SIDEBAR_LIMIT);
  $("#docs").innerHTML = shown.map((d) => `
    <li>
      <label title="${esc(d.filename)}">
        <input type="checkbox" data-id="${d.doc_id}" ${state.selected.has(d.doc_id) ? "checked" : ""}>
        <span class="fname">${esc(d.filename)}</span>
      </label>
      <span class="meta">${d.chunks} фр.</span>
      <button class="del" data-id="${d.doc_id}" data-name="${esc(d.filename)}" title="Удалить из базы">✕</button>
    </li>`).join("") + (all.length > shown.length
      ? `<li class="docs-empty">…и ещё ${(all.length - shown.length).toLocaleString("ru-RU")}. Уточните поиск или откройте «Базу знаний».</li>` : "")
    + (!all.length ? `<li class="docs-empty">Ничего не найдено</li>` : "");
}
let docFilterTimer;
$("#docFilter").addEventListener("input", () => { clearTimeout(docFilterTimer); docFilterTimer = setTimeout(renderSidebarDocs, 150); });

function updateScopeHint() {
  $("#scopeHint").textContent = state.selected.size
    ? `Поиск только в выбранных: ${state.selected.size}.`
    : "Поиск по всем документам. Отметьте галочками, чтобы искать только в выбранных.";
}

$("#docs").addEventListener("change", (e) => {
  const id = e.target.dataset.id; if (!id) return;
  e.target.checked ? state.selected.add(id) : state.selected.delete(id);
  updateScopeHint();
});
$("#docs").addEventListener("click", (e) => {
  const btn = e.target.closest(".del"); if (btn) deleteDoc(btn.dataset.id, btn.dataset.name);
});

async function deleteDoc(id, name) {
  if (!confirm(`Удалить «${name}» из базы знаний?`)) return false;
  await api(`${API.ingest}/documents/${id}`, { method: "DELETE" });
  if (state.openDoc === id) closeDetail();
  refreshDocs();
  return true;
}

// ---------- база знаний ----------
const fmtSize = (b) => b == null ? "—" : b < 1024 ? `${b} Б` : b < 1048576 ? `${(b / 1024).toFixed(0)} КБ` : `${(b / 1048576).toFixed(1)} МБ`;
const fmtDate = (iso, full = false) => !iso ? "—" : new Date(iso).toLocaleString("ru-RU", full
  ? { day: "numeric", month: "long", year: "numeric", hour: "2-digit", minute: "2-digit" }
  : { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
const plural = (n, one, few, many) => { const m10 = n % 10, m100 = n % 100; return m10 === 1 && m100 !== 11 ? one : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? few : many; };

// Реквизиты и связи считаются дольше (нужно прочитать документы), поэтому подгружаются отдельно
let graphBusy = false;
async function refreshGraph() {
  if (graphBusy) return; graphBusy = true;
  try { state.graph = await api(`${API.analysis}/graph`); indexGraph(); renderKb(); } catch { /* анализ недоступен — таблица работает и без него */ }
  finally { graphBusy = false; }
}
// Индексы по doc_id: при тысячах писем поиск перебором на каждой строке таблицы тормозит
let graphIndex = { docs: new Map(), parent: new Map(), thread: new Map() };
function indexGraph() {
  const g = state.graph;
  graphIndex = { docs: new Map(g.docs.map((d) => [d.doc_id, d])), parent: new Map(), thread: new Map() };
  for (const l of g.links) {
    const cur = graphIndex.parent.get(l.from);
    // главная связь документа — с договором, иначе первая найденная
    if (!cur || g.docs && graphIndex.docs.get(l.to)?.type === "contract") graphIndex.parent.set(l.from, l);
  }
  for (const t of g.threads || []) for (const id of t.docs) graphIndex.thread.set(id, t);
}
const docInfo = (id) => graphIndex.docs.get(id);
const parentOf = (id) => graphIndex.parent.get(id);
const threadOf = (id) => graphIndex.thread.get(id);
const reqText = (m) => !m ? "" : [m.number ? `№ ${m.number}` : "", m.date ? `от ${m.date.split("-").reverse().join(".")}` : ""].filter(Boolean).join(" ");

function showTab(name) {
  state.tab = name;
  const tabFor = name === "analysis" ? "kb" : name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tabFor));
  for (const v of ["chat", "kb", "risks", "analysis", "admin"]) $(`#view-${v}`).hidden = v !== name;
  $("#clearChat").hidden = name !== "chat";
  if (name === "kb") refreshDocs();
  if (name === "risks") window.renderRisksTab?.();
  if (name === "admin") window.renderAdmin?.();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

const KB_PAGE = 200;
$("#kbMore").addEventListener("click", () => { state.kbLimit += KB_PAGE; renderKb(); });

function renderKb() {
  const docs = state.docs;
  const total = docs.reduce((a, d) => a + (d.size || 0), 0);
  const chunks = docs.reduce((a, d) => a + d.chunks, 0);
  const last = docs.map((d) => d.uploaded_at).filter(Boolean).sort().pop();
  const outdated = docs.filter((d) => d.outdated).length;
  $("#kbStats").innerHTML = [
    [docs.length, plural(docs.length, "документ", "документа", "документов")],
    [fmtSize(total), "общий объём"],
    [chunks, plural(chunks, "фрагмент", "фрагмента", "фрагментов")],
    [last ? fmtDate(last) : "—", "последняя загрузка"],
  ].map(([v, l]) => `<div class="stat"><b>${esc(v)}</b><span>${l}</span></div>`).join("") +
    (outdated ? `<div class="stat warn"><b>${outdated}</b><span>с устаревшим индексом</span></div>` : "");

  const fmtSel = $("#kbFormat"), cur = fmtSel.value;
  const formats = [...new Set(docs.map((d) => d.format))].sort();
  fmtSel.innerHTML = `<option value="">Все форматы</option>` + formats.map((f) => `<option value="${esc(f)}">${esc(f.toUpperCase())}</option>`).join("");
  fmtSel.value = formats.includes(cur) ? cur : "";

  const typeSel = $("#kbType"), curType = typeSel.value;
  const types = [...new Set(docs.map((d) => docInfo(d.doc_id)?.type).filter(Boolean))];
  typeSel.innerHTML = `<option value="">Все типы</option>` + types.map((t) => `<option value="${t}">${esc(state.graph.types[t])}</option>`).join("");
  typeSel.value = types.includes(curType) ? curType : "";

  const needle = $("#kbFilter").value.trim().toLowerCase();
  const sort = $("#kbSort").value;
  const rows = docs
    .filter((d) => (!needle || d.filename.toLowerCase().includes(needle) || (docInfo(d.doc_id)?.title || "").toLowerCase().includes(needle)
        || (docInfo(d.doc_id)?.sender || "").toLowerCase().includes(needle) || (docInfo(d.doc_id)?.recipient || "").toLowerCase().includes(needle))
      && (!fmtSel.value || d.format === fmtSel.value) && (!typeSel.value || docInfo(d.doc_id)?.type === typeSel.value))
    .sort((a, b) => sort === "name" ? a.filename.localeCompare(b.filename, "ru")
      : sort === "size" ? (b.size || 0) - (a.size || 0)
      : sort === "chunks" ? b.chunks - a.chunks
      : sort === "date-asc" ? (a.uploaded_at || "").localeCompare(b.uploaded_at || "")
      : (b.uploaded_at || "").localeCompare(a.uploaded_at || ""));

  state.kbRows = rows;
  const page = rows.slice(0, state.kbLimit);
  $("#kbMore").hidden = rows.length <= page.length;
  $("#kbMore").textContent = `Показать ещё (${Math.min(KB_PAGE, rows.length - page.length)} из ${(rows.length - page.length).toLocaleString("ru-RU")})`;
  $("#kbRows").innerHTML = rows.length ? page.map((d) => {
    const m = docInfo(d.doc_id), link = parentOf(d.doc_id), target = link && docInfo(link.to);
    return `
    <tr data-id="${d.doc_id}" class="${state.openDoc === d.doc_id ? "active" : ""}">
      <td class="name" title="${esc(d.filename)}"><span class="fmt">${esc(d.format.toUpperCase())}</span> ${m?.subject
        ? `${esc(m.subject)}<div class="sub muted">${esc([m.sender, m.recipient].filter(Boolean).map((x) => x.slice(0, 40)).join(" → "))} · ${esc(d.filename)}</div>`
        : esc(d.filename)}${d.outdated ? ` <span class="badge" title="Проиндексирован старой версией — переиндексируйте для точного поиска">старый индекс</span>` : ""}</td>
      <td>${m ? `<span class="type t-${m.type}">${esc(m.type_name)}</span>` : `<span class="muted">…</span>`}</td>
      <td class="muted">${esc(reqText(m)) || "—"}</td>
      <td class="name link-cell">${target ? `<span class="muted">${esc(link.relation)}</span> ${esc(target.title || target.filename)}` : "—"}</td>
      <td class="num">${fmtSize(d.size)}</td>
      <td class="num">${d.chunks}</td>
      <td class="date">${fmtDate(d.uploaded_at)}</td>
    </tr>`;
  }).join("")
    : `<tr><td colspan="7" class="kb-empty">${docs.length ? "Ничего не найдено" : "База знаний пуста. Перетащите документы в левую панель."}</td></tr>`;
  $("#kbTable").hidden = state.kbMode !== "table";
  $("#kbMore").hidden = state.kbMode !== "table" || rows.length <= page.length;
  $("#kbChains").hidden = state.kbMode !== "chains";
  $("#kbThreads").hidden = state.kbMode !== "threads";
  if (state.kbMode === "chains") window.renderChains?.(rows);
  if (state.kbMode === "threads") window.renderThreads?.(rows);
}
["input", "change"].forEach((ev) => ["#kbFilter", "#kbFormat", "#kbType", "#kbSort"].forEach((id) => $(id).addEventListener(ev, () => { state.kbLimit = KB_PAGE; renderKb(); })));
$("#kbMode").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-mode]"); if (!b) return;
  state.kbMode = b.dataset.mode;
  $("#kbMode").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  renderKb();
});
$("#kbRows").addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) openDetail(tr.dataset.id); });

function closeDetail() { state.openDoc = null; $("#kbDetail").hidden = true; renderKb(); }

async function openDetail(id) {
  state.openDoc = id; renderKb();
  const box = $("#kbDetail"); box.hidden = false;
  box.innerHTML = `<div class="muted">Загружаю…</div>`;
  let d;
  try { d = await api(`${API.ingest}/documents/${id}`); } catch (e) { box.innerHTML = `<div class="err">Не удалось открыть: ${esc(e.message)}</div>`; return; }
  if (state.openDoc !== id) return;
  const row = (k, v) => `<dt>${k}</dt><dd>${v}</dd>`;
  box.innerHTML = `
    <div class="kb-detail-head"><h2 title="${esc(d.filename)}">${esc(d.filename)}</h2><button class="icon-btn" id="kbClose" aria-label="Закрыть">✕</button></div>
    <div class="kb-actions">
      <button class="send small" id="kbAsk">Спросить по документу</button>
      ${d.has_file && state.canOpen ? `<button class="btn" id="kbOpen">Открыть файл</button>` : ""}
      ${d.has_file ? `<a class="btn" href="${API.ingest}/documents/${id}/file" download="${esc(d.filename)}">Скачать</a>` : ""}
      ${d.has_file ? `<button class="btn" id="kbReindex">Переиндексировать</button>` : ""}
      <button class="btn danger" id="kbDelete">Удалить</button>
    </div>
    <dl class="kb-meta">
      ${row("Формат", esc(d.format.toUpperCase()))}
      ${row("Размер", d.size != null ? `${fmtSize(d.size)} <span class="muted">(${d.size.toLocaleString("ru-RU")} байт)</span>` : "копия файла не сохранилась")}
      ${row("Загружен", fmtDate(d.uploaded_at, true))}
      ${d.pages ? row("Страниц", d.pages) : ""}
      ${row("Фрагментов", d.chunks)}
      ${row("Текста", `${d.characters.toLocaleString("ru-RU")} символов`)}
      ${row("Индекс", d.outdated ? `<span class="warn">старой версии — переиндексируйте</span>` : "актуальный")}
      ${row("ID", `<code>${esc(d.doc_id)}</code>`)}
    </dl>
    <div id="kbAnalysis"></div>
    ${d.sections.length ? `<details class="kb-sections"><summary>Разделы (${d.sections.length})</summary><ol>${d.sections.map((x) => `<li>${esc(x)}</li>`).join("")}</ol></details>` : ""}
    <div class="kb-chunks-head"><b>Текст документа</b><input type="search" id="kbChunkFilter" placeholder="Найти в тексте…"></div>
    <div class="kb-chunks" id="kbChunks"></div>`;

  const renderChunks = () => {
    const q = $("#kbChunkFilter").value.trim().toLowerCase();
    const list = d.chunks_list.filter((c) => !q || c.text.toLowerCase().includes(q));
    const mark = (t) => { const e = esc(t); if (!q) return e; const re = new RegExp(esc(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"); return e.replace(re, (m) => `<mark>${m}</mark>`); };
    $("#kbChunks").innerHTML = list.length ? list.map((c) => `
      <div class="src"><div class="src-head"><span>Фрагмент ${c.chunk + 1}${c.page ? ` · стр. ${c.page}` : ""}${c.section ? ` · ${esc(c.section)}` : ""}</span></div>
      <div class="src-text full">${mark(c.text)}</div></div>`).join("") : `<div class="muted">Не найдено</div>`;
  };
  $("#kbChunkFilter").addEventListener("input", renderChunks);
  renderChunks();

  $("#kbClose").addEventListener("click", closeDetail);
  $("#kbAsk").addEventListener("click", () => {
    state.selected = new Set([id]); refreshDocs(); showTab("chat"); $("#question").focus();
  });
  $("#kbOpen")?.addEventListener("click", () => api(`${API.ingest}/documents/${id}/open`, { method: "POST" }).catch((e) => alert(e.message)));
  $("#kbReindex")?.addEventListener("click", async () => {
    await api(`${API.ingest}/reindex?doc_id=${id}`, { method: "POST" }); pollJobs();
    $("#kbReindex").disabled = true; $("#kbReindex").textContent = "В очереди…";
  });
  $("#kbDelete").addEventListener("click", () => deleteDoc(id, d.filename));
  window.enhanceDetail?.(id, $("#kbAnalysis"));
}

// ---------- загрузка ----------
const STATUS_RU = { queued: "в очереди", parsing: "читаю текст", embedding: "считаю эмбеддинги", saving: "сохраняю в базу", done: "готово", skipped: "уже в базе", error: "ошибка", waiting: "жду нейросеть (Ollama не отвечает)" };

// Тысячи писем отправляем пачками (до 40 файлов / 40 МБ): один огромный запрос браузер может не осилить,
// а так видно, сколько уже отправлено, и сбой одной пачки не теряет остальные
const BATCH_FILES = 40, BATCH_BYTES = 40 * 1024 * 1024;
async function uploadFiles(files) {
  if (!files.length) return;
  const batches = [];
  let cur = [], size = 0;
  for (const f of files) {
    if (cur.length && (cur.length >= BATCH_FILES || size + f.size > BATCH_BYTES)) { batches.push(cur); cur = []; size = 0; }
    cur.push(f); size += f.size;
  }
  if (cur.length) batches.push(cur);
  const rejected = [];
  let sent = 0, accepted = 0;
  const box = $("#uploadProgress");
  box.hidden = files.length < 2;
  for (const batch of batches) {
    box.innerHTML = `Отправляю файлы: ${sent.toLocaleString("ru-RU")} из ${files.length.toLocaleString("ru-RU")}<div class="bar"><i style="width:${Math.round(100 * sent / files.length)}%"></i></div>`;
    const fd = new FormData();
    for (const f of batch) fd.append("files", f, f.name);
    try {
      const res = await api(`${API.ingest}/upload`, { method: "POST", body: fd });
      rejected.push(...res.rejected);
      accepted += res.accepted_count ?? res.accepted.length;
    } catch (e) { rejected.push(...batch.map((f) => ({ filename: f.name, error: e.message }))); }
    sent += batch.length;
    pollJobs();
  }
  box.hidden = true;
  if (rejected.length) alert(`Не принято файлов: ${rejected.length}\n` + rejected.slice(0, 30).map((r) => `${r.filename}: ${r.error}`).join("\n")
    + (rejected.length > 30 ? `\n…и ещё ${rejected.length - 30}` : ""));
  else if (files.length > 1) toast(`Принято в обработку: ${accepted.toLocaleString("ru-RU")} документов`);
}

let pollTimer = null, lastFinished = null, docsTimer = null;
async function pollJobs() {
  clearTimeout(pollTimer);
  let sum;
  try { sum = await api(`${API.ingest}/jobs/summary`); } catch { return; }
  const c = sum.counts, n = (k) => c[k] || 0;
  const waiting = n("queued") + n("parsing") + n("embedding") + n("saving") + n("waiting");
  const finished = n("done") + n("skipped") + n("error");
  const head = waiting ? `<div class="job-summary">
      <b>Обработка: ${finished.toLocaleString("ru-RU")} из ${(finished + waiting).toLocaleString("ru-RU")}</b>
      <div class="bar"><i style="width:${Math.round(100 * finished / Math.max(1, finished + waiting))}%"></i></div>
      <div class="state">готово ${n("done")}${n("skipped") ? ` · уже были в базе ${n("skipped")}` : ""}${n("error") ? ` · ошибок ${n("error")}` : ""}</div>
    </div>` : "";
  const row = (j) => {
    const pct = j.status === "embedding" && j.chunks ? Math.round((100 * j.done) / j.chunks) : j.status === "done" ? 100 : 5;
    const extra = j.status === "embedding" ? ` ${j.done}/${j.chunks}` : j.status === "done" ? ` · ${j.chunks} фрагментов` : "";
    return `<div class="job ${j.status}">
      <div class="name">${esc(j.filename)}</div>
      <div class="state">${STATUS_RU[j.status] || j.status}${extra}${j.error ? ": " + esc(j.error) : ""}</div>
      ${j.status !== "error" ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}
    </div>`;
  };
  const recent = sum.recent.filter((j) => Date.now() - Date.parse(j.finished_at) < 8000);
  const errors = sum.errors.slice(0, waiting ? 3 : 10);
  $("#jobs").innerHTML = head + sum.active.map(row).join("") + (waiting ? "" : recent.map(row).join(""))
    + errors.map(row).join("") + (n("error") > errors.length ? `<div class="state muted">…ошибок всего: ${n("error")}</div>` : "")
    + (!waiting && finished ? `<button class="link small" id="clearJobs">Очистить список</button>` : "");
  $("#clearJobs")?.addEventListener("click", async () => { await api(`${API.ingest}/jobs/clear`, { method: "POST" }); pollJobs(); });
  // при массовой загрузке документы готовы каждую секунду — список обновляем не чаще раза в 5 секунд
  if (sum.last_finished && sum.last_finished !== lastFinished) {
    lastFinished = sum.last_finished;
    if (!docsTimer) docsTimer = setTimeout(() => { docsTimer = null; refreshDocs(); }, waiting ? 5000 : 300);
  }
  pollTimer = setTimeout(pollJobs, waiting ? 1500 : 5000);
}

const dz = $("#dropzone");
$("#fileInput").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });
["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
document.addEventListener("dragover", (e) => e.preventDefault());
document.addEventListener("drop", (e) => { e.preventDefault(); if (!dz.contains(e.target)) uploadFiles([...e.dataTransfer.files]); });

$("#reindex").addEventListener("click", async () => {
  await api(`${API.ingest}/reindex`, { method: "POST" });
  $("#reindex").hidden = true;
  pollJobs();
});

$("#scanInbox").addEventListener("click", async () => {
  const r = await api(`${API.ingest}/inbox/scan`, { method: "POST" });
  if (!r.queued.length) alert("Папка inbox пуста. Положите туда файлы и нажмите ещё раз.\n" + $("#scanInbox").title.replace("Проиндексировать всё из папки ", ""));
  pollJobs();
});

// ---------- чат ----------
function addMessage(role, html, cls = "") {
  $("#empty")?.remove();
  const div = document.createElement("div");
  div.className = `msg ${role}`;
  div.innerHTML = `<div class="bubble ${cls}">${html}</div>`;
  $("#messages").appendChild(div);
  scrollDown();
  return div;
}
const scrollDown = () => { const m = $("#messages"); m.scrollTop = m.scrollHeight; };

// [1], [2, 3] → кликабельные ссылки; номера несуществующих фрагментов (модель иногда ошибается) убираем
function renderAnswer(text, nSources) {
  return esc(text).replace(/\s*\[(\d+(?:\s*,\s*\d+)*)\]/g, (_, nums) => {
    const valid = nums.split(/\s*,\s*/).filter((n) => +n >= 1 && +n <= nSources);
    return valid.length ? " " + valid.map((n) => `<span class="cite" data-n="${n}">${n}</span>`).join(" ") : "";
  });
}

function renderSources(sources) {
  if (!sources.length) return "";
  return `<details class="sources"><summary>Источники (${sources.length})</summary>${sources.map((s) => `
    <div class="src" data-n="${s.n}">
      <div class="src-head"><span>[${s.n}] ${esc(s.filename)}${s.page ? `, стр. ${esc(s.page)}` : ""}${s.section ? ` · ${esc(s.section)}` : ""}</span>${s.score != null ? `<span class="muted">${(s.score * 100).toFixed(0)}%</span>` : ""}</div>
      <div class="src-text">${esc(s.text)}</div>
    </div>`).join("")}</details>`;
}

$("#messages").addEventListener("click", (e) => {
  const c = e.target.closest(".cite"); if (!c) return;
  const msg = c.closest(".msg"); const det = msg.querySelector("details"); if (!det) return;
  det.open = true;
  msg.querySelectorAll(".src").forEach((s) => s.classList.toggle("hl", s.dataset.n === c.dataset.n));
  msg.querySelector(`.src[data-n="${c.dataset.n}"]`)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
});

async function ask(question) {
  if (state.busy || !question.trim()) return;
  state.busy = true; $("#send").disabled = true;
  addMessage("user", esc(question));
  const node = addMessage("bot", "Ищу в документах…", "thinking");
  const bubble = node.querySelector(".bubble");
  let answer = "", sources = [], query = "";

  try {
    const r = await fetch(`${API.chat}/ask`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question, history: state.history, doc_ids: state.selected.size ? [...state.selected] : null }),
    });
    if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
    const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
    for (;;) {
      const { value, done } = await reader.read(); if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, i); buf = buf.slice(i + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === "query") { query = ev.text; bubble.textContent = `Ищу: «${query}»…`; }
        else if (ev.type === "sources") { sources = ev.sources; bubble.textContent = sources.length ? "Нашёл фрагменты, формулирую ответ…" : ""; }
        else if (ev.type === "token") { answer += ev.text; bubble.classList.remove("thinking"); bubble.innerHTML = renderAnswer(answer, sources.length); scrollDown(); }
        else if (ev.type === "final") {
          // ответ, сверенный с источниками: исправлены номера ссылок; числа, которых нет в документах, — предупреждение
          answer = ev.text; bubble.innerHTML = renderAnswer(answer, sources.length);
          if (ev.unsupported.length) bubble.insertAdjacentHTML("afterend", `<div class="verify-warn">⚠ Не найдено в документах: ${ev.unsupported.map(esc).join(", ")}. Проверьте по источникам.</div>`);
          if (ev.other_document?.length) bubble.insertAdjacentHTML("afterend", `<div class="verify-warn">⚠ Возможно, условие взято из другого документа: ${ev.other_document.map(esc).join("; ")}</div>`);
        }
        else if (ev.type === "error") throw new Error(ev.error);
      }
    }
    bubble.insertAdjacentHTML("afterend", renderSources(sources));
    if (query) bubble.insertAdjacentHTML("beforebegin", `<div class="query muted">Искал: «${esc(query)}»</div>`);
    // Для следующего вопроса запоминаем, на какие фрагменты опирался ответ: так уточнения
    // («а какой там срок?») ищутся в том же документе
    const cited = new Set([...answer.matchAll(/\[(\d+(?:\s*,\s*\d+)*)\]/g)].flatMap((m) => m[1].split(/\s*,\s*/).map(Number)));
    const used = sources.filter((s) => cited.has(s.n));
    state.history.push({ role: "user", content: question }, {
      role: "assistant", content: answer,
      sources: (used.length ? used : sources).map((s) => ({ doc_id: s.doc_id, filename: s.filename, chunks: s.chunks })),
    });
  } catch (e) {
    bubble.className = "bubble err";
    bubble.textContent = "Ошибка: " + e.message;
  } finally {
    state.busy = false; $("#send").disabled = false; scrollDown();
  }
}

const q = $("#question");
q.addEventListener("input", () => { q.style.height = "auto"; q.style.height = q.scrollHeight + "px"; });
q.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
$("#composer").addEventListener("submit", (e) => { e.preventDefault(); const v = q.value; q.value = ""; q.style.height = "auto"; ask(v); });
document.querySelectorAll(".example").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
$("#clearChat").addEventListener("click", () => { state.history = []; location.reload(); });
$("#toggleSidebar").addEventListener("click", () => $("#sidebar").classList.toggle("open"));

// ---------- старт ----------
(async function init() {
  await window.authReady;   // сначала вход (auth.js)
  refreshDocs(); pollJobs();
  let ok = await refreshStatus();
  const tick = async () => { ok = await refreshStatus(); setTimeout(tick, ok ? 30000 : 3000); };
  setTimeout(tick, ok ? 30000 : 3000);
})();
