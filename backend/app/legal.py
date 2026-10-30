"""Юридическая структура документов — без нейросети, чтобы реквизиты и тексты пунктов не искажались.

- реквизиты: тип документа (договор, доп. соглашение, письмо…), номер, дата, стороны;
- ссылки на договоры («по Договору № 15 от 01.02.2024») — из них строятся связи между документами;
- пункты договора (1, 1.1, 1.1.1…) — для сводной редакции, сравнения и реестра рисков;
- правки из доп. соглашений («пункт 2.1 изложить в следующей редакции», «исключить»,
  «слова … заменить словами …», «дополнить пунктом …») и их применение к договору;
- пословное сравнение текстов.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

TYPES = {
    "contract": "Договор",
    "amendment": "Доп. соглашение",
    "letter": "Письмо",
    "claim": "Претензия",
    "act": "Акт",
    "protocol": "Протокол разногласий",
    "invoice": "Счёт",
    "consolidated": "Сводная редакция",
    "other": "Документ",
}
# Как документ связан с договором, на который ссылается
RELATIONS = {
    "amendment": "доп. соглашение к",
    "letter": "письмо по",
    "claim": "претензия по",
    "act": "акт по",
    "protocol": "протокол разногласий к",
    "invoice": "счёт по",
    "consolidated": "сводная редакция",
    "contract": "ссылается на",
    "other": "упоминает",
}
CONSOLIDATED_MARK = "Сводная редакция подготовлена автоматически"

# ---------- даты и номера ----------

MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "ма": 5, "июн": 6, "июл": 7, "август": 8,
          "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
DATE = r"(?:\d{1,2}\.\d{1,2}\.\d{2,4}|[«\"“]?\s*\d{1,2}\s*[»\"”]?\s+[а-яё]+\s+\d{4})"
NUM = r"№\s*([A-Za-zА-Яа-яЁё0-9](?:[\w\-/.]*[\w])?)"


def parse_date(s: str | None) -> str | None:
    """«01» февраля 2024 г. / 01.02.2024 / 1.2.24 → 2024-02-01."""
    if not s:
        return None
    s = s.strip().lower()
    m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", s)
    if m:
        d, mo, y = int(m[1]), int(m[2]), int(m[3])
        y += 2000 if y < 100 else 0
    else:
        m = re.fullmatch(r"[«\"“]?\s*(\d{1,2})\s*[»\"”]?\s+([а-яё]+)\s+(\d{4})", s)
        if not m:
            return None
        d, y = int(m[1]), int(m[3])
        mo = next((n for stem, n in sorted(MONTHS.items(), key=lambda kv: -len(kv[0])) if m[2].startswith(stem)), 0)
    if not (1 <= d <= 31 and 1 <= mo <= 12 and 1990 <= y <= 2100):
        return None
    return f"{y:04d}-{mo:02d}-{d:02d}"


def ru_date(iso: str | None) -> str:
    return "" if not iso else f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"


def norm_num(s: str) -> str:
    return re.sub(r"\s+", "", s).lower().replace("–", "-").replace("—", "-").strip(".-/")


# ---------- реквизиты ----------

ORG = re.compile(r"(?:ООО|ОАО|ЗАО|ПАО|АО|НАО|ИП|ФГУП|ФГБУ|ГУП|МУП|ГБУ|МБУ|АНО|НКО)\s*[«\"“][^»\"”\n]{2,80}[»\"”]|"
                 r"(?:ИП|Индивидуальный предприниматель)\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё.]+){1,2}")
ROLE = re.compile(r"(?:далее|именуем\w*\s+в\s+дальнейшем)\s*(?:по\s+тексту\s*)?[—–-]?\s*(?:именуем\w+\s+)?"
                  r"[«\"“]?\s*([А-ЯЁ][а-яё]+(?:\s*\d)?)\s*[»\"”]?", re.IGNORECASE)
_SERVICE_ROLES = {"договор", "соглашение", "стороны", "сторона", "контракт", "работы", "услуги", "товар", "объект"}


def _title_lines(text: str, n: int = 6) -> list[str]:
    return [x.strip() for x in text.split("\n") if x.strip()][:n]


def detect_type(text: str) -> str:
    if CONSOLIDATED_MARK in text[:3000]:
        return "consolidated"
    title = "\n".join(_title_lines(text)).lower().replace("ё", "е")
    head = text[:3000].lower().replace("ё", "е")
    if "дополнительное соглашение" in title:
        return "amendment"
    if "протокол разногласий" in title:
        return "protocol"
    if re.search(r"(^|\n)\s*претензия\b", title) or re.search(r"\bпретензи[яю]\b", title):
        return "claim"
    if re.search(r"(^|\n)\s*акт\b", title):
        return "act"
    if re.search(r"(^|\n)\s*сч[её]т\b", title):
        return "invoice"
    if re.search(r"(^|\n)\s*(государственный\s+|муниципальный\s+)?(договор|контракт)\b", title):
        return "contract"
    if re.search(r"исх\.?\s*№|уважаем|настоящим\s+(сообщаем|уведомляем|информируем)|просим\s|сообщаем|"
                 r"с\s+уважением|направляем\s+в\s+ваш", head):
        return "letter"
    if re.search(r"(^|\n)\s*соглашение\b", title):
        return "contract"
    return "other"


def parties(text: str) -> list[dict]:
    """Стороны с ролями: ООО «Альфа» (далее — «Заказчик»)."""
    head = text[:6000]
    result, seen = [], set()
    orgs = [(m.start(), m.end(), re.sub(r"\s+", " ", m.group(0))) for m in ORG.finditer(head)]
    for m in ROLE.finditer(head):
        role = m.group(1).strip()
        if role.lower() in _SERVICE_ROLES:
            continue
        before = [o for o in orgs if o[1] <= m.start() and m.start() - o[1] < 600]
        name = before[-1][2] if before else ""
        key = (name, role.lower())
        if key not in seen:
            seen.add(key)
            result.append({"name": name, "role": role})
    for _, _, name in orgs:  # организации без роли (например, в письмах)
        if name not in {p["name"] for p in result} and len(result) < 6:
            result.append({"name": name, "role": ""})
    return result


_REF_A = re.compile(rf"(?:договор|контракт)\w*(?:\s+[а-яё\-]+){{0,5}}?\s*{NUM}(?:\s*от\s*({DATE}))?", re.IGNORECASE)
_REF_B = re.compile(rf"(?:договор|контракт)\w*(?:\s+[а-яё\-]+){{0,5}}?\s+от\s+({DATE})\s*(?:г(?:ода|\.)?)?\s*{NUM}",
                    re.IGNORECASE)


def contract_refs(text: str) -> list[dict]:
    """Ссылки на договоры: «по Договору подряда № 15 от 01.02.2024», «к договору от 01.02.2024 № 15»."""
    refs = {}
    for m in _REF_A.finditer(text):
        refs.setdefault((norm_num(m[1]), parse_date(m[2])), m.group(0))
    for m in _REF_B.finditer(text):
        refs.setdefault((norm_num(m[2]), parse_date(m[1])), m.group(0))
    # если есть ссылка с датой, такая же без даты лишняя
    dated = {n for n, d in refs if d}
    return [{"num": n, "date": d, "text": re.sub(r"\s+", " ", t)[:120]}
            for (n, d), t in refs.items() if d or n not in dated]


def own_requisites(text: str, doc_type: str) -> tuple[str | None, str | None]:
    """Номер и дата самого документа — из заголовка («ДОГОВОР № 15», «Исх. № 12 от 05.03.2024»)."""
    lines = _title_lines(text, 10)
    title = "\n".join(lines)
    num = date = None
    if doc_type in ("letter", "claim"):
        m = re.search(rf"исх\w*\.?\s*{NUM}(?:\s*от\s*({DATE}))?", text[:1500], re.IGNORECASE)
        if m:
            num, date = m[1], parse_date(m[2])
    if not num:
        m = re.search(NUM, title)
        if m and not re.search(r"(?:к|по)\s+(?:договор|контракт)\w*[^№\n]{0,40}$", title[: m.start()], re.IGNORECASE):
            num = m[1]
    if not date:
        # строки вида «к Договору № 15 от 01.02.2024» — это дата договора, а не самого документа
        own = [x for x in (lines[:8] if doc_type != "letter" else text[:1500].split("\n"))
               if not re.search(r"(?:к|по)\s+(?:договор|контракт)", x, re.IGNORECASE)]
        m = re.search(DATE, "\n".join(own))
        date = parse_date(m.group(0)) if m else None
    return (norm_num(num) if num else None), date


def analyze_meta(text: str) -> dict:
    doc_type = detect_type(text)
    num, date = own_requisites(text, doc_type)
    refs = contract_refs(text)
    if doc_type == "contract" and num:
        refs = [r for r in refs if r["num"] != num]
    title = next((x for x in _title_lines(text, 3) if len(x) > 3), "")[:150]
    return {"type": doc_type, "type_name": TYPES[doc_type], "number": num, "date": date,
            "title": title, "parties": parties(text), "refs": refs}


def build_links(metas: dict[str, dict], manual: dict) -> list[dict]:
    """Связи «документ → договор» по номеру и дате договора + ручные правки пользователя."""
    by_num: dict[str, list[str]] = {}
    for doc_id, m in metas.items():
        if m["type"] == "contract" and m.get("number"):
            by_num.setdefault(m["number"], []).append(doc_id)
    links: dict[tuple[str, str], dict] = {}
    for doc_id, m in metas.items():
        for ref in m.get("refs", []):
            cands = [c for c in by_num.get(ref["num"], []) if c != doc_id]
            if ref["date"]:
                same_date = [c for c in cands if metas[c].get("date") in (None, ref["date"])]
                cands = same_date or cands
            for c in cands:
                links.setdefault((doc_id, c), {"from": doc_id, "to": c, "relation": RELATIONS[m["type"]],
                                               "auto": True, "evidence": ref["text"]})
    removed = {tuple(x) for x in manual.get("remove", [])}
    for key in list(links):
        if key in removed:
            del links[key]
    for a, b in manual.get("add", []):
        if a in metas and b in metas and (a, b) not in links:
            links[(a, b)] = {"from": a, "to": b, "relation": RELATIONS[metas[a]["type"]], "auto": False,
                             "evidence": "привязано вручную"}
    return list(links.values())


# ---------- пункты договора ----------

@dataclass
class Clause:
    num: str            # «2.1»; у преамбулы — пусто
    text: str           # текст без номера
    status: str = ""    # changed / added / deleted
    old: str | None = None
    sources: list[str] = field(default_factory=list)

    @property
    def key(self) -> tuple[int, ...]:
        return tuple(int(x) for x in self.num.split(".")) if self.num else ()

    def render(self) -> str:
        return f"{self.num}. {self.text}" if self.num else self.text

    def to_dict(self) -> dict:
        return {"num": self.num, "text": self.text, "status": self.status, "old": self.old,
                "sources": self.sources, "diff": word_diff(self.old or "", self.text) if self.status == "changed" else None}


_CLAUSE_LINE = re.compile(r"^\s*(?:(?:статья|раздел)\s+)?(\d{1,2}(?:\.\d{1,3}){0,3})\.?(?:\s+|(?=[А-ЯЁA-Z«\"]))(.*)$",
                          re.IGNORECASE)


def _plausible(prev: tuple[int, ...] | None, new: tuple[int, ...]) -> bool:
    """Похоже ли, что строка начинает следующий пункт, а не «10 000 рублей» в середине текста."""
    if prev is None:
        return new[0] == 1 and all(x == 1 for x in new[1:])
    if len(new) == len(prev) + 1 and new[:-1] == prev and new[-1] == 1:
        return True                                         # 2 → 2.1
    for depth in range(len(prev), 0, -1):                  # 2.3 → 2.4, 3, 3.1
        if len(new) >= depth and new[: depth - 1] == prev[: depth - 1] \
                and prev[depth - 1] < new[depth - 1] <= prev[depth - 1] + 2 and all(x == 1 for x in new[depth:]):
            return True
    return False


def _join(lines: list[str]) -> str:
    """Склеивает строки, разорванные при извлечении из PDF, но сохраняет абзацы."""
    out = ""
    for line in lines:
        line = line.strip()
        if not line:
            continue
        last = out.rsplit("\n", 1)[-1]
        if out and not re.search(r"[.:;!?»)]$", out) and line[:1].islower() and not last.isupper():
            out += " " + line
        else:
            out += ("\n" if out else "") + line
    return out


def parse_clauses(text: str) -> list[Clause]:
    clauses: list[Clause] = []
    current_num, buf, prev = "", [], None
    for line in text.split("\n"):
        m = _CLAUSE_LINE.match(line)
        if m:
            key = tuple(int(x) for x in m[1].split("."))
            if _plausible(prev, key):
                if buf or current_num:
                    clauses.append(Clause(current_num, _join(buf)))
                current_num, buf, prev = m[1], [m[2]], key
                continue
        buf.append(line)
    if buf or current_num:
        clauses.append(Clause(current_num, _join(buf)))
    return [c for c in clauses if c.num or c.text]


def is_heading(c: Clause) -> bool:
    return bool(c.num) and "." not in c.num and len(c.text) < 90 and not c.text.rstrip().endswith((".", ";"))


# ---------- правки из доп. соглашений ----------

CL = r"(\d{1,2}(?:\.\d{1,3}){0,3})"
UNIT = r"(?:пункт|подпункт|п\.|пп\.|раздел|статью|статья)"
DOCW = r"(?:\s+(?:настоящего\s+)?(?:договора|контракта))?"
REDACTION = r"в\s+(?:следующей|новой)\s+редакции\s*:?"
WORDS_OLD = r"(?:слова|слово|цифры|цифру|число|сумму|дату|текст|фразу)"
WORDS_NEW = r"(?:словами|словом|цифрами|цифрой|числом|суммой|датой|текстом|фразой)"

_PATTERNS = [
    ("replace", re.compile(rf"{UNIT}\s*{CL}{DOCW}\s+(?:изложить|читать)\s+{REDACTION}", re.I)),
    ("replace", re.compile(rf"изложить\s+{UNIT}\s*{CL}{DOCW}\s+{REDACTION}", re.I)),
    ("append", re.compile(rf"дополнить\s+(?:пункт|подпункт|п\.)\s*{CL}{DOCW}\s+(?:новым\s+)?(?:абзацем|предложением)"
                          rf"[^:\n]{{0,40}}?следующего\s+содержания\s*:?", re.I)),
    ("add", re.compile(rf"дополнить\s+[^\n:]{{0,60}}?(?:новым\s+|новой\s+)?(?:пункт(?:ом)?|подпункт(?:ом)?|раздел(?:ом)?|"
                       rf"стать[её]й|главой)\s*{CL}[^:\n]{{0,40}}?следующего\s+содержания\s*:?", re.I)),
    ("delete", re.compile(rf"{UNIT}\s*{CL}{DOCW}\s+(?:исключить|признать\s+утратившим\s+силу|считать\s+утратившим\s+силу)",
                          re.I)),
    ("delete", re.compile(rf"исключить\s+(?:из\s+договора\s+)?{UNIT}\s*{CL}", re.I)),
    ("replace_words", re.compile(rf"в\s+(?:пункте|подпункте|п\.|пп\.|разделе|статье)\s*{CL}{DOCW}\s+{WORDS_OLD}\s*", re.I)),
    ("replace_words_all", re.compile(rf"по\s+(?:всему\s+)?тексту\s+(?:договора|контракта)\s+{WORDS_OLD}\s*", re.I)),
]
_INSTRUCTION_WORDS = re.compile(r"изложить|дополнить|исключить|заменить|утратившим\s+силу", re.I)
_NEXT_ITEM = re.compile(r"\n\s*\d{1,2}\.\s+(?=(?:пункт|подпункт|п\.|раздел|стать|дополнить|исключить|изложить|в\s+пункте|"
                        r"в\s+разделе|по\s+(?:всему\s+)?тексту|настоящ|остальн|соглашени|во\s+всем|все\s+остальн|абзац|приложени|"
                        r"слова|стороны|иные|в\s+связи|признать))", re.I)


def _quoted(text: str, pos: int) -> tuple[str, int] | None:
    """Текст в кавычках начиная с pos, с учётом вложенных «ёлочек» (ООО «Альфа»)."""
    while pos < len(text) and text[pos] in " \t\n:":
        pos += 1
    if pos >= len(text) or text[pos] not in "«\"“„":
        return None
    if text[pos] == "«":
        depth = 0
        for i in range(pos, len(text)):
            depth += {"«": 1, "»": -1}.get(text[i], 0)
            if depth == 0:
                return text[pos + 1 : i], i + 1
        return None
    close = {"\"": "\"", "“": "”", "„": "“"}[text[pos]]
    end = text.find(close, pos + 1)
    return (text[pos + 1 : end], end + 1) if end > 0 else None


def _body(text: str, pos: int, item: str = "") -> tuple[str, int]:
    """Новая редакция: в кавычках или (без кавычек) до следующего пункта самого соглашения."""
    q = _quoted(text, pos)
    if q:
        return q[0].strip(), q[1]
    # первая строка новой редакции может сама начинаться с «6.» — ищем конец со второй строки
    start = len(text) - len(text[pos:].lstrip(" \t\n:"))
    after = text.find("\n", start) if text.find("\n", start) > 0 else len(text)
    m = re.compile(rf"\n\s*{int(item) + 1}\.\s+(?!\d)").search(text, after) if item else None
    m = m or _NEXT_ITEM.search(text, pos)
    end = m.start() if m else len(text)
    return text[pos:end].strip(" \n:«»\""), end


@dataclass
class Op:
    op: str                 # replace / add / delete / append / replace_words
    clause: str | None      # None — по всему тексту
    text: str = ""
    old: str = ""
    new: str = ""
    source: str = ""        # «ДС № 1 от 10.03.2024, п. 2»
    quote: str = ""         # исходная формулировка правки
    by_llm: bool = False
    applied: bool = False
    error: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in
                ("op", "clause", "text", "old", "new", "source", "quote", "by_llm", "applied", "error")}


def _item_no(text: str, pos: int) -> str:
    """Номер пункта самого соглашения, в котором стоит правка («1.», «2.») — для ссылки на основание."""
    start = text.rfind("\n", 0, pos) + 1
    m = re.match(r"\s*(\d{1,2})\.\s", text[start:pos + 1])
    return m[1] if m else ""


def parse_amendment(text: str, source: str) -> tuple[list[Op], list[str]]:
    """Правки из текста доп. соглашения → (распознанные операции, нераспознанные фрагменты с правками)."""
    found: list[tuple[int, int, Op]] = []
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= x or a >= y for x, y in taken)

    for kind, rx in _PATTERNS:
        for m in rx.finditer(text):
            if not free(m.start(), m.end()):
                continue
            clause = m.group(1) if rx.groups else None
            if kind == "replace_words_all":
                clause = None
            item = _item_no(text, m.start())
            src = f"{source}, п. {item}" if item else source
            quote_start = m.start()
            if kind in ("replace", "add", "append"):
                body, end = _body(text, m.end(), item)
                if not body:
                    continue
                op = Op(kind, clause, text=body, source=src)
            elif kind == "delete":
                end = m.end()
                op = Op("delete", clause, source=src)
            else:
                old = _quoted(text, m.end())
                if not old:
                    continue
                m2 = re.match(rf"\s*заменить\s+{WORDS_NEW}\s*", text[old[1]:], re.I)
                new = _quoted(text, old[1] + m2.end()) if m2 else None
                if not new:
                    continue
                end = new[1]
                op = Op("replace_words", clause, old=old[0], new=new[0], source=src)
            op.quote = re.sub(r"\s+", " ", text[quote_start:end])[:600]
            found.append((m.start(), end, op))
            taken.append((m.start(), end))

    # фразы с «изложить / исключить / заменить», которые не удалось разобрать правилами
    unparsed = []
    for m in re.finditer(r"[^\n]*(?:\n(?!\s*\d{1,2}\.\s)[^\n]*){0,6}", text):
        frag = m.group(0)
        if not frag.strip() or not _INSTRUCTION_WORDS.search(frag):
            continue
        a, b = m.start(), m.end()
        if free(a, b) and not re.search(r"неотъемлем|остальные\s+условия|вступает\s+в\s+силу", frag, re.I):
            unparsed.append(re.sub(r"\s+", " ", frag).strip()[:800])
    return [op for _, _, op in sorted(found, key=lambda x: x[0])], unparsed


def _strip_num(num: str, text: str) -> str:
    return re.sub(rf"^\s*{re.escape(num)}\.?\s*", "", text.strip())


def _new_clauses(num: str, text: str) -> list[Clause]:
    """Новая редакция пункта/раздела → пункты (раздел 7 может прийти вместе с 7.1, 7.2…)."""
    text = _strip_num(num, text)
    result = [Clause(num, "")]
    lines = []
    for line in text.split("\n"):
        m = _CLAUSE_LINE.match(line)
        if m and m[1].startswith(num + ".") and m[1] != num:
            result[-1].text = _join(lines)
            result.append(Clause(m[1], ""))
            lines = [m[2]]
        else:
            lines.append(line)
    result[-1].text = _join(lines)
    return result


def _is_under(num: str, parent: str) -> bool:
    return num == parent or num.startswith(parent + ".")


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("ё", "е").replace("Ё", "Е")).strip()


def apply_ops(clauses: list[Clause], ops: list[Op]) -> list[Clause]:
    """Применяет правки по порядку. Удалённые пункты остаются со статусом deleted — чтобы было видно, что их убрали."""
    clauses = [Clause(c.num, c.text, c.status, c.old, list(c.sources)) for c in clauses]

    def find(num: str) -> int | None:
        return next((i for i, c in enumerate(clauses) if c.num == num and c.status != "deleted"), None)

    def mark(c: Clause, status: str, src: str) -> None:
        if c.old is None and c.status != "added":
            c.old = c.text
        if c.status != "added":
            c.status = status
        c.sources.append(src)

    for op in ops:
        idx = find(op.clause) if op.clause else None
        if op.op in ("replace", "delete") and idx is None:
            if op.op == "replace":           # заменяют несуществующий пункт — по сути добавляют
                op.op, op.error = "add", "пункт не найден в договоре — добавлен"
            else:
                op.error = f"пункт {op.clause} не найден в договоре"
                continue
        if op.op == "replace":
            olds = [c for c in clauses if c.num and _is_under(c.num, op.clause) and c.status != "deleted"]
            first = clauses.index(olds[0])
            new = _new_clauses(op.clause, op.text)
            old_text = "\n".join(c.render() for c in olds)
            for c in olds:
                clauses.remove(c)
            for j, c in enumerate(new):
                c.status, c.sources = "changed", [op.source]
                c.old = (_strip_num(op.clause, old_text) if j == 0 and len(new) == 1
                         else next((o.text for o in olds if o.num == c.num), None))
                if c.old is None:
                    c.status = "added"
                clauses.insert(first + j, c)
            for o in olds:                  # подпункты, которых нет в новой редакции, утратили силу
                if o.num not in {c.num for c in new}:
                    gone = Clause(o.num, o.text, "deleted", o.text, [op.source])
                    clauses.insert(first + len(new), gone)
        elif op.op == "add":
            new = _new_clauses(op.clause, op.text)
            if idx is not None:             # такой пункт уже есть — заменяем
                op.error = op.error or "пункт уже был — заменён новой редакцией"
                mark(clauses[idx], "changed", op.source)
                clauses[idx].text = new[0].text
                op.applied = True
                continue
            key = new[0].key
            pos = len(clauses)
            for i, c in enumerate(clauses):
                if c.num and c.key > key and not _is_under(c.num, op.clause):
                    pos = i
                    break
            for j, c in enumerate(new):
                c.status, c.sources = "added", [op.source]
                clauses.insert(pos + j, c)
        elif op.op == "delete":
            for c in clauses:
                if c.num and _is_under(c.num, op.clause) and c.status != "deleted":
                    mark(c, "deleted", op.source)
        elif op.op == "append":
            if idx is None:
                op.error = f"пункт {op.clause} не найден в договоре"
                continue
            mark(clauses[idx], "changed", op.source)
            clauses[idx].text = clauses[idx].text.rstrip() + "\n" + op.text.strip()
        elif op.op == "replace_words":
            targets = [clauses[idx]] if idx is not None else ([] if op.clause else
                                                               [c for c in clauses if c.status != "deleted"])
            hit = False
            for c in targets:
                if op.old in c.text:
                    new_text = c.text.replace(op.old, op.new)
                elif _norm_ws(op.old) in _norm_ws(c.text):   # отличаются пробелы или «ё»
                    new_text = _norm_ws(c.text).replace(_norm_ws(op.old), op.new)
                else:
                    continue
                mark(c, "changed", op.source)
                c.text, hit = new_text, True
            if not hit:
                op.error = (f"в пункте {op.clause} не найдены слова «{op.old}»" if op.clause
                            else f"в тексте договора не найдены слова «{op.old}»")
                continue
        op.applied = True
    return clauses


# ---------- сравнение ----------

def word_diff(a: str, b: str) -> list[list[str]]:
    """[["=", "общий текст"], ["-", "удалено"], ["+", "добавлено"]]."""
    ta, tb = re.findall(r"\s+|[^\s]+", a), re.findall(r"\s+|[^\s]+", b)
    out: list[list[str]] = []

    def push(kind: str, s: str) -> None:
        if not s:
            return
        if out and out[-1][0] == kind:
            out[-1][1] += s
        else:
            out.append([kind, s])

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ta, tb, autojunk=False).get_opcodes():
        if tag == "equal":
            push("=", "".join(ta[i1:i2]))
        else:
            push("-", "".join(ta[i1:i2]))
            push("+", "".join(tb[j1:j2]))
    return out


def compare(text_a: str, text_b: str) -> dict:
    """Сравнение двух документов: по номерам пунктов, если они есть, иначе по абзацам."""
    ca, cb = parse_clauses(text_a), parse_clauses(text_b)
    numbered = sum(1 for c in ca if c.num) >= 3 and sum(1 for c in cb if c.num) >= 3
    rows = []
    if numbered:
        a_map, b_map = {c.num: c for c in ca}, {c.num: c for c in cb}
        order = list(dict.fromkeys([c.num for c in ca] + [c.num for c in cb]))
        order.sort(key=lambda n: tuple(int(x) for x in n.split(".")) if n else ())
        for n in order:
            a, b = a_map.get(n), b_map.get(n)
            if a and b:
                same = _norm_ws(a.text) == _norm_ws(b.text)
                rows.append({"num": n, "status": "same" if same else "changed", "a": a.text, "b": b.text,
                             "diff": None if same else word_diff(a.text, b.text)})
            elif a:
                rows.append({"num": n, "status": "removed", "a": a.text, "b": None, "diff": None})
            else:
                rows.append({"num": n, "status": "added", "a": None, "b": b.text, "diff": None})
    else:
        pa = [x.strip() for x in text_a.split("\n") if x.strip()]
        pb = [x.strip() for x in text_b.split("\n") if x.strip()]
        for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, pa, pb, autojunk=False).get_opcodes():
            if tag == "equal":
                rows += [{"num": "", "status": "same", "a": x, "b": x, "diff": None} for x in pa[i1:i2]]
            elif tag == "replace" and i2 - i1 == j2 - j1:
                rows += [{"num": "", "status": "changed", "a": x, "b": y, "diff": word_diff(x, y)}
                         for x, y in zip(pa[i1:i2], pb[j1:j2])]
            else:
                rows += [{"num": "", "status": "removed", "a": x, "b": None, "diff": None} for x in pa[i1:i2]]
                rows += [{"num": "", "status": "added", "a": None, "b": y, "diff": None} for y in pb[j1:j2]]
    counts = {k: sum(1 for r in rows if r["status"] == k) for k in ("same", "changed", "added", "removed")}
    return {"by_clauses": numbered, "rows": rows, "counts": counts}
