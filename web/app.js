// ДокПоиск — фронтенд без сборки и зависимостей.
const $ = (s) => document.querySelector(s);
const API = { ingest: "/api/ingest", chat: "/api/chat" };

const state = {
  history: [],          // [{role, content}] для контекста диалога
  selected: new Set(),  // doc_id выбранных документов (пусто = все)
  busy: false,
  knownJobs: new Set(),
};

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
    const m = h.models;
    if (!m.llm) {
      el.innerHTML = `<b>Ollama не запущена.</b> Установите её с ollama.com (один раз) и откройте — затем перезапустите ДокПоиск.<br><span class="muted">${esc(m.error || "")}</span>`;
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
  const ul = $("#docs");
  $("#docCount").textContent = docs.length ? `(${docs.length})` : "";
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
$("#docs").addEventListener("click", async (e) => {
  const btn = e.target.closest(".del"); if (!btn) return;
  if (!confirm(`Удалить «${btn.dataset.name}» из базы знаний?`)) return;
  await api(`${API.ingest}/documents/${btn.dataset.id}`, { method: "DELETE" });
  refreshDocs();
});

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
