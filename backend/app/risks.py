"""Реестр рисков договора или письма.

Два слоя:
1. Правила — надёжно находят типовые риски (высокая неустойка, односторонний отказ, молчаливая приёмка,
   отсутствие форс-мажора…) и всегда ссылаются на конкретный пункт.
2. Нейросеть — критически читает пункты пачками и добавляет то, что правилами не поймать
   (размытые формулировки, скрытые обязательства, противоречия). Её ответы проверяются:
   риск без существующего пункта отбрасывается.
"""
from __future__ import annotations

import json
import re
from typing import AsyncIterator

from . import ollama
from .legal import Clause, is_heading

LEVELS = ("высокий", "средний", "низкий")
# Конец предложения — но не «п. 5.1», «ст. 333», «г. Москва», «1 000 руб. в день»
_SENT_END = re.compile(r"(?<!\bп)(?<!\bпп)(?<!\bст)(?<!\bг)(?<!\bруб)(?<!\bкоп)(?<!\bтыс)(?<!\bмлн)(?<!\bд)(?<!\bул)"
                       r"(?<!\bт\.е)(?<!\bисх)(?<!\bвх)(?<!\bим)(?<!\bстр)(?<!\bч)[.;!?](?=\s+|$)|\n")


def _risk(clause: str, quote: str, risk: str, level: str, whom: str, rec: str, source: str = "правило") -> dict:
    return {"clause": clause, "quote": re.sub(r"\s+", " ", quote).strip()[:400], "risk": risk, "level": level,
            "for_whom": whom, "recommendation": rec, "source": source}


def _sentences(c: Clause):
    start = 0
    for m in _SENT_END.finditer(c.text):
        if c.text[start:m.end()].strip():
            yield c.text[start:m.end()].strip()
        start = m.end()
    if c.text[start:].strip():
        yield c.text[start:].strip()


def _actor(sentence: str, roles: list[str]) -> str:
    """Кто в предложении действует (платит, вправе…): первая упомянутая роль."""
    best = None
    for r in roles:
        m = re.search(rf"\b{re.escape(r[:-1] if len(r) > 5 else r)}\w*", sentence, re.IGNORECASE)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), r)
    return best[1] if best else ""


def _other(role: str, roles: list[str]) -> str:
    others = [r for r in roles if r != role]
    return others[0] if len(others) == 1 else ("контрагента" if not role else "другой стороны")


def _for(whom: str, side: str | None) -> str:
    if not whom:
        return "обе стороны"
    return f"{whom} (мы)" if side and whom.lower() == side.lower() else whom


