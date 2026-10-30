"""Тесты поиска и диалога на настоящей встроенной базе Qdrant, но без нейросетей:
эмбеддинги и LLM подменены простыми заглушками. Запуск: pytest backend/tests"""
import asyncio
import hashlib
import json
import math

import pytest

from app import chat_service, config, lexical, ollama, store
from app.chat_service import _clean_rewrite, _merge_text, _turns
from app.chunking import chunk_pages, embed_text

CONTRACT = [
    (1, "ДОГОВОР ПОДРЯДА № 15\nООО «Альфа» (Заказчик) и ООО «Бета» (Подрядчик) заключили договор.\n"
        "1. ПРЕДМЕТ ДОГОВОРА\nПодрядчик выполняет строительство склада."),
    (2, "4. ОПЛАТА\n4.1. Заказчик оплачивает работы в течение 30 банковских дней после подписания акта.\n"
        "4.2. Аванс составляет 20 % от цены договора."),
    (3, "5. ОТВЕТСТВЕННОСТЬ СТОРОН\n5.1. За просрочку оплаты Заказчик уплачивает неустойку 0,1 % "
        "от суммы долга за каждый день.\n5.2. За просрочку работ Подрядчик уплачивает пеню 0,5 %."),
    (4, "8. СПОРЫ\nСпоры рассматриваются в Арбитражном суде г. Москвы."),
]


def fake_vector(text: str) -> list[float]:
    """«Эмбеддинг» из основ слов: похожие тексты получают близкие векторы."""
    v = [0.0] * 64
    for t in lexical.tokenize(text):
        v[int(hashlib.md5(t.encode()).hexdigest(), 16) % 64] += 1
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [x / n for x in v]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "QDRANT_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(config, "QDRANT_URL", "")
    monkeypatch.setattr(config, "MIN_SCORE", 0.05)
    monkeypatch.setattr(store, "_client", None)

    async def embed(texts):
        return [fake_vector(t) for t in texts]

    monkeypatch.setattr(ollama, "embed", embed)
    lexical.invalidate()

    async def load():
        chunks = chunk_pages(CONTRACT, 200, 40)
        vecs = await embed([embed_text("Договор_подряда_15.pdf", c.section, c.text) for c in chunks])
        await store.ensure_collection(len(vecs[0]))
        await store.upsert("a" * 16, "Договор_подряда_15.pdf", chunks, vecs, "2026-01-01")

    asyncio.run(load())
    yield
    asyncio.run(store.client().close())
    store._client = None


def test_sections_are_tracked():
    chunks = chunk_pages(CONTRACT, 200, 40)
    by_text = {c.text.split("\n")[0]: c.section for c in chunks}
    assert by_text["4. ОПЛАТА"] == "4. ОПЛАТА"
    assert any(c.section == "5. ОТВЕТСТВЕННОСТЬ СТОРОН" and "5.2" in c.text for c in chunks)
    assert all(c.section != "ООО «Альфа» (Заказчик) и ООО «Бета» (Подрядчик) заключили договор." for c in chunks)


def test_bm25_finds_exact_terms():
    idx = lexical.Index([
        {"doc_id": "a", "chunk": 0, "text": "Срок оплаты 30 банковских дней"},
        {"doc_id": "a", "chunk": 1, "text": "Спор рассматривается в арбитражном суде"},
        {"doc_id": "b", "chunk": 0, "text": "Неустойка 0,1 % за каждый день просрочки"},
    ])
    assert idx.search("какая неустойка?", 3)[0][0]["chunk"] == 0 and idx.search("неустойку", 3)[0][0]["doc_id"] == "b"
    assert idx.search("в каком суде споры", 3)[0][0]["chunk"] == 1
    assert idx.search("неустойка", 3, doc_ids=["a"]) == []


def test_merge_removes_overlap():
    assert _merge_text("Первое предложение. Хвост, который повторяется", "Хвост, который повторяется и дальше") \
        == "Первое предложение. Хвост, который повторяется и дальше"
    assert _merge_text("Конец страницы", "Начало следующей") == "Конец страницы\nНачало следующей"


