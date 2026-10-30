"""Всё в одном процессе: индексация + чат + веб-интерфейс.

Используется настольным приложением (app/desktop.py). Docker-вариант запускает
ingest_api (вход, индексация, анализ, админ) и chat_api отдельными контейнерами — код тот же.
"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import admin_service, analysis_service, auth, chat_service, ingest_service, logs

WEB_DIR = Path(os.getenv("WEB_DIR", Path(__file__).resolve().parents[2] / "web"))

logs.setup("desktop")
app = FastAPI(title="DocSearch")
app.mount("/api/auth", auth.app)
app.mount("/api/ingest", ingest_service.app)
app.mount("/api/chat", chat_service.app)
app.mount("/api/analysis", analysis_service.app)
app.mount("/api/admin", admin_service.app)
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