def rule_risks(clauses: list[Clause], roles: list[str], doc_type: str, side: str | None) -> list[dict]:
    risks: list[dict] = []
    active = [c for c in clauses if c.status != "deleted"]
    full = "\n".join(c.text for c in active).lower().replace("ё", "е")

    if doc_type in ("letter", "claim"):
        return _letter_risks(active, side)

    penalty_payers = {}
    for c in active:
        for s in _sentences(c):
            low = s.lower().replace("ё", "е")
            num = c.num or "преамбула"
            # неустойка за каждый день
            if re.search(r"неустойк|пен[яиюей]\b|пени\b|штраф", low):
                payer = _actor(s, roles)
                penalty_payers.setdefault(payer, num)
                m = re.search(r"(\d+(?:[.,]\d+)?)\s*%[^.;]{0,80}?(?:за\s+каждый\s+(?:календарный\s+|рабочий\s+)?день|в\s+день|"
                              r"ежедневно)", low)
                if m:
                    rate = float(m[1].replace(",", "."))
                    if rate > 0.1:
                        risks.append(_risk(num, s, f"Высокая неустойка: {m[1]} % в день (≈{rate * 365:.0f} % годовых). "
                                                   "Суд может снизить по ст. 333 ГК РФ, но до суда это прямые потери.",
                                           "высокий" if rate >= 0.3 else "средний", _for(payer, side),
                                           "Снизить ставку до 0,1 % в день или ниже и установить предел (например, 10 % цены)."))
                    if not re.search(r"не\s+более|не\s+может\s+превыша|не\s+превыша|ограничива|максимальн|предел", c.text.lower()):
                        risks.append(_risk(num, s, "Неустойка не ограничена сверху: может вырасти до суммы, сопоставимой с ценой договора.",
                                           "средний", _for(payer, side),
                                           "Добавить: «…но не более 10 % от цены Договора»."))
            if re.search(r"в\s+одностороннем\s+(?:внесудебном\s+)?порядке[^.;]{0,120}(?:отказ|расторг)|односторонн\w+\s+"
                         r"(?:внесудебн\w+\s+)?отказ", low):
                who = _actor(s, roles)
                hard = re.search(r"без\s+(?:объяснения|указания)\s+причин|в\s+любое\s+время|без\s+возмещени", low)
                risks.append(_risk(num, s, f"Право одностороннего отказа{' у ' + who if who else ''}"
                                           f"{' в любое время / без причин' if hard else ''}: договор могут прекратить без суда.",
                                   "высокий" if hard else "средний", _for(_other(who, roles) if who else "", side),
                                   "Ограничить основания отказа, установить срок уведомления (30+ дней) и компенсацию понесённых расходов."))
            if re.search(r"(?:вправе|может)\s+в\s+одностороннем\s+порядке\s+(?:изменить|изменять|увеличить|увеличивать|пересмотреть)", low):
                who = _actor(s, roles)
                risks.append(_risk(num, s, f"{who or 'Сторона'} может в одностороннем порядке менять условия (цену, сроки).",
                                   "высокий", _for(_other(who, roles) if who else "", side),
                                   "Изменение условий — только дополнительным соглашением, подписанным обеими сторонами."))
            m = re.search(r"в\s+течение\s+(\d+)\s*(?:\([^)]*\)\s*)?(рабочих|банковских|календарных)?\s*дн", low)
            if m and re.search(r"оплат|оплачива|перечисл|плат[её]ж", low):
                days = int(m[1]) * (1.4 if m[2] in ("рабочих", "банковских") else 1)
                if days > 30:
                    payer = _actor(s, roles)
                    risks.append(_risk(num, s, f"Длинный срок оплаты: {m[1]} {m[2] or ''} дней — кассовый разрыв у получателя денег.",
                                       "средний", _for(_other(payer, roles) if payer else "получатель оплаты", side),
                                       "Сократить срок до 10–15 рабочих дней или предусмотреть аванс."))
            m = re.search(r"(?:предоплат|авансов|аванс)\w*[^.;]{0,80}?(\d{2,3})\s*%|(\d{2,3})\s*%[^.;]{0,60}?(?:предоплат|аванс)", low)
            if m and int(m[1] or m[2]) >= 50 and not re.search(r"гарант|обеспечени", full):
                payer = _actor(s, roles)
                risks.append(_risk(num, s, f"Крупный аванс ({m[1] or m[2]} %) без обеспечения возврата.",
                                   "средний", _for(payer, side),
                                   "Запросить банковскую гарантию возврата аванса или платить поэтапно."))
            if re.search(r"считают?ся\s+(?:принят|подписан|согласован|выполнен)|не\s+направ\w+\s+мотивированн\w+\s+отказ", low):
                who = _actor(s, roles)
                risks.append(_risk(num, s, "«Молчаливая» приёмка: работы/документы считаются принятыми, если вовремя не возразить.",
                                   "средний", _for(who, side),
                                   "Проверить, что срок на проверку реален, и закрепить порядок уведомлений (кому, как, когда)."))
            if re.search(r"автоматически\s+(?:продлева|пролонгир)|считается\s+продленн|пролонгируется", low):
                risks.append(_risk(num, s, "Автопролонгация: договор продлится сам, если не направить уведомление вовремя.",
                                   "низкий", "обе стороны", "Поставить контроль срока уведомления о непродлении."))
            if re.search(r"третейск", low):
                risks.append(_risk(num, s, "Спор передаётся в третейский суд: решение сложно обжаловать, процедура может быть дорогой.",
                                   "средний", "обе стороны", "Предусмотреть рассмотрение в государственном арбитражном суде."))
            m = re.search(r"по\s+месту\s+нахождения\s+(\w+)", low)
            if m and re.search(r"суд", low):
                home = next((r for r in roles if m[1].startswith(r.lower()[:5])), "")
                risks.append(_risk(num, s, f"Подсудность по месту нахождения {m[1]}: судиться придётся на его территории.",
                                   "низкий", _for(_other(home, roles), side) if home else "другая сторона",
                                   "Согласовать нейтральную подсудность или свой регион."))
            if re.search(r"(?:уступ\w+|переда\w+)\s+(?:свои\s+)?(?:права|требовани)[^.;]{0,80}?без\s+(?:предварительного\s+)?"
                         r"(?:письменного\s+)?согласия", low):
                who = _actor(s, roles)
                risks.append(_risk(num, s, "Права по договору можно уступить третьему лицу без согласия другой стороны.",
                                   "средний", _for(_other(who, roles) if who else "", side),
                                   "Уступка — только с письменного согласия."))
            if re.search(r"не\s+нес[её]т\s+(?:никакой\s+)?ответственност", low):
                who = _actor(s, roles)
                risks.append(_risk(num, s, f"Освобождение {who or 'стороны'} от ответственности.",
                                   "средний", _for(_other(who, roles) if who else "", side),
                                   "Сузить исключение до случаев непреодолимой силы и вины другой стороны."))

    # ответственность только у одной стороны
    payers = {p for p in penalty_payers if p}
    if len(roles) >= 2 and len(payers) == 1:
        only = next(iter(payers))
        risks.append(_risk(penalty_payers[only], "", f"Неустойка предусмотрена только для стороны «{only}»: ответственность асимметрична.",
                           "средний", _for(only, side), "Предусмотреть зеркальную ответственность другой стороны."))

    if doc_type in ("contract", "consolidated", "other"):
        missing = [
            (r"цен[аеуы]|стоимост|сумм[аы]\s+договора", "Не найдены условия о цене.", "высокий",
             "Указать цену или порядок её определения."),
            (r"непреодолим|форс-?мажор", "Нет условия о непреодолимой силе (форс-мажоре).", "средний",
             "Добавить раздел о форс-мажоре с порядком уведомления."),
            (r"спор|суд|претензи", "Не определён порядок разрешения споров и подсудность.", "средний",
             "Добавить претензионный порядок и суд."),
            (r"ответственност|неустойк|штраф|пен[ия]", "Нет условий об ответственности сторон.", "средний",
             "Добавить неустойку за просрочку и порядок возмещения убытков."),
            (r"срок\s+действия|вступает\s+в\s+силу|действует\s+до", "Не указан срок действия договора.", "низкий",
             "Указать срок действия и порядок продления."),
            (r"конфиденциальн", "Нет условия о конфиденциальности.", "низкий",
             "Добавить, если по договору передаётся чувствительная информация."),
        ]
        if re.search(r"подряд", full[:500]):
            missing.append((r"срок\w*\s+(?:выполнения|начала|окончания)|выполн\w+\s+в\s+срок|сроки\s+работ",
                            "Не найдены сроки выполнения работ — существенное условие договора подряда (ст. 708 ГК РФ).",
                            "высокий", "Указать начальный и конечный сроки работ."))
        for rx, text, level, rec in missing:
            if not re.search(rx, full):
                risks.append(_risk("—", "", text, level, "обе стороны", rec))
    return risks


