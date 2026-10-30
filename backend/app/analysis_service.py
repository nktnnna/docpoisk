"""Анализ документов: связи между документами, сводная редакция договора, сравнение, реестр рисков.

Работает с сохранёнными копиями файлов (STORAGE_DIR), поэтому в Docker живёт в том же контейнере,
что и индексация (см. app/ingest_api.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from . import config, exports, ingest_service, legal, ollama, risks, store
from .chunking import normalize
from .parsing import extract_pages

log = logging.getLogger("analysis")
app = FastAPI(title="Анализ документов")

META_VERSION = 1
_meta_cache: dict[tuple, dict] = {}
_meta_lock = asyncio.Lock()


def _dir(doc_id: str) -> Path:
    path = Path(config.STORAGE_DIR) / doc_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


def _stream(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="application/x-ndjson",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ---------- документы и реквизиты ----------

async def _doc(doc_id: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{16}", doc_id):
        raise HTTPException(400, "Некорректный идентификатор")
    doc = next((d for d in await store.list_docs() if d["doc_id"] == doc_id), None)
    if doc is None:
        raise HTTPException(404, "Документ не найден")
    return doc


async def doc_text(doc: dict) -> str:
    """Полный текст: из сохранённой копии файла, а если её нет — склеиваем фрагменты из базы."""
    path = ingest_service._stored_file(doc)
    if path:
        pages = await asyncio.to_thread(extract_pages, doc["filename"], path.read_bytes())
        return "\n".join(normalize(t) for _, t in pages)
    from .chat_service import _merge_text

    text = ""
    for c in await store.doc_chunks(doc["doc_id"]):
        text = _merge_text(text, c["text"]) if text else c["text"]
    return text


async def meta(doc: dict) -> dict:
    """Реквизиты документа (тип, номер, дата, стороны, ссылки на договоры) — считаются один раз и кешируются."""
    doc_id = doc["doc_id"]
    key = (doc_id, doc.get("uploaded_at"))   # после переиндексации реквизиты пересчитываются
    if key in _meta_cache:
        return _meta_cache[key]
    cache = Path(config.STORAGE_DIR) / doc_id / "meta.json"
    data = _read_json(cache, {})
    if data.get("v") != META_VERSION or data.get("uploaded_at") != doc.get("uploaded_at"):
        data = {**await asyncio.to_thread(legal.analyze_meta, await doc_text(doc)),
                "v": META_VERSION, "uploaded_at": doc.get("uploaded_at")}
        _write_json(_dir(doc_id) / "meta.json", data)
    _meta_cache[key] = data
    return data


def _links_file() -> Path:
    return Path(config.STORAGE_DIR) / "links.json"


async def graph() -> dict:
    docs = await store.list_docs()
    async with _meta_lock:
        metas = {}
        for d in docs:
            try:
                metas[d["doc_id"]] = await meta(d)
            except Exception:
                log.exception("Не удалось разобрать %s", d["filename"])
                metas[d["doc_id"]] = {"type": "other", "type_name": legal.TYPES["other"], "number": None,
                                      "date": None, "title": "", "parties": [], "refs": []}
    links = legal.build_links(metas, _read_json(_links_file(), {}))
    return {"docs": [{**d, **{k: v for k, v in metas[d["doc_id"]].items() if k not in ("v", "uploaded_at")}}
                     for d in docs],
            "links": links, "types": legal.TYPES}


def _short_label(m: dict) -> str:
    """«ДС № 1 от 10.03.2024» — основание правки, читается в любом падеже: «в ред. ДС № 1…»."""
    out = "ДС" if m["type"] == "amendment" else m["type_name"]
    return out + (f" № {m['number']}" if m.get("number") else "") + (f" от {legal.ru_date(m['date'])}" if m.get("date") else "")


def _label(m: dict, genitive: bool = False) -> str:
    name = {"amendment": ("доп. соглашение", "Дополнительного соглашения")}.get(m["type"], (m["type_name"], m["type_name"]))
    out = name[1 if genitive else 0]
    if m.get("number"):
        out += f" № {m['number']}"
    if m.get("date"):
        out += f" от {legal.ru_date(m['date'])}"
    return out[0].upper() + out[1:]


@app.get("/graph")
async def get_graph():
    """Все документы с реквизитами и связи «документ → договор»."""
    return await graph()


class LinkReq(BaseModel):
    from_id: str
    to_id: str


@app.post("/links")
async def add_link(req: LinkReq):
    data = _read_json(_links_file(), {})
    pair = [req.from_id, req.to_id]
    data["remove"] = [x for x in data.get("remove", []) if x != pair]
    if pair not in data.setdefault("add", []):
        data["add"].append(pair)
    _write_json(_links_file(), data)
    return {"ok": True}


@app.delete("/links")
async def remove_link(req: LinkReq):
    data = _read_json(_links_file(), {})
    pair = [req.from_id, req.to_id]
    data["add"] = [x for x in data.get("add", []) if x != pair]
    if pair not in data.setdefault("remove", []):
        data["remove"].append(pair)
    _write_json(_links_file(), data)
    return {"ok": True}


@app.get("/documents/{doc_id}/clauses")
async def clauses(doc_id: str):
    doc = await _doc(doc_id)
    return [c.to_dict() for c in legal.parse_clauses(await doc_text(doc))]


# ---------- сводная редакция ----------

LLM_OP_PROMPT = """Ты разбираешь правку из дополнительного соглашения к договору. Преобразуй её в JSON:
{"op": "replace" | "add" | "delete" | "append" | "replace_words" | "unknown",
 "clause": "номер пункта договора, например 2.1",
 "text": "новый текст пункта (для replace, add, append) — дословно из правки",
 "old": "заменяемые слова (для replace_words) — дословно", "new": "новые слова (для replace_words) — дословно"}
