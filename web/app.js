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
  kbMode: "table",      // таблица или цепочки «договор → доп. соглашения, письма»
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
  const ul = $("#docs");
  $("#docCount").textContent = docs.length ? `(${docs.length})` : "";
  $("#kbCount").textContent = docs.length ? `(${docs.length})` : "";
  const ids = new Set(docs.map((d) => d.doc_id));
  for (const id of [...state.selected]) if (!ids.has(id)) state.selected.delete(id);
  const outdated = docs.filter((d) => d.outdated).length;
  $("#reindex").hidden = !outdated;
  $("#reindex").textContent = `Обновить индекс старых документов (${outdated}) — поиск станет точнее`;
  if (!docs.length) { ul.innerHTML = `<li class="docs-empty">Пока ничего не загружено</li>`; updateScopeHint(); return; }
  ul.innerHTML = docs.map((d) => `
    <li>
      <label title="${esc(d.filename)}">
        <input type="checkbox" data-id="${d.doc_id}" ${state.selected.has(d.doc_id) ? "checked" : ""}>
        <span class="fname">${esc(d.filename)}</span>
      </label>
      <span class="meta">${d.chunks} фр.</span>
      <button class="del" data-id="${d.doc_id}" data-name="${esc(d.filename)}" title="Удалить из базы">✕</button>
    </li>`).join("");
  updateScopeHint();
}

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
  try { state.graph = await api(`${API.analysis}/graph`); renderKb(); } catch { /* анализ недоступен — таблица работает и без него */ }
  finally { graphBusy = false; }
}
const docInfo = (id) => state.graph?.docs.find((d) => d.doc_id === id);
const parentOf = (id) => state.graph?.links.find((l) => l.from === id);
const reqText = (m) => !m ? "" : [m.number ? `№ ${m.number}` : "", m.date ? `от ${m.date.split("-").reverse().join(".")}` : ""].filter(Boolean).join(" ");

function showTab(name) {
  state.tab = name;
  const tabFor = name === "analysis" ? "kb" : name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === tabFor));
  for (const v of ["chat", "kb", "risks", "analysis"]) $(`#view-${v}`).hidden = v !== name;
  $("#clearChat").hidden = name !== "chat";
  if (name === "kb") refreshDocs();
  if (name === "risks") window.renderRisksTab?.();
}
document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));

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
    .filter((d) => (!needle || d.filename.toLowerCase().includes(needle) || (docInfo(d.doc_id)?.title || "").toLowerCase().includes(needle))
      && (!fmtSel.value || d.format === fmtSel.value) && (!typeSel.value || docInfo(d.doc_id)?.type === typeSel.value))
    .sort((a, b) => sort === "name" ? a.filename.localeCompare(b.filename, "ru")
      : sort === "size" ? (b.size || 0) - (a.size || 0)
      : sort === "chunks" ? b.chunks - a.chunks
      : sort === "date-asc" ? (a.uploaded_at || "").localeCompare(b.uploaded_at || "")
      : (b.uploaded_at || "").localeCompare(a.uploaded_at || ""));

  $("#kbRows").innerHTML = rows.length ? rows.map((d) => {
    const m = docInfo(d.doc_id), link = parentOf(d.doc_id), target = link && docInfo(link.to);
    return `
    <tr data-id="${d.doc_id}" class="${state.openDoc === d.doc_id ? "active" : ""}">
      <td class="name" title="${esc(d.filename)}"><span class="fmt">${esc(d.format.toUpperCase())}</span> ${esc(d.filename)}${d.outdated ? ` <span class="badge" title="Проиндексирован старой версией — переиндексируйте для точного поиска">старый индекс</span>` : ""}</td>
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
  $("#kbChains").hidden = state.kbMode !== "chains";
  if (state.kbMode === "chains") window.renderChains?.(rows);
}
["input", "change"].forEach((ev) => ["#kbFilter", "#kbFormat", "#kbType", "#kbSort"].forEach((id) => $(id).addEventListener(ev, renderKb)));
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
const STATUS_RU = { queued: "в очереди", parsing: "читаю текст", embedding: "считаю эмбеддинги", saving: "сохраняю в базу", done: "готово", error: "ошибка" };

async function uploadFiles(files) {
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f, f.name);
  try {
    const res = await api(`${API.ingest}/upload`, { method: "POST", body: fd });
    if (res.rejected.length) alert(res.rejected.map((r) => `${r.filename}: ${r.error}`).join("\n"));
  } catch (e) { alert("Не удалось загрузить: " + e.message); }
  pollJobs();
}

let pollTimer = null;
async function pollJobs() {
  clearTimeout(pollTimer);
  let jobs = [];
  try { jobs = await api(`${API.ingest}/jobs`); } catch { return; }
  const recent = jobs.filter((j) => j.status !== "done" || Date.now() - Date.parse(j.finished_at) < 8000);
  $("#jobs").innerHTML = recent.map((j) => {
    const pct = j.status === "embedding" && j.chunks ? Math.round((100 * j.done) / j.chunks) : j.status === "done" ? 100 : 5;
    const extra = j.status === "embedding" ? ` ${j.done}/${j.chunks}` : j.status === "done" ? ` · ${j.chunks} фрагментов` : "";
    return `<div class="job ${j.status}">
      <div class="name">${esc(j.filename)}</div>
      <div class="state">${STATUS_RU[j.status] || j.status}${extra}${j.error ? ": " + esc(j.error) : ""}</div>
      ${j.status !== "error" ? `<div class="bar"><i style="width:${pct}%"></i></div>` : ""}
    </div>`;
  }).join("");
  let finishedNow = false;
  for (const j of jobs) if (j.status === "done" && !state.knownJobs.has(j.job_id)) { state.knownJobs.add(j.job_id); finishedNow = true; }
  if (finishedNow) refreshDocs();
  const active = jobs.some((j) => !["done", "error"].includes(j.status));
  pollTimer = setTimeout(pollJobs, active ? 1000 : 5000);
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
  refreshDocs(); pollJobs();
  let ok = await refreshStatus();
  const tick = async () => { ok = await refreshStatus(); setTimeout(tick, ok ? 30000 : 3000); };
  setTimeout(tick, ok ? 30000 : 3000);
})();
