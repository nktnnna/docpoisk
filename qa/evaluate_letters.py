"""Точность автосвязей писем против truth.json и скорость «Базы знаний» на тысячах документов.

python qa/evaluate_letters.py qa/letters 8780
"""
import json, sys, time, itertools, httpx
from collections import defaultdict
from pathlib import Path
B = Path(sys.argv[1]); port = sys.argv[2] if len(sys.argv) > 2 else "8780"
truth = json.load(open(B / "corpus" / "truth.json"))
c = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=900)
c.post("/api/auth/login", json={"username": "qa", "password": "Qa-test-2024"}).raise_for_status()
for i in range(3):
    t = time.time(); g = c.get("/api/analysis/graph").json(); print(f"/graph: {time.time()-t:.2f} с, {len(json.dumps(g))//1024} КБ")
t = time.time(); docs = c.get("/api/ingest/documents").json(); print(f"/documents: {time.time()-t:.2f} с, документов {len(docs)}")
name = {d["doc_id"]: d["filename"] for d in g["docs"]}
types = defaultdict(int)
for d in g["docs"]: types[d["type"]] += 1
print("типы:", dict(types))
th = {d: truth.get(name[d]) for d in name}
ll = [l for l in g["links"] if th.get(l["from"]) and th.get(l["to"])]
good = sum(th[l["from"]] == th[l["to"]] for l in ll)
by_rel = defaultdict(lambda: [0, 0])
for l in ll:
    by_rel[l["relation"]][0] += 1; by_rel[l["relation"]][1] += th[l["from"]] == th[l["to"]]
print(f"связей письмо→письмо: {len(ll)}, верных: {good} ({100*good/max(1,len(ll)):.1f} %)")
for r, (n, ok) in by_rel.items(): print(f"   {r}: {ok}/{n}")
print("вложения → письмо:", sum(l["relation"] == "вложение к" for l in g["links"]), "| письмо → договор:", sum(l["relation"] == "письмо по" for l in g["links"]))
# сборка переписки целиком
comp = {}
for t_ in g["threads"]:
    for d in t_["docs"]: comp[d] = t_["id"]
groups = defaultdict(list)
for d, t_ in th.items():
    if t_: groups[t_].append(d)
whole = sum(1 for ids in groups.values() if len({comp.get(d, d) for d in ids}) == 1)
pairs = [(a, b) for ids in groups.values() for a, b in itertools.combinations(ids, 2)]
together = sum(comp.get(a) is not None and comp.get(a) == comp.get(b) for a, b in pairs)
mixed = sum(1 for t_ in g["threads"] if len({th.get(d) for d in t_["docs"]}) > 1)
print(f"переписок: {len(groups)}, собраны целиком: {whole} ({100*whole/len(groups):.1f} %); пар писем вместе: {100*together/len(pairs):.1f} %")
print(f"цепочек найдено: {len(g['threads'])}, из них смешаны разные переписки: {mixed}")
