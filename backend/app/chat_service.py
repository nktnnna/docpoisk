"""Сервис вопросов-ответов: вопрос → поиск (векторный + по словам) → ответ LLM по найденным фрагментам.

Запускается отдельным контейнером `chat`. Ответ стримится в формате NDJSON:
сначала {"type":"query",...} и {"type":"sources",...}, потом много {"type":"token",...}, в конце {"type":"done"}.

Как учитывается диалог. Уточняющий вопрос («а какой там срок?») сам по себе искать бесполезно,
поэтому LLM сначала переписывает его в самостоятельный («какой срок оплаты по договору подряда № 15?»),
а фрагменты, на которые опирался прошлый ответ, тоже попадают в кандидаты.
"""
from __future__ import annotations

import json
import logging
import re

from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import auth, config, lexical, ollama, store

log = logging.getLogger("chat")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

app = FastAPI(title="Вопросы по документам")
auth.protect(app)

SYSTEM_PROMPT = """Ты — ассистент, который отвечает на вопросы по фрагментам документов (договоры, регламенты, судебные материалы и т.п.).

Правила:
1. Факты бери ТОЛЬКО из фрагментов в последнем сообщении. Не добавляй общих знаний и ничего не выдумывай.
2. Вопрос может продолжать диалог («а какой там срок?», «а для заказчика?»). Пойми по истории, о каком документе и предмете речь, но ответ всё равно строй по фрагментам.
3. Прочитай все фрагменты и найди тот, где прямо говорится об ответе. Смотри, из какого документа и раздела фрагмент: не переноси условия одного документа на другой. Если документов несколько и в них разное — ответь по каждому отдельно.
4. Числа, проценты, суммы, сроки, даты, ФИО, названия судов и организаций переписывай из фрагмента дословно, не пересчитывай и не меняй.
5. Начинай сразу с ответа по существу, не повторяй вопрос. Пиши по-русски, кратко.
6. В конце каждого утверждения ставь номер фрагмента, из которого оно взято, например [2].
7. Если ответ есть лишь частично — дай то, что есть, и скажи, чего в документах нет. Если ответа нет совсем — ответь одной фразой: «В загруженных документах ответа на этот вопрос нет.»"""

USER_TEMPLATE = """Фрагменты документов:

{context}

=====
Вопрос: {question}{clarified}

Ответь на вопрос по фрагментам выше. Значения переписывай дословно, после каждого утверждения укажи номер фрагмента в квадратных скобках."""

CONDENSE_PROMPT = """Ты помогаешь искать по документам. По истории диалога перепиши последний вопрос пользователя так, чтобы он был понятен без истории: подставь, о каком документе, договоре, пункте, стороне или сроке идёт речь.
Если вопрос и так понятен без истории — повтори его без изменений.
Не отвечай на вопрос. Выведи только переписанный вопрос, одной строкой."""

# Вопросы «о документе в целом»: по ним нужен не только похожий фрагмент, но и начало документа
_OVERVIEW = re.compile(
    r"о\s?ч[её]м|кратко|перескаж|резюм|суть|содержан|что это за|что за документ|предмет договора|"
    r"стороны договора|кто сторон|между кем|основн\w* (услови|положени)", re.IGNORECASE)
_CITE = re.compile(r"\s*\[\d+(?:\s*,\s*\d+)*\](?:\s*,?\s*\[\d+(?:\s*,\s*\d+)*\])*")
RRF_K = 60


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    # предыдущие реплики [{role, content, sources?}]; у ответов sources = [{doc_id, filename, chunks}]
    history: list[dict] = Field(default_factory=list)
    doc_ids: list[str] | None = None                     # искать только в выбранных документах
    top_k: int | None = Field(default=None, ge=1, le=20)


# ---------- диалог ----------

def _turns(history: list[dict]) -> list[dict]:
    """Последние реплики диалога без мусора и без старых номеров ссылок.

    Номера [1], [2] в прошлых ответах относятся к прошлым фрагментам: если их оставить,
    модель путает их с номерами фрагментов нового вопроса.
    """
    turns = [m for m in history
             if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str) and m["content"].strip()]
    turns = turns[-2 * config.HISTORY_TURNS:]
    return [{**m, "content": _CITE.sub("", m["content"]).strip()} for m in turns]


