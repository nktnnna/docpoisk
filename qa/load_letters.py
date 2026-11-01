"""Загрузка писем из make_letters.py в запущенный экземпляр и замер времени индексации.

python qa/load_letters.py qa/letters http://127.0.0.1:8780
"""
import sys, time, httpx
from pathlib import Path
B = Path(sys.argv[1]); c = httpx.Client(base_url=sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8780", timeout=900)
st = c.get("/api/auth/status").json()
c.post("/api/auth/setup" if st["setup_needed"] else "/api/auth/login", json={"username": "qa", "password": "Qa-test-2024"}).raise_for_status()
t0 = time.time()
for f in ["Почта_выгрузка.mbox", "Письма_DOCX.zip", "Почта_с_вложениями.zip"]:
    r = c.post("/api/ingest/upload", files={"files": (f, (B / "corpus" / f).read_bytes())}).json()
    print(f, "принято:", r["accepted_count"], "отклонено:", len(r["rejected"]), f"{time.time()-t0:.1f}s", flush=True)
while True:
    s = c.get("/api/ingest/jobs/summary").json(); k = s["counts"]
    left = sum(k.get(x, 0) for x in ("queued", "parsing", "embedding", "saving", "waiting"))
    print(f"{time.time()-t0:6.0f}s  {k}", flush=True)
    if not left: break
    time.sleep(20)
print("индексация:", round(time.time() - t0), "с")