def _letter_risks(clauses: list[Clause], side: str | None) -> list[dict]:
    risks = []
    for c in clauses:
        for s in _sentences(c):
            low = s.lower().replace("ё", "е")
            refs = re.findall(r"(?:п\.|пункт\w*)\s*(\d{1,2}(?:\.\d{1,3}){0,3})", low)
            clause = ", ".join(f"п. {r}" for r in refs) or "—"
            if re.search(r"(?:признаем|признает|признаю)\s+(?:наличие\s+)?(?:задолженност|долг)|гарантируем\s+оплат", low):
                risks.append(_risk(clause, s, "Признание долга: прерывает срок исковой давности (ст. 203 ГК РФ) и "
                                              "служит доказательством в суде.", "высокий", "отправитель",
                                   "Согласовать формулировку с юристом до отправки/подписания."))
            if re.search(r"расторж|отказ\w*\s+от\s+(?:исполнения\s+)?(?:договор|контракт)", low):
                risks.append(_risk(clause, s, "Уведомление о расторжении / отказе от договора.", "высокий", "получатель",
                                   "Проверить основания по договору и сроки ответа; зафиксировать дату получения."))
            if re.search(r"претензи|требуем|взыскан|обратиться\s+в\s+(?:арбитражный\s+)?суд|в\s+судебном\s+порядке", low):
                risks.append(_risk(clause, s, "Претензия / угроза судебного спора.", "высокий", "получатель",
                                   "Ответить в претензионный срок по договору, подготовить позицию и доказательства."))
            if re.search(r"(?:в\s+течение|не\s+позднее|до)\s+[^.;]{0,40}?(?:\d+\s*(?:\([^)]*\)\s*)?(?:рабочих|календарных|"
                         r"банковских)?\s*дн|\d{1,2}\.\d{1,2}\.\d{2,4})", low) and re.search(r"треб|прос|необходимо|обязан", low):
                risks.append(_risk(clause, s, "Требование с конкретным сроком исполнения.", "средний", "получатель",
                                   "Поставить срок на контроль; при несогласии — направить мотивированный ответ до его истечения."))
            elif re.search(r"неустойк|штраф|пен[ия]\b", low):
                risks.append(_risk(clause, s, "Упоминаются неустойка или штраф.", "средний", "получатель",
                                   "Сверить расчёт с условиями договора."))
    return risks


