"""Сервис индексации: принимает файлы → текст → чанки → эмбеддинги → Qdrant.

Запускается отдельным контейнером `ingest`.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from . import config, ollama, store
from .chunking import chunk_pages, embed_text
from .parsing import SUPPORTED, extract_pages, unsupported_reason

log = logging.getLogger("ingest")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="Индексация документов")

# Задачи индексации (в памяти: после перезапуска история очищается, сами документы остаются в базе)
jobs: dict[str, dict] = {}
_queue: asyncio.Queue[tuple[str, str, bytes]] | None = None
_worker_task: asyncio.Task | None = None


def _safe_name(name: str) -> str:
    name = Path(name).name
    return re.sub(r"[^\w.\- ()\[\]]+", "_", name, flags=re.UNICODE) or "file"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def _index(job_id: str, filename: str, data: bytes) -> None:
    job = jobs[job_id]
    doc_id = hashlib.sha256(data).hexdigest()[:16]
    job.update(status="parsing", doc_id=doc_id)
    pages = await asyncio.to_thread(extract_pages, filename, data)
    if not pages:
        raise ValueError(
            "В файле не найден текст. Если это скан — сначала распознайте его (OCR), "
            "например, в ABBYY FineReader или через «Сохранить как PDF с текстом»."
        )
    chunks = chunk_pages(pages, config.CHUNK_SIZE, config.CHUNK_OVERLAP)
    job.update(status="embedding", chunks=len(chunks), done=0)

    vectors: list[list[float]] = []
    step = config.EMBED_BATCH
    for i in range(0, len(chunks), step):
        vectors.extend(await ollama.embed([embed_text(filename, c.section, c.text) for c in chunks[i : i + step]]))
        job["done"] = len(vectors)

    job["status"] = "saving"
    await store.ensure_collection(len(vectors[0]))
    await store.delete_doc(doc_id)  # повторная загрузка того же файла заменяет старую версию
    await store.upsert(doc_id, filename, chunks, vectors, _now())

    dest = Path(config.STORAGE_DIR) / doc_id
    dest.mkdir(parents=True, exist_ok=True)
    (dest / filename).write_bytes(data)


async def _worker() -> None:
    assert _queue is not None
    # Один воркер: файлы индексируются по очереди, чтобы не перегружать ноутбук
    while True:
        job_id, filename, data = await _queue.get()
        try:
            await _index(job_id, filename, data)
            jobs[job_id].update(status="done", finished_at=_now())
            log.info("Проиндексирован %s (%s чанков)", filename, jobs[job_id].get("chunks"))
        except Exception as e:
            log.exception("Ошибка индексации %s", filename)
            jobs[job_id].update(status="error", error=str(e), finished_at=_now())
        finally:
            _queue.task_done()


def _enqueue(filename: str, data: bytes) -> dict:
    filename = _safe_name(filename)
    job_id = hashlib.sha1(f"{filename}{_now()}{len(jobs)}".encode()).hexdigest()[:12]
    jobs[job_id] = {"job_id": job_id, "filename": filename, "status": "queued", "created_at": _now()}
    _ensure_worker()
    _queue.put_nowait((job_id, filename, data))
    return jobs[job_id]


def _ensure_worker() -> None:
    # Воркер запускается лениво: так сервис работает и сам по себе, и вмонтированным в app.main
    global _queue, _worker_task
    if _queue is None:
        _queue = asyncio.Queue()
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker())


@app.get("/health")
async def health():
    return {"ok": True, "formats": SUPPORTED, "models": await ollama.status(), "inbox": config.INBOX_DIR}


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
        reason = unsupported_reason(f.filename or "")
        if reason:
            rejected.append({"filename": f.filename, "error": reason})
            continue
        accepted.append(_enqueue(f.filename or "file", await f.read()))
    return {"accepted": accepted, "rejected": rejected}


@app.post("/inbox/scan")
async def scan_inbox():
    """Индексирует все файлы из папки data/inbox (удобно для сотен документов сразу)."""
    inbox = Path(config.INBOX_DIR)
    inbox.mkdir(parents=True, exist_ok=True)
    queued = []
    for path in sorted(inbox.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED:
            queued.append(_enqueue(path.name, path.read_bytes()))
    return {"queued": queued}


@app.post("/reindex")
async def reindex(only_outdated: bool = True):
    """Заново проиндексировать сохранённые копии файлов (после обновления приложения)."""
    docs = await store.list_docs()
    queued = []
    for d in docs:
        if only_outdated and not d.get("outdated"):
            continue
        path = Path(config.STORAGE_DIR) / d["doc_id"] / d["filename"]
        if path.is_file():
            queued.append(_enqueue(d["filename"], path.read_bytes()))
    return {"queued": queued}


@app.get("/jobs")
async def list_jobs():
    return sorted(jobs.values(), key=lambda j: j["created_at"], reverse=True)[:200]


@app.get("/documents")
async def documents():
    return await store.list_docs()


@app.delete("/documents/{doc_id}")
async def delete_document(doc_id: str):
    if not re.fullmatch(r"[0-9a-f]{16}", doc_id):
        raise HTTPException(400, "Некорректный идентификатор")
    await store.delete_doc(doc_id)
    folder = Path(config.STORAGE_DIR) / doc_id
    if folder.is_dir():
        for p in folder.iterdir():
            p.unlink()
        folder.rmdir()
    return {"deleted": doc_id}
