"""Работа с векторной базой Qdrant (метрика — косинусная близость)."""
from __future__ import annotations

import uuid

from qdrant_client import AsyncQdrantClient, models

from . import config

_client: AsyncQdrantClient | None = None


def client() -> AsyncQdrantClient:
    global _client
    if _client is None:
        if config.QDRANT_URL:
            _client = AsyncQdrantClient(url=config.QDRANT_URL, timeout=60)
        else:
            _client = AsyncQdrantClient(path=config.QDRANT_PATH)  # встроенный режим, без сервера
    return _client


async def ensure_collection(dim: int) -> None:
    c = client()
    if await c.collection_exists(config.COLLECTION):
        info = await c.get_collection(config.COLLECTION)
        current = info.config.params.vectors.size
        if current != dim:
            raise RuntimeError(
                f"Коллекция создана для эмбеддингов размера {current}, а модель {config.EMBED_MODEL} "
                f"даёт {dim}. Сменили EMBED_MODEL? Удалите папку с базой "
                f"({config.QDRANT_PATH}) и загрузите документы заново."
            )
        return
    await c.create_collection(
        config.COLLECTION,
        vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE),
    )
    if config.QDRANT_URL:  # индексы полей есть только у сервера; во встроенном режиме не нужны
        await c.create_payload_index(config.COLLECTION, "doc_id", models.PayloadSchemaType.KEYWORD)


# Версия индексации. 2 — эмбеддинги с именем документа и разделом. Старые документы работают,
# но ищутся хуже; интерфейс предлагает их переиндексировать.
INDEX_VERSION = 2


def point_id(doc_id: str, chunk: int) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{doc_id}:{chunk}"))


async def upsert(doc_id: str, filename: str, chunks, vectors, uploaded_at: str) -> None:
    points = [
        models.PointStruct(
            id=point_id(doc_id, ch.index),
            vector=vec,
            payload={
                "doc_id": doc_id,
                "filename": filename,
                "page": ch.page,
                "chunk": ch.index,
                "section": ch.section,
                "text": ch.text,
                "uploaded_at": uploaded_at,
                "v": INDEX_VERSION,
            },
        )
        for ch, vec in zip(chunks, vectors)
    ]
    for i in range(0, len(points), 256):
        await client().upsert(config.COLLECTION, points=points[i : i + 256], wait=True)
    _changed()


def _changed() -> None:
    from . import lexical  # индекс по словам строится из этой же базы — сбрасываем его

    lexical.invalidate()


def _doc_filter(doc_ids: list[str] | None):
    if not doc_ids:
        return None
    return models.Filter(must=[models.FieldCondition(key="doc_id", match=models.MatchAny(any=doc_ids))])


async def search(vector: list[float], limit: int, doc_ids: list[str] | None = None) -> list[dict]:
    if not await client().collection_exists(config.COLLECTION):
        return []
    res = await client().query_points(
        config.COLLECTION,
        query=vector,
        limit=limit,
        query_filter=_doc_filter(doc_ids),
        with_payload=True,
        score_threshold=config.MIN_SCORE,
    )
    return [{**p.payload, "score": round(p.score, 4)} for p in res.points]


async def get_chunks(keys: list[tuple[str, int]]) -> dict[tuple[str, int], dict]:
    """Фрагменты по (doc_id, номер) — для соседних кусков текста и продолжения диалога."""
    if not keys or not await client().collection_exists(config.COLLECTION):
        return {}
    res = await client().retrieve(config.COLLECTION, ids=[point_id(d, c) for d, c in keys], with_payload=True)
    return {(p.payload["doc_id"], p.payload["chunk"]): p.payload for p in res}


async def count() -> int:
    if not await client().collection_exists(config.COLLECTION):
        return 0
    return (await client().count(config.COLLECTION, exact=True)).count


async def all_chunks() -> list[dict]:
    if not await client().collection_exists(config.COLLECTION):
        return []
    result, offset = [], None
    while True:
        points, offset = await client().scroll(
            config.COLLECTION, limit=1000, offset=offset, with_payload=True, with_vectors=False)
        result.extend(p.payload for p in points)
        if offset is None:
            return result


async def delete_doc(doc_id: str) -> None:
    if await client().collection_exists(config.COLLECTION):
        await client().delete(config.COLLECTION, points_selector=_doc_filter([doc_id]), wait=True)
        _changed()


async def list_docs() -> list[dict]:
    """Список документов: собираем по первому чанку каждого документа."""
    if not await client().collection_exists(config.COLLECTION):
        return []
    docs: dict[str, dict] = {}
    offset = None
    while True:
        points, offset = await client().scroll(
            config.COLLECTION,
            limit=1000,
            offset=offset,
            with_payload=["doc_id", "filename", "uploaded_at", "v"],
            with_vectors=False,
        )
        for p in points:
            d = docs.setdefault(
                p.payload["doc_id"],
                {"doc_id": p.payload["doc_id"], "filename": p.payload["filename"],
                 "uploaded_at": p.payload.get("uploaded_at"), "chunks": 0,
                 "outdated": p.payload.get("v", 1) < INDEX_VERSION},
            )
            d["chunks"] += 1
        if offset is None:
            break
    return sorted(docs.values(), key=lambda d: d.get("uploaded_at") or "", reverse=True)