# ---------- нейросеть ----------

LLM_SYSTEM = """Ты — опытный юрист. Критически проверяешь {what} на риски{side}.
Обращай внимание на: односторонние права другой стороны, неясные условия, невыгодные или нереальные сроки, неограниченную ответственность, обязательства и расходы, которые легко упустить, противоречия между пунктами.
Каждый риск опиши конкретно, своими словами: что именно написано в пункте и чем это грозит. Не пиши общих слов вроде «размытые формулировки» без объяснения.
Не утверждай, что чего-то «нет» или «не указано», если это есть в тексте пункта. Если в пункте рисков нет — не упоминай его.
Отвечай только JSON, значения — по-русски."""

LLM_USER = """{items}

Верни JSON: {{"risks": [{{"clause": "номер пункта из списка", "risk": "в чём риск, кратко", "for_whom": "для какой стороны", "level": "высокий|средний|низкий", "recommendation": "как исправить"}}]}}
Не больше 5 самых существенных рисков. Если рисков нет — {{"risks": []}}."""


def batches(clauses: list[Clause], limit: int = 2500) -> list[list[Clause]]:
    out, cur, size = [], [], 0
    for c in clauses:
        if c.status == "deleted" or not c.text.strip() or is_heading(c):
            continue
        if cur and size + len(c.text) > limit:
            out.append(cur)
            cur, size = [], 0
        cur.append(c)
        size += len(c.text)
    if cur:
        out.append(cur)
    return out


async def llm_risks(batch: list[Clause], doc_type: str, side: str | None) -> list[dict]:
    what = {"letter": "письмо", "claim": "претензию"}.get(doc_type, "договор")
    items = "\n\n".join(f"[{c.num or 'преамбула'}] {c.text[:1500]}" for c in batch)
    messages = [
        {"role": "system", "content": LLM_SYSTEM.format(what=what, side=f" для стороны «{side}»" if side else "")},
        {"role": "user", "content": LLM_USER.format(items=items)},
    ]
    raw = await ollama.chat(messages, max_tokens=900, fmt="json")
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    items = data.get("risks") if isinstance(data, dict) else data
    known = {c.num or "преамбула": c for c in batch}
    result = []
    for r in items if isinstance(items, list) else []:
        if not isinstance(r, dict):
            continue
        num = str(r.get("clause") or "").strip().removeprefix("п.").strip().rstrip(".")
        text = str(r.get("risk") or "").strip()
        if num not in known or len(text) < 5:
            continue          # нейросеть сослалась на несуществующий пункт — не доверяем
        if _generic(text) or _contradicted(text, known[num].text):
            continue          # общие слова или «нет X», когда X в пункте есть (так ошибалась модель на 3B)
        level = str(r.get("level") or "").lower()
        # уровень от нейросети не выше «среднего»: на тестах 3B ставила «высокий» почти всему —
        # окончательную оценку даёт юрист, а высокий уровень остаётся за проверенными правилами
        level = "низкий" if "низ" in level else "средний"
        whom = str(r.get("for_whom") or "")[:60]
        if side and whom and side.lower()[:5] not in whom.lower():
            level = "низкий"          # риск для другой стороны, а мы смотрим со своей
        result.append(_risk(num, known[num].text[:300], text, level, whom,
                            str(r.get("recommendation") or "")[:400], "нейросеть"))
    return result


