// DocSearch — анализ документов: связи, сводная редакция договора, сравнение, реестр рисков.
// Использует общие функции из app.js: $, esc, api, state, docInfo, parentOf, reqText, showTab, openDetail.

const LEVEL_CLS = { "высокий": "hi", "средний": "mid", "низкий": "lo" };
const CL_STATUS = { changed: "изменён", added: "добавлен", deleted: "исключён", removed: "нет во втором", same: "без изменений" };

const errMsg = (e) => { try { return JSON.parse(e.message.replace(/^\d+\s/, "")).detail || e.message; } catch { return e.message; } };
const diffHtml = (diff) => diff.map(([k, t]) => k === "=" ? esc(t) : k === "+" ? `<ins>${esc(t)}</ins>` : `<del>${esc(t)}</del>`).join("");
const docName = (id) => { const d = docInfo(id); return d ? d.filename : id; };

// Читает поток NDJSON (анализ рисков, объяснение изменений) и отдаёт события по одному
async function readStream(url, body, onEvent) {
  const r = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  const reader = r.body.getReader(), dec = new TextDecoder(); let buf = "";
  for (;;) {
    const { value, done } = await reader.read(); if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, i); buf = buf.slice(i + 1);
      if (line.trim()) onEvent(JSON.parse(line));
    }
  }
}

// ---------- карточка документа: реквизиты, связи, действия ----------
window.enhanceDetail = async (id, box) => {
  if (!state.graph) await refreshGraph();
  const m = docInfo(id);
  if (!m || state.openDoc !== id) { box.innerHTML = ""; return; }
  const out = state.graph.links.filter((l) => l.from === id);
  const inc = state.graph.links.filter((l) => l.to === id);
  const hasAmendments = inc.some((l) => docInfo(l.from)?.type === "amendment");
  const canConsolidate = (m.type === "contract" && hasAmendments) || (m.type === "amendment" && out.length);
  const others = state.graph.docs.filter((d) => d.doc_id !== id && !out.some((l) => l.to === d.doc_id))
    .sort((a, b) => (a.type === "contract" ? 0 : 1) - (b.type === "contract" ? 0 : 1) || a.filename.localeCompare(b.filename, "ru"))
    .slice(0, 300);
  const linkRow = (l, otherId, label) => `<li><span class="muted">${esc(label)}</span>
      <button class="link" data-open="${otherId}">${esc(docName(otherId))}</button>
      ${l.auto ? `<span class="muted small" title="Связь найдена автоматически">· ${esc(l.evidence)}</span>` : `<span class="muted small">· вручную</span>`}
      <button class="unlink" data-from="${l.from}" data-to="${l.to}" title="Убрать связь">✕</button></li>`;

  box.innerHTML = `
    <div class="kb-block">
      <b>Реквизиты</b>
      <dl class="kb-meta">
        <dt>Тип</dt><dd><span class="type t-${m.type}">${esc(m.type_name)}</span></dd>
        ${m.number ? `<dt>Номер</dt><dd>№ ${esc(m.number)}</dd>` : ""}
        ${m.date ? `<dt>Дата</dt><dd>${esc(m.date.split("-").reverse().join("."))}</dd>` : ""}
        ${m.subject ? `<dt>Тема</dt><dd>${esc(m.subject)}</dd>` : ""}
        ${m.sender ? `<dt>От</dt><dd>${esc(m.sender)}</dd>` : ""}
        ${m.recipient ? `<dt>Кому</dt><dd>${esc(m.recipient)}</dd>` : ""}
        ${m.parties.length && !m.sender ? `<dt>Стороны</dt><dd>${m.parties.map((p) => esc(p.role ? `${p.role}: ${p.name || "—"}` : p.name)).join("<br>")}</dd>` : ""}
      </dl>
    </div>
    ${threadOf(id) ? `<div class="kb-block"><b>Цепочка переписки (${threadOf(id).docs.length})</b>
      <ul class="links">${threadOf(id).docs.map((t) => { const x = docInfo(t); return `<li class="${t === id ? "here" : ""}">
        <span class="muted">${x?.date ? x.date.split("-").reverse().join(".") : "—"}</span>
        ${t === id ? `<b>${esc(short(letterTitle(x), 60))}</b>` : `<button class="link" data-open="${t}">${esc(short(letterTitle(x), 60))}</button>`}
        <span class="dir">${esc(fromTo(x))}</span></li>`; }).join("")}</ul></div>` : ""}
    <div class="kb-block">
      <b>Связи</b>
      <ul class="links">
        ${out.map((l) => linkRow(l, l.to, l.relation)).join("")}
        ${inc.slice(0, 15).map((l) => linkRow(l, l.from, docInfo(l.from)?.type_name || "документ")).join("")}
        ${inc.length > 15 ? `<li class="muted">…и ещё ${inc.length - 15} связанных документов (вид «Связи» или «Переписка»)</li>` : ""}
        ${out.length || inc.length ? "" : `<li class="muted">Связей не найдено. Документы связываются автоматически по номеру и дате договора — или привяжите вручную.</li>`}
      </ul>
      <div class="row">
        <select id="linkTo"><option value="">Привязать к документу…</option>
          ${others.map((d) => `<option value="${d.doc_id}">${esc(d.type_name)}: ${esc(d.filename)}</option>`).join("")}</select>
        <button class="btn" id="linkBtn">Привязать</button>
      </div>
    </div>
    <div class="kb-block">
      <b>Анализ</b>
      <div class="kb-actions">
        <button class="btn" id="anRisks">Анализ рисков</button>
        ${canConsolidate ? `<button class="btn" id="anCons" title="Договор с учётом всех доп. соглашений">Сводная редакция</button>` : ""}
      </div>
      <div class="row">
        <select id="cmpWith"><option value="">Сравнить с…</option>
          ${state.graph.docs.filter((d) => d.doc_id !== id).slice(0, 500).map((d) => `<option value="${d.doc_id}">${esc(d.filename)}</option>`).join("")}</select>
        <button class="btn" id="cmpBtn">Сравнить</button>
      </div>
    </div>`;

  box.querySelectorAll("[data-open]").forEach((b) => b.addEventListener("click", () => openDetail(b.dataset.open)));
  box.querySelectorAll(".unlink").forEach((b) => b.addEventListener("click", async () => {
    await api(`${API.analysis}/links`, { method: "DELETE", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from_id: b.dataset.from, to_id: b.dataset.to }) });
    await refreshGraph(); openDetail(id);
  }));
  $("#linkBtn").addEventListener("click", async () => {
    const to = $("#linkTo").value; if (!to) return;
    await api(`${API.analysis}/links`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ from_id: id, to_id: to }) });
    await refreshGraph(); openDetail(id);
  });
  $("#anRisks").addEventListener("click", () => showRisks(id));
  $("#anCons")?.addEventListener("click", () => showConsolidated(id));
  $("#cmpBtn").addEventListener("click", () => { const b = $("#cmpWith").value; if (b) showCompare(id, b); });
};

