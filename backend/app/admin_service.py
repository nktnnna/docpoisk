"""Раздел администратора: пользователи, журнал действий, логи сервисов и консоль.

Консоль — команды, которые напрямую меняют данные приложения: исправить неверно определённые
реквизиты, связь, риск в реестре, переиндексировать или удалить документ, проверить поиск.
Если ввести фразу обычным языком, нейросеть предложит подходящую команду, но не выполнит её сама.
"""
from __future__ import annotations

import io
import os
import json
import logging
import re
import shlex
import socket
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
import httpx
from fastapi.responses import Response
from pydantic import BaseModel

from . import analysis_service as an
from . import auth, config, ingest_service, legal, ollama, store

log = logging.getLogger("admin")
app = FastAPI(title="Администрирование")
auth.protect(app)
admin_only = [Depends(auth.require_admin)]


# ---------- пользователи ----------

class NewUser(BaseModel):
    username: str
    password: str
    role: str = "user"
    full_name: str = ""


class UserUpdate(BaseModel):
    role: str | None = None
    disabled: bool | None = None
    password: str | None = None
    full_name: str | None = None


@app.get("/users", dependencies=admin_only)
async def users():
    return [auth.public(n, u) for n, u in sorted(auth.load_users().items())]


@app.post("/users", dependencies=admin_only)
async def create_user(req: NewUser):
    name = auth.check_username(req.username)
    auth.check_new_password(req.password)
    if req.role not in auth.ROLES:
        raise HTTPException(400, "Неизвестная роль")
    data = auth.load_users()
    if name in data:
        raise HTTPException(409, "Такой пользователь уже есть")
    data[name] = {"password": auth.hash_password(req.password), "role": req.role,
                  "created_at": auth._now(), "full_name": req.full_name.strip()[:80]}
    auth.save_users(data)
    auth.audit("Создан пользователь", f"{name} ({auth.ROLES[req.role]})")
    return auth.public(name, data[name])


def _admins_left(data: dict, without: str) -> int:
    return sum(1 for n, u in data.items() if n != without and u["role"] == "admin" and not u.get("disabled"))


@app.patch("/users/{name}", dependencies=admin_only)
async def update_user(name: str, req: UserUpdate):
    data = auth.load_users()
    u = data.get(name)
    if not u:
        raise HTTPException(404, "Пользователь не найден")
    changes = []
    demote = (req.role and req.role != "admin") or req.disabled
    if demote and u["role"] == "admin" and not _admins_left(data, name):
        raise HTTPException(400, "Нельзя убрать последнего администратора")
    if req.role is not None:
        if req.role not in auth.ROLES:
            raise HTTPException(400, "Неизвестная роль")
        u["role"] = req.role
        changes.append(f"роль: {auth.ROLES[req.role]}")
    if req.disabled is not None:
        u["disabled"] = req.disabled
        changes.append("заблокирован" if req.disabled else "разблокирован")
    if req.password:
        auth.check_new_password(req.password)
        u["password"] = auth.hash_password(req.password)
        changes.append("пароль сброшен")
    if req.full_name is not None:
        u["full_name"] = req.full_name.strip()[:80]
        changes.append("ФИО")
    auth.save_users(data)
    auth.audit("Изменён пользователь", f"{name}: {', '.join(changes)}")
    return auth.public(name, u)


@app.delete("/users/{name}", dependencies=admin_only)
async def delete_user(name: str):
    data = auth.load_users()
    if name not in data:
        raise HTTPException(404, "Пользователь не найден")
    if name == auth.current_user.get()["username"]:
        raise HTTPException(400, "Нельзя удалить самого себя")
    if data[name]["role"] == "admin" and not _admins_left(data, name):
        raise HTTPException(400, "Нельзя удалить последнего администратора")
    del data[name]
    auth.save_users(data)
    auth.audit("Удалён пользователь", name)
    return {"ok": True}


# ---------- сервисы (контейнеры) ----------

