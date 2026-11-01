"""Сервис индексации: принимает файлы → текст → чанки → эмбеддинги → Qdrant.

Запускается отдельным контейнером `ingest`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from . import auth, config, legal, mail, ollama, store
from .chunking import chunk_pages, embed_text, headings, normalize
from .parsing import PARSERS, SUPPORTED, extract_pages, unsupported_reason

log = logging.getLogger("ingest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="Индексация документов")
auth.protect(app)

# Задачи индексации (в памяти: после перезапуска история очищается, сами документы остаются в базе)
jobs: dict[str, dict] = {}
# В очереди — путь к файлу на диске, а не байты: 3000 писем не держим в памяти
_queue: asyncio.Queue[tuple[str, str, Path, bool]] | None = None
_links_lock = asyncio.Lock()
_worker_task: asyncio.Task | None = None


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\- ()\[\]]+", "_", name, flags=re.UNICODE) or "file"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _incoming() -> Path:
    path = Path(config.STORAGE_DIR) / "_incoming"
    path.mkdir(parents=True, exist_ok=True)
    return path


async def _previous(doc_id: str) -> dict | None:
    """Первый фрагмент этого документа, если он уже был загружен."""
    return (await store.get_chunks([(doc_id, 0)])).get((doc_id, 0))


async def _index(job_id: str, filename: str, data: bytes) -> None:
    job = jobs[job_id]
    doc_id = hashlib.sha256(data).hexdigest()[:16]
    job.update(status="parsing", doc_id=doc_id)
    previous = await _previous(doc_id)
    # тот же файл уже в базе текущей версией индекса — повторная загрузка той же выгрузки почты
    # не тратит время на эмбеддинги
    if (not job.get("force") and previous and previous.get("v") == store.INDEX_VERSION
            and (Path(config.STORAGE_DIR) / doc_id / filename).exists()):
        job["status"] = "skipped"
        return
    pages = await asyncio.to_thread(extract_pages, filename, data)
    if not pages:
        raise ValueError(
            "В файле не найден текст. Если это скан — сначала распознайте его (OCR), "
            "например, в ABBYY FineReader или через «Сохранить как PDF с текстом»."
        )
    chunks = chunk_pages(pages, config.CHUNK_SIZE, config.CHUNK_OVERLAP)
    # реквизиты документа — в каждый фрагмент: модель должна видеть, что перед ней
    # «Доп. соглашение № 1 от 20.05.2024 к договору № 15/2024», а не только имя файла
    meta = await asyncio.to_thread(legal.analyze_meta, "\n".join(normalize(t) for _, t in pages))
    doc = {"type": meta["type"], "label": legal.doc_label(meta), "number": meta.get("number"),
           "ref": next((r["num"] for r in meta.get("refs") or []), None)}
    job.update(status="embedding", chunks=len(chunks), done=0)

    vectors: list[list[float]] = []
    step = config.EMBED_BATCH
    for i in range(0, len(chunks), step):
        vectors.extend(await ollama.embed([embed_text(filename, c.section, c.text) for c in chunks[i : i + step]]))
        job["done"] = len(vectors)

    job["status"] = "saving"
    uploaded = _now()
    await store.ensure_collection(len(vectors[0]))
    if previous:
        # повторная загрузка заменяет старую версию; для нового документа удалять нечего, а удаление
        # по фильтру во встроенной базе просматривает всю коллекцию — на тысячах писем это заметно
        await store.delete_doc(doc_id)
    await store.upsert(doc_id, filename, chunks, vectors, uploaded, doc)

    dest = Path(config.STORAGE_DIR) / doc_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / filename).write_bytes(data)
    # реквизиты считаются один раз здесь — «База знаний» и связи не перечитывают тысячи файлов
    (dest / "meta.json").write_text(json.dumps({**meta, "v": legal.META_VERSION, "uploaded_at": uploaded},
                                               ensure_ascii=False), encoding="utf-8")


async def _index_patiently(job_id: str, filename: str, data: bytes) -> None:
    """Если нейросеть недоступна (Ollama не запущена или перезапускается), задание ждёт и повторяется,
    а не падает: иначе очередь из тысяч писем за минуту превращалась в тысячи ошибок."""
    delay = 5
    while True:
        try:
            return await _index(job_id, filename, data)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError):
            jobs[job_id]["status"] = "waiting"
            log.warning("Ollama недоступна — жду %s с и повторяю %s", delay, filename)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def _worker() -> None:
    assert _queue is not None
    # Один воркер: файлы индексируются по очереди, чтобы не перегружать ноутбук
    while True:
        job_id, filename, path, temporary = await _queue.get()
        try:
            await _index_patiently(job_id, filename, await asyncio.to_thread(path.read_bytes))
            if jobs[job_id]["status"] != "skipped":
                jobs[job_id]["status"] = "done"
            jobs[job_id]["finished_at"] = _now()
            log.info("Проиндексирован %s (%s чанков)", filename, jobs[job_id].get("chunks"))
        except Exception as e:
            log.exception("Ошибка индексации %s", filename)
            jobs[job_id].update(status="error", error=str(e), finished_at=_now())
        finally:
            if temporary:
                path.unlink(missing_ok=True)
            _queue.task_done()


def _enqueue_path(filename: str, path: Path, temporary: bool, force: bool = False) -> dict:
    filename = _safe_name(filename)
    job_id = hashlib.sha1(f"{filename}{_now()}{len(jobs)}".encode()).hexdigest()[:12]
    jobs[job_id] = {"job_id": job_id, "filename": filename, "status": "queued", "created_at": _now(), "force": force}
    _ensure_worker()
    _queue.put_nowait((job_id, filename, path, temporary))
    return jobs[job_id]


def _enqueue(filename: str, data: bytes, force: bool = True) -> dict:
    """Поставить в очередь файл из памяти (сводная редакция, переиндексация — заново, даже если уже в базе)."""
    tmp = _incoming() / f"{hashlib.sha1(data).hexdigest()[:12]}_{_safe_name(filename)}"
    tmp.write_bytes(data)
    return _enqueue_path(filename, tmp, temporary=True, force=force)


def _expand(filename: str, path: Path, temporary: bool, depth: int = 0) -> tuple[list[tuple], list[dict], list]:
    """Файл → что индексировать. Архив и почтовый ящик раскладываются на отдельные файлы,
    у письма .eml/.msg вложения становятся отдельными документами, привязанными к письму.

    Работает в отдельном потоке (распаковка тысяч писем не должна тормозить приложение), поэтому
    сам в очередь ничего не ставит: возвращает (файлы [(имя, путь, временный)], отклонённые,
    пары «вложение → письмо» по doc_id), а в очередь их ставит основной цикл — см. _enqueue_all."""
    ext = Path(filename).suffix.lower()
    items, rejected, attach = [], [], []
    try:
        if ext in (".zip", ".mbox") and depth < 3:
            entries = mail.unzip(str(path)) if ext == ".zip" else \
                [(f"{Path(filename).stem}_{i:05d}.eml", raw) for i, raw in enumerate(mail.split_mbox(str(path)), start=1)]
            if temporary:
                path.unlink(missing_ok=True)
            for name, raw in entries:
                reason = unsupported_reason(name)
                if reason:
                    rejected.append({"filename": name, "error": reason})
                    continue
                tmp = _incoming() / f"{hashlib.sha1(raw).hexdigest()[:12]}_{_safe_name(name)}"
                tmp.write_bytes(raw)
                i, r, a = _expand(name, tmp, True, depth + 1)
                items += i
                rejected += r
                attach += a
            return items, rejected, attach
        items.append((filename, path, temporary))
        if ext in (".eml", ".msg"):
            raw = path.read_bytes()
            try:
                _, atts = mail.parse_eml(raw) if ext == ".eml" else mail.parse_msg(raw)
            except Exception:
                atts = []
            letter_id = hashlib.sha256(raw).hexdigest()[:16]
            for name, data in atts:
                if Path(name).suffix.lower() in PARSERS and Path(name).suffix.lower() not in (".eml", ".msg"):
                    att_name = f"{Path(filename).stem} — {name}"
                    tmp = _incoming() / f"{hashlib.sha1(data).hexdigest()[:12]}_{_safe_name(att_name)}"
                    tmp.write_bytes(data)
                    items.append((att_name, tmp, True))
                    attach.append([hashlib.sha256(data).hexdigest()[:16], letter_id])
    except Exception as e:
        log.exception("Не удалось разобрать %s", filename)
        rejected.append({"filename": filename, "error": f"не удалось открыть: {e}"})
    return items, rejected, attach


async def _enqueue_all(filename: str, path: Path, temporary: bool) -> tuple[list[dict], list[dict]]:
    items, rejected, attach = await asyncio.to_thread(_expand, filename, path, temporary)
    await _remember_attachments(attach)
    return [_enqueue_path(n, p, t) for n, p, t in items], rejected


async def _remember_attachments(pairs: list) -> None:
    """Связи «вложение → письмо» — в тот же файл, где ручные связи (их читает раздел анализа)."""
    if not pairs:
        return
    async with _links_lock:
        path = Path(config.STORAGE_DIR) / "links.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        known = {tuple(x) for x in data.get("attach", [])}
        data["attach"] = data.get("attach", []) + [p for p in pairs if tuple(p) not in known]
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _ensure_worker() -> None:
    # Воркер запускается лениво: так сервис работает и сам по себе, и вмонтированным в app.main
    global _queue, _worker_task
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


def _opener() -> list[str] | None:
    """Чем открыть файл в программе по умолчанию — только когда сервер работает на компьютере пользователя."""
    if sys.platform == "darwin":
        return ["open"]
    if shutil.which("xdg-open") and os.getenv("DISPLAY"):
        return ["xdg-open"]
    return None


@app.get("/health")
async def health():
    return {"ok": True, "formats": SUPPORTED, "models": await ollama.status(), "inbox": config.INBOX_DIR,
            "can_open": _opener() is not None}


@app.post("/models/pull")
async def pull_models():
    """Скачать недостающие модели (кнопка в интерфейсе)."""
    status = await ollama.status()
    if "error" in status:
        raise HTTPException(503, status["error"])
    for key in ("embed", "llm"):
        m = status[key]
        busy = m["pull"] and not m["pull"].startswith(("готово", "ошибка"))
        if not m["ready"] and not busy:
            asyncio.create_task(ollama.pull(m["name"]))
    return {"started": True}


@app.post("/upload")
async def upload(files: list[UploadFile] = File(...)):
    accepted, rejected = [], []
    for f in files:
        name = f.filename or "file"
        reason = unsupported_reason(name)
        if reason:
            rejected.append({"filename": name, "error": reason})
            continue
        # сразу на диск, по частям: большие архивы и почтовые ящики не держим в памяти
        tmp = _incoming() / f"{hashlib.sha1(f'{name}{_now()}{len(jobs)}'.encode()).hexdigest()[:12]}_{_safe_name(name)}"
        with open(tmp, "wb") as out:
            while chunk := await f.read(1024 * 1024):
                out.write(chunk)
        a, r = await _enqueue_all(name, tmp, True)
        accepted += a
        rejected += r
    if accepted:
        names = ", ".join(j["filename"] for j in accepted[:20])
        auth.audit("Загрузка документов", f"{len(accepted)} файл(ов): {names}{' …' if len(accepted) > 20 else ''}")
    return {"accepted": accepted[:200], "accepted_count": len(accepted), "rejected": rejected}


@app.post("/inbox/scan")
async def scan_inbox():
    """Индексирует все файлы из папки data/inbox (удобно для сотен документов сразу)."""
    inbox = Path(config.INBOX_DIR)
    inbox.mkdir(parents=True, exist_ok=True)
    queued = []
    for path in sorted(inbox.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED and not path.name.startswith("."):
            queued += (await _enqueue_all(path.name, path, False))[0]   # исходники в inbox не трогаем
    auth.audit("Загрузка из папки inbox", f"{len(queued)} файлов")
    return {"queued": queued[:200], "queued_count": len(queued)}


@app.post("/reindex")
async def reindex(only_outdated: bool = True, doc_id: str | None = None):
    """Заново проиндексировать сохранённые копии файлов (после обновления приложения или одного документа)."""
    docs = await store.list_docs()
    queued = []
    for d in docs:
        if doc_id and d["doc_id"] != doc_id:
            continue
        if not doc_id and only_outdated and not d.get("outdated"):
            continue
        path = _stored_file(d)
        if path:
            queued.append(_enqueue(d["filename"], path.read_bytes()))
    if queued:
        auth.audit("Переиндексация", ", ".join(j["filename"] for j in queued)[:300])
    return {"queued": queued}


@app.get("/jobs")
async def list_jobs():
    return sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)[:200]


@app.get("/jobs/summary")
async def jobs_summary():
    """Общий прогресс большой загрузки (тысячи писем): сколько в очереди, готово, пропущено, с ошибкой —
    и только те задания, которые стоит показать (идущие сейчас и ошибки), а не все 3000."""
    counts: dict[str, int] = {}
    for j in jobs.values():
        counts[j["status"]] = counts.get(j["status"], 0) + 1
    active = [j for j in jobs.values() if j["status"] in ("parsing", "embedding", "saving", "waiting")]
    errors = sorted((j for j in jobs.values() if j["status"] == "error"), key=lambda j: j.get("finished_at", ""),
                    reverse=True)
    recent = sorted((j for j in jobs.values() if j["status"] in ("done", "skipped")),
                    key=lambda j: j.get("finished_at", ""), reverse=True)
    return {"counts": counts, "total": len(jobs), "active": active, "errors": errors[:50], "recent": recent[:5],
            "last_finished": recent[0]["finished_at"] if recent else None}


@app.post("/jobs/clear")
async def clear_jobs():
    """Убрать завершённые задания из списка (сами документы остаются в базе)."""
    for k in [k for k, j in jobs.items() if j["status"] in ("done", "skipped", "error")]:
        del jobs[k]
    return {"ok": True}


def _stored_file(doc: dict) -> Path | None:
    path = Path(config.STORAGE_DIR) / doc["doc_id"] / Path(doc["filename"]).name
    return path if path.is_file() else None


def _describe(doc: dict) -> dict:
    """Карточка документа: что есть в базе + сведения о сохранённой копии файла."""
    path = _stored_file(doc)
    return {**doc, "format": Path(doc["filename"]).suffix.lower().lstrip(".") or "—",
            "size": path.stat().st_size if path else None, "has_file": path is not None}


async def _find_doc(doc_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{16}", doc_id):
        raise HTTPException(400, "Некорректный идентификатор")
    doc = next((d for d in await store.list_docs() if d["doc_id"] == doc_id), None)
    if doc is None:
        raise HTTPException(404, "Документ не найден")
    return doc


@app.get("/documents")
async def documents():
    return [_describe(d) for d in await store.list_docs()]


@app.get("/documents/{doc_id}")
async def document(doc_id: str):
    """Документ целиком: сведения и все фрагменты текста с номерами страниц и разделами."""
    doc = _describe(await _find_doc(doc_id))
    chunks = await store.doc_chunks(doc_id)
    # оглавление: все заголовки по тексту (у фрагмента в базе хранится только один, ведущий)
    sections = list(dict.fromkeys(h for c in chunks for h in headings(c["text"])))
    return {**doc, "sections": sections, "characters": sum(len(c["text"]) for c in chunks),
            "chunks_list": [{"chunk": c["chunk"], "page": c.get("page"), "section": c.get("section") or "",
                             "text": c["text"]} for c in chunks]}


@app.get("/documents/{doc_id}/file")
async def document_file(doc_id: str):
    doc = await _find_doc(doc_id)
    path = _stored_file(doc)
    if not path:
        raise HTTPException(404, "Копия файла не сохранилась — загрузите документ заново")
    auth.audit("Скачивание документа", doc["filename"])
    return FileResponse(path, filename=doc["filename"])


@app.post("/documents/{doc_id}/open")
async def open_document(doc_id: str):
    """Открыть исходный файл в программе по умолчанию (Просмотр, Word…) — в настольном приложении."""
    path, opener = _stored_file(await _find_doc(doc_id)), _opener()
    if not path:
        raise HTTPException(404, "Копия файла не сохранилась — загрузите документ заново")
    if not opener:
        raise HTTPException(501, "Открыть файл можно только в настольном приложении — скачайте его")
    subprocess.Popen([*opener, str(path)])
    return {"opened": doc_id}


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    if not re.fullmatch(r"[0-9a-f]{16}", doc_id):
        raise HTTPException(400, "Некорректный идентификатор")
    doc = next((d for d in await store.list_docs() if d["doc_id"] == doc_id), None)
    auth.audit("Удаление документа", doc["filename"] if doc else doc_id)
    await store.delete_doc(doc_id)
    folder = Path(config.STORAGE_DIR) / doc_id
    if folder.is_dir():
        for p in folder.iterdir():
            p.unlink()
        folder.rmdir()
    return {"deleted": doc_id}