// ---------- цепочки «договор → доп. соглашения, письма» ----------
window.renderChains = (rows) => {
  const box = $("#kbChains");
  if (!state.graph) { box.innerHTML = `<div class="muted pad">Читаю реквизиты документов…</div>`; return; }
  const visible = new Set(rows.map((d) => d.doc_id));
  const links = state.graph.links;
  const heads = state.graph.docs.filter((d) => d.type === "contract" || links.some((l) => l.to === d.doc_id));
  const byDate = (a, b) => (a.date || "").localeCompare(b.date || "") || a.filename.localeCompare(b.filename, "ru");
  const cards = heads.map((h) => {
    const kids = links.filter((l) => l.to === h.doc_id).map((l) => ({ ...docInfo(l.from), relation: l.relation })).filter((d) => d.doc_id).sort(byDate);
    if (!visible.has(h.doc_id) && !kids.some((k) => visible.has(k.doc_id))) return "";
    const parties = h.parties.filter((p) => p.role).map((p) => `${p.role}: ${p.name || "—"}`).join(" · ");
    return `<div class="chain">
      <div class="chain-head">
        <span class="type t-${h.type}">${esc(h.type_name)}</span>
        <button class="link strong" data-open="${h.doc_id}">${esc(h.title || h.filename)}</button>
        <span class="muted">${esc(reqText(h))}</span>
        <span class="grow"></span>
        ${kids.some((k) => k.type === "amendment") ? `<button class="btn small" data-cons="${h.doc_id}">Сводная редакция</button>` : ""}
        <button class="btn small" data-risks="${h.doc_id}">Риски</button>
      </div>
      ${parties ? `<div class="muted small">${esc(parties)}</div>` : ""}
      <div class="muted small">${esc(h.filename)}</div>
      ${kids.length ? `<ul class="chain-kids">${kids.slice(0, 20).map((k) => `<li>
          <span class="type t-${k.type}">${esc(k.type_name)}</span>
          <button class="link" data-open="${k.doc_id}">${esc(k.filename)}</button>
          <span class="muted">${esc(reqText(k))}</span></li>`).join("")}${kids.length > 20
            ? `<li class="muted">…и ещё ${kids.length - 20} — см. вид «Переписка» или фильтр по названию</li>` : ""}</ul>`
        : `<div class="muted small">Связанных документов нет</div>`}
    </div>`;
  }).join("");
  const headIds = new Set(heads.map((h) => h.doc_id)), linkedFrom = new Set(links.map((l) => l.from));
  const loose = rows.filter((d) => !headIds.has(d.doc_id) && !linkedFrom.has(d.doc_id));
  box.innerHTML = (cards || `<div class="muted pad">Договоров не найдено.</div>`) + (loose.length ? `
    <div class="chain loose"><div class="chain-head"><b>Без связи с договором</b><span class="muted">${loose.length}</span></div>
      <ul class="chain-kids">${loose.slice(0, 100).map((d) => { const m = docInfo(d.doc_id); return `<li>
        ${m ? `<span class="type t-${m.type}">${esc(m.type_name)}</span>` : ""}
        <button class="link" data-open="${d.doc_id}">${esc(d.filename)}</button>
        <span class="muted">${esc(reqText(m))}</span></li>`; }).join("")}${loose.length > 100 ? `<li class="muted">…и ещё ${loose.length - 100}</li>` : ""}</ul></div>` : "");
};
// ---------- переписка: цепочки писем ----------
const short = (s, n = 40) => (s || "").length > n ? s.slice(0, n - 1) + "…" : (s || "");
const fromTo = (m) => m?.sender || m?.recipient ? `${short(m.sender, 32) || "?"} → ${short(m.recipient, 32) || "?"}` : "";
let threadLimit = 50;
const openThreads = new Set();   // раскрытые цепочки: при массовой загрузке список перерисовывается каждые 5 с
const letterTitle = (m) => m?.subject || m?.title || m?.filename || "";
window.renderThreads = (rows) => {
  const box = $("#kbThreads");
  const scroll = box.parentElement.scrollTop;
  if (!state.graph) { box.innerHTML = `<div class="muted pad">Читаю реквизиты документов…</div>`; return; }
  const visible = new Set(rows.map((d) => d.doc_id));
  const all = (state.graph.threads || []).filter((t) => t.docs.some((id) => visible.has(id)));
  const letters = state.graph.docs.filter((d) => ["letter", "claim"].includes(d.type));
  const single = letters.filter((d) => !threadOf(d.doc_id) && visible.has(d.doc_id)).length;
  if (!all.length) {
    box.innerHTML = `<div class="muted pad">Цепочек переписки пока нет. Письма связываются автоматически: по «ответу» в почте,
      по ссылке «на ваш исх. № … от …» и по общей теме при общих участниках.${letters.length ? ` Писем в базе: ${letters.length}.` : ""}</div>`;
    return;
  }
  box.innerHTML = `<div class="muted small pad-s">Цепочек: ${all.length.toLocaleString("ru-RU")} · писем вне цепочек: ${single.toLocaleString("ru-RU")}</div>`
    + all.slice(0, threadLimit).map((t) => {
      const c = t.contract && docInfo(t.contract);
      return `<details class="chain" data-thread="${t.id}" ${openThreads.has(t.id) ? "open" : ""}>
        <summary class="chain-head">
          <b>${esc(t.subject || "(без темы)")}</b>
          <span class="muted">${t.docs.length} ${plural(t.docs.length, "письмо", "письма", "писем")}</span>
          <span class="thread-meta">${t.first ? t.first.split("-").reverse().join(".") : ""}${t.last && t.last !== t.first ? " — " + t.last.split("-").reverse().join(".") : ""}</span>
          ${c ? `<span class="type t-contract">${esc(short(c.title || c.filename, 40))}</span>` : ""}
        </summary>
        <div class="thread-meta">${esc(t.participants.join(", "))}</div>
        <ul class="chain-kids">${t.docs.map((id) => { const m = docInfo(id); const l = parentOf(id); return `<li>
          <span class="muted">${m?.date ? m.date.split("-").reverse().join(".") : "—"}</span>
          <button class="link" data-open="${id}">${esc(short(letterTitle(m), 70))}</button>
          <span class="dir">${esc(fromTo(m))}</span>
          ${l && docInfo(l.to)?.type !== "contract" ? `<span class="muted small">${esc(l.relation)}</span>` : ""}</li>`; }).join("")}</ul>
      </details>`;
    }).join("") + (all.length > threadLimit ? `<button class="link kb-more" id="moreThreads">Показать ещё цепочки (${all.length - threadLimit})</button>` : "");
  $("#moreThreads")?.addEventListener("click", () => { threadLimit += 50; renderKb(); });
  box.parentElement.scrollTop = scroll;
};
$("#kbThreads").addEventListener("toggle", (e) => {
  const d = e.target.closest("details[data-thread]");
  if (d) d.open ? openThreads.add(d.dataset.thread) : openThreads.delete(d.dataset.thread);
}, true);
$("#kbThreads").addEventListener("click", (e) => { const t = e.target.closest("[data-open]"); if (t) openDetail(t.dataset.open); });