@app.get("/services", dependencies=admin_only)
async def services():
    """Из чего состоит приложение и всё ли работает.

    Настольная версия — один процесс: все части внутри приложения, контейнеров нет.
    Docker-версия — каждая часть в своём контейнере; здесь видно, отвечает ли каждый.
    """
    docker = bool(config.QDRANT_URL)
    here = socket.gethostname()
    logs = {p.name.split(".")[0] for p in _log_files()}

    async def probe(url: str) -> tuple[bool, str]:
        try:
            async with httpx.AsyncClient(timeout=3) as c:
                r = await c.get(url)
            return r.status_code in (200, 401), f"HTTP {r.status_code}"   # 401 — жив, просто требует вход
        except httpx.HTTPError as e:
            return False, f"не отвечает: {e.__class__.__name__}"

    items = []
    if docker:
        ok, info = await probe(os.getenv("WEB_URL", "http://web/"))
        items.append({"name": "web", "role": "Интерфейс (nginx)", "ok": ok, "info": info, "where": "контейнер web"})
    else:
        items.append({"name": "web", "role": "Интерфейс", "ok": True, "info": "окно приложения", "where": "внутри приложения"})
    items.append({"name": "ingest", "role": "Вход, загрузка и индексация, анализ, администрирование", "ok": True,
                  "info": "этот сервис отвечает на запрос", "where": f"контейнер ingest ({here})" if docker else "внутри приложения"})
    if docker:
        ok, info = await probe(os.getenv("CHAT_URL", "http://chat:8000") + "/health")
        items.append({"name": "chat", "role": "Вопросы и ответы", "ok": ok, "info": info, "where": "контейнер chat"})
    else:
        items.append({"name": "chat", "role": "Вопросы и ответы", "ok": True, "info": "работает", "where": "внутри приложения"})
    try:
        n = await store.count()
        items.append({"name": "qdrant", "role": "Векторная база", "ok": True, "info": f"фрагментов: {n}",
                      "where": "контейнер qdrant" if docker else f"файлы в {config.QDRANT_PATH}"})
    except Exception as e:
        items.append({"name": "qdrant", "role": "Векторная база", "ok": False, "info": str(e)[:200],
                      "where": "контейнер qdrant" if docker else config.QDRANT_PATH})
    st = await ollama.status()
    models = "" if "error" in st else " · ".join(f"{m['name']} {'✓' if m['ready'] else '✗ не скачана'}" for m in (st["llm"], st["embed"]))
    items.append({"name": "ollama", "role": "Нейросети", "ok": bool(st.get("ok")),
                  "info": st.get("error") or models, "where": "контейнер ollama" if "//ollama:" in config.OLLAMA_URL
                  else f"программа Ollama на компьютере ({config.OLLAMA_URL})"})
    for it in items:
        it["log"] = it["name"] if it["name"] in logs else ("desktop" if not docker and "desktop" in logs else None)
    return {"mode": "docker" if docker else "desktop", "services": items}


# ---------- журнал и логи ----------

@app.get("/audit", dependencies=admin_only)
async def audit_log(limit: int = 500, user: str = "", action: str = ""):
    return auth.read_audit(min(limit, 5000), user, action)


def _log_files() -> list[Path]:
    root = Path(config.LOG_DIR)
    return sorted(p for p in root.glob("*.log*") if p.is_file()) if root.is_dir() else []


@app.get("/logs", dependencies=admin_only)
async def logs():
    return [{"name": p.name, "size": p.stat().st_size,
             "modified": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")}
            for p in _log_files()]


def _tail(path: Path, lines: int) -> str:
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 400 * lines))
        data = f.read().decode("utf-8", errors="replace")
    return "\n".join(data.splitlines()[-lines:])


@app.get("/logs/{name}", dependencies=admin_only)
async def log_tail(name: str, lines: int = 300):
    path = next((p for p in _log_files() if p.name == name), None)
    if not path:
        raise HTTPException(404, "Файл лога не найден")
    return {"name": name, "text": _tail(path, min(lines, 5000))}


