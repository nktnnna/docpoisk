// DocSearch — вход, сессия, диалоги, уведомления и скачивание файлов. Загружается первым.
const $a = (s) => document.querySelector(s);
const escA = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let authResolve;
window.authReady = new Promise((r) => { authResolve = r; });   // app.js начинает работу после входа
window.currentUser = null;
let appStarted = false;

// Любой ответ 401 (сессия истекла, пароль сменили) — показываем вход, а не ошибку
const rawFetch = window.fetch.bind(window);
window.fetch = async (input, init) => {
  const r = await rawFetch(input, init);
  if (r.status === 401 && !String(input).includes("/api/auth/")) showLogin("Сессия завершилась — войдите снова.");
  return r;
};

// ---------- уведомления и диалоги ----------
let toastTimer;
function toast(text, bad = false) {
  const t = $a("#toast");
  t.textContent = text; t.className = `toast${bad ? " bad" : ""}`; t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, bad ? 7000 : 3500);
}

// Окно с полями (вместо prompt(), который во встроенном окне macOS не работает). Возвращает значения или null.
function dialog({ title, text = "", fields = [], ok = "OK" }) {
  return new Promise((resolve) => {
    const back = document.createElement("div");
    back.className = "auth dialog-back";
    back.innerHTML = `<form class="auth-card">
      <h1>${escA(title)}</h1>${text ? `<p class="muted">${escA(text)}</p>` : ""}
      ${fields.map((f) => `<label>${escA(f.label)}<input name="${f.name}" type="${f.type || "text"}" autocomplete="${f.autocomplete || "off"}" ${f.required === false ? "" : "required"} value="${escA(f.value || "")}"></label>`).join("")}
      <div class="auth-err"></div>
      <div class="row"><button type="button" class="btn grow" data-cancel>Отмена</button><button class="send grow" type="submit">${escA(ok)}</button></div>
    </form>`;
    document.body.appendChild(back);
    const form = back.querySelector("form");
    const close = (v) => { back.remove(); resolve(v); };
    back.querySelector("[data-cancel]").addEventListener("click", () => close(null));
    back.addEventListener("keydown", (e) => { if (e.key === "Escape") close(null); });
    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(form));
      const check = fields.find((f) => f.check && f.check(data));
      if (check) { form.querySelector(".auth-err").textContent = check.check(data); return; }
      close(data);
    });
    form.querySelector("input")?.focus();
  });
}