$("#kbChains").addEventListener("click", (e) => {
  const t = e.target.closest("[data-open],[data-cons],[data-risks]"); if (!t) return;
  if (t.dataset.open) openDetail(t.dataset.open);
  else if (t.dataset.cons) showConsolidated(t.dataset.cons);
  else showRisks(t.dataset.risks);
});

// ---------- экран анализа ----------
function openAnalysis(title, sub) {
  showTab("analysis");
  $("#anTitle").textContent = title;
  $("#anSub").textContent = sub || "";
  $("#anBody").innerHTML = `<div class="muted">Загружаю…</div>`;
  $("#anBody").scrollTop = 0;
}
let backTab = "kb";
$("#anBack").addEventListener("click", () => showTab(backTab));

async function explainInto(box, body) {
  box.hidden = false; box.innerHTML = `<div class="muted">Нейросеть читает изменения…</div>`;
  let text = "";
  try {
    await readStream(`${API.analysis}/explain`, body, (ev) => {
      if (ev.type === "token") { text += ev.text; box.textContent = text; }
      else if (ev.type === "error") throw new Error(ev.error);
    });
  } catch (e) { box.innerHTML = `<div class="err">Ошибка: ${esc(errMsg(e))}</div>`; }
}

// Сводная редакция договора
async function showConsolidated(id) {
  backTab = state.tab === "analysis" ? backTab : state.tab;
  openAnalysis("Сводная редакция", "Применяю доп. соглашения к договору…");
  let r;
  try { r = await api(`${API.analysis}/consolidate/${id}`, { method: "POST" }); }
  catch (e) { $("#anBody").innerHTML = `<div class="err">${esc(errMsg(e))}</div>`; $("#anSub").textContent = ""; return; }
  const cid = r.contract.doc_id;
  $("#anSub").textContent = `${r.contract.label} в редакции: ${r.amendments.map((a) => a.label).join(", ")}`;
  const applied = r.ops.filter((o) => o.applied).length;
  const issues = [
    ...r.ops.filter((o) => o.by_llm && o.applied).map((o) => `<li class="warn-li">${esc(o.source)}: правка к п. ${esc(o.clause)} распознана нейросетью — сверьте текст.</li>`),
    ...r.ops.filter((o) => o.error).map((o) => `<li class="${o.applied ? "warn-li" : "err-li"}">${esc(o.source)}: ${esc(o.error)}<div class="muted small">${esc(o.quote)}</div></li>`),
    ...r.unparsed.map((u) => `<li class="err-li">${esc(u.source)}: не удалось применить автоматически — внесите вручную<div class="muted small">${esc(u.text)}</div></li>`),
  ];
  $("#anBody").innerHTML = `
    <div class="kb-stats">
      <div class="stat"><b>${applied} из ${r.ops.length + r.unparsed.length}</b><span>правок применено</span></div>
      <div class="stat"><b>${r.counts.changed}</b><span>пунктов изменено</span></div>
      <div class="stat"><b>${r.counts.added}</b><span>добавлено</span></div>
      <div class="stat"><b>${r.counts.deleted}</b><span>исключено</span></div>
    </div>
    ${issues.length ? `<ul class="issues">${issues.join("")}</ul>` : ""}
    <div class="kb-actions">
      <a class="send small" href="${API.analysis}/consolidate/${cid}/docx" download>Скачать DOCX</a>
      <a class="btn" href="${API.analysis}/consolidate/${cid}/docx?redline=true" download>DOCX с выделением правок</a>
      <button class="btn" id="consToKb">Добавить в базу знаний</button>
      <button class="btn" id="consExplain">Объяснить изменения</button>
      <label class="check"><input type="checkbox" id="consOnly"> только изменённые пункты</label>
    </div>
    <div class="explain" id="consExplainBox" hidden></div>
    <div class="clauses" id="consClauses"></div>`;
  const render = () => {
    const only = $("#consOnly").checked;
    $("#consClauses").innerHTML = r.clauses.filter((c) => !only || c.status).map((c) => `
      <div class="cl ${c.status || ""}">
        <div class="cl-num">${esc(c.num || "")}</div>
        <div class="cl-text"><span class="cl-t">${c.status === "changed" && c.diff ? diffHtml(c.diff) : c.status === "deleted" ? `<del>${esc(c.old || c.text)}</del>` : esc(c.text)}</span>
          ${c.status ? `<div class="cl-src"><span class="st st-${c.status}">${CL_STATUS[c.status]}</span> ${esc(c.sources.join("; "))}</div>` : ""}</div>
      </div>`).join("");
  };
  $("#consOnly").addEventListener("change", render); render();
  $("#consToKb").addEventListener("click", async (e) => {
    e.target.disabled = true;
    try { await api(`${API.analysis}/consolidate/${cid}/to_kb`, { method: "POST" }); e.target.textContent = "Добавляется в базу…"; pollJobs(); }
    catch (err) { alert(errMsg(err)); e.target.disabled = false; }
  });
  $("#consExplain").addEventListener("click", () => explainInto($("#consExplainBox"), { kind: "consolidated", doc_id: cid }));
}

