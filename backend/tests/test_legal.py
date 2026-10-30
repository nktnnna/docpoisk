"""Тесты анализа документов без нейросети: реквизиты, связи, сводная редакция, сравнение, риски."""
from app.exports import consolidated_docx, risks_xlsx
from app.legal import analyze_meta, apply_ops, build_links, compare, parse_amendment, parse_clauses, parse_date
from app.risks import rule_risks

CONTRACT = """ДОГОВОР ПОДРЯДА № 15/2024
г. Москва «01» февраля 2024 г.
ООО «Альфа», именуемое в дальнейшем «Заказчик», и ООО «Бета» (далее — «Подрядчик») заключили договор:
1. ПРЕДМЕТ ДОГОВОРА
1.1. Подрядчик выполняет строительство склада.
2. ЦЕНА И ОПЛАТА
2.1. Цена Договора составляет 1 000 000 рублей.
2.2. Заказчик оплачивает работы в течение 30 банковских дней после подписания акта. Сумма
10 000 рублей удерживается.
3. СРОКИ
3.1. Срок выполнения работ — 60 дней.
5. ОТВЕТСТВЕННОСТЬ
5.1. За просрочку оплаты Заказчик уплачивает неустойку 0,1 % от суммы долга за каждый день.
5.2. За просрочку работ Подрядчик уплачивает пеню 0,5 % за каждый день.
6. ПРИЁМКА
6.1. Если Заказчик не направит мотивированный отказ в течение 5 дней, работы считаются принятыми.
6.2. Подрядчик вправе в одностороннем порядке изменить сроки работ."""

AMENDMENT = """ДОПОЛНИТЕЛЬНОЕ СОГЛАШЕНИЕ № 1
к Договору подряда № 15/2024 от 01.02.2024
г. Москва 10 марта 2024 г.
1. Пункт 2.1 Договора изложить в следующей редакции:
«2.1. Цена Договора составляет 1 200 000 рублей, включая работы ООО «Гамма».»
2. Дополнить Договор пунктом 3.2 следующего содержания:
«3.2. Подрядчик вправе выполнить работы досрочно.»
3. Пункт 5.2 Договора исключить.
4. В пункте 2.2 Договора слова «30 банковских дней» заменить словами «45 календарных дней».
5. Раздел 1 изложить в следующей редакции:
1. ПРЕДМЕТ
1.1. Подрядчик выполняет строительство склада и навеса.
1.2. Материалы предоставляет Подрядчик.
6. Абзац второй пункта 3.1 изложить иначе.
7. Настоящее соглашение является неотъемлемой частью Договора."""

LETTER = """ООО «Альфа»
Исх. № 45 от 05.04.2024
Уважаемый Иван Иванович!
В связи с просрочкой по договору № 15/2024 просим оплатить неустойку согласно п. 5.1 в течение 10 дней.
В случае неоплаты будем вынуждены обратиться в Арбитражный суд."""


def test_dates():
    assert parse_date("«01» февраля 2024") == "2024-02-01"
    assert parse_date("1.3.24") == "2024-03-01"
    assert parse_date("10 мая 2024") == "2024-05-10"
    assert parse_date("45.13.2024") is None


def test_requisites_and_links():
    c, a, l = analyze_meta(CONTRACT), analyze_meta(AMENDMENT), analyze_meta(LETTER)
    assert (c["type"], c["number"], c["date"]) == ("contract", "15/2024", "2024-02-01")
    assert {p["role"]: p["name"] for p in c["parties"]} == {"Заказчик": "ООО «Альфа»", "Подрядчик": "ООО «Бета»"}
    # дата доп. соглашения — его собственная, а не дата договора из строки «к Договору … от 01.02.2024»
    assert (a["type"], a["number"], a["date"]) == ("amendment", "1", "2024-03-10")
    assert (l["type"], l["number"], l["date"]) == ("letter", "45", "2024-04-05")

    metas = {"c": c, "a": a, "l": l}
    links = build_links(metas, {})
    assert {(x["from"], x["to"], x["relation"]) for x in links} == {("a", "c", "доп. соглашение к"), ("l", "c", "письмо по")}
    # ручные правки: убрать автоматическую связь и добавить свою
    links = build_links(metas, {"remove": [["l", "c"]], "add": [["l", "a"]]})
    assert {(x["from"], x["to"], x["auto"]) for x in links} == {("a", "c", True), ("l", "a", False)}


def test_clauses_ignore_numbers_inside_text():
    nums = [c.num for c in parse_clauses(CONTRACT)]
    assert nums == ["", "1", "1.1", "2", "2.1", "2.2", "3", "3.1", "5", "5.1", "5.2", "6", "6.1", "6.2"]
    assert "10 000 рублей удерживается" in parse_clauses(CONTRACT)[5].text   # «10 000» — не пункт 10


