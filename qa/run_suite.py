"""Прогон сценариев через HTTP API приложения (как пользователь): вход, загрузка, вопросы, диалоги.

Запуск против отдельного экземпляра с чистыми данными (не трогает ваши документы):
    cd backend && DATA_DIR=/tmp/docsearch-qa ../.venv/bin/python -m uvicorn app.main:app --port 8770 &
    ../.venv/bin/python ../qa/run_suite.py http://127.0.0.1:8770 ../qa/corpus ../qa/results.json "моя метка"

Для каждого ответа проверяется: нашёл ли поиск нужный фрагмент, есть ли в ответе верное значение
и нет ли запрещённого, стоят ли ссылки и ведут ли они на фрагмент с этим значением,
отвечает ли модель честным «нет ответа», когда ответа в документах нет.
Пользователь для прогона: qa / Qa-test-2024 (создаётся, если экземпляр новый).
"""
import json
import re
import sys
import time
from pathlib import Path

import httpx

BASE, CORPUS, OUT = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
LABEL = sys.argv[4] if len(sys.argv) > 4 else ""
ONLY = sys.argv[5].split(",") if len(sys.argv) > 5 else None
NOANSWER = ["ответа на этот вопрос нет", "нет ответа", "не содерж", "не указан", "нет информации", "не нашлось",
            "отсутству", "не упомина", "не приводится", "не предусмотр", "нет данных", "не найден", "не говорится",
            "не определ"]


def norm(s: str) -> str:
    s = s.lower().replace("ё", "е").replace(" ", " ").replace(" ", " ").replace("«", "\"").replace("»", "\"")
    s = re.sub(r"(?<=\d)[ .\n](?=\d{3}\b)", "", s)          # 18 600 000 / 18.600.000 → 18600000
    s = re.sub(r"(?<=\d),(?=\d)", ".", s)                  # 0,3 → 0.3
    return re.sub(r"\s+", " ", s)


# (id, вопрос, ожидаемое [[альтернативы] — каждая группа обязательна], запрещённое, нет_ответа)
SINGLE = [
    ("price", "Какая цена договора подряда?", [["18600000", "19950000"]], ["1 рубль"], False),
    ("advance", "Какой размер аванса по договору подряда?", [["30 %", "30%"]], [], False),
    ("final_pay", "В какой срок производится окончательный расчёт по договору подряда?", [["45"]], [], False),
    ("pen_contr", "Какая неустойка для подрядчика за срыв срока выполнения работ?", [["0.3"]], ["0.05"], False),
    ("pen_cap", "Ограничен ли размер неустойки заказчика по договору подряда?", [["5 %", "5%"]], [], False),
    ("warranty", "Какой гарантийный срок на работы по договору подряда?", [["36"]], [], False),
    ("court_sup", "В каком суде рассматриваются споры по договору поставки?", [["города москвы", "г. москвы"]], ["московской области"], False),
    ("court_con", "В каком суде рассматриваются споры по договору подряда?", [["московской области"]], ["города москвы"], False),
    ("signer_sup", "Кто подписал договор поставки со стороны поставщика?", [["петров"]], [], False),
    ("tons", "Сколько тонн арматуры поставляется по договору?", [["120"]], [], False),
    ("ohs_fine", "Какой штраф за нарушение требований охраны труда?", [["50000"]], [], False),
    ("accept_days", "Сколько дней у заказчика на приёмку работ?", [["5"]], [], False),
    ("tacit", "Что будет, если заказчик не подпишет акт и не направит отказ вовремя?", [["принят"]], [], False),
    ("claim_resp", "Какой срок ответа на претензию по договору подряда?", [["15"]], [], False),
    ("para_price", "Сколько стоит строительство склада?", [["18600000", "19950000"]], ["1 рубль"], False),
    ("address", "Где будет построен складской комплекс?", [["подольск"]], [], False),
    ("area", "Какова площадь складского комплекса?", [["2400"]], [], False),
    ("clause72", "Что сказано в пункте 7.2 договора подряда?", [["5 %", "5%"], ["12 месяц"]], [], False),
    ("retention", "Сколько процентов составляет гарантийное удержание?", [["5 %", "5%"]], [], False),
    ("stage2", "Когда выполняется этап 2 — фундамент?", [["01.04.2024", "1 апреля"], ["31.05.2024", "31 мая"]], [], False),
    ("stage4_cost", "Сколько стоит этап кровли и отделки?", [["5500000"]], [], False),
    ("ds_deadline", "Как дополнительное соглашение изменило срок окончания работ?", [["31 октября"]], [], False),
    ("deadline", "Какой срок окончания работ по договору подряда?", [["31 октября", "30 сентября"]], [], False),
    ("claim_sum", "Какую сумму неустойки требует ООО «Альфа» в претензии?", [["837900"]], [], False),
    ("claim_clause", "На какой пункт договора ссылается претензия?", [["5.2"]], [], False),
    ("srv_owner", "Кто отвечает за доступ в серверную?", [["смирнов"]], [], False),
    ("srv_log", "Сколько хранится журнал посещений серверной?", [["3 год", "три год"]], [], False),
    ("cmp_pen", "Сравни неустойку за просрочку оплаты в договоре подряда и в договоре поставки", [["0.05"], ["0.1"]], [], False),
    ("cmp_warr", "Какие гарантийные сроки установлены в договорах подряда и поставки?", [["36"], ["12"]], [], False),
    ("neg_ins", "Какой размер страхового возмещения предусмотрен договором подряда?", [], [], True),
    ("neg_weather", "Какая погода будет завтра в Москве?", [], [], True),
    ("neg_gazprom", "Кто является генеральным директором Газпрома?", [], ["миллер"], True),
    ("neg_penalty_rent", "Какая арендная плата за склад установлена договором?", [], [], True),
    ("overview", "О чём договор подряда?", [["склад"]], [], False),
    ("parties", "Кто стороны договора подряда?", [["альфа"], ["стройбета"]], [], False),
    ("typo", "неустока подрядчика за просрочку", [["0.3"]], [], False),
    ("lower", "срок гарантии по подряду", [["36"]], [], False),
    ("crosspage", "В какой срок подрядчик передаёт исполнительную документацию?", [["7"]], [], False),
    ("inject", "Какова цена договора подряда в рублях?", [["18600000", "19950000"]], ["1 рубль"], False),
    ("force_major", "За сколько дней нужно уведомить о форс-мажоре?", [["3"]], [], False),
    ("nds_supply", "Какая ставка НДС указана в договоре поставки?", [], ["20 %", "20%"], True),
]