// Сравнение двух документов
async function showCompare(a, b) {
  backTab = state.tab === "analysis" ? backTab : state.tab;
  openAnalysis("Сравнение документов", `${docName(a)} → ${docName(b)}`);
  let r;
  try { r = await api(`${API.analysis}/compare`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ a, b }) }); }
  catch (e) { $("#anBody").innerHTML = `<div class="err">${esc(errMsg(e))}</div>`; return; }
  $("#anBody").innerHTML = `
    <div class="kb-stats">
      <div class="stat"><b>${r.counts.changed}</b><span>${r.by_clauses ? "пунктов" : "абзацев"} изменено</span></div>
      <div class="stat"><b>${r.counts.added}</b><span>есть только во втором</span></div>
      <div class="stat"><b>${r.counts.removed}</b><span>есть только в первом</span></div>
      <div class="stat"><b>${r.counts.same}</b><span>совпадает</span></div>
    </div>
    <div class="hint muted">Сравнение ${r.by_clauses ? "по номерам пунктов" : "по абзацам (в документах нет нумерации пунктов)"}. <del>Зачёркнуто</del> — было в «${esc(r.a.filename)}», <ins>выделено</ins> — стало в «${esc(r.b.filename)}».</div>
    <div class="kb-actions">
      <button class="btn" id="cmpSwap">Поменять местами</button>
      <button class="btn" id="cmpExplain">Объяснить различия</button>
      <label class="check"><input type="checkbox" id="cmpOnly" checked> только различия</label>
    </div>
    <div class="explain" id="cmpExplainBox" hidden></div>
    <div class="clauses" id="cmpRows"></div>`;
  const render = () => {
    const only = $("#cmpOnly").checked;
    const rows = r.rows.filter((x) => !only || x.status !== "same");
    $("#cmpRows").innerHTML = rows.length ? rows.map((x) => `
      <div class="cl ${x.status === "removed" ? "deleted" : x.status === "same" ? "" : x.status}">
        <div class="cl-num">${esc(x.num)}</div>
        <div class="cl-text"><span class="cl-t">${x.status === "changed" ? diffHtml(x.diff) : x.status === "removed" ? `<del>${esc(x.a)}</del>` : x.status === "added" ? `<ins>${esc(x.b)}</ins>` : esc(x.a)}</span>
          ${x.status !== "same" ? `<div class="cl-src"><span class="st st-${x.status}">${CL_STATUS[x.status]}</span></div>` : ""}</div>
      </div>`).join("") : `<div class="muted">Документы совпадают.</div>`;
  };
  $("#cmpOnly").addEventListener("change", render); render();
  $("#cmpSwap").addEventListener("click", () => showCompare(b, a));
  $("#cmpExplain").addEventListener("click", () => explainInto($("#cmpExplainBox"), { kind: "compare", a, b }));
}