def test_amendment_applied():
    ops, unparsed = parse_amendment(AMENDMENT, "ДС № 1")
    assert [(o.op, o.clause) for o in ops] == [("replace", "2.1"), ("add", "3.2"), ("delete", "5.2"),
                                                 ("replace_words", "2.2"), ("replace", "1")]
    assert ops[0].text.endswith("включая работы ООО «Гамма».")          # вложенные кавычки не обрывают текст
    assert unparsed == ["6. Абзац второй пункта 3.1 изложить иначе."]   # непонятное — не теряется, а показывается

    result = {c.num: c for c in apply_ops(parse_clauses(CONTRACT), ops)}
    assert result["2.1"].text == "Цена Договора составляет 1 200 000 рублей, включая работы ООО «Гамма»."
    assert result["2.1"].old == "Цена Договора составляет 1 000 000 рублей." and result["2.1"].status == "changed"
    assert "45 календарных дней" in result["2.2"].text and "10 000 рублей" in result["2.2"].text
    assert result["3.2"].status == "added" and result["5.2"].status == "deleted"
    assert result["1"].text == "ПРЕДМЕТ" and result["1.2"].status == "added"
    assert result["1.1"].sources == ["ДС № 1, п. 5"]
    order = [c.num for c in apply_ops(parse_clauses(CONTRACT), ops)]
    assert order.index("3.2") == order.index("3.1") + 1                 # новый пункт встал на своё место
    assert all(o.applied and not o.error for o in ops)


def test_missing_clause_reported():
    ops, _ = parse_amendment("1. Пункт 9.9 Договора исключить.\n2. В пункте 2.1 слова «миллион» заменить словами «два»", "ДС")
    apply_ops(parse_clauses(CONTRACT), ops)
    assert [o.applied for o in ops] == [False, False]
    assert "9.9" in ops[0].error and "миллион" in ops[1].error


def test_compare_by_clauses():
    other = CONTRACT.replace("60 дней", "90 дней").replace("6.2. Подрядчик вправе в одностороннем порядке изменить сроки работ.", "")
    res = compare(CONTRACT, other)
    assert res["by_clauses"] and res["counts"]["changed"] == 1 and res["counts"]["removed"] == 1
    row = next(r for r in res["rows"] if r["status"] == "changed")
    assert row["num"] == "3.1" and ["-", "60"] in row["diff"] and ["+", "90"] in row["diff"]


def test_rule_risks_cite_clauses():
    risks = rule_risks(parse_clauses(CONTRACT), ["Заказчик", "Подрядчик"], "contract", "Заказчик")
    by = {(r["clause"], r["risk"].split(":")[0].split(" ")[0]) for r in risks}
    assert ("5.2", "Высокая") in by                                   # 0,5 % в день
    assert any(r["clause"] == "6.1" and "приёмка" in r["risk"] for r in risks)
    assert any(r["clause"] == "6.2" and r["level"] == "высокий" and r["for_whom"] == "Заказчик (мы)" for r in risks)
    assert any(r["clause"] == "2.2" and "срок оплаты" in r["risk"] for r in risks)
    assert any(r["clause"] == "—" and "непреодолимой силе" in r["risk"] for r in risks)

    letter = rule_risks(parse_clauses(LETTER), [], "letter", None)
    assert any(r["clause"] == "п. 5.1" and "сроком" in r["risk"] for r in letter)   # «п. 5.1» не разрезано на «п.»
    assert any("суд" in r["risk"] for r in letter)


def test_exports():
    ops, _ = parse_amendment(AMENDMENT, "ДС № 1")
    clauses = apply_ops(parse_clauses(CONTRACT), ops)
    result = {"title": "ДОГОВОР ПОДРЯДА № 15/2024", "amendments": [{"label_gen": "Дополнительного соглашения № 1"}],
              "clauses": [c.to_dict() for c in clauses], "ops": [o.to_dict() for o in ops], "unparsed": []}
    for redline in (False, True):
        data = consolidated_docx(result, redline)
        assert data[:2] == b"PK"
    import io

    import docx
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(consolidated_docx(result, False))).paragraphs)
    assert "1 200 000 рублей" in text and "5.2. Утратил силу." in text and "Сводная редакция" in text
    xlsx = risks_xlsx([{"filename": "Договор.docx", "created_at": "2024-01-01", "risks": rule_risks(
        parse_clauses(CONTRACT), ["Заказчик", "Подрядчик"], "contract", None)}])
    assert xlsx[:2] == b"PK"