replace — пункт излагается в новой редакции; add — договор дополняется новым пунктом; delete — пункт исключается;
append — к пункту добавляется абзац; replace_words — в пункте одни слова заменяются другими.
Текст переписывай дословно, ничего не придумывай. Если правка непонятна — {"op": "unknown"}."""


async def _llm_op(fragment: str, source: str) -> legal.Op | None:
    """Правку, которую не разобрали правила, пробуем разобрать нейросетью (и помечаем — «проверьте»)."""
    try:
        raw = await ollama.chat([{"role": "system", "content": LLM_OP_PROMPT},
                                 {"role": "user", "content": fragment}], max_tokens=600, fmt="json")
        data = json.loads(raw)
    except Exception:
        return None
    kind = data.get("op")
    clause = str(data.get("clause") or "").strip().rstrip(".")
    if kind not in ("replace", "add", "delete", "append", "replace_words") or not re.fullmatch(r"\d{1,2}(\.\d{1,3}){0,3}", clause):
        return None
    op = legal.Op(kind, clause, text=str(data.get("text") or ""), old=str(data.get("old") or ""),
                  new=str(data.get("new") or ""), source=source, quote=fragment[:600], by_llm=True)
    if kind in ("replace", "add", "append") and not op.text.strip():
        return None
    if kind == "replace_words" and not (op.old and op.new and op.old in fragment and op.new in fragment):
        return None   # заменяемые слова должны дословно стоять в правке, иначе это фантазия модели
    return op


async def _amendments_of(contract_id: str, g: dict) -> list[dict]:
    docs = {d["doc_id"]: d for d in g["docs"]}
    ams = [docs[link["from"]] for link in g["links"]
           if link["to"] == contract_id and docs.get(link["from"], {}).get("type") == "amendment"]
    return sorted(ams, key=lambda d: (d.get("date") or "", d.get("number") or "", d.get("uploaded_at") or ""))


async def build_consolidated(contract_id: str, use_llm: bool = True) -> dict:
    g = await graph()
    contract = next((d for d in g["docs"] if d["doc_id"] == contract_id), None)
    if contract is None:
        raise HTTPException(404, "Документ не найден")
    if contract["type"] == "amendment":
        parent = next((link["to"] for link in g["links"] if link["from"] == contract_id), None)
        if not parent:
            raise HTTPException(400, "Это доп. соглашение не привязано к договору. Привяжите его в карточке документа.")
        contract = next(d for d in g["docs"] if d["doc_id"] == parent)
    amendments = await _amendments_of(contract["doc_id"], g)
    if not amendments:
        raise HTTPException(400, "К договору не привязано ни одного дополнительного соглашения. "
                                 "Загрузите их или привяжите вручную в карточке документа.")

    base = legal.parse_clauses(await doc_text(contract))
    ops: list[legal.Op] = []
    unparsed: list[dict] = []
    for am in amendments:
        label = _short_label(am)
        found, rest = legal.parse_amendment(await doc_text(am), label)
        ops += found
        for frag in rest:
            op = await _llm_op(frag, label) if use_llm else None
            if op:
                ops.append(op)
            else:
                unparsed.append({"source": label, "text": frag})
    result_clauses = legal.apply_ops(base, ops)
    result = {
        "contract": {"doc_id": contract["doc_id"], "filename": contract["filename"], "label": _label(contract)},
        "title": contract.get("title") or Path(contract["filename"]).stem,
        "amendments": [{"doc_id": a["doc_id"], "filename": a["filename"], "label": _label(a),
                        "label_gen": _label(a, genitive=True)} for a in amendments],
        "clauses": [c.to_dict() for c in result_clauses],
        "ops": [op.to_dict() for op in ops],
        "unparsed": unparsed,
        "counts": {k: sum(1 for c in result_clauses if c.status == k) for k in ("changed", "added", "deleted")},
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _write_json(_dir(contract["doc_id"]) / "consolidated.json", result)
    return result


@app.post("/consolidate/{doc_id}")
async def consolidate(doc_id: str):
    """Договор с учётом всех привязанных доп. соглашений (по порядку дат)."""
    await _doc(doc_id)
    return await build_consolidated(doc_id)


def _saved_consolidated(doc_id: str) -> dict:
    data = _read_json(Path(config.STORAGE_DIR) / doc_id / "consolidated.json", None)
    if not data:
        raise HTTPException(404, "Сначала соберите сводную редакцию")
    return data


def _download(data: bytes, filename: str, media: str) -> Response:
    return Response(data, media_type=media,
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"})


DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _consolidated_name(result: dict, redline: bool) -> str:
    stem = Path(result["contract"]["filename"]).stem
    return f"Сводная редакция{' (правки)' if redline else ''} — {stem}.docx"


@app.get("/consolidate/{doc_id}/docx")
async def consolidated_docx(doc_id: str, redline: bool = False):
    result = _saved_consolidated(doc_id)
    data = await asyncio.to_thread(exports.consolidated_docx, result, redline)
    return _download(data, _consolidated_name(result, redline), DOCX)


@app.post("/consolidate/{doc_id}/to_kb")
async def consolidated_to_kb(doc_id: str):
    """Добавить сводную редакцию в базу знаний — чтобы задавать вопросы по действующей редакции."""
    result = _saved_consolidated(doc_id)
    data = await asyncio.to_thread(exports.consolidated_docx, result, False)
    job = ingest_service._enqueue(_consolidated_name(result, False), data)
    return {"job": job}


# ---------- сравнение ----------

class CompareReq(BaseModel):
    a: str
    b: str


@app.post("/compare")
async def compare(req: CompareReq):
    da, db = await _doc(req.a), await _doc(req.b)
    result = await asyncio.to_thread(legal.compare, await doc_text(da), await doc_text(db))
    return {"a": {"doc_id": da["doc_id"], "filename": da["filename"]},
            "b": {"doc_id": db["doc_id"], "filename": db["filename"]}, **result}


class ExplainReq(BaseModel):
    kind: str                    # consolidated | compare
    doc_id: str | None = None
    a: str | None = None
    b: str | None = None


EXPLAIN_PROMPT = """Ты — юрист. Тебе дан перечень изменений в договоре (было → стало).
Кратко, по пунктам, опиши существенные изменения: что поменялось по сути и кому это выгодно или невыгодно.
Ссылайся на номера пунктов. Числа, суммы и сроки переписывай дословно. Не выдумывай изменений, которых нет в перечне.
Пиши по-русски."""


@app.post("/explain")
async def explain(req: ExplainReq):
    """Нейросеть объясняет простыми словами, что изменилось (по сводной редакции или по сравнению)."""
    if req.kind == "consolidated" and req.doc_id:
        res = _saved_consolidated(req.doc_id)
        rows = [{"num": c["num"], "status": c["status"], "a": c["old"], "b": c["text"]}
                for c in res["clauses"] if c["status"]]
    elif req.kind == "compare" and req.a and req.b:
        res = await compare(CompareReq(a=req.a, b=req.b))
        rows = [r for r in res["rows"] if r["status"] != "same"]
    else:
        raise HTTPException(400, "Не указано, что объяснять")
    names = {"changed": "изменён", "added": "добавлен", "deleted": "исключён", "removed": "исключён"}
    lines, size = [], 0
    for r in rows:
        line = f"Пункт {r['num'] or '—'} ({names.get(r['status'], r['status'])}).\nБыло: {(r['a'] or '—')[:700]}\nСтало: {(r['b'] or '—')[:700]}"
        if size + len(line) > config.CONTEXT_CHARS:
            lines.append(f"…и ещё {len(rows) - len(lines)} изменений")
            break
        lines.append(line)
        size += len(line)

    async def gen():
        if not rows:
            yield _line({"type": "token", "text": "Различий не найдено."})
            yield _line({"type": "done"})
            return
        try:
            async for piece in ollama.chat_stream([{"role": "system", "content": EXPLAIN_PROMPT},
                                                   {"role": "user", "content": "\n\n".join(lines)}]):
                yield _line({"type": "token", "text": piece})
            yield _line({"type": "done"})
        except Exception as e:
            log.exception("Ошибка объяснения")
            yield _line({"type": "error", "error": str(e)})

    return _stream(gen())


# ---------- риски ----------

class RiskReq(BaseModel):
    side: str | None = None          # «Заказчик» — оцениваем риски для этой стороны; пусто — для всех
    consolidated: bool = True        # для договора с доп. соглашениями — анализировать действующую редакцию
    use_llm: bool = True


@app.post("/risks/{doc_id}")
async def analyze_risks(doc_id: str, req: RiskReq):
    doc = await _doc(doc_id)

    async def gen():
        try:
            g = await graph()
            info = next(d for d in g["docs"] if d["doc_id"] == doc_id)
            clauses, basis = None, info["filename"]
            if req.consolidated and info["type"] == "contract" and await _amendments_of(doc_id, g):
                yield _line({"type": "status", "text": "Собираю действующую редакцию с учётом доп. соглашений…"})
                res = await build_consolidated(doc_id, use_llm=req.use_llm)
                clauses = [legal.Clause(c["num"], c["text"], c["status"]) for c in res["clauses"]]
                basis = f"{info['filename']} в ред. " + ", ".join(a["label_gen"] for a in res["amendments"])
            if clauses is None:
                clauses = legal.parse_clauses(await doc_text(doc))
            roles = list(dict.fromkeys(p["role"] for p in info.get("parties", []) if p.get("role")))

            # письмо, привязанное к договору: подставим текст пунктов договора, на которые оно ссылается
            contract_clauses = {}
            parent = next((link["to"] for link in g["links"] if link["from"] == doc_id), None)
            if parent and info["type"] in ("letter", "claim", "act", "other"):
                pdoc = next(d for d in g["docs"] if d["doc_id"] == parent)
                contract_clauses = {c.num: c.text for c in legal.parse_clauses(await doc_text(pdoc)) if c.num}
                roles = roles or list(dict.fromkeys(p["role"] for p in pdoc.get("parties", []) if p.get("role")))
                basis += f" (по документу «{pdoc['filename']}»)"
            yield _line({"type": "status", "text": "Проверяю типовые риски…"})

            found = []
            async for ev in risks.analyze(clauses, roles, info["type"], req.side, req.use_llm):
                if ev["type"] == "risk":
                    r = ev["risk"]
                    refs = re.findall(r"\d{1,2}(?:\.\d{1,3}){0,3}", r["clause"]) if r["clause"] != "—" else []
                    linked = [f"п. {n}: {contract_clauses[n]}" for n in refs if n in contract_clauses]
                    if linked:
                        r["contract_clause"] = "\n".join(linked)[:800]
                    found.append(r)
                elif ev["type"] == "progress" and ev["total"]:
                    ev = {**ev, "text": f"Нейросеть проверяет пункты: {ev['done']} из {ev['total']} частей"}
                yield _line(ev)
            register = {"doc_id": doc_id, "filename": info["filename"], "basis": basis, "side": req.side,
                        "type": info["type"], "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "model": config.LLM_MODEL if req.use_llm else None, "risks": risks.sort_risks(found)}
            _write_json(_dir(doc_id) / "risks.json", register)
            yield _line({"type": "done", "register": register})
        except Exception as e:
            log.exception("Ошибка анализа рисков")
            yield _line({"type": "error", "error": str(e)})

    return _stream(gen())


def _registers(doc_id: str | None = None) -> list[dict]:
    root = Path(config.STORAGE_DIR)
    paths = [root / doc_id / "risks.json"] if doc_id else sorted(root.glob("*/risks.json"))
    regs = [r for r in (_read_json(p, None) for p in paths) if r]
    return sorted(regs, key=lambda r: r.get("created_at", ""), reverse=True)


@app.get("/risks")
async def all_risks():
    """Сводный реестр: все сохранённые результаты анализа рисков (по документам, которые ещё есть в базе)."""
    alive = {d["doc_id"] for d in await store.list_docs()}
    return [r for r in _registers() if r["doc_id"] in alive]


@app.get("/risks/{doc_id}")
async def doc_risks(doc_id: str):
    regs = _registers(doc_id) if re.fullmatch(r"[0-9a-f]{16}", doc_id) else []
    if not regs:
        raise HTTPException(404, "Анализ рисков ещё не проводился")
    return regs[0]


@app.get("/risks.xlsx")
async def risks_xlsx(doc_id: str | None = None):
    regs = [await doc_risks(doc_id)] if doc_id else await all_risks()
    if not regs:
        raise HTTPException(404, "Реестр пуст: запустите анализ рисков хотя бы для одного документа")
    data = await asyncio.to_thread(exports.risks_xlsx, regs)
    name = f"Реестр рисков — {Path(regs[0]['filename']).stem}.xlsx" if doc_id else \
        f"Реестр рисков {datetime.now():%d.%m.%Y}.xlsx"
    return _download(data, name, XLSX)