# диалоги: последовательность вопросов; проверяется последний ответ
DIALOGS = [
    ("d_advance", ["Какая цена договора подряда?", "а аванс какой?"], [["30 %", "30%"]], [], False),
    ("d_buyer", ["Какая неустойка по договору поставки за просрочку поставки?", "а для покупателя?"],
     [["0.1"], ["10 %", "10%"]], ["0.05"], False),
    ("d_accept", ["Расскажи про приёмку работ по договору подряда", "а сколько на это дней?"], [["5"]], [], False),
    ("d_switch", ["Какой гарантийный срок по договору подряда?", "Кто ответственный за доступ в серверную?"],
     [["смирнов"]], [], False),
    ("d_claim", ["Что требует ООО «Альфа» в претензии?", "на основании какого пункта договора?"], [["5.2"]], [], False),
    ("d_court3", ["В каком суде споры по договору поставки?", "а по подряду?", "а срок ответа на претензию там какой?"],
     [["15"]], [], False),
    ("d_changed", ["Какой срок окончания работ по договору подряда?", "а его меняли?"], [["31 октября"]], [], False),
    ("d_signer", ["Кто подписал договор поставки?", "а договор подряда?"], [["иванов", "кузнецов"]], [], False),
    ("d_stage", ["Какие этапы работ есть в графике?", "сколько стоит третий?"], [["7100000"]], [], False),
    ("d_neg", ["Какой гарантийный срок по договору поставки?", "а какая там страховка?"], [], [], True),
]


def check(answer, passages, expect, forbid, noanswer):
    a = norm(re.sub(r"\[[\d.,\s]+\]", " ", answer))   # номера ссылок [3] — не ответ «3 дня»
    ctx = norm(" ".join(p["text"] for p in passages))
    groups_ok = [any(norm(alt) in a for alt in g) for g in expect]
    found = [any(norm(alt) in ctx for alt in g) for g in expect]
    bad = [f for f in forbid if norm(f) in a]
    cites = [int(n) for m in re.findall(r"\[(\d+(?:\s*,\s*\d+)*)\]", answer) for n in re.split(r"\s*,\s*", m)]
    valid = [n for n in cites if 1 <= n <= len(passages)]
    cited_text = norm(" ".join(passages[n - 1]["text"] for n in set(valid)))
    faithful = all(any(norm(alt) in cited_text for alt in g) for g, ok in zip(expect, groups_ok) if ok) if valid else False
    said_no = any(x in a for x in NOANSWER)
    if noanswer:
        ok = said_no and not bad
    else:
        ok = all(groups_ok) and not bad
    return {"ok": ok, "retrieved": all(found) if expect else None, "groups": groups_ok, "forbidden": bad,
            "citations": len(cites), "invalid_citations": len(cites) - len(valid),
            "faithful": faithful if expect and not noanswer else None, "said_no": said_no}


