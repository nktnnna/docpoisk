"""Docker-вариант: вход, индексация, анализ и администрирование в одном контейнере —
им нужны одни и те же копии файлов и папка пользователей."""
from fastapi import FastAPI

from . import admin_service, analysis_service, auth, ingest_service, logs

logs.setup("ingest")
app = FastAPI(title="Индексация и анализ документов")
app.mount("/auth", auth.app)
app.mount("/admin", admin_service.app)
app.mount("/analysis", analysis_service.app)
app.mount("/", ingest_service.app)