@app.get("/logs.zip", dependencies=admin_only)
async def logs_zip():
    """Все логи сервисов + журнал входов и действий + пользователи (без паролей) — одним архивом."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in _log_files():
            z.write(p, f"logs/{p.name}")
        audit_file = Path(config.AUTH_DIR) / "audit.jsonl"
        if audit_file.exists():
            z.write(audit_file, "audit.jsonl")
        z.writestr("users.json", json.dumps([auth.public(n, u) for n, u in auth.load_users().items()],
                                            ensure_ascii=False, indent=1))
        z.writestr("about.txt", f"DocSearch, выгрузка {datetime.now():%d.%m.%Y %H:%M}\n"
                                f"LLM: {config.LLM_MODEL}\nЭмбеддинги: {config.EMBED_MODEL}\n")
    auth.audit("Выгрузка логов")
    return an._download(buf.getvalue(), f"DocSearch логи {datetime.now():%Y-%m-%d %H-%M}.zip", "application/zip")


# ---------- консоль ----------

HELP = """Команды (id документа можно сокращать до первых 4+ символов):

  docs [текст]                  список документов (id, тип, реквизиты, имя)
  doc <id>                      сведения о документе
  meta <id>                     реквизиты документа
  meta <id> set поле=значение…  исправить реквизиты: type=contract|amendment|letter|claim|act|protocol|invoice|other
                                number=15/2024  date=01.02.2024  title="Договор подряда"
  meta <id> reset               вернуть автоматическое определение
  links [id]                    связи документов
  link <id> <id_договора>       привязать документ к договору
  unlink <id> <id_договора>     убрать связь
  risks <id>                    реестр рисков документа с номерами строк
  risk <id> <№> set поле=…      исправить риск: level=высокий|средний|низкий risk="…" recommendation="…" clause=5.1 for_whom=…
  risk <id> <№> del             удалить риск (ложное срабатывание)
  risk <id> add clause=… level=… risk="…" [recommendation="…"]   добавить риск вручную
  clauses <id>                  как разобраны пункты документа
  search <запрос>               какие фрагменты находит поиск (проверка качества поиска)
  cons <id>                     пересобрать сводную редакция договора
  reindex <id|outdated|all>     переиндексировать
  delete <id>                   удалить документ из базы
  jobs                          очередь индексации
  stats                         сводка по базе
  users                         пользователи
  passwd <пользователь> <пароль> задать новый пароль (если забыли — через терминал: python -m app.console)
  audit [N] [пользователь]      последние записи журнала
  config                        текущие настройки
  help                          эта справка