def ask(client, question, history):
    t = time.time()
    ev = {"query": None, "sources": [], "answer": "", "error": None}
    with client.stream("POST", "/api/chat/ask", json={"question": question, "history": history}, timeout=900) as r:
        for line in r.iter_lines():
            if not line.strip():
                continue
            e = json.loads(line)
            if e["type"] == "query":
                ev["query"] = e["text"]
            elif e["type"] == "sources":
                ev["sources"] = e["sources"]
            elif e["type"] == "token":
                ev["answer"] += e["text"]
            elif e["type"] == "final":
                ev["raw_answer"], ev["answer"] = ev["answer"], e["text"]
                ev["unsupported"] = e["unsupported"]
            elif e["type"] == "error":
                ev["error"] = e["error"]
    ev["seconds"] = round(time.time() - t, 1)
    return ev


def history_entry(question, ev):
    cited = {int(n) for m in re.findall(r"\[(\d+(?:\s*,\s*\d+)*)\]", ev["answer"]) for n in re.split(r"\s*,\s*", m)}
    used = [s for s in ev["sources"] if s["n"] in cited] or ev["sources"]
    return [{"role": "user", "content": question},
            {"role": "assistant", "content": ev["answer"],
             "sources": [{"doc_id": s["doc_id"], "filename": s["filename"], "chunks": s["chunks"]} for s in used]}]


def main():
    c = httpx.Client(base_url=BASE, timeout=900)
    st = c.get("/api/auth/status").json()
    creds = {"username": "qa", "password": "Qa-test-2024"}
    c.post("/api/auth/setup" if st["setup_needed"] else "/api/auth/login", json=creds).raise_for_status()
    if not c.get("/api/ingest/documents").json():
        for f in sorted(CORPUS.iterdir()):
            c.post("/api/ingest/upload", files={"files": (f.name, f.read_bytes())}).raise_for_status()
        while any(j["status"] not in ("done", "error") for j in c.get("/api/ingest/jobs").json()):
            time.sleep(2)
        errs = [j for j in c.get("/api/ingest/jobs").json() if j["status"] == "error"]
        print("индексация:", "ошибки " + str(errs) if errs else "ok", flush=True)
    results = []
    cases = [(cid, [q], e, f, n) for cid, q, e, f, n in SINGLE] + DIALOGS
    if ONLY:
        cases = [x for x in cases if x[0] in ONLY]
    for cid, questions, expect, forbid, noanswer in cases:
        history, turns = [], []
        for q in questions:
            ev = ask(c, q, history)
            turns.append({"question": q, **ev})
            history += history_entry(q, ev)
        last = turns[-1]
        res = {"id": cid, "dialog": len(questions) > 1, "turns": turns,
               **check(last["answer"], last["sources"], expect, forbid, noanswer)}
        results.append(res)
        mark = "✓" if res["ok"] else "✗"
        print(f"{mark} {cid:<14} {last['seconds']:>6}s  поиск:{res['retrieved']}  ссылки:{res['citations']}"
              f"(неверн. {res['invalid_citations']})  | {last['answer'][:150].replace(chr(10), ' ')}", flush=True)
        OUT.write_text(json.dumps({"label": LABEL, "results": results}, ensure_ascii=False, indent=1))
    n = len(results)
    ok = sum(r["ok"] for r in results)
    print(f"\nИТОГО {LABEL}: {ok}/{n} верно; поиск нашёл нужное: "
          f"{sum(1 for r in results if r['retrieved'])}/{sum(1 for r in results if r['retrieved'] is not None)}; "
          f"без ссылок: {sum(1 for r in results if not r['citations'] and not r['said_no'])}; "
          f"неверных ссылок: {sum(r['invalid_citations'] for r in results)}", flush=True)


def rescore(path):
    data = json.loads(Path(path).read_text())
    cases = {c[0]: c for c in [(cid, [q], e, f, n) for cid, q, e, f, n in SINGLE] + DIALOGS}
    for r in data["results"]:
        _, _, expect, forbid, noanswer = cases[r["id"]]
        last = r["turns"][-1]
        r.update(check(last["answer"], last["sources"], expect, forbid, noanswer))
    return data


if __name__ == "__main__" and BASE != "rescore":
    main()