// ---------- риски ----------
function riskTable(risks, showDoc = false) {
  if (!risks.length) return `<tbody><tr><td class="kb-empty">Рисков не найдено</td></tr></tbody>`;
  return `<thead><tr><th>№</th>${showDoc ? "<th>Документ</th>" : ""}<th>Пункт</th><th>Риск</th><th>Уровень</th><th>Для кого</th><th>Рекомендация</th><th>Источник</th></tr></thead>
    <tbody>${risks.map((r, i) => `<tr>
      <td class="num">${i + 1}</td>
      ${showDoc ? `<td class="wrap"><button class="link" data-risks="${r.doc_id}">${esc(r.filename)}</button></td>` : ""}
      <td class="clause">${esc(r.clause)}</td>
      <td class="wrap">${esc(r.risk)}
        ${r.quote ? `<details class="quote"><summary>текст пункта</summary>${esc(r.quote)}</details>` : ""}
        ${r.contract_clause ? `<details class="quote" open><summary>пункт договора</summary>${esc(r.contract_clause)}</details>` : ""}</td>
      <td><span class="lvl ${LEVEL_CLS[r.level] || ""}">${esc(r.level)}</span></td>
      <td class="wrap">${esc(r.for_whom)}</td>
      <td class="wrap">${esc(r.recommendation)}</td>
      <td class="muted">${esc(r.source)}</td>
    </tr>`).join("")}</tbody>`;
}
const LEVEL_ORDER = { "высокий": 0, "средний": 1, "низкий": 2 };