def test_turns_strip_old_citations():
    turns = _turns([{"role": "user", "content": "Срок оплаты?"},
                    {"role": "assistant", "content": "30 банковских дней [2], [3, 4].", "sources": []},
                    {"role": "system", "content": "мусор"}])
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert turns[1]["content"] == "30 банковских дней."


def test_clean_rewrite():
    assert _clean_rewrite("Переписанный вопрос: «Какая неустойка по договору подряда?»\n", "а неустойка?") \
        == "Какая неустойка по договору подряда?"
    assert _clean_rewrite("", "а неустойка?") == "а неустойка?"
    assert _clean_rewrite("Ответ: " + "очень длинно " * 100, "а суд?") == "а суд?"


def test_retrieve_returns_passages_with_neighbors(db):
    passages = asyncio.run(chat_service._retrieve("В каком суде рассматриваются споры?", 2, None))
    assert "Арбитражном суде г. Москвы" in passages[0]["text"]
    # к найденному фрагменту приклеен соседний с прошлой страницы
    assert passages[0]["page"] == "3–4" and passages[0]["section"] == "8. СПОРЫ"
    assert "5.2" in passages[0]["text"]
    # отрывки не повторяют один и тот же текст дважды
    texts = " ".join(p["text"] for p in passages)
    assert texts.count("Арбитражном суде") == 1


def test_follow_up_question_uses_dialog(db, monkeypatch):
    """«а для подрядчика?» сам по себе ничего не значит — нужен прошлый вопрос."""
    seen = {}

    async def chat(messages, max_tokens=200):
        seen["prompt"] = messages[-1]["content"]
        return "Какая неустойка за просрочку для Подрядчика?"

    async def chat_stream(messages):
        seen["final"] = messages
        yield "Пеня 0,5 % [1]."

    monkeypatch.setattr(ollama, "chat", chat)
    monkeypatch.setattr(ollama, "chat_stream", chat_stream)

    first = asyncio.run(chat_service._retrieve("Какая неустойка за просрочку оплаты?", 2, None))
    history = [
        {"role": "user", "content": "Какая неустойка за просрочку оплаты?"},
        {"role": "assistant", "content": "0,1 % от суммы долга за каждый день [1].",
         "sources": [{"doc_id": p["doc_id"], "filename": p["filename"], "chunks": p["chunks"]} for p in first[:1]]},
    ]

    async def run():
        resp = await chat_service.ask(chat_service.Ask(question="а для подрядчика?", history=history))
        return [json.loads(line) async for line in resp.body_iterator]

    events = asyncio.run(run())
    assert events[0] == {"type": "query", "text": "Какая неустойка за просрочку для Подрядчика?"}
    assert "Договор_подряда_15.pdf" in seen["prompt"]          # LLM знает, о каком документе шла речь
    sources = next(e for e in events if e["type"] == "sources")["sources"]
    assert any("0,5 %" in s["text"] for s in sources)
    final = seen["final"]
    assert [m["role"] for m in final] == ["system", "user", "assistant", "user"]
    assert "[1]" not in final[2]["content"]                     # старые ссылки не путают модель
    assert "С учётом диалога" in final[-1]["content"] and "а для подрядчика?" in final[-1]["content"]


def test_knowledge_base_api(db, tmp_path, monkeypatch):
    from app import ingest_service

    monkeypatch.setattr(config, "STORAGE_DIR", str(tmp_path / "files"))
    (tmp_path / "files" / ("a" * 16)).mkdir(parents=True)
    (tmp_path / "files" / ("a" * 16) / "Договор_подряда_15.pdf").write_bytes(b"x" * 2048)

    [doc] = asyncio.run(ingest_service.documents())
    assert doc["format"] == "pdf" and doc["size"] == 2048 and doc["pages"] == 4 and not doc["outdated"]

    full = asyncio.run(ingest_service.document("a" * 16))
    assert "5. ОТВЕТСТВЕННОСТЬ СТОРОН" in full["sections"] and "8. СПОРЫ" in full["sections"]
    assert [c["chunk"] for c in full["chunks_list"]] == list(range(full["chunks"]))
    assert full["chunks_list"][-1]["page"] == 4

    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(ingest_service.document("b" * 16))
