"""Вход по паролю, сессии, роли и журнал действий (кто, когда, что сделал).

- Пароли хранятся только как хеш scrypt с солью — восстановить пароль из файла нельзя.
- Сессия — подписанный (HMAC-SHA256) токен в cookie HttpOnly + SameSite=Strict: недоступен
  скриптам страницы и не уходит с чужих сайтов. После смены пароля или блокировки
  пользователя все его сессии перестают действовать.
- После 5 неудачных попыток вход для этого имени блокируется на 5 минут.
- Хранилище — файлы в config.AUTH_DIR, поэтому проверка работает в любом контейнере,
  которому подключена эта папка.
"""
from __future__ import annotations

import base64
import contextvars
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from . import config

log = logging.getLogger("auth")
COOKIE = "docsearch_session"
ROLES = {"admin": "администратор", "user": "пользователь"}
MAX_FAILS, LOCK_SECONDS = 5, 300

current_user: contextvars.ContextVar[dict | None] = contextvars.ContextVar("current_user", default=None)
current_ip: contextvars.ContextVar[str] = contextvars.ContextVar("current_ip", default="")
_lock = threading.Lock()
_fails: dict[str, list[float]] = {}


def _dir() -> Path:
    path = Path(config.AUTH_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_private(path: Path, data: str) -> None:
    """Атомарная запись с правами только для владельца."""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _secret() -> bytes:
    path = _dir() / "secret.key"
    with _lock:
        if not path.exists():
            _write_private(path, secrets.token_hex(32))
    return bytes.fromhex(path.read_text().strip())


# ---------- пользователи ----------

def load_users() -> dict[str, dict]:
    try:
        return json.loads((_dir() / "users.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_users(users: dict[str, dict]) -> None:
    with _lock:
        _write_private(_dir() / "users.json", json.dumps(users, ensure_ascii=False, indent=1))


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, _ = stored.split("$")
        return hmac.compare_digest(hash_password(password, bytes.fromhex(salt)), stored)
    except ValueError:
        return False


def check_new_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(400, "Пароль должен быть не короче 8 символов")
    if password.isdigit() or password.lower() == password and password.isalpha():
        raise HTTPException(400, "Пароль слишком простой: добавьте цифры, заглавные буквы или знаки")


def check_username(name: str) -> str:
    name = name.strip()
    if not re.fullmatch(r"[\w.@\-]{2,40}", name):
        raise HTTPException(400, "Имя пользователя: 2–40 символов, буквы, цифры, точка, дефис, @")
    return name


def public(name: str, u: dict) -> dict:
    return {"username": name, "role": u["role"], "role_name": ROLES.get(u["role"], u["role"]),
            "disabled": u.get("disabled", False), "created_at": u.get("created_at"), "last_login": u.get("last_login"),
            "full_name": u.get("full_name", "")}


# ---------- сессии ----------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _pv(u: dict) -> str:
    """«Версия пароля»: меняется при смене пароля — старые сессии становятся недействительными."""
    return hashlib.sha256(u["password"].encode()).hexdigest()[:12]


def make_token(name: str, u: dict) -> str:
    payload = _b64(json.dumps({"u": name, "pv": _pv(u), "exp": int(time.time()) + config.SESSION_HOURS * 3600,
                               "sid": secrets.token_hex(8)}).encode())
    sig = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{sig}"


def user_from_token(token: str | None) -> dict | None:
    if not token or "." not in token:
        return None
    payload, sig = token.rsplit(".", 1)
    good = _b64(hmac.new(_secret(), payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(sig, good):
        return None
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    u = load_users().get(data.get("u", ""))
    if not u or u.get("disabled") or data.get("exp", 0) < time.time() or data.get("pv") != _pv(u):
        return None
    return {"username": data["u"], "role": u["role"]}


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "")


# ---------- журнал ----------

def audit(action: str, detail: str = "", ok: bool = True, user: str | None = None) -> None:
    """Запись в журнал: кто (текущий пользователь), когда, что, с какого адреса, успешно ли."""
    u = current_user.get()
    entry = {"ts": _now(), "user": user or (u["username"] if u else ""), "action": action,
             "detail": detail[:500], "ip": current_ip.get(), "ok": ok}
    line = json.dumps(entry, ensure_ascii=False)
    try:
        with _lock, open(_dir() / "audit.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        log.exception("Не удалось записать журнал")
    log.info("audit %s", line)


def read_audit(limit: int = 500, user: str = "", action: str = "") -> list[dict]:
    try:
        lines = (_dir() / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if (user and e.get("user") != user) or (action and action.lower() not in e.get("action", "").lower()):
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


# ---------- защита сервисов ----------

def protect(app: FastAPI) -> None:
    """Все запросы к сервису — только с действующей сессией."""
    @app.middleware("http")
    async def _require_login(request: Request, call_next):
        current_ip.set(_client_ip(request))
        if not config.AUTH_ENABLED:
            current_user.set({"username": "dev", "role": "admin"})
            return await call_next(request)
        user = user_from_token(request.cookies.get(COOKIE))
        if not user:
            return JSONResponse({"detail": "Требуется вход в систему"}, status_code=401)
        current_user.set(user)
        request.state.user = user
        return await call_next(request)


def require_admin() -> dict:
    u = current_user.get()
    if not u or u["role"] != "admin":
        raise HTTPException(403, "Доступно только администратору")
    return u


# ---------- API входа ----------

app = FastAPI(title="Вход")


class Credentials(BaseModel):
    username: str
    password: str
    full_name: str = ""


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


def _set_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(COOKIE, token, max_age=config.SESSION_HOURS * 3600, httponly=True, samesite="strict", path="/")


def _me(request: Request) -> dict:
    current_ip.set(_client_ip(request))
    if not config.AUTH_ENABLED:
        return {"username": "dev", "role": "admin"}
    user = user_from_token(request.cookies.get(COOKIE))
    if not user:
        raise HTTPException(401, "Требуется вход в систему")
    current_user.set(user)
    return user


@app.get("/status")
async def status(request: Request):
    """Что показать на экране входа: нужно ли создать первого администратора, вошёл ли пользователь."""
    user = None
    try:
        user = _me(request)
    except HTTPException:
        pass
    users = load_users()
    if user and user["username"] in users:
        user = {**user, **public(user["username"], users[user["username"]])}
    return {"enabled": config.AUTH_ENABLED, "setup_needed": config.AUTH_ENABLED and not users, "user": user,
            "roles": ROLES}


@app.post("/setup")
async def setup(req: Credentials, request: Request):
    """Первый запуск: создание администратора. Работает, только пока пользователей нет."""
    current_ip.set(_client_ip(request))
    with _lock:
        if load_users():
            raise HTTPException(409, "Администратор уже создан — войдите")
    name = check_username(req.username)
    check_new_password(req.password)
    users = {name: {"password": hash_password(req.password), "role": "admin", "created_at": _now(), "last_login": _now(),
                    "full_name": req.full_name.strip()[:80]}}
    save_users(users)
    audit("Создан первый администратор", name, user=name)
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, make_token(name, users[name]))
    return resp


@app.post("/login")
async def login(req: Credentials, request: Request):
    current_ip.set(_client_ip(request))
    name = req.username.strip()
    now = time.time()
    recent = [t for t in _fails.get(name.lower(), []) if now - t < LOCK_SECONDS]
    if len(recent) >= MAX_FAILS:
        audit("Вход заблокирован", f"{name}: слишком много попыток", ok=False, user=name)
        raise HTTPException(429, f"Слишком много неудачных попыток. Попробуйте через {LOCK_SECONDS // 60} минут.")
    users = load_users()
    u = users.get(name)
    if not u or not verify_password(req.password, u["password"]) or u.get("disabled"):
        _fails[name.lower()] = recent + [now]
        audit("Неудачный вход", "пользователь заблокирован" if u and u.get("disabled") else "неверное имя или пароль",
              ok=False, user=name)
        raise HTTPException(401, "Неверное имя пользователя или пароль")
    _fails.pop(name.lower(), None)
    u["last_login"] = _now()
    save_users(users)
    audit("Вход", "", user=name)
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, make_token(name, u))
    return resp


@app.post("/logout")
async def logout(request: Request):
    try:
        _me(request)
        audit("Выход")
    except HTTPException:
        pass
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@app.post("/password")
async def change_password(req: PasswordChange, user: dict = Depends(_me)):
    users = load_users()
    u = users.get(user["username"])
    if not u or not verify_password(req.old_password, u["password"]):
        audit("Смена пароля", "неверный текущий пароль", ok=False)
        raise HTTPException(400, "Текущий пароль указан неверно")
    check_new_password(req.new_password)
    u["password"] = hash_password(req.new_password)
    save_users(users)
    audit("Смена пароля")
    resp = JSONResponse({"ok": True})
    _set_cookie(resp, make_token(user["username"], u))   # текущая сессия остаётся, остальные завершаются
    return resp