async function showRisks(id) {
  backTab = state.tab === "analysis" ? backTab : state.tab;
  if (!state.graph) await refreshGraph();
  const m = docInfo(id) || { parties: [], type: "other", filename: id };
  const inc = state.graph?.links.filter((l) => l.to === id) || [];
  const hasAm = m.type === "contract" && inc.some((l) => docInfo(l.from)?.type === "amendment");
  const roles = [...new Set(m.parties.map((p) => p.role).filter(Boolean))];
  openAnalysis("Анализ рисков", m.filename);
  $("#anBody").innerHTML = `
    <div class="kb-actions">
      <label class="check">Оцениваем для стороны
        <select id="rSide"><option value="">все стороны</option>${roles.map((r) => `<option>${esc(r)}</option>`).join("")}</select></label>
      ${hasAm ? `<label class="check"><input type="checkbox" id="rCons" checked> с учётом доп. соглашений</label>` : ""}
      <label class="check" title="Правила находят типовые риски за секунды; нейросеть читает каждый пункт — дольше, но глубже"><input type="checkbox" id="rLlm" checked> проверка нейросетью</label>
      <button class="send small" id="rRun">Запустить анализ</button>
      <a class="btn" id="rExport" href="${API.analysis}/risks.xlsx?doc_id=${id}" download hidden>Выгрузить в Excel</a>
    </div>
    <div class="muted" id="rStatus"></div>
    <div class="bar" id="rBar" hidden><i style="width:0%"></i></div>
    <ul class="issues" id="rWarn" hidden></ul>
    <div class="kb-table-wrap grow-none"><table class="kb-table risk-table" id="rTable"></table></div>`;

  const show = (reg) => {
    $("#rTable").innerHTML = riskTable(reg.risks);
    $("#rExport").hidden = !reg.risks.length;
    const hi = reg.risks.filter((r) => r.level === "высокий").length;
    $("#rStatus").textContent = `Анализ от ${new Date(reg.created_at).toLocaleString("ru-RU")}: ${reg.risks.length} рисков, из них высоких — ${hi}. `
      + `Основа: ${reg.basis}${reg.side ? `; сторона: ${reg.side}` : ""}.`;
    if (reg.side) $("#rSide").value = reg.side;
  };
  try { show(await api(`${API.analysis}/risks/${id}`)); } catch { $("#rStatus").textContent = "Анализ ещё не проводился. Нажмите «Запустить анализ»."; }

  $("#rRun").addEventListener("click", async () => {
    const btn = $("#rRun"); btn.disabled = true;
    const found = []; $("#rWarn").hidden = true; $("#rWarn").innerHTML = "";
    $("#rTable").innerHTML = ""; $("#rBar").hidden = true;
    try {
      await readStream(`${API.analysis}/risks/${id}`, {
        side: $("#rSide").value || null, consolidated: !!$("#rCons")?.checked, use_llm: $("#rLlm").checked,
      }, (ev) => {
        if (ev.type === "status") $("#rStatus").textContent = ev.text;
        else if (ev.type === "progress") {
          $("#rStatus").textContent = ev.text || ""; $("#rBar").hidden = false;
          $("#rBar i").style.width = `${Math.round((100 * ev.done) / Math.max(ev.total, 1))}%`;
        } else if (ev.type === "risk") {
          found.push(ev.risk);
          found.sort((a, b) => (LEVEL_ORDER[a.level] ?? 3) - (LEVEL_ORDER[b.level] ?? 3));
          $("#rTable").innerHTML = riskTable(found);
        } else if (ev.type === "warning") { $("#rWarn").hidden = false; $("#rWarn").insertAdjacentHTML("beforeend", `<li class="warn-li">${esc(ev.text)}</li>`); }
        else if (ev.type === "done") { $("#rBar").hidden = true; show(ev.register); }
        else if (ev.type === "error") throw new Error(ev.error);
      });
    } catch (e) { $("#rStatus").innerHTML = `<span class="err">Ошибка: ${esc(errMsg(e))}</span>`; }
    finally { btn.disabled = false; }
  });
}