// ---------- скачивание ----------
// Файл скачивается запросом: если сервер вернул ошибку — показываем её сообщением,
// а не открываем ответ вместо приложения. В окне на Mac сохраняем через системный диалог.
async function downloadFile(url) {
  let r;
  try { r = await rawFetch(url); } catch (e) { toast("Не удалось скачать: " + e.message, true); return; }
  if (!r.ok) {
    let msg = `${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* не JSON */ }
    if (r.status === 401) showLogin("Сессия завершилась — войдите снова.");
    toast(msg, true); return;
  }
  const cd = r.headers.get("content-disposition") || "";
  const m = cd.match(/filename\*=UTF-8''([^;]+)/i) || cd.match(/filename="?([^";]+)"?/i);
  const name = m ? decodeURIComponent(m[1]) : url.split("/").pop().split("?")[0];
  const blob = await r.blob();
  if (window.pywebview?.api?.save_file) {
    const b64 = await new Promise((res) => { const fr = new FileReader(); fr.onload = () => res(String(fr.result).split(",")[1]); fr.readAsDataURL(blob); });
    const path = await window.pywebview.api.save_file(name, b64);
    if (path) toast(`Сохранено: ${path}`);
    return;
  }
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = name; a.dataset.direct = "1";
  document.body.appendChild(a); a.click();
  // убираем ссылку не сразу: браузер берёт имя файла из неё уже после клика
  setTimeout(() => { a.remove(); URL.revokeObjectURL(a.href); }, 10000);
}
document.addEventListener("click", (e) => {
  const a = e.target.closest("a[download]");
  if (!a || a.dataset.direct) return;
  e.preventDefault();
  downloadFile(a.getAttribute("href"));
});

// ---------- вход ----------
let authMode = "login";
function showLogin(hint = "") {
  authMode = "login";
  $a("#authTitle").textContent = "Вход в DocSearch";
  $a("#authHint").textContent = hint;
  $a("#authNameRow").hidden = true; $a("#authPass2Row").hidden = true;
  $a("#authPass").autocomplete = "current-password";
  $a("#authBtn").textContent = "Войти";
  $a("#authErr").textContent = "";
  $a("#auth").hidden = false;
  ($a("#authUser").value ? $a("#authPass") : $a("#authUser")).focus();
}
function showSetup() {
  authMode = "setup";
  $a("#authTitle").textContent = "Первый запуск";
  $a("#authHint").textContent = "Создайте учётную запись администратора. Потом в разделе «Админ» можно добавить других пользователей.";
  $a("#authNameRow").hidden = false; $a("#authPass2Row").hidden = false;
  $a("#authPass").autocomplete = "new-password";
  $a("#authBtn").textContent = "Создать и войти";
  $a("#auth").hidden = false;
  $a("#authUser").focus();
}

$a("#authForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $a("#authErr"); err.textContent = "";
  const body = { username: $a("#authUser").value.trim(), password: $a("#authPass").value, full_name: $a("#authFullName").value };
  if (authMode === "setup" && body.password !== $a("#authPass2").value) { err.textContent = "Пароли не совпадают"; return; }
  $a("#authBtn").disabled = true;
  try {
    const r = await rawFetch(`/api/auth/${authMode === "setup" ? "setup" : "login"}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    if (!r.ok) { err.textContent = (await r.json().catch(() => ({}))).detail || `Ошибка ${r.status}`; return; }
    $a("#authPass").value = ""; $a("#authPass2").value = "";
    if (appStarted) { location.reload(); return; }   // сессия истекла посреди работы — начинаем заново
    await afterLogin();
  } finally { $a("#authBtn").disabled = false; }
});

async function afterLogin(st) {
  st = st || await (await rawFetch("/api/auth/status")).json();
  window.currentUser = st.user;
  $a("#auth").hidden = true;
  const u = st.user;
  $a("#userMenu").hidden = !st.enabled;
  $a("#adminTab").hidden = u?.role !== "admin";
  if (u) {
    $a("#userBtn").textContent = (u.full_name || u.username).split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
    $a("#userName").innerHTML = `<b>${escA(u.full_name || u.username)}</b><br><span class="muted">${escA(u.username)} · ${escA(u.role_name || u.role)}</span>`;
  }
  appStarted = true;
  authResolve();
}

$a("#userBtn").addEventListener("click", (e) => { e.stopPropagation(); $a("#userPop").hidden = !$a("#userPop").hidden; });
document.addEventListener("click", (e) => { if (!e.target.closest("#userMenu")) $a("#userPop").hidden = true; });
$a("#logout").addEventListener("click", async () => {
  await rawFetch("/api/auth/logout", { method: "POST" });
  location.reload();
});
$a("#changePass").addEventListener("click", async () => {
  $a("#userPop").hidden = true;
  const v = await dialog({
    title: "Смена пароля", ok: "Сменить",
    text: "После смены пароля вход на других устройствах завершится.",
    fields: [
      { name: "old_password", label: "Текущий пароль", type: "password", autocomplete: "current-password" },
      { name: "new_password", label: "Новый пароль (от 8 символов)", type: "password", autocomplete: "new-password" },
      { name: "repeat", label: "Повторите новый пароль", type: "password", autocomplete: "new-password",
        check: (d) => d.new_password !== d.repeat ? "Пароли не совпадают" : "" },
    ],
  });
  if (!v) return;
  const r = await rawFetch("/api/auth/password", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ old_password: v.old_password, new_password: v.new_password }) });
  r.ok ? toast("Пароль изменён") : toast((await r.json().catch(() => ({}))).detail || "Ошибка", true);
});

(async function startAuth() {
  let st;
  try { st = await (await rawFetch("/api/auth/status")).json(); }
  catch { authResolve(); return; }   // сервер не отвечает — app.js покажет это в статусе
  if (!st.enabled) return afterLogin(st);
  if (st.setup_needed) return showSetup();
  if (!st.user) return showLogin();
  afterLogin(st);
})();