_GENERIC = {"размыт", "формулировк", "невыгодн", "нереальн", "срок", "скрыт", "обязательств", "расход", "односторон",
            "прав", "неограничен", "ответствен", "противореч", "пункт", "отсутств", "важн", "гарант", "услов"}


def _words(text: str) -> set[str]:
    from .lexical import tokenize

    return {t for t in tokenize(text) if not t[0].isdigit()}


def _generic(risk: str) -> bool:
    """«Размытые формулировки», «невыгодные сроки» — категории из промпта без объяснения."""
    words = _words(risk)
    return len(words) < 3 or len(words - _GENERIC) < 2


def _contradicted(risk: str, clause: str) -> bool:
    """«Отсутствие конкретного суда…», когда в пункте написано, какой суд."""
    m = re.match(r"\s*(?:отсутств\w*|нет|не указан\w*|не определ\w*|не установлен\w*|не предусмотрен\w*)\s+(.+)",
                 risk, re.IGNORECASE)
    if not m:
        return False
    claim = re.split(r",|\s(?:что|может|могут|котор\w*|из-за|поэтому)\s", m.group(1))[0]   # без «…, что может привести к…»
    claimed = _words(claim) - {"конкретн", "четк", "ясн", "точн", "услов"}
    return bool(claimed) and len(claimed & _words(clause)) / len(claimed) >= 0.5


def _similar(a: dict, b: dict) -> bool:
    if a["clause"] != b["clause"]:
        return False
    wa, wb = set(re.findall(r"\w{5,}", a["risk"].lower())), set(re.findall(r"\w{5,}", b["risk"].lower()))
    return bool(wa and wb) and len(wa & wb) / min(len(wa), len(wb)) >= 0.4


async def analyze(clauses: list[Clause], roles: list[str], doc_type: str, side: str | None,
                  use_llm: bool = True) -> AsyncIterator[dict]:
    """Поток событий: {"type":"risk"}, {"type":"progress"}; правила — сразу, нейросеть — по пачкам."""
    found = rule_risks(clauses, roles, doc_type, side)
    if side:
        for r in found:   # риск другой стороны (её высокая неустойка) для нас скорее плюс — не поднимаем тревогу
            if r["for_whom"] and "обе" not in r["for_whom"] and side.lower()[:5] not in r["for_whom"].lower():
                r["level"] = "низкий"
    for r in found:
        yield {"type": "risk", "risk": r}
    if not use_llm:
        return
    parts = batches(clauses)
    yield {"type": "progress", "done": 0, "total": len(parts)}
    for i, batch in enumerate(parts, start=1):
        try:
            for r in await llm_risks(batch, doc_type, side):
                if not any(_similar(r, f) for f in found):
                    found.append(r)
                    yield {"type": "risk", "risk": r}
        except Exception as e:  # одна неудачная пачка не должна обрывать весь анализ
            yield {"type": "warning", "text": f"Пункты {batch[0].num}–{batch[-1].num}: нейросеть не ответила ({e})"}
        yield {"type": "progress", "done": i, "total": len(parts)}


def sort_risks(risks: list[dict]) -> list[dict]:
    def key(r):
        num = r["clause"].split(",")[0].replace("п.", "").strip()
        parts = tuple(int(x) for x in num.split(".")) if re.fullmatch(r"\d+(?:\.\d+)*", num) else (999,)
        return LEVELS.index(r["level"]) if r["level"] in LEVELS else 3, parts
    return sorted(risks, key=key)