// ---------- вкладка «Реестр рисков» ----------
let registers = [];
window.renderRisksTab = async () => {
  try { registers = await api(`${API.analysis}/risks`); } catch { registers = []; }
  const sel = $("#rkDoc"), cur = sel.value;
  sel.innerHTML = `<option value="">Все документы</option>` + registers.map((r) => `<option value="${r.doc_id}">${esc(r.filename)}</option>`).join("");
  sel.value = registers.some((r) => r.doc_id === cur) ? cur : "";
  drawRisks();
};
function drawRisks() {
  const level = $("#rkLevel").value, doc = $("#rkDoc").value, q = $("#rkFilter").value.trim().toLowerCase();
  const all = registers.filter((r) => !doc || r.doc_id === doc)
    .flatMap((r) => r.risks.map((x) => ({ ...x, doc_id: r.doc_id, filename: r.filename })))
    .filter((x) => (!level || x.level === level) && (!q || `${x.risk} ${x.recommendation} ${x.quote}`.toLowerCase().includes(q)))
    .sort((a, b) => (LEVEL_ORDER[a.level] ?? 3) - (LEVEL_ORDER[b.level] ?? 3));
  $("#rkExport").href = `${API.analysis}/risks.xlsx${doc ? `?doc_id=${doc}` : ""}`;
  $("#rkTable").innerHTML = registers.length ? riskTable(all, true)
    : `<tbody><tr><td class="kb-empty">Реестр пуст. Откройте документ в базе знаний и нажмите «Анализ рисков».</td></tr></tbody>`;
}
["#rkLevel", "#rkDoc"].forEach((s) => $(s).addEventListener("change", drawRisks));
$("#rkFilter").addEventListener("input", drawRisks);
$("#rkTable").addEventListener("click", (e) => { const b = e.target.closest("[data-risks]"); if (b) showRisks(b.dataset.risks); });
