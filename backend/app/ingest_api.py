"""Docker-вариант: индексация и анализ документов в одном контейнере — им нужны одни и те же копии файлов."""
from fastapi import FastAPI

from . import analysis_service, ingest_service

app = FastAPI(title="Индексация и анализ документов")
app.mount("/analysis", analysis_service.app)
app.mount("/", ingest_service.app)