Можно писать и обычным языком — нейросеть предложит подходящую команду."""


def _models(st: dict) -> str:
    if "error" in st:
        return st["error"]
    return ", ".join(f"{m['name']} — {'готова' if m['ready'] else 'не скачана'}" for m in (st["llm"], st["embed"]))


class ConsoleError(Exception):
    pass


async def _find(prefix: str) -> dict:
    prefix = prefix.lower()
    docs = await store.list_docs()
    hits = [d for d in docs if d["doc_id"].startswith(prefix)] if len(prefix) >= 4 else []
    if not hits:
        hits = [d for d in docs if prefix in d["filename"].lower()] if len(prefix) >= 3 else []
    if not hits:
        raise ConsoleError(f"Документ «{prefix}» не найден. Список: docs")
    if len(hits) > 1:
        raise ConsoleError("Подходит несколько документов, уточните:\n" +
                           "\n".join(f"  {d['doc_id']}  {d['filename']}" for d in hits[:10]))
    return hits[0]


def _kv(args: list[str]) -> dict[str, str]:
    out = {}
    for a in args:
        if "=" not in a:
            raise ConsoleError(f"Ожидалось поле=значение, а получено «{a}»")
        k, v = a.split("=", 1)
        out[k.strip().lower()] = v.strip()
    return out


def _req(m: dict) -> str:
    return " ".join(x for x in [f"№ {m['number']}" if m.get("number") else "",
                                f"от {legal.ru_date(m['date'])}" if m.get("date") else ""] if x)


async def _graph_docs() -> tuple[dict, dict]:
    g = await an.graph()
    return g, {d["doc_id"]: d for d in g["docs"]}


async def run_command(line: str) -> str:
    try:
        parts = shlex.split(line)
    except ValueError as e:
        raise ConsoleError(f"Не разобрать строку: {e}")
    if not parts:
        return ""
    cmd, args = parts[0].lower(), parts[1:]

    if cmd in ("help", "?", "помощь"):
        return HELP

    if cmd == "docs":
        g, _ = await _graph_docs()
        needle = " ".join(args).lower()
        rows = [d for d in g["docs"] if not needle or needle in d["filename"].lower() or needle in (d.get("title") or "").lower()]
        return "\n".join(f"{d['doc_id']}  {d['type_name']:<18} {_req(d):<26} {d['filename']}" for d in rows) or "Документов нет"

    if cmd == "doc" and args:
        d = await _find(args[0])
        _, docs = await _graph_docs()
        m, info = docs[d["doc_id"]], ingest_service._describe(d)
        return (f"{d['filename']}\n  id: {d['doc_id']}\n  тип: {m['type_name']}  {_req(m)}\n  заголовок: {m.get('title')}\n"
                f"  стороны: {', '.join((p['role'] + ': ' if p['role'] else '') + p['name'] for p in m['parties']) or '—'}\n"
                f"  размер: {info['size']} байт, фрагментов: {d['chunks']}, загружен: {d['uploaded_at']}\n"
                f"  индекс: {'устаревший' if d.get('outdated') else 'актуальный'}"
                + (f"\n  исправлено вручную: {', '.join(m['overridden'])}" if m.get("overridden") else ""))

    if cmd == "meta" and args:
        d = await _find(args[0])
        if len(args) >= 2 and args[1] == "reset":
            an.set_override(d["doc_id"], None)
            auth.audit("Консоль: реквизиты сброшены", d["filename"])
            return "Реквизиты снова определяются автоматически."
        if len(args) >= 3 and args[1] == "set":
            fields = _kv(args[2:])
            bad = set(fields) - an.META_FIELDS
            if bad:
                raise ConsoleError(f"Нельзя менять: {', '.join(bad)}. Можно: {', '.join(sorted(an.META_FIELDS))}")
            if "type" in fields and fields["type"] not in legal.TYPES:
                raise ConsoleError(f"Тип: {', '.join(legal.TYPES)}")
            if "date" in fields:
                iso = legal.parse_date(fields["date"]) or (fields["date"] if re.fullmatch(r"\d{4}-\d\d-\d\d", fields["date"]) else None)
                if not iso:
                    raise ConsoleError("Дата: ДД.ММ.ГГГГ")
                fields["date"] = iso
            an.set_override(d["doc_id"], fields)
            auth.audit("Консоль: исправлены реквизиты", f"{d['filename']}: {fields}")
            args = args[:1]
        m = await an.meta(d)
        return json.dumps({k: m.get(k) for k in ("type", "type_name", "number", "date", "title", "parties", "refs",
                                                  "subject", "sender", "recipient", "letter_refs", "overridden")
                           if m.get(k) not in (None, "", [])}, ensure_ascii=False, indent=1)

    if cmd == "links":
        g, docs = await _graph_docs()
        only = (await _find(args[0]))["doc_id"] if args else None
        rows = [l for l in g["links"] if not only or only in (l["from"], l["to"])]
        return "\n".join(f"{docs[l['from']]['filename']} —{l['relation']}→ {docs[l['to']]['filename']}"
                         f"  ({'авто: ' + l['evidence'] if l['auto'] else 'вручную'})" for l in rows) or "Связей нет"

    if cmd in ("link", "unlink") and len(args) == 2:
        a, b = await _find(args[0]), await _find(args[1])
        if cmd == "link":
            await an.add_link(an.LinkReq(from_id=a["doc_id"], to_id=b["doc_id"]))
        else:
            await an.remove_link(an.LinkReq(from_id=a["doc_id"], to_id=b["doc_id"]))
        return f"Готово: {a['filename']} {'→' if cmd == 'link' else '✕'} {b['filename']}"

    if cmd == "risks" and args:
        d = await _find(args[0])
        reg = next(iter(an._registers(d["doc_id"])), None)
        if not reg:
            return "Анализ рисков для документа ещё не проводился."
        return "\n".join(f"{i:>3}. [{r['level']}] п. {r['clause']}: {r['risk']}  ({r['source']})"
                         for i, r in enumerate(reg["risks"], start=1)) or "Рисков нет"

    if cmd == "risk" and len(args) >= 2:
        d = await _find(args[0])
        reg = next(iter(an._registers(d["doc_id"])), None)
        if not reg:
            raise ConsoleError("Анализ рисков для документа ещё не проводился")
        who = auth.current_user.get()["username"]
        if args[1] == "add":
            f = _kv(args[2:])
            if not f.get("risk"):
                raise ConsoleError('Нужно хотя бы risk="…"')
            level = f.get("level", "средний")
            if level not in ("высокий", "средний", "низкий"):
                raise ConsoleError("level: высокий | средний | низкий")
            reg["risks"].append({"clause": f.get("clause", "—"), "quote": "", "risk": f["risk"], "level": level,
                                 "for_whom": f.get("for_whom", ""), "recommendation": f.get("recommendation", ""),
                                 "source": f"добавлено вручную ({who})"})
            an.save_register(reg)
            auth.audit("Консоль: риск добавлен", f"{d['filename']}: {f['risk']}")
            return f"Добавлено. Теперь рисков: {len(reg['risks'])}"
        try:
            n = int(args[1])
            r = reg["risks"][n - 1]
        except (ValueError, IndexError):
            raise ConsoleError("Номер риска — из команды risks <id>")
        if len(args) >= 3 and args[2] == "del":
            reg["risks"].pop(n - 1)
            an.save_register(reg)
            auth.audit("Консоль: риск удалён", f"{d['filename']}: {r['risk']}")
            return f"Удалён риск {n}: {r['risk']}"
        if len(args) >= 4 and args[2] == "set":
            f = _kv(args[3:])
            allowed = {"level", "risk", "recommendation", "clause", "for_whom"}
            if set(f) - allowed:
                raise ConsoleError(f"Можно менять: {', '.join(sorted(allowed))}")
            if "level" in f and f["level"] not in ("высокий", "средний", "низкий"):
                raise ConsoleError("level: высокий | средний | низкий")
            r.update(f)
            r["source"] = f"{r['source'].split(' (исправлено')[0]} (исправлено: {who})"
            an.save_register(reg)
            auth.audit("Консоль: риск исправлен", f"{d['filename']} №{n}: {f}")
            return f"Исправлено: [{r['level']}] п. {r['clause']}: {r['risk']}"
        raise ConsoleError("risk <id> <№> set поле=… | risk <id> <№> del | risk <id> add …")

    if cmd == "clauses" and args:
        d = await _find(args[0])
        cl = legal.parse_clauses(await an.doc_text(d))
        return "\n".join(f"{c.num or '—':>8}  {c.text[:110].replace(chr(10), ' ')}" for c in cl)

    if cmd == "search" and args:
        from .chat_service import _retrieve

        passages = await _retrieve(" ".join(args), None, None)
        return "\n\n".join(f"[{i}] {p['filename']}, стр. {p.get('page') or '—'}, {p.get('section') or ''}"
                           f" (близость {p['score'] if p['score'] is not None else '—'})\n{p['text'][:300]}"
                           for i, p in enumerate(passages, start=1)) or "Ничего не найдено"

    if cmd == "cons" and args:
        d = await _find(args[0])
        try:
            res = await an.build_consolidated(d["doc_id"])
        except HTTPException as e:
            raise ConsoleError(e.detail)
        auth.audit("Консоль: сводная редакция", d["filename"])
        return (f"Применено правок: {sum(o['applied'] for o in res['ops'])} из {len(res['ops']) + len(res['unparsed'])}; "
                f"изменено {res['counts']['changed']}, добавлено {res['counts']['added']}, исключено {res['counts']['deleted']}"
                + "".join(f"\n  ! {o['source']}: {o['error']}" for o in res["ops"] if o["error"])
                + "".join(f"\n  ✗ {u['source']}: {u['text'][:150]}" for u in res["unparsed"]))

    if cmd == "reindex" and args:
        if args[0] in ("all", "outdated"):
            res = await ingest_service.reindex(only_outdated=args[0] == "outdated")
        else:
            res = await ingest_service.reindex(doc_id=(await _find(args[0]))["doc_id"])
        return f"В очередь поставлено: {len(res['queued'])}. Ход — команда jobs."

    if cmd == "delete" and args:
        d = await _find(args[0])
        await ingest_service.delete_document(d["doc_id"])
        return f"Удалён: {d['filename']}"

    if cmd == "jobs":
        jobs = await ingest_service.list_jobs()
        return "\n".join(f"{j['status']:<10} {j['filename']}" + (f"  {j['error']}" if j.get("error") else "")
                         for j in jobs[:30]) or "Очередь пуста"

    if cmd == "stats":
        docs = await store.list_docs()
        regs = an._registers()
        return (f"Документов: {len(docs)}, фрагментов: {sum(d['chunks'] for d in docs)}, "
                f"устаревший индекс: {sum(1 for d in docs if d.get('outdated'))}\n"
                f"Реестров рисков: {len(regs)}, рисков: {sum(len(r['risks']) for r in regs)}\n"
                f"Пользователей: {len(auth.load_users())}\nМодели: {_models(await ollama.status())}")

    if cmd == "passwd" and len(args) == 2:
        try:
            await update_user(args[0], UserUpdate(password=args[1], disabled=False))
        except HTTPException as e:
            raise ConsoleError(e.detail)
        auth._fails.pop(args[0].lower(), None)
        return f"Пароль пользователя {args[0]} изменён, блокировка снята."

    if cmd == "users":
        return "\n".join(f"{u['username']:<20} {u['role_name']:<15} {'ЗАБЛОКИРОВАН ' if u['disabled'] else ''}"
                         f"последний вход: {u.get('last_login') or '—'}" for u in await users())

    if cmd == "audit":
        n = int(args[0]) if args and args[0].isdigit() else 30
        who = next((a for a in args if not a.isdigit()), "")
        return "\n".join(f"{e['ts']}  {e['user'] or '—':<15} {'✓' if e['ok'] else '✗'} {e['action']}: {e['detail']}  {e['ip']}"
                         for e in auth.read_audit(n, who)) or "Журнал пуст"

    if cmd == "config":
        keys = ["LLM_MODEL", "EMBED_MODEL", "OLLAMA_URL", "QDRANT_URL", "DATA_DIR", "STORAGE_DIR", "AUTH_DIR", "LOG_DIR",
                "CHUNK_SIZE", "CHUNK_OVERLAP", "TOP_K", "MIN_SCORE", "CONTEXT_CHARS", "LLM_NUM_CTX", "SESSION_HOURS"]
        return "\n".join(f"{k} = {getattr(config, k)}" for k in keys)

    raise ConsoleError(f"__unknown__{cmd}")


SUGGEST_PROMPT = """Ты помощник администратора программы DocSearch. Подбери ОДНУ команду консоли под просьбу администратора.
Список команд:
{help}