def _short(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


async def _standalone(question: str, turns: list[dict]) -> str:
    """Переписывает уточняющий вопрос в самостоятельный, понятный без истории."""
    if not turns or not config.CONDENSE_QUESTION:
        return question
    lines = []
    for m in turns:
        if m["role"] == "user":
            lines.append(f"Пользователь: {_short(m['content'], 400)}")
        else:
            docs = sorted({s["filename"] for s in m.get("sources") or [] if s.get("filename")})
            where = f" (по документам: {', '.join(docs)})" if docs else ""
            lines.append(f"Ассистент{where}: {_short(m['content'], 500)}")
    messages = [
        {"role": "system", "content": CONDENSE_PROMPT},
        {"role": "user", "content": "История диалога:\n" + "\n".join(lines)
         + f"\n\nПоследний вопрос: {question}\n\nПереписанный вопрос:"},
    ]
    try:
        out = await ollama.chat(messages, max_tokens=120)
    except Exception:
        log.warning("Не удалось переформулировать вопрос", exc_info=True)
        return question
    return _clean_rewrite(out, question)


def _clean_rewrite(out: str, question: str) -> str:
    line = next((x.strip() for x in out.splitlines() if x.strip()), "")
    line = re.sub(r"^(переписанный вопрос|вопрос)\s*:\s*", "", line, flags=re.IGNORECASE).strip(" «»\"'")
    # модель иногда начинает отвечать вместо переформулировки — тогда ищем по исходному вопросу
    if len(line) < 3 or len(line) > 3 * len(question) + 300:
        return question
    return line


# ---------- поиск ----------

async def _candidates(queries: list[str], doc_ids: list[str] | None, prev: list[dict], limit: int) -> list[dict]:
    """Объединяет векторный поиск, поиск по словам и фрагменты прошлого ответа (Reciprocal Rank Fusion)."""
    ranked: list[tuple[float, list[dict]]] = []

    vectors = await ollama.embed(queries)
    for vec in vectors:
        hits = await store.search(vec, config.CANDIDATES, doc_ids)
        if hits:
            # фрагменты, заметно менее похожие, чем лучший, — шум, который сбивает модель
            best = hits[0]["score"]
            ranked.append((1.0, [h for h in hits if h["score"] >= best - config.REL_SCORE_GAP]))

    words = await lexical.search(" ".join(queries), config.CANDIDATES, doc_ids)
    if words:
        best = words[0][1]
        ranked.append((1.0, [{**p, "score": None} for p, s in words if s >= 0.3 * best]))

    # Продолжение диалога: на что опирался прошлый ответ, скорее всего, нужно и сейчас
    keys = [(s["doc_id"], c) for s in prev for c in s.get("chunks") or []
            if isinstance(s.get("doc_id"), str) and isinstance(c, int) and (not doc_ids or s["doc_id"] in doc_ids)]
    if keys:
        found = await store.get_chunks(keys[:limit * 2])
        ranked.append((0.5, [{**found[k], "score": None} for k in keys if k in found]))

    fused: dict[tuple[str, int], dict] = {}
    for weight, hits in ranked:
        for rank, h in enumerate(hits):
            key = (h["doc_id"], h["chunk"])
            item = fused.setdefault(key, {**h, "rrf": 0.0})
            item["rrf"] += weight / (RRF_K + rank)
            if h.get("score") is not None and (item.get("score") is None or h["score"] > item["score"]):
                item["score"] = h["score"]
    return sorted(fused.values(), key=lambda h: h["rrf"], reverse=True)[:limit]


def _merge_text(a: str, b: str) -> str:
    """Склеивает соседние фрагменты, убирая перекрытие (конец a повторяется в начале b)."""
    for k in range(min(len(a), len(b), 2 * config.CHUNK_OVERLAP + 50), 19, -1):
        if a.endswith(b[:k]):
            return a + b[k:]
    return a + "\n" + b


def _pages(chunks: list[dict]) -> str | None:
    pages = [c["page"] for c in chunks if c.get("page")]
    if not pages:
        return None
    return str(pages[0]) if pages[0] == pages[-1] else f"{pages[0]}–{pages[-1]}"


async def _passages(hits: list[dict], focus_docs: list[str]) -> list[dict]:
    """Найденные фрагменты + соседние куски текста, склеенные в связные отрывки.

    Пункт договора часто начинается в одном фрагменте, а заканчивается в следующем
    (или на следующей странице), поэтому модели даём и соседей — в пределах CONTEXT_CHARS.
    """
    budget = config.CONTEXT_CHARS
    rank: dict[tuple[str, int], float] = {}
    chosen: dict[tuple[str, int], dict] = {}

    def take(key, payload, r) -> None:
        nonlocal budget
        if key in chosen:
            rank[key] = min(rank[key], r)
        elif len(payload["text"]) <= budget or not chosen:
            chosen[key] = payload
            rank[key] = r
            budget -= len(payload["text"])

    # Вопрос о документе в целом — начало документа (стороны, предмет) идёт первым
    starts = await store.get_chunks([(d, c) for d in focus_docs for c in range(2)]) if focus_docs else {}
    for key in sorted(starts):
        take(key, {**starts[key], "score": None}, -1)

    for r, h in enumerate(hits):
        take((h["doc_id"], h["chunk"]), h, r)

    n = config.NEIGHBORS
    want = [(h["doc_id"], h["chunk"] + d) for h in hits for d in range(-n, n + 1) if d and h["chunk"] + d >= 0]
    neighbors = await store.get_chunks(list(dict.fromkeys(k for k in want if k not in chosen)))
    for r, h in enumerate(hits):
        for d in [x for i in range(1, n + 1) for x in (-i, i)]:
            key = (h["doc_id"], h["chunk"] + d)
            if key in neighbors:
                take(key, {**neighbors[key], "score": None}, r + 0.5)  # сосед чуть ниже самого фрагмента

    # соседние номера одного документа → один отрывок
    passages: list[dict] = []
    for key in sorted(chosen):
        doc_id, chunk = key
        last = passages[-1] if passages else None
        if last and last["doc_id"] == doc_id and last["chunks"][-1] == chunk - 1:
            last["parts"].append(chosen[key])
            if rank[key] < last["rank"]:
                last["main"] = chosen[key]
            last["chunks"].append(chunk)
            last["rank"] = min(last["rank"], rank[key])
        else:
            passages.append({"doc_id": doc_id, "chunks": [chunk], "parts": [chosen[key]],
                             "main": chosen[key], "rank": rank[key]})

    result = []
    for p in sorted(passages, key=lambda p: p["rank"]):
        parts = p.pop("parts")
        text = parts[0]["text"]
        for part in parts[1:]:
            text = _merge_text(text, part["text"])
        scores = [x["score"] for x in parts if x.get("score") is not None]
        result.append({
            "doc_id": p["doc_id"], "chunks": p["chunks"], "filename": parts[0]["filename"],
            "page": _pages(parts), "section": p["main"].get("section") or "",
            "score": max(scores) if scores else None, "text": text,
        })
    return result


async def _retrieve(question: str, top_k: int | None, doc_ids: list[str] | None,
                    turns: list[dict] | None = None, query: str | None = None) -> list[dict]:
    turns = turns or []
    query = query or question
    queries = list(dict.fromkeys([query, question]))
    prev = next((m.get("sources") or [] for m in reversed(turns) if m["role"] == "assistant"), [])
    hits = await _candidates(queries, doc_ids, prev if turns else [], top_k or config.TOP_K)
    if not hits:
        return []

    focus: list[str] = []
    if _OVERVIEW.search(query):
        focus = doc_ids[:2] if doc_ids and len(doc_ids) <= 2 else list(dict.fromkeys(h["doc_id"] for h in hits))[:1]
    return await _passages(hits, focus)


def _context(passages: list[dict]) -> str:
    blocks = []
    for i, p in enumerate(passages, start=1):
        where = p["filename"] + (f", стр. {p['page']}" if p.get("page") else "")
        if p.get("section"):
            where += f", раздел «{p['section']}»"
        blocks.append(f"Фрагмент [{i}] — документ {where}:\n{p['text']}")
    return "\n\n".join(blocks)


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


@app.get("/health")
async def health():
    return {"ok": True, "models": await ollama.status()}


@app.post("/search")
async def search(req: Ask):
    """Только поиск фрагментов, без ответа LLM — удобно для отладки качества поиска."""
    turns = _turns(req.history)
    query = await _standalone(req.question, turns)
    return {"query": query, "passages": await _retrieve(req.question, req.top_k, req.doc_ids, turns, query)}


@app.post("/ask")
async def ask(req: Ask):
    auth.audit("Вопрос", req.question[:200])
    async def gen():
        try:
            turns = _turns(req.history)
            query = await _standalone(req.question, turns)
            if query != req.question:
                yield _line({"type": "query", "text": query})
            passages = await _retrieve(req.question, req.top_k, req.doc_ids, turns, query)
            yield _line({"type": "sources", "sources": [{"n": i, **p} for i, p in enumerate(passages, start=1)]})
            if not passages:
                yield _line({"type": "token", "text": "В загруженных документах не нашлось фрагментов, "
                             "относящихся к вопросу. Проверьте, что нужные документы загружены, "
                             "или переформулируйте вопрос."})
                yield _line({"type": "done"})
                return

            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            for m in turns:
                messages.append({"role": m["role"], "content": _short(m["content"], 800)})
            clarified = f"\n(С учётом диалога: {query})" if query != req.question else ""
            messages.append({"role": "user", "content": USER_TEMPLATE.format(
                context=_context(passages), question=req.question, clarified=clarified)})
            async for piece in ollama.chat_stream(messages):
                yield _line({"type": "token", "text": piece})
            yield _line({"type": "done"})
        except Exception as e:
            log.exception("Ошибка при ответе")
            yield _line({"type": "error", "error": str(e)})

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
