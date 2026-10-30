// DocSearch — раздел администратора: консоль, пользователи, журнал входов и действий, логи.
const AD = "/api/admin";
const json = (body, method = "POST") => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
let adminPane = "console";

window.renderAdmin = () => showPane(adminPane);

function showPane(name) {
  adminPane = name;
  $("#adminMode").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.pane === name));
  for (const p of ["console", "services", "users", "audit", "logs"]) $(`#pane-${p}`).hidden = p !== name;
  if (name === "console") { if (!$("#consoleOut").childElementCount) consolePrint("Консоль администратора DocSearch. Команды — help.", "muted"); $("#consoleInput").focus(); }
  if (name === "services") loadServices();
  if (name === "users") loadUsers();
  if (name === "audit") loadAudit();
  if (name === "logs") loadLogs();
}
$("#adminMode").addEventListener("click", (e) => { const b = e.target.closest("button[data-pane]"); if (b) showPane(b.dataset.pane); });

// ---------- консоль ----------
const conHistory = []; let histPos = 0;
function consolePrint(text, cls = "") {
  const div = document.createElement("div");
  div.className = `con-line ${cls}`;
  div.textContent = text;
  $("#consoleOut").appendChild(div);
  $("#consoleOut").scrollTop = $("#consoleOut").scrollHeight;
  return div;
}
async function runConsole(cmd) {
  consolePrint(`> ${cmd}`, "con-cmd");
  conHistory.push(cmd); histPos = conHistory.length;
  const wait = consolePrint("…", "muted");
  try {
    const r = await api(`${AD}/console`, json({ command: cmd }));
    wait.remove();
    const out = consolePrint(r.output, r.ok ? "" : "con-err");
    if (r.suggest) {
      const btn = document.createElement("button");
      btn.className = "btn small"; btn.textContent = `Выполнить: ${r.suggest}`;
      btn.addEventListener("click", () => { btn.remove(); runConsole(r.suggest); });
      out.appendChild(document.createElement("br")); out.appendChild(btn);
    }
    if (/^(meta|link|unlink|reindex|delete|risk)\b/.test(cmd)) { refreshGraph(); refreshDocs(); }
  } catch (e) { wait.remove(); consolePrint("Ошибка: " + errMsg(e), "con-err"); }
}
$("#consoleForm").addEventListener("submit", (e) => {
  e.preventDefault();
  const cmd = $("#consoleInput").value.trim(); if (!cmd) return;
  $("#consoleInput").value = "";
  if (cmd === "clear" || cmd === "cls") { $("#consoleOut").innerHTML = ""; return; }
  runConsole(cmd);
});
$("#consoleChips").addEventListener("click", (e) => { const b = e.target.closest("[data-cmd]"); if (b) runConsole(b.dataset.cmd); });
$("#consoleOut").addEventListener("click", () => { if (!getSelection().toString()) $("#consoleInput").focus(); });
$("#consoleInput").addEventListener("keydown", (e) => {
  if (e.key === "ArrowUp" && histPos > 0) { histPos--; $("#consoleInput").value = conHistory[histPos]; e.preventDefault(); }
  if (e.key === "ArrowDown") { histPos = Math.min(conHistory.length, histPos + 1); $("#consoleInput").value = conHistory[histPos] || ""; e.preventDefault(); }
});

// ---------- сервисы (контейнеры) ----------
async function loadServices() {
  $("#servicesList").innerHTML = `<div class="muted">Проверяю…</div>`;
  let r;
  try { r = await api(`${AD}/services`); } catch (e) { $("#servicesList").innerHTML = `<div class="err">${esc(errMsg(e))}</div>`; return; }
  $("#servicesMode").textContent = r.mode === "docker"
    ? "Docker-версия: каждая часть работает в своём контейнере. Логи каждого контейнера — во вкладке «Логи», в терминале: bash dev.sh logs <имя>."
    : "Настольная версия: все части работают внутри одного приложения, Docker и контейнеры не нужны. Версия с отдельными контейнерами запускается командой bash dev.sh up (нужен Docker Desktop).";
  $("#servicesList").innerHTML = r.services.map((s) => `
    <div class="svc ${s.ok ? "" : "bad"}">
      <div class="svc-head"><span class="dot"></span><b>${esc(s.name)}</b><span class="muted">${esc(s.role)}</span></div>
      <div class="small">${esc(s.where)}</div>
      <div class="small muted">${esc(s.info)}</div>
      ${s.log ? `<button class="link small" data-log="${esc(s.log)}">лог</button>` : ""}
    </div>`).join("");
}
$("#servicesList").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-log]"); if (!b) return;
  showPane("logs");
  setTimeout(() => { const opt = [...$("#logFile").options].find((o) => o.value === `${b.dataset.log}.log`); if (opt) { $("#logFile").value = opt.value; loadLogs(); } }, 400);
});