Документы (id, тип, имя):
{docs}

Ответь JSON: {{"command": "команда целиком", "explain": "что она сделает, одной фразой"}}.
Если подходящей команды нет — {{"command": "", "explain": "почему"}}. Используй только id из списка."""


class ConsoleReq(BaseModel):
    command: str


@app.post("/console", dependencies=admin_only)
async def console(req: ConsoleReq):
    line = req.command.strip()
    if not line:
        return {"output": ""}
    try:
        return {"output": await run_command(line), "ok": True}   # изменения пишут в журнал сами команды
    except ConsoleError as e:
        if not str(e).startswith("__unknown__"):
            return {"output": f"Ошибка: {e}", "ok": False}
    except HTTPException as e:
        return {"output": f"Ошибка: {e.detail}", "ok": False}
    except Exception as e:
        log.exception("Ошибка консоли")
        return {"output": f"Ошибка: {e}", "ok": False}

    # не команда — просим нейросеть подсказать команду (выполнит её только сам администратор)
    docs = await store.list_docs()
    try:
        _, info = await _graph_docs()
        doc_lines = "\n".join(f"{d['doc_id']}  {info[d['doc_id']]['type_name']}  {d['filename']}" for d in docs[:80])
        raw = await ollama.chat([{"role": "system", "content": SUGGEST_PROMPT.format(help=HELP, docs=doc_lines or "—")},
                                 {"role": "user", "content": line}], max_tokens=200, fmt="json")
        data = json.loads(raw)
        cmd = str(data.get("command") or "").strip()
        if cmd and cmd.split()[0].lower() in HELP:
            return {"output": f"Похоже, подойдёт команда:\n  {cmd}\n{data.get('explain', '')}", "suggest": cmd, "ok": True}
        return {"output": f"Не нашёл подходящей команды. {data.get('explain', '')}\nСписок команд: help", "ok": False}
    except Exception:
        return {"output": f"Неизвестная команда «{line.split()[0]}». Список команд: help", "ok": False}