// ---------- пользователи ----------
async function loadUsers() {
  let users = [];
  try { users = await api(`${AD}/users`); } catch (e) { toast(errMsg(e), true); return; }
  const me = window.currentUser?.username;
  $("#usersTable").innerHTML = `<thead><tr><th>Пользователь</th><th>ФИО</th><th>Роль</th><th>Статус</th><th>Последний вход</th><th>Создан</th><th></th></tr></thead>
    <tbody>${users.map((u) => `<tr data-user="${esc(u.username)}">
      <td class="name">${esc(u.username)}${u.username === me ? ` <span class="muted">(вы)</span>` : ""}</td>
      <td>${esc(u.full_name || "—")}</td>
      <td><select data-act="role">${Object.entries({ user: "пользователь", admin: "администратор" }).map(([k, v]) => `<option value="${k}" ${u.role === k ? "selected" : ""}>${v}</option>`).join("")}</select></td>
      <td>${u.disabled ? `<span class="lvl hi">заблокирован</span>` : `<span class="lvl lo">активен</span>`}</td>
      <td class="date">${fmtDate(u.last_login)}</td>
      <td class="date">${fmtDate(u.created_at)}</td>
      <td><div class="actions">
        <button class="btn small" data-act="password">Сбросить пароль</button>
        <button class="btn small" data-act="toggle">${u.disabled ? "Разблокировать" : "Заблокировать"}</button>
        ${u.username !== me ? `<button class="btn small danger" data-act="delete">Удалить</button>` : ""}
      </div></td></tr>`).join("")}</tbody>`;
}
async function updateUser(name, body) {
  try { await api(`${AD}/users/${encodeURIComponent(name)}`, json(body, "PATCH")); toast("Сохранено"); }
  catch (e) { toast(errMsg(e), true); }
  loadUsers();
}
$("#usersTable").addEventListener("change", (e) => {
  const sel = e.target.closest("[data-act=role]"); if (!sel) return;
  updateUser(sel.closest("tr").dataset.user, { role: sel.value });
});
$("#usersTable").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-act]"); if (!b) return;
  const name = b.closest("tr").dataset.user;
  if (b.dataset.act === "toggle") updateUser(name, { disabled: b.textContent === "Заблокировать" });
  if (b.dataset.act === "password") {
    const v = await dialog({ title: `Новый пароль для ${name}`, ok: "Сохранить", text: "Все сессии пользователя завершатся.",
      fields: [{ name: "password", label: "Новый пароль (от 8 символов)", type: "password", autocomplete: "new-password" }] });
    if (v) updateUser(name, { password: v.password });
  }
  if (b.dataset.act === "delete" && confirm(`Удалить пользователя ${name}?`)) {
    try { await api(`${AD}/users/${encodeURIComponent(name)}`, { method: "DELETE" }); toast("Удалён"); } catch (err) { toast(errMsg(err), true); }
    loadUsers();
  }
});
$("#newUserForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target));
  try { await api(`${AD}/users`, json(data)); toast(`Пользователь ${data.username} добавлен`); e.target.reset(); }
  catch (err) { toast(errMsg(err), true); }
  loadUsers();
});

// ---------- журнал ----------
async function loadAudit() {
  const user = $("#auditUser").value, action = $("#auditAction").value.trim();
  let rows = [];
  try { rows = await api(`${AD}/audit?limit=1000&user=${encodeURIComponent(user)}&action=${encodeURIComponent(action)}`); }
  catch (e) { toast(errMsg(e), true); return; }
  if (!user) {
    const sel = $("#auditUser"), names = [...new Set(rows.map((r) => r.user).filter(Boolean))].sort();
    sel.innerHTML = `<option value="">Все пользователи</option>` + names.map((n) => `<option>${esc(n)}</option>`).join("");
  }
  if ($("#auditFails").checked) rows = rows.filter((r) => !r.ok);
  $("#auditTable").innerHTML = `<thead><tr><th>Когда</th><th>Пользователь</th><th>Действие</th><th>Подробности</th><th>Адрес</th><th></th></tr></thead>
    <tbody>${rows.length ? rows.map((r) => `<tr class="${r.ok ? "" : "fail"}">
      <td class="date">${new Date(r.ts).toLocaleString("ru-RU")}</td>
      <td>${esc(r.user || "—")}</td>
      <td>${esc(r.action)}</td>
      <td class="wrap">${esc(r.detail)}</td>
      <td class="muted">${esc(r.ip)}</td>
      <td>${r.ok ? "" : `<span class="lvl hi">ошибка</span>`}</td></tr>`).join("")
      : `<tr><td colspan="6" class="kb-empty">Записей нет</td></tr>`}</tbody>`;
}
$("#auditRefresh").addEventListener("click", loadAudit);
$("#auditUser").addEventListener("change", loadAudit);
$("#auditFails").addEventListener("change", loadAudit);
let auditTimer; $("#auditAction").addEventListener("input", () => { clearTimeout(auditTimer); auditTimer = setTimeout(loadAudit, 300); });

// ---------- логи ----------
async function loadLogs() {
  let files = [];
  try { files = await api(`${AD}/logs`); } catch (e) { toast(errMsg(e), true); return; }
  const sel = $("#logFile"), cur = sel.value;
  sel.innerHTML = files.map((f) => `<option value="${esc(f.name)}">${esc(f.name)} · ${fmtSize(f.size)}</option>`).join("");
  if (files.some((f) => f.name === cur)) sel.value = cur;
  if (!files.length) { $("#logText").textContent = "Логов пока нет."; return; }
  const r = await api(`${AD}/logs/${encodeURIComponent(sel.value)}?lines=500`);
  $("#logText").textContent = r.text || "(пусто)";
  $("#logText").scrollTop = $("#logText").scrollHeight;
}
$("#logFile").addEventListener("change", loadLogs);
$("#logRefresh").addEventListener("click", loadLogs);
